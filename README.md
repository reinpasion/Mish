# Mish

An offline-capable AI desktop buddy that lives **right on your Windows screen**. Powered by a local GGUF language model (`llama-cpp-python`, with optional Ollama vision for screen reading), Mish watches your active windows, manages your tasks and focus timers, auto-manages RAM when heavy apps launch, and chats with you locally on your own PC.

## What makes Mish different

- **Local dual-brain AI:** Loads a `main` GGUF model on your PC and automatically switches to a smaller `lite` model (or unloads to save RAM) when you open heavy apps, run low on memory, or step away from your keyboard
- **Screen & window awareness:** Tracks your active window title/app against your chosen focus profile, gives random screen glances (`look` / `observe`), and can inspect your screen via an optional local vision model
- **Errand runner & file reader:** Can open apps for you (even searching the Windows Start menu when needed) and read/summarize files dropped onto him (`!read`) or copied to your clipboard
- **Personal memory & tasks:** Remembers facts (`remember ...`) and keeps a local to-do list (`Active Tasks`) with due-date nudges, plus automatic syncing from **Blackboard**, **Google** (Calendar + Gmail), and **Microsoft** (Outlook + To Do)
- **Alive on your desktop:** Sits as a compact dark rounded face with expressive eyes that follow your cursor, react to your mood/activity, and tuck neatly into the top-center edge of your screen when you need him out of the way

## The PC it runs on

Built and tested on Windows. Mish automatically monitors CPU and free RAM so he stays out of the way when you open games, IDEs, or heavy software.

### Minimum specs you need

- Any Windows 10 or Windows 11 PC (64-bit)
- Python 3.10 or newer
- 8 GB of RAM recommended when running local `.gguf` AI models (4 GB works using the `lite` profile or without a local model)
- Internet only for the initial package install and optional Blackboard, Google, or Microsoft syncing

## What is in this folder

| File / Folder | What it is |
|---|---|
| `mish.pyw` | The main file you double-click or run to start Mish (no black terminal window) |
| `mish/brain.py` | Mish's core logic: local GGUF loader, auto RAM/CPU model swapping, screen vision, timers, and command parser |
| `mish/ui.py` | Draws Mish's face, eyes, animations, speech bubbles, and pop-up panels |
| `mish/eyes.py` | Controls eye movement, blinking, and expressions |
| `mish/config.py` | Stores theme colors, focus profiles, model rules, and prompts |
| `mish/store.py` | Saves your local memory, tasks, usage stats, and settings |
| `mish/win_utils.py` | Handles Windows screen capture, active window detection, clipboard, and app launching |
| `mish/integrations/blackboard.py` | Pulls deadlines from your Blackboard calendar link |
| `mish/integrations/google_sync.py` | Connects Google Calendar and Gmail |
| `mish/integrations/microsoft_sync.py` | Connects Outlook and Microsoft To Do |
| `mish/integrations/scheduler.py` | Runs background sync jobs so Mish never freezes |

Local `.gguf` model files are not stored in this GitHub repo because of file size. Download Mish's fine-tuned model weights from Hugging Face: **[reiHien/MishV6](https://huggingface.co/reiHien/MishV6/tree/main)**

Personal memory (`memory.json`) and account tokens are also **not** included in this folder.

## How to set it up

1. Download or clone this folder onto your PC.
2. Download the `.gguf` model file(s) from **[Hugging Face (`reiHien/MishV6`)](https://huggingface.co/reiHien/MishV6/tree/main)** and place them inside the `models/` folder. *(If you put two `.gguf` files in `models/`, Mish automatically uses the larger one as `main` and the smaller one as `lite`.)*
3. Open **PowerShell** inside the `Mish` folder and install the required packages:
```powershell
   pip install pillow psutil plyer apscheduler sqlalchemy icalendar requests google-api-python-client google-auth-httplib2 google-auth-oauthlib msal
   pip install llama-cpp-python --only-binary=llama-cpp-python --extra-index-url [https://abetlen.github.io/llama-cpp-python/whl/cpu](https://abetlen.github.io/llama-cpp-python/whl/cpu)
   ```
4. Start Mish:
   - **Normal mode (no console window):** Double-click `mish.pyw`
   - **With debug logs:** Run `python -u mish.pyw` in PowerShell

## How to use Mish

- **Chat or run commands:** Right-click Mish and open the chat box to talk or type commands.
- **Drop a file on him:** Drag and drop any supported file onto Mish (or use `!read`) so he can inspect and summarize it.
- **Move him:** Click and drag his dark body anywhere on your screen.
- **Tuck him away:** Drag him to the **top-center** of your monitor to dock him into a compact top tab. Pull down on the tab to pop him back out.
- **Open menus:** Right-click Mish (or click the small gear icon when hovering over him) to open **Active Tasks**, **Connections**, or **Customize**.
- **Close any window:** Click `✕` in the top-right corner or press `Esc`.

## Commands

Mish supports both **`!` system/tool commands** (which work even if his AI brain is asleep or on strike) and **plain-text commands**.

### `!` Bang Commands

| Command | What it does |
|---|---|
| `!sys` | Shows CPU usage, system RAM, Mish's RAM footprint, and active AI model |
| `!brain [main\|lite\|off\|auto]` | Checks brain status or manually switches between `main`, `lite`, `off`, and `auto` |
| `!heavy [add\|remove <exe>]` | Lists or edits heavy apps that trigger Mish to shrink to `lite` / unload RAM |
| `!open <app>` | Launches an app or sends Mish to search and open it from the Windows Start menu |
| `!apps` | Lists all apps and custom aliases Mish knows how to open |
| `!alias <name> [path]` | Adds a shortcut name for an app path (omit `path` to remove the alias) |
| `!read <file path>` | Reads and summarizes a file (same as dropping a file onto Mish) |
| `!clip2task` | Turns the first line of your clipboard into a new task |
| `!focus [min]` / `!focus pomo [w] [b]` | Starts a focus timer or Pomodoro loop; also supports `pause`, `resume`, `stop` |
| `!profile [name]` | Shows or switches your active focus profile (adjusts work vs. slacking app detection) |
| `!sync` | Manually syncs Blackboard, Google, and Microsoft right now |
| `!bb <ics_url>` | Saves your Blackboard calendar iCal feed URL and syncs it |
| `!msid <client_id>` | Saves your Microsoft Azure Client ID for Outlook / To Do sign-in |
| `!connect <google\|microsoft>` | Opens browser sign-in for Google or Microsoft |
| `!mail <gmail\|microsoft> \| to \| subj \| body` | Sends an email directly from the command box |
| `!clean` | Clears saved chat history and empties `Mish.log` (keeps tasks and facts safe) |
| `!time` / `!ver` | Shows current time/day or Mish's version and active profile |

### Plain-Text Commands

| Category | Examples you can type |
|---|---|
| **Tasks** | `tasks`, `add finish lab report by friday`, `done 1`, `delete 2` |
| **Memory** | `facts`, `remember my exam is at 9am`, `forget 1` |
| **Focus Timer** | `focus 25`, `pomo 25 5`, `pause focus`, `resume focus`, `stop focus` |
| **Screen & Clipboard** | `look`, `look what is this error?`, `observe on`, `observe off`, `summarize clipboard` |
| **Stats & Help** | `stats`, `today`, `help` |

## What to expect & known quirks

- **He can be a bit stupid sometimes:** Mish runs a tiny local model so he stays fast and lightweight on normal laptops. Expect occasional goofy answers, misunderstood context, or confidently wrong guesses—use `!clean` if his chat history gets messy.
- **Bugs & rough edges:** Mish is an active personal project. If a window acts weird, a command hiccups, or something breaks, check `Mish.log` in your Mish data folder (`Documents\Mish\Mish.log`) and feel free to open an **Issue** on this GitHub repo with what happened and the log snippet.

## Version History

Mish follows an **`x.y.za`** versioning format:
- **`x`** = Major milestone / full architecture overhaul
- **`y`** = Major feature release
- **`z`** = Small feature additions
- **`a`** = Bug fixes & small tweaks

### Released Versions

| Version | Release Type | Highlights |
| :--- | :--- | :--- |
| **`0.3.00`** | Initial Release | First working build of Mish as a monolithic desktop companion (`mish.pyw`) with basic animated eyes, canned lines, and early local LLM chat support. |
| **`0.4.00`** | Major Update | Modularized the codebase into the `mish/` package (`brain.py`, `ui.py`, `eyes.py`, `config.py`, `store.py`, `win_utils.py`); introduced dual `main`/`lite` GGUF model auto-swapping based on RAM/CPU load, window tracking, focus timers, and `!` bang commands. |
| **`0.5.00`** | Major Update | Redesigned Mish into a dark obsidian squircle HUD with 2D spring-physics eye tracking, dynamic mood/state colors, top-center notch docking, and theme-adaptive (`Auto` / `Dark` / `Light`) 4x supersampled PIL panels (`Studio`, `Active Tasks`, `Connections`). |
| **`0.5.10`** | Feature & Hub Update | Added the `AutomationHub` background scheduler (`APScheduler` + `SQLAlchemy`) with Blackboard iCal feed syncing, Google Calendar/Gmail integration, Microsoft Outlook/To Do integration, and focus profiles. |
| **`0.5.21`** *(Current)* | Feature & Polish Update | Refined HUD panel auto-sizing and layout polish, added clipboard-to-task (`!clip2task`) and Start menu app search errands (`!open`), and published Mish's fine-tuned GGUF brain (`reiHien/MishV6`) alongside updated setup docs. |
