# Mish

Your own desktop companion that lives **right on your Windows screen**. It runs locally on your PC, tracks your cursor with spring-physics eyes, keeps up with your tasks and school deadlines, and reacts to what you're doing.

## What it can do

- Sit on your desktop as a dark squircle face that follows your mouse and reacts with different eye expressions and colors
- Tuck neatly into the top-center of your screen like a notch (and stay floating when placed in the corners)
- Keep a local to-do list (`Active Tasks`) with due-date reminders and built-in focus/break timers
- Sync deadlines and schedules from **Blackboard** (iCal feed), **Google** (Calendar + Gmail), and **Microsoft** (Outlook + To Do)
- Switch between **Auto**, **Dark**, and **Light** HUD themes and customize eye shapes and colors live (`Mish // Studio`)
- Talk to you using built-in lines or an optional local AI model running offline on your PC

## The PC it runs on

Built and tested on Windows. Since Mish is lightweight Python + Pillow graphics, it runs smoothly on most Windows laptops and desktops.

### Minimum specs you need

- Any Windows 10 or Windows 11 PC (64-bit)
- Python 3.10 or newer
- 4 GB of RAM or more (8 GB+ recommended if you also run a local AI model)
- Internet only for the initial Python package install and optional Blackboard / Google / Microsoft sync

## What is in this folder

| File / Folder | What it is |
|---|---|
| `mish.pyw` | The main file you double-click or run to start Mish (no black terminal window) |
| `mish/ui.py` | Draws Mish's face, eyes, animations, and all pop-up panels |
| `mish/brain.py` | Mish's mood, focus timers, task logic, and replies |
| `mish/eyes.py` | Spring physics and eye expressions |
| `mish/config.py` | Light/Dark theme colors and saved settings |
| `mish/store.py` | Saves your tasks and preferences locally |
| `mish/win_utils.py` | Windows screen edge, click-through, and multi-monitor helpers |
| `mish/integrations/blackboard.py` | Pulls deadlines from your Blackboard calendar link |
| `mish/integrations/google_sync.py` | Connects Google Calendar and Gmail |
| `mish/integrations/microsoft_sync.py` | Connects Outlook and Microsoft To Do |
| `mish/integrations/scheduler.py` | Runs background checks so Mish never freezes |

Local settings, task files (`memory.json`), and AI model weights are **not** included in this folder.

## How to set it up

1. Download or clone this folder onto your PC.
2. Open **PowerShell** inside the `Mish` folder and install the required packages:
   ```powershell
   pip install pillow apscheduler sqlalchemy
   ```
3. Start Mish:
   - **Normal mode (no console window):** Double-click `mish.pyw`
   - **With debug logs:** Run `python -u mish.pyw` in PowerShell

## How to use Mish

- **Move him:** Click and drag his dark squircle body anywhere on screen.
- **Tuck him away:** Drag him to the **top-center** of your monitor to dock him into a compact top notch. Pull down on the notch to pop him back out.
- **Open menus:** Right-click Mish (or click the small gear icon when hovering over him) to open **Active Tasks**, **Connections**, or **Customize HUD**.
- **Close any panel:** Click `✕` in the top-right corner or press `Esc`.

## A reminder

- Your tasks, Blackboard link, and Google/Microsoft tokens are saved **only on your own computer**.
- Never upload your personal `credentials.json`, tokens, or `settings.json` if you share your folder online.
