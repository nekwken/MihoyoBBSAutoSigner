"""DPI 感知与像素换算。

高分屏上字体发虚的根因是进程未声明 DPI 感知：Windows 会把整个窗口按位图拉伸。
必须在创建任何 Tk 窗口之前调用 enable_dpi_awareness()，随后用 px() 换算设计稿里的
像素值（字号以「点」为单位，Tk 会按 DPI 自动缩放，无需换算）。
"""
from __future__ import annotations

import ctypes
import sys

_SCALE = 1.0
_PER_MONITOR_AWARE_V2 = -4


def enable_dpi_awareness() -> bool:
    """声明进程 DPI 感知（优先 per-monitor v2）；必须在建窗之前调用。"""
    if sys.platform != "win32":
        return False
    try:
        fn = ctypes.windll.user32.SetProcessDpiAwarenessContext
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = ctypes.c_bool
        if fn(ctypes.c_void_p(_PER_MONITOR_AWARE_V2)):
            return True
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
        return True
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()  # system aware
        return True
    except Exception:
        return False


def set_scale_from_dpi(dpi: float) -> float:
    """按给定 DPI 设定缩放系数（96 DPI = 1.0）。"""
    global _SCALE
    try:
        _SCALE = max(1.0, round(float(dpi) / 96.0, 3))
    except Exception:
        _SCALE = 1.0
    return _SCALE


def apply_dpi_to_tk(tk_root, dpi: float) -> float:
    """设定缩放系数并让 Tk 的字体缩放与之匹配（字号以点为单位，随 DPI 放大）。"""
    value = set_scale_from_dpi(dpi)
    try:
        tk_root.tk.call("tk", "scaling", float(dpi) / 72.0)
    except Exception:
        pass
    return value


def window_dpi(tk_root) -> int:
    """取窗口所在显示器的 DPI（失败返回 0）。"""
    if sys.platform != "win32":
        return 0
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(tk_root.winfo_id()) or tk_root.winfo_id()
        return int(user32.GetDpiForWindow(hwnd) or 0)
    except Exception:
        return 0


def set_scale_from(tk_root) -> float:
    """按当前显示器的实际 DPI 设定缩放系数（96 DPI = 1.0）。"""
    dpi = 0
    try:
        dpi = float(tk_root.winfo_fpixels("1i"))
    except Exception:
        dpi = 0
    if not dpi:
        dpi = float(window_dpi(tk_root) or 96)
    value = set_scale_from_dpi(dpi)
    try:
        tk_root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        pass
    return value


def scale() -> float:
    return _SCALE


def px(value: float) -> int:
    """把设计稿像素换算为当前 DPI 下的像素（0 保持 0）。"""
    try:
        return max(0, int(round(float(value) * _SCALE)))
    except Exception:
        return int(value)
