"""Step 7: YouTube scheduling uploads and an optional Instagram Reels stub."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List
from zoneinfo import ZoneInfo


YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def authenticate_youtube(workspace: str | Path) -> Any:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "google-api-python-client and google-auth-oauthlib are required "
            "for YouTube uploads"
        ) from exc

    workspace_path = Path(workspace)
    workspace_path.mkdir(parents=True, exist_ok=True)
    token_path = workspace_path / "youtube_token.json"
    client_secrets = Path("client_secrets.json")

    credentials = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(str(token_path), YOUTUBE_SCOPES)
    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            if not client_secrets.exists():
                raise FileNotFoundError("client_secrets.json")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_secrets),
                YOUTUBE_SCOPES,
            )
            credentials = flow.run_local_server(port=0)
        token_path.write_text(credentials.to_json(), encoding="utf-8")

    return build("youtube", "v3", credentials=credentials)


def calculate_schedule_slots(
    count: int,
    start_from: datetime | None = None,
    timezone_name: str = "UTC",
) -> List[datetime]:
    if count <= 0:
        return []
    try:
        local_zone = ZoneInfo(timezone_name)
    except Exception:
        local_zone = timezone.utc

    now = start_from or datetime.now(local_zone)
    if now.tzinfo is None:
        now = now.replace(tzinfo=local_zone)
    else:
        now = now.astimezone(local_zone)

    slots: List[datetime] = []
    day = now.date()
    while len(slots) < count:
        for hour in (13, 20):
            slot = now.replace(year=day.year, month=day.month, day=day.day,
                               hour=hour, minute=0, second=0, microsecond=0)
            if slot > now:
                slots.append(slot)
                if len(slots) == count:
                    break
        day += timedelta(days=1)
    return slots


def _iso8601_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def upload_approved_clips(state: Dict[str, Any], workspace: str | Path,
                          dry_run: bool = False) -> List[Dict[str, Any]]:
    clips = state.get("clips", [])
    if not clips:
        return []

    timezone_name = os.getenv("SCHEDULE_TIMEZONE", "UTC")
    slots = calculate_schedule_slots(len(clips), timezone_name=timezone_name)
    if dry_run:
        youtube = None
    else:
        youtube = authenticate_youtube(workspace)

    live_urls: List[Dict[str, Any]] = []
    for clip, slot in zip(clips, slots):
        clip_id = clip.get("id")
        path = Path(clip["path"])
        scheduled_for = _iso8601_utc(slot)
        video_id = ""

        if youtube is not None:
            try:
                from googleapiclient.http import MediaFileUpload
            except ImportError as exc:
                raise RuntimeError("google-api-python-client is required") from exc

            title = f"{clip.get('hook_title', 'Creator Short')} #Shorts"
            description = f"{clip.get('description', '')}\n{clip.get('caption', '')}\n#Shorts"
            body = {
                "snippet": {
                    "title": title,
                    "description": description,
                    "categoryId": "22",
                },
                "status": {
                    "privacyStatus": "private",
                    "publishAt": scheduled_for,
                },
            }
            media = MediaFileUpload(str(path), mimetype="video/mp4", resumable=True)
            request = youtube.videos().insert(
                part="snippet,status",
                body=body,
                media_body=media,
            )
            response = None
            while response is None:
                _, response = request.next_chunk()
            video_id = response["id"]

        url = f"https://youtube.com/shorts/{video_id}" if video_id else ""
        live_urls.append({
            "clip_id": clip_id,
            "url": url,
            "scheduled_for": scheduled_for,
            "submitted": bool(video_id),
        })
    state["live_urls"] = live_urls
    return live_urls


def publish_instagram_reel(
    instagram_account_id: str,
    access_token: str,
    video_url: str,
    caption: str,
) -> Dict[str, Any]:
    """Toggleable Instagram Graph API Reels interface stub."""
    if os.getenv("INSTAGRAM_REELS_ENABLED", "").lower() not in {"1", "true", "yes"}:
        raise RuntimeError("Instagram Reels publishing is disabled")

    import urllib.parse
    import urllib.request

    base = "https://graph.facebook.com/v20.0"
    create_url = f"{base}/{instagram_account_id}/media"
    create_data = urllib.parse.urlencode({
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "access_token": access_token,
    }).encode("utf-8")
    with urllib.request.urlopen(urllib.request.Request(create_url, data=create_data), timeout=30) as response:
        container = __import__("json").loads(response.read().decode("utf-8"))

    publish_url = f"{base}/{instagram_account_id}/media_publish"
    publish_data = urllib.parse.urlencode({
        "creation_id": container["id"],
        "access_token": access_token,
    }).encode("utf-8")
    with urllib.request.urlopen(urllib.request.Request(publish_url, data=publish_data), timeout=30) as response:
        return __import__("json").loads(response.read().decode("utf-8"))


def run(state: Dict[str, Any], workspace: str | Path) -> Dict[str, Any]:
    dry_run = os.getenv("YOUTUBE_DRY_RUN", "").lower() in {"1", "true", "yes"}
    upload_approved_clips(state, workspace, dry_run=dry_run)
    return state


def process(state: Dict[str, Any], workspace: str | Path) -> Dict[str, Any]:
    return run(state, workspace)
