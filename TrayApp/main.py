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
    # 必须在任何 Tk 窗口创建之前声明 DPI 感知，否则高分屏下整窗被系统拉伸导致字体发虚
    from dpi import enable_dpi_awareness

    enable_dpi_awareness()
    if _already_running():
        _signal_running_instance()
        return
    from tray_app import main as tray_main

    tray_main()


def _signal_running_instance() -> None:
    """已有实例在跑时：请求其弹出设置页（再次启动=用户明确要看界面）。"""
    try:
        from app_config import HOME

        (HOME / "show_request.flag").write_text("", encoding="utf-8")
    except Exception:
        pass


if __name__ == "__main__":
    main()
