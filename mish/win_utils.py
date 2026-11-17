"""Helpers + Windows plumbing: logging, time parsing, window/idle queries, key sending,
screen capture, app launching and the ctypes layered-window surface.

Split from the monolithic mish.pyw by split_mish.py; this package is now the source of truth.
"""
from __future__ import annotations

import os
import sys
import ast
import ctypes
import io
import json
import re
import shutil
import subprocess
import threading
import time
import psutil
import win32gui
import win32process
import ctypes.wintypes as _wt
from pathlib import Path
from datetime import date, datetime, timedelta
from PIL import Image, ImageGrab

from .config import (
    ACTIVITY_KEEP,
    APP_DIR,
    DATA_DIR,
    FACT_LIMIT,
    FILE_CHAR_LIMIT,
    FILE_READ_BYTES,
    MAX_RENAGS,
    OBSERVE_MAX_SIDE,
    OBSERVE_WHOLE_DESKTOP,
    RENAG_INTERVAL,
    SLACK_KEYWORDS,
    STATS_KEEP_DAYS,
    TEXT_EXTS,
    WORK_KEYWORDS,
)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log(*parts):
    """Append to Documents/Mish/Mish.log (the exe has no console). Never raises."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        p = DATA_DIR / "Mish.log"
        if p.exists() and p.stat().st_size > 200_000:
            p.unlink()
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{now_iso()} {' '.join(str(x) for x in parts)}\n")
    except Exception:
        pass


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def fmt_dur(secs: float) -> str:
    secs = int(secs)
    h, m = divmod(secs // 60, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m" if m else f"{secs}s"


def fmt_due(iso: str) -> str:
    due = datetime.fromisoformat(iso)
    now = datetime.now()
    if due.date() == now.date():
        label = f"today {due:%H:%M}"
    elif due.date() == (now + timedelta(days=1)).date():
        label = f"tomorrow {due:%H:%M}"
    else:
        label = f"{due:%a %d %b %H:%M}"
    return label + (" (overdue)" if due < now else "")


REL_RE = re.compile(r"\bin\s+(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|s|m|h|d)\b", re.I)
ABS_RE = re.compile(r"\b(?:(tomorrow)\s+)?at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)
TOMORROW_RE = re.compile(r"\btomorrow\b", re.I)
UNIT_SECS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_when(text: str, now: datetime | None = None):
    """Split "buy milk in 30m" / "call mum at 6pm" / "gym tomorrow" into (task text, due datetime | None)."""
    now = now or datetime.now()
    due = None
    m = REL_RE.search(text)
    if m:
        due = now + timedelta(seconds=int(m.group(1)) * UNIT_SECS[m.group(2)[0].lower()])
        text = text[: m.start()] + " " + text[m.end():]
    else:
        m = ABS_RE.search(text)
        if m:
            tomorrow, hour, minute, ampm = m.group(1), int(m.group(2)), int(m.group(3) or 0), m.group(4)
            if minute <= 59 and hour <= 23:
                if ampm:
                    hours = [hour % 12 + (12 if ampm.lower() == "pm" else 0)]
                elif 1 <= hour <= 11:
                    hours = [hour, hour + 12]
                else:
                    hours = [hour]
                base = now + timedelta(days=1) if tomorrow else now
                cands = []
                for h in hours:
                    c = base.replace(hour=h, minute=minute, second=0, microsecond=0)
                    if c <= now:
                        c += timedelta(days=1)
                    cands.append(c)
                due = min(cands)
                text = text[: m.start()] + " " + text[m.end():]
        elif TOMORROW_RE.search(text):
            m = TOMORROW_RE.search(text)
            due = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            text = text[: m.start()] + " " + text[m.end():]

    cleaned = re.sub(r"\s+", " ", text).strip(" ,.-")
    cleaned = re.sub(r"^(to|about|that)\s+", "", cleaned, flags=re.I)
    return cleaned, due


def bundle_dir() -> Path:
    """Directory containing bundled read-only assets (works for both .pyw and frozen .exe)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return APP_DIR


# ---- Windows helpers ----------------------------------------------------------
def get_active_window_info():
    """(window title, process name) of the foreground window."""
    try:
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd) or "Desktop / Idle"
        app_name = ""
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            app_name = psutil.Process(pid).name().lower()
        except Exception:
            pass
        return title, app_name
    except Exception:
        return "Unknown Window", ""


def idle_seconds() -> float:
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        ctypes.windll.kernel32.GetTickCount.restype = ctypes.c_uint
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        pass
    return 0.0


def is_fullscreen_foreground() -> bool:
    """True if the active foreground window covers the entire monitor (games, videos, presentations)."""
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd or win32gui.IsIconic(hwnd):
            return False
        cls = win32gui.GetClassName(hwnd)
        if cls in ("Progman", "WorkerW", "Shell_TrayWnd"):
            return False
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        hm = ctypes.windll.user32.MonitorFromWindow(hwnd, 2)
        mi = _MONINFO()
        mi.cbSize = ctypes.sizeof(_MONINFO)
        if ctypes.windll.user32.GetMonitorInfoW(hm, ctypes.byref(mi)):
            mr = mi.rcMonitor
            return l <= mr.left and t <= mr.top and r >= mr.right and b >= mr.bottom
    except Exception:
        pass
    return False


def classify(title: str, app: str) -> str:
    blob = f"{title} {app}".lower()
    if any(k in blob for k in SLACK_KEYWORDS):
        return "slack"
    if any(k in blob for k in WORK_KEYWORDS):
        return "work"
    return "neutral"


# ---- keyboard automation (used by the !open errand) ------------------------------------
def _key_event(vk: int, up: bool = False):
    ctypes.windll.user32.keybd_event(vk, 0, 2 if up else 0, 0)


def _tap_key(vk: int):
    _key_event(vk)
    _key_event(vk, up=True)


def _type_text(text: str, delay: float = 0.04):
    """Type text into whatever has focus, one key at a time (handles Shift for capitals and symbols)."""
    ctypes.windll.user32.VkKeyScanW.restype = ctypes.c_short      # so "no such key" really is -1
    for ch in text:
        vk = ctypes.windll.user32.VkKeyScanW(ord(ch))
        if vk != -1:
            code, shift = vk & 0xFF, (vk >> 8) & 1
            if shift:
                _key_event(0x10)                                    # Shift down
            _tap_key(code)
            if shift:
                _key_event(0x10, up=True)
        time.sleep(delay)


def capture_screen() -> bytes | None:
    """PNG of the active window (or the whole desktop), shrunk for the vision model."""
    try:
        full = ImageGrab.grab(all_screens=True).convert("RGB")
        img = full
        if not OBSERVE_WHOLE_DESKTOP:
            try:
                hwnd = win32gui.GetForegroundWindow()
                if hwnd and not win32gui.IsIconic(hwnd):
                    l, t, r, b = win32gui.GetWindowRect(hwnd)
                    ox = ctypes.windll.user32.GetSystemMetrics(76)
                    oy = ctypes.windll.user32.GetSystemMetrics(77)
                    l, r = max(l - ox, 0), min(r - ox, full.width)
                    t, b = max(t - oy, 0), min(b - oy, full.height)
                    if r - l > 200 and b - t > 150:
                        img = full.crop((l, t, r, b))
            except Exception:
                pass
        img.thumbnail((OBSERVE_MAX_SIDE, OBSERVE_MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        log("screen capture failed:", repr(e))
        return None


# ---- reading files ------------------------------------------------------------
def inspect_file(raw_path: str):
    """Safely read a text/code file. Returns (info, None) or (None, error text).
    Python and JSON files get a real syntax check, so the model reacts to facts instead of guessing."""
    p = Path(raw_path.strip().strip('"'))
    try:
        if not p.is_file():
            return None, "That isn't a file I can read."
        ext = p.suffix.lower()
        if ext not in TEXT_EXTS:
            return None, f"I only read text and code files, not {ext or 'that kind'}."
        size = p.stat().st_size
        with open(p, "rb") as f:
            data = f.read(FILE_READ_BYTES)
    except OSError as e:
        log("inspect_file failed:", repr(e))
        return None, "I couldn't open that file."
    if b"\x00" in data[:2048]:
        return None, "That looks like a binary file, not text."

    text = data.decode("utf-8", errors="replace")
    partial = size > FILE_READ_BYTES
    check = "no automatic syntax check for this file type"
    if ext in (".py", ".pyw", ".json"):
        kind = "JSON" if ext == ".json" else "Python"
        if partial:
            check = "file too large for a syntax check"
        else:
            try:
                json.loads(text) if ext == ".json" else ast.parse(text)
                check = f"{kind} syntax check passed, no errors"
            except (SyntaxError, json.JSONDecodeError) as e:
                check = f"{kind} syntax ERROR on line {e.lineno}: {e.msg}"
    snippet = text[:FILE_CHAR_LIMIT]
    facts = f"file {p.name}, {size} bytes, about {text.count(chr(10)) + 1} lines, {check}"
    if partial or len(text) > FILE_CHAR_LIMIT:
        facts += f"; only the first {len(snippet)} characters are shown"
    return dict(name=p.name, facts=facts, snippet=snippet, error="ERROR" in check), None


# ---- launching apps -----------------------------------------------------------
EXEC_EXTS = (".exe", ".bat", ".cmd", ".com")


def _known_apps() -> dict:
    """Apps that live in predictable places: name -> (path, default args)."""
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    pf = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
    pf86 = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
    code = (local / "Programs" / "Microsoft VS Code" / "Code.exe", [])
    return {
        "discord": (local / "Discord" / "Update.exe", ["--processStart", "Discord.exe"]),
        "spotify": (roaming / "Spotify" / "Spotify.exe", []),
        "chrome": (pf / "Google" / "Chrome" / "Application" / "chrome.exe", []),
        "code": code, "vscode": code,
        "steam": (pf86 / "Steam" / "steam.exe", []),
        "epic": (pf86 / "Epic Games" / "Launcher" / "Portal" / "Binaries" / "Win64" / "EpicGamesLauncher.exe", []),
    }


def _spawn(path, args):
    subprocess.Popen([str(path), *args], creationflags=0x08000000, close_fds=True)   # CREATE_NO_WINDOW


def launch_app(target: str, args: list[str], aliases: dict, allow_any: bool = True) -> bool:
    """Start an app without a shell. Order: user/default aliases, known install spots, an explicit path,
    PATH, common folders, then Windows' own lookup. With allow_any=False (used when the *model* asks for an app)
    only aliases and known apps are allowed, never arbitrary programs or paths."""
    key = target.lower().removesuffix(".exe")
    try:
        alias = aliases.get(target.lower()) or aliases.get(key)
        if alias:
            target = alias
        known = _known_apps().get(key)
        if known and known[0].exists():
            _spawn(known[0], [*known[1], *args])
            return True
        if not alias and not allow_any:
            return False

        p = Path(target)
        if p.exists():
            if p.suffix.lower() in EXEC_EXTS:
                _spawn(p, args)
            else:
                os.startfile(str(p))                 # folders and documents open in their default app
            return True
        exe = shutil.which(target)
        if exe:
            _spawn(exe, args)
            return True
        name = target if target.lower().endswith(".exe") else target + ".exe"
        home = Path.home()
        for folder in (home / "AppData/Local/Programs", home / "AppData/Local", home / "AppData/Roaming",
                       Path("C:/Program Files"), Path("C:/Program Files (x86)")):
            if (folder / name).exists():
                _spawn(folder / name, args)
                return True
        if not args:
            os.startfile(target)                     # registered "App Paths" programs, URLs, etc.
            return True
    except Exception as e:
        log("launch failed:", target, repr(e))
    return False


# 4. STORE (memory.json)
class Store:
    """Tasks, facts (text -> strength), chat history, screen notes and usage stats, saved atomically."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self._dirty = False
        self.data = self._load()
        self.save()

    @staticmethod
    def _defaults() -> dict:
        return {"tasks": [], "facts": {}, "chat_history": [], "stats": {},
                "next_id": 1, "activity": [], "last_decay": None}

    def _load(self) -> dict:
        data = self._defaults()
        bak = self.path.with_suffix(".json.bak")
        for candidate in (self.path, bak):
            if candidate.exists():
                try:
                    data.update(json.loads(candidate.read_text(encoding="utf-8")))
                    if candidate == bak:
                        log("memory restored from backup")
                    break
                except Exception as e:
                    log(f"memory load failed ({candidate.name}):", repr(e))
                    if candidate == self.path:
                        try:
                            self.path.replace(self.path.with_suffix(".broken.json"))
                        except OSError:
                            pass
        facts = data.get("facts")
        if isinstance(facts, list):                      # old format: a plain list of strings
            data["facts"] = {f: 3.0 for f in facts if isinstance(f, str)}
        elif not isinstance(facts, dict):
            data["facts"] = {}
        return data

    def save(self):
        with self.lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists():
                    try:
                        shutil.copy2(self.path, self.path.with_suffix(".json.bak"))
                    except OSError:
                        pass
                tmp = self.path.with_suffix(".tmp")
                payload = json.dumps(self.data, indent=2, ensure_ascii=False).encode("utf-8")
                with open(tmp, "wb") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
                self._dirty = False
            except Exception as e:
                log("memory save failed:", repr(e))

    # ---- tasks ----------------------------------------------------------------
    def add_task(self, text: str, due: datetime | None = None):
        with self.lock:
            d = self.data
            if any(t["text"].lower() == text.lower() and not t["done"] for t in d["tasks"]):
                return None
            task = {
                "id": d["next_id"], "text": text, "done": False, "created": now_iso(),
                "due": due.isoformat(timespec="seconds") if due else None,
                "nags": 0, "last_nag": None,
            }
            d["next_id"] += 1
            d["tasks"].append(task)
            self.save()
            return task

    def pending(self) -> list:
        with self.lock:
            return sorted((t for t in self.data["tasks"] if not t["done"]),
                          key=lambda t: (t["due"] is None, t["due"] or "", t["id"]))

    def resolve_task(self, ref: str):
        """A task by its number in the pending list (as shown by `tasks`) or by part of its text."""
        ref = ref.strip().lstrip("#").lower()
        pending = self.pending()
        if ref.isdigit():
            i = int(ref) - 1
            return pending[i] if 0 <= i < len(pending) else None
        return next((t for t in pending if ref in t["text"].lower()), None)

    def complete(self, task: dict):
        with self.lock:
            task["done"], task["done_at"] = True, now_iso()
            self.save()

    def delete(self, task: dict):
        with self.lock:
            if task in self.data["tasks"]:
                self.data["tasks"].remove(task)
                self.save()

    def due_for_nag(self, now: datetime) -> list:
        out = []
        with self.lock:
            for t in self.data["tasks"]:
                if t["done"] or not t["due"] or datetime.fromisoformat(t["due"]) > now:
                    continue
                if t["nags"] == 0:
                    out.append(t)
                elif (t["nags"] <= MAX_RENAGS and t["last_nag"]
                      and (now - datetime.fromisoformat(t["last_nag"])).total_seconds() >= RENAG_INTERVAL):
                    out.append(t)
        return out

    def mark_nagged(self, task: dict):
        with self.lock:
            task["nags"] += 1
            task["last_nag"] = now_iso()
            self.save()

    # ---- facts ----------------------------------------------------------------
    def facts_sorted(self) -> list:
        """[(fact, strength)] strongest first. The numbers shown by `facts` and used by `forget` come from here."""
        with self.lock:
            return sorted(self.data["facts"].items(), key=lambda kv: kv[1], reverse=True)

    def add_fact(self, text: str, strength: float = 3.0):
        with self.lock:
            facts = self.data["facts"]
            facts[text] = facts.get(text, 0.0) + strength
            self.data["facts"] = dict(self.facts_sorted()[:FACT_LIMIT])
            self.save()

    def forget_fact(self, ref: str):
        """Forget a fact by its number in `facts` or by part of its text. Returns the fact, or None."""
        with self.lock:
            items = self.facts_sorted()
            ref = ref.strip().lower()
            if ref.isdigit():
                i = int(ref) - 1
                fact = items[i][0] if 0 <= i < len(items) else None
            else:
                fact = next((f for f, _ in items if ref in f.lower()), None)
            if fact:
                del self.data["facts"][fact]
                self.save()
            return fact

    def decay_facts(self, days: int = 1):
        with self.lock:
            kept = {f: s - days for f, s in self.data["facts"].items() if s - days > 1.0}
            self.data["facts"] = kept
            self.save()

    def run_daily_decay(self):
        """Fade unused facts by 1 per day. The last run is stored, so it survives restarts and long gaps."""
        with self.lock:
            today = date.today()
            try:
                last = date.fromisoformat(self.data.get("last_decay") or "")
            except ValueError:
                last = today
            if today > last:
                self.decay_facts(min((today - last).days, 30))
            if self.data.get("last_decay") != today.isoformat():
                self.data["last_decay"] = today.isoformat()
                self.save()

    # ---- chat history, screen notes, usage stats ------------------------------
    def add_history(self, role: str, content: str):
        with self.lock:
            self.data["chat_history"] = (self.data["chat_history"] + [{"role": role, "content": content}])[-12:]
            self.save()

    def add_activity(self, text: str):
        with self.lock:
            self.data["activity"] = (self.data["activity"] + [{"t": now_iso(), "text": text}])[-ACTIVITY_KEEP:]
            self.save()

    def today_stats(self) -> dict:
        with self.lock:
            day = date.today().isoformat()
            stats = self.data["stats"]
            if day not in stats:
                stats[day] = {"work": 0.0, "slack": 0.0, "neutral": 0.0, "apps": {}}
                for old in sorted(stats)[:-STATS_KEEP_DAYS]:
                    del stats[old]
            return stats[day]

    def record_usage(self, app: str, category: str, secs: float):
        with self.lock:
            s = self.today_stats()
            s[category] += secs
            s["apps"][app] = s["apps"].get(app, 0.0) + secs
            self._dirty = True


_U32 = ctypes.WinDLL("user32", use_last_error=True)
_G32 = ctypes.WinDLL("gdi32", use_last_error=True)
_VP = ctypes.c_void_p


class _BLEND(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


class _BMI(ctypes.Structure):
    _fields_ = [("header", _BMIH), ("pad", ctypes.c_uint32 * 3)]


class _MONINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("rcMonitor", _wt.RECT), ("rcWork", _wt.RECT),
                ("dwFlags", ctypes.c_uint32)]


def _win_setup():
    P = ctypes.POINTER
    _U32.GetDC.argtypes, _U32.GetDC.restype = [_VP], _VP
    _U32.ReleaseDC.argtypes, _U32.ReleaseDC.restype = [_VP, _VP], ctypes.c_int
    _U32.GetAncestor.argtypes, _U32.GetAncestor.restype = [_VP, ctypes.c_uint], _VP
    _U32.GetWindowRect.argtypes, _U32.GetWindowRect.restype = [_VP, P(_wt.RECT)], ctypes.c_int
    _U32.UpdateLayeredWindow.argtypes = [_VP, _VP, P(_wt.POINT), P(_wt.SIZE), _VP, P(_wt.POINT),
                                         ctypes.c_uint32, P(_BLEND), ctypes.c_uint32]
    _U32.UpdateLayeredWindow.restype = ctypes.c_int
    _U32.MonitorFromWindow.argtypes, _U32.MonitorFromWindow.restype = [_VP, ctypes.c_uint32], _VP
    _U32.GetMonitorInfoW.argtypes, _U32.GetMonitorInfoW.restype = [_VP, P(_MONINFO)], ctypes.c_int
    get = getattr(_U32, "GetWindowLongPtrW", None) or _U32.GetWindowLongW
    put = getattr(_U32, "SetWindowLongPtrW", None) or _U32.SetWindowLongW
    get.argtypes, get.restype = [_VP, ctypes.c_int], ctypes.c_ssize_t
    put.argtypes, put.restype = [_VP, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t
    _G32.CreateCompatibleDC.argtypes, _G32.CreateCompatibleDC.restype = [_VP], _VP
    _G32.CreateDIBSection.argtypes = [_VP, P(_BMI), ctypes.c_uint, P(_VP), _VP, ctypes.c_uint32]
    _G32.CreateDIBSection.restype = _VP
    _G32.SelectObject.argtypes, _G32.SelectObject.restype = [_VP, _VP], _VP
    _G32.DeleteObject.argtypes, _G32.DeleteObject.restype = [_VP], ctypes.c_int
    _G32.DeleteDC.argtypes, _G32.DeleteDC.restype = [_VP], ctypes.c_int
    return get, put


_GWL, _SWL = _win_setup()


class _LayeredSurface:
    """A top-level HWND shown through UpdateLayeredWindow: premultiplied BGRA, true per-pixel alpha."""

    GWL_EXSTYLE, WS_EX_LAYERED, ULW_ALPHA, AC_SRC_ALPHA, GA_ROOT = -20, 0x00080000, 2, 1, 2

    def __init__(self):
        self.hwnd = None
        self.memdc = self.bmp = self.old = None
        self.bits = None
        self.size = (0, 0)

    def attach(self, root) -> bool:
        if self.hwnd:
            return True
        inner = root.winfo_id()
        top = _U32.GetAncestor(inner, self.GA_ROOT) or inner
        ex = _GWL(top, self.GWL_EXSTYLE)
        # drop + re-add the bit so an earlier SetLayeredWindowAttributes() cannot make UpdateLayeredWindow fail
        _SWL(top, self.GWL_EXSTYLE, ex & ~self.WS_EX_LAYERED)
        _SWL(top, self.GWL_EXSTYLE, ex | self.WS_EX_LAYERED)
        self.hwnd = top
        return True

    def _alloc(self, w: int, h: int):
        self.free()
        screen = _U32.GetDC(None)
        try:
            self.memdc = _G32.CreateCompatibleDC(screen)
            bmi = _BMI()
            bmi.header.biSize = ctypes.sizeof(_BMIH)
            bmi.header.biWidth, bmi.header.biHeight = w, -h          # negative = top-down rows
            bmi.header.biPlanes, bmi.header.biBitCount = 1, 32
            bits = _VP()
            self.bmp = _G32.CreateDIBSection(screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            self.bits = bits.value
        finally:
            _U32.ReleaseDC(None, screen)
        self.old = _G32.SelectObject(self.memdc, self.bmp)
        self.size = (w, h)

    def free(self):
        if self.memdc:
            if self.old:
                _G32.SelectObject(self.memdc, self.old)
            if self.bmp:
                _G32.DeleteObject(self.bmp)
            _G32.DeleteDC(self.memdc)
        self.memdc = self.bmp = self.old = self.bits = None

    def upload(self, rgba: "Image.Image"):
        w, h = rgba.size
        if (w, h) != self.size or not self.bits:
            self._alloc(w, h)
        r, g, b, a = rgba.convert("RGBa").split()                   # premultiplied alpha
        data = Image.merge("RGBA", (b, g, r, a)).tobytes()          # -> BGRA byte order
        ctypes.memmove(self.bits, data, len(data))

    def rect(self):
        rc = _wt.RECT()
        _U32.GetWindowRect(self.hwnd, ctypes.byref(rc))
        return rc.left, rc.top

    def present(self, x=None, y=None, alpha: float = 1.0):
        if not (self.hwnd and self.memdc):
            return False
        if x is None or y is None:
            x, y = self.rect()
        w, h = self.size
        pt, sz, src = _wt.POINT(int(x), int(y)), _wt.SIZE(w, h), _wt.POINT(0, 0)
        blend = _BLEND(0, 0, int(clamp(alpha, 0.0, 1.0) * 255), self.AC_SRC_ALPHA)   # AC_SRC_OVER, per-pixel alpha
        screen = _U32.GetDC(None)
        try:
            return bool(_U32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(pt), ctypes.byref(sz),
                                                 self.memdc, ctypes.byref(src), 0, ctypes.byref(blend),
                                                 self.ULW_ALPHA))
        finally:
            _U32.ReleaseDC(None, screen)
