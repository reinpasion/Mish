"""Mish - a cheeky 16-bit desktop companion with a bundled local brain.

Entry point. The code lives in the mish/ package (split from mish.pyw by split_mish.py):
  config, win_utils, store, brain, eyes, core, ui.

The DLL search paths below MUST be registered before anything imports llama_cpp or the win32
extensions (they are imported by the mish.* modules), otherwise Windows can fail with an
access violation reading 0x0000000000000000.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# The DLL search paths MUST be registered before llama_cpp / win32 extensions are imported,
# otherwise Windows can fail with "access violation reading 0x0000000000000000".
if getattr(sys, "frozen", False):
    _root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    os.chdir(_root)
    _dll_dirs = [_root] + ([_root / "llama_cpp"] if (_root / "llama_cpp").exists() else [])
    for _d in _dll_dirs:
        try:
            os.add_dll_directory(str(_d))
        except Exception:
            pass
    os.environ["PATH"] = os.pathsep.join([str(d) for d in _dll_dirs] + [os.environ.get("PATH", "")])

# Package imports come AFTER the DLL setup on purpose (hence the noqa).
from mish.win_utils import log  # noqa: E402
from mish.ui import main  # noqa: E402


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        log("CRASH:", traceback.format_exc())
        raise
