"""Constants, prompts and tunables (no behaviour).

Split from the monolithic mish.pyw by split_mish.py; this package is now the source of truth.
"""
from __future__ import annotations

import sys
from pathlib import Path


# Folder that holds mish.pyw (portable.flag, models/ and canned_lines.json are looked up here).
# This module lives one level down, in mish/, hence .parent.parent.
APP_DIR = Path(__file__).resolve().parent.parent


def user_data_dir() -> Path:
    """Writable directory for memory, settings, logs, and models (portable if portable.flag exists)."""
    exe_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else APP_DIR
    if (exe_dir / "portable.flag").exists():
        return exe_dir / "MishData"
    return Path.home() / "Documents" / "Mish"


DATA_DIR = user_data_dir()


# ---- brain ----
BRAIN_CTX = 1536
MODEL_GLOB = "*.gguf"


VISION_MODEL = "moondream"       # screen vision needs Ollama; off in the standalone build
VISION_KEEP_ALIVE = "5m"


# ---- timing & behaviour ----
REACTION_COOLDOWN = 45           # seconds between window-switch reactions


AMBIENT_MIN_GAP = 90             # minimum seconds between any unsolicited popups
BRAIN_IDLE_UNLOAD_S = 15 * 60    # release the auto-mode GGUF after 15m idle
__version__ = "0.5.50b"
DEFAULT_SETTINGS = {"dev": 0, "brain_mode": "auto", "profile": "neutral", "profile_auto": 1}
HOTKEY_MOD = 0x0001 | 0x4000     # MOD_ALT | MOD_NOREPEAT
HOTKEY_VK = 0x4D                 # 'M' -> Alt+M global summon


PROFILE_NAMES = ("work", "neutral", "chill")
_PROFILE_CATEGORIES = {"code": "work", "work": "work", "media": "slack", "social": "slack", "game": "slack"}
PROFILES = {
    "work": {
        "categories": _PROFILE_CATEGORIES,
        "slack_step": 12, "slack_grace_s": 45, "work_decay": 3, "annoy_cap": 100,
        "muted": frozenset({"switch", "pester"}),
        "persona": "Be a concise, practical coach. Steer back to the current task when relevant.",
    },
    "neutral": {
        "categories": _PROFILE_CATEGORIES,
        "slack_step": 8, "slack_grace_s": 90, "work_decay": 2, "annoy_cap": 100,
        "muted": frozenset(),
        "persona": "Be a cheeky, balanced desktop companion.",
    },
    "chill": {
        "categories": {**_PROFILE_CATEGORIES, "media": "neutral", "social": "neutral", "game": "neutral"},
        "slack_step": 0, "slack_grace_s": 0, "work_decay": 2, "annoy_cap": 35,
        "muted": frozenset({"nag"}),
        "persona": "Be a relaxed hangout buddy. Never scold about games, media, or open tabs.",
    },
}

# Built-in lines for the relaxed profile; user-provided buckets can replace them.
PROFILE_CANNED_DEFAULTS = {
    "switch__chill": ["Over to {app}. Nice.", "New window, same good company."],
    "pester__chill": ["Hey, hope you're having a good time.", "I'm here if you feel like chatting."],
    "gaming__chill": ["Enjoy the game. I'm cheering for you.", "Have fun in {app}."],
    "slacking__chill": ["Taking a breather? Enjoy it.", "Hope you're having a good time in {app}."],
}


WINDOW_SWITCH_CHANCE = 0.25
FOCUS_DEFAULT_MIN = 25
BREAK_DEFAULT_MIN = 5
IDLE_PAUSE = 3 * 60              # stop watching/pestering after this much idle time
AUTO_SLEEP_TIMEOUT = 5 * 60
BORED_AFTER = 60                 # idle seconds before his eyes go bored
DROWSY_AFTER = AUTO_SLEEP_TIMEOUT // 2
SCHEDULER_TICK = 15
RENAG_INTERVAL = 5 * 60          # seconds between repeat nags for an overdue task
MAX_RENAGS = 3
STATS_KEEP_DAYS = 14
PESTER_MIN, PESTER_MAX = 7, 18   # minutes between unprompted interruptions
OBSERVE_MIN, OBSERVE_MAX = 3, 8  # minutes between random screen glances
OBSERVE_MAX_SIDE = 1024
OBSERVE_WHOLE_DESKTOP = False
ACTIVITY_KEEP = 40


# ---- memory ----
FACT_LIMIT = 20                  # facts kept, strongest first


FACT_PROMPT_MIN = 2.5            # only facts at least this strong go into his prompt


# ---- heavy mode / automatic brain unloading ----
HEAVY_RAM_PCT = 85               # sustained system RAM usage that counts as heavy load


HEAVY_CPU = 90                   # sustained CPU % (outside his own thinking) that counts as heavy load
HEAVY_ENTER_S = 8                # sustained pressure before unloading
HEAVY_EXIT_S = 25                # sustained calm before auto can load again
HW_POLL_S = 3
DEFAULT_HEAVY = ["", ""]            # edit with !heavy add / remove
AMBIENT_EVENTS = {"observe", "reminder", "slacking", "switch", "pester", "wake"}   # muted in heavy mode
ANNOYANCE_START = 15
MOOD_IRRITATED_AT = 40           # tier "irritated" from here
MOOD_FED_UP_AT = 80              # tier "fed_up" from here
ANNOY_NAG_STEP = 12              # +annoyance each time a due-task nag fires
ANNOY_DONE_REWARD = 25           # -annoyance when a task is completed
SLACK_BUMP_GAP = 45              # min seconds between two slack bumps
DEV_SLACK_STREAK_S = 5
DEV_SLACK_BUMP_GAP = 5
DEV_REACTION_COOLDOWN = 5
DEV_AMBIENT_MIN_GAP = 5
DEV_WINDOW_SWITCH_CHANCE = 1.0


# ---- app keywords ----
SLACK_KEYWORDS = [
    "youtube", "reddit", "twitch", "netflix", "facebook", "twitter", "x.com",
    "instagram", "tiktok", "anime", "discord", "steam", "fallout", "vortex",
    "epicgames", "9gag",
]


WORK_KEYWORDS = [
    "visual studio code", "code.exe", "godot", "ltspice", "python", "pycharm",
    "matlab", "kicad", "altium", "proteus", "arduino", "winword.exe",
    "excel.exe", "powerpnt.exe", ".pdf", "github", "stack overflow",
]


# ---- files he is willing to read (text and code only) ----
TEXT_EXTS = {
    ".txt", ".py", ".pyw", ".json", ".md", ".csv", ".log", ".ini", ".cfg", ".toml", ".yaml", ".yml",
    ".js", ".ts", ".html", ".css", ".c", ".h", ".cpp", ".hpp", ".cs", ".java", ".bat", ".ps1", ".sql",
    ".sh", ".xml", ".v", ".sv", ".vhd", ".asm", ".ino", ".tex", ".asc", ".cir", ".net", ".sp", ".spice",
}


FILE_READ_BYTES = 65536          # never read more than this from disk
FILE_CHAR_LIMIT = 2200           # excerpt that goes into the model (his context window is small)


# ---- !open shortcuts ----
DEFAULT_APPS = {
    "notepad": "notepad.exe", "calc": "calc.exe", "paint": "mspaint.exe",
    "explorer": "explorer.exe", "taskmgr": "taskmgr.exe",
}


# ---- persona & prompts ----
PERSONA = (
    "You are Mish, a cheeky little companion who lives on the user's PC. Your manner: nosy, needy, teasing and a bit "
    "of a know-it-all, like a bratty younger sibling. You ask pointed or nosy questions, bring up things the user said "
    "or did earlier, give unsolicited opinions, and love being right. Never cruel, no real insults. You do care, and when "
    "the user asks for real help you give a correct answer, with a little sass. Speak directly to the user in first person."
)


RULES = (
    "STRICT RULES: Output ONLY the words you say out loud. Plain text, max 25 words. Do not start with your name or any "
    "label like 'Mish:'. Never describe or mention your own personality, never call yourself a gremlin, annoying or an AI. "
    "No parenthetical actions, no emojis, no quotation marks. Fresh wording every time.\n"
    "Do NOT invent facts about the user. Only use facts explicitly listed in CONTEXT. "
    "If you don't know something, make a guess-free snarky comment instead of inventing an answer. "
    "ALWAYS check the 'Current time' in the CONTEXT below to know what time it is."
)


OPEN_RULE = (
    "If the user asks you to open an app, reply with exactly [OPEN: appname] and nothing else.\n"
    "EXAMPLES:\nUser: Can you open discord?\nMish: [OPEN: discord]\nUser: open notepad for me\nMish: [OPEN: notepad]"
)


EVENT_INSTRUCTIONS = {
    "chat": "Reply to the user's message. Actually answer or help with what they asked, but with some sass "
            "(tease, nitpick, or add a nosy follow-up). Connect it to the chat history when relevant.",
    "file": "The user just dropped a file on you. Facts and an excerpt: {extra}\nReact with one quick, sassy remark "
            "that uses ONLY these facts (name, type, size, syntax check result). If the check found an error, point it "
            "out with the line number. Never invent problems the check did not report.",
}


STRIKE_EVENTS = {"strike_start", "strike_end", "strike_chat"}


EVENT_HINTS = {     # fallback face if his words don't match a keyword rule
    "slacking": "smug", "reminder": "smug", "switch": "curious", "pester": "smug", "nag": "squint",
    "startup": "happy", "wake": "surprised", "poke": "surprised", "strike_start": "angry",
    "strike_end": "squint", "strike_chat": "strike", "file": "squint", "heavy_end": "smug",
    "focus_punish": "angry", "gaming": "smug",
}


HELP_TEXT = """Commands:
  add <task> [in 30m | at 6pm]
  tasks | done <n> | delete <n>
  remember <fact> | facts | forget <n>
  focus [min] | pomodoro [work] [break] | pause/resume/stop focus | stats
  look [question] | watch on|off
  !open <app> | !apps | !alias <name> <path>
  !read <file> (or drop a file on me)
  !sys | !time | !clean | !brain [on|off|auto] | !heavy
  !profile [work|neutral|chill|auto]"""


BANG_HELP = "Commands: !sync, !bb, !connect, !mail, !msid, !open, !apps, !alias, !read, !sys, !time, !clean, !brain, !heavy, !profile, !focus, !clip2task, !ver, !dev"