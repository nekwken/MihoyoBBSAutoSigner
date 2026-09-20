from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

import autostart
from app_config import (
    APP_NAME,
    APP_VERSION,
    BBS_ROOT,
    BOARD_ROWS,
    ICON_PATH,
    TrayConfig,
)
from make_icon import ensure_icon
from account_store import format_account_status, load_account_info, logout_and_clear
from device_identity import ensure_device
from dpi import (apply_dpi_to_tk, density as _density, min_density, monitor_dpi,
                 monitor_work_area, px as _px, set_density, set_scale_from,
                 window_frame_height)
from widgets import (PillSwitch, ReorderList, RoundedCard, RoundedSelect, ScrollPage,
                     TabBar, WheelPicker)

# 排版密度阶梯：从松到紧，取第一个内容装得下的（下限由 dpi.min_density 决定）
DENSITY_STEPS = (1.0, 0.94, 0.88, 0.82, 0.76, 0.70, 0.66, 0.62)

# 板块表列宽（设计稿像素）：表头与每行共用，保证两列开关严格对齐
BOARD_COLUMNS = ((0, 116), (1, 92), (2, 92))
import theme as _theme
from runner import read_log_tail, run_checkin
from scheduler import parse_hhmm
from stoken_login import (
    StokenResult,
    exchange_cookie,
    fetch_account_profile,
    login_by_sms,
    login_verify,
    send_sms,
    write_bbs_config,
)

# 外观：颜色与字体由 theme.py 提供，主题切换时由 apply_theme() 刷新这些模块级变量
UI_FAMILY = "Microsoft YaHei UI"


def _ui_font(size: float, weight: str | None = None) -> tuple:
    """界面字体：字号按排版密度收紧（单位是点，Tk 会再按 DPI 放大；Tk 只吃整数）。"""
    try:
        size = max(7, int(round(float(size) * _density())))
    except Exception:
        pass
    return (UI_FAMILY, size, weight) if weight else (UI_FAMILY, size)


def _refresh_colors() -> None:
    global ACCENT, MUTED, PANEL, LINE, CARD, TEXT, DANGER, WARN
    global ENTRY_BG, ENTRY_FG, WINDOW_BG, BTN_BG, BTN_FG
    p = _theme.palette()
    ACCENT = p["accent"]
    MUTED = p["muted"]
    PANEL = p["panel"]
    LINE = p["line"]
    CARD = p["card"]
    TEXT = p["text"]
    DANGER = p["danger"]
    WARN = p["warn"]
    ENTRY_BG = p["entry_bg"]
    ENTRY_FG = p["entry_fg"]
    WINDOW_BG = p["window"]
    BTN_BG = p["btn_bg"]
    BTN_FG = p["btn_fg"]


ACCENT = "#2563EB"
MUTED = "#6B7280"
PANEL = "#F3F4F6"
LINE = "#E5E7EB"
CARD = "#FFFFFF"
TEXT = "#1F2937"
DANGER = "#DC2626"
WARN = "#B45309"
ENTRY_BG = "#FFFFFF"
ENTRY_FG = "#111827"
WINDOW_BG = "#F3F4F6"
BTN_BG = "#1F2937"
BTN_FG = "#FFFFFF"
_refresh_colors()
ABOUT_TEXT = f"""米游社自动签到器
MihoyoBBSAutoSigner {APP_VERSION}

米游社 / 米哈游游戏辅助签到托盘
功能：社区打卡 · 游戏签到 · 云游戏签到 · 定时任务 · 开机自启 · 短信登录

签到引擎：Womsxd/MihoyoBBSTools
接口与错误码参考社区公开整理

图形验证仅弹窗由用户完成，不会自动绕过极验。

开源仓库：github.com/nekwken/MihoyoBBSAutoSigner
许可证：MIT

免责声明：
本工具仅供个人学习与自用，请遵守米哈游用户协议与当地法律。
勿用于批量账号、商业牟利或干扰服务的行为。非官方项目。
"""


def _bind_wheel(widget: tk.Widget, getter, setter, lo: int, hi: int, fmt: str = "{:02d}") -> None:
    def on_wheel(event: tk.Event) -> None:
        step = 1 if getattr(event, "delta", 0) > 0 else -1
        try:
            cur = int(getter())
        except Exception:
            cur = lo
        nxt = cur + step
        if nxt < lo:
            nxt = hi
        if nxt > hi:
            nxt = lo
        setter(fmt.format(nxt))

    widget.bind("<MouseWheel>", on_wheel)
    widget.bind("<Button-4>", lambda e: on_wheel(type("E", (), {"delta": 120})()))
    widget.bind("<Button-5>", lambda e: on_wheel(type("E", (), {"delta": -120})()))


class SettingsWindow:
    def __init__(
        self,
        cfg: TrayConfig,
        on_saved: Callable[[TrayConfig], None],
        on_run_now: Callable[[], bool | None],
        on_request_quit: Callable[[], None] | None = None,
        status_text: str = "",
        master: tk.Misc | None = None,
    ) -> None:
        self.cfg = cfg
        self.on_saved = on_saved
        self.on_run_now = on_run_now
        self.on_request_quit = on_request_quit
        self.status_text = status_text
        self._login_busy = False
        self.quit_requested = False
        self._sms_left = 0
        self._sms_after = None
        self._btn_sms = None
        self._show_flag = False
        self._ui_queue: queue.Queue = queue.Queue()
        self._checkin_running = False
        self._dots = 0
        self._wait_job = None
        self.root = tk.Toplevel(master) if master is not None else tk.Tk()
        set_scale_from(self.root)
        dpi0, _covered = monitor_dpi(self.root)
        self._dpi_now = int(dpi0 or int(self.root.winfo_fpixels("1i") or 96))
        self._dpi_pending = 0
        self._dpi_since = 0.0
        self._last_dpi_apply = 0.0
        self._rebuilding = False
        _theme.set_mode(getattr(self.cfg, "ui_theme", "system"))
        _refresh_colors()
        self.root.title("米游社自动签到器 · 设置")
        self._apply_window_icon(self.root)
        self._apply_window_metrics()
        self.root.configure(bg=WINDOW_BG)
        self.root.attributes("-topmost", True)
        self.root.withdraw()
        self._board_vars: dict[int, tk.BooleanVar] = {}
        self.var_hour = tk.StringVar(value="09")
        self.var_minute = tk.StringVar(value="30")
        self._build()
        self._fit_layout()
        try:
            self.root.bind_all("<MouseWheel>", self._on_wheel, add="+")
        except Exception:
            pass
        try:
            self.root.bind("<Map>", self._on_window_mapped, add="+")
        except Exception:
            pass
        self._restyle_window()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_window_mapped(self, event) -> None:
        """窗口每次显示都重新套用边框样式。

        DWM 对「窗口显示之后再改标题栏属性」经常不生效，所以显示时补一次。
        """
        if getattr(event, "widget", None) is not self.root:
            return
        self._restyle_window()

    def _restyle_window(self) -> None:
        try:
            _theme.apply_window_style(self.root, mica=True)
        except Exception:
            pass

    def _on_theme_pick(self) -> None:
        """切换外观：立即生效（保存后随配置持久化）。"""
        mode = {"跟随系统": "system", "浅色": "light", "深色": "dark"}.get(
            self.var_theme.get(), "system")
        _theme.set_mode(mode)
        self.cfg.ui_theme = mode
        try:
            self.cfg.save()
        except Exception:
            pass
        self.apply_theme()

    def _apply_window_metrics(self) -> None:
        """给窗口一个临时尺寸：高度先占满当前显示器可用空间，便于测量固定框架高度。"""
        try:
            work_w, work_h = monitor_work_area(self.root)
            frame_h = window_frame_height(self.root)
            screen_w = work_w or self.root.winfo_screenwidth()
            screen_h = work_h or self.root.winfo_screenheight()
            w = max(_px(520), min(_px(660), screen_w - _px(48)))
            h = max(_px(420), screen_h - frame_h - _px(24))
            self.root.geometry(f"{w}x{h}")
            self.root.minsize(_px(520), _px(380))
        except Exception:
            pass

    # ── 自适应排版：密度 → 尺寸 → 必要时滚动 ─────────────────────────────
    @staticmethod
    def _pack_padding(widget) -> int:
        """控件在 pack 里占用的上下外边距之和（单个数值表示上下各一份）。"""
        try:
            value = widget.pack_info().get("pady", 0)
        except Exception:
            return 0
        if isinstance(value, (list, tuple)):
            try:
                return sum(int(v) for v in value[:2])
            except Exception:
                return 0
        try:
            parts = str(value).split()
            if len(parts) == 1:
                return 2 * int(parts[0])
            return sum(int(v) for v in parts[:2])
        except Exception:
            return 0

    def _chrome_height(self) -> int:
        """标题、页签、底部按钮等固定框架占用的高度（用请求高度算，窗口还没显示也能测）。"""
        total = 0
        try:
            self.root.update_idletasks()
            for child in self.root.winfo_children():
                if child is self.content:
                    total += self._pack_padding(child)
                    continue
                total += child.winfo_reqheight() + self._pack_padding(child)
        except Exception:
            return _px(170)
        return max(_px(120), total)

    def _content_height(self) -> int:
        """当前密度下最高的页面内容高度（物理像素）。"""
        self.root.update_idletasks()
        best = 0
        for page in getattr(self, "_pages", []):
            try:
                best = max(best, page.inner.winfo_reqheight())
            except Exception:
                pass
        return best

    def _content_width(self) -> int:
        best = 0
        for page in getattr(self, "_pages", []):
            try:
                best = max(best, page.inner.winfo_reqwidth())
            except Exception:
                pass
        return best

    def _set_density(self, value: float) -> None:
        """切换排版密度：像素与字号都是建界面时写死的，必须重建。"""
        set_density(value)
        self._rebuild_ui(refresh=False)

    def _sync_pages(self) -> None:
        for page in getattr(self, "_pages", []):
            sync = getattr(page, "sync", None)
            if sync is not None:
                try:
                    sync()
                except Exception:
                    pass

    def _fit_layout(self) -> None:
        """按当前显示器可用空间选排版密度并定窗口尺寸。

        密度自松到紧取第一个内容装得下的（下限见 dpi.min_density，再紧字号就看不清了）；
        都不装不下时窗口取满可用高度，由 ScrollPage 提供滚动 —— 内容不会被裁掉。
        """
        try:
            self.root.update_idletasks()
            work_w, work_h = monitor_work_area(self.root)
            screen_w = work_w or self.root.winfo_screenwidth()
            screen_h = work_h or self.root.winfo_screenheight()
            frame_h = window_frame_height(self.root)
            margin = _px(24)
            avail_w = max(_px(520), screen_w - margin)
            avail_client = max(_px(380), screen_h - frame_h - margin)
            floor = min_density(self._dpi_now)
            chrome = self._chrome_height()
            viewport = max(_px(240), avail_client - chrome)
            for step in DENSITY_STEPS:
                if step < floor - 1e-6:
                    break
                if abs(_density() - step) > 1e-6:
                    self._set_density(step)
                    chrome = self._chrome_height()
                    viewport = max(_px(240), avail_client - chrome)
                if self._content_height() <= viewport:
                    break
            need = self._content_height()
            w = min(max(_px(660), self._content_width() + _px(56)), avail_w)
            h = min(max(_px(480), need + chrome), avail_client)
            self.root.geometry(f"{w}x{h}")
            self.root.resizable(False, False)
            self.root.update_idletasks()
            self._sync_pages()
        except Exception:
            traceback.print_exc()

    def _on_wheel(self, event) -> str | None:
        """页面内容装不下时才用滚轮翻页；装得下就不拦截，保持完全不可滚动。"""
        try:
            if event.widget.winfo_toplevel() is not self.root:
                return None
            page = self._pages[int(getattr(self, "_tab_index", 0))]
            if not page.is_scrollable():
                return None
            step = -int(event.delta / 120) or -1
            page.scroll_by(step * 3)
            return "break"
        except Exception:
            return None

    def _watch_dpi(self) -> None:
        """窗口被拖到不同缩放比例的显示器时重新排版；跟随系统主题变化。

        窗口横跨两块缩放不同的显示器时不动：Windows 按窗口面积判定所属显示器，
        重排会改变窗口尺寸，衬得 DPI 左右来回跳，于是反复重排直到界面卡死。
        """
        now = time.time()
        if not getattr(self, "_rebuilding", False):
            dpi, covered = monitor_dpi(self.root)
            changed = bool(dpi) and abs(dpi - self._dpi_now) >= 24
            if not changed:
                self._dpi_pending = 0
            elif covered < 0.98:
                # 卡在两块显示器之间：等拖到任一侧稳定下来再重排
                self._dpi_pending = 0
            elif dpi != self._dpi_pending:
                self._dpi_pending, self._dpi_since = dpi, now
            elif now - self._dpi_since >= 0.3 and now - self._last_dpi_apply >= 1.5:
                self._apply_dpi_change(dpi)
        if now - getattr(self, "_theme_checked", 0.0) < 3.0:
            return
        self._theme_checked = now
        if _theme.mode() != "system":
            return
        want = "light" if _theme.system_uses_light_theme() else "dark"
        if want != _theme.render_mode():
            _theme.set_mode("system")
            self.apply_theme()

    def _apply_dpi_change(self, dpi: int) -> None:
        """确认 DPI 稳定变化后换算比例并重建界面。"""
        self._dpi_now = dpi
        self._dpi_pending = 0
        self._last_dpi_apply = time.time()
        apply_dpi_to_tk(self.root, dpi)
        self._rebuild_for_dpi()

    def _rebuild_for_dpi(self) -> None:
        """按新 DPI 重建界面（保留勾选状态与当前页签），随后重新适配排版。"""
        self._apply_window_metrics()
        self._rebuild_ui(refresh=True)
        self._fit_layout()

    def _rebuild_ui(self, refresh: bool = True) -> None:
        """重建界面（保留勾选状态与当前页签）。"""
        if getattr(self, "_rebuilding", False):   # 重建期间不再接受新的重排
            return
        self._rebuilding = True
        try:
            idx = 0
            try:
                idx = self.nb.index(self.nb.select())
            except Exception:
                pass
            for child in list(self.root.winfo_children()):
                child.destroy()
            # 其中几个由 _build 重新创建；这里顺手清掉，避免留下旧变量
            for name in ("_board_vars", "_game_vars", "_board_cbs"):
                getattr(self, name, {}).clear()
            self._cloud_token_status = {}
            self._build()
            if refresh:
                self._refresh_account_status()
                self._refresh_last_run()
            try:
                self.nb.select(idx)
            except Exception:
                pass
            self._sync_pages()
        except Exception:
            traceback.print_exc()
            try:                  # 重建失败也要留下可用界面，否则窗口会变成一片空白
                self._build()
            except Exception:
                traceback.print_exc()
        finally:
            self._rebuilding = False

    @staticmethod
    def _apply_window_icon(win: tk.Misc) -> None:
        try:
            ico_path = Path(ensure_icon())
            # Windows Tk PhotoImage is more reliable with PNG than multi-size ICO
            png = ico_path.with_suffix(".png")
            if not png.exists():
                png = Path(ICON_PATH).with_suffix(".png")
            if not png.exists():
                return
            img = tk.PhotoImage(file=str(png))
            win._mihoyo_icon_ref = img  # type: ignore[attr-defined]
            win.iconphoto(True, img)
        except Exception:
            pass

    def _apply_ttk_style(self) -> None:
        """ttk 样式随主题刷新（clam 主题才支持自定义配色）。"""
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        p = _theme.palette()
        panel, card, line = p["panel"], p["card"], p["line"]
        style.configure("TCheckbutton", background=panel, foreground=p["text"],
                        font=_ui_font(10), focuscolor=panel,
                        indicatorcolor=p["accent"], indicatormargin=_px(4))
        style.map("TCheckbutton",
                  background=[("active", panel)], foreground=[("disabled", p["muted"])])
        style.configure("TButton", font=_ui_font(10))
        style.configure("TLabel", background=panel, foreground=p["text"], font=_ui_font(10))
        style.configure("Title.TLabel", background=panel, foreground=p["text"],
                        font=_ui_font(14, "bold"))
        style.configure("Muted.TLabel", background=panel, foreground=p["muted"],
                        font=_ui_font(9))
        style.configure("Section.TLabel", background=panel, foreground=p["text"],
                        font=_ui_font(10, "bold"))
        # 卡片内文字（卡片底色）
        style.configure("Card.TLabel", background=card, foreground=p["text"],
                        font=_ui_font(10))
        style.configure("CardMuted.TLabel", background=card, foreground=p["muted"],
                        font=_ui_font(9))
        # 页面级文字（Mica 底色上直接显示）
        style.configure("Page.TLabel", background=p["window"], foreground=p["text"],
                        font=_ui_font(10))
        style.configure("PageMuted.TLabel", background=p["window"],
                        foreground=p["muted"], font=_ui_font(9))
        style.configure("TNotebook", background=panel, borderwidth=0, tabmargins=0)
        style.configure("TNotebook.Tab", background=p["tab_bg"], foreground=p["tab_fg"],
                        padding=(_px(12), _px(5)), borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", p["tab_active_bg"])],
                  foreground=[("selected", p["text"])])
        # 现代风格滚动条：无箭头、细滑块、轨道透明（Mica 生效时轨道即背景）
        style.configure("Vertical.TScrollbar", background=p["muted"], troughcolor=p["window"],
                        bordercolor=p["window"], arrowcolor=p["window"], borderwidth=0,
                        lightcolor=p["muted"], darkcolor=p["muted"], arrowsize=0,
                        width=_px(8))
        style.map("Vertical.TScrollbar",
                  background=[("active", p["accent"]), ("pressed", p["accent"])])
        style.configure("TEntry", fieldbackground=p["entry_bg"], foreground=p["entry_fg"],
                        insertcolor=p["entry_fg"], bordercolor=line)
        style.configure("TSpinbox", fieldbackground=p["entry_bg"], foreground=p["entry_fg"],
                        insertcolor=p["entry_fg"], arrowcolor=p["text"], bordercolor=line)
        style.configure("TCombobox", fieldbackground=p["entry_bg"], background=p["entry_bg"],
                        foreground=p["entry_fg"], arrowcolor=p["text"], bordercolor=line)
        style.map("TCombobox", fieldbackground=[("readonly", p["entry_bg"])],
                  foreground=[("readonly", p["entry_fg"])])
        try:
            # 下拉列表是独立 Tk 列表，需要走 option 数据库
            self.root.option_add("*TCombobox*Listbox.background", p["entry_bg"])
            self.root.option_add("*TCombobox*Listbox.foreground", p["entry_fg"])
            self.root.option_add("*TCombobox*Listbox.selectBackground", p["accent"])
            self.root.option_add("*TCombobox*Listbox.selectForeground", p["accent_text"])
        except Exception:
            pass

    def apply_theme(self, rebuild: bool = True) -> None:
        """套用当前主题：刷新调色板、ttk 样式、标题栏与 Mica，必要时重建界面。"""
        _refresh_colors()
        try:
            _theme.apply_window_style(self.root, mica=True)
        except Exception:
            pass
        if rebuild:
            self._rebuild_for_dpi()

    def _build(self) -> None:
        self._apply_ttk_style()
        page_bg = _theme.palette()["window"]

        header = tk.Frame(self.root, bg=page_bg)
        header.pack(fill="x", padx=_px(20), pady=(_px(12), 0))
        tk.Label(header, text="米游社自动签到器", bg=page_bg, fg=TEXT,
                 font=_ui_font(15, "bold")).pack(anchor="w")
        self.status_var = tk.StringVar(value=self.status_text or "就绪")
        tk.Label(header, textvariable=self.status_var, bg=page_bg, fg=MUTED,
                 font=_ui_font(9)).pack(anchor="w", pady=(_px(2), 0))

        tab_wrap = tk.Frame(self.root, bg=page_bg)
        tab_wrap.pack(fill="x", padx=_px(20), pady=(_px(8), 0))
        self.tabs = TabBar(tab_wrap, ["功能", "账号", "关于"], self._on_tab_select,
                           bg=page_bg, fg=TEXT, muted=MUTED, accent=ACCENT,
                           font=_ui_font(12))
        self.tabs.pack(anchor="w")

        self.content = tk.Frame(self.root, bg=page_bg)
        self.content.pack(fill="both", expand=True, padx=_px(20), pady=_px(6))
        self.tab_feat = ScrollPage(self.content, bg=page_bg)
        self.tab_acct = ScrollPage(self.content, bg=page_bg)
        self.tab_about = ScrollPage(self.content, bg=page_bg)
        self._pages = [self.tab_feat, self.tab_acct, self.tab_about]
        index = getattr(self, "_tab_index", 0)
        for i, page in enumerate(self._pages):
            if i == index:
                page.place(x=0, y=0, relwidth=1, relheight=1)
            else:
                page.place_forget()
        self.tabs._index = index
        self.tabs._paint()
        self.nb = _TabsShim(self)  # 兼容既有的 select/index 调用

        self._build_features(self.tab_feat)
        self._build_account(self.tab_acct)
        self._build_about(self.tab_about)

        btns = tk.Frame(self.root, bg=page_bg)
        btns.pack(fill="x", padx=_px(20), pady=(_px(4), _px(12)))
        tk.Button(
            btns, text="立即签到", bg=ACCENT, fg="white", relief="flat",
            font=_ui_font(10, "bold"), padx=_px(14), pady=_px(6), command=self._run_now,
        ).pack(side="left")
        tk.Button(
            btns, text="保存设置", bg=BTN_BG, fg=BTN_FG, relief="flat",
            font=_ui_font(10, "bold"), padx=_px(14), pady=_px(6), command=self._save,
        ).pack(side="left", padx=_px(8))
        tk.Button(
            btns, text="查看日志", bg=page_bg, fg=TEXT, activebackground=LINE,
            activeforeground=TEXT, relief="flat", font=_ui_font(10),
            padx=_px(10), pady=_px(6), command=self._show_log,
        ).pack(side="left")
        tk.Button(
            btns, text="关闭", bg=WINDOW_BG, fg=TEXT, activebackground=LINE,
            activeforeground=TEXT, relief="flat", font=_ui_font(10),
            padx=_px(10), pady=_px(6), command=self._on_close,
        ).pack(side="right")

    # ── 标签页切换（带滑动动画）─────────────────────────────────────────
    def _on_tab_select(self, idx: int) -> None:
        self._switch_page(idx)

    def _switch_page(self, idx: int, animate: bool = True) -> None:
        if not hasattr(self, "_pages"):
            return
        idx = max(0, min(len(self._pages) - 1, int(idx)))
        job = getattr(self, "_page_anim_job", None)
        if job:
            # 上一次动画未结束：先立即落位再开始新的（否则点击会被吞）
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
            self._page_anim_job = None
            self._finish_page_switch()
        prev = getattr(self, "_tab_index", 0)
        if idx == prev:
            return
        self._page_old = self._pages[prev]
        self._page_new = self._pages[idx]
        self._tab_index = idx
        if not animate:
            self._page_old.place_forget()
            self._page_new.place(x=0, y=0, relwidth=1, relheight=1)
            return
        # 方向感：向后翻页从右侧进入，向前翻页从左侧进入；距离短、帧数少 → 顺滑
        distance = _px(26)
        direction = 1 if idx > prev else -1
        self._page_new.place(x=direction * distance, y=0, relwidth=1, relheight=1)
        self._page_new.lift()
        self._slide_step(0, direction, distance)

    def _slide_step(self, step: int, direction: int, distance: int) -> None:
        """非阻塞滑动：逐帧 after 调度（阻塞式动画会让窗口掉层、吞掉点击）。"""
        steps = 9
        if step >= steps or getattr(self, "_page_new", None) is None:
            self._finish_page_switch()
            return

        def ease(t: float) -> float:
            return 1 - (1 - t) * (1 - t)

        offset = int(direction * distance * (1 - ease((step + 1) / steps)))
        try:
            self._page_new.place(x=offset, y=0, relwidth=1, relheight=1)
        except Exception:
            self._finish_page_switch()
            return
        self._page_anim_job = self.root.after(13, lambda: self._slide_step(step + 1, direction, distance))

    def _finish_page_switch(self) -> None:
        try:
            if getattr(self, "_page_old", None) is not None:
                self._page_old.place_forget()
            if getattr(self, "_page_new", None) is not None:
                self._page_new.place(x=0, y=0, relwidth=1, relheight=1)
        except Exception:
            pass
        self._page_old = None
        self._page_new = None
        self._page_anim_job = None

    def _card(self, parent: tk.Frame, title: str | None = None) -> tk.Frame:
        """放一张圆角卡片（Mica 风格），返回可放内容的 body。"""
        surround = _theme.palette()["window"]
        card = RoundedCard(parent, surround=surround, radius=8, pad=_px(8))
        card.pack(fill="x", pady=(0, _px(6)))
        body = card.body
        if title:
            tk.Label(body, text=title, bg=CARD, fg=TEXT,
                     font=_ui_font(10, "bold")).pack(anchor="w", pady=(0, _px(6)))
        return body

    def _page_body(self, parent) -> tk.Frame:
        """页面容器：默认不滚动（内容装得下就没有滚动条），只有装不下时才允许滚动。"""
        page_bg = _theme.palette()["window"]
        host = getattr(parent, "inner", parent)
        inner = tk.Frame(host, bg=page_bg)
        inner.pack(fill="both", expand=True, padx=_px(8), pady=_px(6))
        if parent is getattr(self, "tab_feat", None):
            self._feat_inner = inner
        return inner

    def _build_features(self, parent: tk.Frame) -> None:
        wrap = self._page_body(parent)

        card = self._card(wrap, "游戏签到 · 社区打卡")
        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x")
        for col, minsize in BOARD_COLUMNS:
            head.grid_columnconfigure(col, minsize=_px(minsize))
        head.grid_columnconfigure(3, weight=1)
        tk.Label(head, text="游戏 / 板块", bg=CARD, fg=TEXT,
                 font=_ui_font(9, "bold"), anchor="w").grid(row=0, column=0, sticky="w")
        tk.Label(head, text="游戏签到", bg=CARD, fg=TEXT,
                 font=_ui_font(9, "bold"), anchor="w").grid(row=0, column=1, sticky="w")
        tk.Label(head, text="社区打卡", bg=CARD, fg=TEXT,
                 font=_ui_font(9, "bold"), anchor="w").grid(row=0, column=2, sticky="w")
        head_right = tk.Frame(head, bg=CARD)
        head_right.grid(row=0, column=4, sticky="e")
        self._btn_hidden = tk.Button(
            head_right, text="显示项目…", bg=CARD, fg=MUTED, activebackground=LINE,
            activeforeground=TEXT, relief="flat", bd=0, font=_ui_font(9),
            cursor="hand2", command=self._hidden_items_dialog)
        self._btn_hidden.pack(side="right")
        tk.Label(head_right, text="拖动行可排序", bg=CARD, fg=MUTED,
                 font=_ui_font(9)).pack(side="right", padx=(0, _px(10)))

        box = tk.Frame(card, bg=CARD, highlightbackground=LINE, highlightthickness=_px(1))
        box.pack(fill="x")
        game_flags = {
            "honkai3rd": self.cfg.enable_honkai3rd,
            "genshin": self.cfg.enable_genshin,
            "honkai2": self.cfg.enable_honkai2,
            "tears": self.cfg.enable_tears,
            "honkai_sr": self.cfg.enable_honkai_sr,
            "zzz": self.cfg.enable_zzz,
        }
        self._game_vars: dict[str, tk.BooleanVar] = {}
        self._board_vars: dict[int, tk.BooleanVar] = {}
        self._board_game_flags = game_flags
        self._board_list = ReorderList(box, row_height=24, gap=2, bg=CARD,
                                       on_reorder=self._on_board_reordered)
        self._board_list.pack(fill="x", padx=_px(6), pady=_px(4))
        self._render_board_rows()

        # 云游戏：三行合成一行，省一大截高度
        card = self._card(wrap, "云游戏签到")
        cloud = tk.Frame(card, bg=CARD)
        cloud.pack(fill="x")
        self.var_cloud_genshin = tk.BooleanVar(value=self.cfg.cloud_genshin)
        self.var_cloud_sr = tk.BooleanVar(value=self.cfg.cloud_sr)
        self.var_cloud_zzz = tk.BooleanVar(value=self.cfg.cloud_zzz)
        for label, var, pad in (
            ("云原神", self.var_cloud_genshin, 0),
            ("云星穹铁道", self.var_cloud_sr, _px(14)),
            ("云绝区零", self.var_cloud_zzz, _px(14)),
        ):
            box_item = tk.Frame(cloud, bg=CARD)
            box_item.pack(side="left", padx=(pad, 0))
            PillSwitch(box_item, variable=var, bg=CARD).pack(side="left")
            ttk.Label(box_item, text=label, style="Card.TLabel").pack(
                side="left", padx=_px(6))

        card = self._card(wrap, "自动定时签到")
        self.var_sched = tk.BooleanVar(value=self.cfg.schedule_enabled)
        sched_row = tk.Frame(card, bg=CARD)
        sched_row.pack(fill="x")
        PillSwitch(sched_row, variable=self.var_sched, bg=CARD).pack(side="left")
        tk.Label(sched_row, text="启用定时任务", bg=CARD, fg=TEXT,
                 font=_ui_font(10)).pack(side="left", padx=_px(8))
        ttk.Label(sched_row, text="随机延迟(秒)", style="Card.TLabel").pack(
            side="left", padx=(_px(16), _px(4)))
        self.ent_delay = tk.Entry(sched_row, font=_ui_font(10), width=6, bg=ENTRY_BG,
                                  fg=ENTRY_FG, insertbackground=ENTRY_FG, relief="flat",
                                  highlightthickness=_px(1), highlightbackground=LINE,
                                  highlightcolor=ACCENT)
        self.ent_delay.pack(side="left")
        self.ent_delay.insert(0, str(self.cfg.random_delay_sec))

        time_row = tk.Frame(card, bg=CARD)
        time_row.pack(fill="x", pady=(_px(4), 0))
        ttk.Label(time_row, text="签到时间", style="Card.TLabel").pack(side="left")
        first = (self.cfg.schedule_times or ["09:30"])[0]
        parsed = parse_hhmm(first) or (9, 30)
        self.var_hour.set(f"{parsed[0]:02d}")
        self.var_minute.set(f"{parsed[1]:02d}")
        # iOS 风格滚轮：时 / 分（滚轮或拖动，带吸附动画）
        self.wheel_hour = WheelPicker(
            time_row, values=[f"{h:02d}" for h in range(24)],
            variable=self.var_hour, width=52)
        self.wheel_hour.pack(side="left", padx=(_px(10), _px(2)))
        tk.Label(time_row, text=":", bg=CARD, fg=TEXT, font=_ui_font(12)).pack(side="left")
        self.wheel_minute = WheelPicker(
            time_row, values=[f"{m:02d}" for m in range(0, 60, 5)],
            variable=self.var_minute, width=52)
        self.wheel_minute.pack(side="left", padx=(_px(2), _px(10)))

        card = self._card(wrap, "启动 / 关闭")
        self.var_autostart = tk.BooleanVar(value=autostart.is_enabled())
        self.var_launch_run = tk.BooleanVar(value=self.cfg.run_on_launch)
        self.var_silent = tk.BooleanVar(value=bool(self.cfg.silent_launch))
        self.var_min_tray = tk.BooleanVar(value=bool(self.cfg.minimize_to_tray))
        # 2×2 排布：开关说明已足够清楚，不再各占一整行
        grid = tk.Frame(card, bg=CARD)
        grid.pack(fill="x")
        for i, (label, var) in enumerate((
            ("开机自启动", self.var_autostart),
            ("关闭时最小化到托盘", self.var_min_tray),
            ("启动后立即签到一次", self.var_launch_run),
            ("静默启动", self.var_silent),
        )):
            cell = tk.Frame(grid, bg=CARD)
            cell.grid(row=i // 2, column=i % 2, sticky="w",
                      padx=(0, _px(12)), pady=_px(1))
            PillSwitch(cell, variable=var, bg=CARD).pack(side="left")
            tk.Label(cell, text=label, bg=CARD, fg=TEXT,
                     font=_ui_font(10)).pack(side="left", padx=_px(6))

        theme_row = tk.Frame(card, bg=CARD)
        theme_row.pack(fill="x", pady=(_px(8), 0))
        tk.Label(theme_row, text="外观", bg=CARD, fg=TEXT, font=_ui_font(10)).pack(side="left")
        self.var_theme = tk.StringVar(
            value={"system": "跟随系统", "light": "浅色", "dark": "深色"}.get(
                self.cfg.ui_theme, "跟随系统"))
        self.cbo_theme = RoundedSelect(
            theme_row, values=["跟随系统", "浅色", "深色"], variable=self.var_theme,
            command=self._on_theme_pick, width=108)
        self.cbo_theme.pack(side="left", padx=_px(8))

        btns = tk.Frame(wrap, bg=PANEL)
        btns.pack(fill="x", pady=(_px(6), _px(0)))
        tk.Button(
            btns, text="最小化到托盘", bg=PANEL, fg=TEXT, activebackground=LINE,
            activeforeground=TEXT, relief="flat", font=_ui_font(10),
            padx=_px(8), pady=_px(4), command=self._minimize_to_tray,
        ).pack(side="left")
        tk.Button(
            btns, text="退出程序", bg=PANEL, activebackground=LINE, relief="flat", font=_ui_font(10),
            fg=DANGER, padx=_px(8), pady=_px(4), command=self._quit_app,
        ).pack(side="left", padx=_px(8))
        self.lastrun_var = tk.StringVar(value=self._last_run_text())
        ttk.Label(btns, textvariable=self.lastrun_var, style="PageMuted.TLabel").pack(
            side="right")

        ttk.Label(
            wrap,
            text=f"签到配置：{BBS_ROOT / 'config' / 'config.yaml'}",
            style="PageMuted.TLabel",
        ).pack(anchor="w", pady=(_px(6), _px(0)))

    def _last_run_text(self) -> str:
        last = (self.cfg.last_run or "").replace("T", " ")[:16] or "从未"
        status = self.cfg.last_status or ""
        return f"上次运行：{last}    结果：{status}" if status else f"上次运行：{last}"

    def _refresh_last_run(self) -> None:
        try:
            self.cfg = TrayConfig.load()
        except Exception:
            pass
        try:
            self.lastrun_var.set(self._last_run_text())
        except Exception:
            pass

    # ── 板块表：拖动排序 / 隐藏与恢复 ────────────────────────────────────
    def _board_rows_ordered(self) -> list[tuple[int, str, str | None]]:
        """全部板块（含隐藏项）按配置顺序；未记录的按默认顺序补在后面。"""
        by_gid = {row[0]: row for row in BOARD_ROWS}
        order = [g for g in (self.cfg.board_order or [])
                 if isinstance(g, int) and g in by_gid]
        order += [row[0] for row in BOARD_ROWS if row[0] not in set(order)]
        return [by_gid[g] for g in order]

    def _board_view(self) -> list[tuple[int, str, str | None]]:
        """要显示的板块行（隐藏项不返回）。"""
        hidden = set(self.cfg.board_hidden or [])
        return [row for row in self._board_rows_ordered() if row[0] not in hidden]

    def _render_board_rows(self) -> None:
        """构建行（仅在结构变化时调用；纯排序不重建，避免闪烁）。"""
        lst = getattr(self, "_board_list", None)
        if lst is None:
            return
        visible = self._board_view()
        specs = []
        for gid, name, game_key in visible:
            specs.append((gid, lambda frame, g=gid, n=name, k=game_key:
                          self._build_board_row(frame, g, n, k)))
        lst.set_rows(specs)
        self._sync_hidden_button()

    def _build_board_row(self, frame: tk.Frame, gid: int, name: str,
                         game_key: str | None) -> None:
        for col, minsize in BOARD_COLUMNS:
            frame.grid_columnconfigure(col, minsize=_px(minsize))
        frame.grid_columnconfigure(3, weight=1)
        tk.Label(frame, text=name, bg=CARD, fg=TEXT, font=_ui_font(10),
                 anchor="w").grid(row=0, column=0, sticky="w", padx=(_px(8), 0))
        if game_key:
            var = self._game_vars.get(game_key)
            if var is None:
                var = tk.BooleanVar(value=bool(self._board_game_flags.get(game_key)))
                self._game_vars[game_key] = var
            PillSwitch(frame, variable=var, bg=CARD).grid(row=0, column=1, sticky="w")
        else:
            tk.Label(frame, text="—", bg=CARD, fg=MUTED, font=_ui_font(10)).grid(
                row=0, column=1, sticky="w")
        bvar = self._board_vars.get(gid)
        if bvar is None:
            bvar = tk.BooleanVar(value=gid in set(self.cfg.checkin_list))
            self._board_vars[gid] = bvar
        PillSwitch(frame, variable=bvar, bg=CARD).grid(row=0, column=2, sticky="w")
        tk.Label(frame, text="⋮⋮", bg=CARD, fg=MUTED, font=_ui_font(11)).grid(
            row=0, column=4, sticky="e", padx=(0, _px(10)))

    def _sync_hidden_button(self) -> None:
        btn = getattr(self, "_btn_hidden", None)
        if btn is None:
            return
        n = len(self.cfg.board_hidden or [])
        try:
            btn.configure(text=f"显示项目（隐藏 {n}）…" if n else "显示项目…")
        except Exception:
            pass

    def _on_board_reordered(self, keys: list) -> None:
        """可见行的新顺序写回完整顺序；隐藏项保留原位置。"""
        hidden = set(self.cfg.board_hidden or [])
        rest = iter([int(k) for k in keys])
        full = []
        for gid, _n, _k in self._board_rows_ordered():
            if gid in hidden:
                full.append(gid)
            else:
                full.append(next(rest, gid))
        self.cfg.board_order = full
        try:
            self.cfg.save()
        except Exception:
            pass

    def _hidden_items_dialog(self) -> None:
        """显示项目子菜单：一个列表里同时隐藏与恢复（开关即显示状态）。"""
        win = tk.Toplevel(self.root)
        win.title("显示项目")
        win.configure(bg=CARD)
        win.transient(self.root)
        win.resizable(False, False)
        try:
            self._apply_window_icon(win)
        except Exception:
            pass
        body = tk.Frame(win, bg=CARD, padx=_px(16), pady=_px(14))
        body.pack(fill="both", expand=True)
        tk.Label(body, text="显示项目", bg=CARD, fg=TEXT,
                 font=_ui_font(10, "bold")).pack(anchor="w")
        tk.Label(body, text="关掉开关即隐藏（不显示在列表里，也不签到）；打开即恢复，"
                            "并回到原来的位置。恢复后要签到请重新打开「社区打卡」",
                 bg=CARD, fg=MUTED, font=_ui_font(9), justify="left",
                 wraplength=_px(380)).pack(anchor="w", pady=(_px(2), _px(10)))
        rows = tk.Frame(body, bg=CARD)
        rows.pack(fill="x")
        count_var = tk.StringVar()
        dlg_vars: dict[int, tk.BooleanVar] = {}

        def refresh_count() -> None:
            n = len(self.cfg.board_hidden or [])
            count_var.set(f"已隐藏 {n} 项" if n else "当前无隐藏项")

        for gid, name, _game in BOARD_ROWS:
            row = tk.Frame(rows, bg=CARD)
            row.pack(fill="x", pady=_px(2))
            tk.Label(row, text=name, bg=CARD, fg=TEXT, font=_ui_font(10),
                     anchor="w", width=14).pack(side="left")
            var = tk.BooleanVar(value=gid not in set(self.cfg.board_hidden or []))
            dlg_vars[gid] = var
            PillSwitch(row, variable=var, bg=CARD,
                       command=lambda g=gid, v=var: self._toggle_board_from_dialog(g, v.get())
                       ).pack(side="left", padx=_px(6))
            var.trace_add("write", lambda *_a: refresh_count())
        refresh_count()

        btns = tk.Frame(body, bg=CARD)
        btns.pack(fill="x", pady=(_px(12), 0))
        tk.Label(btns, textvariable=count_var, bg=CARD, fg=MUTED,
                 font=_ui_font(9)).pack(side="left")

        def show_all() -> None:
            self._restore_all_boards()
            for var in dlg_vars.values():
                if not var.get():
                    var.set(True)

        tk.Button(btns, text="全部显示", bg=CARD, fg=TEXT, activebackground=LINE,
                  activeforeground=TEXT, relief="flat", font=_ui_font(9),
                  padx=_px(10), pady=_px(3), cursor="hand2",
                  command=show_all).pack(side="right", padx=(_px(6), 0))
        tk.Button(btns, text="关闭", bg=ACCENT, fg="white", relief="flat",
                  font=_ui_font(9), padx=_px(10), pady=_px(3), cursor="hand2",
                  command=win.destroy).pack(side="right")
        win.update_idletasks()
        w = max(_px(380), win.winfo_reqwidth())
        h = win.winfo_reqheight()
        win.geometry(self._centered_geometry(win, w, h))
        win.update_idletasks()
        self._center_exact(win)
        try:
            _theme.apply_window_style(win)
        except Exception:
            pass
        win.grab_set()
        try:
            win.focus_force()
        except Exception:
            pass

    def _toggle_board_from_dialog(self, gid: int, visible: bool) -> None:
        """子菜单里的开关：直接按目标状态设置（不依赖主列表里的变量）。"""
        hidden = set(self.cfg.board_hidden or [])
        if visible:
            hidden.discard(gid)
        else:
            hidden.add(gid)
            bvar = self._board_vars.get(gid)
            if bvar is not None:
                bvar.set(False)
            self.cfg.checkin_list = [g for g in (self.cfg.checkin_list or [])
                                     if g != gid]
        self.cfg.board_hidden = sorted(hidden)
        try:
            self.cfg.save()
        except Exception:
            pass
        self._render_board_rows()

    def _restore_all_boards(self) -> None:
        """全部显示（子菜单里的按钮）。"""
        self.cfg.board_hidden = []
        try:
            self.cfg.save()
        except Exception:
            pass
        self._render_board_rows()

    def _build_account(self, parent: tk.Frame) -> None:
        wrap = self._page_body(parent)
        login_card = self._card(wrap, "登录")
        ttk.Label(
            login_card,
            text="短信验证码登录；云游戏凭证在签到时自动获取。若触发图形验证会弹出窗口，请手动完成",
            style="CardMuted.TLabel",
            wraplength=_px(420),
            justify="left",
        ).pack(anchor="w", pady=(_px(0), _px(8)))

        form = tk.Frame(login_card, bg=CARD)
        form.pack(fill="x")
        ttk.Label(form, text="手机号", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", pady=_px(3))
        self.ent_account = tk.Entry(form, font=_ui_font(10), width=28, bg=ENTRY_BG, fg=ENTRY_FG,
                                    insertbackground=ENTRY_FG, relief="flat",
                                    highlightthickness=_px(1), highlightbackground=LINE,
                                    highlightcolor=ACCENT)
        self.ent_account.grid(row=0, column=1, sticky="w", padx=_px(8), pady=_px(3))
        ttk.Label(form, text="短信验证码", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", pady=_px(3))
        self.ent_sms = tk.Entry(form, font=_ui_font(10), width=16, bg=ENTRY_BG, fg=ENTRY_FG,
                                insertbackground=ENTRY_FG, relief="flat",
                                highlightthickness=_px(1), highlightbackground=LINE,
                                highlightcolor=ACCENT)
        self.ent_sms.grid(row=1, column=1, sticky="w", padx=_px(8), pady=_px(3))

        btns = tk.Frame(login_card, bg=CARD)
        btns.pack(fill="x", pady=_px(10))
        self._btn_sms = tk.Button(
            btns, text="发送短信验证码", bg=CARD, fg=TEXT, activebackground=LINE,
            activeforeground=TEXT, relief="flat", font=_ui_font(10),
            padx=_px(8), pady=_px(4), command=self._send_sms,
        )
        self._btn_sms.pack(side="left")
        tk.Button(
            btns, text="登录", bg=ACCENT, fg="white", relief="flat",
            font=_ui_font(10, "bold"), padx=_px(8), pady=_px(4), command=self._login_sms,
        ).pack(side="left", padx=_px(8))
        tk.Button(
            btns, text="退出登录并清除数据", bg=CARD, activebackground=LINE,
            relief="flat", font=_ui_font(10),
            fg=DANGER, padx=_px(8), pady=_px(4), command=self._logout,
        ).pack(side="left")

        self.login_var = tk.StringVar(value="")
        self.snap_var = tk.StringVar(value="")
        ttk.Label(
            login_card, textvariable=self.login_var, style="CardMuted.TLabel",
            wraplength=_px(420), justify="left",
        ).pack(anchor="w", pady=(_px(4), 0))

        account_card = self._card(wrap, "当前签到账号")
        self.snap_label = tk.Label(
            account_card, textvariable=self.snap_var, bg=CARD,
            font=_ui_font(10), fg=MUTED, justify="left",
        )
        self.snap_label.pack(anchor="w")
        self._refresh_account_status()

        if (self.cfg.device_id or "").strip() and (self.cfg.device_fp or "").strip():
            try:
                ensure_device(self.cfg)
            except Exception as e:
                self.login_var.set(f"设备标识初始化失败：{e}")
        else:
            self._start_device_setup()

    def _start_device_setup(self) -> None:
        """首次运行：后台生成 device_id / device_fp（getFp 要联网，不能卡住建界面）。"""
        if getattr(self, "_device_busy", False):
            return
        self._device_busy = True

        def work() -> None:
            try:
                ensure_device(self.cfg)
            except Exception as e:
                self._ui_queue.put(
                    lambda m=f"设备标识初始化失败：{e}": self.login_var.set(m))
            finally:
                self._device_busy = False

        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception:
            self._device_busy = False


    def _refresh_account_status(self) -> None:
        info = load_account_info()
        if info.get("error"):
            self.snap_var.set(info["error"])
            self.login_var.set(info["error"])
            return
        self.snap_var.set(format_account_status(info, self.cfg.account_nickname))
        if info.get("logged_in"):
            self.login_var.set("当前状态：已登录")
            if not self.cfg.account_nickname or not info.get("stuid"):
                self._start_profile_refresh()
        else:
            self.login_var.set("当前状态：未登录")

    def _start_profile_refresh(self) -> None:
        """后台补全 uid / 米游社昵称（仅在缺失时触发一次）。"""
        if getattr(self, "_profile_busy", False):
            return
        self._profile_busy = True

        def work() -> None:
            try:
                import yaml as _yaml
                from app_config import engine_config_path
                from stoken_login import fetch_account_profile
                from runner import _bbs_config_path
                path = _bbs_config_path()
                data = _yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                acc = data.get("account") or {}
                stoken = str(acc.get("stoken") or "").strip()
                mid = str(acc.get("mid") or "").strip()
                stuid = str(acc.get("stuid") or "").strip()
                if not stoken:
                    return
                did, fp = self._device()
                uid, nick = fetch_account_profile(stoken, mid, did, fp, stuid)
                if uid and not stuid:
                    acc["stuid"] = uid
                    path.write_text(_yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                                    encoding="utf-8")
                if nick:
                    self.cfg.account_nickname = nick
                if uid:
                    self.cfg.account_stuid = str(uid)
                if nick or uid:
                    self.cfg.save()
            except Exception:
                pass
            finally:
                self._profile_busy = False
                self._post_ui(self._refresh_account_status)

        threading.Thread(target=work, daemon=True).start()

    def _build_about(self, parent: tk.Frame) -> None:
        wrap = self._page_body(parent)
        about_card = self._card(wrap, "关于")
        txt = tk.Text(
            about_card, bg=CARD, fg=TEXT, font=_ui_font(10), insertbackground=TEXT,
            relief="flat", highlightthickness=0, height=18, wrap="word",
        )
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", ABOUT_TEXT)
        txt.configure(state="disabled")

        btns = tk.Frame(about_card, bg=CARD)
        btns.pack(fill="x", pady=(_px(10), 0))
        tk.Button(
            btns, text="打开配置目录", bg=CARD, fg=TEXT, activebackground=LINE,
            activeforeground=TEXT, relief="flat", font=_ui_font(10),
            padx=_px(8), pady=_px(4),
            command=lambda: __import__("os").startfile(str(BBS_ROOT / "config")),
        ).pack(side="left")
        tk.Button(
            btns, text="打开日志", bg=CARD, fg=TEXT, activebackground=LINE,
            activeforeground=TEXT, relief="flat", font=_ui_font(10),
            padx=_px(8), pady=_px(4),
            command=lambda: (
                __import__("os").startfile(str(__import__("app_config").LOG_PATH))
                if __import__("app_config").LOG_PATH.exists()
                else self._dialog("关于", "日志尚未生成")
            ),
        ).pack(side="left", padx=_px(8))

    def _post_ui(self, fn) -> None:
        self._ui_queue.put(fn)

    def _device(self) -> tuple[str, str]:
        return ensure_device(self.cfg)

    def _set_sms_button(self, enabled: bool, text: str | None = None) -> None:
        btn = self._btn_sms
        if not btn:
            return
        try:
            if text is not None:
                btn.configure(text=text)
            btn.configure(state="normal" if enabled else "disabled")
        except Exception:
            pass

    def _start_sms_cooldown(self, seconds: int) -> None:
        self._sms_left = max(1, int(seconds))
        self._set_sms_button(False, f"{self._sms_left}s 后可重发")
        self._tick_sms_cooldown()

    def _tick_sms_cooldown(self) -> None:
        if self._sms_after is not None:
            try:
                self.root.after_cancel(self._sms_after)
            except Exception:
                pass
            self._sms_after = None
        if self._sms_left <= 0:
            self._sms_left = 0
            self._set_sms_button(True, "发送短信验证码")
            return
        self._set_sms_button(False, f"{self._sms_left}s 后可重发")
        self._sms_left -= 1
        try:
            self._sms_after = self.root.after(1000, self._tick_sms_cooldown)
        except Exception:
            self._sms_after = None

    def _clear_sms_cooldown(self) -> None:
        if self._sms_after is not None:
            try:
                self.root.after_cancel(self._sms_after)
            except Exception:
                pass
            self._sms_after = None
        self._sms_left = 0
        self._set_sms_button(True, "发送短信验证码")

    def _send_sms(self) -> None:
        if self._login_busy:
            return
        if self._sms_left > 0:
            self._dialog("发送验证码", f"请等待 {self._sms_left} 秒后再发送")
            return
        mobile = self.ent_account.get().strip()
        if not mobile:
            self._dialog("签到账号", "请先填写手机号", kind="error")
            return
        did, fp = self._device()
        self._login_busy = True
        self._set_sms_button(False, "发送中…")
        self.login_var.set("正在发送验证码…（若需图形验证将弹出窗口）")

        self._hint_parent_for_dialogs()

        def work() -> None:
            res = send_sms(mobile, did, fp, open_window=True)
            self._post_ui(lambda: self._sms_done(res))

        threading.Thread(target=work, daemon=True).start()

    def _login_sms(self) -> None:
        if self._login_busy:
            return
        mobile = self.ent_account.get().strip()
        code = self.ent_sms.get().strip()
        if not mobile or not code:
            self._dialog("签到账号", "请填写手机号和短信验证码", kind="error")
            return
        did, fp = self._device()
        self._login_busy = True
        self.login_var.set("正在短信登录…（若需图形验证将弹出窗口）")

        self._hint_parent_for_dialogs()

        def work() -> None:
            res = login_by_sms(mobile, code, did, fp, open_window=True)
            if res.ok and res.stoken:
                if res.mid:
                    verified = login_verify(res.stoken, res.mid, did, fp)
                    res.stoken = verified.stoken or res.stoken
                    res.message = f"{res.message}；{verified.message}".strip("；")
                try:
                    uid, nick = fetch_account_profile(res.stoken, res.mid, did, fp, res.stuid)
                    res.stuid = res.stuid or uid
                    res.nickname = nick or res.nickname
                except Exception:
                    pass
                try:
                    ct, lt = exchange_cookie(res.stoken, res.mid, res.stuid, did, fp)
                    res.cookie_token = ct
                    res.ltoken = lt
                except Exception:
                    pass
                try:
                    write_bbs_config(res)
                except Exception as e:
                    res.message += f"；写入配置失败：{e}"
                    res.ok = False
                if res.nickname:
                    self.cfg.account_nickname = res.nickname
                if res.stuid:
                    self.cfg.account_stuid = str(res.stuid)
                try:
                    self.cfg.save()
                except Exception:
                    pass
            self._post_ui(lambda: self._login_done(res))

        threading.Thread(target=work, daemon=True).start()

    def _sms_done(self, res: StokenResult) -> None:
        self._login_busy = False
        self.login_var.set(res.message)
        self._refresh_account_status()
        if res.ok:
            self._start_sms_cooldown(res.sms_countdown or 60)
            self._dialog("签到账号", res.message)
        else:
            self._clear_sms_cooldown()
            if "图形验证" in (res.message or ""):
                self.login_var.set(res.message + "（未进入倒计时，可立即重试）")
            self._dialog("签到账号", res.message, kind="warning")

    def _login_done(self, res: StokenResult) -> None:
        self._login_busy = False
        self.login_var.set(res.message)
        self._refresh_account_status()
        if res.ok:
            self._dialog("签到账号", res.message + "\n已写入签到配置")
        else:
            self._dialog("签到账号", res.message, kind="warning")

    def _collect_times(self) -> list[str] | None:
        return [f"{self.var_hour.get().zfill(2)}:{self.var_minute.get().zfill(2)}"]

    def _logout(self) -> None:
        if not self._dialog("退出登录", "确定退出登录并清除账号数据与云游戏凭证？", ask=True):
            return
        ok, msg = logout_and_clear(self.cfg)
        self._refresh_account_status()
        if ok:
            self.login_var.set("当前状态：未登录")
            self._dialog("退出登录", msg)
        else:
            self._dialog("退出登录", msg, kind="error")

    def _collect(self) -> TrayConfig | None:
        visible = {row[0] for row in self._board_view()}
        boards = [gid for gid, var in self._board_vars.items()
                  if var.get() and gid in visible]
        any_board = bool(boards)
        any_game = any(var.get() for var in self._game_vars.values())
        if not any_board and not any_game:
            self._dialog("设置", "请至少勾选一项：游戏签到或社区打卡", kind="error")
            return None
        times = self._collect_times()
        if times is None:
            return None
        if self.var_sched.get() and not times:
            self._dialog("设置", "启用定时至少要有一个时间点", kind="error")
            return None
        try:
            delay = max(0, int(self.ent_delay.get().strip() or "0"))
        except ValueError:
            self._dialog("设置", "随机延迟必须是整数秒", kind="error")
            return None

        cfg = self.cfg
        cfg.enable_bbs = any_board
        cfg.enable_genshin = self._game_vars["genshin"].get()
        cfg.enable_honkai3rd = self._game_vars["honkai3rd"].get()
        cfg.enable_honkai2 = self._game_vars["honkai2"].get()
        cfg.enable_tears = self._game_vars["tears"].get()
        cfg.enable_honkai_sr = self._game_vars["honkai_sr"].get()
        cfg.enable_zzz = self._game_vars["zzz"].get()
        cfg.checkin_list = sorted(boards)
        cfg.cloud_genshin = self.var_cloud_genshin.get()
        cfg.cloud_zzz = self.var_cloud_zzz.get()
        cfg.cloud_sr = self.var_cloud_sr.get()
        cfg.schedule_enabled = self.var_sched.get()
        cfg.schedule_times = times
        cfg.random_delay_sec = delay
        cfg.autostart = self.var_autostart.get()
        cfg.run_on_launch = self.var_launch_run.get()
        cfg.silent_launch = self.var_silent.get()
        cfg.minimize_to_tray = self.var_min_tray.get()
        cfg.ui_theme = {"跟随系统": "system", "浅色": "light", "深色": "dark"}.get(
            self.var_theme.get(), "system")
        ensure_device(cfg)
        return cfg

    def _save(self) -> None:
        cfg = self._collect()
        if not cfg:
            return
        cfg.save()
        autostart.set_enabled(cfg.autostart)
        self.on_saved(cfg)
        self.status_var.set("设置已保存")
        self._dialog("设置", "已保存")

    def _run_now(self) -> None:
        if self._checkin_running:
            return
        cfg = self._collect()
        if not cfg:
            return
        cfg.save()
        self._start_waiting()

        def work() -> None:
            ran = True
            try:
                result = self.on_run_now()
                if result is False:
                    ran = False
            except Exception as e:
                msg = f"签到异常：{e}"
            else:
                if ran:
                    fresh = TrayConfig.load()
                    self.cfg = fresh
                    msg = fresh.last_status or "就绪"
                else:
                    # 已有签到在执行：不能把上次的结果当成这次的结果
                    msg = "已有签到正在进行，完成后会更新状态"
            self._post_ui(lambda m=msg: self._finish_checkin(m))

        threading.Thread(target=work, daemon=True, name="mihoyo-ui-checkin").start()

    def _start_waiting(self) -> None:
        self._checkin_running = True
        self._dots = 0
        self._wait_job = None
        self._tick_waiting()

    def _tick_waiting(self) -> None:
        self._wait_job = None
        if not self._checkin_running:
            return
        self._dots = (self._dots % 3) + 1
        dots = "." * self._dots
        try:
            self.status_var.set(f"请稍候{dots}")
        except Exception:
            pass
        try:
            self._wait_job = self.root.after(400, self._tick_waiting)
        except Exception:
            self._wait_job = None

    def _stop_waiting(self) -> None:
        self._checkin_running = False
        if self._wait_job is not None:
            try:
                self.root.after_cancel(self._wait_job)
            except Exception:
                pass
            self._wait_job = None

    def _finish_checkin(self, message: str) -> None:
        self._stop_waiting()
        try:
            self.cfg = TrayConfig.load()
        except Exception:
            pass
        self._refresh_last_run()
        try:
            self.status_var.set(message or "就绪")
        except Exception:
            pass

    def _parent_rect(self) -> tuple[int, int, int, int] | None:
        """设置窗口在屏幕上的矩形；不可见时返回 None。"""
        try:
            self.root.update_idletasks()
            if self.root.state() == "withdrawn":
                return None
            w, h = self.root.winfo_width(), self.root.winfo_height()
            if w <= 1 or h <= 1:
                return None
            return self.root.winfo_rootx(), self.root.winfo_rooty(), w, h
        except Exception:
            return None

    def _centered_geometry(self, win: tk.Toplevel, width: int, height: int) -> str:
        """子窗口居中于设置窗口；设置窗口不可用时退回屏幕居中。"""
        pos = self._parent_rect()
        if pos is None:
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            x, y = (sw - width) // 2, (sh - height) // 2
        else:
            px, py, pw, ph = pos
            x, y = px + (pw - width) // 2, py + (ph - height) // 2
        return f"{width}x{height}+{max(0, x)}+{max(0, y)}"

    def _hint_parent_for_dialogs(self) -> None:
        """把设置窗口位置告知图形验证窗口，使其居中而非屏幕居中。"""
        try:
            from aigis_solver import set_parent_rect

            set_parent_rect(self._parent_rect())
        except Exception:
            pass

    def _dialog(self, title: str, message: str, kind: str = "info", ask: bool = False) -> bool:
        """自绘模态对话框：居中于设置窗口（原生 messagebox 只能居中于屏幕）。"""
        win = tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        win.resizable(False, False)
        try:
            self._apply_window_icon(win)
        except Exception:
            pass

        color = {"error": DANGER, "warning": WARN}.get(kind, TEXT)
        win.configure(bg=CARD)
        body = tk.Frame(win, bg=CARD, padx=_px(18), pady=_px(14))
        body.pack(fill="both", expand=True)
        tk.Label(body, text=title, bg=CARD, fg=color,
                 font=_ui_font(10, "bold")).pack(anchor="w")
        tk.Label(body, text=message, bg=CARD, fg=TEXT, justify="left",
                 wraplength=_px(420), font=_ui_font(9)).pack(anchor="w", pady=(_px(6), _px(14)))
        btns = tk.Frame(body, bg=CARD)
        btns.pack(fill="x")
        result = {"ok": False}

        def close(val: bool) -> None:
            result["ok"] = val
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()

        if ask:
            tk.Button(btns, text="取消", bg=CARD, fg=TEXT, activebackground=LINE,
                      activeforeground=TEXT, relief="flat", font=_ui_font(10),
                      padx=_px(10), pady=_px(3), command=lambda: close(False)).pack(side="right")
            tk.Button(btns, text="确定", bg=ACCENT, fg="white", relief="flat", font=_ui_font(10),
                      padx=_px(10), pady=_px(3), command=lambda: close(True)).pack(side="right", padx=_px(6))
        else:
            tk.Button(btns, text="确定", bg=ACCENT, fg="white", relief="flat",
                      font=_ui_font(10), padx=_px(10), pady=_px(3),
                      command=lambda: close(True)).pack(side="right")

        win.bind("<Return>", lambda e: close(True))
        win.bind("<Escape>", lambda e: close(False))
        win.protocol("WM_DELETE_WINDOW", lambda: close(not ask))
        win.update_idletasks()
        w = max(_px(360), min(_px(520), win.winfo_reqwidth()))
        h = win.winfo_reqheight()
        win.geometry(self._centered_geometry(win, w, h))
        win.update_idletasks()
        self._center_exact(win)
        try:  # 窗口实体化之后再设置标题栏颜色，否则不生效
            _theme.apply_window_style(win)
        except Exception:
            pass
        win.grab_set()
        try:
            win.focus_force()
        except Exception:
            pass
        self.root.wait_window(win)
        return result["ok"]

    def _center_exact(self, win: tk.Toplevel) -> None:
        """用 Win32 校正窗口位置，使其像素级居中于设置窗口（Tk 与边框度量不同）。"""
        try:
            import ctypes
            import ctypes.wintypes as wt

            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
            rect = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            fw, fh = rect.right - rect.left, rect.bottom - rect.top
            if fw <= 1 or fh <= 1:
                return
            pos = self._parent_rect()
            if pos is None:
                x = (user32.GetSystemMetrics(0) - fw) // 2
                y = (user32.GetSystemMetrics(1) - fh) // 2
            else:
                px, py, pw, ph = pos
                x = px + (pw - fw) // 2
                y = py + (ph - fh) // 2
            SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
            user32.SetWindowPos(hwnd, 0, int(x), int(y), 0, 0,
                                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        except Exception:
            pass

    def _show_log(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("运行日志")
        win.configure(bg=CARD)
        self._apply_window_icon(win)
        win.geometry(self._centered_geometry(win, _px(640), _px(420)))
        win.update_idletasks()
        try:
            _theme.apply_window_style(win)
        except Exception:
            pass
        txt = tk.Text(win, wrap="none", font=("Consolas", 10), bg=CARD, fg=TEXT,
                      insertbackground=TEXT, relief="flat")
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", read_log_tail(120))
        txt.configure(state="disabled")

    def _minimize_to_tray(self) -> None:
        self._stop_waiting()
        self.cfg.minimize_to_tray = True
        try:
            self.var_min_tray.set(True)
        except Exception:
            pass
        self.cfg.save()
        self.root.withdraw()

    def _quit_app(self) -> None:
        self._stop_waiting()
        self.cfg.minimize_to_tray = False
        try:
            self.var_min_tray.set(False)
        except Exception:
            pass
        self.cfg.save()
        self.quit_requested = True
        if self.on_request_quit:
            self.on_request_quit()
        else:
            self.root.destroy()

    def _on_close(self) -> None:
        try:
            self.cfg.minimize_to_tray = bool(self.var_min_tray.get())
            self.cfg.save()
        except Exception:
            pass
        if self.cfg.minimize_to_tray:
            self.root.withdraw()
            return
        self.quit_requested = True
        if self.on_request_quit:
            self.on_request_quit()
        else:
            self.root.destroy()

    def request_show(self) -> None:
        self._show_flag = True

    def show_now(self) -> None:
        self._show_flag = False
        self._refresh_last_run()
        self._refresh_account_status()
        try:
            self.root.deiconify()
            self._restyle_window()          # 显示后补一次边框样式（DWM 常丢）
            self.root.lift()
            self.root.focus_force()
            self.root.attributes("-topmost", True)
            self.root.after(300, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass

    def hide_now(self) -> None:
        try:
            self.root.withdraw()
        except Exception:
            pass

    def pump(self) -> None:
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except queue.Empty:
            pass
        except Exception:
            pass
        if self._show_flag:
            self.show_now()
        self._watch_dpi()

    def destroy(self) -> None:
        self._stop_waiting()
        try:
            if self._sms_after is not None:
                self.root.after_cancel(self._sms_after)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def show(self) -> None:
        self.show_now()
        self.root.mainloop()


class _TabsShim:
    """兼容旧代码里的 Notebook 调用（select / index）。"""

    def __init__(self, win: "SettingsWindow") -> None:
        self._win = win

    def select(self, idx: int | None = None):
        if idx is None:
            return self._win._tab_index
        try:
            self._win.tabs.select(int(idx))
        except Exception:
            pass
        return int(idx)

    def index(self, _page=None) -> int:
        return int(getattr(self._win, "_tab_index", 0))
