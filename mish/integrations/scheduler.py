from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from ..config import user_data_dir
from ..win_utils import idle_seconds, log
from .blackboard import BlackboardSync
from .google_sync import GoogleSync
from .microsoft_sync import MicrosoftSync

_ACTIVE_HUB: AutomationHub | None = None


def _job_sync_deadlines():
    if _ACTIVE_HUB is not None:
        _ACTIVE_HUB.sync_all_deadlines()


def _job_poll_alerts():
    if _ACTIVE_HUB is not None:
        _ACTIVE_HUB.poll_alerts()


def _job_oversleep_failsafe():
    if _ACTIVE_HUB is not None:
        _ACTIVE_HUB._check_oversleep_failsafe()


def _job_scheduled_email(item: dict):
    if _ACTIVE_HUB is not None:
        _ACTIVE_HUB._run_scheduled_email(item)


class AutomationHub:
    def __init__(self, store, ui_queue):
        self.store = store
        self.ui = ui_queue
        self.base_dir = user_data_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.base_dir / "integrations.json"
        self.cfg = self._load_config()

        self.bb = BlackboardSync(self.cfg.get("blackboard_ics_url", ""), self.base_dir / "bb_synced.json")
        self.google = GoogleSync(self.base_dir)
        self.ms = MicrosoftSync(self.cfg.get("microsoft_client_id", ""), self.base_dir)

        self.sched = None
        self._started = False
        self._failsafe_fired_date = ""
        self._ms_action_lock = threading.Lock()
        self._ms_action_pending = False

    def _load_config(self) -> dict:
        if not self.cfg_path.exists():
            default = {
                "blackboard_ics_url": "",
                "microsoft_client_id": "",
                "gmail_query": "is:unread newer_than:1d (urgent OR deadline OR exam OR assignment)",
                "priority_keywords": ["urgent", "deadline", "exam", "assignment"],
                "scheduled_emails": [],
                "oversleep_failsafe": {
                    "enabled": False,
                    "warn_hour": 7,
                    "warn_minute": 30,
                    "fire_hour": 8,
                    "fire_minute": 0,
                    "idle_threshold_sec": 1800,
                    "provider": "gmail",
                    "to": "",
                    "subject": "Running late / unreachable",
                    "body": "Automated alert from Mish: I haven't checked in this morning and may have overslept.",
                },
            }
            self.cfg_path.write_text(json.dumps(default, indent=2), encoding="utf-8")
            return default
        return json.loads(self.cfg_path.read_text(encoding="utf-8"))

    def save_config(self):
        self.cfg_path.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")

    def set_blackboard_url(self, url: str):
        self.cfg["blackboard_ics_url"] = url.strip()
        self.bb.ics_url = url.strip()
        self.save_config()

    def set_ms_client_id(self, client_id: str):
        self.cfg["microsoft_client_id"] = client_id.strip()
        self.ms.configure(client_id.strip())
        self.save_config()

    def open_config_file(self):
        try:
            os.startfile(str(self.cfg_path))
        except Exception as e:
            log("hub: open_config_file failed:", repr(e))

    def status(self) -> dict:
        """Describe locally saved sign-ins without invoking either auth client."""
        return {
            "bb_url": self.bb.ics_url,
            "bb_connected": bool(self.bb.ics_url),
            "bb_count": len(self.bb._seen),
            "google_has_creds": self.google.creds_file.exists(),
            "google_connected": self.google.token_file.is_file(),
            "ms_client_id": self.ms.client_id,
            "ms_connected": bool(self.ms.client_id and self.ms.cache_file.is_file()),
        }

    def install_google_creds(self, src_path: str) -> tuple[bool, str]:
        try:
            data = json.loads(Path(src_path).read_text(encoding="utf-8"))
            if "installed" not in data and "web" not in data:
                return False, "Not a valid Google OAuth client JSON."
            self.google.creds_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return True, "Saved credentials.json!"
        except Exception as e:
            return False, f"Invalid JSON: {e}"

    def disconnect_google(self):
        self.google.disconnect()

    def disconnect_microsoft(self):
        self.ms.disconnect()

    def start(self):
        if self._started:
            return
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
        from apscheduler.schedulers.background import BackgroundScheduler

        db_url = f"sqlite:///{(self.base_dir / 'mish_jobs.sqlite').as_posix()}"
        self.sched = BackgroundScheduler(
            jobstores={"default": SQLAlchemyJobStore(url=db_url)},
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
        )

        self.sched.add_job(_job_sync_deadlines, "interval", minutes=15, id="sync_deadlines", replace_existing=True)
        self.sched.add_job(_job_poll_alerts, "interval", minutes=5, id="poll_alerts", replace_existing=True)
        self.sched.add_job(_job_oversleep_failsafe, "interval", minutes=1, id="oversleep_guard", replace_existing=True)

        for item in self.cfg.get("scheduled_emails", []):
            self.sched.add_job(
                _job_scheduled_email,
                "cron",
                id=f"mail:{item['id']}",
                replace_existing=True,
                kwargs={"item": item},
                **item["cron"],
            )

        global _ACTIVE_HUB
        _ACTIVE_HUB = self
        try:
            self.sched.start()
            self._started = True
            self.sched.add_job(_job_sync_deadlines, "date", run_date=datetime.now().astimezone())
        except Exception:
            self.shutdown()
            raise

    def shutdown(self):
        global _ACTIVE_HUB
        try:
            if self.sched is not None and self.sched.running:
                self.sched.shutdown(wait=False)
                # The scheduler thread has joined. Dispose any SQLite connection opened on its final wake.
                self.sched.remove_jobstore("default")
        except Exception:
            pass
        if _ACTIVE_HUB is self:
            _ACTIVE_HUB = None
        self._started = False

    def sync_now_async(self, announce_empty: bool = True):
        def work():
            added = self.sync_all_deadlines(silent=True)
            if added:
                preview = ", ".join(added[:2]) + (f" (+{len(added) - 2} more)" if len(added) > 2 else "")
                self.ui.put(("say", f"Synced {len(added)} new deadline(s): {preview}", "happy"))
            elif announce_empty:
                self.ui.put(("say", "All Blackboard, Google & Microsoft deadlines are up to date.", "neutral"))
        threading.Thread(target=work, daemon=True).start()

    def connect_google_async(self):
        def work():
            ok, msg = self.google.authorize_interactive()
            self.ui.put(("say", msg, "happy" if ok else "sad"))
            if ok:
                self.sync_all_deadlines()
        threading.Thread(target=work, daemon=True).start()

    def _start_ms_action(self, action) -> bool:
        with self._ms_action_lock:
            if self._ms_action_pending:
                return False
            self._ms_action_pending = True

        def run():
            try:
                action()
            except Exception as error:
                log("hub: Microsoft account action failed:", repr(error))
                self.ui.put(("say", "Microsoft account action failed.", "sad"))
            finally:
                with self._ms_action_lock:
                    self._ms_action_pending = False

        try:
            threading.Thread(target=run, daemon=True).start()
        except Exception:
            with self._ms_action_lock:
                self._ms_action_pending = False
            raise
        return True

    def connect_microsoft_async(self, client_id: str | None = None) -> bool:
        def work():
            if client_id is not None:
                self.set_ms_client_id(client_id)
            ok, msg = self.ms.authorize_interactive()
            self.ui.put(("say", msg, "happy" if ok else "sad"))
            if ok:
                self.sync_all_deadlines()
        return self._start_ms_action(work)

    def disconnect_microsoft_async(self) -> bool:
        def work():
            self.disconnect_microsoft()
            self.ui.put(("say", "Microsoft disconnected.", "neutral"))
        return self._start_ms_action(work)

    def sync_all_deadlines(self, silent: bool = False) -> list[str]:
        added: list[str] = []
        added.extend(self.bb.sync(self.store))
        added.extend(self.google.sync_calendar_deadlines(self.store))
        added.extend(self.ms.sync_deadlines(self.store))
        if added and not silent:
            preview = ", ".join(added[:2]) + (f" (+{len(added) - 2} more)" if len(added) > 2 else "")
            self.ui.put(("say", f"Synced {len(added)} new deadline(s): {preview}", "curious"))
        return added

    def poll_alerts(self):
        self.google.check_imminent_events(self.ui, minutes_ahead=15)
        self.google.poll_priority_gmail(self.ui, self.cfg.get("gmail_query", ""))
        self.ms.poll_priority_mail(self.ui, tuple(self.cfg.get("priority_keywords", [])))

    def send_email_now(self, provider: str, to_addr: str, subject: str, body: str) -> bool:
        p = provider.strip().lower()
        if p in ("ms", "microsoft", "outlook"):
            return self.ms.send_email(to_addr, subject, body)
        return self.google.send_email(to_addr, subject, body)

    def _run_scheduled_email(self, item: dict):
        ok = self.send_email_now(item.get("provider", "gmail"), item["to"], item["subject"], item["body"])
        log(f"scheduled email '{item.get('id', 'unnamed')}': ok={ok}")

    def _check_oversleep_failsafe(self):
        fs = self.cfg.get("oversleep_failsafe", {})
        if not fs.get("enabled") or not fs.get("to"):
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if self._failsafe_fired_date == today:
            return

        idle = idle_seconds()
        thresh = fs.get("idle_threshold_sec", 1800)

        if now.hour == fs.get("warn_hour") and now.minute == fs.get("warn_minute") and idle >= thresh:
            self.ui.put(("say", "Morning check-in! Move your mouse or I'll send your oversleep alert.", "scared"))

        if now.hour == fs.get("fire_hour") and now.minute == fs.get("fire_minute") and idle >= thresh:
            self._failsafe_fired_date = today
            self._run_scheduled_email(fs)
            self.ui.put(("say", "Oversleep failsafe triggered — sent your preset message.", "sad"))