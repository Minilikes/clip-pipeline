"""Steps 3-4: timestamped transcription and viral-moment compliance analysis."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

try:
    from pydantic import BaseModel, ConfigDict, Field
except ImportError:
    BaseModel = None
    ConfigDict = None
    Field = None


if BaseModel is not None:
    class CandidateMoment(BaseModel):
        model_config = ConfigDict(extra="forbid")
        id: int
        start_sec: float
        end_sec: float
        hook_title: str
        caption: str
        description: str

    class CandidateMoments(BaseModel):
        model_config = ConfigDict(extra="forbid")
        clips: list[CandidateMoment] = Field(min_length=6, max_length=10)

    class ComplianceVerdict(BaseModel):
        model_config = ConfigDict(extra="forbid")
        approved: bool
        reasons: list[str] = Field(default_factory=list)
else:
    class CandidateMoment:
        def __init__(self, id: int, start_sec: float, end_sec: float,
                     hook_title: str, caption: str, description: str, **_: Any):
            self.id, self.start_sec, self.end_sec = id, start_sec, end_sec
            self.hook_title, self.caption, self.description = hook_title, caption, description

    class CandidateMoments:
        def __init__(self, clips: list[CandidateMoment]):
            self.clips = clips

    class ComplianceVerdict:
        def __init__(self, approved: bool, reasons: list[str] | None = None):
            self.approved = approved
            self.reasons = reasons or []


def _load_transcript_cache(transcript_path: Path) -> Dict[str, Any] | None:
    if not transcript_path.exists():
        return None
    try:
        data = json.loads(transcript_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("segments"), list):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _source_fingerprint(source_path: Path) -> str:
    digest = hashlib.sha256()
    with source_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def transcribe_video(source_path: Path, transcript_path: Path) -> Dict[str, Any]:
    cached = _load_transcript_cache(transcript_path)
    if cached is not None:
        cache_fingerprint = cached.get("source_fingerprint")
        if cache_fingerprint is None:
            print("Transcript: reusing workspace/transcript.json")
            return cached
        source_fingerprint = _source_fingerprint(source_path)
        if cache_fingerprint == source_fingerprint:
            print("Transcript: reusing workspace/transcript.json")
            return cached

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("faster-whisper is required for transcription") from exc

    model = None
    try:
        model = WhisperModel("base", device="cuda", compute_type="float16")
    except Exception:
        model = WhisperModel("base", device="cpu", compute_type="int8")

    segments_out: List[Dict[str, Any]] = []
    words_out: List[Dict[str, Any]] = []

    segments, info = model.transcribe(
        str(source_path),
        word_timestamps=True,
        vad_filter=True,
    )
    for segment in segments:
        segment_words = []
        if getattr(segment, "words", None):
            for word in segment.words:
                item = {
                    "word": getattr(word, "word", ""),
                    "start": float(getattr(word, "start", 0.0)),
                    "end": float(getattr(word, "end", 0.0)),
                }
                segment_words.append(item)
                words_out.append(dict(item))
        segments_out.append({
            "start": float(segment.start),
            "end": float(segment.end),
            "text": segment.text.strip(),
            "words": segment_words,
        })

    transcript = {
        "source_fingerprint": _source_fingerprint(source_path),
        "language": getattr(info, "language", "en"),
        "language_probability": getattr(info, "language_probability", None),
        "segments": segments_out,
        "words": words_out,
    }
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Transcript: saved to workspace/transcript.json")
    return transcript


def _all_words(transcript: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    words = transcript.get("words")
    if isinstance(words, list):
        return [word for word in words if isinstance(word, Mapping)]
    result: List[Mapping[str, Any]] = []
    for segment in transcript.get("segments", []):
        result.extend(segment.get("words", []))
    return result


def snap_clip_boundaries(
    start_sec: float,
    end_sec: float,
    words: Iterable[Mapping[str, Any]],
) -> tuple[float, float]:
    word_list = list(words)
    if not word_list:
        return float(start_sec), float(end_sec)

    before = [w for w in word_list if float(w["start"]) <= start_sec]
    after = [w for w in word_list if float(w["end"]) >= end_sec]
    snapped_start = float(before[-1]["start"]) if before else float(word_list[0]["start"])
    snapped_end = float(after[0]["end"]) if after else float(word_list[-1]["end"])
    return snapped_start, snapped_end


def _candidate_to_dict(candidate: Any) -> Dict[str, Any]:
    if hasattr(candidate, "model_dump"):
        return candidate.model_dump()
    if isinstance(candidate, Mapping):
        return dict(candidate)
    return {
        "id": getattr(candidate, "id"),
        "start_sec": getattr(candidate, "start_sec"),
        "end_sec": getattr(candidate, "end_sec"),
        "hook_title": getattr(candidate, "hook_title"),
        "caption": getattr(candidate, "caption"),
        "description": getattr(candidate, "description"),
    }


def _inject_required_text(candidate: Dict[str, Any], rules: Mapping[str, Any]) -> Dict[str, Any]:
    caption = str(candidate.get("caption", "")).strip()
    description = str(candidate.get("description", "")).strip()

    for hashtag in rules.get("required_hashtags", []):
        if hashtag and hashtag not in caption:
            caption = f"{caption} {hashtag}".strip()
        if hashtag and hashtag not in description:
            description = f"{description} {hashtag}".strip()

    for disclosure in rules.get("required_disclosures", []):
        if disclosure and disclosure not in caption:
            caption = f"{caption} {disclosure}".strip()
        if disclosure and disclosure not in description:
            description = f"{description} {disclosure}".strip()

    credit = str(rules.get("creator_credit_format", "")).strip()
    if credit:
        if credit not in caption:
            caption = f"{caption} {credit}".strip()
        if credit not in description:
            description = f"{description} {credit}".strip()

    candidate["caption"] = caption
    candidate["description"] = description
    return candidate


def _clip_transcript(transcript: Mapping[str, Any], start: float, end: float) -> str:
    segments = []
    for segment in transcript.get("segments", []):
        seg_start, seg_end = float(segment["start"]), float(segment["end"])
        if seg_end >= start and seg_start <= end:
            segments.append(str(segment.get("text", "")).strip())
    return " ".join(part for part in segments if part)


def _gemini_client():
    global genai, types
    if genai is None or types is None:
        try:
            from google import genai as genai_module
            from google.genai import types as types_module
        except ImportError as exc:
            raise RuntimeError("google-genai is required for moment analysis") from exc
        genai, types = genai_module, types_module
    return genai.Client(), types


def analyze_moments(transcript: Mapping[str, Any], rules: Mapping[str, Any]) -> List[Dict[str, Any]]:
    client, sdk_types = _gemini_client()
    prompt = (
        "From this timestamped transcript, select 6 to 10 self-contained viral moments. "
        "Each clip must last within the campaign duration bounds. Snap starts and ends exactly "
        "to word boundaries. Generate hook_title, platform caption, and description. "
        "Include every required hashtag, disclosure, and creator credit exactly where appropriate.\n\n"
        f"RULES:\n{json.dumps(dict(rules), ensure_ascii=False)}\n\n"
        f"TRANSCRIPT:\n{json.dumps(dict(transcript), ensure_ascii=False)}"
    )
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=sdk_types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=CandidateMoments,
        ),
    )
    parsed = getattr(response, "parsed", None)
    if parsed is None:
        parsed = json.loads(response.text)

    if hasattr(parsed, "clips"):
        raw_candidates = parsed.clips
    elif isinstance(parsed, Mapping):
        raw_candidates = parsed.get("clips", [])
    else:
        raw_candidates = getattr(parsed, "clips", [])

    words = _all_words(transcript)
    min_duration = float(rules.get("min_duration_sec", 20))
    max_duration = float(rules.get("max_duration_sec", 58))
    candidates: List[Dict[str, Any]] = []

    for index, raw in enumerate(raw_candidates, start=1):
        candidate = _candidate_to_dict(raw)
        candidate["id"] = candidate.get("id", index)
        start, end = snap_clip_boundaries(
            float(candidate["start_sec"]),
            float(candidate["end_sec"]),
            words,
        )
        duration = end - start
        if duration < min_duration or duration > max_duration:
            continue
        candidate["start_sec"], candidate["end_sec"] = start, end
        candidate = _inject_required_text(candidate, rules)
        candidate["transcript"] = _clip_transcript(transcript, start, end)
        candidates.append(candidate)
        if len(candidates) == 10:
            break

    return candidates


def judge_compliance(
    candidate: Mapping[str, Any],
    rules: Mapping[str, Any],
) -> bool:
    client, sdk_types = _gemini_client()
    prompt = (
        "You are the Compliance Judge. Return whether this candidate is approved and list reasons. "
        "Reject it for banned topics/rules, missing required disclosures, or missing required hashtags. "
        "Also verify creator credit format and duration bounds.\n\n"
        f"BANNED/RULES:\n{json.dumps(rules.get('banned_topics_or_rules', []), ensure_ascii=False)}\n"
        f"DISCLOSURES:\n{json.dumps(rules.get('required_disclosures', []), ensure_ascii=False)}\n"
        f"HASHTAGS:\n{json.dumps(rules.get('required_hashtags', []), ensure_ascii=False)}\n"
        f"CREDIT:\n{rules.get('creator_credit_format', '')}\n"
        f"CANDIDATE:\n{json.dumps(dict(candidate), ensure_ascii=False)}"
    )
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=sdk_types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ComplianceVerdict,
        ),
    )
    parsed = getattr(response, "parsed", None)
    if parsed is None:
        parsed = json.loads(response.text)
    if hasattr(parsed, "approved"):
        return bool(parsed.approved)
    return bool(parsed.get("approved", False)) if isinstance(parsed, Mapping) else False


def run(state: Dict[str, Any], workspace: str | Path) -> Dict[str, Any]:
    if not isinstance(state, dict):
        raise TypeError("state must be a dictionary")
    source = Path(workspace) / "source.mp4"
    transcript_path = Path(workspace) / "transcript.json"
    if not source.exists():
        raise FileNotFoundError(source)
    if "rules" not in state:
        raise ValueError("state['rules'] is required")

    transcript = transcribe_video(source, transcript_path)
    candidates = analyze_moments(transcript, state["rules"])
    passing = [
        candidate for candidate in candidates
        if judge_compliance(candidate, state["rules"])
    ]
    state["candidates"] = passing
    print(f"Compliance: {len(passing)} of {len(candidates)} candidates passed")
    return state


def process(state: Dict[str, Any], workspace: str | Path) -> Dict[str, Any]:
    return run(state, workspace)
