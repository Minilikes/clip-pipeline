"""Human-in-the-loop entry point for the 9-step clip pipeline."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args: Any, **kwargs: Any) -> bool:
        return False

from modules.brief_downloader import run as brief_downloader
from modules.transcriber_analyzer import run as transcriber_analyzer
from modules.video_renderer import run as video_renderer
from modules.social_uploader import run as social_uploader
from modules.payout_watcher import run as payout_watcher


WORKSPACE = Path("workspace")
STATE_PATH = WORKSPACE / "pipeline_state.json"


def load_state() -> Dict[str, Any]:
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                return state
        except (json.JSONDecodeError, OSError):
            pass
    return {"current_step": 1}


def save_state(state: Dict[str, Any]) -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def prompt_campaign() -> Dict[str, str]:
    print("Step 1 - campaign setup")
    source_url = input("Authorized source video URL: ").strip()
    while not source_url:
        source_url = input("Authorized source video URL (required): ").strip()

    print("Paste the campaign brief rules. Type END on a separate line to finish:")
    brief_lines = []
    while True:
        line = input()
        if line.strip() == "END":
            break
        brief_lines.append(line)
    brief = "\n".join(brief_lines).strip()
    if not brief:
        raise ValueError("Campaign brief rules are required")

    campaign_name = input("Campaign name (optional): ").strip()
    return {
        "source_url": source_url,
        "brief": brief,
        "campaign_name": campaign_name or "Campaign",
    }


def approve_clips(state: Dict[str, Any]) -> Dict[str, Any]:
    rendered_clips = state.get("clips", [])
    if not rendered_clips:
        raise RuntimeError("No rendered clips are available for approval")

    print("\nStep 6 - clip approval")
    for clip in rendered_clips:
        clip_id = str(clip.get("id"))
        print(f"\n[{clip_id}] {clip.get('hook_title', '')}")
        print(f"  File: {clip.get('path', '')}")
        print(f"  Time: {clip.get('start_sec')}-{clip.get('end_sec')} sec")
        print(f"  Caption: {clip.get('caption', '')}")
        print(f"  Description: {clip.get('description', '')}")

    choice = input("Approve all clips? Enter y, or comma-separated clip IDs: ").strip()
    if choice.lower() == "y":
        selected_ids = {str(clip.get("id")) for clip in rendered_clips}
    else:
        selected_ids = {item.strip() for item in choice.split(",") if item.strip()}

    approved = [clip for clip in rendered_clips if str(clip.get("id")) in selected_ids]
    if not approved:
        raise RuntimeError("No clips were selected")

    state["selected_clip_ids"] = [clip["id"] for clip in approved]
    state["clips"] = approved
    print(f"Approved {len(approved)} clip(s): {', '.join(map(str, state['selected_clip_ids']))}")
    return state


def submit_live_urls(state: Dict[str, Any]) -> Dict[str, Any]:
    try:
        import pyperclip
    except ImportError:
        pyperclip = None

    live_urls = state.get("live_urls", [])
    pending = [item for item in live_urls if not item.get("submitted")]
    if not pending:
        print("Step 8 - no unsubmitted YouTube URLs")
        return state

    print("\nStep 8 - submit clips to Vyro")
    for item in pending:
        url = item.get("url", "")
        if not url:
            continue
        print(f"\nClip {item.get('clip_id')}: {url}")
        if pyperclip is not None:
            try:
                pyperclip.copy(url)
                print("Copied to clipboard.")
            except Exception:
                print("Clipboard unavailable; copy the URL above manually.")
        else:
            print("pyperclip unavailable; copy the URL above manually.")

        input("Submit this URL on Vyro, then press Enter to confirm: ")
        item["submitted"] = True
        save_state(state)
    return state


def advance(state: Dict[str, Any], step: int) -> None:
    state["current_step"] = step
    save_state(state)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the clip campaign pipeline")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear workspace/pipeline_state.json and start a new campaign",
    )
    args = parser.parse_args()

    load_dotenv()
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    os.environ["WORKSPACE"] = str(WORKSPACE)

    if args.reset and STATE_PATH.exists():
        STATE_PATH.unlink()
        print("Reset pipeline state.")

    state = load_state()
    current_step = int(state.get("current_step", 1))

    if current_step <= 1:
        state.update(prompt_campaign())
        advance(state, 2)

    if current_step <= 2:
        state = brief_downloader(state, WORKSPACE)
        advance(state, 3)

    if current_step <= 4:
        state = transcriber_analyzer(state, WORKSPACE)
        advance(state, 5)

    if current_step <= 5:
        state = video_renderer(state, WORKSPACE)
        advance(state, 6)

    if current_step <= 6:
        state = approve_clips(state)
        advance(state, 7)

    if current_step <= 7:
        state = social_uploader(state, WORKSPACE)
        advance(state, 8)

    if current_step <= 8:
        state = submit_live_urls(state)
        advance(state, 9)

    if current_step <= 9:
        state = payout_watcher(state, WORKSPACE)
        advance(state, 10)

    save_state(state)
    print("\nPipeline complete (saved to workspace/pipeline_state.json).")


if __name__ == "__main__":
    main()
