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
import random
import winreg
from pathlib import Path
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageTk

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
    EXPRESSIONS,
    EYE_PRESETS,
    GLYPHS,
    EyeEngine,
    STATE_RGB,
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


def _monitor_of(widget):
    """(left, top, right, bottom) of the monitor a Tk window is on."""
    try:
        top = _U32.GetAncestor(widget.winfo_id(), 2) or widget.winfo_id()
        mi = _MONINFO()
        mi.cbSize = ctypes.sizeof(_MONINFO)
        if _U32.GetMonitorInfoW(_U32.MonitorFromWindow(top, 2), ctypes.byref(mi)):
            r = mi.rcMonitor
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return 0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight()

THEMES = {
    "dark": dict(
        name="dark",
        bg="#070a11", card="#0d121d", line="#1e293b", text="#e2e8f0",
        text_hi="#f8fafc", muted="#64748b", hover="#131a2a",
        shell_rgb=(5, 7, 11), shell_outline=(255, 255, 255, 46),
        bubble_rgb=(6, 9, 16), bubble_text=(248, 250, 252, 255),
        shadow_rgb=(0, 0, 0, 255), is_light=False,
    ),
    "light": dict(
        name="light",
        bg="#f8fafc", card="#ffffff", line="#cbd5e1", text="#0f172a",
        text_hi="#020617", muted="#64748b", hover="#e2e8f0",
        shell_rgb=(248, 250, 252), shell_outline=(15, 23, 42, 46),
        bubble_rgb=(248, 250, 252), bubble_text=(15, 23, 42, 255),
        shadow_rgb=(255, 255, 255, 255), is_light=True,
    ),
}


def system_is_light() -> bool:
    """Read Windows Personalize\\AppsUseLightTheme (1 = Light, 0 = Dark)."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as k:
            val, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
            return bool(val)
    except Exception:
        return False


def get_theme(mode: str = "auto") -> dict:
    if mode == "light" or (mode == "auto" and system_is_light()):
        return THEMES["light"]
    return THEMES["dark"]


def adaptive_accent(hex_col: str, is_light: bool) -> str:
    """Darken bright pastel/cyan glow colors on light backgrounds so text & icons stay readable."""
    if not is_light:
        return hex_col
    r, g, b = _hex_rgb(hex_col)
    if (0.299 * r + 0.587 * g + 0.114 * b) > 155:
        return _rgb_hex((r * 0.58, g * 0.58, b * 0.58))
    return hex_col

class DynamicSpeechBubble(tk.Toplevel):
    """Frameless ghost strip: feathered dark gradient (true per-pixel alpha via UpdateLayeredWindow), no box."""
    WRAP, FONT_PX, LINE_H, MAX_LINES = 250, 13, 18, 6
    FEATHER, SLIDE, ALPHA = 14, 10, 1.0          # FEATHER = shadow margin around the 14px card
    IN_S, OUT_S = 0.20, 0.18

    def __init__(self, parent, text, duration=6000, accent="#00e6ff", bg="#111115", theme: dict | None = None):
        super().__init__(parent)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg="#000000")
        self._parent, self._closing, self._phase = parent, False, "in"
        self._k, self._k0 = 0.0, 1.0
        self._x = self._y = 0
        self._above = True
        self._th = theme or get_theme("auto")
        try:
            self._accent = _hex_rgb(adaptive_accent(accent, self._th["is_light"]))
        except Exception:
            self._accent = (15, 23, 42) if self._th["is_light"] else (255, 255, 255)
        self._sf = _LayeredSurface()
        self._img = self._render(text)
        w, h = self._img.size
        self._place_near()
        self.geometry(f"{w}x{h}+{self._x}+{self._y}")
        self.update_idletasks()
        self._sf.attach(self)
        self._sf.upload(self._img)
        self._present()
        self.deiconify()
        self.update_idletasks()
        self._t0 = time.perf_counter()
        self.bind("<Button-1>", lambda e: self._close())
        self._step()
        self.after(duration, self._close)

    def _render(self, text):
        """Theme-adaptive 14px card (soft drop shadow + 1px hairline) drawn with true per-pixel alpha."""
        S, F, px, th = _SS, self.FEATHER, self.FONT_PX, self._th
        lines = _wrap(text, px, self.WRAP, self.MAX_LINES) or [""]
        head, hpx = "MISH", 8.5
        head_w = sum(_measure(ch, hpx) + 1.4 for ch in head)
        inner_w = int(math.ceil(max(head_w, max(_measure(l, px) for l in lines))))
        inner_h = 16 + len(lines) * self.LINE_H
        padx, pady = 15, 11
        w, h = inner_w + 2 * (F + padx), inner_h + 2 * (F + pady)
        box = (F * S, F * S, (w - F) * S - 1, (h - F) * S - 1)
        size = (w * S, h * S)

        sh = Image.new("L", size, 0)                              # soft drop shadow, 3px down
        ImageDraw.Draw(sh).rounded_rectangle((box[0], box[1] + 3 * S, box[2], box[3] + 3 * S), radius=14 * S, fill=255)
        sh = sh.filter(ImageFilter.GaussianBlur(5 * S)).point(lambda v: int(v * (0.20 if th["is_light"] else 0.45)))
        shadow = Image.new("RGBA", size, (0, 0, 0, 255))
        shadow.putalpha(sh)
        card = Image.new("RGBA", size, (0, 0, 0, 0))
        ImageDraw.Draw(card).rounded_rectangle(box, radius=14 * S, fill=_hex_rgb(th["bg"]) + (244,),
                                               outline=_hex_rgb(th["line"]) + (255,), width=S)
        out = Image.alpha_composite(shadow, card)
        d = ImageDraw.Draw(out)
        x, y = (F + padx), (F + pady)
        for ch in head:
            d.text((x * S, y * S), ch, font=_font(hpx), fill=self._accent + (235,))
            x += _measure(ch, hpx) + 1.4
        body_col = _hex_rgb(th["text_hi"]) + (255,)
        for i, line in enumerate(lines):
            x, yy = F + padx, y + 16 + i * self.LINE_H
            for emoji, run in _runs(line):
                f = _font(px, emoji) or _font(px)
                try:
                    if emoji:
                        d.text((x * S, yy * S), run, font=f, embedded_color=True)
                    else:
                        d.text((x * S, yy * S), run, font=f, fill=body_col)
                except Exception:
                    pass
                x += f.getlength(run) / S
        return out.resize((w, h), Image.Resampling.LANCZOS)

    def _place_near(self):
        p, F = self._parent, self.FEATHER
        w, h = self._img.size
        l, t, r, b = _monitor_of(p)
        px, py, pw, ph = p.winfo_x(), p.winfo_y(), p.winfo_width(), p.winfo_height()
        cx = px + pw // 2
        top, bottom = (py + 14, py + 98) if ph >= 100 else (py, py + ph)
        self._above = top - h + F - 2 >= t + 4
        self._y = top - h + F - 2 if self._above else bottom - F + 2
        x = cx - 60 - F if cx < (l + r) // 2 else cx + 60 + F - w
        self._x = int(clamp(x, l + 4, max(l + 4, r - w - 4)))
        self._y = int(clamp(self._y, t + 2, max(t + 2, b - h - 2)))

    def update_position(self):
        """Stay attached to the parent while it is dragged, without touching the fade timer."""
        try:
            if not self.winfo_exists():
                return
            self._place_near()
            self._present()
        except tk.TclError:
            pass

    def _present(self):
        off = (1.0 - self._k) * self.SLIDE * (1 if self._above else -1)
        self._sf.present(self._x, self._y + off, self.ALPHA * self._k)

    def _step(self):
        try:
            if not self.winfo_exists() or self._phase == "hold":
                return
            d = self.IN_S if self._phase == "in" else self.OUT_S
            e = smoothstep((time.perf_counter() - self._t0) / d)
            self._k = e if self._phase == "in" else self._k0 * (1.0 - e)
            self._present()
            if e < 1.0:
                self.after(14, self._step)
            elif self._phase == "out":
                self.destroy()
            else:
                self._phase = "hold"
        except tk.TclError:
            pass

    def _close(self):
        try:
            if self._closing or not self.winfo_exists():
                return
        except tk.TclError:
            return
        self._closing, self._phase, self._k0 = True, "out", self._k
        self._t0 = time.perf_counter()
        self._step()

    def destroy(self):
        try:
            self._sf.free()
        except Exception:
            pass
        super().destroy()


class GhostMenu(tk.Toplevel):
    """Theme-aware right-click menu: 14px rounded shell, 1px hairline, hover rows, right-aligned hints.
    Closes on Esc / focus loss. items = (label, hint, callback, danger) tuples; None = separator."""

    def __init__(self, parent, items, x, y, accent="#ffffff", bounds=None, theme: dict | None = None):
        super().__init__(parent)
        th = theme or get_theme("auto")
        c = ui_colors(th, accent)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.97)
        shell = RoundedShell(self, th, radius=14, inset=3)
        shell.pack(fill="both", expand=True)
        inner = tk.Frame(shell.body, bg=c["bg"])
        inner.pack(fill="both", expand=True, padx=6, pady=6)
        for it in items:
            if it is None:
                tk.Frame(inner, bg=c["line"], height=1).pack(fill="x", padx=10, pady=4)
                continue
            label, hint, cb, danger = it
            row = tk.Frame(inner, bg=c["bg"], cursor="hand2")
            row.pack(fill="x", pady=1)
            name = tk.Label(row, text=label, bg=c["bg"], fg=c["rose"] if danger else c["text"],
                            font=("Segoe UI", 10), anchor="w", padx=12, pady=5)
            name.pack(side="left", fill="x", expand=True)
            hl = tk.Label(row, text=hint, bg=c["bg"], fg=c["muted"], font=("Consolas", 8), padx=10)
            hl.pack(side="right")
            ws = (row, name, hl)

            def refresh(ws=ws):
                try:
                    under = self.winfo_containing(*self.winfo_pointerxy())
                except Exception:
                    under = None
                col = c["hover"] if under in ws else c["bg"]
                for w in ws:
                    w.configure(bg=col)
            for w in ws:
                w.bind("<Enter>", lambda e, r=refresh: self.after(1, r))
                w.bind("<Leave>", lambda e, r=refresh: self.after(1, r))
                w.bind("<Button-1>", lambda e, cbk=cb: self._pick(cbk))
        self.update_idletasks()
        pad = 2 * shell._inset + 12                # was a hard-coded 18 (inset 3)
        w = max(inner.winfo_reqwidth() + pad, 232)
        h = inner.winfo_reqheight() + pad
        l, t, r, b = bounds or (0, 0, self.winfo_screenwidth(), self.winfo_screenheight())
        x = int(clamp(x, l + 4, max(l + 4, r - w - 4)))
        y = int(clamp(y, t + 4, max(t + 4, b - h - 4)))
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<FocusOut>", lambda e: self.destroy() if e.widget is self else None)
        self.deiconify()
        self.update_idletasks()
        self.focus_force()

    def _pick(self, cb):
        parent = self.master
        self.destroy()
        if cb:
            parent.after(30, cb)


def _make_draggable(win, *handles):
    """Drag a frameless window by any of the given handle widgets."""
    st = {}

    def press(e):
        st["dx"], st["dy"] = e.x_root - win.winfo_x(), e.y_root - win.winfo_y()

    def move(e):
        if st:
            win.geometry(f"+{e.x_root - st['dx']}+{e.y_root - st['dy']}")
    for h in handles:
        h.bind("<ButtonPress-1>", press, add="+")
        h.bind("<B1-Motion>", move, add="+")
        h.configure(cursor="fleur")


def _hover_group(win, widgets, on, off):
    """Run on()/off() as the pointer enters/leaves any widget of a group (no flicker between children)."""
    def check():
        try:
            under = win.winfo_containing(*win.winfo_pointerxy())
        except Exception:
            under = None
        (on if under in widgets else off)()
    for w in widgets:
        w.bind("<Enter>", lambda e: win.after(1, check), add="+")
        w.bind("<Leave>", lambda e: win.after(1, check), add="+")


# ===================================================================== PIL widgets (BEGIN)
# Shared, theme-aware controls for every HUD panel. All art is drawn with Pillow at _SS x supersampling and
# downsampled with LANCZOS. Each widget owns exactly ONE PhotoImage (updated in place with .paste() while its
# size is unchanged) and releases it in destroy(), so hover / drag / animation never allocate new images.

def _lum(rgb) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def _mixhex(a: str, b: str, t: float) -> str:
    return _rgb_hex(_mix(_hex_rgb(a), _hex_rgb(b), t))


def _parent_bg(parent, fallback: str) -> str:
    try:
        v = str(parent.cget("bg"))
        return v if v.startswith("#") and len(v) == 7 else fallback
    except Exception:
        return fallback


def ui_colors(theme: dict, accent: str) -> dict:
    """The palette every PIL widget draws from. `theme` is face.theme, `accent` is face.eye_col."""
    light = bool(theme["is_light"])
    acc = adaptive_accent(accent, light)
    return dict(
        bg=theme["bg"], card=theme["card"], line=theme["line"], text=theme["text"], text_hi=theme["text_hi"],
        muted=theme["muted"], hover=theme["hover"], accent=acc, light=light,
        rose="#e11d48" if light else "#fb7185",
        on_accent="#ffffff" if _lum(_hex_rgb(acc)) < 150 else "#05070b",
    )


def _aa(w: int, h: int):
    S = _SS
    img = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img), S


def _down(img, w: int, h: int):
    return img.resize((w, h), Image.Resampling.LANCZOS)

def _bg_rgb(widget):
    """Widget background as an (r, g, b) tuple, whatever form Tk reports it in."""
    try:
        v = str(widget.cget("bg"))
        if len(v) == 7 and v.startswith("#"):
            return _hex_rgb(v)
        r, g, b = widget.winfo_rgb(v)
        return (r >> 8, g >> 8, b >> 8)
    except Exception:
        return (0, 0, 0)


def _flatten(img, rgb, snap: int = 0):
    """Composite an RGBA image onto a solid colour -> opaque RGB. Edges blend with the real background, so there is
    no fringe. snap > 0 forces pixels with alpha below it to exactly `rgb` (used for -transparentcolor keys)."""
    a = img.getchannel("A")
    if snap:
        a = a.point(lambda v: 0 if v < snap else v)
    base = Image.new("RGB", img.size, rgb)
    base.paste(img.convert("RGB"), mask=a)
    return base


def render_card(w: int, h: int, radius: int, fill: str, line: str):
    """Rounded rectangle with a crisp 1px hairline. The outline is drawn inside the box, so after an exact 4:1 BOX
    downsample it covers exactly one pixel row/column (LANCZOS would ring and soften it)."""
    img, d, S = _aa(w, h)
    d.rounded_rectangle((0, 0, w * S - 1, h * S - 1), radius=radius * S, fill=fill, outline=line, width=S)
    return img.resize((w, h), Image.Resampling.BOX)

def render_shell(w: int, h: int, radius: int, fill: str, line: str, key_rgb):
    """Shell art for a -transparentcolor window: opaque RGB over the key colour. Pixels that are mostly outside the
    curve (alpha < 110) become exactly the key colour, so Windows cuts them out and no off-key halo is left."""
    return _flatten(render_card(w, h, radius, fill, line), key_rgb, snap=110)

def _soft_shadow(size, box, blur, alpha, clip=None):
    """Black blurred ellipse -> RGBA layer (optionally clipped by an L mask)."""
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).ellipse(box, fill=255)
    m = m.filter(ImageFilter.GaussianBlur(blur)).point(lambda v: int(v * alpha))
    if clip is not None:
        m = ImageChops.multiply(m, clip)
    lay = Image.new("RGBA", size, (0, 0, 0, 255))
    lay.putalpha(m)
    return lay

def render_pill_button(w: int, h: int, text: str, c: dict, kind: str = "secondary", state: str = "normal",
                       px: float = 9):
    img, d, S = _aa(w, h)
    if kind == "primary":
        base, fg, line = c["accent"], c["on_accent"], None
        tone = "#000000" if _lum(_hex_rgb(base)) > 140 else "#ffffff"
    elif kind == "danger":
        base, fg, line, tone = c["card"], c["rose"], c["line"], c["rose"]
    else:
        base, fg, line, tone = c["hover"], c["text"], c["line"], c["text"]
    if state == "hover":
        base = _mixhex(base, tone, 0.10)
    elif state == "press":
        base = _mixhex(base, tone, 0.20)
    elif state == "disabled":
        base, fg, line = c["card"], c["muted"], c["line"]
    d.rounded_rectangle((0, 0, w * S - 1, h * S - 1), radius=h * S / 2.0, fill=base, outline=line, width=S)
    f = _font(px)
    cx, cy = w * S / 2.0, h * S / 2.0 + (S if state == "press" else 0)
    try:
        d.text((cx, cy), text, font=f, fill=fg, anchor="mm")
    except Exception:
        d.text((cx - f.getlength(text) / 2.0, cy - px * S * 0.6), text, font=f, fill=fg)
    return _down(img, w, h)


def render_toggle(w: int, h: int, t: float, c: dict, hover: bool = False):
    """Switch track + knob. t = 0 (off) .. 1 (on); the knob colour blends white -> on_accent so it never
    disappears against a white accent."""
    img, d, S = _aa(w, h)
    off = _mixhex(c["line"], c["muted"], 0.35)
    track = _mixhex(off, c["accent"], t)
    if hover:
        track = _mixhex(track, "#000000" if c["light"] else "#ffffff", 0.08)
    box = (0, 0, w * S - 1, h * S - 1)
    d.rounded_rectangle(box, radius=h * S / 2.0, fill=track)
    clip = Image.new("L", img.size, 0)
    ImageDraw.Draw(clip).rounded_rectangle(box, radius=h * S / 2.0, fill=255)
    r = (h - 6) / 2.0
    x, cy = 3 + r + t * (w - 6 - 2 * r), h / 2.0
    img = Image.alpha_composite(img, _soft_shadow(img.size, ((x - r) * S, (cy - r + 1) * S, (x + r) * S,
                                                             (cy + r + 1) * S), 1.2 * S, 0.35, clip))
    ImageDraw.Draw(img).ellipse(((x - r) * S, (cy - r) * S, (x + r) * S, (cy + r) * S),
                                fill=_mixhex("#ffffff", c["on_accent"], t))
    return _down(img, w, h)


def render_slider(w: int, h: int, frac: float, c: dict, hover: bool = False, press: bool = False,
                  label: str = "", label_w: int = 0, pad: int = 10):
    img, d, S = _aa(w, h)
    tx0, tx1, cy, th = pad, w - label_w - pad, h / 2.0, 4
    frac = clamp(frac, 0.0, 1.0)
    kx = tx0 + frac * (tx1 - tx0)
    d.rounded_rectangle((tx0 * S, (cy - th / 2) * S, tx1 * S, (cy + th / 2) * S), radius=th * S / 2.0,
                        fill=_mixhex(c["line"], c["muted"], 0.25))
    if kx - tx0 > 0.5:
        d.rounded_rectangle((tx0 * S, (cy - th / 2) * S, kx * S, (cy + th / 2) * S), radius=th * S / 2.0,
                            fill=c["accent"])
    r = 7.0 if (hover or press) else 6.0
    if press:                                                       # soft accent halo while dragging
        g = Image.new("L", img.size, 0)
        ImageDraw.Draw(g).ellipse(((kx - r - 4) * S, (cy - r - 4) * S, (kx + r + 4) * S, (cy + r + 4) * S), fill=255)
        g = g.filter(ImageFilter.GaussianBlur(2 * S)).point(lambda v: int(v * 0.30))
        halo = Image.new("RGBA", img.size, _hex_rgb(c["accent"]) + (255,))
        halo.putalpha(g)
        img = Image.alpha_composite(img, halo)
    img = Image.alpha_composite(img, _soft_shadow(img.size, ((kx - r) * S, (cy - r + 1) * S, (kx + r) * S,
                                                             (cy + r + 1) * S), 1.2 * S, 0.40))
    dd = ImageDraw.Draw(img)
    dd.ellipse(((kx - r) * S, (cy - r) * S, (kx + r) * S, (cy + r) * S), fill="#ffffff",
               outline=c["accent"], width=int(1.5 * S))
    if label:
        f = _font(8.5)
        try:
            dd.text(((w - 2) * S, cy * S), label, font=f, fill=c["muted"], anchor="rm")
        except Exception:
            dd.text(((w - 2) * S - f.getlength(label), (cy - 6) * S), label, font=f, fill=c["muted"])
    return _down(img, w, h)


class _PilCanvas(tk.Canvas):
    """Canvas that shows one PIL image through a single reusable PhotoImage and cleans up after itself.
    The image is flattened onto the canvas background (opaque RGB), so corners are smooth on any theme."""

    def __init__(self, parent, w: int, h: int, bg: str, **kw):
        super().__init__(parent, width=w, height=h, bg=bg, highlightthickness=0, bd=0, **kw)
        self.w, self.h = int(w), int(h)
        self._photo = None
        self._item = None
        self._after = None
        self._alive = True

    def configure(self, cnf=None, **kw):
        out = super().configure(cnf, **kw)
        if (kw.keys() & {"bg", "background"}) and getattr(self, "_alive", False) \
                and getattr(self, "_photo", None) is not None:
            self._draw()                                             # the art is baked onto the old colour
        return out
    config = configure

    def _show(self, img):
        try:
            flat = _flatten(img, _bg_rgb(self))
            if self._photo is None or (self._photo.width(), self._photo.height()) != flat.size:
                self._photo = ImageTk.PhotoImage(flat, master=self)
                if self._item is None:
                    self._item = self.create_image(0, 0, image=self._photo, anchor="nw")
                else:
                    self.itemconfigure(self._item, image=self._photo)
            else:
                self._photo.paste(flat)                              # in place: no new image per frame
        except tk.TclError:
            pass

    def destroy(self):
        self._alive = False
        if self._after is not None:
            try:
                self.after_cancel(self._after)
            except Exception:
                pass
            self._after = None
        self._photo = self._item = None
        try:
            super().destroy()
        except tk.TclError:
            pass

class PillButton(_PilCanvas):
    """Anti-aliased pill button. kind: 'secondary' | 'primary' | 'danger'. Hover and press states."""

    def __init__(self, parent, text: str, command=None, theme: dict | None = None, accent: str = "#ffffff",
                 kind: str = "secondary", height: int = 26, padx: int = 14, width: int | None = None,
                 px: float = 9, bg: str | None = None):
        self._theme = theme or get_theme("auto")
        self._accent, self._kind, self._px, self._padx = accent, kind, px, padx
        self._text, self._command = text, command
        self._enabled, self._hover, self._press, self._fixed_w = True, False, False, width
        self._c = ui_colors(self._theme, accent)
        w = width or int(math.ceil(_measure(text, px))) + 2 * padx
        super().__init__(parent, w, height, bg or _parent_bg(parent, self._c["bg"]), cursor="hand2")
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False, press=False))
        self.bind("<ButtonPress-1>", lambda e: self._set(press=True))
        self.bind("<ButtonRelease-1>", self._release)
        self._draw()

    def _state(self) -> str:
        if not self._enabled:
            return "disabled"
        return "press" if self._press else "hover" if self._hover else "normal"

    def _draw(self):
        if self._alive:
            self._show(render_pill_button(self.w, self.h, self._text, self._c, self._kind, self._state(), self._px))

    def _set(self, **kw):
        if "hover" in kw:
            self._hover = kw["hover"]
        if "press" in kw:
            self._press = kw["press"] and self._enabled
        self._draw()

    def _release(self, e):
        was = self._press
        self._set(press=False)
        if was and self._enabled and 0 <= e.x < self.w and 0 <= e.y < self.h and self._command:
            self._command()

    def invoke(self):
        if self._enabled and self._command:
            self._command()

    def set_text(self, text: str):
        self._text = text
        if not self._fixed_w:
            self.w = int(math.ceil(_measure(text, self._px))) + 2 * self._padx
            self.configure(width=self.w)
        self._draw()

    def set_enabled(self, on: bool):
        self._enabled = bool(on)
        self.configure(cursor="hand2" if on else "arrow")
        self._draw()

    def set_kind(self, kind: str):
        self._kind = kind
        self._draw()

    def retheme(self, theme: dict, accent: str | None = None, bg: str | None = None):
        self._theme, self._accent = theme, accent or self._accent
        self._c = ui_colors(theme, self._accent)
        self.configure(bg=bg or _parent_bg(self.master, self._c["bg"]))
        self._draw()


class ToggleSwitch(_PilCanvas):
    """40x22 switch with an eased 160 ms knob slide. command(value) fires immediately on click."""
    W, H = 40, 22

    def __init__(self, parent, value: bool = False, command=None, theme: dict | None = None,
                 accent: str = "#ffffff", bg: str | None = None):
        self._theme = theme or get_theme("auto")
        self._accent, self._command, self._value = accent, command, bool(value)
        self._c = ui_colors(self._theme, accent)
        self._t = 1.0 if self._value else 0.0
        self._from = self._goal = self._t
        self._t0, self._hover = 0.0, False
        super().__init__(parent, self.W, self.H, bg or _parent_bg(parent, self._c["bg"]), cursor="hand2")
        self.bind("<Enter>", lambda e: self._hov(True))
        self.bind("<Leave>", lambda e: self._hov(False))
        self.bind("<Button-1>", lambda e: self.toggle())
        self._draw()

    def _hov(self, on: bool):
        self._hover = on
        self._draw()

    def _draw(self):
        if self._alive:
            self._show(render_toggle(self.W, self.H, self._t, self._c, self._hover))

    def get(self) -> bool:
        return self._value

    def set(self, value: bool, animate: bool = False):
        self._value = bool(value)
        goal = 1.0 if self._value else 0.0
        if animate:
            self._from, self._goal, self._t0 = self._t, goal, time.perf_counter()
            if self._after is None:
                self._step()
        else:
            self._t = self._from = self._goal = goal
            self._draw()

    def toggle(self):
        self.set(not self._value, animate=True)
        if self._command:
            self._command(self._value)

    def _step(self):
        self._after = None
        if not self._alive:
            return
        u = min(1.0, (time.perf_counter() - self._t0) / 0.16)
        self._t = self._from + (self._goal - self._from) * smoothstep(u)
        self._draw()
        if u < 1.0:
            try:
                self._after = self.after(14, self._step)
            except tk.TclError:
                self._after = None

    def retheme(self, theme: dict, accent: str | None = None, bg: str | None = None):
        self._theme, self._accent = theme, accent or self._accent
        self._c = ui_colors(theme, self._accent)
        self.configure(bg=bg or _parent_bg(self.master, self._c["bg"]))
        self._draw()


class RoundSlider(_PilCanvas):
    """Rounded slider. command(value) while dragging, on_release(value) once on mouse-up (save there)."""
    PAD, LABEL_W = 10, 40

    def __init__(self, parent, from_: float, to: float, value: float, command=None, on_release=None,
                 width: int = 280, height: int = 24, step: float | None = None, fmt=None,
                 theme: dict | None = None, accent: str = "#ffffff", bg: str | None = None):
        self._theme = theme or get_theme("auto")
        self._accent, self._command, self._on_release = accent, command, on_release
        self._lo, self._hi, self._step_v, self._fmt = float(from_), float(to), step, fmt
        self._val = clamp(float(value), self._lo, self._hi)
        self._c = ui_colors(self._theme, accent)
        self._hover = self._press = False
        super().__init__(parent, width, height, bg or _parent_bg(parent, self._c["bg"]), cursor="hand2")
        self.bind("<Enter>", lambda e: self._flag(hover=True))
        self.bind("<Leave>", lambda e: self._flag(hover=False))
        self.bind("<ButtonPress-1>", self._down_ev)
        self.bind("<B1-Motion>", self._move)
        self.bind("<ButtonRelease-1>", self._up)
        self._draw()

    @property
    def value(self) -> float:
        return self._val

    def _label_w(self) -> int:
        return self.LABEL_W if self._fmt else 0

    def _draw(self):
        if not self._alive:
            return
        span = (self._hi - self._lo) or 1.0
        self._show(render_slider(self.w, self.h, (self._val - self._lo) / span, self._c, self._hover, self._press,
                                 self._fmt(self._val) if self._fmt else "", self._label_w(), self.PAD))

    def _flag(self, **kw):
        self._hover = kw.get("hover", self._hover)
        self._press = kw.get("press", self._press)
        self._draw()

    def _from_x(self, x: float) -> float:
        tx0, tx1 = self.PAD, self.w - self._label_w() - self.PAD
        v = self._lo + clamp((x - tx0) / max(1.0, tx1 - tx0), 0.0, 1.0) * (self._hi - self._lo)
        return round(v / self._step_v) * self._step_v if self._step_v else v

    def _down_ev(self, e):
        self._press = True
        self._move(e)

    def _move(self, e):
        if not self._press:
            return
        v = clamp(self._from_x(e.x), self._lo, self._hi)
        if v != self._val:
            self._val = v
            if self._command:
                self._command(v)
        self._draw()

    def _up(self, _e):
        self._press = False
        self._draw()
        if self._on_release:
            self._on_release(self._val)

    def set(self, value: float):
        self._val = clamp(float(value), self._lo, self._hi)
        self._draw()

    def retheme(self, theme: dict, accent: str | None = None, bg: str | None = None):
        self._theme, self._accent = theme, accent or self._accent
        self._c = ui_colors(theme, self._accent)
        self.configure(bg=bg or _parent_bg(self.master, self._c["bg"]))
        self._draw()


class RoundedCard(tk.Canvas):
    """10px inner card: put widgets in `.body`. Height follows the content (see sync()); width follows the parent.
    The card art is flattened onto the canvas background, so corners blend cleanly in Dark and Light."""

    def __init__(self, parent, theme: dict | None = None, radius: int = 10, pad: int = 10, bg: str | None = None,
                 fill: str | None = None, line: str | None = None):
        self._theme = theme or get_theme("auto")
        self._fill, self._line = fill, line                           # optional overrides (input wells)
        self._r, self._size = radius, (0, 0)
        self._pad = max(int(pad), int(math.ceil(radius - (radius - 1) / math.sqrt(2))))   # body corners stay inside the curve
        self._photo = self._bg_item = None
        super().__init__(parent, bg=bg or _parent_bg(parent, self._theme["bg"]), highlightthickness=0, bd=0,
                         height=2 * self._pad + 8)
        self.body = tk.Frame(self, bg=self._fill or self._theme["card"])
        self._wid = self.create_window(self._pad, self._pad, window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._fit)
        self.bind("<Configure>", self._resize)

    def configure(self, cnf=None, **kw):
        out = super().configure(cnf, **kw)
        if (kw.keys() & {"bg", "background"}) and self._photo is not None:
            self._rerender()                                          # art was flattened onto the old bg
        return out
    config = configure

    def sync(self):
        """Set the canvas height from the content right now. _HudPanel calls this before the first paint, because
        <Configure> events are not delivered by update_idletasks()."""
        try:
            want = self.body.winfo_reqheight() + 2 * self._pad
            if int(self.cget("height")) != want:
                self.configure(height=want)
        except tk.TclError:
            pass

    def _fit(self, _e=None):
        self.sync()

    def _rerender(self):
        self._size = (0, 0)
        self._resize(type("E", (), {"width": self.winfo_width(), "height": self.winfo_height()})())

    def _resize(self, e):
        w, h = e.width, e.height
        if w < 8 or h < 8:
            return
        try:
            self.itemconfigure(self._wid, width=max(1, w - 2 * self._pad))
            if (w, h) != self._size:
                self._size = (w, h)
                art = render_card(w, h, self._r, self._fill or self._theme["card"], self._line or self._theme["line"])
                self._photo = ImageTk.PhotoImage(_flatten(art, _bg_rgb(self)), master=self)
                if self._bg_item is None:
                    self._bg_item = self.create_image(0, 0, image=self._photo, anchor="nw")
                else:
                    self.itemconfigure(self._bg_item, image=self._photo)
                self.tag_lower(self._bg_item)
        except tk.TclError:
            pass

    def retheme(self, theme: dict, bg: str | None = None):
        self._theme = theme
        self.configure(bg=bg or _parent_bg(self.master, theme["bg"]))
        self.body.configure(bg=self._fill or theme["card"])
        self._rerender()

    def set_line(self, color: str | None):
        """Recolour the hairline (e.g. accent while an input inside has focus). None = theme line."""
        self._line = color
        self._rerender()

    def destroy(self):
        self._photo = self._bg_item = None
        try:
            super().destroy()
        except tk.TclError:
            pass

class RoundedShell(tk.Canvas):
    """14px outer chrome for a frameless Toplevel: pack it, then build the panel inside `.body`.

    Corners are made see-through with -transparentcolor on a key colour one step away from the panel colour.
    Windows has no per-pixel alpha for Tk widgets, so the four corners are 1-bit; the edge is snapped to the key
    and the hairline hides most of the stepping. The body is inset far enough that its square corners stay inside
    the curve (min_inset), otherwise they poke through the outline."""

    @staticmethod
    def min_inset(radius: int) -> int:
        return int(math.ceil(radius - (radius - 1) / math.sqrt(2)))

    def __init__(self, win, theme: dict, radius: int = 14, inset: int = 3):
        self._win, self._theme, self._r = win, theme, radius
        self._inset = max(int(inset), self.min_inset(radius))          # radius 14 -> 5
        self._size, self._photo, self._item = (0, 0), None, None
        self._line = None
        key = self._key(theme)
        super().__init__(win, bg=key, highlightthickness=0, bd=0)
        self._apply_key(key)
        self.body = tk.Frame(self, bg=theme["bg"])
        self._wid = self.create_window(self._inset, self._inset, window=self.body, anchor="nw")
        self.bind("<Configure>", self._resize)

    @staticmethod
    def _key(theme: dict) -> str:
        r, g, b = _hex_rgb(theme["bg"])
        return _rgb_hex((r, g, b + 1 if b < 255 else b - 1))

    def _apply_key(self, key: str):
        try:
            self._win.configure(bg=key)
            self._win.wm_attributes("-transparentcolor", key)
        except tk.TclError:
            pass

    def _resize(self, e):
        w, h = e.width, e.height
        if w < 8 or h < 8:
            return
        try:
            i = self._inset
            self.itemconfigure(self._wid, width=max(1, w - 2 * i), height=max(1, h - 2 * i))
            if (w, h) != self._size:
                self._size = (w, h)
                art = render_shell(w, h, self._r, self._theme["bg"], self._line or self._theme["line"],
                                   _hex_rgb(self._key(self._theme)))
                self._photo = ImageTk.PhotoImage(art, master=self)
                if self._item is None:
                    self._item = self.create_image(0, 0, image=self._photo, anchor="nw")
                else:
                    self.itemconfigure(self._item, image=self._photo)
                self.tag_lower(self._item)
        except tk.TclError:
            pass

    def _rerender(self):
        self._size = (0, 0)
        self._resize(type("E", (), {"width": self.winfo_width(), "height": self.winfo_height()})())

    def retheme(self, theme: dict):
        self._theme = theme
        key = self._key(theme)
        self.configure(bg=key)
        self._apply_key(key)
        self.body.configure(bg=theme["bg"])
        self._rerender()

    def set_line(self, color: str | None):
        """Recolour the 1px hairline (the command bar lights it up in the accent while you type)."""
        self._line = color
        self._rerender()

    def destroy(self):
        self._photo = self._item = None
        try:
            super().destroy()
        except tk.TclError:
            pass

def render_check(size: int, c: dict, hover: bool = False, press: bool = False):
    """Task completion circle: hollow ring -> accent ring + faint check on hover -> filled check on press."""
    img, d, S = _aa(size, size)
    m = 1.5
    box = (m * S, m * S, (size - m) * S - 1, (size - m) * S - 1)
    if press:
        d.ellipse(box, fill=c["accent"])
    elif hover:
        d.ellipse(box, fill=_mixhex(c["card"], c["accent"], 0.22), outline=c["accent"], width=int(1.5 * S))
    else:
        d.ellipse(box, outline=_mixhex(c["muted"], c["card"], 0.15), width=int(1.5 * S))
    if hover or press:
        pts = [(0.30 * size * S, 0.52 * size * S), (0.44 * size * S, 0.66 * size * S), (0.72 * size * S, 0.36 * size * S)]
        d.line(pts, fill=c["on_accent"] if press else c["accent"], width=int(1.8 * S), joint="curve")
    return _down(img, size, size)


def render_swatch(size: int, color: str, selected: bool, hover: bool, c: dict):
    img, d, S = _aa(size, size)
    cx = size / 2.0
    g = Image.new("L", img.size, 0)
    ImageDraw.Draw(g).ellipse(((cx - 9) * S, (cx - 9) * S, (cx + 9) * S, (cx + 9) * S), fill=255)
    g = g.filter(ImageFilter.GaussianBlur(3 * S)).point(lambda v: int(v * (0.25 if c["light"] else 0.45)))
    glow = Image.new("RGBA", img.size, _hex_rgb(color) + (255,))
    glow.putalpha(g)
    img = Image.alpha_composite(img, glow)
    d = ImageDraw.Draw(img)
    r = 8.0 if hover else 7.0
    d.ellipse(((cx - r) * S, (cx - r) * S, (cx + r) * S, (cx + r) * S), fill=color, outline=c["line"], width=S)
    if selected:
        r2 = size / 2.0 - 1.5
        d.ellipse(((cx - r2) * S, (cx - r2) * S, (cx + r2) * S, (cx + r2) * S), outline=c["text"], width=int(1.6 * S))
    return _down(img, size, size)


def render_segmented(w: int, h: int, labels, sel: int, c: dict, hover: int = -1, px: float = 9):
    img, d, S = _aa(w, h)
    d.rounded_rectangle((0, 0, w * S - 1, h * S - 1), radius=h * S / 2.0, fill=c["bg"], outline=c["line"], width=S)
    n = max(1, len(labels))
    seg = (w - 6) / n
    f = _font(px)
    for i, lab in enumerate(labels):
        x0 = 3 + i * seg
        box = (x0 * S, 3 * S, (x0 + seg) * S - 1, (h - 3) * S - 1)
        rad = (h - 6) * S / 2.0
        if i == sel:
            d.rounded_rectangle(box, radius=rad, fill=c["accent"])
            fg = c["on_accent"]
        else:
            if i == hover:
                d.rounded_rectangle(box, radius=rad, fill=_mixhex(c["bg"], c["text"], 0.10))
            fg = c["text"]
        cx, cy = (x0 + seg / 2.0) * S, h * S / 2.0
        try:
            d.text((cx, cy), lab, font=f, fill=fg, anchor="mm")
        except Exception:
            d.text((cx - f.getlength(lab) / 2.0, cy - px * S * 0.6), lab, font=f, fill=fg)
    return _down(img, w, h)


def render_mini_preview(w: int, h: int, color: str, shape: str, mood_on: bool, c: dict):
    """Live preview for the Customize panel: Mish's always-dark squircle + eyes, and the mood-tint legend."""
    img, d, S = _aa(w, h)
    bw, bh, pad = 144, 76, 3
    cx, cy = 94.0, h / 2.0
    lw, lh = (bw + 2 * pad) * S, (bh + 2 * pad) * S
    mask = Image.new("L", (lw, lh), 0)
    ImageDraw.Draw(mask).polygon(_squircle_points(lw / 2.0, lh / 2.0, bw / 2.0 * S, bh / 2.0 * S), fill=255)
    inner = mask.filter(ImageFilter.MinFilter(2 * S + 1))
    body = Image.new("RGBA", (lw, lh), NOTCH_BG + (255,))
    body.putalpha(mask)
    rim = Image.new("RGBA", (lw, lh), (255, 255, 255, 255))
    rim.putalpha(ImageChops.subtract(mask, inner).point(lambda v: int(v * 0.18)))
    grad = ImageChops.invert(Image.linear_gradient("L")).resize((lw, 18 * S), Image.Resampling.BILINEAR)
    spec = Image.new("L", (lw, lh), 0)
    spec.paste(grad, (0, pad * S))
    spec = ImageChops.multiply(ImageChops.multiply(spec, inner).filter(ImageFilter.GaussianBlur(S)), mask)
    hl = Image.new("RGBA", (lw, lh), (255, 255, 255, 255))
    hl.putalpha(spec.point(lambda v: int(v * 0.16)))
    sq = Image.alpha_composite(Image.alpha_composite(body, rim), hl)

    ew, eh, rad = SHAPE_DIMS.get(shape, SHAPE_DIMS["pill"])
    rgb = _hex_rgb(color)
    em = Image.new("L", (lw, lh), 0)
    ed = ImageDraw.Draw(em)
    for sgn in (-1, 1):
        ex, ey = lw / 2.0 + sgn * (10 + ew / 2.0) * S, lh / 2.0 - 6 * S
        ed.rounded_rectangle((ex - ew * S / 2.0, ey - eh * S / 2.0, ex + ew * S / 2.0 - 1, ey + eh * S / 2.0 - 1),
                             radius=min(rad * S, ew * S / 2.0, eh * S / 2.0), fill=255)
    glow = Image.new("RGBA", (lw, lh), rgb + (255,))
    glow.putalpha(em.filter(ImageFilter.GaussianBlur(3.5 * S)).point(lambda v: int(v * 0.55)))
    eyes = Image.new("RGBA", (lw, lh), rgb + (255,))
    eyes.putalpha(em)
    sq = Image.alpha_composite(sq, glow)
    sq = Image.alpha_composite(sq, eyes)
    sd = ImageDraw.Draw(sq)
    f = _font(9)
    try:
        sd.ellipse((lw / 2.0 - 26 * S, lh / 2.0 + 21 * S, lw / 2.0 - 20 * S, lh / 2.0 + 27 * S), fill=(255, 255, 255, 255))
        sd.text((lw / 2.0 - 14 * S, lh / 2.0 + 24 * S), "idle", font=f, fill=(226, 232, 240, 255), anchor="lm")
    except Exception:
        pass
    img.alpha_composite(sq, (int((cx - bw / 2.0 - pad) * S), int((cy - bh / 2.0 - pad) * S)))

    d = ImageDraw.Draw(img)                                           # mood legend (dimmed when tints are off)
    items = (("happy", STATE_RGB["happy"]), ("curious", STATE_RGB["curious"]),
             ("alert", STATE_RGB["scared"]), ("sleepy", STATE_RGB["sleepy"]))
    off = _hex_rgb(_mixhex(c["muted"], c["card"], 0.45))
    for i, (name, col) in enumerate(items):
        y = h / 2.0 - 25.5 + i * 17
        colr = col if mood_on else off
        d.ellipse((190 * S, (y - 4) * S, 198 * S, (y + 4) * S), fill=colr)
        try:
            d.text((206 * S, y * S), name, font=f, fill=c["text"] if mood_on else c["muted"], anchor="lm")
        except Exception:
            d.text((206 * S, (y - 6) * S), name, font=f, fill=c["text"] if mood_on else c["muted"])
    return _down(img, w, h)


class CheckCircle(_PilCanvas):
    """20px completion circle for task rows. command() fires on a click released inside the circle."""

    def __init__(self, parent, command=None, theme: dict | None = None, accent: str = "#ffffff",
                 size: int = 20, bg: str | None = None):
        self._theme = theme or get_theme("auto")
        self._accent, self._command, self._hover, self._press = accent, command, False, False
        self._c = ui_colors(self._theme, accent)
        super().__init__(parent, size, size, bg or _parent_bg(parent, self._c["card"]), cursor="hand2")
        self.bind("<Enter>", lambda e: self._flag(hover=True))
        self.bind("<Leave>", lambda e: self._flag(hover=False, press=False))
        self.bind("<ButtonPress-1>", lambda e: self._flag(press=True))
        self.bind("<ButtonRelease-1>", self._release)
        self._draw()

    def _draw(self):
        if self._alive:
            self._show(render_check(self.w, self._c, self._hover, self._press))

    def _flag(self, **kw):
        self._hover = kw.get("hover", self._hover)
        self._press = kw.get("press", self._press)
        self._draw()

    def _release(self, e):
        was = self._press
        self._flag(press=False)
        if was and 0 <= e.x < self.w and 0 <= e.y < self.h and self._command:
            self._command()                                           # last: the row may be destroyed by it


class Swatch(_PilCanvas):
    """28px glow-colour swatch. command(hex) on click."""

    def __init__(self, parent, color: str, selected: bool = False, command=None, theme: dict | None = None,
                 accent: str = "#ffffff", size: int = 28, bg: str | None = None):
        self._theme = theme or get_theme("auto")
        self.color, self._sel, self._command, self._hover = color, bool(selected), command, False
        self._c = ui_colors(self._theme, accent)
        super().__init__(parent, size, size, bg or _parent_bg(parent, self._c["card"]), cursor="hand2")
        self.bind("<Enter>", lambda e: self._hov(True))
        self.bind("<Leave>", lambda e: self._hov(False))
        self.bind("<Button-1>", lambda e: self._command(self.color) if self._command else None)
        self._draw()

    def _hov(self, on: bool):
        self._hover = on
        self._draw()

    def _draw(self):
        if self._alive:
            self._show(render_swatch(self.w, self.color, self._sel, self._hover, self._c))

    def select(self, on: bool):
        if bool(on) != self._sel:
            self._sel = bool(on)
            self._draw()


class SegmentedControl(_PilCanvas):
    """Pill segmented control. options = ((label, key), ...). command(key) on click."""

    def __init__(self, parent, options, value, command=None, theme: dict | None = None, accent: str = "#ffffff",
                 width: int = 280, height: int = 28, px: float = 9, bg: str | None = None):
        self._theme = theme or get_theme("auto")
        self._opts, self._command, self._px = list(options), command, px
        self._c = ui_colors(self._theme, accent)
        self._idx = self._index(value)
        self._hover = -1
        super().__init__(parent, width, height, bg or _parent_bg(parent, self._c["card"]), cursor="hand2")
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._set_hover(-1))
        self.bind("<Button-1>", self._click)
        self._draw()

    def _index(self, key) -> int:
        for i, (_l, k) in enumerate(self._opts):
            if k == key:
                return i
        return -1

    def _at(self, x: float) -> int:
        seg = (self.w - 6) / max(1, len(self._opts))
        i = int((x - 3) // seg)
        return i if 0 <= i < len(self._opts) else -1

    def _draw(self):
        if self._alive:
            self._show(render_segmented(self.w, self.h, [l for l, _k in self._opts], self._idx, self._c,
                                        self._hover, self._px))

    def _set_hover(self, i: int):
        if i != self._hover:
            self._hover = i
            self._draw()

    def _motion(self, e):
        self._set_hover(self._at(e.x))

    def _click(self, e):
        i = self._at(e.x)
        if i < 0:
            return
        self._idx = i
        self._draw()
        if self._command:
            self._command(self._opts[i][1])

    def set_value(self, key):
        i = self._index(key)
        if i != self._idx:
            self._idx = i
            self._draw()


class MiniPreview(_PilCanvas):
    """Live Mish preview (dark squircle + eyes + mood legend). update() re-renders in place."""

    def __init__(self, parent, width: int, height: int, theme: dict, accent: str, color: str, shape: str,
                 mood_on: bool, bg: str | None = None):
        self._c = ui_colors(theme, accent)
        self._color, self._shape, self._mood = color, shape, bool(mood_on)
        super().__init__(parent, width, height, bg or _parent_bg(parent, self._c["card"]))
        self._draw()

    def _draw(self):
        if self._alive:
            self._show(render_mini_preview(self.w, self.h, self._color, self._shape, self._mood, self._c))

    def update_preview(self, color: str, shape: str, mood_on: bool):
        self._color, self._shape, self._mood = color, shape, bool(mood_on)
        self._draw()
# ===================================================================== PIL widgets (END)


class _HudPanel(tk.Toplevel):
    """Base for every HUD popover: 14px RoundedShell chrome, theme palette from face.theme, rebuild() on theme
    change. Subclasses implement _build(body) (+ _snapshot()/_restore() to keep typed text across a rebuild).
    The panel is measured and sized before it is shown, so it opens at its final width and height."""
    WIDTH, INSET = 340, 5
    BOTTOM_PAD = 12                                # space under the last row, same on every panel

    def __init__(self, face):
        super().__init__(face.root)
        self.face = face
        self._geo_set, self._tick_id = False, None
        self._fitting, self._last_h = False, 0
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.97)
        self.c = self._palette()
        self.shell = RoundedShell(self, face.theme, radius=14, inset=self.INSET)
        self.shell.pack(fill="both", expand=True)
        self.body = self.shell.body
        l, t, r, _b = face._monitor_rect()
        wx, wy = face._win_xy()
        x = wx + face.root.winfo_width() + 12
        if x + self.WIDTH > r:
            x = max(l + 10, wx - self.WIDTH - 12)
        self._pos = (int(x), int(max(t + 10, wy)))
        self._build(self.body)
        self._fit()                                # full size BEFORE the first paint
        self.bind("<Escape>", lambda e: self.destroy())
        self.body.bind("<Configure>", lambda _e: self._fit(), add="+")
        self.deiconify()
        self.after_idle(self._fit)                 # cheap safety passes for late font / wrap changes
        self.after(80, self._fit)
        self.focus_force()

    # ---- palette / sizing / rebuild ----
    def _palette(self) -> dict:
        c = ui_colors(self.face.theme, self.face.eye_col)
        light = c["light"]
        c["green"] = "#059669" if light else "#34d399"
        c["amber"] = "#d97706" if light else "#fbbf24"
        return c

    def _sync_cards(self, widget):
        """Post-order walk: inner cards (input wells) are sized before the cards that contain them."""
        for ch in widget.winfo_children():
            self._sync_cards(ch)
            if isinstance(ch, RoundedCard):
                ch.sync()

    def _fit(self):
        if self._fitting:
            return
        self._fitting = True
        try:
            for _ in range(3):                     # nested cards need a geometry pass between syncs
                self._sync_cards(self.body)
                self.update_idletasks()
            h = min(self.body.winfo_reqheight() + 2 * self.shell._inset, self.face.root.winfo_screenheight() - 40)
            if not self._geo_set:
                self._geo_set = True
                self._last_h = h
                self.geometry(f"{self.WIDTH}x{h}+{self._pos[0]}+{self._pos[1]}")
            elif h != self._last_h:
                self._last_h = h
                self.geometry(f"{self.WIDTH}x{h}")
        except tk.TclError:
            pass
        finally:
            self._fitting = False

    def rebuild(self):
        """Re-skin in place (theme / accent changed). Keeps position and any typed text."""
        try:
            if not self.winfo_exists():
                return
            snap = self._snapshot()
            for w in self.body.winfo_children():
                w.destroy()
            self.c = self._palette()
            self.shell.retheme(self.face.theme)
            self._build(self.body)
            self._restore(snap)
            self._fit()
        except tk.TclError:
            pass

    def _snapshot(self) -> dict:
        return {}

    def _restore(self, snap: dict):
        pass

    def _build(self, body):
        raise NotImplementedError

    # ---- shared building blocks ----
    def _header(self, parent, title: str):
        c = self.c
        head = tk.Frame(parent, bg=c["bg"])
        head.pack(fill="x", padx=14, pady=(12, 8))
        lbl = tk.Label(head, text=title, bg=c["bg"], fg=c["accent"], font=("Consolas", 9, "bold"), anchor="w")
        lbl.pack(side="left")
        close = tk.Label(head, text="\u2715", bg=c["bg"], fg=c["muted"], cursor="hand2", font=("Segoe UI", 10))
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.destroy())
        close.bind("<Enter>", lambda e: close.configure(fg=c["rose"]))
        close.bind("<Leave>", lambda e: close.configure(fg=c["muted"]))
        _make_draggable(self, head, lbl)
        return lbl

    def _card(self, parent, caption: str = "", pady=(0, 8)):
        """10px inner card; returns it (put widgets in .body). `caption` adds a small-caps heading row."""
        c = self.c
        card = RoundedCard(parent, self.face.theme, radius=10, pad=10, bg=c["bg"])
        card.pack(fill="x", padx=14, pady=pady)
        if caption:
            tk.Label(card.body, text=caption, bg=c["card"], fg=c["muted"], font=("Consolas", 8, "bold"),
                     anchor="w").pack(fill="x")
        return card

    def _input_well(self, parent, font, show=None):
        """Rounded text input (8px). Hairline turns accent while focused. Returns (well, entry)."""
        c = self.c
        well = RoundedCard(parent, self.face.theme, radius=8, pad=5, fill=c["bg"], line=c["line"], bg=c["card"])
        entry = tk.Entry(well.body, bg=c["bg"], fg=c["text_hi"], insertbackground=c["accent"], relief="flat", bd=0,
                         highlightthickness=0, font=font, show=show or "")
        entry.pack(fill="x", ipady=3)
        entry.bind("<FocusIn>", lambda e: well.set_line(c["accent"]), add="+")
        entry.bind("<FocusOut>", lambda e: well.set_line(c["line"]), add="+")
        return well, entry

    def destroy(self):
        if self._tick_id is not None:
            try:
                self.after_cancel(self._tick_id)
            except Exception:
                pass
            self._tick_id = None
        try:
            super().destroy()
        except tk.TclError:
            pass

class TaskPanel(_HudPanel):
    """Active tasks (local only). Reads/writes the same memory.json as Mish and polls it every second."""
    LIST_H = 250
    HINT = ""

    def __init__(self, face):
        self.store, self._sig = face.mish.store, None
        super().__init__(face)
        self._tick()

    def _snapshot(self) -> dict:
        try:
            return {"entry": self.entry.get()}
        except Exception:
            return {}

    def _restore(self, snap: dict):
        if snap.get("entry"):
            self.entry.insert(0, snap["entry"])

    def _build(self, body):
        c, th, acc = self.c, self.face.theme, self.face.eye_col
        self._sig = None
        self.header = self._header(body, "ACTIVE TASKS [0]")
        bar = tk.Frame(body, bg=c["bg"])
        bar.pack(fill="x", padx=14)
        PillButton(bar, "Add", self.add, th, acc, kind="primary", height=30, px=9, bg=c["bg"]).pack(side="right", padx=(8, 0))
        well, self.entry = self._input_well(bar, ("Segoe UI", 10))
        well.configure(bg=c["bg"])
        well.pack(side="left", fill="x", expand=True)
        self.status = tk.Label(body, text=self.HINT, bg=c["bg"], fg=c["muted"], font=("Segoe UI", 8), anchor="w")
        self.status.pack(fill="x", padx=16, pady=(2, 4))
        self.entry.bind("<Return>", self.add)

        card = self._card(body, pady=(0, self.BOTTOM_PAD))
        self.canvas = tk.Canvas(card.body, bg=c["card"], height=self.LIST_H, width=10, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.rows = tk.Frame(self.canvas, bg=c["card"])
        self._rid = self.canvas.create_window((0, 0), window=self.rows, anchor="nw")
        self.rows.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._rid, width=e.width))
        self._wheel(self.canvas)
        self._wheel(self.rows)
        self.refresh()
        self.entry.focus_force()

    def _wheel(self, w):
        w.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"), add="+")

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

    def remove(self, task):
        self.store.delete(task)
        self._say(f"Removed: {task['text'][:40]}")
        self.refresh()

    def finish(self, task):
        self.store.complete(task)
        self._say(f"Done: {task['text'][:40]}")
        self.refresh()

    def _say(self, text, warn=False):
        self.status.configure(text=text, fg=self.c["rose"] if warn else self.c["accent"])

    # ---- list ----
    def refresh(self):
        tasks = self.store.pending()
        sig = [(t["id"], t["text"], fmt_due(t["due"]) if t["due"] else "") for t in tasks]
        if sig == self._sig:
            return
        self._sig = sig
        for w in self.rows.winfo_children():
            w.destroy()
        self.header.configure(text=f"ACTIVE TASKS [{len(tasks)}]")
        if not tasks:
            lbl = tk.Label(self.rows, text="Nothing to do. Suspicious.", bg=self.c["card"], fg=self.c["muted"],
                           font=("Segoe UI", 10))
            lbl.pack(anchor="w", padx=8, pady=10)
            self._wheel(lbl)
        for t in tasks:
            self._row(t)

    def _row(self, task):
        c = self.c
        row = tk.Frame(self.rows, bg=c["card"])
        row.pack(fill="x", pady=1)
        chk = CheckCircle(row, lambda t=task: self.finish(t), self.face.theme, self.face.eye_col, size=20, bg=c["card"])
        chk.pack(side="left", padx=(6, 6), pady=8)
        rm = tk.Label(row, text="\u2715", bg=c["card"], fg=c["card"], cursor="hand2", font=("Segoe UI", 10), padx=8)
        rm.pack(side="right")
        info = tk.Frame(row, bg=c["card"])
        info.pack(side="left", fill="x", expand=True, pady=5)
        txt = tk.Label(info, text=task["text"], bg=c["card"], fg=c["text"], font=("Segoe UI", 10),
                       anchor="w", justify="left", wraplength=190)
        txt.pack(fill="x")
        ws = [row, chk, rm, info, txt]
        if task["due"]:
            label = fmt_due(task["due"])
            badge = tk.Label(info, text=label, bg=c["card"], fg=c["rose"] if "overdue" in label else c["muted"],
                             font=("Segoe UI", 8), anchor="w")
            badge.pack(fill="x")
            ws.append(badge)
        rm.bind("<Button-1>", lambda e, t=task: self.remove(t))

        def paint(hovering: bool):
            col = c["hover"] if hovering else c["card"]
            for w in ws:
                w.configure(bg=col)
            rm.configure(fg=c["rose"] if hovering else col)
        _hover_group(self, ws, lambda: paint(True), lambda: paint(False))
        for w in ws:
            self._wheel(w)

    def _tick(self):
        try:
            if not self.winfo_exists():
                return
            self.refresh()
            self._tick_id = self.after(1000, self._tick)
        except tk.TclError:
            pass


class CustomizeStudio(_HudPanel):
    """MISH // STUDIO: theme-aware, every change saved and previewed live (mini Mish on top)."""
    SHAPES = (("Pill", "pill"), ("Round", "round"), ("Slim", "slim"), ("Visor", "visor"), ("Square", "square"))
    THEMES_OPTS = (("Auto", "auto"), ("Dark", "dark"), ("Light", "light"))
    @property
    def INNER(self) -> int:                        # panel width - shell insets - 2*14 outer pad - 2*10 card pad
        return self.WIDTH - 2 * self.shell._inset - 2 * 14 - 2 * 10
    def __init__(self, face):
        self.s = face.settings
        super().__init__(face)

    def _build(self, body):
        s, c, th, acc = self.s, self.c, self.face.theme, self.face.eye_col
        W = self.INNER
        self.title_lbl = self._header(body, "MISH // STUDIO")

        pv = self._card(body)                                         # live preview
        self.preview = MiniPreview(pv.body, W, 96, th, acc, s["eye"], s["shape"], s["mood"], bg=c["card"])
        self.preview.pack()

        card = self._card(body, "GLOW COLOUR")
        row = tk.Frame(card.body, bg=c["card"])
        row.pack(fill="x", pady=(6, 0))
        self.sw = []
        for _name, hexv in EYE_PRESETS:
            sw = Swatch(row, hexv, hexv.lower() == str(s["eye"]).lower(), lambda h: self.change(eye=h), th, acc,
                        bg=c["card"])
            sw.pack(side="left")
            self.sw.append(sw)
        PillButton(row, "+ Custom", self._custom, th, acc, height=26, px=8.5, bg=c["card"]).pack(side="right")

        card = self._card(body, "HUD THEME")
        self.theme_seg = SegmentedControl(card.body, self.THEMES_OPTS, s["theme"], lambda k: self.change(theme=k),
                                          th, acc, width=W, bg=c["card"])
        self.theme_seg.pack(pady=(6, 0))

        card = self._card(body, "CAPSULE GEOMETRY")
        self.shape_seg = SegmentedControl(card.body, self.SHAPES, s["shape"], lambda k: self.change(shape=k),
                                          th, acc, width=W, px=8.5, bg=c["card"])
        self.shape_seg.pack(pady=(6, 0))

        card = self._card(body, "HUD OPACITY")
        self.slider = RoundSlider(card.body, 40, 100, int(round(s["opacity"] * 100)), command=self._slide,
                                  on_release=lambda v: self.s.save(), width=W, step=1,
                                  fmt=lambda v: f"{int(round(v))}%", theme=th, accent=acc, bg=c["card"])
        self.slider.pack(pady=(4, 0))

        card = self._card(body, "SIGNAL MOOD TINTS")
        row = tk.Frame(card.body, bg=c["card"])
        row.pack(fill="x", pady=(6, 0))
        tk.Label(row, text="Mood-reactive eye colours", bg=c["card"], fg=c["text"], font=("Segoe UI", 9),
                 anchor="w").pack(side="left")
        self.mood_sw = ToggleSwitch(row, bool(s["mood"]), lambda v: self.change(mood=v), th, acc, bg=c["card"])
        self.mood_sw.pack(side="right")

        foot = tk.Frame(body, bg=c["bg"])
        foot.pack(fill="x", padx=14, pady=(6, self.BOTTOM_PAD))
        PillButton(foot, "Reset", self._reset, th, acc, height=28, bg=c["bg"]).pack(side="left")
        PillButton(foot, "Done", self.destroy, th, acc, kind="primary", height=28, bg=c["bg"]).pack(side="right")

    # ---- state ----
    def change(self, **kw):
        before = getattr(self.face, "_panel_key", None)
        self.s.update(**kw)
        self.face.apply_theme()                                       # may schedule rebuild() via _retheme_panels
        if getattr(self.face, "_panel_key", None) == before:
            self._sync()

    def _custom(self):
        from tkinter import colorchooser                  # the one native dialog left: Windows has no dark picker
        _rgb, picked = colorchooser.askcolor(color=self.s["eye"], parent=self, title="Glow colour")
        if picked:
            self.change(eye=picked)

    def _reset(self):
        self.s.reset()
        self.face.apply_theme()
        self.after_idle(self.rebuild)

    def _slide(self, v):
        self.s.data["opacity"] = round(v / 100.0, 2)       # saved on mouse release, not on every pixel
        self.face.apply_theme()

    def _sync(self):
        try:
            s = self.s
            for sw in self.sw:
                sw.select(sw.color.lower() == str(s["eye"]).lower())
            self.theme_seg.set_value(s["theme"])
            self.shape_seg.set_value(s["shape"])
            if self.mood_sw.get() != bool(s["mood"]):
                self.mood_sw.set(bool(s["mood"]), animate=True)
            self.preview.update_preview(s["eye"], s["shape"], s["mood"])
        except tk.TclError:
            pass


class ConnectionsPanel(_HudPanel):
    """MISH // CONNECTIONS: Blackboard, Google & Microsoft, on the shared themed widgets."""
    WIDTH = 360

    def __init__(self, face):
        self.hub = face.mish.hub
        super().__init__(face)
        self._tick()

    def _snapshot(self) -> dict:
        try:
            return {"bb": self.bb_entry.get(), "ms": self.ms_entry.get()}
        except Exception:
            return {}

    def _restore(self, snap: dict):
        for key, ent in (("bb", self.bb_entry), ("ms", self.ms_entry)):
            if snap.get(key):
                ent.delete(0, "end")
                ent.insert(0, snap[key])

    def _conn_card(self, parent, title_text):
        c = self.c
        card = self._card(parent, pady=(0, 8))
        top = tk.Frame(card.body, bg=c["card"])
        top.pack(fill="x")
        tk.Label(top, text=title_text, bg=c["card"], fg=c["text"], font=("Consolas", 8, "bold")).pack(side="left")
        badge = tk.Label(top, text="", bg=c["card"], font=("Consolas", 8, "bold"))
        badge.pack(side="right")
        row = tk.Frame(card.body, bg=c["card"])
        row.pack(fill="x", pady=(8, 0))
        return card, badge, row

    def _build(self, body):
        c, th, acc = self.c, self.face.theme, self.face.eye_col
        self._header(body, "MISH // CONNECTIONS")
        self.status_bar = tk.Label(body, text="Manage external calendars, mail & deadlines", bg=c["bg"], fg=c["muted"],
                                   font=("Segoe UI", 8), anchor="w")
        self.status_bar.pack(fill="x", padx=16, pady=(0, 8))

        _card, self.bb_badge, row = self._conn_card(body, "BLACKBOARD ICAL FEED")
        PillButton(row, "Save & Sync", self._save_bb, th, acc, kind="primary", height=28, px=8.5).pack(side="right", padx=(8, 0))
        well, self.bb_entry = self._input_well(row, ("Consolas", 8))
        well.pack(side="left", fill="x", expand=True)

        _card, self.g_badge, row = self._conn_card(body, "GOOGLE (CALENDAR + GMAIL)")
        PillButton(row, "Load JSON", self._pick_google_json, th, acc, height=28, px=8.5).pack(side="left")
        self.g_btn = PillButton(row, "Connect", self._toggle_google, th, acc, kind="primary", height=28, px=8.5, width=96)
        self.g_btn.pack(side="right")

        _card, self.ms_badge, row = self._conn_card(body, "MICROSOFT (OUTLOOK + TO DO)")
        self.ms_btn = PillButton(row, "Connect", self._toggle_ms, th, acc, kind="primary", height=28, px=8.5, width=96)
        self.ms_btn.pack(side="right", padx=(8, 0))
        well, self.ms_entry = self._input_well(row, ("Consolas", 8))
        well.pack(side="left", fill="x", expand=True)

        foot = tk.Frame(body, bg=c["bg"])
        foot.pack(fill="x", padx=14, pady=(2, self.BOTTOM_PAD))
        PillButton(foot, "Sync All Now", self._sync_all, th, acc, kind="primary", height=28, bg=c["bg"]).pack(side="left")
        PillButton(foot, "Edit Config JSON", self.hub.open_config_file, th, acc, height=28, bg=c["bg"]).pack(side="right")

        st = self.hub.status()
        if st["bb_url"]:
            self.bb_entry.insert(0, st["bb_url"])
        if st["ms_client_id"]:
            self.ms_entry.insert(0, st["ms_client_id"])
        self._refresh()

    def _note(self, text, color=None):
        self.status_bar.configure(text=text, fg=color or self.c["accent"])

    def _save_bb(self):
        url = self.bb_entry.get().strip()
        if not url:
            self._note("Paste your Blackboard .ics link first.", self.c["rose"])
            return
        self.hub.set_blackboard_url(url)
        self._note("Saved Blackboard URL â€” syncing...", self.c["green"])
        self.hub.sync_now_async(announce_empty=True)
        self.after(1500, self._refresh)

    def _pick_google_json(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(parent=self, title="Select Google credentials.json",
                                          filetypes=[("JSON files", "*.json")])
        if not path:
            return
        ok, msg = self.hub.install_google_creds(path)
        self._note(msg, self.c["green"] if ok else self.c["rose"])
        self._refresh()

    def _toggle_google(self):
        if self.hub.status()["google_connected"]:
            self.hub.disconnect_google()
            self._note("Google disconnected.", self.c["amber"])
            self._refresh()
        else:
            self._note("Opening browser for Google sign-in...")
            self.hub.connect_google_async()

    def _toggle_ms(self):
        if self.hub.status()["ms_connected"]:
            self.hub.disconnect_microsoft()
            self._note("Microsoft disconnected.", self.c["amber"])
            self._refresh()
        else:
            cid = self.ms_entry.get().strip()
            if not cid:
                self._note("Paste your Azure App Client ID first.", self.c["rose"])
                return
            self.hub.set_ms_client_id(cid)
            self._note("Opening browser for Microsoft sign-in...")
            self.hub.connect_microsoft_async()

    def _sync_all(self):
        self._note("Syncing Blackboard, Google & Microsoft...")
        self.hub.sync_now_async(announce_empty=True)

    @staticmethod
    def _btn_state(btn, text: str, kind: str):
        if btn._text != text:
            btn.set_text(text)
        if btn._kind != kind:
            btn.set_kind(kind)

    def _refresh(self):
        try:
            c, st = self.c, self.hub.status()
            if st["bb_connected"]:
                self.bb_badge.configure(text=f"\u25cf LINKED ({st['bb_count']})", fg=c["green"])
            else:
                self.bb_badge.configure(text="\u25cb NOT SET", fg=c["muted"])
            if st["google_connected"]:
                self.g_badge.configure(text="\u25cf CONNECTED", fg=c["green"])
                self._btn_state(self.g_btn, "Disconnect", "danger")
            elif st["google_has_creds"]:
                self.g_badge.configure(text="\u25cf READY TO SIGN IN", fg=c["amber"])
                self._btn_state(self.g_btn, "Connect", "primary")
            else:
                self.g_badge.configure(text="\u25cb NO CREDENTIALS.JSON", fg=c["muted"])
                self._btn_state(self.g_btn, "Connect", "secondary")
            if st["ms_connected"]:
                self.ms_badge.configure(text="\u25cf CONNECTED", fg=c["green"])
                self._btn_state(self.ms_btn, "Disconnect", "danger")
            elif st["ms_client_id"]:
                self.ms_badge.configure(text="\u25cf READY TO SIGN IN", fg=c["amber"])
                self._btn_state(self.ms_btn, "Connect", "primary")
            else:
                self.ms_badge.configure(text="\u25cb NO CLIENT ID", fg=c["muted"])
                self._btn_state(self.ms_btn, "Connect", "primary")
        except tk.TclError:
            pass

    def _tick(self):
        try:
            if not self.winfo_exists():
                return
            self._refresh()
            self._tick_id = self.after(1500, self._tick)
        except tk.TclError:
            pass

class DevConsole(tk.Toplevel):
    """Plain-Tk developer console: live telemetry, quick triggers and a command line.
    No RoundedShell / PIL. Commands run on a worker thread; results return through the UI queue."""
    W, H = 480, 470
    BG, FIELD, FG, DIM, ACC, HOVER = "#111318", "#1A1D24", "#E2E8F0", "#64748b", "#34d399", "#262b36"
    FONT = ("Consolas", 9)

    def __init__(self, face):
        super().__init__(face.root)
        self.face, self.mish = face, face.mish
        self._tick_id = None
        self.title("Mish Dev Console")
        self.configure(bg=self.BG)
        self.attributes("-topmost", True)
        self.minsize(380, 320)
        self._build()
        self._dock()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.log("Dev console ready. Type help() for commands.")
        self._tick()
        self.cmd.focus_set()

    # ---- widgets ----
    def _button(self, parent, text, command):
        return tk.Button(parent, text=text, command=command, bg=self.FIELD, fg=self.FG, activebackground=self.HOVER,
                         activeforeground=self.FG, relief="flat", bd=0, font=("Consolas", 8), padx=8, pady=3,
                         cursor="hand2")

    def _entry(self, parent, width=None):
        e = tk.Entry(parent, bg=self.FIELD, fg=self.FG, insertbackground=self.FG, relief="flat", bd=0,
                     highlightthickness=1, highlightbackground=self.HOVER, highlightcolor=self.ACC, font=self.FONT)
        if width:
            e.configure(width=width)
        return e

    def _build(self):
        self.status = tk.Label(self, text="", bg=self.BG, fg=self.ACC, font=self.FONT, justify="left", anchor="w")
        self.status.pack(fill="x", padx=10, pady=(8, 6))

        act = tk.Frame(self, bg=self.BG)
        act.pack(fill="x", padx=10)
        self.param = self._entry(act, 22)
        self.param.insert(0, "fedup(85)")
        self.param.pack(side="left", ipady=3)
        self.param.bind("<Return>", lambda e: self._run(self.param.get()))
        self._button(act, "Run", lambda: self._run(self.param.get())).pack(side="left", padx=(6, 0))

        quick = tk.Frame(self, bg=self.BG)
        quick.pack(fill="x", padx=10, pady=(6, 6))
        for label, cmd in (("Fire Gaming", "fire(gaming)"), ("Fire Nag", "fire(nag)"),
                           ("Last Prompt", "prompt()"), ("Toggle Timers", "toggle_timers()")):
            self._button(quick, label, lambda c=cmd: self._run(c)).pack(side="left", padx=(0, 6))

        mid = tk.Frame(self, bg=self.BG)
        mid.pack(fill="both", expand=True, padx=10)
        sb = tk.Scrollbar(mid, orient="vertical")
        self.out = tk.Text(mid, bg=self.FIELD, fg=self.FG, font=self.FONT, relief="flat", bd=0, wrap="word",
                           state="disabled", highlightthickness=0, yscrollcommand=sb.set, height=10)
        sb.configure(command=self.out.yview)
        sb.pack(side="right", fill="y")
        self.out.pack(side="left", fill="both", expand=True)

        bar = tk.Frame(self, bg=self.BG)
        bar.pack(fill="x", padx=10, pady=(6, 10))
        tk.Label(bar, text=">", bg=self.BG, fg=self.ACC, font=("Consolas", 10, "bold")).pack(side="left")
        self.cmd = self._entry(bar)
        self.cmd.pack(side="left", fill="x", expand=True, padx=(6, 0), ipady=3)
        self.cmd.bind("<Return>", self._submit)

    def _dock(self):
        """Sit beside Mish (right side, or left if there is no room), clamped to his monitor."""
        f = self.face
        l, t, r, b = f._monitor_rect()
        wx, wy = f._win_xy()
        x = wx + f.root.winfo_width() + 12
        if x + self.W > r:
            x = max(l + 10, wx - self.W - 12)
        y = int(clamp(wy, t + 10, max(t + 10, b - self.H - 60)))
        self.geometry(f"{self.W}x{self.H}+{int(x)}+{y}")

    # ---- output / commands ----
    def log(self, text: str):
        try:
            self.out.configure(state="normal")
            self.out.insert("end", str(text) + "\n")
            if int(self.out.index("end-1c").split(".")[0]) > 500:       # keep the pane small
                self.out.delete("1.0", "100.0")
            self.out.see("end")
            self.out.configure(state="disabled")
        except tk.TclError:
            pass

    def _submit(self, _e=None):
        txt = self.cmd.get().strip()
        self.cmd.delete(0, "end")
        self._run(txt)

    def _run(self, cmd: str):
        cmd = cmd.strip()
        if not cmd:
            return
        self.log(f"> {cmd}")
        threading.Thread(target=self._exec, args=(cmd,), daemon=True).start()

    def _exec(self, cmd: str):
        """Worker thread: nothing here touches Tk; the result goes back through the UI queue."""
        m = self.mish
        try:
            if m.is_dev_cmd(cmd):
                out = m.run_dev_cmd(cmd)
            elif cmd.startswith("!"):
                out = m.handle_bang(cmd)
                if out:
                    m.say(out)                                  # same bubble he would show from the chat box
            else:
                m.handle_user_input(cmd)
                out = "(sent to Mish as chat)"
        except Exception as e:
            out = f"error: {e!r}"
        self.face.ui.put(("dev_out", out or "(no output)"))

    # ---- live telemetry ----
    def _tick(self):
        try:
            if not self.winfo_exists():
                return
            self.status.configure(text=self.mish.dev_status_text())
            self._tick_id = self.after(1000, self._tick)
        except tk.TclError:
            pass

    def destroy(self):
        if self._tick_id is not None:
            try:
                self.after_cancel(self._tick_id)
            except Exception:
                pass
            self._tick_id = None
        try:
            super().destroy()
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

        self.btn_gear = tk.Button(root, text="âš™", font=("Consolas", 9, "bold"), bd=0, relief="flat",
                                  cursor="hand2", command=self.show_menu)
        self.btn_gear.place(relx=0.88, rely=0.82, anchor="center", width=20, height=20)

        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="ðŸ’¬ Chat Input", command=self.open_chat_dialog)
        self.menu.add_command(label="ðŸ‘€ Look Screen", command=lambda: self.mish.observe_async(force=True))
        self.menu.add_command(label="ðŸ“Š Stats", command=lambda: self.show_speech_bubble(self.mish.stats_text()))
        self.menu.add_command(label="ðŸ“ Tasks", command=self.open_tasks)
        self.menu.add_command(label="ðŸŽ¨ Customize...", command=self.open_customize)
        self.menu.add_separator()
        self.menu.add_command(label="âŒ Exit", command=self.root.destroy)

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
            self.speak(f"Submitted a Windows search for {app}." if ok else f"I couldn't search for {app}.",
                       "happy" if ok else "sad")
        self._animate(self.ERRAND_RETURN_S, frame, done)

    # ---- theme -----------------------------------------------------------------------
    def _derive_theme(self):
         s = self.settings
         self.theme = get_theme(s["theme"])
         self.bg, self.eye_col = s["bg"], s["eye"]
         self.panel = _rgb_hex(_mix(_hex_rgb(self.bg), (255, 255, 255), 0.07))

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
        self._retheme_panels()

    def _retheme_panels(self):
        """Re-skin any open HUD panel when the theme (Light/Dark) or the accent colour actually changed."""
        key = (self.theme["name"], self.eye_col)
        old = getattr(self, "_panel_key", None)
        self._panel_key = key
        if old is None or old == key:
            return
        for attr in ("tasks_win", "conn_win", "cust"):
            w = getattr(self, attr, None)
            try:
                if w is not None and w.winfo_exists():
                    w.after_idle(w.rebuild)
            except tk.TclError:
                pass

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

    def open_connections(self):
        win = getattr(self, "conn_win", None)
        try:
            if win is not None and win.winfo_exists():
                win.lift()
                return
        except tk.TclError:
            pass
        self.conn_win = ConnectionsPanel(self)

    def open_customize(self):
        if self.cust is not None:
            try:
                if self.cust.winfo_exists():
                    self.cust.lift()
                    return
            except tk.TclError:
                pass
        self.cust = CustomizeStudio(self)

    def open_dev_console(self):
        win = getattr(self, "dev_win", None)
        try:
            if win is not None and win.winfo_exists():
                win.lift()
                return
        except tk.TclError:
            pass
        self.dev_win = DevConsole(self)

    def close_dev_console(self):
        win = getattr(self, "dev_win", None)
        try:
            if win is not None and win.winfo_exists():
                win.destroy()
        except tk.TclError:
            pass
        self.dev_win = None

    def dev_out(self, text: str):
        win = getattr(self, "dev_win", None)
        try:
            if win is not None and win.winfo_exists():
                win.log(text)
        except tk.TclError:
            pass

    # ---- speech bubbles, menu, chat box -------------------------------------------------
    def show_speech_bubble(self, text: str, duration: int | None = None, timer: bool = False):
        if self.active_bubble and self.active_bubble.winfo_exists():
            self.active_bubble.destroy()
        self._bubble_is_timer = timer
        self.active_bubble = DynamicSpeechBubble(self.root, text, duration or speech_duration_ms(text),
                                                 accent=self.eye_col, bg=self.panel, theme=self.theme)

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
            self.mish.react_async("strike_chat", extra="they tried to open the chat box")
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
            self.mish.handle_files(paths, use_llm=False)
        try:
            windnd.hook_dropfiles(self.root, func=on_drop, force_unicode=True)
        except Exception as e:
            log("file drop hook failed:", repr(e))

    # ---- drawing -------------------------------------------------------------------------

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
                self.mish.react_async("scared")
            elif now - self.edge_hold_start > 6.0:
                self.trigger_strike_mode()
        else:
            self.is_scared = False
            self.edge_hold_start = 0.0

        if self.mish.strike_mode and now > self.strike_until:
            self.mish.strike_mode = False
            self.mish.react_async("strike_end")

    def snap_to_center_punish(self, window_name: str):
        """Focus mode: jump to the middle of the screen and demand that the distraction be closed."""
        w, h = self.SIZE
        self.root.geometry(f"+{self.root.winfo_screenwidth() // 2 - w // 2}+{self.root.winfo_screenheight() // 2 - h // 2}")
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        threading.Thread(target=self.mish.focus_punish_say, args=(window_name,), daemon=True).start()

    def trigger_dizzy_state(self):
        self.is_dizzy = True
        self.mish.react_async("dizzy")

        def recover():
            time.sleep(3.0)
            self.is_dizzy = False
            self.ui.put(("expr", "sad", 2.5))                   # the "never again" look
        threading.Thread(target=recover, daemon=True).start()

    def trigger_strike_mode(self):
        self.is_scared = False
        self.mish.strike_mode = True
        self.strike_until = time.time() + 60.0
        self.mish.react_async("strike_start")

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
                    elif kind == "connections":
                        self.open_connections()
                    elif kind == "dev_panel":
                        self.open_dev_console() if item[1] else self.close_dev_console()
                    elif kind == "dev_out":
                        self.dev_out(item[1])
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

EYE_PAD, EYE_CY, EYE_GAP, NOTCH_BG = 10, 44, 20, (11, 13, 18)     # NOTCH_BG = #0B0D12, always dark
HALO_W, HALO_H = 110, 56                                          # tight inner bloom, clipped to the body
SHAPE_DIMS = {                       # shape -> (width, height, corner radius) in px
    "pill": (16, 28, 8), "round": (24, 24, 12), "slim": (10, 30, 5),
    "visor": (26, 14, 7), "square": (18, 22, 4),
}


def _q(rgb):
    """Quantise a colour so sprite caches and the redraw signature stay stable while colours glide."""
    return tuple(min(255, int(round(v / 6.0)) * 6) for v in rgb)


def _squircle_points(cx, cy, a, b, n=4.5, steps=200):
    """Superellipse outline (|x/a|^n + |y/b|^n = 1): a true squircle rather than a rounded rectangle."""
    pts = []
    for i in range(steps):
        t = 2 * math.pi * i / steps
        c, s = math.cos(t), math.sin(t)
        pts.append((cx + a * math.copysign(abs(c) ** (2.0 / n), c),
                    cy + b * math.copysign(abs(s) ** (2.0 / n), s)))
    return pts


def _fade(img, k: float):
    if k < 0.99:
        img.putalpha(img.getchannel("A").point(lambda v: int(v * k)))
    return img


def _mmss(secs: float) -> str:
    s = max(0, int(secs))
    return f"{s // 60:02d}:{s % 60:02d}"


class LayeredMishWindow(RobotFaceWindow):
    """RobotFaceWindow rendered through UpdateLayeredWindow, plus the top handle and Dynamic Island."""

    HANDLE, HANDLE_WIDE = (150, 22), (212, 28)       # notch: resting / hovered
    ISLAND_W, ISLAND_MIN_H, ISLAND_MAX_LINES = 368, 56, 6
    ISLAND_Y, DOCK_Y, DOCK_ZONE_Y, PULL_DY = 8, 18, 30, 14
    BODY_RECT, BODY_PAD = (48, 12, 192, 92), 4       # obsidian squircle inside the 240x130 window
    HIT_RECT = BODY_RECT                             # the opaque body is the hit area; the rest is click-through
    GEAR_RECT = (164, 62, 184, 82)                   # shown on hover only
    FACE_CENTER = (120, 44)
    TEXT_COL, ISLAND_FONT_PX, LINE_H = (248, 250, 252), 13, 18
    _eye_cache, _halo_cache, _status_cache, _gear_cache, _body_cache = {}, {}, {}, {}, {}
    _st = None
    _notch_wide = False

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
        self._last_sys_check = 0.0
        root.wm_attributes = self._wma_proxy         # Tk must never touch -alpha / -transparentcolor
        super().__init__(root, mish, ui_queue)

        self.canvas.pack_forget()                    # the old Tk-drawn widgets are replaced by the bitmap
        self.btn_gear.place_forget()
        for seq, fn in (("<ButtonPress-1>", self._ev_press), ("<B1-Motion>", self._ev_drag),
                        ("<ButtonRelease-1>", self._ev_release), ("<Button-3>", self._ev_right),
                        ("<Motion>", self._ev_motion), ("<Map>", lambda e: self._invalidate())):
            root.bind(seq, fn, add="+")
        self.apply_theme()
        self._invalidate()
        self._compose()
        self._apply()
        if self.settings.get("dev", 0) == 1:
            root.after(600, self.open_dev_console)

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

    # ------------------------------------------------------------------ cached sprites
    def _eye_sprite(self, spec, rgb):
        """One vector eye (3x supersampled, LANCZOS down) with a tight bloom. Cached by (spec, colour)."""
        key = (spec, rgb)
        spr = self._eye_cache.get(key)
        if spr is not None:
            return spr
        kind, w, hb, eh, rad, top, bot, slant, px, py, side = spec
        S, P = 3, EYE_PAD
        cw, chh = int(w + 2 * P), int(hb + 2 * P)
        W, H = cw * S, chh * S
        m = Image.new("L", (W, H), 0)
        d = ImageDraw.Draw(m)
        cx, cy, hw = W / 2.0, H / 2.0, w * S / 2.0
        if kind == "dome":                                   # eh = dome height; flat bottom, round top
            dh = eh * S
            bottom = cy + 6 * S
            d.rounded_rectangle((cx - hw, bottom - dh, cx + hw - 1, bottom + dh - 1),
                                radius=min(rad * S, hw, dh), fill=255)
            d.rectangle((0, bottom, W, H), fill=0)
        else:
            hh = eh * S / 2.0
            box = (cx - hw, cy - hh, cx + hw - 1, cy + hh - 1)
            r = min(rad * S, hw, hh)
            d.rounded_rectangle(box, radius=r, fill=255)
            if kind == "ring":
                t = 3 * S
                d.rounded_rectangle((box[0] + t, box[1] + t, box[2] - t, box[3] - t),
                                    radius=max(0.0, r - t), fill=0)
                pr = 3.5 * S
                qx, qy = cx + px * S, cy + py * S
                d.ellipse((qx - pr, qy - pr, qx + pr, qy + pr), fill=255)
            if top > 0 or slant:                             # eyelid: erase above a (possibly slanted) line
                yo = cy - hh + top * S
                yl, yr = (yo - slant * S, yo + slant * S) if side == "l" else (yo + slant * S, yo - slant * S)
                d.polygon([(0, 0), (W, 0), (W, yr), (0, yl)], fill=0)
            if bot > 0:
                d.rectangle((0, cy + hh - bot * S, W, H), fill=0)
        m = m.resize((cw, chh), Image.Resampling.LANCZOS)
        glow = Image.new("RGBA", (cw, chh), rgb + (255,))
        glow.putalpha(m.filter(ImageFilter.GaussianBlur(3)).point(lambda v: int(v * 0.5)))
        eye = Image.new("RGBA", (cw, chh), rgb + (255,))
        eye.putalpha(m)
        spr = Image.alpha_composite(glow, eye)
        if len(self._eye_cache) > 400:
            self._eye_cache.clear()
        self._eye_cache[key] = spr
        return spr

    def _halo(self, rgb):
        hl = self._halo_cache.get(rgb)
        if hl is None:
            g = ImageChops.invert(Image.radial_gradient("L")).resize((HALO_W, HALO_H), Image.Resampling.BILINEAR)
            g = g.point(lambda v: int(v * v / 255 * 0.16))
            hl = Image.new("RGBA", g.size, rgb + (255,))
            hl.putalpha(g)
            if len(self._halo_cache) > 200:
                self._halo_cache.clear()
            self._halo_cache[rgb] = hl
        return hl

    def _body_sprite(self, breath):
        """Obsidian squircle for one breathing step: fill, 1px white rim at 18%, top specular.
        Returns (RGBA sprite, L mask), both BODY-sized + padding. Cached per breath level (7 entries)."""
        hit = self._body_cache.get(breath)
        if hit is not None:
            return hit
        S, pad = _SS, self.BODY_PAD
        x0, y0, x1, y1 = self.BODY_RECT
        bw, bh = x1 - x0 + 2 * pad, y1 - y0 + 2 * pad
        W, H = bw * S, bh * S
        k = 1.0 + breath * 0.005                                     # +-1.5% breathing scale
        a, b = (x1 - x0) / 2.0 * S * k, (y1 - y0) / 2.0 * S * k
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).polygon(_squircle_points(W / 2.0, H / 2.0, a, b), fill=255)
        inner = mask.filter(ImageFilter.MinFilter(2 * S + 1))        # eroded by 1 logical px
        body = Image.new("RGBA", (W, H), NOTCH_BG + (255,))
        body.putalpha(mask)
        rim = Image.new("RGBA", (W, H), (255, 255, 255, 255))
        rim.putalpha(ImageChops.subtract(mask, inner).point(lambda v: int(v * 0.18)))
        grad = ImageChops.invert(Image.linear_gradient("L")).resize((W, 18 * S), Image.Resampling.BILINEAR)
        spec = Image.new("L", (W, H), 0)
        spec.paste(grad, (0, int(H / 2.0 - b)))
        spec = ImageChops.multiply(spec, inner).filter(ImageFilter.GaussianBlur(S))
        spec = ImageChops.multiply(spec, mask).point(lambda v: int(v * 0.16))
        hl = Image.new("RGBA", (W, H), (255, 255, 255, 255))
        hl.putalpha(spec)
        out = Image.alpha_composite(Image.alpha_composite(body, rim), hl)
        res = (out.resize((bw, bh), Image.Resampling.LANCZOS), mask.resize((bw, bh), Image.Resampling.LANCZOS))
        if len(self._body_cache) > 16:
            self._body_cache.clear()
        self._body_cache[breath] = res
        return res

    def _gear_sprite(self, rgb):
        spr = self._gear_cache.get(rgb)
        if spr is None:
            S, N = _SS, 20
            lay = Image.new("RGBA", (N * S, N * S), (0, 0, 0, 0))
            d = ImageDraw.Draw(lay)
            c, col = N * S / 2.0, rgb + (200,)
            d.polygon(_gear_points(c, c, 9 * S, 6.6 * S), fill=col)
            d.ellipse((c - 6.8 * S, c - 6.8 * S, c + 6.8 * S, c + 6.8 * S), fill=col)
            d.ellipse((c - 2.6 * S, c - 2.6 * S, c + 2.6 * S, c + 2.6 * S), fill=(0, 0, 0, 0))
            spr = self._gear_cache[rgb] = lay.resize((N, N), Image.Resampling.LANCZOS)
        return spr

    def _status_sprite(self, text, rgb):
        """Crisp status dot + clean text on the always-dark squircle body (no glow/shadow)."""
        key = (text, rgb)
        spr = self._status_cache.get(key)
        if spr is not None:
            return spr
        S, px = _SS, 11
        f = _font(px)
        tw = _measure(text, px)
        cw, chh = int(tw + 12 + 28), 24
        x0, cy = (cw - (12 + tw)) / 2.0, chh / 2.0
        dc, tx = x0 + 3, x0 + 12
        lay = Image.new("RGBA", (cw * S, chh * S), (0, 0, 0, 0))
        d = ImageDraw.Draw(lay)
        dot = ((dc - 3) * S, (cy - 3) * S, (dc + 3) * S, (cy + 3) * S)
        d.ellipse(dot, fill=rgb + (255,))
        fill = (226, 232, 240, 255)  # crisp light slate (#E2E8F0); change to (255, 255, 255, 255) for pure white
        try:
            d.text((tx * S, cy * S), text, font=f, fill=fill, anchor="lm")
        except Exception:
            d.text((tx * S, (cy - 7) * S), text, font=f, fill=fill)
        spr = lay.resize((cw, chh), Image.Resampling.LANCZOS)
        if len(self._status_cache) > 64:
            self._status_cache.clear()
        self._status_cache[key] = spr
        return spr

    def _mini_eyes(self, lay, S, x, cy, ew, eh, gap, rgb, blink):
        """Two tiny glowing pill eyes for the notch / island (bloom blurred in a tight crop only)."""
        eh = max(2.0, eh * (1.0 - 0.85 * blink))
        boxes = [((x + i * (ew + gap)) * S, (cy - eh / 2) * S, (x + i * (ew + gap) + ew) * S - 1, (cy + eh / 2) * S - 1)
                 for i in range(2)]
        rad = min(ew, eh) * S / 2.0
        pad = 8 * S
        x0c, y0c = max(0, int(boxes[0][0] - pad)), max(0, int(boxes[0][1] - pad))
        x1c, y1c = min(lay.width, int(boxes[1][2] + pad)), min(lay.height, int(boxes[0][3] + pad))
        if x1c > x0c and y1c > y0c:
            gl = Image.new("RGBA", (x1c - x0c, y1c - y0c), (0, 0, 0, 0))
            gd = ImageDraw.Draw(gl)
            for b in boxes:
                gd.rounded_rectangle((b[0] - x0c, b[1] - y0c, b[2] - x0c, b[3] - y0c), radius=rad, fill=rgb + (255,))
            gl = gl.filter(ImageFilter.GaussianBlur(2.6 * S))
            gl.putalpha(gl.getchannel("A").point(lambda v: int(v * 0.7)))
            lay.alpha_composite(gl, (x0c, y0c))
        d = ImageDraw.Draw(lay)
        for b in boxes:
            d.rounded_rectangle(b, radius=rad, fill=rgb + (255,))

    # ------------------------------------------------------------------ visual state (cheap; drives the redraw signature)
    def _status(self):
        m, now = self.mish, time.time()
        if m.strike_mode:
            return "strike", (244, 63, 94)
        if self.is_sleeping:
            return "sleeping", (148, 163, 184)
        if self.is_scared or self.is_dizzy:
            return "alert", (251, 191, 36)
        if m.thinking_visible:
            return "thinking", (255, 255, 255)
        if self.is_listening:
            return "listening", (255, 255, 255)
        if m.focus_paused_rem > 0:
            return f"paused {_mmss(m.focus_paused_rem)}", (56, 189, 248)
        if m.in_focus():
            return f"focus {_mmss(m.focus_until - now)}", (52, 211, 153)
        if m.in_break():
            return f"break {_mmss(m.focus_until - now)}", (56, 189, 248)
        return "idle", (255, 255, 255)

    def _in_dock_band(self, wx: float) -> bool:
        l, _, r, _ = self._monitor_rect()
        return abs((wx + self.FACE_CENTER[0]) - (l + r) / 2.0) <= 180

    def _hover(self, pointer) -> bool:
        if not pointer:
            return False
        x, y = self._win_xy()
        px, py = pointer[0] - x, pointer[1] - y
        if self._mode == "float":
            x0, y0, x1, y1 = self.HIT_RECT
            return x0 <= px <= x1 and y0 <= py <= y1
        return -2 <= px <= self._g["w"] + 2 and -2 <= py <= self._g["h"] + 2

    def _visual_state(self, now, pointer):
        e, p = self.eyes, self.eyes.p
        ex = EXPRESSIONS[e.active]
        w, hb, rad = SHAPE_DIMS.get(self.settings["shape"], SHAPE_DIMS["pill"])
        sc, blink, hs = hb / 12.0, e.blink_amt, p["h"]
        pulse = 1 if (now < e.talk_until and math.sin(now * 16.0) > 0.4) else 0
        gx = clamp(e.gaze[0] + p["gx"] + e.inertia[0], -4, 4)
        gy = clamp(e.gaze[1] + p["gy"] + e.inertia[1], -3, 3)
        jx = jy = 0
        if ex.get("jitter"):
            jx, jy = random.choice((-1, 0, 1)), random.choice((-1, 0, 1))
        elif ex.get("shake"):
            jy = random.choice((-1, 0, 1))
        ring = p["ring"] > 0.5
        gox, goy = (0, 0) if ring else (int(round(gx * 1.6)), int(round(gy * 1.5)))
        eyes = []
        for side, sgn in (("l", -1), ("r", 1)):
            cx = int(round(120 + sgn * (EYE_GAP / 2.0 + w / 2.0) + gox + jx))
            cy = EYE_CY + goy + jy
            scale = (0.62 if side == "l" else 1.05) if e.active == "thinking" else 1.0
            eh = max(4, int(round(hb * scale * hs)))
            if p["arc_" + side] > 0.5:                                   # happy / laugh / love: dome
                dh = 1.5 if blink > 0.5 else (6 + p["amp"] * 1.5) * hb / 28.0
                spec = ("dome", w, hb, round(dh * 2) / 2, rad, 0, 0, 0, 0, 0, side)
                cy += 2 * pulse
            else:
                half = eh / 2.0 - 1.2
                top, bot = p["top_" + side] * sc, p["bot_" + side] * sc + (3 * pulse if blink < 0.3 else 0)
                top = min(half, max(top, top + (half - top) * blink))
                bot = min(half, max(bot, bot + (half - bot) * blink))
                slant = p["sl_" + side] * sc * (1 - blink)
                pxo = pyo = 0
                if ring:
                    mx, my = max(0.0, w / 2.0 - 6.5), max(0.0, eh / 2.0 - 6.5)
                    if ex.get("orbit"):
                        a = e.orbit * (1 if side == "l" else -1)
                        pxo, pyo = math.cos(a) * mx, math.sin(a) * my
                    elif ex.get("jitter"):
                        pxo, pyo = random.uniform(-1, 1) * mx, random.uniform(-1, 1) * my
                    else:
                        pxo, pyo = clamp(gx * 0.5, -mx, mx), clamp(gy * 0.8, -my, my)
                spec = ("ring" if ring else "cap", w, hb, eh, rad, round(top * 2) / 2, round(bot * 2) / 2,
                        round(slant * 2) / 2, int(round(pxo)), int(round(pyo)), side)
            eyes.append((spec, cx, cy))
        rgb = _q(e.color)
        status = self._status()
        st = dict(
            eyes=tuple(eyes), rgb=rgb, status=status, hover=self._hover(pointer),
            emote=(e.emote, 1 if int(now * 5) % 2 else 0) if (e.emote and now < e.emote_until) else None,
            zzz=int(round(math.sin(now * 2.2) * 2)) if self.is_sleeping else None,
            blink=round(blink, 1),
            breath=int(round(math.sin(now * (0.7 if self.is_sleeping else 1.15)) * 3)),   # -3..3 -> +-1.5%
        )
        st["mini"] = (rgb, st["blink"], status, st["hover"])
        return st

    def render_16bit_eyes(self):
        now = time.time()
        if self.settings["theme"] == "auto" and now - self._last_sys_check > 2.5:
            self._last_sys_check = now
            if get_theme("auto")["name"] != self.theme["name"]:
                self.apply_theme()
        try:
            pointer = self.root.winfo_pointerxy()
        except tk.TclError:
            pointer = None
        wx, wy = self._win_xy()
        inertia = (0.0, 0.0) if self.is_sleeping else (
            clamp(-self.vel_x * 0.12, -4, 4), clamp(-self.vel_y * 0.12, -3, 3))
        self.eyes.update(now, {
            "strike": self.mish.strike_mode, "sleeping": self.is_sleeping, "scared": self.is_scared,
            "dizzy": self.is_dizzy, "held": self.is_held, "thinking": self.mish.thinking_visible,
            "listening": self.is_listening, "focus": self.mish.in_focus(), "idle_t": self.idle_t,
            "annoyance": self.mish.annoyance,
            "pointer": pointer, "face": (wx + self.FACE_CENTER[0], wy + self.FACE_CENTER[1]), "inertia": inertia,
        })
        st = self._visual_state(now, pointer)
        self._st = st
        if self._mode == "handle" and not self._anim_on and st["hover"] != self._notch_wide:
            self._notch_wide = st["hover"]
            self._morph("handle", 0.22)
        self.draw_cells(st)

    def draw_cells(self, st):
        """`st` is the visual-state dict. Composes + uploads only when its signature changed."""
        self._st = st
        key = (self._mode, st["mini"]) if self.docked() else (self._mode, tuple(st.values()))
        if key == self._last_cells and not self._dirty:
            return
        self._last_cells, self._dirty = key, False
        if self._anim_on:
            return
        self._compose()
        self._apply()

    # ------------------------------------------------------------------ layers (each returns a w x h RGBA)
    def _draw_body(self, w, h):
        st = self._st
        lay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        if not st:
            return lay
        S = _SS
        ox, oy = (w - self.SIZE[0]) // 2, (h - self.SIZE[1]) // 2
        rgb = st["rgb"]

        def put(layer, img, x, y):
            tmp = Image.new("RGBA", layer.size, (0, 0, 0, 0))
            tmp.paste(img, (int(x), int(y)))
            return Image.alpha_composite(layer, tmp)

        body, bmask = self._body_sprite(st["breath"])
        bx, by = ox + self.BODY_RECT[0] - self.BODY_PAD, oy + self.BODY_RECT[1] - self.BODY_PAD
        lay = put(lay, body, bx, by)

        el = Image.new("RGBA", (w, h), (0, 0, 0, 0))                     # eyes + bloom + emote, clipped to the body
        el = put(el, self._halo(rgb), ox + 120 - HALO_W // 2, oy + EYE_CY - HALO_H // 2)
        for spec, cx, cy in st["eyes"]:
            spr = self._eye_sprite(spec, rgb)
            el = put(el, spr, ox + cx - spr.width // 2, oy + cy - spr.height // 2)
        if st["emote"]:
            name, bounce = st["emote"]
            spec, ecx, ecy = st["eyes"][1]
            ed = ImageDraw.Draw(el)
            gcol = ((255, 122, 200) if name == "heart" else rgb) + (255,)
            gx0, gy0 = ox + ecx + spec[1] // 2 + 10, oy + ecy - spec[2] // 2 - 4 + bounce * 2
            for r, row in enumerate(GLYPHS[name]):
                for c, ch in enumerate(row):
                    if ch == "#":
                        ed.rectangle((gx0 + c * 3, gy0 + r * 3, gx0 + c * 3 + 2, gy0 + r * 3 + 2), fill=gcol)
        clip = Image.new("L", (w, h), 0)
        clip.paste(bmask, (bx, by))
        el.putalpha(ImageChops.multiply(el.getchannel("A"), clip))
        lay = Image.alpha_composite(lay, el)

        if st["zzz"] is not None:                                         # floats outside the body, top right
            bob = st["zzz"]
            zl = Image.new("RGBA", (44 * S, 36 * S), (0, 0, 0, 0))
            zd = ImageDraw.Draw(zl)
            zd.text((2 * S, (14 + bob) * S), "Z", font=_font(16), fill=(148, 163, 184, 255))
            zd.text((22 * S, (2 + bob) * S), "z", font=_font(11), fill=(148, 163, 184, 255))
            lay = put(lay, zl.resize((44, 36), Image.Resampling.LANCZOS), ox + 172, oy + 2)
        text, scol = st["status"]
        spr = self._status_sprite(text, scol)
        lay = put(lay, spr, ox + 120 - spr.width // 2, oy + 73 - spr.height // 2)
        if st["hover"] and text != "sleeping":
            lay = put(lay, self._gear_sprite(rgb), ox + self.GEAR_RECT[0], oy + self.GEAR_RECT[1])
        return lay

    def _draw_handle(self, w, h):
        S = _SS
        lay = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
        st = self._st
        if st:
            rgb = st["rgb"]
            text, scol = st["status"]
            self._mini_eyes(lay, S, 16, h / 2.0, 5, 10, 5, rgb, st["blink"])
            d = ImageDraw.Draw(lay)
            try:
               d.text((w / 2.0 * S, h / 2.0 * S), text, font=_font(11), fill=(226, 232, 240, 255), anchor="mm")
            except Exception:
                pass
            dx, dy = w - 16, h / 2.0
            glow = Image.new("RGBA", lay.size, (0, 0, 0, 0))
            ImageDraw.Draw(glow).ellipse(((dx - 3) * S, (dy - 3) * S, (dx + 3) * S, (dy + 3) * S), fill=scol + (255,))
            glow = glow.filter(ImageFilter.GaussianBlur(3 * S))
            glow.putalpha(glow.getchannel("A").point(lambda v: min(255, v * 2)))
            lay = Image.alpha_composite(lay, glow)
            ImageDraw.Draw(lay).ellipse(((dx - 3) * S, (dy - 3) * S, (dx + 3) * S, (dy + 3) * S), fill=scol + (255,))
        return lay.resize((w, h), Image.Resampling.LANCZOS)

    def _draw_island(self, w, h):
        S = _SS
        lay = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
        isl, st = self._isl, self._st
        if isl:
            ox, oy = (w - isl["w"]) / 2.0, (h - isl["h"]) / 2.0
            rgb, blink = (st["rgb"], st["blink"]) if st else ((255, 255, 255), 0.0)
            self._mini_eyes(lay, S, ox + 16, oy + isl["h"] / 2.0, 6, 12, 6, rgb, blink)
            d = ImageDraw.Draw(lay)
            sx = (ox + 44) * S
            d.rounded_rectangle((sx, (oy + 14) * S, sx + 1.5 * S, (oy + isl["h"] - 14) * S), radius=S / 2.0,
                                fill=rgb + (110,))
            tx = ox + isl["tx"] 
            ty = oy + (isl["h"] - len(isl["lines"]) * self.LINE_H) / 2.0
            px = self.ISLAND_FONT_PX
            for i, line in enumerate(isl["lines"]):
                x, y = tx, ty + i * self.LINE_H
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
        return lay.resize((w, h), Image.Resampling.LANCZOS)

    def _shell(self, w, h):
        """Notch / island body: black rounded shell + translucent hairline. Returns (shell RGBA, clip mask L)."""
        g, S = self._g, _SS
        W, H = w * S, h * S
        top = -g["flat"] * 2 * g["r"] * S                           # flat top = rounding pushed off the screen edge
        rad = max(0.0, min(g["r"] * S, (W - 1) / 2.0, (H - 1 - top) / 2.0))
        box = (0, top, W - 1, H - 1)
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).rounded_rectangle(box, radius=rad, fill=255)
        shell = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(shell).rounded_rectangle(box, radius=rad, fill=NOTCH_BG + (255,),
                                                outline=(255, 255, 255, 46), width=S)
        return (shell.resize((w, h), Image.Resampling.LANCZOS), mask.resize((w, h), Image.Resampling.LANCZOS))

    def _compose(self):
        g, wt = self._g, self._wt
        w, h = max(1, int(round(g["w"]))), max(1, int(round(g["h"])))
        out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        if wt["body"] > 0.01:                                        # floating: no box at all
            out = Image.alpha_composite(out, _fade(self._draw_body(w, h), wt["body"]))
        sw = 1.0 - wt["body"]
        if sw > 0.01:
            shell, mask = self._shell(w, h)
            dock = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            for name, draw in (("island", self._draw_island), ("handle", self._draw_handle)):
                if wt[name] > 0.01:
                    dock = Image.alpha_composite(dock, _fade(draw(w, h), wt[name]))
            dock.putalpha(ImageChops.multiply(dock.getchannel("A"), mask))
            out = Image.alpha_composite(out, _fade(Image.alpha_composite(shell, dock), sw))
        self._sf.attach(self.root)
        self._sf.upload(out)
        self._frame_ready = True

    # ------------------------------------------------------------------ placement: window + surface always move together
    def _place(self, x: float, y: float, alpha: float | None = None):
        x, y = int(x), int(y)
        self.root.geometry(f"+{x}+{y}")
        if alpha is not None:
            self._alpha = clamp(alpha, 0.0, 1.0)
        self._apply(x, y)

    def _base_alpha(self) -> float:
        return 0.35 if self.is_sleeping else self.settings["opacity"]

    def _sleep_wake(self, now: float):
        idle_t = self.idle_t = idle_seconds()
        if idle_t >= AUTO_SLEEP_TIMEOUT and not self.is_sleeping and not self.mish.strike_mode:
            self.is_sleeping = True
            self._wma_proxy("-alpha", 0.35)
        elif idle_t < AUTO_SLEEP_TIMEOUT and self.is_sleeping:
            self.is_sleeping = False
            self._wma_proxy("-alpha", self.settings["opacity"])
            self.eyes.set_expression("surprised", 1.2)
            self.mish.react_async("wake")

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
            self._place(nx, ny)
            self._near_top = ny <= self._monitor_rect()[1] + self.DOCK_ZONE_Y and self._in_dock_band(nx)
            if self.active_bubble and self.active_bubble.winfo_exists():
                self.active_bubble.update_position()
        elif event.y_root - p["y"] >= self.PULL_DY and not self._pulled:
            self._pulled = True                                       # pulled down: pop back out
            self.pop_out()

    # ------------------------------------------------------------------ geometry / island / timers
    def _target(self, mode: str) -> dict:
        l, t, r, _b = self._monitor_rect()
        cx = (l + r) // 2
        if mode == "handle":
            w, h = self.HANDLE_WIDE if self._notch_wide else self.HANDLE
            return dict(x=cx - w // 2, y=t, w=float(w), h=float(h), r=h / 2, flat=1.0)
        if mode == "island":
            iw, ih = (self._isl["w"], self._isl["h"]) if self._isl else (self.ISLAND_W, self.ISLAND_MIN_H)
            return dict(x=cx - iw // 2, y=t + self.ISLAND_Y, w=float(iw), h=float(ih), r=min(36.0, ih / 2), flat=0.0)
        fx, fy = self._float_target
        return dict(x=fx, y=fy, w=float(self.SIZE[0]), h=float(self.SIZE[1]), r=float(self.RADIUS), flat=0.0)

    def _layout_island(self, text: str) -> dict:
        tx = 58                                                       # mini eyes + divider + gap
        lines = _wrap(text, self.ISLAND_FONT_PX, self.ISLAND_W - tx - 20, self.ISLAND_MAX_LINES)
        return dict(text=text, lines=lines, tx=tx, w=self.ISLAND_W,
                    h=max(self.ISLAND_MIN_H, len(lines) * self.LINE_H + 26))

    def show_timer_bubble(self, minutes: int, seconds: int, phase: str = "work"):
        """No bubble any more: the micro-status line / notch reads the countdown from mish state."""

    def clear_timer_bubble(self):
        """Nothing to clear: the status line falls back to 'idle' by itself."""

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


    # ------------------------------------------------------------------ rendering (Pillow, 4x supersampled)
    def _invalidate(self):
        self._dirty = True
        self._last_cells = None

    def _rgb(self, h: str):
        return _hex_rgb(h)



    def _apply(self, x=None, y=None):
        if not self._frame_ready:
            self._compose()
        return self._sf.present(x, y, self._alpha)

    # ------------------------------------------------------------------ overrides of the old canvas drawing
    def draw_squircle_background(self):
        self._invalidate()

    def apply_theme(self):
        super().apply_theme()
        dm = getattr(self, "dock_menu", None)
        if dm is not None:
            dm.configure(bg=self.panel, fg=self.eye_col, activebackground=self.eye_col, activeforeground=self.bg)
        self._invalidate()


    def _weights(self, mode: str) -> dict:
        return {
            "body": 1.0 if mode == "float" else 0.0,
            "island": 1.0 if mode == "island" else 0.0,
            "handle": 1.0 if mode == "handle" else 0.0,}

    # ------------------------------------------------------------------ cancellable morph animation
    def _morph(self, mode: str, seconds: float, done=None, spring: bool = False):
        """Animate the layered surface and commit Tk geometry at the end."""
        src_mode, src_g, src_wt = self._mode, dict(self._g), dict(self._wt)
        sx, sy = self._win_xy()
        dst, dst_wt = self._target(mode), self._weights(mode)
        self._anim_tok += 1
        tok = self._anim_tok
        self._mode = mode
        self._anim_on = True
        t0 = time.perf_counter()

        def step():
            if tok != self._anim_tok:
                return
            t = min(1.0, (time.perf_counter() - t0) / seconds) if seconds > 0 else 1.0
            e = ease_out_back(t, 0.7) if spring else smoothstep(t)
            f = smoothstep(t)
            self._g = {k: src_g[k] + (dst[k] - src_g[k]) * e for k in ("w", "h", "r", "flat")}
            self._g["w"], self._g["h"] = max(1.0, self._g["w"]), max(1.0, self._g["h"])
            self._wt = {k: src_wt[k] + (dst_wt[k] - src_wt[k]) * f for k in dst_wt}
            x = int(round(sx + (dst["x"] - sx) * e))
            y = int(round(sy + (dst["y"] - sy) * e))
            try:
                if t < 1.0:
                    self._compose()
                    self._apply(x, y)
                    self.root.after(12, step)
                    return

                w, h = int(round(self._g["w"])), int(round(self._g["h"]))
                expected = (x, y, x + w, y + h)
                self.root.geometry(f"{w}x{h}")
                self.root.update_idletasks()
                if tok != self._anim_tok:
                    return
                self._compose()
                for _ in range(2):
                    if self._apply(x, y) and self._sf.bounds() == expected:
                        break
                else:
                    log("morph presentation failed:", mode, expected, self._sf.bounds())
                    self._mode, self._g, self._wt = src_mode, src_g, src_wt
                    self._anim_on = False
                    self._invalidate()
                    self.root.geometry(f"{round(src_g['w'])}x{round(src_g['h'])}")
                    self.root.update_idletasks()
                    if tok == self._anim_tok:
                        self._compose()
                        self._apply(sx, sy)
                    return
            except tk.TclError:
                if tok == self._anim_tok:
                    self._mode, self._g, self._wt = src_mode, src_g, src_wt
                    self._anim_on = False
                    self._invalidate()
                return
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
        top_zone = self.docked() or self._anim_on or (self.is_held and cur_y <= t + self.DOCK_ZONE_Y and self._in_dock_band(cur_x))

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
                self.mish.react_async("scared")
            elif now - self.edge_hold_start > 6.0:
                self.trigger_strike_mode()
        else:
            self.is_scared = False
            self.edge_hold_start = 0.0
        if self.mish.strike_mode and now > self.strike_until:
            self.mish.strike_mode = False
            self.mish.react_async("strike_end")



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
    # ------------------------------------------------------------------ Ghost command bar (Alt+M / Ask Mish)
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
            self.mish.react_async("strike_chat", extra="they tried to open the chat box")
            return
        th = self.theme
        c = ui_colors(th, self.eye_col)
        BG, MUTED, accent = c["bg"], c["muted"], c["accent"]
        W, H = 316, 42
        l, t, r, b = self._monitor_rect()
        wx, wy = self._win_xy()
        floating = self._mode == "float"
        cx = wx + self.root.winfo_width() // 2
        py = wy + (98 if floating else self.root.winfo_height() + 6)       # just under the body / notch
        if py + H > b - 8:
            py = max(t + 8, wy + (10 if floating else 0) - H - 6)
        px = int(clamp(cx - W // 2, l + 8, max(l + 8, r - W - 8)))

        dlg = self._chat_dlg = tk.Toplevel(self.root)
        dlg.withdraw()
        dlg.overrideredirect(True)
        dlg.attributes("-topmost", True)
        dlg.attributes("-alpha", 0.96)
        dlg.geometry(f"{W}x{H}+{px}+{int(py)}")
        shell = RoundedShell(dlg, th, radius=14, inset=3)                 # 14px chrome; hairline lights up on typing
        shell.pack(fill="both", expand=True)
        row = tk.Frame(shell.body, bg=BG)
        row.pack(fill="both", expand=True, padx=(10, 8))
        tk.Label(row, text=">", bg=BG, fg=accent, font=("Consolas", 11, "bold")).pack(side="left")
        var = tk.StringVar(master=dlg)
        tasks = tk.Label(row, text="tasks", bg=BG, fg=MUTED, cursor="hand2", font=("Segoe UI", 9))
        tasks.pack(side="right", padx=(8, 0))
        entry = tk.Entry(row, textvariable=var, bg=BG, fg=c["text_hi"], insertbackground=accent, relief="flat",
                         bd=0, highlightthickness=0, font=("Segoe UI", 10))
        entry.pack(side="left", fill="x", expand=True, padx=(8, 0), ipady=3)
        ph = tk.Label(row, text="ask mish...", bg=BG, fg=MUTED, font=("Segoe UI", 10))
        ph.place(in_=entry, x=0, rely=0.5, anchor="w")
        ph.bind("<Button-1>", lambda e: entry.focus_force())

        def close(_e=None):
            try:
                dlg.destroy()
            except tk.TclError:
                pass

        def on_text(*_):
            if var.get():
                ph.place_forget()
                shell.set_line(accent)                                     # hairline lights up while typing
            else:
                ph.place(in_=entry, x=0, rely=0.5, anchor="w")
                shell.set_line(None)

        def send(_e=None):
            txt = var.get().strip()
            close()
            if txt:
                threading.Thread(target=self.mish.handle_user_input, args=(txt,), daemon=True).start()

        def open_tasks(_e=None):
            close()
            self.open_tasks()

        var.trace_add("write", on_text)
        tasks.bind("<Button-1>", open_tasks)
        tasks.bind("<Enter>", lambda e: tasks.configure(fg=accent))
        tasks.bind("<Leave>", lambda e: tasks.configure(fg=MUTED))
        entry.bind("<Return>", send)
        entry.bind("<Escape>", close)
        entry.bind("<FocusOut>", close)
        self.is_listening = True                                           # eyes look down while it is open
        dlg.bind("<Destroy>", lambda e: setattr(self, "is_listening", False) if e.widget is dlg else None)
        dlg.deiconify()
        dlg.update_idletasks()
        entry.focus_force()

    # ------------------------------------------------------------------ Ghost context menu
    def open_context_menu(self, x, y):
        old = getattr(self, "_ctx", None)
        try:
            if old is not None and old.winfo_exists():
                old.destroy()
        except tk.TclError:
            pass
        d = self.docked()
        items = [
            ("Ask Mish", "Alt+M", self.open_chat_dialog, False),
            ("Look at Screen", "", lambda: self.mish.observe_async(force=True), False),
            ("Active Tasks", "", self.open_tasks, False),
            ("Connections...", "", self.open_connections, False),
            ("Pop Out" if d else "Tuck to Top Notch", "", self.pop_out if d else self.tuck_to_top, False),
            ("Session Stats", "", lambda: self.show_speech_bubble(self.mish.stats_text()), False),
            ("Customize HUD...", "", self.open_customize, False),
        ]
        if self.settings.get("dev", 0) == 1:
            items.append(("Dev Console", "", self.open_dev_console, False))
        items += [None, ("Exit Mish", "", self.root.destroy, True)]
        self._ctx = GhostMenu(self.root, items, int(x), int(y), accent=self.eye_col,
                              bounds=self._monitor_rect(), theme=self.theme)

    def show_menu(self):
        x, y = self._win_xy()
        self.open_context_menu(x + self.GEAR_RECT[0], y + self.GEAR_RECT[3])

    def _ev_right(self, e):
        if e.widget is not self.root or self.errand:
            return
        self.open_context_menu(e.x_root, e.y_root)

    def on_press(self, event):
        if self.errand or self._anim_on:
            self._press = None
            return
        wx, wy = self._win_xy()
        self._press = dict(x=event.x_root, y=event.y_root, wx=wx, wy=wy, gear=self._in_gear(event.x, event.y))
        self.press_root = (event.x_root, event.y_root)
        self.moved = False
        self._pulled = False



    def on_release(self, event):
        p, self._press = self._press, None
        if self.errand or not p or self._anim_on:
            return
        if self._mode == "float":
            self._near_top = False
            if self.is_held:
                self.is_held = False
                if self._win_xy()[1] <= self._monitor_rect()[1] + self.DOCK_Y and self._in_dock_band(self._win_xy()[0]):
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
    S, N = 4, 64
    img = Image.new("RGBA", (N * S, N * S), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((2 * S, 2 * S, (N - 2) * S, (N - 2) * S), fill=(7, 10, 17, 255),
                                outline=(30, 41, 59, 255), width=2 * S)
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for cx in (23, 41):
        gd.rounded_rectangle(((cx - 4.5) * S, 21 * S, (cx + 4.5) * S, 43 * S), radius=4.5 * S, fill=(255, 255, 255, 255))
    glow = glow.filter(ImageFilter.GaussianBlur(3 * S))
    glow.putalpha(glow.getchannel("A").point(lambda v: int(v * 0.7)))
    img = Image.alpha_composite(img, glow)
    d = ImageDraw.Draw(img)
    for cx in (23, 41):
        d.rounded_rectangle(((cx - 4.5) * S, 21 * S, (cx + 4.5) * S, 43 * S), radius=4.5 * S, fill=(255, 255, 255, 255))
    return img.resize((N, N), Image.Resampling.LANCZOS)


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
            "Place one quantized .gguf model here, such as reiHien/MishV6.\n"
            "Auto loads it for chat, file analysis, or clipboard summaries.\n"
            "Use !brain on to keep it loaded, !brain off to unload it, or !brain auto to restore on-demand loading.\n"
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

    timer_resolution = False
    try:
        try:
            timer_resolution = ctypes.windll.winmm.timeBeginPeriod(1) == 0
        except OSError as error:
            log("Windows timer resolution unavailable:", repr(error))
        _run_app()
    finally:
        if timer_resolution:
            ctypes.windll.winmm.timeEndPeriod(1)


def _run_app():
    ensure_portable_setup(user_data_dir())
    ui_queue: queue.Queue = queue.Queue()
    mish = Mish(ui_queue)
    icon = None


    def quit_app(icon=None, _item=None):
        mish.shutdown()
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
        ui_queue.put(("quit",))

    try:
        root = tk.Tk()
        LayeredMishWindow(root, mish, ui_queue)
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

        root.mainloop()
    finally:
        quit_app(icon)
