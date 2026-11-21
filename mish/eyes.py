"""Profile-aware eye shape and idle posture."""

from __future__ import annotations

import math

from .brain import EXPRESSIONS, EyeEngine, _TARGETS
from .config import MOOD_FED_UP_AT, MOOD_IRRITATED_AT

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