from __future__ import annotations

import multiprocessing as mp
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MUTEX_NAME = "Global\\MihoyoBBSAutoSignerSingleInstance"


def _already_running() -> bool:
    if sys.platform != "win32":
        return False
    ctypes = __import__("ctypes")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() == 183


def main() -> None:
    mp.freeze_support()
    if _already_running():
        return
    from tray_app import main as tray_main

    tray_main()


if __name__ == "__main__":
    main()
