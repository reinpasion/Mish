"""Tk window, layered-window renderer, speech bubble, task panel, tray icon and main().

Split from the monolithic mish.pyw by split_mish.py; this package is now the source of truth.
"""
from __future__ import annotations

import os
import sys
import ctypes
import math
import queue
import threading
import time
import tkinter as tk
import pystray
import ctypes.wintypes as _wt
from pathlib import Path
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .config import AUTO_SLEEP_TIMEOUT, FOCUS_DEFAULT_MIN, HOTKEY_MOD, HOTKEY_VK, user_data_dir
from .win_utils import (
    _LayeredSurface,
    _MONINFO,
    _U32,
    clamp,
    fmt_due,
    idle_seconds,
    log,
    parse_when,
)
from .brain import (
    BG_PRESETS,
    EYE_PRESETS,
    EyeEngine,
    Mish,
    _hex_rgb,
    _mix,
    _rgb_hex,
    pick_expression,
    speech_duration_ms,
)


# ---- easing curves: t runs 0 -> 1 ----------------------------------------------------
def ease_in_cubic(t: float) -> float:
    return t * t * t


def ease_out_back(t: float, s: float = 0.9) -> float:
    """Eases out with a small overshoot, like a spring settling."""
    t -= 1.0
    return 1.0 + (s + 1.0) * t ** 3 + s * t ** 2


def smoothstep(t: float) -> float:
    t = clamp(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


class DynamicSpeechBubble(tk.Toplevel):
    TEXT_COL = "#e9eaf0"          # soft off-white text
    FONT = ("Segoe UI", 10)       # bubble font
    WRAP = 240                    # max text width before wrapping (px)
    ALPHA = 0.96                  # final opacity (fades in and out)

    def __init__(self, parent, text, duration=6000, accent="#00e6ff", bg="#111115"):
        super().__init__(parent)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg="#000000")
        try:
            self.attributes("-alpha", 0.0)
        except tk.TclError:
            pass

        try:
            border = _rgb_hex(_mix(_hex_rgb(bg), _hex_rgb(accent), 0.45))   # muted accent, not full-strength
        except Exception:
            border = accent

        frame = tk.Frame(self, bg=bg, highlightbackground=border, highlightthickness=1)
        frame.pack(fill="both", expand=True)
        tk.Frame(frame, bg=accent, width=3).pack(side="left", fill="y")       # thin accent stripe
        tk.Label(frame, text=text, fg=self.TEXT_COL, bg=bg, font=self.FONT, wraplength=self.WRAP,
                 justify="left", padx=12, pady=9).pack(side="left")

        self.update_idletasks()
        bw = self.winfo_reqwidth()
        bh = self.winfo_reqheight()

        px, py = parent.winfo_x(), parent.winfo_y()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        sw = parent.winfo_screenwidth()

        bx = px + (pw // 2) - (bw // 2)
        bx = max(10, min(bx, sw - bw - 10))
        by = py + ph + 6 if py - bh - 6 < 10 else py - bh - 6

        self.geometry(f"{bw}x{bh}+{bx}+{by}")
        self.bind("<Button-1>", lambda e: self._close())
        self._fade(0.0, self.ALPHA, 0.16)
        self.after(duration, self._close)

    def update_position(self):
        """Reposition the bubble to stay attached to the parent, without resetting the fade timer."""
        if not self.winfo_exists():
            return
        self.update_idletasks()
        bw = self.winfo_width()
        bh = self.winfo_height()
        px, py = self.master.winfo_x(), self.master.winfo_y()
        pw, ph = self.master.winfo_width(), self.master.winfo_height()
        sw = self.master.winfo_screenwidth()
        bx = max(10, min(px + (pw // 2) - (bw // 2), sw - bw - 10))
        by = py + ph + 6 if py - bh - 6 < 10 else py - bh - 6
        self.geometry(f"+{bx}+{by}")

    def _fade(self, a, target, step):
        """Step the window opacity toward `target`; destroys the bubble once fully faded out."""
        if not self.winfo_exists():
            return
        a = min(a + step, target) if step > 0 else max(a + step, target)
        try:
            self.attributes("-alpha", a)
        except tk.TclError:
            pass
        if a != target:
            self.after(16, lambda: self._fade(a, target, step))
        elif target <= 0.0:
            self.destroy()

    def _close(self):
        if self.winfo_exists() and not getattr(self, "_closing", False):
            self._closing = True
            self._fade(self.ALPHA, 0.0, -0.16)


# ---- TASK PANEL (separate window) -------------------------------------------------------
# Paste this class into mish.pyw in section 8, directly ABOVE `class RobotFaceWindow:`.
# It only uses things the file already has: tk, datetime, parse_when, fmt_due and the Store.

class TaskPanel(tk.Toplevel):
    """A separate "Mish Tasks" window: add a task, tick it off, or remove it.
    It reads and writes the same memory.json as Mish, so tasks added in chat show up here within a second."""

    TEXT, MUTED, WARN = "#e8e8ee", "#8a8a99", "#ff6b6b"
    WIDTH, LIST_H = 340, 260
    HINT = "e.g.  call mum at 6pm   |   stretch in 30m   |   gym tomorrow"

    def __init__(self, face):
        super().__init__(face.root)
        self.store = face.mish.store
        self.bg, self.panel, self.accent = face.bg, face.panel, face.eye_col
        self._sig = None

        self.title("Mish Tasks")
        self.configure(bg=self.bg)
        self.attributes("-topmost", True)
        self.resizable(False, False)
        x = face.root.winfo_x() + face.root.winfo_width() + 12
        if x + self.WIDTH > face.root.winfo_screenwidth():
            x = max(10, face.root.winfo_x() - self.WIDTH - 12)
        self.geometry(f"+{x}+{max(10, face.root.winfo_y())}")

        # ---- header, add bar, status line ----
        self.header = tk.Label(self, text="", bg=self.bg, fg=self.accent, font=("Segoe UI", 11, "bold"), anchor="w")
        self.header.pack(fill="x", padx=12, pady=(10, 4))

        bar = tk.Frame(self, bg=self.bg)
        bar.pack(fill="x", padx=12)
        self.entry = tk.Entry(bar, bg=self.panel, fg=self.TEXT, insertbackground=self.accent,
                              relief="flat", font=("Segoe UI", 10))
        self.entry.pack(side="left", fill="x", expand=True, ipady=5)
        tk.Button(bar, text="Add", command=self.add, bg=self.accent, fg=self.bg, activebackground=self.accent,
                  activeforeground=self.bg, bd=0, relief="flat", cursor="hand2", padx=12,
                  font=("Segoe UI", 9, "bold")).pack(side="left", padx=(6, 0), ipady=2)
        self.entry.bind("<Return>", self.add)

        self.status = tk.Label(self, text=self.HINT, bg=self.bg, fg=self.MUTED, font=("Segoe UI", 8), anchor="w")
        self.status.pack(fill="x", padx=12, pady=(4, 6))

        # ---- scrollable task list ----
        box = tk.Frame(self, bg=self.bg)
        box.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.canvas = tk.Canvas(box, bg=self.bg, highlightthickness=0, width=self.WIDTH - 40, height=self.LIST_H)
        scroll = tk.Scrollbar(box, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.rows = tk.Frame(self.canvas, bg=self.bg)
        self._rows_id = self.canvas.create_window((0, 0), window=self.rows, anchor="nw")
        self.rows.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._rows_id, width=e.width))
        self.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))
        self.bind("<Escape>", lambda e: self.destroy())

        self.entry.focus_force()
        self.refresh()
        self._tick()

    # ---- actions ----
    def add(self, _event=None):
        raw = self.entry.get().strip()
        if not raw:
            return
        what, due = parse_when(raw)
        task = self.store.add_task(what or raw, due)
        if task:
            self.entry.delete(0, "end")
            self._say("Added" + (f" - due {fmt_due(task['due'])}" if task["due"] else ""))
        else:
            self._say("Already on the list.", warn=True)
        self.refresh()

    def remove(self, task: dict):
        self.store.delete(task)
        self._say(f"Removed: {task['text'][:40]}")
        self.refresh()

    def finish(self, task: dict):
        self.store.complete(task)
        self._say(f"Done: {task['text'][:40]}")
        self.refresh()

    def _say(self, text: str, warn: bool = False):
        self.status.configure(text=text, fg=self.WARN if warn else self.accent)

    # ---- list drawing ----
    def refresh(self):
        """Rebuild the list only when something actually changed (a task added, removed or turned overdue)."""
        tasks = self.store.pending()
        sig = [(t["id"], t["text"], fmt_due(t["due"]) if t["due"] else "") for t in tasks]
        if sig == self._sig:
            return
        self._sig = sig
        for w in self.rows.winfo_children():
            w.destroy()
        self.header.configure(text=f"Tasks ({len(tasks)})")
        if not tasks:
            tk.Label(self.rows, text="Nothing to do. Suspicious.", bg=self.bg, fg=self.MUTED,
                     font=("Segoe UI", 10)).pack(anchor="w", pady=8)
        for t in tasks:
            self._row(t)

    def _row(self, task: dict):
        row = tk.Frame(self.rows, bg=self.panel)
        row.pack(fill="x", pady=2)
        for glyph, fn in (("✕", self.remove), ("✓", self.finish)):       # packed right-to-left: ✕ ends up rightmost
            tk.Button(row, text=glyph, command=lambda t=task, f=fn: f(t), bg=self.panel, fg=self.accent,
                      activebackground=self.accent, activeforeground=self.bg, bd=0, relief="flat",
                      cursor="hand2", font=("Segoe UI", 11), width=2).pack(side="right", padx=(0, 4))
        info = tk.Frame(row, bg=self.panel)
        info.pack(side="left", fill="x", expand=True, padx=(10, 4), pady=5)
        tk.Label(info, text=task["text"], bg=self.panel, fg=self.TEXT, font=("Segoe UI", 10),
                 anchor="w", justify="left", wraplength=190).pack(fill="x")
        if task["due"]:
            label = fmt_due(task["due"])
            tk.Label(info, text=label, bg=self.panel, fg=self.WARN if "overdue" in label else self.MUTED,
                     font=("Segoe UI", 8), anchor="w").pack(fill="x")

    def _tick(self):
        """Pick up tasks added by chat, !commands or reminders while this window is open."""
        try:
            if not self.winfo_exists():
                return
            self.refresh()
            self.after(1000, self._tick)
        except tk.TclError:
            pass


class RobotFaceWindow:
    P_SIZE = 4                 # screen pixels per "16-bit" pixel
    MASK_KEY = "#000001"       # chroma-key colour that makes the squircle corners transparent
    RADIUS = 24
    FACE_CENTER = (108, 48)    # between the eyes, inside the window (for mouse-following)
    SIZE = (240, 130)

    def __init__(self, root: tk.Tk, mish: Mish, ui_queue: queue.Queue):
        self.root, self.mish, self.ui = root, mish, ui_queue
        self.settings = mish.settings
        self.eyes = EyeEngine()
        self.active_bubble = None
        self._bubble_is_timer = False
        self.cust = None
        self._derive_theme()

        root.title("Mish")
        root.geometry(f"{self.SIZE[0]}x{self.SIZE[1]}+100+100")
        root.configure(bg=self.MASK_KEY)
        root.overrideredirect(True)
        root.wm_attributes("-topmost", True)
        root.wm_attributes("-transparentcolor", self.MASK_KEY)

        self.canvas = tk.Canvas(root, bg=self.MASK_KEY, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.btn_gear = tk.Button(root, text="⚙", font=("Consolas", 9, "bold"), bd=0, relief="flat",
                                  cursor="hand2", command=self.show_menu)
        self.btn_gear.place(relx=0.88, rely=0.82, anchor="center", width=20, height=20)

        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="💬 Chat Input", command=self.open_chat_dialog)
        self.menu.add_command(label="👀 Look Screen", command=lambda: self.mish.observe_async(force=True))
        self.menu.add_command(label="📊 Stats", command=lambda: self.show_speech_bubble(self.mish.stats_text()))
        self.menu.add_command(label="📝 Tasks", command=self.open_tasks)
        self.menu.add_command(label="🎨 Customize...", command=self.open_customize)
        self.menu.add_separator()
        self.menu.add_command(label="❌ Exit", command=self.root.destroy)

        # physics / drag
        self.vel_x = self.vel_y = 0.0
        self.last_pos_x = self.last_pos_y = 100
        self.last_move_t = time.time()
        self.shake_energy = 0.0
        self.idle_t = 0.0
        self.drag_x = self.drag_y = 0
        self.press_root = (0, 0)
        self.moved = False
        self.poke_times: list[float] = []

        # states
        self.edge_hold_start = 0.0
        self.strike_until = 0.0
        self.is_scared = self.is_sleeping = self.is_held = False
        self.is_listening = self.is_dizzy = False
        self.errand = None                            # set while an !open errand is playing
        self.zzz_frame = 0
        self._last_cells = None

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.enable_file_drop()

        self.apply_theme()
        root.update()
        self.last_pos_x, self.last_pos_y = root.winfo_x(), root.winfo_y()
        self.last_move_t = time.time()
        self.animate_loop()
        self.poll()

    # ---- errands: he dives off the bottom while Windows search runs, then springs back -----------
    ERRAND_DIVE_S = 0.55         # wind-up hop + dive
    ERRAND_RETURN_S = 0.70       # spring back
    ERRAND_TIMEOUT_MS = 15000    # come back anyway if the search never reports

    def _base_alpha(self) -> float:
        return 0.3 if self.is_sleeping else self.settings["opacity"]

    def _place(self, x: float, y: float, alpha: float | None = None):
        self.root.geometry(f"+{int(x)}+{int(y)}")
        if alpha is not None:
            self.root.wm_attributes("-alpha", clamp(alpha, 0.0, 1.0))

    def _animate(self, seconds: float, frame, done=None):
        """Call frame(t) with t running 0 -> 1 over `seconds`. Time-based, so a slow frame never makes it stutter."""
        t0 = time.perf_counter()

        def step():
            t = min(1.0, (time.perf_counter() - t0) / seconds)
            try:
                frame(t)
                if t < 1.0:
                    self.root.after(12, step)
                elif done:
                    done()
            except tk.TclError:                                    # window closed mid-animation
                pass
        step()

    def start_errand(self, app: str):
        """Hop up, dive off the bottom of the screen and fade out. The search starts as he drops out of sight."""
        if self.errand:
            return
        x0, y0 = self.root.winfo_x(), self.root.winfo_y()
        off_y = self.root.winfo_screenheight() + 12
        hop, base = 16, self._base_alpha()
        e = self.errand = {"x": x0, "y": y0, "app": app, "away": False, "ok": None,
                           "launched": False, "returning": False}
        self.vel_x = self.vel_y = 0.0
        self.is_held = False
        if self.active_bubble and self.active_bubble.winfo_exists():
            self.active_bubble._close()                            # fades out instead of vanishing
        self.eyes.set_expression("smug", 1.5)
        self.eyes.blink_now(time.time(), 0.2)

        def frame(t):
            if t < 0.22:                                           # 1) anticipation: a quick hop up
                y = y0 - hop * math.sin(math.pi / 2 * (t / 0.22))
            else:                                                  # 2) the dive: speeds up toward the bottom
                y = (y0 - hop) + (off_y - (y0 - hop)) * ease_in_cubic((t - 0.22) / 0.78)
            self._place(x0, y, base * (1.0 - smoothstep((t - 0.6) / 0.4)))
            if t >= 0.5 and not e["launched"]:                     # start typing while he is still dropping
                e["launched"] = True
                self.mish.do_os_search(app)

        def done():
            e["away"] = True
            self._place(x0, off_y, 0.0)
            self.root.after(self.ERRAND_TIMEOUT_MS, self._errand_timeout)
            if e["ok"] is not None:                                # the search already finished
                self._errand_return()
        self._animate(self.ERRAND_DIVE_S, frame, done)

    def errand_finished(self, ok: bool):
        """The search thread is done. He comes back as soon as he is out of sight."""
        e = self.errand
        if not e:
            return
        e["ok"] = ok
        if e["away"]:
            self._errand_return()

    def _errand_timeout(self):
        e = self.errand
        if e and e["away"] and not e["returning"]:
            e["ok"] = bool(e["ok"])
            self._errand_return()

    def _errand_return(self):
        """Spring back up from below the screen to exactly where he was, with a small overshoot."""
        e = self.errand
        if not e or e["returning"]:
            return
        e["returning"] = True
        x0, y0, app, ok = e["x"], e["y"], e["app"], e["ok"]
        off_y = self.root.winfo_screenheight() + 12
        base, top = self._base_alpha(), min(e["y"], 0)
        self.root.lift()
        self.eyes.set_expression("happy" if ok else "sad", 2.2)

        def frame(t):
            y = off_y + (y0 - off_y) * ease_out_back(t)
            self._place(x0, max(y, top), base * smoothstep(t / 0.35))

        def done():
            self._place(x0, y0, base)
            self.errand = None
            self.eyes.blink_now(time.time(), 0.2)
            self.speak(f"Opening {app}." if ok else f"I couldn't search for {app}.", "happy" if ok else "sad")
        self._animate(self.ERRAND_RETURN_S, frame, done)

    # ---- theme -----------------------------------------------------------------------
    def _derive_theme(self):
        s = self.settings
        self.bg, self.eye_col = s["bg"], s["eye"]
        self.panel = _rgb_hex(_mix(_hex_rgb(self.bg), (255, 255, 255), 0.07))    # a touch lighter than the background

    def apply_theme(self):
        """Apply Settings to the window, eyes, gear button, menu and bubbles. Safe to call any time."""
        self._derive_theme()
        s = self.settings
        self.eyes.configure(color=self.eye_col, mood=s["mood"], shape=s["shape"])
        self.draw_squircle_background()
        self.canvas.tag_lower("bg_squircle")                  # keep the background behind the eye pixels
        self.btn_gear.configure(bg=self.panel, fg=self.eye_col, activebackground=self.eye_col, activeforeground=self.bg)
        self.menu.configure(bg=self.panel, fg=self.eye_col, activebackground=self.eye_col, activeforeground=self.bg)
        self.root.wm_attributes("-alpha", 0.3 if self.is_sleeping else s["opacity"])
        self._last_cells = None                               # force a redraw

    def draw_squircle_background(self):
        """Rounded-corner background; the corners use the chroma-key colour so the desktop shows through."""
        c, (w, h), r = self.canvas, self.SIZE, self.RADIUS
        c.delete("bg_squircle")
        kw = dict(outline="", tags="bg_squircle")
        c.create_rectangle(r, 0, w - r, h, fill=self.bg, **kw)
        c.create_rectangle(0, r, w, h - r, fill=self.bg, **kw)
        for x0, y0 in ((0, 0), (w - 2 * r, 0), (0, h - 2 * r), (w - 2 * r, h - 2 * r)):
            c.create_oval(x0, y0, x0 + 2 * r, y0 + 2 * r, fill=self.bg, **kw)
        arc = dict(style="arc", outline=self.panel, width=2, tags="bg_squircle")
        c.create_arc(0, 0, 2 * r, 2 * r, start=90, extent=90, **arc)
        c.create_arc(w - 2 * r, 0, w, 2 * r, start=0, extent=90, **arc)
        c.create_arc(0, h - 2 * r, 2 * r, h, start=180, extent=90, **arc)
        c.create_arc(w - 2 * r, h - 2 * r, w, h, start=270, extent=90, **arc)
        line = dict(fill=self.panel, width=2, tags="bg_squircle")
        c.create_line(r, 0, w - r, 0, **line)
        c.create_line(r, h, w - r, h, **line)
        c.create_line(0, r, 0, h - r, **line)
        c.create_line(w, r, w, h - r, **line)

    def open_tasks(self):
        win = getattr(self, "tasks_win", None)
        try:
            if win is not None and win.winfo_exists():
                win.lift()
                return
        except tk.TclError:
            pass
        self.tasks_win = TaskPanel(self)

    def open_customize(self):
        if self.cust is not None:
            try:
                if self.cust.winfo_exists():
                    self.cust.lift()
                    return
            except tk.TclError:
                pass
        from tkinter import colorchooser
        PANEL, TEXT, TROUGH = "#16161d", "#e8e8ee", "#2a2a35"   # fixed dark panel so labels always stay readable
        s = self.settings
        win = self.cust = tk.Toplevel(self.root)
        win.title("Customize Mish")
        win.configure(bg=PANEL)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        px = self.root.winfo_x() + self.root.winfo_width() + 12
        if px + 340 > self.root.winfo_screenwidth():
            px = max(10, self.root.winfo_x() - 352)
        win.geometry(f"+{px}+{max(10, self.root.winfo_y())}")
        building = [True]                                        # ignore callbacks fired while the panel is built

        def change(**kw):
            if not building[0]:
                s.update(**kw)
                self.apply_theme()

        def heading(text):
            tk.Label(win, text=text, bg=PANEL, fg=TEXT, font=("Segoe UI", 9, "bold"),
                     anchor="w").pack(fill="x", padx=12, pady=(10, 3))

        def swatches(presets, key):
            row = tk.Frame(win, bg=PANEL)
            row.pack(fill="x", padx=10)
            for _name, hexv in presets:
                tk.Button(row, bg=hexv, activebackground=hexv, width=3, height=1, bd=0, relief="flat",
                          cursor="hand2", command=lambda h=hexv: change(**{key: h})).pack(side="left", padx=2, pady=2)

            def custom():
                _rgb, picked = colorchooser.askcolor(color=s[key], parent=win, title="Pick a colour")
                if picked:
                    change(**{key: picked})
            tk.Button(row, text="Custom...", command=custom, bg=TROUGH, fg=TEXT, bd=0, relief="flat",
                      cursor="hand2", padx=6).pack(side="left", padx=6)

        heading("Eye colour")
        swatches(EYE_PRESETS, "eye")
        heading("Background")
        swatches(BG_PRESETS, "bg")

        heading("Eye shape")
        shape_var = tk.StringVar(master=win, value=s["shape"])
        row = tk.Frame(win, bg=PANEL)
        row.pack(fill="x", padx=10)
        for label, key in (("Round", "round"), ("Square", "square"), ("Pill", "pill"), ("Slim", "slim"), ("Visor", "visor")):
            tk.Radiobutton(row, text=label, value=key, variable=shape_var,
                           command=lambda: change(shape=shape_var.get()), bg=PANEL, fg=TEXT, selectcolor=TROUGH,
                           activebackground=PANEL, activeforeground=TEXT, bd=0,
                           highlightthickness=0).pack(side="left", padx=4)

        heading("Opacity")
        scale = tk.Scale(win, from_=40, to=100, orient="horizontal", length=290, bg=PANEL, fg=TEXT,
                         troughcolor=TROUGH, highlightthickness=0, bd=0,
                         command=lambda v: change(opacity=float(v) / 100.0))
        scale.set(int(round(s["opacity"] * 100)))
        scale.pack(padx=12)

        mood_var = tk.BooleanVar(master=win, value=s["mood"])
        tk.Checkbutton(win, text="Mood colours (red = angry, amber = thinking...)", variable=mood_var,
                       command=lambda: change(mood=mood_var.get()), bg=PANEL, fg=TEXT, selectcolor=TROUGH,
                       activebackground=PANEL, activeforeground=TEXT, bd=0,
                       highlightthickness=0).pack(anchor="w", padx=10, pady=(8, 0))

        def reset():
            s.reset()
            self.apply_theme()
            win.destroy()
            self.open_customize()

        btns = tk.Frame(win, bg=PANEL)
        btns.pack(fill="x", padx=12, pady=12)
        tk.Button(btns, text="Reset", command=reset, bg=TROUGH, fg=TEXT, bd=0, relief="flat",
                  cursor="hand2", padx=12, pady=3).pack(side="left")
        tk.Button(btns, text="Done", command=win.destroy, bg=PANEL, fg=TEXT, bd=0, relief="flat",
                  cursor="hand2", padx=12, pady=3).pack(side="right")
        building[0] = False

    # ---- speech bubbles, menu, chat box -------------------------------------------------
    def show_speech_bubble(self, text: str, duration: int | None = None, timer: bool = False):
        if self.active_bubble and self.active_bubble.winfo_exists():
            self.active_bubble.destroy()
        self._bubble_is_timer = timer
        self.active_bubble = DynamicSpeechBubble(self.root, text, duration or speech_duration_ms(text),
                                                 accent=self.eye_col, bg=self.panel)

    def clear_timer_bubble(self):
        if self._bubble_is_timer:
            if self.active_bubble and self.active_bubble.winfo_exists():
                self.active_bubble.destroy()
            self._bubble_is_timer = False
            if getattr(self, "_mode", "float") == "island":
                self.collapse_island()

    def show_timer_bubble(self, minutes: int, seconds: int, phase: str = "work"):
        """Live 1s focus/break countdown. Never covers speech; updates in-place without flickering."""
        if self.active_bubble and self.active_bubble.winfo_exists() and not self._bubble_is_timer:
            return
        icon = "" if phase == "work" else ""
        tag = "Focus" if phase == "work" else "Break"
        label = f"{icon} {tag}: {minutes:02d}:{seconds:02d}"
        if self._bubble_is_timer and self.active_bubble and self.active_bubble.winfo_exists():
            for w in self.active_bubble.winfo_children():
                for child in w.winfo_children():
                    if isinstance(child, tk.Label):
                        child.configure(text=label)
                        return
        self.show_speech_bubble(label, duration=3000, timer=True)

    def speak(self, text: str, hint: str | None = None):
        """Bubble + the face that matches his words + a talking pulse."""
        dur = speech_duration_ms(text)
        self.eyes.set_expression(pick_expression(text, hint), dur / 1000.0 * 0.9)
        self.eyes.start_talking(len(text))
        self.show_speech_bubble(text, dur)

    def show_menu(self):
        x = self.root.winfo_rootx() + self.btn_gear.winfo_x()
        y = self.root.winfo_rooty() + self.btn_gear.winfo_y()
        try:
            self.menu.tk_popup(x, y)
        finally:
            self.menu.grab_release()

    def open_chat_dialog(self):
        existing = getattr(self, "_chat_dlg", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.destroy()
                    return
            except tk.TclError:
                pass
        if self.mish.strike_mode:
            self.mish.react_async("strike_chat", extra="they tried to open the chat box", force=True)
            return
        dlg = self._chat_dlg = tk.Toplevel(self.root)
        dlg.configure(bg=self.bg)
        dlg.overrideredirect(True)
        dlg.attributes("-topmost", True)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        px = clamp(self.root.winfo_x() + (self.root.winfo_width() // 2) - 140, 10, sw - 290)
        py = self.root.winfo_y() + self.root.winfo_height() + 5
        if py + 75 > sh:
            py = max(10, self.root.winfo_y() - 75)
        dlg.geometry(f"280x70+{int(px)}+{int(py)}")

        entry = tk.Entry(dlg, bg=self.panel, fg=self.eye_col, insertbackground=self.eye_col,
                         font=("Consolas", 10), relief="flat")
        entry.pack(fill="x", padx=8, pady=16, ipady=4)
        entry.focus_force()
        entry.bind("<Escape>", lambda e: dlg.destroy())

        self.is_listening = True                              # he looks down at the box while it is open
        dlg.bind("<Destroy>", lambda e: setattr(self, "is_listening", False) if e.widget is dlg else None)

        def send(_=None):
            txt = entry.get().strip()
            dlg.destroy()
            if txt:
                threading.Thread(target=self.mish.handle_user_input, args=(txt,), daemon=True).start()

        entry.bind("<Return>", send)
        dlg.bind("<FocusOut>", lambda e: dlg.destroy())

    # ---- file drop ---------------------------------------------------------------------
    def enable_file_drop(self):
        """Drop a text/code file on his face. Needs the small `windnd` package (pip install windnd)."""
        try:
            import windnd
        except ImportError:
            log("windnd not installed: file drop disabled (use !read <file> instead)")
            return

        def on_drop(files):
            paths = [f.decode("utf-8", "replace") if isinstance(f, bytes) else str(f) for f in files]
            self.eyes.set_expression("curious", 1.2)
            self.mish.handle_files(paths)
        try:
            windnd.hook_dropfiles(self.root, func=on_drop, force_unicode=True)
        except Exception as e:
            log("file drop hook failed:", repr(e))

    # ---- drawing -------------------------------------------------------------------------
    def draw_cells(self, cells: dict):
        """Draw {(x, y): colour}. Skips identical frames and merges horizontal pixel runs into single rectangles."""
        key = tuple(sorted(cells.items()))
        if key == self._last_cells:
            return
        self._last_cells = key
        self.canvas.delete("pix")
        P = self.P_SIZE
        rows: dict = {}
        for (x, y), col in cells.items():
            rows.setdefault((y, col), []).append(x)
        for (y, col), xs in rows.items():
            xs.sort()
            start = prev = xs[0]
            for x in xs[1:] + [None]:
                if x is not None and x == prev + 1:
                    prev = x
                    continue
                self.canvas.create_rectangle(start * P, y * P, (prev + 1) * P, (y + 1) * P,
                                             fill=col, outline="", tags="pix")
                if x is not None:
                    start = prev = x

    def render_16bit_eyes(self):
        now = time.time()
        try:
            pointer = self.root.winfo_pointerxy()
        except tk.TclError:
            pointer = None
        face = (self.root.winfo_x() + self.FACE_CENTER[0], self.root.winfo_y() + self.FACE_CENTER[1])
        inertia = (0.0, 0.0) if self.is_sleeping else (
            clamp(-self.vel_x * 0.12, -4, 4), clamp(-self.vel_y * 0.12, -3, 3))
        self.eyes.update(now, {
            "strike": self.mish.strike_mode, "sleeping": self.is_sleeping, "scared": self.is_scared,
            "dizzy": self.is_dizzy, "held": self.is_held, "thinking": self.mish.thinking_visible,
            "listening": self.is_listening, "focus": self.mish.in_focus(), "idle_t": self.idle_t,
            "pointer": pointer, "face": face, "inertia": inertia,
        })
        cells = self.eyes.cells(now)
        if self.is_sleeping:
            self.add_zzz(cells)
        self.draw_cells(cells)

    def add_zzz(self, cells: dict):
        """Floating "Zzz" on whichever upper corner is away from the screen edge."""
        start_x = 4 if self.root.winfo_x() > self.root.winfo_screenwidth() // 2 else 48
        self.zzz_frame = (self.zzz_frame + 1) % 120
        big_y = 6 + int(math.sin(self.zzz_frame * 0.15) * 3)
        small_y = 1 + int(math.sin(self.zzz_frame * 0.15 + 1.2) * 2)
        zc = self.eyes.hex_color()
        for r, row in enumerate(("###", ".#.", "###")):
            for c, ch in enumerate(row):
                if ch == "#":
                    cells[(start_x + c, big_y + r)] = zc
                    cells[(start_x + c + 5, small_y + r)] = zc

    # ---- physics and states ---------------------------------------------------------------
    def update_physics(self):
        now = time.time()
        dt = max(now - self.last_move_t, 0.001)
        cur_x, cur_y = self.root.winfo_x(), self.root.winfo_y()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()

        if self.errand:                                       # scripted movement is not being shaken, and the
            self.vel_x = self.vel_y = self.shake_energy = 0.0  # screen edge is where he is meant to go
            self.last_pos_x, self.last_pos_y, self.last_move_t = cur_x, cur_y, now
            self.is_scared, self.edge_hold_start = False, 0.0
            return
        vx, vy = (cur_x - self.last_pos_x) / dt, (cur_y - self.last_pos_y) / dt
        self.vel_x += (vx - self.vel_x) * 0.25
        self.vel_y += (vy - self.vel_y) * 0.25

        if math.hypot(vx - self.vel_x, vy - self.vel_y) > 2500:       # shaking him dizzy
            self.shake_energy += 1.0
            if self.shake_energy > 4.0 and not self.is_dizzy and not self.mish.strike_mode:
                self.trigger_dizzy_state()
        else:
            self.shake_energy = max(0.0, self.shake_energy - 0.1)
        self.last_pos_x, self.last_pos_y, self.last_move_t = cur_x, cur_y, now

        idle_t = self.idle_t = idle_seconds()                          # falls asleep when you leave
        if idle_t >= AUTO_SLEEP_TIMEOUT and not self.is_sleeping and not self.mish.strike_mode:
            self.is_sleeping = True
            self.root.wm_attributes("-alpha", 0.3)
        elif idle_t < AUTO_SLEEP_TIMEOUT and self.is_sleeping:
            self.is_sleeping = False
            self.root.wm_attributes("-alpha", self.settings["opacity"])
            self.eyes.set_expression("surprised", 1.2)
            self.mish.react_async("wake")

        off_edge = cur_x < -20 or cur_x > sw - 180 or cur_y < -20 or cur_y > sh - 90   # scared at the screen edge
        if off_edge and not self.mish.strike_mode:
            self.is_scared = True
            if self.edge_hold_start == 0.0:
                self.edge_hold_start = now
                self.mish.react_async("scared", force=True)
            elif now - self.edge_hold_start > 6.0:
                self.trigger_strike_mode()
        else:
            self.is_scared = False
            self.edge_hold_start = 0.0

        if self.mish.strike_mode and now > self.strike_until:
            self.mish.strike_mode = False
            self.mish.react_async("strike_end", force=True)

    def snap_to_center_punish(self, window_name: str):
        """Focus mode: jump to the middle of the screen and demand that the distraction be closed."""
        w, h = self.SIZE
        self.root.geometry(f"+{self.root.winfo_screenwidth() // 2 - w // 2}+{self.root.winfo_screenheight() // 2 - h // 2}")
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.mish.react_async("focus_punish", window_name[:40], force=True)

    def trigger_dizzy_state(self):
        self.is_dizzy = True
        self.mish.react_async("dizzy", force=True)

        def recover():
            time.sleep(3.0)
            self.is_dizzy = False
            self.ui.put(("expr", "sad", 2.5))                   # the "never again" look
        threading.Thread(target=recover, daemon=True).start()

    def trigger_strike_mode(self):
        self.is_scared = False
        self.mish.strike_mode = True
        self.strike_until = time.time() + 60.0
        self.mish.react_async("strike_start", force=True)

    def animate_loop(self):
        try:
            self.update_physics()
            self.render_16bit_eyes()
        except Exception as e:
            log("animate_loop error:", repr(e))
        self.root.after(35, self.animate_loop)

    # ---- mouse -----------------------------------------------------------------------------
    def on_press(self, event):
        if self.errand:
            return
        self.drag_x, self.drag_y = event.x, event.y
        self.press_root = (event.x_root, event.y_root)
        self.moved = False

    def on_drag(self, event):
        if self.errand:
            return
        if not self.moved:
            if math.hypot(event.x_root - self.press_root[0], event.y_root - self.press_root[1]) < 5:
                return                                          # still just a click
            self.moved = True
            self.is_held = True                                 # picked up -> worried eyes
        self.root.geometry(f"+{self.root.winfo_x() + event.x - self.drag_x}+{self.root.winfo_y() + event.y - self.drag_y}")
        if self.active_bubble and self.active_bubble.winfo_exists():
            self.active_bubble.update_position()

    def on_release(self, event):
        if self.errand:
            return
        if self.is_held:
            self.is_held = False
            self.eyes.set_expression("happy", 1.4)              # relief at being put down
        elif not self.moved:
            self.poke()

    def poke(self):
        if self.mish.strike_mode:
            return
        now = time.time()
        self.poke_times = [t for t in self.poke_times if now - t < 6.0] + [now]
        n = len(self.poke_times)
        self.eyes.set_expression("angry" if n >= 5 else "squint" if n >= 3 else "surprised", 2.0)
        self.mish.react_async("poke", extra=f"poked {n} time{'s' if n > 1 else ''} in the last few seconds")

    # ---- messages from the worker threads --------------------------------------------------------
    def poll(self):
        try:
            while True:
                item = self.ui.get_nowait()
                kind = item[0]
                if kind == "quit":
                    self.root.destroy()
                    return
                try:
                    if kind == "say":
                        self.speak(item[1], item[2] if len(item) > 2 else None)
                    elif kind == "errand_start":
                        self.start_errand(item[1])
                    elif kind == "errand_done":
                        self.errand_finished(bool(item[1]))
                    elif kind == "expr":
                        self.eyes.set_expression(item[1], item[2] if len(item) > 2 else 2.0)
                    elif kind == "timer":
                        self.show_timer_bubble(item[1], item[2], item[3] if len(item) > 3 else "work")
                    elif kind == "timer_clear":
                        self.clear_timer_bubble()
                    elif kind == "focus_punish":
                        self.snap_to_center_punish(item[1])
                    elif kind == "show":
                        self.root.deiconify()
                    elif kind == "hide":
                        self.root.withdraw()
                    elif kind == "summon":
                        self.summon()
                except Exception as e:
                    log("ui event failed:", kind, repr(e))
        except queue.Empty:
            pass
        self.root.after(150, self.poll)


_SS = 4                                         # supersampling factor
_FONT_CACHE: dict = {}
_FONT_FILES = ("segoeui.ttf", "seguisb.ttf", "arial.ttf", "DejaVuSans.ttf")
_EMOJI_FILES = ("seguiemj.ttf",)


def _font(px: float, emoji: bool = False):
    key = (round(px * _SS), emoji)
    f = _FONT_CACHE.get(key)
    if f is None:
        for name in (_EMOJI_FILES if emoji else _FONT_FILES):
            try:
                f = ImageFont.truetype(name, key[0])
                break
            except Exception:
                continue
        if f is None and not emoji:
            try:
                f = ImageFont.load_default(key[0])
            except Exception:
                f = ImageFont.load_default()
        _FONT_CACHE[key] = f
    return f


def _is_emoji(ch: str) -> bool:
    o = ord(ch)
    return 0x2300 <= o <= 0x23FF or o >= 0x2600


def _runs(text: str):
    out: list = []
    for ch in text.replace("\ufe0f", ""):
        e = _is_emoji(ch)
        if out and out[-1][0] == e:
            out[-1][1] += ch
        else:
            out.append([e, ch])
    return out


def _measure(text: str, px: float) -> float:
    total = 0.0
    for e, run in _runs(text):
        f = _font(px, e) or _font(px)
        total += f.getlength(run)
    return total / _SS


def _wrap(text: str, px: float, width: float, max_lines: int) -> list:
    lines: list = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split(" "):
            trial = (cur + " " + word).strip() if cur else word
            if _measure(trial, px) <= width:
                cur = trial
                continue
            if cur:
                lines.append(cur)
            cur = ""
            while _measure(word, px) > width and len(word) > 1:      # break an over-long word
                n = len(word)
                while n > 1 and _measure(word[:n], px) > width:
                    n -= 1
                lines.append(word[:n])
                word = word[n:]
            cur = word
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and _measure(last + "...", px) > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "..."
    return lines


def _gear_points(cx, cy, r_out, r_in, teeth=8):
    pts, step = [], 2 * math.pi / teeth
    for k in range(teeth):
        a = k * step
        for ang, rad in ((a - 0.36 * step, r_in), (a - 0.20 * step, r_out),
                         (a + 0.20 * step, r_out), (a + 0.36 * step, r_in)):
            pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
    return pts


class LayeredMishWindow(RobotFaceWindow):
    """RobotFaceWindow rendered through UpdateLayeredWindow, plus the top handle and Dynamic Island."""

    HANDLE = (88, 9)
    ISLAND_W, ISLAND_MIN_H, ISLAND_MAX_LINES = 380, 72, 6
    ISLAND_Y, DOCK_Y, DOCK_ZONE_Y, PULL_DY = 8, 18, 30, 14
    GEAR_RECT = (200, 95, 222, 117)                  # gear hit box inside the 240x130 body
    TEXT_COL, ISLAND_FONT_PX, LINE_H = (233, 234, 240), 13, 18
    EYE_CROP = (4, 3, 50, 22)                        # grid region holding the two eyes (cells)

    # ------------------------------------------------------------------ setup
    def __init__(self, root, mish, ui_queue):
        # everything the first animate_loop() tick (inside super().__init__) may touch
        self._sf = _LayeredSurface()
        self._alpha = float(mish.settings["opacity"])
        self._mode = "float"                         # logical mode: float | handle | island
        self._g = dict(w=float(self.SIZE[0]), h=float(self.SIZE[1]), r=float(self.RADIUS), flat=0.0)
        self._wt = dict(body=1.0, island=0.0, handle=0.0)
        self._cells: dict = {}
        self._dirty = True
        self._frame_ready = False
        self._anim_tok = 0
        self._anim_on = False
        self._dock_mon = None
        self._float_target = (100, 100)
        self._isl = None                             # island layout dict while a message is showing
        self._isl_after = None
        self._isl_end = 0.0
        self._timer_mute_until = 0.0
        self._press = None
        self._pulled = False
        self._near_top = False
        self._orig_wma = root.wm_attributes
        root.wm_attributes = self._wma_proxy         # Tk must never touch -alpha / -transparentcolor
        super().__init__(root, mish, ui_queue)

        self.canvas.pack_forget()                    # the old Tk-drawn widgets are replaced by the bitmap
        self.btn_gear.place_forget()
        self.dock_menu = tk.Menu(root, tearoff=0)
        self.dock_menu.add_command(label="⬇ Pop Out", command=self.pop_out)
        self.dock_menu.add_command(label="💬 Chat Input", command=self.open_chat_dialog)
        self.dock_menu.add_separator()
        self.dock_menu.add_command(label="❌ Exit", command=self.root.destroy)
        self.menu.insert_command(5, label="📌 Tuck to Top Handle", command=self.tuck_to_top)
        for seq, fn in (("<ButtonPress-1>", self._ev_press), ("<B1-Motion>", self._ev_drag),
                        ("<ButtonRelease-1>", self._ev_release), ("<Button-3>", self._ev_right),
                        ("<Motion>", self._ev_motion), ("<Map>", lambda e: self._invalidate())):
            root.bind(seq, fn, add="+")
        self.apply_theme()
        self._invalidate()
        self._compose()
        self._apply()

    def _wma_proxy(self, *a, **kw):
        """Intercept the two Tk attributes that fight with UpdateLayeredWindow; everything else passes through."""
        if a and a[0] in ("-alpha", "-transparentcolor"):
            if a[0] == "-alpha":
                if len(a) == 1:
                    return self._alpha
                self._alpha = clamp(float(a[1]), 0.0, 1.0)
                if self._frame_ready:
                    self._apply()
            elif len(a) == 1:
                return ""
            return None
        return self._orig_wma(*a, **kw)

    # ------------------------------------------------------------------ geometry helpers
    def _win_xy(self):
        try:
            return self._sf.rect() if self._sf.hwnd else (self.root.winfo_x(), self.root.winfo_y())
        except Exception:
            return self.root.winfo_x(), self.root.winfo_y()

    def _monitor_rect(self):
        """(left, top, right, bottom) of the monitor Mish is on (or is docked to)."""
        if self._dock_mon:
            return self._dock_mon
        try:
            if self._sf.hwnd:
                hm = _U32.MonitorFromWindow(self._sf.hwnd, 2)          # MONITOR_DEFAULTTONEAREST
                mi = _MONINFO()
                mi.cbSize = ctypes.sizeof(_MONINFO)
                if _U32.GetMonitorInfoW(hm, ctypes.byref(mi)):
                    r = mi.rcMonitor
                    return r.left, r.top, r.right, r.bottom
        except Exception:
            pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def docked(self) -> bool:
        return self._mode in ("handle", "island")

    def _target(self, mode: str) -> dict:
        l, t, r, _b = self._monitor_rect()
        cx = (l + r) // 2
        if mode == "handle":
            w, h = self.HANDLE
            return dict(x=cx - w // 2, y=t, w=float(w), h=float(h), r=h / 2, flat=1.0)
        if mode == "island":
            iw, ih = (self._isl["w"], self._isl["h"]) if self._isl else (self.ISLAND_W, self.ISLAND_MIN_H)
            return dict(x=cx - iw // 2, y=t + self.ISLAND_Y, w=float(iw), h=float(ih), r=min(36.0, ih / 2), flat=0.0)
        fx, fy = self._float_target
        return dict(x=fx, y=fy, w=float(self.SIZE[0]), h=float(self.SIZE[1]), r=float(self.RADIUS), flat=0.0)

    @staticmethod
    def _weights(mode: str) -> dict:
        return dict(body=float(mode == "float"), island=float(mode == "island"), handle=float(mode == "handle"))

    # ------------------------------------------------------------------ rendering (Pillow, 4x supersampled)
    def _invalidate(self):
        self._dirty = True
        self._last_cells = None

    def _rgb(self, h: str):
        return _hex_rgb(h)

    def _draw_body(self, w: int, h: int) -> "Image.Image":
        S = _SS
        lay = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
        d = ImageDraw.Draw(lay)
        ox, oy = (w - self.SIZE[0]) / 2.0, (h - self.SIZE[1]) / 2.0
        P = self.P_SIZE
        for (cx, cy), col in self._cells.items():
            try:
                rgb = _hex_rgb(col)
            except Exception:
                continue
            x0, y0 = (ox + cx * P) * S, (oy + cy * P) * S
            d.rectangle((x0, y0, x0 + P * S - 1, y0 + P * S - 1), fill=rgb + (255,))
        gx0, gy0, gx1, gy1 = self.GEAR_RECT                          # gear icon
        gx0, gy0, gx1, gy1 = [(v + o) * S for v, o in ((gx0, ox), (gy0, oy), (gx1, ox), (gy1, oy))]
        d.rounded_rectangle((gx0, gy0, gx1, gy1), radius=6 * S, fill=_hex_rgb(self.panel) + (255,))
        gcx, gcy = (gx0 + gx1) / 2, (gy0 + gy1) / 2
        eye = _hex_rgb(self.eye_col) + (255,)
        d.polygon(_gear_points(gcx, gcy, 7.2 * S, 5.3 * S), fill=eye)
        d.ellipse((gcx - 5.0 * S, gcy - 5.0 * S, gcx + 5.0 * S, gcy + 5.0 * S), fill=eye)
        d.ellipse((gcx - 2.2 * S, gcy - 2.2 * S, gcx + 2.2 * S, gcy + 2.2 * S), fill=_hex_rgb(self.panel) + (255,))
        return lay

    def _draw_island(self, w: int, h: int) -> "Image.Image":
        S = _SS
        lay = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
        isl = self._isl
        if not isl:
            return lay
        d = ImageDraw.Draw(lay)
        ox, oy = (w - isl["w"]) / 2.0, (h - isl["h"]) / 2.0
        c0, r0, c1, r1 = self.EYE_CROP
        cell = 2                                                     # compact eyes: 2 px per pixel-art cell
        ex = ox + 16
        ey = oy + (isl["h"] - (r1 - r0) * cell) / 2.0
        for (cx, cy), col in self._cells.items():
            if not (c0 <= cx < c1 and r0 <= cy < r1):
                continue
            try:
                rgb = _hex_rgb(col)
            except Exception:
                continue
            x0, y0 = (ex + (cx - c0) * cell) * S, (ey + (cy - r0) * cell) * S
            d.rectangle((x0, y0, x0 + cell * S - 1, y0 + cell * S - 1), fill=rgb + (255,))
        sep_x = (ox + 16 + (c1 - c0) * cell + 10) * S                # thin accent divider
        d.rounded_rectangle((sep_x, (oy + 16) * S, sep_x + 2 * S, (oy + isl["h"] - 16) * S), radius=S,
                            fill=_hex_rgb(self.eye_col) + (150,))
        tx = ox + isl["tx"]
        ty = oy + (isl["h"] - len(isl["lines"]) * self.LINE_H) / 2.0
        px = self.ISLAND_FONT_PX
        for i, line in enumerate(isl["lines"]):
            x = tx
            y = ty + i * self.LINE_H
            for emoji, run in _runs(line):
                f = _font(px, emoji) or _font(px)
                try:
                    if emoji:
                        d.text((x * S, y * S), run, font=f, embedded_color=True)
                    else:
                        d.text((x * S, y * S), run, font=f, fill=self.TEXT_COL + (255,))
                except Exception:
                    pass
                x += f.getlength(run) / S
        return lay

    def _draw_handle(self, w: int, h: int) -> "Image.Image":
        S = _SS
        lay = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
        acc = _hex_rgb(self.eye_col)
        bw, bh = 30, 3
        x0, y0 = (w - bw) / 2.0 * S, (h - bh) / 2.0 * S + S * 0.5
        glow = Image.new("RGBA", lay.size, (0, 0, 0, 0))
        ImageDraw.Draw(glow).rounded_rectangle((x0 - 3 * S, y0 - S, x0 + (bw + 3) * S, y0 + (bh + 1) * S),
                                               radius=2 * S, fill=acc + (120,))
        glow = glow.filter(ImageFilter.GaussianBlur(2.2 * S))
        lay = Image.alpha_composite(lay, glow)
        ImageDraw.Draw(lay).rounded_rectangle((x0, y0, x0 + bw * S, y0 + bh * S), radius=bh * S / 2,
                                              fill=acc + (255,))
        return lay

    def _compose(self):
        g = self._g
        w, h = max(1, int(round(g["w"]))), max(1, int(round(g["h"])))
        S = _SS
        W, H = w * S, h * S
        top = -g["flat"] * 2 * g["r"] * S                            # flat top = rounding pushed off the screen edge
        rad = max(0.0, min(g["r"] * S, (W - 1) / 2.0, (H - 1 - top) / 2.0))
        box = (0, top, W - 1, H - 1)
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).rounded_rectangle(box, radius=rad, fill=255)
        shell = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(shell).rounded_rectangle(box, radius=rad, fill=_hex_rgb(self.bg) + (255,),
                                                outline=_hex_rgb(self.panel) + (255,), width=2 * S)
        content = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        for name, draw in (("body", self._draw_body), ("island", self._draw_island), ("handle", self._draw_handle)):
            wt = self._wt.get(name, 0.0)
            if wt <= 0.01:
                continue
            lay = draw(w, h)
            if wt < 0.99:
                lay.putalpha(lay.getchannel("A").point(lambda v, k=wt: int(v * k)))
            content = Image.alpha_composite(content, lay)
        content.putalpha(ImageChops.multiply(content.getchannel("A"), mask))
        out = Image.alpha_composite(shell, content).resize((w, h), Image.Resampling.LANCZOS)
        self._sf.attach(self.root)
        self._sf.upload(out)
        self._frame_ready = True

    def _apply(self, x=None, y=None):
        if not self._frame_ready:
            self._compose()
        self._sf.present(x, y, self._alpha)

    # ------------------------------------------------------------------ overrides of the old canvas drawing
    def draw_squircle_background(self):
        self._invalidate()

    def apply_theme(self):
        super().apply_theme()
        dm = getattr(self, "dock_menu", None)
        if dm is not None:
            dm.configure(bg=self.panel, fg=self.eye_col, activebackground=self.eye_col, activeforeground=self.bg)
        self._invalidate()

    def draw_cells(self, cells: dict):
        key = tuple(sorted(cells.items()))
        dirty = self._dirty
        if key == self._last_cells and not dirty:
            return
        self._last_cells, self._cells, self._dirty = key, dict(cells), False
        if self._anim_on or (self._mode == "handle" and not dirty):
            return                                                   # animation frames / handle need no eye redraw
        self._compose()
        self._apply()

    def _place(self, x: float, y: float, alpha: float | None = None):
        """Used by the errand animation: move the window and fade it with the layered alpha."""
        self.root.geometry(f"+{int(x)}+{int(y)}")
        if alpha is not None:
            self._alpha = clamp(alpha, 0.0, 1.0)
        self._apply(int(x), int(y))

    def show_menu(self):
        x = self.root.winfo_rootx() + self.GEAR_RECT[0]
        y = self.root.winfo_rooty() + self.GEAR_RECT[3]
        try:
            self.menu.tk_popup(x, y)
        finally:
            self.menu.grab_release()

    # ------------------------------------------------------------------ cancellable morph animation
    def _morph(self, mode: str, seconds: float, done=None, spring: bool = False):
        """Animate the window (position, size, corner radius, content cross-fade) to `mode`."""
        self._anim_tok += 1
        tok = self._anim_tok
        self._mode = mode
        self._anim_on = True
        src_g, (sx, sy) = dict(self._g), self._win_xy()
        src_wt, dst_wt = dict(self._wt), self._weights(mode)
        dst = self._target(mode)
        t0 = time.perf_counter()

        def step():
            if tok != self._anim_tok:
                return                                               # superseded by a newer animation
            t = min(1.0, (time.perf_counter() - t0) / seconds)
            e = ease_out_back(t, 0.7) if spring else smoothstep(t)
            f = smoothstep(t)
            self._g = {k: src_g[k] + (dst[k] - src_g[k]) * e for k in ("w", "h", "r", "flat")}
            self._g["w"], self._g["h"] = max(1.0, self._g["w"]), max(1.0, self._g["h"])
            self._wt = {k: src_wt[k] + (dst_wt[k] - src_wt[k]) * f for k in dst_wt}
            x = int(round(sx + (dst["x"] - sx) * e))
            y = int(round(sy + (dst["y"] - sy) * e))
            try:
                self.root.geometry(f"{int(round(self._g['w']))}x{int(round(self._g['h']))}+{x}+{y}")
                self._compose()
                self._apply(x, y)
            except tk.TclError:
                return
            if t < 1.0:
                self.root.after(12, step)
            else:
                self._anim_on = False
                self._invalidate()
                if done:
                    done()
        step()

    # ------------------------------------------------------------------ top handle
    def tuck_to_top(self):
        if self._mode != "float" or self.errand:
            return
        self._dock_mon = self._monitor_rect()
        self.is_held = self.is_scared = False
        self.edge_hold_start = 0.0
        if self.active_bubble and self.active_bubble.winfo_exists():
            self.active_bubble._close()
        self.eyes.set_expression("smug", 1.0)
        self._morph("handle", 0.34)

    def summon(self):
        """Global hotkey summon: pop out if tucked, bring to front, and open quick chat."""
        self.root.deiconify()
        self.root.lift()
        if self.docked():
            self.pop_out(immediate=True)
        self.root.after(40, self.open_chat_dialog)

    def pop_out(self, immediate: bool = False):
        if self._mode == "float":
            return
        l, t, r, _b = self._monitor_rect()
        remaining = (self._isl_end - time.time()) * 1000 if self._isl else 0
        text = self._isl["text"] if self._isl else None
        self._cancel_island_timer()
        self._float_target = ((l + r) // 2 - self.SIZE[0] // 2, t + 36)
        self.eyes.set_expression("happy", 1.4)

        def done():
            self._dock_mon = None
            self._isl = None
            self._invalidate()
            if text and remaining > 900:                             # carry the unfinished message into a normal bubble
                bubble_text = text
                self.root.after(60, lambda: RobotFaceWindow.show_speech_bubble(self, bubble_text, int(remaining)))
        if immediate:
            self._anim_tok += 1
            self._anim_on = False
            self._mode = "float"
            tgt = self._target("float")
            self._g = dict(w=tgt["w"], h=tgt["h"], r=tgt["r"], flat=0.0)
            self._wt = self._weights("float")
            self.root.geometry(f"{self.SIZE[0]}x{self.SIZE[1]}+{tgt['x']}+{tgt['y']}")
            self._compose()
            self._apply(tgt["x"], tgt["y"])
            done()
        else:
            self._morph("float", 0.42, done=done, spring=True)

    # ------------------------------------------------------------------ Dynamic Island
    def _layout_island(self, text: str) -> dict:
        tx = 16 + (self.EYE_CROP[2] - self.EYE_CROP[0]) * 2 + 10 + 2 + 12        # eyes + divider + gap
        width = self.ISLAND_W - tx - 20
        lines = _wrap(text, self.ISLAND_FONT_PX, width, self.ISLAND_MAX_LINES)
        h = max(self.ISLAND_MIN_H, len(lines) * self.LINE_H + 30)
        return dict(text=text, lines=lines, tx=tx, w=self.ISLAND_W, h=h)

    def _cancel_island_timer(self):
        if self._isl_after is not None:
            try:
                self.root.after_cancel(self._isl_after)
            except Exception:
                pass
            self._isl_after = None

    def show_speech_bubble(self, text: str, duration: int | None = None, timer: bool = False):
        if not self.docked():
            return super().show_speech_bubble(text, duration, timer)
        if timer and time.time() < self._timer_mute_until:
            return
        self._bubble_is_timer = timer
        dur = duration or speech_duration_ms(text)
        new = self._layout_island(text)
        same_box = self._mode == "island" and not self._anim_on and self._isl \
            and (self._isl["w"], self._isl["h"]) == (new["w"], new["h"])
        self._isl, self._isl_end = new, time.time() + dur / 1000.0
        self._cancel_island_timer()
        self._isl_after = self.root.after(dur, self.collapse_island)
        if same_box:
            self._invalidate()                                       # same size: just swap the text
        else:
            self._morph("island", 0.42, spring=True)

    def show_timer_bubble(self, minutes: int, seconds: int, phase: str = "work"):
        if self.docked():
            if self._mode == "island" and self._isl and not self._bubble_is_timer:
                return                                               # never cover a real message with a countdown
            icon = "" if phase == "work" else ""
            tag = "Focus" if phase == "work" else "Break"
            self.show_speech_bubble(f"{icon} {tag}: {minutes:02d}:{seconds:02d}", duration=3000, timer=True)
            return
        super().show_timer_bubble(minutes, seconds, phase)

    def collapse_island(self, dismissed: bool = False):
        if self._mode != "island":
            return
        self._cancel_island_timer()
        if dismissed and self._bubble_is_timer:
            self._timer_mute_until = time.time() + 300               # a clicked-away countdown stays away for 5 min

        def done():
            self._isl = None
            self._invalidate()
        self._morph("handle", 0.36, done=done)

    # ------------------------------------------------------------------ physics: never panic near / at the top edge
    def update_physics(self):
        now = time.time()
        dt = max(now - self.last_move_t, 0.001)
        cur_x, cur_y = self._win_xy()
        l, t, r, b = self._monitor_rect()
        top_zone = self.docked() or self._anim_on or (self.is_held and cur_y <= t + self.DOCK_ZONE_Y)

        if self.errand or self.docked() or self._anim_on:            # scripted / docked movement is not "shaking"
            self.vel_x = self.vel_y = self.shake_energy = 0.0
            self.last_pos_x, self.last_pos_y, self.last_move_t = cur_x, cur_y, now
            self.is_scared, self.edge_hold_start = False, 0.0
            self._sleep_wake(now)
            return
        vx, vy = (cur_x - self.last_pos_x) / dt, (cur_y - self.last_pos_y) / dt
        self.vel_x += (vx - self.vel_x) * 0.25
        self.vel_y += (vy - self.vel_y) * 0.25
        if math.hypot(vx - self.vel_x, vy - self.vel_y) > 2500:
            self.shake_energy += 1.0
            if self.shake_energy > 4.0 and not self.is_dizzy and not self.mish.strike_mode:
                self.trigger_dizzy_state()
        else:
            self.shake_energy = max(0.0, self.shake_energy - 0.1)
        self.last_pos_x, self.last_pos_y, self.last_move_t = cur_x, cur_y, now
        self._sleep_wake(now)

        off_edge = cur_x < l - 20 or cur_x > r - 180 or cur_y < t - 20 or cur_y > b - 90
        if top_zone:
            off_edge = False                                         # docking at the top is not falling off the screen
        if off_edge and not self.mish.strike_mode:
            self.is_scared = True
            if self.edge_hold_start == 0.0:
                self.edge_hold_start = now
                self.mish.react_async("scared", force=True)
            elif now - self.edge_hold_start > 6.0:
                self.trigger_strike_mode()
        else:
            self.is_scared = False
            self.edge_hold_start = 0.0
        if self.mish.strike_mode and now > self.strike_until:
            self.mish.strike_mode = False
            self.mish.react_async("strike_end", force=True)

    def _sleep_wake(self, now: float):
        idle_t = self.idle_t = idle_seconds()
        if idle_t >= AUTO_SLEEP_TIMEOUT and not self.is_sleeping and not self.mish.strike_mode:
            self.is_sleeping = True
            self._wma_proxy("-alpha", 0.3)
        elif idle_t < AUTO_SLEEP_TIMEOUT and self.is_sleeping:
            self.is_sleeping = False
            self._wma_proxy("-alpha", self.settings["opacity"])
            self.eyes.set_expression("surprised", 1.2)
            self.mish.react_async("wake")

    # ------------------------------------------------------------------ errands / focus punish need the floating body
    def start_errand(self, app: str):
        if self.docked():
            self.pop_out(immediate=True)
        super().start_errand(app)

    def snap_to_center_punish(self, window_name: str):
        if self.docked():
            self.pop_out(immediate=True)
        super().snap_to_center_punish(window_name)

    # ------------------------------------------------------------------ mouse
    def _in_gear(self, x, y) -> bool:
        x0, y0, x1, y1 = self.GEAR_RECT
        return self._mode == "float" and x0 <= x <= x1 and y0 <= y <= y1

    def _ev_press(self, e):
        if e.widget is self.root:
            self.on_press(e)

    def _ev_drag(self, e):
        if e.widget is self.root:
            self.on_drag(e)

    def _ev_release(self, e):
        if e.widget is self.root:
            self.on_release(e)

    def _ev_motion(self, e):
        if e.widget is self.root:
            try:
                self.root.configure(cursor="hand2" if (self._in_gear(e.x, e.y) or self.docked()) else "")
            except tk.TclError:
                pass

    def _ev_right(self, e):
        if e.widget is not self.root or self.errand:
            return
        menu = self.dock_menu if self.docked() else self.menu
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    def on_press(self, event):
        if self.errand or self._anim_on:
            self._press = None
            return
        wx, wy = self._win_xy()
        self._press = dict(x=event.x_root, y=event.y_root, wx=wx, wy=wy, gear=self._in_gear(event.x, event.y))
        self.press_root = (event.x_root, event.y_root)
        self.moved = False
        self._pulled = False

    def on_drag(self, event):
        p = self._press
        if self.errand or not p or self._anim_on:
            return
        if not self.moved:
            if math.hypot(event.x_root - p["x"], event.y_root - p["y"]) < 5:
                return
            self.moved = True
            if self._mode == "float":
                self.is_held = True
        if self._mode == "float":
            nx, ny = p["wx"] + event.x_root - p["x"], p["wy"] + event.y_root - p["y"]
            self.root.geometry(f"+{nx}+{ny}")
            self._near_top = ny <= self._monitor_rect()[1] + self.DOCK_ZONE_Y
            if self.active_bubble and self.active_bubble.winfo_exists():
                self.active_bubble.update_position()
        elif event.y_root - p["y"] >= self.PULL_DY and not self._pulled:
            self._pulled = True                                       # pulled down: pop back out
            self.pop_out()

    def on_release(self, event):
        p, self._press = self._press, None
        if self.errand or not p or self._anim_on:
            return
        if self._mode == "float":
            self._near_top = False
            if self.is_held:
                self.is_held = False
                if self._win_xy()[1] <= self._monitor_rect()[1] + self.DOCK_Y:
                    self.tuck_to_top()                               # dragged to the top edge: dock
                else:
                    self.eyes.set_expression("happy", 1.4)
            elif not self.moved:
                if p["gear"]:
                    self.show_menu()
                else:
                    self.poke()
        elif not self.moved and not self._pulled:
            if self._mode == "handle":
                self.pop_out()                                        # click the handle: pop out
            else:
                self.collapse_island(dismissed=True)                  # click the island: collapse


def create_icon_image():
    img = Image.new("RGB", (64, 64), (8, 8, 10))
    d = ImageDraw.Draw(img)
    d.rectangle((10, 10, 54, 54), outline=(0, 230, 255), width=4)
    d.rectangle((22, 22, 42, 42), fill=(0, 230, 255))
    return img


def ensure_single_instance() -> bool:
    """A second copy would load a second model into RAM, so it quits instead."""
    try:
        ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\MishCompanionSingleInstance")
        if ctypes.windll.kernel32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
            log("another Mish is already running - exiting this copy")
            return False
    except Exception:
        pass
    return True


def _hotkey_loop(ui_queue: queue.Queue, stop_event: threading.Event):
    """Listen for global Alt+M via Win32 RegisterHotKey on a dedicated message thread."""
    WM_HOTKEY, HK_ID = 0x0312, 1
    msg = _wt.MSG()
    if not ctypes.windll.user32.RegisterHotKey(None, HK_ID, HOTKEY_MOD, HOTKEY_VK):
        log("hotkey: RegisterHotKey failed (Alt+M may already be bound by another app)")
        return
    log("hotkey: registered Alt+M")
    try:
        while not stop_event.is_set():
            while ctypes.windll.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                if msg.message == WM_HOTKEY and msg.wParam == HK_ID:
                    ui_queue.put(("summon",))
            stop_event.wait(0.08)
    finally:
        ctypes.windll.user32.UnregisterHotKey(None, HK_ID)


def ensure_portable_setup(base_dir: Path):
    """Create portable data/models directories and a quickstart note for new users."""
    models_dir = base_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    readme = models_dir / "README_MODELS.txt"
    if not readme.exists():
        readme.write_text(
            "Mish Local Models Folder\n"
            "========================\n"
            "Drop your quantized .gguf model files here (e.g. SmolLM2-1.7B-Instruct-Q4_K_M.gguf).\n"
            "Mish will automatically detect and load them on startup.\n"
            "Press Alt+M anytime to summon Mish!\n",
            encoding="utf-8",
        )


def main():
    if not ensure_single_instance():
        return
    for stream in ("stdout", "stderr"):                       # the windowed exe has none; llama.cpp wants one
        if getattr(sys, stream) is None:
            setattr(sys, stream, open(os.devnull, "w"))
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    ensure_portable_setup(user_data_dir())
    ui_queue: queue.Queue = queue.Queue()
    mish = Mish(ui_queue)
    root = tk.Tk()
    LayeredMishWindow(root, mish, ui_queue)

    def quit_app(icon=None, _item=None):
        mish.shutdown()
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
        ui_queue.put(("quit",))

    root.protocol("WM_DELETE_WINDOW", quit_app)

    icon = pystray.Icon(
        "Mish", create_icon_image(), "Mish",
        menu=pystray.Menu(
            pystray.MenuItem("Open Mish", lambda i, it: ui_queue.put(("show",)), default=True),
            pystray.MenuItem("Remind me now", lambda i, it: mish.react_async("reminder")),
            pystray.MenuItem(f"Start focus ({FOCUS_DEFAULT_MIN}m)", lambda i, it: mish.say(mish.start_focus(FOCUS_DEFAULT_MIN))),
            pystray.MenuItem("Stop focus", lambda i, it: mish.say(mish.stop_focus())),
            pystray.MenuItem("Focus status", lambda i, it: mish.say(f"Focus: {'ON' if mish.in_focus() else 'OFF'}")),
            pystray.MenuItem("Look screen", lambda i, it: mish.observe_async(force=True)),
            pystray.MenuItem("Exit", quit_app),
        ),
    )

    for target in (mish.watcher_loop, mish.scheduler_loop, mish.timer_loop, mish.observe_loop, mish.pester_loop,
                   mish.hardware_loop, mish.startup_briefing):
        threading.Thread(target=target, daemon=True).start()
    threading.Thread(target=_hotkey_loop, args=(ui_queue, mish.stop_event), daemon=True).start()
    threading.Thread(target=icon.run, daemon=True).start()

    try:
        root.mainloop()
    finally:
        quit_app(icon)
