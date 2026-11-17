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
MODEL_GLOB = "*.gguf"            # any .gguf in a "models" folder (biggest = main, smallest = lite)


MODEL_NAME = "gemma3:1b"         # only used if the local model fails and Ollama is installed
KEEP_ALIVE = "30m"               # Ollama fallback only
VISION_MODEL = "moondream"       # screen vision needs Ollama; off in the standalone build
VISION_KEEP_ALIVE = "5m"


# ---- timing & behaviour ----
REACTION_COOLDOWN = 45           # seconds between window-switch reactions


AMBIENT_MIN_GAP = 90             # minimum seconds between any unsolicited popups
BRAIN_IDLE_UNLOAD_S = 15 * 60    # unload main brain to RAM after 15m idle
__version__ = "0.5.2-alpha"
HOTKEY_MOD = 0x0001 | 0x4000     # MOD_ALT | MOD_NOREPEAT
HOTKEY_VK = 0x4D                 # 'M' -> Alt+M global summon


FOCUS_PROFILES = {
    "general": {"work_extra": [], "slacking_extra": [], "nag_slacking": True},
    "student": {"work_extra": ["anki", "notion", "obsidian", "onenote", "acrobat", "zotero", "word", "docs"], "slacking_extra": [], "nag_slacking": True},
    "dev": {"work_extra": ["code", "pycharm", "terminal", "powershell", "wt", "git", "docker", "cursor"], "slacking_extra": [], "nag_slacking": True},
    "creative": {"work_extra": ["blender", "photoshop", "premiere", "resolve", "figma", "illustrator", "audacity", "obs"], "slacking_extra": [], "nag_slacking": True},
    "chill": {"work_extra": [], "slacking_extra": [], "nag_slacking": False},
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


# ---- heavy mode / automatic brain swapping ----
HEAVY_RAM_FREE_GB = 1.2          # less free RAM than this counts as heavy load


HEAVY_CPU = 90                   # sustained CPU % (outside his own thinking) that counts as heavy load
HEAVY_ENTER_S = 8                # heavy load must last this long before he shrinks
HEAVY_EXIT_S = 25                # and it must be calm this long before he grows back
SWAP_MIN_GAP = 45                # never swap more often than this
HW_POLL_S = 3
DEFAULT_HEAVY = ["fallout4.exe", "ltspice.exe"]            # edit with !heavy add / remove
AMBIENT_EVENTS = {"observe", "reminder", "slacking", "switch", "pester", "wake"}   # muted in heavy mode
QUIET_EVENTS = {"poke"}          # generated without showing the "thinking" eyes


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
    "observe": "You just glanced at the user's screen: {seen}. Comment on it in a nosy, needling way.",
    "reminder": "Pester the user about ONE specific pending task from the list.",
    "slacking": "The user is distracted by '{window}'. Nag them about it, smug and clingy.",
    "switch": "The user just switched to '{window}'. Make a nosy, needling remark about it.",
    "chat": "Reply to the user's message. Actually answer or help with what they asked, but with some sass "
            "(tease, nitpick, or add a nosy follow-up). Connect it to the chat history when relevant.",
    "pester": "Interrupt the user with an unsolicited, cheeky remark or a nosy question. Base it on the context. "
              "Do not say that you are interrupting.",
    "nag": "A task just came due: {extra}. Nag the user about it.",
    "startup": "You just booted up on the user's PC. Greet them in a cheeky, needling way.",
    "wake": "You were asleep and the user just came back and woke you up. Complain, or nosily ask where they went.",
    "poke": "The user just poked (clicked) you, {extra}. React to being poked, but do not repeat the numbers.",
    "scared": "The user dragged you to the very edge of the screen and you are terrified of falling off. "
              "Panic dramatically and demand to be moved back.",
    "dizzy": "The user is shaking you violently and you are dizzy. Complain about it.",
    "strike_start": "You were left at the screen edge for too long. Announce that you are on strike and refuse "
                    "to talk to the user for 60 seconds.",
    "strike_end": "Your 60-second silent treatment just ended. Come back grudgingly, still petty.",
    "strike_chat": "You are giving the user the silent treatment and refuse to help right now. The user is trying "
                   "to talk to you: {extra}. Give a short, icy refusal.",
    "file": "The user just dropped a file on you. Facts and an excerpt: {extra}\nReact with one quick, sassy remark "
            "that uses ONLY these facts (name, type, size, syntax check result). If the check found an error, point it "
            "out with the line number. Never invent problems the check did not report.",
    "heavy_end": "A demanding program the user was running just closed. You had shrunk your brain to save memory "
                 "while it ran. Comment on it.",
    "focus_punish": "The user is in focus mode but opened '{window}'. You just teleported to the middle of their "
                    "screen to stop them. Demand that they close it.",
    "focus_done": "The user just finished their {extra}. Congratulate them briefly with a tiny bit of sass.",
}


STRIKE_EVENTS = {"strike_start", "strike_end", "strike_chat"}


EVENT_HINTS = {     # fallback face if his words don't match a keyword rule
    "slacking": "smug", "reminder": "smug", "switch": "curious", "pester": "smug", "nag": "squint",
    "startup": "happy", "wake": "surprised", "poke": "surprised", "strike_start": "angry",
    "strike_end": "squint", "strike_chat": "strike", "file": "squint", "heavy_end": "smug",
    "focus_punish": "angry",
}


HELP_TEXT = """Commands:
  add <task> [in 30m | at 6pm]
  tasks | done <n> | delete <n>
  remember <fact> | facts | forget <n>
  focus [min] | pomodoro [work] [break] | pause/resume/stop focus | stats
  look [question] | watch on|off
  !open <app> | !apps | !alias <name> <path>
  !read <file> (or drop a file on me)
  !sys | !time | !clean | !brain | !heavy"""


BANG_HELP = "Commands: !open, !apps, !alias, !read, !sys, !time, !clean, !brain, !heavy, !profile, !focus, !clip2task, !ver"
