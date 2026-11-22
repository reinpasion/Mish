# Mish

Mish is a little desktop companion for Windows. He reacts to what you’re doing and can chat with you offline.

- **Stay focused.** Start a focus timer or Pomodoro and choose how much Mish nudges you.
- **Keep track of tasks.** Add to-dos, get deadline reminders, and sync with Blackboard, Google Calendar, or Microsoft To Do.
- **Ask for a hand.** Have Mish open an app, remember a detail, or summarize a file.

Press **Alt+M** to open **Ask Mish**. Try `focus 25`, `open calculator`, or `help`.

## Setup

You’ll need **64-bit Windows 11** and **Python 3.12**. For local chat, **8 GB of RAM** is recommended.

1. Download or clone this repository and open PowerShell in the `Mish` folder.
2. Run both commands.

   ```powershell
   python -m pip install pillow psutil plyer pywin32 apscheduler sqlalchemy icalendar requests google-api-python-client google-auth-oauthlib msal
   python -m pip install llama-cpp-python --only-binary=llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
   ```

3. Put your `.gguf` file inside the `models` folder. You can use [MishV6](https://huggingface.co/reiHien/MishV6/tree/main) or another GGUF chat model compatible with `llama-cpp-python`.
4. Double-click `mish.pyw` to start Mish.

To link an account, right-click Mish and open **Connections**.

## What’s in the folder

| File or folder | What it does |
|---|---|
| `mish.pyw` | Starts Mish |
| `mish/` | Holds the desktop UI, animations, commands, and local chat code |
| `mish/integrations/` | Handles Blackboard, Google, and Microsoft connections |
| `canned_lines.json` | Holds Mish’s built-in lines for everyday reactions |
| `models/` | Holds your downloaded chat model |

## What to expect

The first chat reply can take a little longer while Mish loads the model. Replies can sometimes be wrong.

Mish is a work in progress. If something breaks, open a GitHub issue with what you did and the relevant lines from `Mish.log`.
