"""DPI 感知与像素换算。

高分屏上字体发虚的根因是进程未声明 DPI 感知：Windows 会把整个窗口按位图拉伸。
必须在创建任何 Tk 窗口之前调用 enable_dpi_awareness()，随后用 px() 换算设计稿里的
像素值（字号以「点」为单位，Tk 会按 DPI 自动缩放，无需换算）。
"""
from __future__ import annotations

import ctypes
import sys

_SCALE = 1.0
_DENSITY = 1.0               # 排版密度：小屏/高缩放下整体收紧（1.0 = 设计稿原尺寸）
_USER_SCALE = 1.0            # 用户缩放（拖动窗口等比缩放 / 「界面缩放」选项）
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


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong)]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_RECT), ctypes.c_void_p)


def _monitor_dpi(hmon) -> int:
    try:
        x, y = ctypes.c_uint(), ctypes.c_uint()
        ctypes.windll.shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(x), ctypes.byref(y))
        return int(x.value)
    except Exception:
        return 0


def _intersects(win: "_RECT", mon: "_RECT") -> bool:
    return (min(win.right, mon.right) > max(win.left, mon.left)
            and min(win.bottom, mon.bottom) > max(win.top, mon.top))


def _straddles_monitors(hwnd, nearest_dpi: int) -> bool:
    """窗口是否同时压在两块 **DPI 不同** 的显示器上。

    只压一块（哪怕被屏幕边缘裁掉一截）不算跨屏 —— 被裁掉时仍需按该显示器重排，
    否则窗口会比屏幕还大却一直不调整。
    """
    try:
        user32 = ctypes.windll.user32
        win = _RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(win)):
            return False
        found = []

        def _cb(hmon, _hdc, _rect, _data):
            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(info)) and _intersects(win, info.rcMonitor):
                found.append(_monitor_dpi(hmon) or nearest_dpi)
            return 1

        user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(_cb), None)
        return len({d for d in found}) > 1
    except Exception:
        return False


def monitor_dpi(tk_root) -> tuple[int, bool]:
    """窗口的 (DPI, 是否横跨两块 DPI 不同的显示器)。

    横跨时不动：Windows 按「面积占比」判定窗口归属，重排会改变窗口尺寸、衬得 DPI
    来回跳，反复重排直到界面卡死（实测过）。只压一块显示器（含被边缘裁掉）则正常重排。
    """
    if sys.platform != "win32":
        return 0, False
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetAncestor(tk_root.winfo_id(), 2) or tk_root.winfo_id()  # GA_ROOT
        hmon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        if not hmon:
            return 0, False
        dpi = _monitor_dpi(hmon) or int(user32.GetDpiForWindow(hwnd) or 0)
        return dpi, _straddles_monitors(hwnd, dpi)
    except Exception:
        return 0, False


def monitor_work_area(tk_root) -> tuple[int, int]:
    """窗口所在显示器的工作区 (宽, 高)；失败返回 (0, 0)。

    Tk 的 winfo_screenwidth/height 只反映主显示器，副屏布局会算错。
    """
    if sys.platform != "win32":
        return 0, 0
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetAncestor(tk_root.winfo_id(), 2) or tk_root.winfo_id()
        hmon = user32.MonitorFromWindow(hwnd, 2)
        if not hmon:
            return 0, 0
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return 0, 0
        work = info.rcWork
        return max(0, work.right - work.left), max(0, work.bottom - work.top)
    except Exception:
        return 0, 0


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


def set_density(value: float) -> float:
    """设定排版密度（0.6~1.0）。px() 与界面字号都会一并跟着收紧。"""
    global _DENSITY
    try:
        _DENSITY = max(0.6, min(1.0, round(float(value), 3)))
    except Exception:
        _DENSITY = 1.0
    return _DENSITY


def density() -> float:
    return _DENSITY


def design_scale() -> float:
    """设计稿像素 → 实际像素的总系数（DPI 缩放 × 排版密度 × 用户缩放）。"""
    return _SCALE * _DENSITY * _USER_SCALE


def logical_scale() -> float:
    """字号要用的系数：排版密度 × 用户缩放。

    （DPI 那一份由 Tk 的 tk scaling 负责——字号是「点」，Tk 会按 DPI 自己放大。）
    """
    return _DENSITY * _USER_SCALE


def set_user_scale(value: float) -> float:
    """用户缩放倍数（等比缩放窗口时用）。"""
    global _USER_SCALE
    try:
        _USER_SCALE = max(0.7, min(2.5, round(float(value), 3)))
    except Exception:
        _USER_SCALE = 1.0
    return _USER_SCALE


def user_scale() -> float:
    return _USER_SCALE


def min_density(dpi: float | None = None) -> float:
    """密度下限：10pt 字号换算到物理像素后不小于约 11px，再小就看不清了。"""
    try:
        value = float(dpi or (96.0 * _SCALE))
        return round(max(0.6, min(1.0, 11.0 / (10.0 * value / 72.0))), 3)
    except Exception:
        return 0.8


def window_frame_height(tk_root) -> int:
    """窗口标题栏 + 边框占用的高度（物理像素）；取不到时按 DPI 估算。"""
    if sys.platform != "win32":
        return 0
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetAncestor(tk_root.winfo_id(), 2) or tk_root.winfo_id()
        win = _RECT()
        client = _RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(win)) and \
                user32.GetClientRect(hwnd, ctypes.byref(client)):
            outer = (win.bottom - win.top) - (client.bottom - client.top)
            if outer >= 0:
                return int(outer)
    except Exception:
        pass
    return int(round(38 * _SCALE))


def px(value: float) -> int:
    """把设计稿像素换算为当前 DPI 与排版密度下的像素（0 保持 0）。"""
    try:
        return max(0, int(round(float(value) * design_scale())))
    except Exception:
        return int(value)
