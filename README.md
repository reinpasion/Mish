# Mish

An offline-capable AI desktop buddy that lives **right on your Windows screen**. Powered by a local language model (`SmolLM2-1.7B-Instruct` via `llama.cpp`) running directly on your PC, Mish can read what is on your screen, remember your tasks and deadlines, react to your work habits, and chat with you without sending your data to the cloud.

## What makes Mish different

- **Local AI brain:** Runs a quantized GGUF model (`SmolLM2-1.7B-Instruct`) right on your own hardware for private, offline conversations—with built-in personality lines as instant fallback
- **Screen awareness:** Can read and understand what you are currently working on to give relevant comments, nudges, or help
- **Alive & autonomous:** Has his own mood, energy, focus/break timers, idle behaviors, and expressive eyes that follow your cursor and react to how you interact with him
- **Personal memory & tasks:** Remembers your to-do list (`Active Tasks`) locally and syncs school/work deadlines from **Blackboard**, **Google** (Calendar + Gmail), and **Microsoft** (Outlook + To Do)
- **Lives on your desktop:** Sits as a compact dark rounded face that can float anywhere or tuck into the top-center edge of your screen when you need him out of the way

## The PC it runs on

Built and tested on Windows. Mish is designed to stay lightweight enough to run alongside your browser, code editor, or games on a regular laptop or desktop.

### Minimum specs you need

- Any Windows 10 or Windows 11 PC (64-bit)
- Python 3.10 or newer
- 8 GB of RAM recommended when running the local GGUF AI model (4 GB works if running without a local model)
- Internet only for the initial package install and optional Blackboard, Google, or Microsoft syncing

## What is in this folder

| File / Folder | What it is |
|---|---|
| `mish.pyw` | The main file you double-click or run to start Mish (no black terminal window) |
| `mish/brain.py` | Mish's core intelligence: local LLM connection, screen context, mood, timers, and replies |
| `mish/ui.py` | Draws Mish's face, eyes, animations, speech bubbles, and pop-up panels |
| `mish/eyes.py` | Controls eye movement, blinking, and expressions |
| `mish/config.py` | Stores Light/Dark theme colors, model settings, and user preferences |
| `mish/store.py` | Saves your local memory, tasks, and settings |
| `mish/win_utils.py` | Handles Windows screen capture/reading, screen edges, and multi-monitor positioning |
| `mish/integrations/blackboard.py` | Pulls deadlines from your Blackboard calendar link |
| `mish/integrations/google_sync.py` | Connects Google Calendar and Gmail |
| `mish/integrations/microsoft_sync.py` | Connects Outlook and Microsoft To Do |
| `mish/integrations/scheduler.py` | Runs background checks so Mish never freezes |

Local AI model weights (`.gguf`), personal memory (`memory.json`), and account tokens are **not** included in this folder.

## How to set it up

1. Download or clone this folder onto your PC.
2. Open **PowerShell** inside the `Mish` folder and install the required packages:
   ```powershell
   pip install pillow apscheduler sqlalchemy
   ```
   *(Install `llama-cpp-python` and place your `.gguf` model in `models/` if using local AI chat.)*
3. Start Mish:
   - **Normal mode (no console window):** Double-click `mish.pyw`
   - **With debug logs:** Run `python -u mish.pyw` in PowerShell

## How to use Mish

- **Chat with him:** Right-click Mish and open the chat/command bar to ask questions or give commands.
- **Move him:** Click and drag his dark body anywhere on your screen.
- **Tuck him away:** Drag him to the **top-center** of your monitor to dock him into a compact top tab. Pull down on the tab to pop him back out.
- **Open menus:** Right-click Mish (or click the small gear icon when hovering over him) to open **Active Tasks**, **Connections**, or **Customize**.
- **Close any window:** Click `✕` in the top-right corner or press `Esc`.

## A reminder

- Your screen captures, chat prompts, tasks, and tokens stay **only on your own computer**.
- Never upload your personal `credentials.json`, tokens, `memory.json`, or large `.gguf` model files if you share your folder online.
