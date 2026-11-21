from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import user_data_dir
from ..win_utils import log

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
MS_SCOPES = ["User.Read", "Calendars.Read", "Tasks.Read", "Mail.Read", "Mail.Send"]


class MicrosoftSync:
    def __init__(self, client_id: str, base_dir: Path | None = None):
        self.base_dir = base_dir or user_data_dir()
        self.cache_file = self.base_dir / "ms_token.json"
        self.state_file = self.base_dir / "ms_synced.json"
        self.client_id = (client_id or "").strip()
        self._cache = None
        self._app = None
        self._client_ready = threading.Condition()
        self._client_busy = False
        self._state = self._load_state()

    @contextmanager
    def _client_operation(self):
        with self._client_ready:
            self._client_ready.wait_for(lambda: not self._client_busy)
            self._client_busy = True
        try:
            yield
        finally:
            with self._client_ready:
                self._client_busy = False
                self._client_ready.notify_all()

    def configure(self, client_id: str):
        with self._client_operation():
            client_id = (client_id or "").strip()
            if client_id != self.client_id:
                self.client_id = client_id
                self._app = None
                self._cache = None

    def disconnect(self):
        with self._client_operation():
            self.cache_file.unlink(missing_ok=True)
            self._app = None
            self._cache = None

    def _init_app(self):
        from msal import PublicClientApplication, SerializableTokenCache

        cache = SerializableTokenCache()
        if self.cache_file.is_file():
            cache.deserialize(self.cache_file.read_text(encoding="utf-8"))
        app = PublicClientApplication(
            self.client_id,
            authority="https://login.microsoftonline.com/common",
            token_cache=cache,
        )
        self._cache, self._app = cache, app

    def _load_state(self) -> dict:
        try:
            if self.state_file.exists():
                return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {"items": {}, "notified_msgs": []}

    def _save_state(self):
        try:
            self.state_file.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        except Exception as e:
            log("ms: save failed:", repr(e))

    def _token(self, interactive: bool = False) -> str | None:
        try:
            with self._client_operation():
                if not self.client_id:
                    return None
                if self._app is None:
                    if not interactive and not self.cache_file.is_file():
                        return None
                    self._init_app()
                accounts = self._app.get_accounts()
                res = self._app.acquire_token_silent(MS_SCOPES, account=accounts[0]) if accounts else None
                if not res and interactive:
                    res = self._app.acquire_token_interactive(scopes=MS_SCOPES)
                if self._cache.has_state_changed:
                    try:
                        self.cache_file.write_text(self._cache.serialize(), encoding="utf-8")
                    except Exception as error:
                        log("ms: token save failed:", repr(error))
                return res.get("access_token") if res else None
        except Exception as error:
            if interactive:
                raise
            log("ms: silent auth failed:", repr(error))
            return None

    def authorize_interactive(self) -> tuple[bool, str]:
        if not self.client_id:
            return False, "Set 'microsoft_client_id' in integrations.json first (or use !msid <client_id>)."
        try:
            ok = self._token(interactive=True) is not None
            return ok, ("Microsoft connected!" if ok else "Microsoft sign-in cancelled.")
        except Exception as e:
            log("ms: interactive auth failed:", repr(e))
            return False, f"Microsoft auth error: {e}"

    def sync_deadlines(self, store, days_ahead: int = 21) -> list[str]:
        tok = self._token(interactive=False)
        if not tok:
            return []
        headers = {"Authorization": f"Bearer {tok}"}
        added: list[str] = []
        seen = self._state.setdefault("items", {})

        try:
            import requests
        except ImportError as error:
            log("ms: requests unavailable:", repr(error))
            return []
        try:
            now = datetime.now(timezone.utc)
            params = {
                "startDateTime": now.isoformat(),
                "endDateTime": (now + timedelta(days=days_ahead)).isoformat(),
                "$top": 50,
            }
            r = requests.get(f"{GRAPH_ROOT}/me/calendarView", headers=headers, params=params, timeout=15)
            r.raise_for_status()
            for ev in r.json().get("value", []):
                eid = f"ev:{ev['id']}"
                if eid in seen:
                    continue
                dt_str = ev.get("start", {}).get("dateTime")
                if not dt_str:
                    continue
                due_dt = datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
                title = f"[Outlook] {ev.get('subject', 'Event').strip()}"
                task = store.add_task(title, due_dt)
                seen[eid] = task["id"] if task else "existing"
                if task:
                    added.append(title)
        except Exception as e:
            log("ms: calendar sync failed:", repr(e))

        try:
            lists_r = requests.get(f"{GRAPH_ROOT}/me/todo/lists", headers=headers, timeout=15)
            lists_r.raise_for_status()
            for lst in lists_r.json().get("value", []):
                tr = requests.get(
                    f"{GRAPH_ROOT}/me/todo/lists/{lst['id']}/tasks",
                    headers=headers,
                    params={"$filter": "status ne 'completed'"},
                    timeout=15,
                )
                if not tr.ok:
                    continue
                for item in tr.json().get("value", []):
                    tid = f"todo:{item['id']}"
                    if tid in seen:
                        continue
                    due_obj = item.get("dueDateTime")
                    due_dt = (
                        datetime.fromisoformat(due_obj["dateTime"]).replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
                        if due_obj and due_obj.get("dateTime")
                        else None
                    )
                    title = f"[ToDo] {item.get('title', 'Task').strip()}"
                    task = store.add_task(title, due_dt)
                    seen[tid] = task["id"] if task else "existing"
                    if task:
                        added.append(title)
        except Exception as e:
            log("ms: todo sync failed:", repr(e))

        self._save_state()
        return added

    def poll_priority_mail(self, ui_queue, keywords=("urgent", "deadline", "exam", "assignment")):
        tok = self._token(interactive=False)
        if not tok:
            return
        try:
            import requests

            r = requests.get(
                f"{GRAPH_ROOT}/me/mailFolders/Inbox/messages",
                headers={"Authorization": f"Bearer {tok}"},
                params={"$filter": "isRead eq false", "$top": 10, "$select": "id,subject,from"},
                timeout=15,
            )
            r.raise_for_status()
            seen = self._state.setdefault("notified_msgs", [])
            for msg in r.json().get("value", []):
                mid = msg["id"]
                subj = msg.get("subject") or ""
                if mid in seen or not any(k in subj.lower() for k in keywords):
                    continue
                sender = msg.get("from", {}).get("emailAddress", {}).get("name", "Outlook")
                ui_queue.put(("say", f"Priority Outlook mail from {sender}: {subj}", "surprised"))
                seen.append(mid)
            self._state["notified_msgs"] = seen[-200:]
            self._save_state()
        except Exception as e:
            log("ms: mail poll failed:", repr(e))

    def send_email(self, to_addr: str, subject: str, body: str) -> bool:
        tok = self._token(interactive=False)
        if not tok:
            return False
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": to_addr}}],
            }
        }
        try:
            import requests

            r = requests.post(
                f"{GRAPH_ROOT}/me/sendMail",
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                json=payload,
                timeout=15,
            )
            return r.status_code in (200, 202)
        except Exception as e:
            log("ms: sendMail failed:", repr(e))
            return False