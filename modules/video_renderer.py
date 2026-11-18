"""Step 5: render vertical clips with punchy word-level ASS subtitles."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping


def _word_entries(transcript: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    words = transcript.get("words")
    if isinstance(words, list):
        return [word for word in words if isinstance(word, Mapping)]
    result: List[Mapping[str, Any]] = []
    for segment in transcript.get("segments", []):
        result.extend(segment.get("words", []))
    return result


def _group_words(words: Iterable[Mapping[str, Any]], clip_start: float, clip_end: float):
    selected = [
        word for word in words
        if float(word["end"]) > clip_start and float(word["start"]) < clip_end
    ]
    # 3 to 5 words per subtitle line, with shorter lines permitted at clip edges.
    index = 0
    while index < len(selected):
        size = min(5, len(selected) - index)
        if len(selected) - (index + size) == 1:
            size -= 1
        size = max(3, size)
        group = selected[index:index + size]
        index += size
        if not group:
            continue
        start = max(clip_start, float(group[0]["start"])) - clip_start
        end = min(clip_end, float(group[-1]["end"])) - clip_start
        text = " ".join(str(word["word"]).strip() for word in group).strip()
        yield max(0.0, start), max(0.0, end), text


def _ass_timestamp(seconds: float) -> str:
    centiseconds = max(0, int(round(seconds * 100)))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, hundredths = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{hundredths:02d}"


def generate_ass_subtitles(
    transcript: Mapping[str, Any],
    clip_start: float,
    clip_end: float,
    output_path: Path,
) -> Path:
    try:
        import pysubs2
        subs = pysubs2.SSAFile()
        style = pysubs2.SSAStyle(
            fontname="Arial",
            fontsize=72,
            bold=True,
            primarycolor=pysubs2.Color(255, 255, 255),
            outline=3,
            shadow=1,
            alignment=2,
            marginl=70,
            marginr=70,
            marginv=300,
        )
        subs.styles["Default"] = style
        for start, end, text in _group_words(_word_entries(transcript), clip_start, clip_end):
            subs.append(pysubs2.SSAEvent(
                start=pysubs2.make_time(s=start),
                end=pysubs2.make_time(s=end),
                text=text,
                style="Default",
            ))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        subs.save(str(output_path), format_="ass")
        return output_path
    except ImportError:
        # Portable fallback keeps rendering usable if pysubs2 is unavailable.
        header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,72,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,3,1,2,70,70,300,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        lines = [header]
        for start, end, text in _group_words(_word_entries(transcript), clip_start, clip_end):
            lines.append(
                f"Dialogue: 0,{_ass_timestamp(start)},{_ass_timestamp(end)},"
                f"Default,,0,0,0,,{{\\b1}}{text}"
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(lines), encoding="utf-8")
        return output_path


def _safe_clip_id(candidate_id: Any) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]", "_", str(candidate_id))
    return value or "1"


def _escape_subtitle_path(path: Path) -> str:
    return str(path).replace("\\", "/").replace(":", "\\:")


def render_clip(source: Path, ass_path: Path, output: Path,
                start_sec: float, end_sec: float) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.0, end_sec - start_sec)
    subtitle_path = _escape_subtitle_path(ass_path)
    vf = f"crop=ih*9/16:ih,scale=1080:1920,subtitles={subtitle_path}"
    command = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{start_sec:.3f}",
        "-i",
        str(source),
        "-t",
        f"{duration:.3f}",
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-crf",
        "20",
        "-preset",
        "fast",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(output),
    ]
    subprocess.run(command, check=True)


def run(state: Dict[str, Any], workspace: str | Path) -> Dict[str, Any]:
    if not isinstance(state, dict):
        raise TypeError("state must be a dictionary")
    workspace_path = Path(workspace)
    source = workspace_path / "source.mp4"
    transcript_path = workspace_path / "transcript.json"
    if not source.exists():
        raise FileNotFoundError(source)
    if not transcript_path.exists():
        raise FileNotFoundError(transcript_path)

    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    temp_subs = workspace_path / "temp_subs"
    approved = workspace_path / "approved_clips"
    temp_subs.mkdir(parents=True, exist_ok=True)
    approved.mkdir(parents=True, exist_ok=True)

    clips: List[Dict[str, Any]] = []
    for candidate in state.get("candidates", []):
        clip_id = candidate.get("id")
        safe_id = _safe_clip_id(clip_id)
        start = float(candidate["start_sec"])
        end = float(candidate["end_sec"])
        ass_path = temp_subs / f"clip_{safe_id}.ass"
        output = approved / f"clip_{safe_id}.mp4"
        generate_ass_subtitles(transcript, start, end, ass_path)
        render_clip(source, ass_path, output, start, end)
        clips.append({
            "id": clip_id,
            "path": str(output),
            "start_sec": start,
            "end_sec": end,
            "hook_title": candidate.get("hook_title", ""),
            "caption": candidate.get("caption", ""),
            "description": candidate.get("description", ""),
        })
    state["clips"] = clips
    print(f"Rendered {len(clips)} approved clip(s)")
    return state


def process(state: Dict[str, Any], workspace: str | Path) -> Dict[str, Any]:
    return run(state, workspace)
