"""Step 2: download the source video and extract brief constraints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None
try:
    from pydantic import BaseModel, ConfigDict, Field

    class BriefRules(BaseModel):
        """Structured constraints extracted from a campaign brief."""

        model_config = ConfigDict(extra="forbid")
        min_duration_sec: int = 20
        max_duration_sec: int = 58
        allowed_platforms: list[str] = Field(default_factory=list)
        required_hashtags: list[str] = Field(default_factory=list)
        required_disclosures: list[str] = Field(default_factory=list)
        creator_credit_format: str = ""
        banned_topics_or_rules: list[str] = Field(default_factory=list)
        language: str = "en"
except ImportError:
    from dataclasses import dataclass, field

    @dataclass
    class BriefRules:
        """Import-compatible fallback; use Pydantic when dependencies are installed."""

        min_duration_sec: int = 20
        max_duration_sec: int = 58
        allowed_platforms: list[str] = field(default_factory=list)
        required_hashtags: list[str] = field(default_factory=list)
        required_disclosures: list[str] = field(default_factory=list)
        creator_credit_format: str = ""
        banned_topics_or_rules: list[str] = field(default_factory=list)
        language: str = "en"

        def model_dump(self) -> Dict[str, Any]:
            return {
                "min_duration_sec": self.min_duration_sec,
                "max_duration_sec": self.max_duration_sec,
                "allowed_platforms": self.allowed_platforms,
                "required_hashtags": self.required_hashtags,
                "required_disclosures": self.required_disclosures,
                "creator_credit_format": self.creator_credit_format,
                "banned_topics_or_rules": self.banned_topics_or_rules,
                "language": self.language,
            }

        @classmethod
        def model_validate(cls, value: Mapping[str, Any]) -> "BriefRules":
            return cls(**value)

        @classmethod
        def model_validate_json(cls, value: str) -> "BriefRules":
            return cls.model_validate(json.loads(value))


def _download_source_video(source_url: str, workspace: Path) -> None:
    global yt_dlp
    if yt_dlp is None:
        try:
            import yt_dlp as yt_dlp_module
        except ImportError as exc:
            raise RuntimeError("yt-dlp is required to download the source video") from exc
        yt_dlp = yt_dlp_module

    workspace.mkdir(parents=True, exist_ok=True)
    source_path = workspace / "source.mp4"
    source_url_marker = workspace / ".source-url.txt"

    downloaded_url = ""
    if source_url_marker.exists():
        downloaded_url = source_url_marker.read_text(encoding="utf-8").strip()

    if source_path.exists():
        if downloaded_url == source_url:
            print("Source video: reusing workspace/source.mp4")
            return

    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4",
        "outtmpl": str(source_path),
        "merge_output_format": "mp4",
        "noplaylist": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([source_url])

    source_url_marker.write_text(source_url, encoding="utf-8")
    print("Source video: downloaded to workspace/source.mp4")


def _parse_brief(brief: str) -> Dict[str, Any]:
    global genai, types
    if genai is None or types is None:
        try:
            from google import genai as genai_module
            from google.genai import types as types_module
        except ImportError as exc:
            raise RuntimeError("google-genai is required to parse the campaign brief") from exc
        genai = genai_module
        types = types_module

    client = genai.Client()
    prompt = (
        "Extract the campaign constraints from the following brief. "
        "Use the defaults only when the brief does not specify a value: "
        "minimum duration 20 seconds, maximum duration 58 seconds, and language 'en'. "
        "Normalize platforms and hashtags as lists of strings. "
        "If the brief has no applicable value, use an empty list or empty string.\n\n"
        f"BRIEF:\n{brief}"
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=BriefRules,
        ),
    )

    parsed = getattr(response, "parsed", None)
    if parsed is None:
        text = getattr(response, "text", None)
        if not text:
            raise ValueError("Gemini returned no structured brief response")
        parsed = json.loads(text)

    if isinstance(parsed, BriefRules):
        rules = parsed
    elif isinstance(parsed, Mapping):
        rules = BriefRules.model_validate(parsed)
    else:
        rules = BriefRules.model_validate_json(str(parsed))

    return rules.model_dump()


def _format_list(values: Iterable[str]) -> str:
    items = list(values)
    return ", ".join(items) if items else "-"


def _print_rules_summary(rules: Mapping[str, Any]) -> None:
    rows = [
        ("Duration", f"{rules.get('min_duration_sec', 20)}-{rules.get('max_duration_sec', 58)} sec"),
        ("Platforms", _format_list(rules.get("allowed_platforms", []))),
        ("Required hashtags", _format_list(rules.get("required_hashtags", []))),
        ("Disclosures", _format_list(rules.get("required_disclosures", []))),
        ("Creator credit", rules.get("creator_credit_format") or "-"),
        ("Banned topics/rules", _format_list(rules.get("banned_topics_or_rules", []))),
        ("Language", rules.get("language", "en")),
    ]

    label_width = max(len("Constraint"), *(len(label) for label, _ in rows))
    value_width = max(len("Value"), *(len(str(value)) for _, value in rows))
    print(f"\n{'Constraint':<{label_width}} | {'Value':<{value_width}}")
    print(f"{'-' * label_width}-+-{'-' * value_width}")
    for label, value in rows:
        print(f"{label:<{label_width}} | {str(value):<{value_width}}")


def run(state: Dict[str, Any], workspace: str | Path) -> Dict[str, Any]:
    """Download the source video and add extracted rules to the pipeline state."""
    if not isinstance(state, dict):
        raise TypeError("state must be a dictionary")

    source_url = state.get("source_url")
    if not isinstance(source_url, str) or not source_url.strip():
        raise ValueError("state['source_url'] is required")

    brief = state.get("brief")
    if not isinstance(brief, str) or not brief.strip():
        raise ValueError("state['brief'] is required")

    workspace_path = Path(workspace)
    _download_source_video(source_url.strip(), workspace_path)
    rules = _parse_brief(brief)
    state["rules"] = rules
    _print_rules_summary(rules)
    return state


def process(state: Dict[str, Any], workspace: str | Path) -> Dict[str, Any]:
    return run(state, workspace)


def execute(state: Dict[str, Any], workspace: str | Path) -> Dict[str, Any]:
    return run(state, workspace)


def brief_downloader(state: Dict[str, Any], workspace: str | Path) -> Dict[str, Any]:
    return run(state, workspace)


def main(state: Dict[str, Any], workspace: str | Path) -> Dict[str, Any]:
    return run(state, workspace)
