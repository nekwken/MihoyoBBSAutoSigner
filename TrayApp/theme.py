"""外观主题：浅色/深色调色板、Windows 11 Mica 背景、深色标题栏、系统主题跟随。

用法：
    theme.set_mode("system" | "light" | "dark")   # 设定模式
    p = theme.palette()                            # 当前调色板（dict）
    theme.apply_window_style(tk_root)              # 套用深色标题栏 + Mica
    theme.system_uses_light_theme()                # 读取系统主题
"""
from __future__ import annotations

import ctypes
import sys

# ── 调色板 ────────────────────────────────────────────────────────────────
LIGHT = {
    "name": "light",
    "panel": "#F3F4F6",
    "card": "#FFFFFF",
    "line": "#E5E7EB",
    "text": "#1F2937",
    "muted": "#6B7280",
    "accent": "#2563EB",
    "accent_text": "#FFFFFF",
    "danger": "#DC2626",
    "warn": "#B45309",
    "entry_bg": "#FFFFFF",
    "entry_fg": "#111827",
    "btn_bg": "#1F2937",
    "btn_fg": "#FFFFFF",
    "tab_bg": "#E5E7EB",
    "tab_fg": "#4B5563",
    "tab_active_bg": "#F3F4F6",
}

DARK = {
    "name": "dark",
    "panel": "#202024",
    "card": "#2A2A31",
    "line": "#3A3A43",
    "text": "#E5E7EB",
    "muted": "#A9B0BD",   # 深色下次要文字（原 #9CA3AF 偏暗，可读性差）
    "accent": "#3B82F6",
    "accent_text": "#FFFFFF",
    "danger": "#F87171",
    "warn": "#FBBF24",
    "entry_bg": "#17171A",
    "entry_fg": "#E5E7EB",
    "btn_bg": "#3F3F46",
    "btn_fg": "#E5E7EB",
    "tab_bg": "#26262C",
    "tab_fg": "#A1A1AA",
    "tab_active_bg": "#202024",
}

_MODE = "system"          # system | light | dark
_RENDER = "light"         # 实际渲染用的调色板
# Mica 需要「颜色键透明」配合，合成不生效时会在圆角处露出近黑色块；
# 默认关闭，保留开关以便后续在确认合成稳定的环境里启用。
MICA_ENABLED = False
_MICA = False
_MICA_KEY = "#0A0B0C"     # 颜色键：该颜色被挖成透明，露出 Mica 背景（调色板中不得使用）


def mica_key() -> str:
    return _MICA_KEY


def mica_enabled() -> bool:
    return _MICA


def mode() -> str:
    return _MODE


def render_mode() -> str:
    return _RENDER


def system_uses_light_theme() -> bool:
    """读取系统「应用模式」：True=浅色。读取失败按浅色处理。"""
    if sys.platform != "win32":
        return True
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return bool(int(value))
    except Exception:
        return True


def windows_supports_mica() -> bool:
    """Windows 11 22H2（build 22621）及以上支持 Mica 背景。"""
    if sys.platform != "win32":
        return False
    try:
        class OSV(ctypes.Structure):
            _fields_ = [
                ("dwOSVersionInfoSize", ctypes.c_ulong),
                ("dwMajorVersion", ctypes.c_ulong),
                ("dwMinorVersion", ctypes.c_ulong),
                ("dwBuildNumber", ctypes.c_ulong),
                ("dwPlatformId", ctypes.c_ulong),
                ("szCSDVersion", ctypes.c_wchar * 128),
            ]

        info = OSV()
        info.dwOSVersionInfoSize = ctypes.sizeof(OSV)
        ctypes.windll.ntdll.RtlGetVersion(ctypes.byref(info))
        return (info.dwMajorVersion, info.dwBuildNumber) >= (10, 22621)
    except Exception:
        return False


def set_mode(value: str) -> str:
    """设定外观模式并刷新当前渲染调色板（不触碰窗口）。"""
    global _MODE, _RENDER, _MICA
    _MODE = value if value in ("system", "light", "dark") else "system"
    if _MODE == "system":
        _RENDER = "light" if system_uses_light_theme() else "dark"
    else:
        _RENDER = _MODE
    _MICA = bool(MICA_ENABLED and windows_supports_mica())
    return _RENDER


def palette() -> dict:
    """当前调色板。

    正文区域一律使用不透明底色（保证任何壁纸下的可读性）；
    启用 Mica 时额外提供 window = 颜色键，用于窗口最外层留白（即 Mica 窗框）。
    """
    base = dict(DARK if _RENDER == "dark" else LIGHT)
    base["window"] = _MICA_KEY if _MICA else base["panel"]
    return base


def _hwnd(tk_root):
    """取窗口的顶层 HWND（GA_ROOT 对普通窗口和带 owner 的对话框都正确）。"""
    try:
        user32 = ctypes.windll.user32
        hwnd = tk_root.winfo_id()
        return user32.GetAncestor(hwnd, 2) or hwnd      # GA_ROOT = 2
    except Exception:
        return None


def set_dark_titlebar(tk_root, dark: bool | None = None) -> None:
    """设置窗口标题栏深浅（默认跟随当前渲染主题）。"""
    if sys.platform != "win32":
        return
    dark = (_RENDER == "dark") if dark is None else dark
    hwnd = _hwnd(tk_root)
    if not hwnd:
        return
    value = ctypes.c_int(1 if dark else 0)
    for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE（新旧两个编号）
        try:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
        except Exception:
            pass


def _colorref(color: str) -> int:
    """#RRGGBB → COLORREF（0x00BBGGRR，DWM 的颜色参数是反的）。"""
    text = str(color).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return 0
    try:
        r, g, b = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return 0
    return (b << 16) | (g << 8) | r


def set_caption_color(tk_root, color: str | None = None, text: str | None = None) -> bool:
    """标题栏与窗口主体同色（Win11 的 DWMWA_CAPTION_COLOR / TEXT_COLOR）。

    只靠 DWMWA_USE_IMMERSIVE_DARK_MODE 不够稳：系统给的是中性色（浅色 #F3F3F3、
    深色 #202020），而我们的面板色带蓝调（#F3F4F6 / #202024），差一点点就成一条
    色带；而且窗口已经显示之后再改那个属性经常不生效（实测某次实例读回 0）。
    """
    if sys.platform != "win32" or _MICA:        # Mica 时标题栏交给系统背景
        return False
    hwnd = _hwnd(tk_root)
    if not hwnd:
        return False
    p = palette()
    ok = False
    for attr, value in ((35, color or p["window"]), (36, text or p["text"])):
        try:
            v = ctypes.c_uint(_colorref(value))
            rc = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))
            ok = ok or rc == 0
        except Exception:
            pass
    return ok


def refresh_frame(tk_root) -> None:
    """强制重画非客户区：改完标题栏属性不动一下窗口，DWM 可能一直用旧边框。"""
    if sys.platform != "win32":
        return
    hwnd = _hwnd(tk_root)
    if not hwnd:
        return
    SWP = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020   # NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED
    try:
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP)
    except Exception:
        pass


def apply_mica(tk_root) -> bool:
    """给窗口套用 Mica 背景 + 颜色键透明（需要 Windows 11 22H2+）。"""
    if sys.platform != "win32" or not _MICA:
        return False
    hwnd = _hwnd(tk_root)
    if not hwnd:
        return False
    ok = False
    try:
        value = ctypes.c_int(2)  # DWMSBT_MAINWINDOW
        ok = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 38, ctypes.byref(value), ctypes.sizeof(value)) == 0
    except Exception:
        ok = False
    if not ok:
        return False
    try:
        user32 = ctypes.windll.user32
        GWL_EXSTYLE, WS_EX_LAYERED, LWA_COLORKEY = -20, 0x00080000, 0x00000001
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
        key = _MICA_KEY.lstrip("#")
        bgr = int(key[4:6] + key[2:4] + key[0:2], 16)  # COLORREF = 0x00BBGGRR
        user32.SetLayeredWindowAttributes(hwnd, bgr, 0, LWA_COLORKEY)
    except Exception:
        pass
    return True


def round_window_region(tk_root, radius: int = 8) -> bool:
    """用窗口区域裁剪出圆角（不依赖颜色键，适合无边框弹层）。"""
    if sys.platform != "win32":
        return False
    hwnd = _hwnd(tk_root)
    if not hwnd:
        return False
    try:
        gdi = ctypes.windll.gdi32
        user32 = ctypes.windll.user32
        w = tk_root.winfo_width()
        h = tk_root.winfo_height()
        if w <= 1 or h <= 1:
            return False
        rgn = gdi.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius * 2, radius * 2)
        user32.SetWindowRgn(hwnd, rgn, True)
        return True
    except Exception:
        return False


def set_rounded_corners(tk_root, enable: bool = True) -> None:
    """圆角窗口（Windows 11：DWMWA_WINDOW_CORNER_PREFERENCE）。"""
    if sys.platform != "win32":
        return
    hwnd = _hwnd(tk_root)
    if not hwnd:
        return
    try:
        value = ctypes.c_int(2 if enable else 1)  # 2=ROUND 1=DONOTROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 33, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


def apply_window_style(tk_root, mica: bool = False) -> None:
    """统一入口：圆角 + 标题栏深浅与配色（所有窗口）+ Mica（仅主窗口）。"""
    set_rounded_corners(tk_root)
    set_dark_titlebar(tk_root)
    set_caption_color(tk_root)
    if mica:
        apply_mica(tk_root)
    refresh_frame(tk_root)
