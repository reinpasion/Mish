from __future__ import annotations

import base64
import json
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import user_data_dir
from ..win_utils import log

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


class GoogleSync:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or user_data_dir()
        self.creds_file = self.base_dir / "credentials.json"
        self.token_file = self.base_dir / "google_token.json"
        self._auth_lock = threading.Lock()
        self._auth_gen = 0
        self.state_file = self.base_dir / "google_synced.json"
        self._state = self._load_state()

    def _load_state(self) -> dict:
        try:
            if self.state_file.exists():
                return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {"events": {}, "notified_events": [], "notified_msgs": []}

    def _save_state(self):
        try:
            self.state_file.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        except Exception as e:
            log("google: state save failed:", repr(e))

    def disconnect(self):
        with self._auth_lock:
            self._auth_gen += 1
            self.token_file.unlink(missing_ok=True)

    def _persist_token(self, creds: Credentials, gen: int) -> bool:
        payload = creds.to_json()
        with self._auth_lock:
            if self._auth_gen != gen:
                return False
            self.token_file.write_text(payload, encoding="utf-8")
            return True

    def _get_creds(self, interactive: bool = False) -> Credentials | None:
        with self._auth_lock:
            gen = self._auth_gen
            try:
                saved = self.token_file.read_text(encoding="utf-8")
            except FileNotFoundError:
                saved = None
        creds = None
        if saved is not None:
            try:
                from google.oauth2.credentials import Credentials
            except ImportError as error:
                log("google: credentials library unavailable:", repr(error))
                return None
            creds = Credentials.from_authorized_user_info(json.loads(saved), SCOPES)
        if creds and creds.expired and creds.refresh_token:
            try:
                from google.auth.transport.requests import Request

                creds.refresh(Request())
                if not self._persist_token(creds, gen):
                    return None
            except Exception as e:
                with self._auth_lock:
                    if self._auth_gen != gen:
                        return None
                log("google: token refresh failed:", repr(e))
                creds = None
        with self._auth_lock:
            if self._auth_gen != gen:
                return None
        if not creds or not creds.valid:
            if not interactive or not self.creds_file.exists():
                return None
            from google_auth_oauthlib.flow import InstalledAppFlow

            flow = InstalledAppFlow.from_client_secrets_file(str(self.creds_file), SCOPES)
            creds = flow.run_local_server(port=0)
            if not self._persist_token(creds, gen):
                return None
        return creds

    def authorize_interactive(self) -> tuple[bool, str]:
        if not self.creds_file.exists():
            return False, f"Put Google OAuth 'credentials.json' in {self.base_dir} first."
        try:
            ok = self._get_creds(interactive=True) is not None
            return ok, ("Google connected!" if ok else "Google sign-in cancelled.")
        except Exception as e:
            log("google: interactive auth failed:", repr(e))
            return False, f"Google auth error: {e}"

    def sync_calendar_deadlines(self, store, days_ahead: int = 21) -> list[str]:
        creds = self._get_creds(interactive=False)
        if not creds:
            return []
        try:
            from googleapiclient.discovery import build

            cal = build("calendar", "v3", credentials=creds, cache_discovery=False)
            now = datetime.now(timezone.utc)
            res = cal.events().list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=(now + timedelta(days=days_ahead)).isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=100,
            ).execute()
        except Exception as e:
            log("google: calendar fetch failed:", repr(e))
            return []

        added: list[str] = []
        seen = self._state.setdefault("events", {})
        for ev in res.get("items", []):
            ev_id = ev.get("id")
            summary = (ev.get("summary") or "Calendar Event").strip()
            if not ev_id or ev_id in seen:
                continue

            start = ev.get("start", {})
            raw_dt = start.get("dateTime") or start.get("date")
            if not raw_dt:
                continue
            if "T" in raw_dt:
                due_dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
            else:
                d = datetime.fromisoformat(raw_dt).date()
                due_dt = datetime.combine(d, datetime.max.time().replace(microsecond=0))

            title = f"[GCal] {summary}"
            task = store.add_task(title, due_dt)
            seen[ev_id] = task["id"] if task else "existing"
            if task:
                added.append(title)

        self._save_state()
        return added

    def check_imminent_events(self, ui_queue, minutes_ahead: int = 15):
        creds = self._get_creds(interactive=False)
        if not creds:
            return
        try:
            from googleapiclient.discovery import build

            cal = build("calendar", "v3", credentials=creds, cache_discovery=False)
            now = datetime.now(timezone.utc)
            res = cal.events().list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=(now + timedelta(minutes=minutes_ahead)).isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute()
        except Exception as e:
            log("google: imminent check failed:", repr(e))
            return

        notified = self._state.setdefault("notified_events", [])
        for ev in res.get("items", []):
            ev_id = ev.get("id")
            start_dt = ev.get("start", {}).get("dateTime")
            if not ev_id or not start_dt or ev_id in notified:
                continue
            mins = max(1, int((datetime.fromisoformat(start_dt.replace("Z", "+00:00")) - now).total_seconds() // 60))
            summary = ev.get("summary", "Upcoming event")
            ui_queue.put(("say", f"Heads up: '{summary}' starts in {mins}m.", "curious"))
            notified.append(ev_id)
        self._state["notified_events"] = notified[-100:]
        self._save_state()

    def poll_priority_gmail(self, ui_queue, query: str):
        creds = self._get_creds(interactive=False)
        if not creds or not query:
            return
        try:
            from googleapiclient.discovery import build

            gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
            msgs = gmail.users().messages().list(userId="me", q=query, maxResults=5).execute().get("messages", [])
            seen = self._state.setdefault("notified_msgs", [])
            for m in msgs:
                mid = m["id"]
                if mid in seen:
                    continue
                meta = gmail.users().messages().get(
                    userId="me", id=mid, format="metadata", metadataHeaders=["From", "Subject"]
                ).execute()
                headers = {h["name"]: h["value"] for h in meta.get("payload", {}).get("headers", [])}
                sender = headers.get("From", "Someone").split("<")[0].strip().strip('"')
                subj = headers.get("Subject", "(no subject)")
                ui_queue.put(("say", f"Priority email from {sender}: {subj}", "surprised"))
                seen.append(mid)
            self._state["notified_msgs"] = seen[-200:]
            self._save_state()
        except Exception as e:
            log("google: gmail poll failed:", repr(e))

    def send_email(self, to_addr: str, subject: str, body: str) -> bool:
        creds = self._get_creds(interactive=False)
        if not creds:
            return False
        try:
            from googleapiclient.discovery import build

            gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
            msg = EmailMessage()
            msg["To"] = to_addr
            msg["Subject"] = subject
            msg.set_content(body)
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
            gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
            return True
        except Exception as e:
            log("google: send_email failed:", repr(e))
            return False