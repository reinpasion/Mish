from __future__ import annotations

import json
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
import icalendar
import requests

from ..config import user_data_dir
from ..win_utils import log


def _to_local_dt(dt_val) -> datetime | None:
    """Convert an iCal DATE or DATETIME into a naive local datetime matching Mish's Store."""
    if dt_val is None:
        return None
    if isinstance(dt_val, datetime):
        if dt_val.tzinfo is not None:
            return dt_val.astimezone().replace(tzinfo=None)
        return dt_val
    if isinstance(dt_val, date):
        return datetime.combine(dt_val, dt_time(23, 59))
    return None


class BlackboardSync:
    def __init__(self, ics_url: str, state_path: Path | None = None):
        self.ics_url = ics_url.strip()
        self.state_path = state_path or (user_data_dir() / "bb_synced.json")
        self._seen: dict[str, dict] = self._load_state()

    def _load_state(self) -> dict[str, dict]:
        try:
            if self.state_path.exists():
                return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as e:
            log("bb: failed to load sync state:", repr(e))
        return {}

    def _save_state(self):
        try:
            self.state_path.write_text(json.dumps(self._seen, indent=2), encoding="utf-8")
        except Exception as e:
            log("bb: failed to save sync state:", repr(e))

    def sync(self, store, days_ahead: int = 60) -> list[str]:
        if not self.ics_url:
            return []
        try:
            resp = requests.get(
                self.ics_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=15,
            )
            resp.raise_for_status()
            cal = icalendar.Calendar.from_ical(resp.content)
        except Exception as e:
            log("bb: fetch/parse failed:", repr(e))
            return []

        now = datetime.now()
        win_start = now - timedelta(hours=12)
        win_end = now + timedelta(days=days_ahead)

        added: list[str] = []
        pending_by_id = {t["id"]: t for t in store.pending()}

        for ev in cal.walk("VEVENT"):
            uid = str(ev.get("UID", "")).strip()
            summary = str(ev.get("SUMMARY", "Blackboard Deadline")).strip()
            dt_prop = ev.get("DUE") or ev.get("DTEND") or ev.get("DTSTART")
            due_dt = _to_local_dt(dt_prop.dt if dt_prop else None)
            if not uid or due_dt is None or not (win_start <= due_dt <= win_end):
                continue

            due_iso = due_dt.isoformat(timespec="seconds")
            title = f"[BB] {summary}"
            prev = self._seen.get(uid)
            if prev:
                task_obj = pending_by_id.get(prev.get("task_id"))
                if task_obj and task_obj.get("due") != due_iso:
                    store.reschedule_task(task_obj, due_iso)
                    self._seen[uid] = {"task_id": task_obj["id"], "due": due_iso}
                continue

            task = store.add_task(title, due_dt)
            self._seen[uid] = {"task_id": task["id"] if task else None, "due": due_iso}
            if task:
                added.append(title)

        self._save_state()
        return added