"""Profile-aware eye shape and idle posture."""

from __future__ import annotations

import math
import random
import re
import time

from .config import BORED_AFTER, DROWSY_AFTER, MOOD_FED_UP_AT, MOOD_IRRITATED_AT
from .win_utils import clamp

# ---- base eye animation and expression geometry ----
C_CYAN, C_AMBER, C_RED, C_ORANGE = "#00e6ff", "#ffd166", "#ff3355", "#ff6600"


C_SLEEP, C_OFF, C_BLUE, C_PURPLE = "#6688aa", "#3a3a48", "#5f8bff", "#c77dff"
C_DIM, C_GREEN, C_PINK = "#3fa3b3", "#5dffb0", "#ff7ac8"


def _hex_rgb(h: str):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def _rgb_hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(clamp(round(v), 0, 255)) for v in rgb)


def _mix(a, b, t: float):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


# ---- eye shapes: every mask fits the same 10x12 box, so all expressions work with all shapes ----
SHAPE_MASKS = {
    "round": ["..######..", ".########.", "##########", "##########", "##########", "##########",
              "##########", "##########", "##########", "##########", ".########.", "..######.."],
    "square": ["##########"] * 12,
    "pill": ["...####...", "..######..", ".########.", ".########.", "##########", "##########",
             "##########", "##########", ".########.", ".########.", "..######..", "...####..."],
    "slim": ["...####...", "..######..", "..######..", "..######..", "..######..", "..######..",
             "..######..", "..######..", "..######..", "..######..", "..######..", "...####..."],
    "visor": ["..........", "..........", "..######..", ".########.", "##########", "##########",
              "##########", "##########", ".########.", "..######..", "..........", ".........."],
}


class Shape:
    def __init__(self, mask):
        self.cells = [(c, r) for r, row in enumerate(mask) for c, ch in enumerate(row) if ch == "#"]
        on = set(self.cells)
        self.border = {(c, r) for (c, r) in self.cells
                       if not all((c + dc, r + dr) in on for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1)))}
        self.cmin, self.cmax = min(c for c, _ in self.cells), max(c for c, _ in self.cells)
        self.rmin, self.rmax = min(r for _, r in self.cells), max(r for _, r in self.cells)
        self.w, self.h = self.cmax - self.cmin + 1, self.rmax - self.rmin + 1
        self.scale = self.h / 12.0           # eyelid amounts are written for a 12-row eye
        self.half = self.h / 2.0 - 0.5       # lid position where the eye is fully shut


SHAPES = {name: Shape(mask) for name, mask in SHAPE_MASKS.items()}


GLYPHS = {
    "!": ["##", "##", "##", "..", "##"],
    "?": ["###", "..#", ".##", "...", ".#."],
    "heart": [".#.#.", "#####", "#####", ".###.", "..#.."],
}


# ---- expressions ----------------------------------------------------------------------
# Numbers that glide smoothly every frame: top/bot = rows hidden by the upper/lower eyelid,
# sl = eyelid tilt (+ angry, - sad), arc = happy "^" eye, ring = hollow eye with pupil, gx/gy = gaze bias.
PARAM_DEFAULTS = dict(
    top_l=0.0, top_r=0.0, bot_l=0.0, bot_r=0.0, sl_l=0.0, sl_r=0.0,
    arc_l=0.0, arc_r=0.0, amp=3.6, ring=0.0, gx=0.0, gy=0.0, h=1.0,
)


# Other keys: color, wander (idle eye-darting), look (follow the mouse), blink (rate), scan, jitter, shake, orbit, emote.
EXPRESSIONS = {
    "idle":      dict(color=C_CYAN),
    "happy":     dict(arc=1.0, amp=3.6, color=C_CYAN, wander=0.0, look=0.3),
    "laugh":     dict(arc=1.0, amp=4.6, color=C_CYAN, wander=0.0, look=0.0, shake=True),
    "love":      dict(arc=1.0, amp=3.6, color=C_PINK, wander=0.0, look=0.2, emote="heart"),
    "wink":      dict(top_r=5.5, bot_r=5.5, color=C_CYAN, wander=0.2),
    "smug":      dict(top=2.6, bot=0.6, slant=0.8, gx=1.5, color=C_PURPLE, wander=0.15, look=0.5),
    "squint":    dict(top=3.6, bot=3.4, slant=0.6, color=C_CYAN, scan=(3.0, 0.8), wander=0.0, look=0.0, blink=0.5),
    "curious":   dict(top_l=2.4, sl_l=0.9, bot_l=0.5, gy=-1.0, color=C_CYAN, wander=0.3, emote="?"),
    "surprised": dict(ring=1.0, h=1.12, color=C_CYAN, wander=0.0, look=0.4, emote="!"),
    "sad":       dict(top=1.2, slant=-2.4, gy=1.5, color=C_BLUE, wander=0.3, look=0.2, blink=0.6),
    "angry":     dict(top=1.6, slant=2.6, color=C_RED, wander=0.1, look=1.0, emote="!", blink=0.4),
    "bored":     dict(top=4.0, bot=1.0, color=C_DIM, wander=0.5, look=0.3, blink=0.4),
    "sleepy":    dict(top=4.6, bot=1.4, slant=-0.5, h=0.94, color=C_DIM, wander=0.2, look=0.2, blink=0.3),
    "thinking":  dict(top=1.0, gy=-2.0, color=C_AMBER, scan=(2.5, 1.5), wander=0.0, look=0.0),
    "focus":     dict(top=1.6, bot=0.6, slant=1.0, color=C_GREEN, wander=0.25, look=0.4, blink=0.5),
    "listening": dict(gy=2.2, color=C_CYAN, wander=0.0, look=0.0),
    "worried":   dict(top=0.8, slant=-1.4, color=C_CYAN, wander=0.0, look=0.0),
    "strike":    dict(top=3.0, bot=0.5, slant=1.8, gx=-3.0, color=C_OFF, wander=0.0, look=0.0, blink=0.3),
    "asleep":    dict(top=5.5, bot=5.5, color=C_SLEEP, wander=0.0, look=0.0, blink=0.0),
    "scared":    dict(ring=1.0, color=C_ORANGE, jitter=True, wander=0.0, look=0.0, emote="!"),
    "dizzy":     dict(ring=1.0, color=C_RED, orbit=True, wander=0.0, look=0.0),
    "calm":      dict(top=0.4, bot=0.2, color=C_CYAN, wander=1.0, look=0.8, blink=0.85),
    "irritated": dict(top=2.0, bot=0.8, slant=1.4, gx=1.0, color="#f97316", wander=0.15, look=0.5, blink=0.6),
    "fed_up":    dict(top=2.4, bot=1.2, slant=2.6, color="#f43f5e", wander=0.05, look=0.9, blink=0.35, emote="!"),
}


ALIASES = {"taunting": "smug", "annoyed": "squint", "excited": "happy", "dead": "strike"}


def _targets(ex: dict) -> dict:
    t = dict(PARAM_DEFAULTS)
    for key, l, r in (("top", "top_l", "top_r"), ("bot", "bot_l", "bot_r"),
                      ("slant", "sl_l", "sl_r"), ("arc", "arc_l", "arc_r")):
        if key in ex:
            t[l] = t[r] = ex[key]
    t.update({k: ex[k] for k in PARAM_DEFAULTS if k in ex})
    return t


_TARGETS = {name: _targets(ex) for name, ex in EXPRESSIONS.items()}
_COLORS = {name: _hex_rgb(ex["color"]) for name, ex in EXPRESSIONS.items()}


STATE_RGB = {                                     # state-driven eye colours (idle / neutral use the user's accent)
    "thinking": (245, 158, 11), "curious": (168, 85, 247),
    "happy": (16, 185, 129), "laugh": (16, 185, 129),
    "scared": (244, 63, 94), "angry": (244, 63, 94), "dizzy": (244, 63, 94),
    "sleepy": (99, 102, 241),
    "irritated": (249, 115, 22),                  # mood: orange
    "fed_up": (244, 63, 94),                      # mood: red
}

# What he says picks his face. First matching rule wins.
_RULES = [(name, re.compile(pat, re.I)) for name, pat in (
    ("laugh",     r"\b(ha(ha)+h?|lol|lmao|rofl)\b|😂|🤣"),
    ("wink",      r";-?\)|😉|\bwink\b"),
    ("angry",     r"\b(immediately|i warned you|ignoring you|enough|get back to work|unacceptable|ugh|how dare)\b|!{3,}"),
    ("surprised", r"\b(whoa|woah|wow|omg|no way|holy|gasp|what the)\b|\?!|!\?"),
    ("sad",       r"\b(sorry|sad|unfortunately|lonely|alone|miss you|cannot|couldn'?t|unable|offline|failed|not found)\b|😢|:\("),
    ("focus",     r"\b(focus (mode )?(is )?on|locked in|deep work|concentrate|distractions)\b"),
    ("love",      r"\b(love (you|it|this)|thank(s| you)|you'?re the best|proud of you|appreciate)\b|❤|♥|💖"),
    ("happy",     r"\b(great|nice|good (job|work)|well done|congrats|congratulations|awesome|amazing|yay|welcome|nailed|perfect|finally|done|added|no pending tasks|active|success|nice one)\b|😊|😄|:-?\)"),
    ("smug",      r"\b(bold choice|huh|sure|totally|again|procrastinat\w*|waiting|psst|reminder|go do it|obviously|shocking|riiight|cute|adorable|interesting choice|tab|sarcas\w*|oh(?=,)|oh really|as if)\b"),
    ("squint",    r"\b(watching|looking at|i see|suspicious|hmm+|let me see|checking|spotted|caught|what are you|what is that)\b"),
    ("bored",     r"\.\.\.|…|\b(yawn|boring|whatever|meh|zzz|tired|sleepy|snore)\b"),
    ("curious",   r"\?|\b(wonder|curious|interesting|what if|how about|tell me)\b"),
)]


def pick_expression(text: str, hint: str | None = None) -> str:
    """The face that matches what he just said (hint = fallback when no keyword matches)."""
    for name, rx in _RULES:
        if rx.search(text):
            return "curious" if name in ("happy", "bored") and "?" in text else name
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) >= 8 and sum(ch.isupper() for ch in letters) / len(letters) > 0.6:
        return "angry"
    if text.count("!") >= 2:
        return "surprised"
    hint = ALIASES.get(hint, hint)
    return hint if hint in EXPRESSIONS else "idle"


def speech_duration_ms(text: str) -> int:
    return int(clamp(2500 + 65 * len(text), 4000, 12000))


class EyeEngine:
    """Smoothly animated eyes: gliding lids, blinking, gaze, mouse-following, talking pulses and emote glyphs.
    Call update() then cells() once per frame."""

    FLOURISH = ["happy", "curious", "squint", "wink", "smug", "happy", "curious"]
    STIFFNESS, DAMPING = 170.0, 14.5                 # damping ratio ~0.55: slight overshoot, then settles

    def __init__(self):
        now = time.time()
        self.p = dict(PARAM_DEFAULTS)
        self.base_rgb = _hex_rgb(C_CYAN)             # the user's eye colour (Customize panel)
        self.mood = True                             # keep mood colours (red = angry...) or use one colour
        self.shape = SHAPES["round"]
        self.color = _COLORS["idle"]
        self.active = "idle"
        self.temp, self.temp_until = "idle", 0.0
        self.last_t = now
        self.gaze = [0.0, 0.0]
        self.vel = [0.0, 0.0]                        # gaze spring velocity
        self.goal = [0.0, 0.0]
        self.next_saccade = now + 1.0
        self.next_blink = now + 2.0
        self.blink_t = None
        self.blink_dur = 0.2
        self.blink_amt = 0.0
        self.next_flourish = now + random.uniform(20, 45)
        self.talk_until = 0.0
        self.emote, self.emote_until = None, 0.0
        self.orbit = 0.0
        self.last_ptr, self.last_ptr_move = None, 0.0
        self.inertia = (0.0, 0.0),
        self._mood = "chill"
    # ---- controls ----------------------------------------------------------------
    def configure(self, color=None, mood=None, shape=None):
        if color:
            self.base_rgb = _hex_rgb(color)
        if mood is not None:
            self.mood = bool(mood)
        if shape in SHAPES:
            self.shape = SHAPES[shape]

    def set_expression(self, name: str, duration: float = 3.0):
        name = ALIASES.get(name, name)
        if name in EXPRESSIONS:
            self.temp, self.temp_until = name, time.time() + duration

    def start_talking(self, n_chars: int):
        self.talk_until = time.time() + clamp(n_chars * 0.045, 0.8, 4.5)

    def blink_now(self, now: float, dur: float = 0.2):
        if self.blink_t is None:
            self.blink_t, self.blink_dur = now, dur

    def hex_color(self) -> str:
        return _rgb_hex(self.color)

    # ---- state ---------------------------------------------------------------------
    _MOOD_HYST = 4                                   # points of annoyance below a threshold before he relaxes

    def _update_mood(self, a):
        """Tier with hysteresis, so annoyance hovering at 40 or 80 can't make the face flicker."""
        if a is None:
            return
        m, h = self._mood, self._MOOD_HYST
        if a >= MOOD_FED_UP_AT:
            m = "fed_up"
        elif m == "fed_up" and a >= MOOD_FED_UP_AT - h:
            m = "fed_up"
        elif a >= MOOD_IRRITATED_AT:
            m = "irritated"
        elif m in ("irritated", "fed_up") and a >= MOOD_IRRITATED_AT - h:
            m = "irritated"
        else:
            m = "chill"
        self._mood = m

    def _resolve(self, now: float, c: dict) -> str:
        """Which face applies right now. Real states beat temporary expressions, which beat idle moods."""
        for flag, face in (("strike", "strike"), ("sleeping", "asleep"), ("scared", "scared"), ("dizzy", "dizzy"),
                           ("held", "worried"), ("thinking", "thinking"), ("listening", "listening")):
            if c.get(flag):
                return face
        if now < self.temp_until:
            return self.temp
        idle_t = c.get("idle_t", 0.0)
        if idle_t > DROWSY_AFTER:
            return "sleepy"
        if idle_t > BORED_AFTER:
            return "bored"
        if self._mood != "chill":                    # "irritated" / "fed_up" are also expression names
            return self._mood
        return "focus" if c.get("focus") else "calm"

    def _on_change(self, new: str, now: float):
        self.blink_now(now, 0.24)
        self.goal = [0.0, 0.0]
        self.next_saccade = now + 0.5
        em = EXPRESSIONS[new].get("emote")
        self.emote, self.emote_until = em, (now + 1.8 if em else 0.0)

    def _goal_color(self, name: str):
        """Idle/neutral = the user's accent. Mood states use STATE_RGB. 'thinking' pulses amber <-> violet.
        'calm' is the accent with a faint emerald tint. With mood tints off, every state keeps the accent."""
        base = self.base_rgb
        ex_col = EXPRESSIONS[name]["color"]
        if name == "strike":
            return _COLORS["strike"]
        if name == "asleep":
            return _mix(_mix(base, (102, 136, 170), 0.6), (0, 0, 0), 0.1)
        if not self.mood:
            return _mix(base, (0, 0, 0), 0.4) if ex_col == C_DIM else base
        if name == "thinking":
            u = 0.5 + 0.5 * math.sin(time.time() * 2.4)
            return _mix(STATE_RGB["thinking"], STATE_RGB["curious"], 0.65 * u)
        if name == "calm":
            return _mix(base, STATE_RGB["happy"], 0.15)
        if name in STATE_RGB:
            return STATE_RGB[name]
        if ex_col == C_DIM:
            return _mix(base, (0, 0, 0), 0.4)
        if ex_col == C_CYAN:
            return base
        return _COLORS[name]

    # ---- per-frame update ------------------------------------------------------------
    def update(self, now: float, c: dict):
        dt = clamp(now - self.last_t, 0.001, 0.2)
        self.last_t = now
        self.inertia = c.get("inertia", (0.0, 0.0))

        self._update_mood(c.get("annoyance"))
        name = self._resolve(now, c)
        if name in ("idle", "calm") and now >= self.next_flourish:   # a random little expression now and then
            self.next_flourish = now + random.uniform(25, 70)
            self.set_expression(random.choice(self.FLOURISH), random.uniform(1.3, 2.2))
            name = self._resolve(now, c)
        if name != self.active:
            self.active = name
            self._on_change(name, now)
        ex = EXPRESSIONS[name]

        k = 1 - math.exp(-dt * 16)
        for key, goal in _TARGETS[name].items():
            self.p[key] += (goal - self.p[key]) * k
        self.color = _mix(self.color, self._goal_color(name), 1 - math.exp(-dt * 9))

        self._update_blink(now, ex)
        self._update_gaze(now, dt, ex, c)
        if ex.get("orbit"):
            self.orbit += dt * 5.0

    def _update_blink(self, now: float, ex: dict):
        rate = ex.get("blink", 1.0)
        if self.blink_t is None:
            self.blink_amt = 0.0
            if rate > 0 and now >= self.next_blink:
                self.blink_t = now
                self.blink_dur = 0.2 if rate >= 0.8 else 0.36        # drowsy = slow blink
            return
        u = (now - self.blink_t) / self.blink_dur
        if u >= 1.0:
            self.blink_t, self.blink_amt = None, 0.0
            gap = random.uniform(2.5, 6.0) / max(rate, 0.15)
            self.next_blink = now + (0.22 if random.random() < 0.18 else gap)   # sometimes a double blink
        else:
            self.blink_amt = min(1.0, 1.8 * (1 - abs(2 * u - 1)))

    def _update_gaze(self, now: float, dt: float, ex: dict, c: dict):
        wander, look = ex.get("wander", 1.0), ex.get("look", 1.0)
        ptr, face = c.get("pointer"), c.get("face", (0, 0))
        tx = ty = 0.0
        following = False
        if ptr is not None:
            if ptr != self.last_ptr:
                self.last_ptr, self.last_ptr_move = ptr, now
            dx, dy = ptr[0] - face[0], ptr[1] - face[1]
            if look > 0 and (math.hypot(dx, dy) < 420 or now - self.last_ptr_move < 5.0):
                following = True
                amp = min(look, 1.0)
                tx = clamp(dx / 160.0, -1, 1) * 3.4 * amp
                ty = clamp(dy / 110.0, -1, 1) * 2.6 * amp
        if not following:
            scan = ex.get("scan")
            if scan:
                tx = scan[0] if int(now / scan[1]) % 2 else -scan[0]
            elif wander > 0:
                if now >= self.next_saccade:
                    self.goal = [0.0, 0.0] if random.random() < 0.35 else \
                        [random.uniform(-3.2, 3.2), random.uniform(-2.2, 2.2)]
                    self.next_saccade = now + random.uniform(1.0, 3.8) / wander
                tx, ty = self.goal
        dt = min(dt, 0.04)                                          # keeps the explicit integrator stable
        for i, goal in enumerate((tx, ty)):                         # 2D damped harmonic spring
            acc = self.STIFFNESS * (goal - self.gaze[i]) - self.DAMPING * self.vel[i]
            self.vel[i] += acc * dt
            self.gaze[i] = clamp(self.gaze[i] + self.vel[i] * dt, -5.0, 5.0)

    # ---- drawing -----------------------------------------------------------------------
    def cells(self, now: float) -> dict:
        """{(grid_x, grid_y): '#rrggbb'} for this frame."""
        ex, p, sh = EXPRESSIONS[self.active], self.p, self.shape
        col = _rgb_hex(self.color)
        light = _rgb_hex(_mix(self.color, (255, 255, 255), 0.6))
        gx = clamp(self.gaze[0] + p["gx"] + self.inertia[0], -4, 4)
        gy = clamp(self.gaze[1] + p["gy"] + self.inertia[1], -3, 3)
        pulse = 1.0 if (now < self.talk_until and math.sin(now * 16.0) > 0.4) else 0.0
        jx = jy = 0
        if ex.get("jitter"):
            jx, jy = random.choice((-1, 0, 1)), random.choice((-1, 0, 1))
        elif ex.get("shake"):
            jy = random.choice((-1, 0, 1))
        ring = p["ring"] > 0.5
        blink = self.blink_amt
        out: dict = {}

        for side, bx in (("l", 8), ("r", 36)):
            ox = bx + jx + (0 if ring else int(round(gx)))
            oy = 6 + jy + (0 if ring else int(round(gy)))
            local: set = set()
            shine: set = set()

            if p["arc_" + side] > 0.5:                                    # happy "^" eye
                if blink > 0.5:
                    local = {(c, r) for c in range(sh.cmin, sh.cmax + 1) for r in (5, 6)}   # closed line
                else:
                    oy += int(pulse)
                    amp = p["amp"]
                    for c in range(sh.cmin, sh.cmax + 1):
                        y = int(round(8.6 - amp * math.sin(math.pi * (c - sh.cmin + 0.5) / sh.w)))
                        local.update({(c, y), (c, y + 1)})
                        if amp > 4.0:
                            local.add((c, y + 2))
            else:                                                         # lidded eye, or hollow ring with a pupil
                top = min(p["top_" + side] * sh.scale, sh.half)
                bot = min(p["bot_" + side] * sh.scale, sh.half)
                slant = p["sl_" + side] * (1 - blink) * sh.scale
                top_e = max(top, top + (sh.half - top) * blink)
                bot_e = max(bot, bot + (sh.half - bot) * blink) + (0 if blink > 0.3 else pulse * sh.scale)
                for (c, r) in sh.cells:
                    inner = (c - 4.5) / 4.5 if side == "l" else (4.5 - c) / 4.5
                    y = r - sh.rmin + 0.5
                    if y < top_e + slant * inner or y > sh.h - bot_e:
                        continue
                    if ring and (c, r) not in sh.border:
                        continue
                    local.add((c, r))
                if ring:
                    if ex.get("orbit"):
                        a = self.orbit * (1 if side == "l" else -1)
                        pgx, pgy = math.cos(a) * 2.4, math.sin(a) * 2.4
                    elif ex.get("jitter"):
                        pgx, pgy = random.uniform(-1.5, 1.5), random.uniform(-1.5, 1.5)
                    else:
                        pgx, pgy = gx * 0.8, gy * 0.8
                    pw, ph = (3 if sh.w >= 8 else 2), (3 if sh.h >= 8 else 2)    # pupil fits the eye
                    mc, mr = (1 if sh.w >= 8 else 0), (1 if sh.h >= 10 else 0)
                    c0 = int(clamp(round(4.5 + pgx - pw / 2), sh.cmin + 1 + mc, sh.cmax - pw - mc))
                    r0 = int(clamp(round(5.5 + pgy - ph / 2), sh.rmin + 1 + mr, sh.rmax - ph - mr))
                    local.update({(c0 + dc, r0 + dr) for dc in range(pw) for dr in range(ph)})
                elif blink < 0.3:                                             # a little shine in the eye
                    sx, sy = int(clamp(round(gx * 0.35), -1, 1)), int(clamp(round(gy * 0.35), -1, 1))
                    sc = sh.cmin + min(2, (sh.w - 4) // 2) + sx
                    sr = sh.rmin + max(2, int(math.ceil(top_e)) + 1) + sy
                    shine = {(sc, sr), (sc + 1, sr), (sc, sr + 1), (sc + 1, sr + 1)} & local

            for (c, r) in local:
                out[(ox + c, oy + r)] = light if (c, r) in shine else col

        self._overlays(out, now, col)
        return out

    def _overlays(self, out: dict, now: float, col: str):
        if self.emote and now < self.emote_until:                    # "!" / "?" / heart popping up
            gcol = C_PINK if self.emote == "heart" else col
            bounce = 1 if int(now * 5) % 2 else 0
            x0 = 52 if self.emote == "heart" else 53
            for r, row in enumerate(GLYPHS[self.emote]):
                for c, ch in enumerate(row):
                    if ch == "#":
                        out[(x0 + c, bounce + r)] = gcol
        if self.active == "thinking":                                # three dots lighting up one by one
            lit = int(now * 2.5) % 4
            for i in range(3):
                for dx in (0, 1):
                    for dy in (0, 1):
                        out[(23 + i * 4 + dx, 25 + dy)] = C_AMBER if i < lit else C_OFF


EYE_SHAPES = ("pill", "round", "slim", "visor", "square")
_TIER_SHAPES = {"chill": "pill", "irritated": "visor", "fed_up": "slim"}
_FOCUS = EXPRESSIONS["focus"]
_IDLE_LIDS = {
    "work": (_FOCUS["top"], _FOCUS["bot"], _FOCUS["slant"]),
    "chill": (0.5, 0.5, 0.0),
    "neutral": (0.0, 0.0, 0.0),
}
_DEFAULT_EXPRESSIONS = frozenset({"idle", "calm", "focus", "irritated", "fed_up"})


def annoyance_tier(annoyance: int) -> str:
    if annoyance >= MOOD_FED_UP_AT:
        return "fed_up"
    return "irritated" if annoyance >= MOOD_IRRITATED_AT else "chill"


def is_eye_shape_pinned(saved_shape: str) -> bool:
    return saved_shape in EYE_SHAPES and saved_shape != "pill"


def effective_eye_shape(saved_shape: str, annoyance: int, shape_pinned: bool = False, *,
                        profile: str = "neutral") -> str:
    """Use the profile's automatic silhouette or keep a Studio override."""
    if shape_pinned and is_eye_shape_pinned(saved_shape):
        return saved_shape
    if profile in ("work", "chill"):
        return "pill"
    tier = annoyance_tier(annoyance)
    if tier != "chill" and is_eye_shape_pinned(saved_shape):
        return saved_shape
    return _TIER_SHAPES[tier]


class ProfileEyeEngine(EyeEngine):
    """Apply profile geometry and drive the mask from the UI frame clock."""

    MASK_ENTER_S = 0.45
    MASK_WAKE_S = 0.55
    MASK_LIFT = -64.0
    WAKE_EXPRESSION_S = 1.2

    def __init__(self):
        super().__init__()
        self.mask_state = "none"
        self.mask_t0 = 0.0
        self._mask_from_y = self.MASK_LIFT
        self._mask_from_alpha = 1.0
        self._wake_until = 0.0

    @staticmethod
    def mask_bob(now: float) -> float:
        return round(math.sin(now * 2.0) * 2) / 2

    def mask_spec(self, now: float) -> tuple[float, float, str] | None:
        """Return the quantized pose, with breathing supplied separately during settled sleep."""
        state = self.mask_state
        if state == "none":
            return None
        if state == "sleeping":
            return 0.0, 1.0, state

        duration = self.MASK_ENTER_S if state == "entering" else self.MASK_WAKE_S
        if now >= self.mask_t0 + duration:
            return (0.0, 1.0, "sleeping") if state == "entering" else None
        t = max(0.0, (now - self.mask_t0) / duration)
        if state == "entering":
            progress = 1.0 - (1.0 - t) ** 3
            y = self._mask_from_y * (1.0 - progress)
            alpha = self._mask_from_alpha + (1.0 - self._mask_from_alpha) * progress
        else:
            back = t - 1.0
            progress = 1.0 + 1.9 * back ** 3 + 0.9 * back ** 2
            y = self._mask_from_y + (self.MASK_LIFT - self._mask_from_y) * progress
            alpha = self._mask_from_alpha * (1.0 - t)
        return round(y * 2) / 2, round(alpha, 2), state

    def _update_mask(self, now: float, sleeping: bool) -> bool:
        state = self.mask_state
        if sleeping and state in ("none", "waking"):
            previous = self.mask_spec(now)
            self._mask_from_y, self._mask_from_alpha = previous[:2] if previous else (self.MASK_LIFT, 1.0)
            self.mask_state, self.mask_t0 = "entering", now
            self._wake_until = 0.0
        elif not sleeping and state in ("entering", "sleeping"):
            y, alpha, phase = self.mask_spec(now)
            self._mask_from_y = y + self.mask_bob(now) if phase == "sleeping" else y
            self._mask_from_alpha = alpha
            self.mask_state, self.mask_t0 = "waking", now
            self._wake_until = now + self.WAKE_EXPRESSION_S
            self.p.update(_TARGETS["surprised"])
            return True
        elif state == "entering" and now >= self.mask_t0 + self.MASK_ENTER_S:
            self.mask_state = "sleeping"
        elif state == "waking" and now >= self.mask_t0 + self.MASK_WAKE_S:
            self.mask_state = "none"
        return False

    def _resolve(self, now: float, context: dict) -> str:
        if now < self._wake_until and not context.get("strike"):
            return "surprised"
        name = super()._resolve(now, context)
        if context["shape_pinned"] or now < self.temp_until or name not in _DEFAULT_EXPRESSIONS:
            return name
        tier = context["mood_tier"]
        if context["profile"] == "neutral" and tier != "chill":
            return tier
        return "idle"

    def update(self, now: float, context: dict):
        woke = self._update_mask(now, bool(context.get("sleeping")))
        super().update(now, context)
        if woke:
            self.blink_t, self.blink_amt = None, 0.0
            self.next_blink = max(self.next_blink, self._wake_until)
        if self.active == "idle" and not context["shape_pinned"] and now >= self.temp_until:
            top, bot, slant = _IDLE_LIDS[context["profile"]]
            self.p.update(top_l=top, top_r=top, bot_l=bot, bot_r=bot, sl_l=slant, sl_r=slant,
                          arc_l=0.0, arc_r=0.0, ring=0.0, h=1.0)
            k = 0.9 if context["profile"] == "chill" else 1.0
            self.color = tuple(int(c * k) for c in self.base_rgb)
