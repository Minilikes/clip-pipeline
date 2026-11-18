"""Step 9: track submitted clips and inspect Vyro/Whop email alerts."""

from __future__ import annotations

import csv
import imaplib
import os
import re
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Dict, Iterable, List


TRACKER_FIELDS = [
    "clip_id",
    "url",
    "campaign_name",
    "timestamp",
    "status",
    "subject",
    "details",
]


def append_submitted_urls(state: Dict[str, Any], workspace: str | Path) -> Path:
    workspace_path = Path(workspace)
    workspace_path.mkdir(parents=True, exist_ok=True)
    tracker = workspace_path / "vyro_tracker.csv"
    campaign_name = str(state.get("campaign_name") or state.get("campaign") or "Campaign")

    existing: Dict[str, Dict[str, str]] = {}
    if tracker.exists():
        with tracker.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                existing[str(row.get("clip_id", ""))] = row

    for item in state.get("live_urls", []):
        if not item.get("submitted"):
            continue
        clip_id = str(item.get("clip_id", ""))
        url = str(item.get("url", "")).strip()
        if not clip_id or not url:
            continue
        row = existing.setdefault(clip_id, {
            "clip_id": clip_id,
            "url": url,
            "campaign_name": campaign_name,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": "submitted",
            "subject": "",
            "details": "",
        })
        row["url"] = url
        row["campaign_name"] = campaign_name

    with tracker.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRACKER_FIELDS)
        writer.writeheader()
        for row in existing.values():
            writer.writerow({field: row.get(field, "") for field in TRACKER_FIELDS})
    return tracker


def parse_alert(subject: str, body: str) -> Dict[str, str] | None:
    text = f"{subject}\n{body}".lower()
    if "payout" in text or "settled" in text or "settlement" in text:
        status = "paid"
    elif "rejected" in text or "not approved" in text or "declined" in text:
        status = "rejected"
    elif "approved" in text or "accepted" in text:
        status = "approved"
    else:
        return None

    amount_match = re.search(r"(?:\$|usd\s*)([0-9]+(?:\.[0-9]{2})?)", text)
    amount = amount_match.group(1) if amount_match else ""
    details = f"{status}" + (f"; payout_usd={amount}" if amount else "")
    return {"status": status, "details": details}


def _decode_message(raw_message: bytes) -> tuple[str, str]:
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    subject = str(message.get("subject", "")).strip()
    if message.is_multipart():
        body = "\n".join(
            part.get_content()
            for part in message.walk()
            if part.get_content_maintype() == "text"
            and part.get_content_subtype() == "plain"
            and isinstance(part.get_content(), str)
        )
    else:
        body = message.get_content() if isinstance(message.get_content(), str) else ""
    return subject, body


def _gmail_messages() -> List[tuple[str, str]]:
    """Read recent unread Vyro/Whop mail through Gmail if credentials are available."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        return []

    workspace = os.getenv("WORKSPACE", "workspace")
    token_path = Path(workspace) / "youtube_token.json"
    if not token_path.exists():
        return []
    scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
    try:
        credentials = Credentials.from_authorized_user_file(str(token_path), scopes)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
    except Exception:
        return []

    try:
        gmail = build("gmail", "v1", credentials=credentials)
        response = gmail.users().messages().list(
            userId="me",
            q="is:unread newer_than:30d (from:vyro OR from:whop)",
            maxResults=100,
        ).execute()
        messages = []
        for item in response.get("messages", []):
            message = gmail.users().messages().get(
                userId="me", id=item["id"], format="raw"
            ).execute()
            raw = message["raw"].replace("-", "+").replace("_", "/")
            import base64
            decoded = base64.b64decode(raw + "=" * (-len(raw) % 4))
            subject, body = _decode_message(decoded)
            messages.append((subject, body))
        return messages
    except Exception:
        return []


def _imap_messages() -> List[tuple[str, str]]:
    host = os.getenv("GMAIL_IMAP_HOST", "imap.gmail.com")
    username = os.getenv("GMAIL_USERNAME")
    app_password = os.getenv("GMAIL_APP_PASSWORD")
    if not username or not app_password:
        return []

    try:
        with imaplib.IMAP4_SSL(host) as mailbox:
            mailbox.login(username, app_password)
            mailbox.select("INBOX")
            for sender in ("vyro", "whop"):
                status, data = mailbox.search(None, "UNSEEN", f'(FROM "{sender}")')
                if status != "OK":
                    continue
                for message_id in (data[0] or b"").split():
                    status, message_data = mailbox.fetch(message_id, "(RFC822)")
                    if status != "OK" or not message_data:
                        continue
                    raw_message = message_data[0][1]
                    yield _decode_message(raw_message)
    except (imaplib.IMAP4.error, OSError):
        return


def _load_tracker(tracker: Path) -> Dict[str, Dict[str, str]]:
    if not tracker.exists():
        return {}
    with tracker.open("r", newline="", encoding="utf-8") as handle:
        return {
            str(row.get("clip_id", "")): row
            for row in csv.DictReader(handle)
        }


def _save_tracker(tracker: Path, rows: Iterable[Dict[str, str]]) -> None:
    with tracker.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRACKER_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in TRACKER_FIELDS})


def update_matching_records(tracker: Path, messages: Iterable[tuple[str, str]]) -> List[Dict[str, str]]:
    rows = _load_tracker(tracker)
    updates: List[Dict[str, str]] = []
    for subject, body in messages:
        alert = parse_alert(subject, body)
        if not alert:
            continue
        haystack = f"{subject}\n{body}".lower()
        matched = False
        for clip_id, row in rows.items():
            url = str(row.get("url", ""))
            video_id = url.rstrip("/").split("/")[-1].lower()
            if (clip_id and clip_id.lower() in haystack) or (
                video_id and video_id in haystack
            ):
                row["status"] = alert["status"]
                row["subject"] = subject
                row["details"] = alert["details"]
                matched = True
                updates.append(row)
        if not matched:
            updates.append({
                "clip_id": "",
                "url": "",
                "campaign_name": "",
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "status": alert["status"],
                "subject": subject,
                "details": alert["details"],
            })
    _save_tracker(tracker, rows.values())
    return updates


def print_alert_summary(updates: Iterable[Dict[str, str]]) -> None:
    updates = list(updates)
    if not updates:
        print("Payout Watcher: no matching new alerts")
        return
    print("\033[1;33mPayout Watcher Alerts\033[0m")
    for update in updates:
        clip = update.get("clip_id") or "unmatched"
        print(
            f"\033[1m{clip}\033[0m | {update.get('status', '-')} | "
            f"{update.get('details', '-')} | {update.get('subject', '-')}"
        )


def run(state: Dict[str, Any], workspace: str | Path) -> Dict[str, Any]:
    tracker = append_submitted_urls(state, workspace)
    messages = _gmail_messages()
    if not messages:
        messages = list(_imap_messages())
    updates = update_matching_records(tracker, messages)
    print_alert_summary(updates)
    state["payout_updates"] = updates
    return state


def process(state: Dict[str, Any], workspace: str | Path) -> Dict[str, Any]:
    return run(state, workspace)
