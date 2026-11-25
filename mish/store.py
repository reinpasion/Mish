"""Persistent memory with small, keyword-ranked snapshots for model context."""

from __future__ import annotations

import re
import time
from datetime import datetime

from .win_utils import Store as _PersistentStore

_WORD = re.compile(r"[^\W_]+")
_STOP_WORDS = frozenset("""
    about all also and any are been can could does for from good had has have hello hey how
    just like may me might mish morning much nice now only our out please say should
    some sup tell than that the their them then there these they this those through
    today too use very want was were what when where which who will with would yeah
    you your yours whats thats going evening afternoon greetings thanks thank
""".split())
_SOURCES = {
    "bb": "Blackboard", "blackboard": "Blackboard", "gcal": "Google Calendar",
    "google calendar": "Google Calendar", "outlook": "Outlook", "todo": "Microsoft To Do",
    "to do": "Microsoft To Do", "microsoft to do": "Microsoft To Do",
}


def _keywords(text: str) -> set[str]:
    return {word for word in _WORD.findall(text.casefold()) if len(word) >= 3 and word not in _STOP_WORDS}


def match_task_reference(reference: str, tasks: list[dict], *, in_context: bool = False) -> list[dict]:
    """Find unambiguous titles or return all candidates for a short, shared reference."""
    def normalize(text):
        return " ".join(_WORD.findall(text.casefold()))

    query = normalize(reference)
    if not query:
        return []

    titles = []
    for task in tasks:
        title = re.sub(r"^\[[^]]+\]\s*", "", task["text"])
        normalized = normalize(title)
        if normalized:
            titles.append((task, normalized))
    if in_context:
        return [task for task, title in titles if f" {title} " in f" {query} "]

    exact = [task for task, title in titles if title == query]
    if exact:
        return exact
    words = _keywords(query)
    return [task for task, title in titles
            if f" {query} " in f" {title} " or (words and words <= _keywords(title))]


def task_source(task: dict) -> str:
    source = str(task.get("source") or "").strip()
    text = task["text"].lstrip()
    prefix = text[1:text.index("]")] if text.startswith("[") and "]" in text else ""
    tagged_source = _SOURCES.get(prefix.strip().casefold(), "")
    if tagged_source == "Blackboard":
        return tagged_source
    return _SOURCES.get(source.casefold(), source) if source else tagged_source


def task_due(task: dict) -> datetime | None:
    try:
        due = datetime.fromisoformat(task.get("due"))
        return due.astimezone().replace(tzinfo=None) if due.tzinfo is not None else due
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _task_order(task: dict) -> tuple[bool, float, int]:
    try:
        due = datetime.fromisoformat(task.get("due")).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        due = float("inf")
    return task_source(task) != "Blackboard", due, task.get("id", 0)


class Store(_PersistentStore):
    def add_fact(self, text: str, strength: float = 3.0):
        with self.lock:
            self._check_open()
            previous = self.data.get("fact_touched")
            if not isinstance(previous, dict):
                previous = {}
            updated = {fact: previous[fact] for fact in self.data["facts"] if fact in previous}
            updated[text] = time.time()
            self.data["fact_touched"] = updated
            super().add_fact(text, strength)

    def relevant_facts(self, query: str, limit: int = 3) -> list[dict]:
        words = _keywords(query)
        if not words or limit <= 0:
            return []
        with self.lock:
            facts = list(self.data["facts"].items())
            touched = self.data.get("fact_touched")
            touched = dict(touched) if isinstance(touched, dict) else {}

        matches = []
        for text, strength in facts:
            overlap = len(words & _keywords(text))
            if overlap:
                updated = touched.get(text, 0)
                recent = updated if isinstance(updated, (int, float)) else 0
                matches.append((overlap, strength, recent, text))
        matches.sort(reverse=True)
        return [{"text": text, "strength": strength} for _, strength, _, text in matches[:limit]]

    def pending(self) -> list[dict]:
        """Return ranked live task objects for commands and sync updates."""
        with self.lock:
            tasks = [(task, task.copy()) for task in self.data["tasks"] if not task["done"]]
        tasks.sort(key=lambda item: _task_order(item[1]))
        return [task for task, _ in tasks]

    def pending_snapshot(self) -> list[dict]:
        with self.lock:
            tasks = [task.copy() for task in self.data["tasks"] if not task["done"]]
        return sorted(tasks, key=_task_order)

    def relevant_tasks(self, query: str, limit: int = 3) -> list[dict]:
        if limit <= 0:
            return []
        pending = self.pending_snapshot()
        words = _keywords(query)
        if len(pending) <= 1 or not words or limit == 1:
            return pending[:1]

        selected = pending[:1]
        for task in pending[1:]:
            if words & _keywords(f"{task['text']} {task_source(task)}"):
                selected.append(task)
                if len(selected) == limit:
                    break
        return selected
