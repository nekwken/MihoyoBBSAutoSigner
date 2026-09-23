from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
import traceback
from datetime import datetime, timedelta
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
    LOG_PATH,
    TrayConfig,
)
from make_icon import ensure_icon
from account_store import format_account_status, load_account_info, logout_and_clear
from device_identity import ensure_device
from dpi import (apply_dpi_to_tk, density as _density, logical_scale as _logical_scale,
                 min_density, monitor_dpi, monitor_work_area, px as _px, set_density,
                 set_scale_from, set_user_scale, user_scale as _user_scale,
                 window_frame_height)
from widgets import (PillSwitch, ProgressPill, ReorderList, RoundedCard, RoundedSelect,
                     ScrollPage, SlimScrollbar, TabBar, WheelPicker)

# 排版密度阶梯：从松到紧，取第一个内容装得下的（下限由 dpi.min_density 决定）
DENSITY_STEPS = (1.0, 0.94, 0.88, 0.82, 0.76, 0.70, 0.66, 0.62)

# 窗口大小档位（按钮逐档放大/缩小；比例会写入 ui_scale）
SCALE_CHOICES = ("70%", "80%", "90%", "100%", "110%", "125%", "150%", "175%", "200%")


def _snap_scale(value: float) -> float:
    """把任意比例吸附到最近档位（默认 100%）。"""
    try:
        steps = [int(s.rstrip("%")) / 100.0 for s in SCALE_CHOICES]
        v = float(value)
        if v <= 0:
            return 1.0
        return min(steps, key=lambda s: abs(s - v))
    except Exception:
        return 1.0


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
    """界面字体：字号按排版密度与用户缩放收缩/放大（单位是点，Tk 会再按 DPI 放大）。"""
    try:
        size = max(7, int(round(float(size) * _logical_scale())))
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
        is_running: Callable[[], bool] | None = None,
    ) -> None:
        self.cfg = cfg
        self.on_saved = on_saved
        self.on_run_now = on_run_now
        self.on_request_quit = on_request_quit
        self.status_text = status_text
        self.is_running = is_running
        self._login_busy = False
        self.quit_requested = False
        self._sms_left = 0
        self._sms_after = None
        self._btn_sms = None
        self._show_flag = False
        # show_now() 自己会盖遮罩；期间由 <Map> 触发的还原处理要跳过，避免盖两遍
        self._in_show_now = False
        # 「界面在隐藏/最小化期间变过」：变了就不再拿旧快照当封面，见 _note_hidden_change
        self._hidden_changed = False
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
        self._fit_density = _density()          # 100% 缩放时密度阶梯选出的值
        self._saved_fp: tuple | None = None
        self._dirty_checked = 0.0
        self._runtime_checked = 0.0
        self._overlay: dict | None = None
        self._float_wins: list = []
        # 窗口大小默认 100%：历史遗留的非整档比例（如拖动时代写入的 1.026）统一吸附
        scale = _snap_scale(getattr(self.cfg, "ui_scale", 1.0) or 1.0)
        set_user_scale(scale)
        if abs(float(getattr(self.cfg, "ui_scale", 1.0) or 1.0) - scale) > 1e-6:
            self.cfg.ui_scale = float(scale)
            try:
                self.cfg.save()
            except Exception:
                pass
        _theme.set_mode(getattr(self.cfg, "ui_theme", "system"))
        _refresh_colors()
        self.root.title("米游社自动签到器 · 设置")
        self._apply_window_icon(self.root)
        self._apply_window_metrics()
        self.root.configure(bg=WINDOW_BG)
        # 切断「子控件请求尺寸 → 主窗口尺寸」的传播：窗口尺寸只由 _apply_layout 决定。
        # 否则拖动窗口时（每次移动都会触发一轮重新布局）子控件请求宽度的微小收缩
        # 会被传播到窗口上 —— 实测副屏上每移动一次宽度就少几个像素、一路缩到下限，
        # 而应用自己一次 geometry() 都没调用过（拖动 20 个 Configure 事件只对应 0 次请求）。
        try:
            self.root.pack_propagate(False)
            self.root.grid_propagate(False)
        except Exception:
            pass
        # 边框「点不动」：命中测试层面拒绝缩放（等价的"用户不能拖缩放"，
        # 且没有 resizable(False) 那个「最小=最大」在高 DPI 副屏被逐帧钳小的副作用）。
        # 必须等窗口映射后再装（见 _install_hittest_guard 的说明），所以这里不装。
        pass
        # `_guard_window_size` 会在尺寸被外部改动时把它拉回设定值，用来替代
        # resizable(False) 的硬锁定（那个在高 DPI 副屏上会让窗口越拖越窄）。
        # 但实测它与跨屏时的系统尺寸调整互掐：`test_dual_real.py` 直接卡死不返回
        # （守则将系统的 DPI 微调误判为"用户改尺寸"，拉回 → 再微调 → 无限循环）。
        # 因此当前只放开 resizable(True, True)（已验证：副屏拖标题栏不再变窄、不再卡），
        # 「用户不能拖边框缩放」这条 UX 待用更稳的做法补（如 WM_NCHITTEST 子类化）。
        # try:
        #     self.root.bind("<Configure>", self._guard_window_size, add="+")
        # except Exception:
        #     pass
        self.root.attributes("-topmost", True)
        self.root.withdraw()
        # `wm resizable` **只在这里设一次**。Tk 在 Windows 上是靠「销毁并重建顶层窗口」
        # 来实现它的：实测调用后窗口句柄直接换掉（19795906→50859688），新窗口客户区
        # 空白、透出桌面 —— 就是用户说的「窗口重新出现」。
        # 以前 `_apply_layout` 里每次排版都调一次，于是每次缩放/重排都要重演一遍
        # （逐帧取证：改尺寸那一刻窗口变成不可见、外框宽 0、随后三帧亮度 126→147→198）。
        # 放在 withdraw 之后：重建发生在不可见状态，用户看不到。
        try:
            self.root.resizable(True, True)
        except Exception:
            pass
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
        # 隐形渲染一次并抓成封面快照：静默启动后窗口从没渲染过，不预热的话
        # 「第一次打开」只能退化成纯色封面（用户实测过）。见 _warmup_snapshot。
        self._warmup_snapshot()

    def _on_window_mapped(self, event) -> None:
        """窗口每次显示都重新套用边框样式。

        DWM 对「窗口显示之后再改标题栏属性」经常不生效，所以显示时补一次。

        另外：从任务栏还原窗口（原生最小化按钮 → 点任务栏按钮）**不经过 show_now()**，
        而紧接着的 `_restyle_window()` 会换非客户区、让客户区整块重画一遍 ——
        不盖遮罩就是我们想要的「窗口闪一下、内容重新冒出来」。所以这里补一次遮罩。
        """
        if getattr(event, "widget", None) is not self.root:
            return
        self._restyle_window()
        # Tk 一旦重建顶层窗口（`wm resizable` 等）就会把 WS_THICKFRAME 加回来，
        # 那时用户又能拖边框缩放了；这里补一刀（已是目标样式时内部直接返回）。
        self._remove_thick_frame()
        if not self._in_show_now:
            self._mask_cover_on_restore()

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
            # 走统一入口：它会登记「期望尺寸」并打上 _applying_size 标记，
            # 否则 _guard_window_size 会以为尺寸被外部改动、反复把它拉回去（实测会卡死）
            self._resize_no_erase(w, h)
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
        """标题、页签、底部按钮等固定框架占用的高度（用请求高度算，窗口还没显示也能测）。

        必须跳过 `_float_wins` 里的浮层：遮罩是 place 在根窗口上的覆盖控件，
        它有几何管理器、请求高度等于整幅遮罩图 —— 算进来会凭空多出一个窗口高度，
        导致重排时算出的「固定框架」比真实值大几百像素，比例被拉成「过长」。
        """
        total = 0
        try:
            self.root.update_idletasks()
            floats = tuple(getattr(self, "_float_wins", ()) or ())
            for child in self.root.winfo_children():
                if child is self.content:
                    total += self._pack_padding(child)
                    continue
                if floats and child in floats:    # 遮罩等浮层不是布局的一部分
                    continue
                if not child.winfo_manager():    # 未 pack 的（如隐藏中的签到横幅）不算
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
        """按当前显示器可用空间选排版密度并定窗口尺寸，然后锁定等比。"""
        size = self._plan_layout()
        if size:
            self._apply_layout(size)

    def _plan_layout(self) -> tuple[int, int] | None:
        """选排版密度并算出目标窗口尺寸（不改窗口）。

        密度自松到紧取第一个内容装得下的（下限由 dpi.min_density，再紧字号就看不清了）；
        都不装不下时窗口取满可用高度，由 ScrollPage 提供滚动 —— 内容不会被裁掉。
        用户自己缩放过时不再动密度，避免把用户的缩放抹平。
        拆出「先算尺寸、后应用」是为了让重排遮罩能提前撑到目标尺寸（否则改尺寸那一下会露边）。
        """
        try:
            self.root.update_idletasks()
            work_w, work_h = monitor_work_area(self.root)
            screen_w = work_w or self.root.winfo_screenwidth()
            screen_h = work_h or self.root.winfo_screenheight()
            frame_h = window_frame_height(self.root)
            margin = _px(24)
            user = _user_scale()
            zoomed = abs(user - 1.0) > 1e-6
            avail_w = max(_px(520), screen_w - margin)
            avail_client = max(_px(380), screen_h - frame_h - margin)
            if zoomed:
                # 用户自己缩放过：沿用基准密度，别再让密度阶梯抵消掉缩放
                if abs(_density() - self._fit_density) > 1e-6:
                    self._set_density(self._fit_density)
            else:
                floor = min_density(self._dpi_now)
                tol = _px(4)                     # 边界容差：避免重建后档位来回跳
                for step in DENSITY_STEPS:
                    if step < floor - 1e-6:
                        break
                    if abs(_density() - step) > 1e-6:
                        self._set_density(step)
                    if self._content_height() <= max(_px(240), avail_client - self._chrome_height()) + tol:
                        break
                self._fit_density = _density()
            chrome = self._chrome_height()
            need = self._content_height()
            base_w = max(_px(660), self._content_width() + _px(56))
            base_h = max(_px(480), need + chrome)
            # 宽度优先：宽度绝不低于内容宽度（否则会被横向切掉）；高度按比例跟随，
            # 屏幕不够高时降到屏幕上限（此时页面纵向滚动，比例按实际重新锁定）
            w = max(_px(420), min(base_w, avail_w))
            h = max(_px(320), min(int(w * (base_h / base_w)), avail_client))
            return int(w), int(h)
        except Exception:
            traceback.print_exc()
            return None

    def _apply_layout(self, size: tuple[int, int]) -> None:
        w, h = size
        self._note_hidden_change(size)
        try:
            self._resize_no_erase(w, h)
            self.root.minsize(_px(420), _px(320))
            # 用户不能拖边框缩放 —— 但**不能**用 resizable(False, False)：
            # 那会让 Tk 把「最小=最大=当前尺寸」报给系统，而 Tk 的边框换算仍是主屏那套，
            # 在 144 DPI 屏上拖标题栏移动时每步会被系统钳掉 6px（一路缩到 minsize），
            # 且每次钳制都伴随整窗重绘 → 「越来越窄 + 卡」两个症状同源。
            # 改为放开 resizable + `_on_root_configure` 守卫：尺寸被外部改动就拉回来。
            #
            # 注意：`wm resizable` 只在 `__init__` 里调一次，这里**绝不能**再调 ——
            # Tk 用「销毁并重建顶层窗口」实现它，放在排版里等于每次缩放都让窗口
            # 消失再出现一次（「窗口重新出现」的真凶，逐帧取证见 __init__ 处注释）。
            self.root.update_idletasks()
            self._sync_pages()
            # 用户不能拖边框缩放：去掉 WS_THICKFRAME（标准 Win32 做法）。
            # 内部有「已经是目标样式就直接返回」的短路，所以每次排版调用很便宜。
            self._remove_thick_frame()
        except Exception:
            traceback.print_exc()

    def _resize_no_erase(self, w: int, h: int) -> None:
        """改窗口尺寸时不让 Windows 先用背景刷涂新露出的区域。

        这是重排里最后一点「轻微闪白」的来源：`wm geometry` 之后新露出的那块
        会被系统用背景刷擦一遍（纯主题底色），随后 Tk 才把遮罩画上去 ——
        中间那一帧就是白边。做法是先 `WM_SETREDRAW=0` 关掉这个窗口的重绘，
        改完尺寸立刻开回来并**同步**重画（遮罩这时已经盖在上面，画完即无缝）。

        同时把目标尺寸记为「期望尺寸」并打上 `_applying_size` 标记：
        `_on_root_configure` 靠它区分「应用自己改的尺寸」与「用户拖边框改的尺寸」。
        """
        self._expect_size = (int(w), int(h))
        self._applying_size = True
        try:
            self._resize_no_erase_inner(w, h)
        finally:
            self._applying_size = False

    def _resize_no_erase_inner(self, w: int, h: int) -> None:
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnd = user32.GetAncestor(self.root.winfo_id(), 2) or self.root.winfo_id()
            WM_SETREDRAW = 0x000B
            RDW_INVALIDATE, RDW_ALLCHILDREN, RDW_UPDATENOW = 0x0001, 0x0080, 0x0100
            # 只对「看得见的普通窗口」冻结重绘。微软文档写明：**对隐藏窗口发送
            # WM_SETREDRAW(TRUE) 会让它变可见**（实测 IsWindowVisible 由 False 变 True）
            # —— 建窗阶段窗口是 withdrawn，照发就会把本该隐藏到托盘的窗口弄出来，
            # 连带 `show_now()` 误判成「已经打开」而跳过遮罩与快照。
            freeze = bool(user32.IsWindowVisible(hwnd)) and not bool(user32.IsIconic(hwnd))
            if freeze:
                user32.SendMessageW(hwnd, WM_SETREDRAW, 0, 0)
            try:
                self.root.geometry(f"{w}x{h}")
            finally:
                if freeze:
                    user32.SendMessageW(hwnd, WM_SETREDRAW, 1, 0)
                    # 不擦背景（与 _force_repaint 同一个道理），并把子控件一起重画
                    user32.RedrawWindow(hwnd, None, 0,
                                        RDW_INVALIDATE | RDW_ALLCHILDREN | RDW_UPDATENOW)
        except Exception:
            try:
                self.root.geometry(f"{w}x{h}")
            except Exception:
                pass

    def _install_hittest_guard(self) -> None:
        """把窗口边框的命中测试改判为标题栏：用户拖不到边框，就无法缩放窗口。

        为什么不用 `resizable(False, False)`：它会让 Tk 把「最小=最大=当前尺寸」
        报给系统，在高 DPI 副屏拖标题栏移动时会被系统按错误单位逐帧钳小
        （实测每步 -6px，越拖越窄，且每次钳制伴随整窗重绘 → 很卡）。
        改在命中测试层面拒绝缩放，不碰任何尺寸约束，因此没有那个副作用；
        附带效果：拖边框现在等于拖标题栏（移动窗口），比"拖了没反应"更合理。

        ctypes 细节（这里踩过坑）：`GetWindowLongPtrW` / `SetWindowLongPtrW` /
        `CallWindowProcW` 必须把 restype/argtypes 全设对 —— 默认 restype=c_int
        会把 64 位函数指针截断，`CallWindowProcW` 立刻访问违例、窗口被打崩。
        回调体整体 try/except：绝不让 Python 异常窜进窗口过程。

        安装时机也很关键：必须在窗口**映射之后**装 —— 建窗阶段 Tk 稍后还会重设
        窗口过程，那时装的会被覆盖（实测 `__init__` 里装完左边缘仍是 HTLEFT）。
        因此这里带「已映射 + 只装一次」两道保护，由 `_apply_layout` / `show_now` 调用。
        """
        if getattr(self, "_hittest_done", False):
            return
        if not self._window_visible():
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            hwnd = user32.GetAncestor(self.root.winfo_id(), 2) or self.root.winfo_id()
            GWLP_WNDPROC = -4
            WM_NCHITTEST = 0x0084
            HTCAPTION = 2
            RESIZE_CODES = {10, 11, 12, 13, 14, 15, 16, 17}   # L/R/T/TL/TR/B/BL/BR
            LONG_PTR = ctypes.c_ssize_t
            WNDPROC = ctypes.WINFUNCTYPE(LONG_PTR, wintypes.HWND, ctypes.c_uint,
                                         ctypes.c_size_t, LONG_PTR)
            user32.GetWindowLongPtrW.restype = LONG_PTR
            user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetWindowLongPtrW.restype = LONG_PTR
            user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, LONG_PTR]
            user32.CallWindowProcW.restype = LONG_PTR
            user32.CallWindowProcW.argtypes = [LONG_PTR, wintypes.HWND, ctypes.c_uint,
                                               ctypes.c_size_t, LONG_PTR]
            old = user32.GetWindowLongPtrW(hwnd, GWLP_WNDPROC)
            if not old:
                return

            def proc(h, msg, wp, lp):          # noqa: ANN001
                try:
                    res = user32.CallWindowProcW(old, h, msg, wp, lp)
                    if msg == WM_NCHITTEST and int(res) in RESIZE_CODES:
                        return HTCAPTION
                    return res
                except Exception:              # 绝不让异常窜进窗口过程
                    return 0

            self._hittest_proc = WNDPROC(proc)  # 必须持有引用，否则回调被回收
            user32.SetWindowLongPtrW(
                hwnd, GWLP_WNDPROC,
                ctypes.cast(self._hittest_proc, ctypes.c_void_p).value or 0)
            self._hittest_done = True
        except Exception:
            pass

    def _remove_thick_frame(self) -> None:
        """去掉窗口的「尺寸边框」样式：用户无法再拖边框缩放。

        这是 Win32 的标准做法（微软文档：`WS_THICKFRAME` = 窗口拥有 sizing border），
        不用两条更危险的路线：
          · `resizable(False, False)` —— Tk 会把允许尺寸范围卡成当前尺寸，
            在高 DPI 副屏上拖标题栏移动时每步被系统钳小 6px（实测，越拖越窄 + 卡）；
          · `WM_NCHITTEST` 子类化 —— 实测缩放一次后回调失效、边缘变成 HTNOWHERE(0)。
        样式改动用 `SWP_FRAMECHANGED` 生效，且带 `SWP_NOSIZE`，不会动窗口尺寸。
        顺带去掉最大化按钮（窗口尺寸由应用决定）。
        """
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
            user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                                 ctypes.c_ssize_t]
            hwnd = user32.GetAncestor(self.root.winfo_id(), 2) or self.root.winfo_id()
            GWL_STYLE = -16
            WS_THICKFRAME, WS_MAXIMIZEBOX = 0x00040000, 0x00010000
            style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
            want = style & ~WS_THICKFRAME & ~WS_MAXIMIZEBOX
            if want == style:
                return                       # 已经去掉过，别再刷一遍
            user32.SetWindowLongPtrW(hwnd, GWL_STYLE, want)
            SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER = 0x0001, 0x0002, 0x0004
            SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x0010, 0x0020
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                                SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER
                                | SWP_NOACTIVATE | SWP_FRAMECHANGED)
            self._thickframe_removed = True
        except Exception:
            pass

    def _guard_window_size(self, event) -> None:
        """把被外部改动的窗口尺寸拉回设定值（替代 resizable(False) 的硬锁定）。

        只用 `resizable(False, False)` 会让 Tk 向系统报「最小=最大=当前尺寸」，
        而 Tk 的边框换算用的是主屏 DPI —— 在 144 DPI 屏上拖标题栏移动时，
        系统每移动一步就把窗口钳小 6px，一路缩到 minsize 才停；每次钳制还要整窗重绘，
        于是表现为「越拖越窄 + 很卡」。这里改成：放开 resizable，自己守卫尺寸。

        只在「尺寸确实被改了」且「不是应用正在改」时动作，位置变化完全不干预，
        所以拖动窗口（移动）期间它一次都不会触发。
        """
        try:
            if event.widget is not self.root:
                return
            if getattr(self, "_applying_size", False) or getattr(self, "_rebuilding", False):
                return
            exp = getattr(self, "_expect_size", None)
            if not exp:
                return
            w, h = int(self.root.winfo_width()), int(self.root.winfo_height())
            if abs(w - exp[0]) <= 2 and abs(h - exp[1]) <= 2:
                return
            self._resize_no_erase(exp[0], exp[1])
        except Exception:
            pass

    # ── 窗口缩放（只有设置页「放大 / 缩小」按钮；窗口不可拖动）────────────
    def set_user_scale(self, value: float, persist: bool = True) -> None:
        """设定界面缩放倍数并重建（1.0 = 100%）。"""
        old = _user_scale()
        new = set_user_scale(value)
        if persist:
            try:
                self.cfg.ui_scale = float(new)
                self.cfg.save()
            except Exception:
                pass
        if abs(new - old) < 1e-6:
            self._sync_scale_selector()
            return
        with self._reflow_mask():
            self._rebuild_for_dpi()
        self._sync_scale_selector()

    def _sync_scale_selector(self) -> None:
        lbl = getattr(self, "lbl_scale_value", None)
        if lbl is None:
            return
        try:
            pct = int(round(_user_scale() * 100))
            lbl.configure(text=f"{pct}%")
        except Exception:
            pass

    def _scale_step_index(self) -> int:
        pct = int(round(_user_scale() * 100))
        target = f"{pct}%"
        if target in SCALE_CHOICES:
            return SCALE_CHOICES.index(target)
        # 非整档（历史拖动留下的比例）吸附到最近档
        values = [int(s.rstrip("%")) for s in SCALE_CHOICES]
        return min(range(len(values)), key=lambda i: abs(values[i] - pct))

    def _on_scale_zoom_in(self) -> None:
        idx = min(self._scale_step_index() + 1, len(SCALE_CHOICES) - 1)
        self.set_user_scale(int(SCALE_CHOICES[idx].rstrip("%")) / 100.0)

    def _on_scale_zoom_out(self) -> None:
        idx = max(self._scale_step_index() - 1, 0)
        self.set_user_scale(int(SCALE_CHOICES[idx].rstrip("%")) / 100.0)

    # ── 重排遮罩 ────────────────────────────────────────────────────────
    # 做法：在设置窗口**内部**铺一个覆盖整块客户区的图片控件，显示模糊快照。
    # 不用独立浮窗的原因（踩过的坑）：
    #   · 独立窗口要和主窗口对齐只能靠外框矩形，会被 Win10/11 的隐形缩放边框撑大一圈；
    #   · 主窗口是 Win11 圆角，方形浮窗的四角会盖出去；
    #   · 浮窗要么 -topmost（主窗口被别的窗口压住时它还浮在最上层），要么费劲维护 z 序；
    # 放在窗口内部，这三个问题自然都没有了。
    _MASK_HOLD_MS = 320          # 界面画完之后再保持遮罩的时长

    def _reflow_mask(self):
        return _ReflowMask(self)

    def _mask_ensure_overlay(self):
        """拿到（必要时创建）遮罩控件状态。"""
        st = self._overlay
        if st is None:
            lbl = tk.Label(self.root, bd=0, highlightthickness=0, bg=WINDOW_BG)
            # 重建界面会销毁窗口的全部子控件：登记进 _float_wins 让销毁循环跳过它
            self._float_wins = [w for w in self._float_wins if self._alive(w)]
            self._float_wins.append(lbl)
            st = {"lbl": lbl, "img": None, "img_size": None, "photo": None,
                  "solid": False, "hide_job": None, "token": None}
            self._overlay = st
        else:
            self._mask_cancel_hide()      # 复用旧遮罩：取消上一次排队的撤罩，重新计时
        return st

    def _mask_show(self) -> None:
        if not self._window_visible():
            return
        try:
            # 最小化到任务栏时不要抓屏：那时窗口矩形里是**别的应用**，
            # 抓下来当封面，还原时会闪出一块完全不相干的模糊图
            if self.root.state() != "normal":
                return
        except Exception:
            return
        self._mask_ensure_overlay()
        self._mask_snapshot()
        self._mask_fit()
        self._mask_paint()

    def _mask_cover_for_show(self) -> bool:
        """打开窗口前先把遮罩贴好（此时窗口还没显示，不能用可见性判断）。

        用上次关闭时抓的模糊快照当封面；没有就退化成主题底色（纯色），
        两者都能把「控件逐个画出来」的过程盖住。窗口还没映射时 `winfo_width()`
        会是 1，`_mask_place_to` 会因此跳过贴图 —— 这里直接按 relwidth/relheight
        铺满，尺寸由窗口自己给。

        **只在快照尺寸 == 当前窗口尺寸时**才用那张模糊图：尺寸对不上说明隐藏期间
        界面变过（签到横幅出现/收起、密度阶梯换档、改过缩放/看板），把旧图拉伸上去
        就是「底图与实际界面不一致」——这时宁可退回纯底色，也不糊一张错位的图。
        """
        st = self._mask_ensure_overlay()
        try:
            w = max(1, int(self.root.winfo_width()))
            h = max(1, int(self.root.winfo_height()))
            if w < 50 or h < 50:
                # 还原瞬间 Tk 还没算好窗口尺寸（实测拿到 134x1）：这时用**快照自身的
                # 尺寸**去缩放，别让这个假尺寸把模糊图判掉 —— 否则封面退化成纯色，
                # 用户看到的就是「任务栏打开没有模糊」，而且是随机出现（取决于这一拍时序）。
                w, h = st.get("img_size") or st.get("placed_size") or (w, h)
            img = st.get("img")
            # 用模糊图的条件：有快照、且不是「隐藏/最小化期间布局变过」留下的旧布局。
            # **不要求尺寸严格相等** —— 尺寸差一点只是把同一套界面缩放着贴（重排路径本来
            # 就这么干），而严格相等会让「改过缩放之后再最小化/还原」永远退化成纯色封面
            # （用户实测「任务栏打开没有模糊」，根因就是这个判据太严）。
            # 真正要防的是「内容/布局变过」：那由 _hidden_changed 负责。
            use_blur = (img is not None and not self._hidden_changed)
            # 记成粘性状态：后面 _mask_fit() → _mask_place_to() 还会再贴一次，
            # 不记住「这次用的是纯底色」就会把旧模糊图又贴回去
            st["solid"] = not use_blur
            if use_blur:
                from PIL import ImageTk
                photo = ImageTk.PhotoImage(img.resize((w, h)))
                st["photo"] = photo
                st["lbl"].configure(image=photo)
                st["placed_size"] = (w, h)
            else:
                st["lbl"].configure(image="")       # 纯主题底色封面
                st["photo"] = None
                st["placed_size"] = None
            st["lbl"].place(x=0, y=0, relwidth=1, relheight=1)
            st["lbl"].lift()
            self._hidden_changed = False        # 封面已经按当前状态定下来了
        except Exception:
            return False
        return True

    def _mask_cover_on_restore(self) -> None:
        """从任务栏还原时也把遮罩盖上（这条路不经过 show_now）。

        用上次留下的快照当封面，**不重新抓屏**：窗口刚还原，抓到的画面可能还没画完，
        而且会抓到「已经模糊的封面」再糊一遍 —— 每还原一次就更糊一层。
        没有快照时 `_mask_cover_for_show()` 会退化成纯主题底色，同样能盖住重画。
        """
        try:
            if not self._mask_cover_for_show():
                return
            self._mask_paint()          # 立刻把封面画到屏幕上，别等事件循环
            self._mask_fit()
            self._mask_schedule_hide()
        except Exception:
            pass

    def _mask_schedule_hide(self) -> None:
        """保持一小段后一次性撤罩（带令牌，避免旧定时器撤掉新遮罩）。"""
        st = self._overlay
        if st is None:
            return
        self._mask_cancel_hide()
        token = id(st["lbl"])
        st["token"] = token
        st["hide_job"] = self.root.after(
            self._MASK_HOLD_MS, lambda t=token: self._mask_hide_if(t))                # 重排是同步阻塞的，不主动画这一下就还是旧画面

    def _mask_paint(self) -> None:
        """让遮罩**立刻出现在屏幕上**。

        Tk 的 `update_idletasks()` 只跑 idle 任务，不处理 Expose —— 也就是说
        「place 了遮罩」只改了内部几何，屏幕还是上一帧。而重排（重建 160+ 控件）
        是同步跑完的，期间事件循环不转，所以不主动画一次的话：
        用户先看到没被盖住的窗口，等重排结束遮罩才冒出来 —— 就是「先重排、后遮罩」。
        同理，窗口改完尺寸那一下新露出的区域也是空的（「窗口重新出现」）。
        """
        if not self._overlay:
            return
        try:
            self._force_repaint()         # 整窗标脏（含遮罩），否则 Tk 不会重画它
        except Exception:
            pass
        try:
            self.root.update()            # 立刻处理 Expose，真正重绘
        except Exception:
            pass

    def _mask_lift_if_shown(self) -> None:
        """重建后把遮罩抬回顶层。

        Tk 里新建的控件叠在已有兄弟之上 —— 密度阶梯会触发**嵌套重建**，
        新控件把遮罩盖住，那一瞬间用户看到的是清晰界面（闪白）。
        """
        st = self._overlay
        if not st:
            return
        try:
            lbl = st["lbl"]
            if lbl.winfo_manager() == "place":
                lbl.lift()
        except Exception:
            pass

    def _mask_snapshot(self) -> None:
        """抓当前客户区并做模糊（先缩小再模糊：像素少，代价低、效果均匀）。"""
        st = self._overlay
        if not st:
            return
        try:
            from PIL import Image, ImageFilter, ImageGrab
            w = max(1, int(self.root.winfo_width()))
            h = max(1, int(self.root.winfo_height()))
            x = int(self.root.winfo_rootx())
            y = int(self.root.winfo_rooty())
            # all_screens=True 不能省：默认只抓主屏，窗口在负坐标的副屏上会抓到全黑图
            shot = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
            if shot.size != (w, h):      # 窗口有部分在屏幕外：抓到的比要小，补齐底色
                img = Image.new("RGB", (w, h), WINDOW_BG)
                img.paste(shot, (0, 0))
            else:
                img = shot.convert("RGB")
            small = img.resize((max(1, w // 3), max(1, h // 3)))
            st["img"] = small.filter(ImageFilter.GaussianBlur(3))
            st["img_size"] = (w, h)       # 记下抓图时的窗口尺寸，见 _mask_cover_for_show
            st["solid"] = False           # 有了新图，就不再是纯底色封面
            st["placed_size"] = None      # 图换了，之前缓存的贴图尺寸作废
            self._hidden_changed = False  # 刚抓的图就是当前界面，不再是「隐藏期间变过」
        except Exception:
            st["img"] = None
            st["img_size"] = None

    def _mask_fit(self) -> None:
        """把遮罩铺满当前客户区（尺寸变了要重铺图）。"""
        self._mask_place_to(int(self.root.winfo_width()), int(self.root.winfo_height()))

    def _mask_cover(self, size: tuple[int, int]) -> None:
        """窗口即将变大时先把遮罩铺到目标尺寸，避免新露出的那块没被盖住。"""
        if not self._overlay:
            return
        try:
            w = max(int(size[0]), int(self.root.winfo_width()))
            h = max(int(size[1]), int(self.root.winfo_height()))
            self._mask_place_to(w, h)
        except Exception:
            pass

    def _mask_place_to(self, w: int, h: int) -> None:
        st = self._overlay
        if not st or w < 50 or h < 50 or not self._alive(st["lbl"]):
            return
        try:
            from PIL import ImageTk
            lbl = st["lbl"]
            if st.get("solid") or st.get("img") is None:
                # 纯底色封面：把上次贴上去的图摘掉，别留一张错位的旧图在下面
                if st.get("photo") is not None:
                    lbl.configure(image="")
                    st["photo"] = None
                    st["placed_size"] = None
            else:
                # 尺寸没变就别重做「回放大 + PhotoImage」：一次重排会连着贴好几遍
                # （_mask_cover 撑到目标尺寸 → _apply_layout → _mask_fit 都是同一尺寸，
                #   撤罩前还会再贴两次），每次都是几十毫秒的整幅缩放。
                same = st.get("placed_size") == (w, h) and lbl.winfo_manager() == "place"
                if not same:
                    photo = ImageTk.PhotoImage(st["img"].resize((w, h)))
                    st["photo"] = photo
                    lbl.configure(image=photo)
                    st["placed_size"] = (w, h)
            lbl.place(x=0, y=0, relwidth=1, relheight=1)
            lbl.lift()          # 重建后新控件建在它上面，必须抬起来
            self.root.update_idletasks()
        except Exception:
            pass

    def _mask_cancel_hide(self) -> None:
        """取消已排队的撤罩计时器。"""
        st = self._overlay
        if not st:
            return
        job = st.get("hide_job")
        if job is None:
            return
        try:
            self.root.after_cancel(job)
        except Exception:
            pass
        st["hide_job"] = None

    def _mask_hide_if(self, token) -> None:
        """只撤掉「当初排队时那一个」遮罩。

        重排可能在保持期内又发生一次（连点缩放、签到横幅出现/收起），
        旧的定时器若无条件撤罩，就会把新遮罩顺手杀掉 —— 表现为遮罩一闪而过。
        """
        st = self._overlay
        if st is None or st.get("token") != token:
            return
        self._mask_hide()

    def _mask_hide(self) -> None:
        """撤掉遮罩（控件留着复用，只 place_forget）。"""
        st = self._overlay
        if not st:
            return
        st["hide_job"] = None
        st["token"] = None
        try:
            st["lbl"].place_forget()
        except Exception:
            pass

    def _force_repaint(self) -> None:
        """把整窗标脏，让 Tk 立刻重画一遍（被遮挡时 Windows 会跳过重画）。

        **必须 bErase=False**：传 True 会先把窗口擦成背景色（纯色/白），
        而 Tk 的 WM_PAINT 只重画它自己认为脏的控件 —— 擦掉的那块就留在屏幕上，
        用户看到的就是「窗口变成一块白，然后元素逐个冒出来」。
        标脏但不擦，Tk 才会在原有内容上把该画的都画出来。
        """
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnd = user32.GetAncestor(self.root.winfo_id(), 2) or self.root.winfo_id()
            user32.InvalidateRect(hwnd, None, False)
            user32.UpdateWindow(hwnd)
        except Exception:
            pass

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
            dpi, straddling = monitor_dpi(self.root)
            changed = bool(dpi) and abs(dpi - self._dpi_now) >= 24
            if not changed:
                self._dpi_pending = 0
            elif straddling:
                # 同时压在两块 DPI 不同的显示器上：等拖到任一侧稳定下来再重排
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

    def _window_visible(self) -> bool:
        try:
            return bool(self.root.winfo_viewable()) and self.root.state() != "withdrawn"
        except Exception:
            return False

    @staticmethod
    def _alive(widget) -> bool:
        try:
            return bool(widget.winfo_exists())
        except Exception:
            return False

    def _apply_dpi_change(self, dpi: int) -> None:
        """确认 DPI 稳定变化后换算比例并重建界面。"""
        self._dpi_now = dpi
        self._dpi_pending = 0
        self._last_dpi_apply = time.time()
        apply_dpi_to_tk(self.root, dpi)
        with self._reflow_mask():
            self._rebuild_for_dpi()

    def _rebuild_for_dpi(self) -> None:
        """按新 DPI / 缩放重建界面（保留勾选状态与当前页签），随后重新适配排版。

        两处关键：
        1) 窗口已经可见时不再套用「临时占满屏幕高度」的尺寸（那会先拉成长条再缩回）；
        2) 有遮罩时**先把遮罩撑到目标尺寸**再改窗口尺寸 —— 反过来做的话，
           窗口先变大的一瞬间会从遮罩边缘露出新画面，看起来就是「闪一下」。
        """
        if not self._window_visible():
            self._apply_window_metrics()
        masked = self._overlay is not None
        self._rebuild_ui(refresh=True)
        size = self._plan_layout()
        if size and masked:
            self._mask_cover(size)          # 先撑大遮罩，覆盖即将出现的区域
        if size:
            self._apply_layout(size)
            if masked:
                # 改完尺寸**立刻**重画：窗口变大时新露出的那块区域是空的，
                # 用户会看到「窗口重新出现」一下（DWM 先把外框画大，客户区还是空的）。
                # 遮罩图在 _mask_cover 里已经预先撑到目标尺寸，所以这一下就是全覆盖。
                self._mask_paint()
        if masked:
            self._mask_fit()                # 尺寸定下后再精确贴合
            self._mask_paint()

    def _rebuild_ui(self, refresh: bool = True) -> None:
        """重建界面（保留勾选状态与当前页签）。"""
        if getattr(self, "_rebuilding", False):   # 重建期间不再接受新的重排
            return
        self._rebuilding = True
        # 重建会重建横幅控件，先记住它在不在显示，建完再恢复，
        # 否则切主题/改缩放会把「签到中」横幅连带标题一起弄没
        was_busy = bool(getattr(self, "_busy_visible", False))
        busy_detail = ""
        try:
            busy_detail = self._busy_detail.get()
        except Exception:
            pass
        try:
            idx = 0
            try:
                idx = self.nb.index(self.nb.select())
            except Exception:
                pass
            for child in list(self.root.winfo_children()):
                if child in getattr(self, "_float_wins", ()):   # 遮罩等浮层不销毁
                    continue
                child.destroy()
            # 其中几个由 _build 重新创建；这里顺手清掉，避免留下旧变量
            for name in ("_board_vars", "_game_vars", "_board_cbs"):
                getattr(self, name, {}).clear()
            self._cloud_token_status = {}
            self._build()
            if was_busy:
                try:                 # 恢复重建前的「签到中」横幅与标题
                    self._show_busy(busy_detail or self.status_text or "签到进行中…")
                except Exception:
                    pass
            # 新控件在 Tk 里叠在已有兄弟之上：把遮罩抬回顶层并立刻重画，
            # 否则嵌套重建（密度阶梯会调 _set_density）那一瞬间会露出清晰界面
            self._mask_lift_if_shown()
            self._mask_paint()
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
        try:                     # 窗口底色要一起换，否则留白处会留着上一套主题的颜色
            self.root.configure(bg=WINDOW_BG)
        except Exception:
            pass
        try:
            _theme.apply_window_style(self.root, mica=True)
        except Exception:
            pass
        if rebuild:
            with self._reflow_mask():
                self._rebuild_for_dpi()
        if getattr(self, "_log_win", None) is not None:
            try:                 # 日志窗口是按旧配色建的，重建一份
                self._close_log()
                self._show_log()
            except Exception:
                pass

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
        self._build_busy_banner(page_bg)
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
        self._btn_run = tk.Button(
            btns, text="立即签到", bg=ACCENT, fg="white", relief="flat",
            font=_ui_font(10, "bold"), padx=_px(14), pady=_px(6),
            activebackground=LINE, activeforeground=TEXT,
            disabledforeground="#FFFFFF", command=self._run_now,
        )
        self._btn_run.pack(side="left")
        self._btn_save = tk.Button(
            btns, text="保存设置", bg=BTN_BG, fg=BTN_FG, relief="flat",
            font=_ui_font(10, "bold"), padx=_px(14), pady=_px(6), command=self._save,
        )
        self._btn_save.pack(side="left", padx=_px(8))
        self.dirty_var = tk.StringVar(value="")
        self.lbl_dirty = tk.Label(
            btns, textvariable=self.dirty_var, bg=page_bg, fg=WARN,
            font=_ui_font(10, "bold"))
        self.lbl_dirty.pack(side="left", padx=_px(6))
        # 保存成功的即时反馈（不弹模态框，避免打断操作）
        self.saved_var = tk.StringVar(value="")
        self.lbl_saved = tk.Label(
            btns, textvariable=self.saved_var, bg=page_bg, fg=ACCENT,
            font=_ui_font(10, "bold"))
        self.lbl_saved.pack(side="left", padx=_px(2))
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
        self._mark_saved()          # 建好界面后：当前取值即为「已保存」基线

    # ── 供托盘调用的线程安全接口（后台线程 → UI 线程）────────────────────
    def set_running(self, running: bool) -> None:
        """托盘侧告知「正在签到」，用于显示/隐藏横幅（可从后台线程调用）。"""
        try:
            self._ui_queue.put(lambda: self._apply_running(running))
        except Exception:
            pass

    def set_status(self, text: str) -> None:
        """托盘侧推送实时状态文本（可从后台线程调用）。"""
        if not text:
            return
        try:
            self._ui_queue.put(lambda t=text: self._apply_status(t))
        except Exception:
            pass

    def _apply_running(self, running: bool) -> None:
        if running:
            self._show_busy(self.status_text or "签到进行中…")
            self._checkin_running = True       # 让状态行也进入等待态
            if self._wait_job is None:
                self._tick_waiting()
        else:
            self._stop_waiting()

    def _apply_status(self, text: str) -> None:
        self.status_text = text
        if self._busy_visible:
            self._busy_detail.set(text)        # 横幅里显示实时细节
            return
        try:
            self.status_var.set(text)
        except Exception:
            pass

    # ── 签到中横幅（固定框架里，任何页都能看到）──────────────────────────
    def _build_busy_banner(self, page_bg: str) -> None:
        """签到进行时显示的醒目横条：转动的进度 + 已用时间 + 实时状态。"""
        self._busy = tk.Frame(self.root, bg=page_bg)
        card = RoundedCard(self._busy, surround=page_bg, radius=8, pad=_px(10))
        card.pack(fill="x")
        body = card.body
        top = tk.Frame(body, bg=CARD)
        top.pack(fill="x")
        self._busy_var = tk.StringVar(value="正在签到…")
        tk.Label(top, textvariable=self._busy_var, bg=CARD, fg=ACCENT,
                 font=_ui_font(11, "bold")).pack(side="left")
        self._busy_time = tk.StringVar(value="")
        tk.Label(top, textvariable=self._busy_time, bg=CARD, fg=MUTED,
                 font=_ui_font(9)).pack(side="right")
        self._busy_bar = ProgressPill(body, bg=CARD, width=560)
        self._busy_bar.pack(fill="x", pady=(_px(6), 0))
        self._busy_detail = tk.StringVar(value="")
        tk.Label(body, textvariable=self._busy_detail, bg=CARD, fg=MUTED,
                 font=_ui_font(9), anchor="w", justify="left",
                 wraplength=_px(560)).pack(fill="x", pady=(_px(4), 0))
        self._busy_started = 0.0
        self._busy_elapsed_job = None
        self._busy_visible = False

    def _show_busy(self, detail: str = "") -> None:
        """显示签到中横幅（重复调用只更新文案，不重复启动动画）。"""
        if not hasattr(self, "_busy"):
            return
        if not self._busy_visible:
            self._busy_visible = True
            self._busy_started = time.time()
            self._busy.pack(fill="x", padx=_px(20), pady=(_px(8), 0),
                            before=self.content)
            try:
                self._busy_bar.start()
            except Exception:
                pass
            self._tick_busy_elapsed()
            self._set_run_button(running=True)
            self._set_window_title(True)
            with self._reflow_mask():
                self._fit_layout()           # 横幅占高，重新适配排版
        if detail:
            self._busy_detail.set(detail)

    def _hide_busy(self) -> None:
        if not hasattr(self, "_busy") or not self._busy_visible:
            return
        self._busy_visible = False
        try:
            self._busy.pack_forget()
        except Exception:
            pass
        try:
            self._busy_bar.stop()
        except Exception:
            pass
        if self._busy_elapsed_job is not None:
            try:
                self.root.after_cancel(self._busy_elapsed_job)
            except Exception:
                pass
            self._busy_elapsed_job = None
        self._set_run_button(running=False)
        self._set_window_title(False)
        with self._reflow_mask():
            self._fit_layout()

    def _tick_busy_elapsed(self) -> None:
        self._busy_elapsed_job = None
        if not self._busy_visible:
            return
        try:
            used = int(time.time() - self._busy_started)
            self._busy_time.set(f"已用 {used // 60:02d}:{used % 60:02d}")
            self._busy_elapsed_job = self.root.after(1000, self._tick_busy_elapsed)
        except Exception:
            self._busy_elapsed_job = None

    def _set_run_button(self, running: bool) -> None:
        btn = getattr(self, "_btn_run", None)
        if btn is None:
            return
        try:
            btn.configure(text="签到中…" if running else "立即签到",
                          bg=MUTED if running else ACCENT,
                          state="disabled" if running else "normal")
        except Exception:
            pass

    def _set_window_title(self, running: bool) -> None:
        try:
            self.root.title("米游社自动签到器 · 设置（签到中…）" if running
                            else "米游社自动签到器 · 设置")
        except Exception:
            pass

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

        size_row = tk.Frame(card, bg=CARD)
        size_row.pack(fill="x", pady=(_px(10), 0))
        tk.Label(size_row, text="窗口大小", bg=CARD, fg=TEXT, font=_ui_font(10)).pack(side="left")
        pct = int(round(_user_scale() * 100))
        self.lbl_scale_value = tk.Label(
            size_row, text=f"{pct}%", bg=CARD, fg=TEXT, font=_ui_font(10, "bold"),
            width=6, anchor="center")
        self.lbl_scale_value.pack(side="left", padx=_px(8))
        tk.Button(
            size_row, text="缩小窗口", bg=CARD, fg=TEXT, activebackground=LINE,
            activeforeground=TEXT, relief="flat", font=_ui_font(10),
            padx=_px(8), pady=_px(3), command=self._on_scale_zoom_out,
        ).pack(side="left", padx=_px(2))
        tk.Button(
            size_row, text="放大窗口", bg=ACCENT, fg="white", activebackground=LINE,
            activeforeground=TEXT, relief="flat", font=_ui_font(10),
            padx=_px(8), pady=_px(3), command=self._on_scale_zoom_in,
        ).pack(side="left", padx=_px(2))

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
        """只刷新「上次运行」相关字段。

        这里不能整体替换 self.cfg：重建界面（切主题 / 改缩放 / 跨屏）时会走到这里，
        整体重载会把用户尚未保存的改动（含刚设置的缩放）一起丢掉。
        """
        try:
            fresh = TrayConfig.load()
            self.cfg.last_run = fresh.last_run
            self.cfg.last_ok_run = fresh.last_ok_run
            self.cfg.last_status = fresh.last_status
        except Exception:
            pass
        try:
            self.lastrun_var.set(self._last_run_text())
        except Exception:
            pass

    # ── 运行态同步：上次运行 / 签到中横幅与标题 ──────────────────────────
    def _sync_runtime(self) -> None:
        """从磁盘与托盘侧同步运行状态（每秒一次，不重建界面）。

        托盘菜单/定时触发的签到不会走设置窗自己的按钮逻辑，若只在按钮回调里刷新，
        「上次运行」会一直是旧值、标题栏也不会显示「签到中…」。
        """
        now = time.time()
        if now - self._runtime_checked < 1.0:
            return
        self._runtime_checked = now

        try:
            fresh = TrayConfig.load()
            if (fresh.last_run, fresh.last_ok_run, fresh.last_status) != (
                    self.cfg.last_run, self.cfg.last_ok_run, self.cfg.last_status):
                self.cfg.last_run = fresh.last_run
                self.cfg.last_ok_run = fresh.last_ok_run
                self.cfg.last_status = fresh.last_status
                self._refresh_last_run()
        except Exception:
            pass

        if self.is_running is None:
            return
        try:
            running = bool(self.is_running())
        except Exception:
            return
        if running and not self._busy_visible:
            self._show_busy(self.status_text or "签到进行中…")
        elif not running and self._busy_visible and not self._checkin_running:
            self._hide_busy()

    # ── 未保存改动提示 ──────────────────────────────────────────────────
    def _settings_fingerprint(self) -> tuple:
        """当前界面上的设置项取值（用于判断是否有未保存改动）。"""
        try:
            visible = {row[0] for row in self._board_view()}
            boards = tuple(sorted(gid for gid, var in self._board_vars.items()
                                  if var.get() and gid in visible))
            games = tuple(sorted(k for k, var in self._game_vars.items() if var.get()))
            return (
                self.var_sched.get(), self.var_hour.get(), self.var_minute.get(),
                self.ent_delay.get().strip(),
                boards, games,
                self.var_cloud_genshin.get(), self.var_cloud_sr.get(), self.var_cloud_zzz.get(),
                self.var_autostart.get(), self.var_min_tray.get(),
                self.var_launch_run.get(), self.var_silent.get(),
                tuple(self.cfg.board_order or ()), tuple(self.cfg.board_hidden or ()),
            )
        except Exception:
            return ()

    def _mark_saved(self) -> None:
        """记录「界面当前状态 = 已保存状态」。"""
        self._saved_fp = self._settings_fingerprint()
        self._update_dirty_hint()

    def _check_dirty(self) -> None:
        now = time.time()
        if now - self._dirty_checked < 0.3:
            return
        self._dirty_checked = now
        self._update_dirty_hint()

    def _update_dirty_hint(self) -> None:
        lbl = getattr(self, "lbl_dirty", None)
        if lbl is None:
            return
        fp = self._settings_fingerprint()
        dirty = bool(self._saved_fp) and fp != self._saved_fp
        try:
            self.dirty_var.set("● 有未保存的更改" if dirty else "")
            self.lbl_dirty.configure(fg=WARN if dirty else _theme.palette()["window"])
            btn = getattr(self, "_btn_save", None)
            if btn is not None:
                btn.configure(bg=WARN if dirty else BTN_BG,
                              fg="#FFFFFF" if dirty else BTN_FG)
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
        self._mark_saved()               # 立即落盘，界面即「已保存」状态
        self._sync_hidden_button()

    def _restore_all_boards(self) -> None:
        """全部显示（子菜单里的按钮）。"""
        self.cfg.board_hidden = []
        try:
            self.cfg.save()
        except Exception:
            pass
        self._render_board_rows()
        self._mark_saved()
        self._sync_hidden_button()

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
            self.snap_var.set(format_account_status(info, self.cfg.account_nickname))
            self.login_var.set(info["error"])
            return
        self.snap_var.set(format_account_status(info, self.cfg.account_nickname))
        if info.get("logged_in"):
            self.login_var.set("当前状态：已登录")
            if not self.cfg.account_nickname or not info.get("stuid"):
                self._start_profile_refresh()
        else:
            reason = info.get("reason") or "当前状态：未登录"
            if not reason.startswith("当前状态"):
                reason = f"当前状态：{reason}"
            self.login_var.set(reason)
            # 托盘配置里残留昵称/UID、引擎侧却没有凭证时，界面容易误以为已登录
            if (self.cfg.account_nickname or self.cfg.account_stuid) and not info.get("stoken_set"):
                self.snap_var.set(
                    f"检测到本地残留的账号展示信息（{self.cfg.account_stuid or 'UID 未知'}），"
                    f"但签到配置中没有有效登录凭证。\n请在「账号」页重新登录。"
                )

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
        # 板块被隐藏时它的行不存在，_game_vars 里也就没有对应项；
        # 这时要沿用配置里的值，不能抛 KeyError（曾导致保存静默失败）
        cfg.enable_genshin = self._game_var("genshin").get()
        cfg.enable_honkai3rd = self._game_var("honkai3rd").get()
        cfg.enable_honkai2 = self._game_var("honkai2").get()
        cfg.enable_tears = self._game_var("tears").get()
        cfg.enable_honkai_sr = self._game_var("honkai_sr").get()
        cfg.enable_zzz = self._game_var("zzz").get()
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

    def _game_var(self, key: str) -> tk.BooleanVar:
        """取某个游戏开关变量；板块行被隐藏时按配置现值补一个（不抛异常）。"""
        var = self._game_vars.get(key)
        if var is None:
            var = tk.BooleanVar(value=bool(getattr(self.cfg, f"enable_{key}", False)))
            self._game_vars[key] = var
        return var

    def _save(self) -> None:
        try:
            cfg = self._collect()
        except Exception as e:            # 绝不让保存静默失败（旧版 KeyError 被 Tk 吞掉）
            traceback.print_exc()
            self._dialog("设置", f"保存失败：{e}", kind="error")
            return
        if not cfg:
            return
        cfg.save()
        autostart.set_enabled(cfg.autostart)
        self.on_saved(cfg)
        self.status_var.set("设置已保存")
        self._mark_saved()
        # 保存后立刻刷新界面上的派生显示（列表 / 账号 / 上次运行），并给出醒目反馈
        try:
            self._render_board_rows()
            self._refresh_account_status()
            self._refresh_last_run()
        except Exception:
            pass
        self._flash_saved()

    def _flash_saved(self) -> None:
        """保存成功的即时反馈：按钮变色 + 提示文字，2.6 秒后复原。"""
        try:
            self.saved_var.set("✓ 设置已保存")
        except Exception:
            pass
        try:
            btn = getattr(self, "_btn_save", None)
            if btn is not None:
                btn.configure(bg=ACCENT, fg="#FFFFFF")
        except Exception:
            pass
        job = getattr(self, "_saved_job", None)
        if job is not None:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        try:
            self._saved_job = self.root.after(2600, self._clear_saved_hint)
        except Exception:
            self._saved_job = None

    def _clear_saved_hint(self) -> None:
        self._saved_job = None
        try:
            self.saved_var.set("")
        except Exception:
            pass
        try:
            btn = getattr(self, "_btn_save", None)
            if btn is not None:
                btn.configure(bg=BTN_BG, fg=BTN_FG)
        except Exception:
            pass
        self._update_dirty_hint()

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
        self._show_busy("准备签到…")
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
        # 横幅上同步显示当前进度（状态行里的滚动文案）
        try:
            text = (self.status_var.get() or "").strip("….")
            if text:
                self._busy_var.set(f"正在签到{text if text != '请稍候' else ''}…")
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
        self._hide_busy()
        try:                                  # 状态行别停在「请稍候…」上
            self.status_var.set(self.status_text or "就绪")
        except Exception:
            pass



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
        """运行日志窗口：跟随文件实时刷新，自动滚到底（手动上翻时不打扰）。"""
        existing = getattr(self, "_log_win", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.deiconify()
                    existing.lift()
                    existing.focus_force()
                    return
            except Exception:
                pass
            self._log_win = None

        win = tk.Toplevel(self.root)
        win.title("运行日志（实时）")
        win.configure(bg=CARD)
        self._apply_window_icon(win)
        win.geometry(self._centered_geometry(win, _px(680), _px(440)))
        win.update_idletasks()
        try:
            _theme.apply_window_style(win)
        except Exception:
            pass
        self._log_win = win

        body = tk.Frame(win, bg=CARD)
        body.pack(fill="both", expand=True, padx=_px(10), pady=(_px(10), _px(6)))
        txt = tk.Text(body, wrap="none", font=("Consolas", 10), bg=CARD, fg=TEXT,
                      insertbackground=TEXT, relief="flat", highlightthickness=0)
        bar = SlimScrollbar(body, target=txt, bg=CARD, width=8)
        txt.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        txt.configure(state="disabled")

        foot = tk.Frame(win, bg=CARD)
        foot.pack(fill="x", padx=_px(10), pady=(0, _px(10)))
        self._log_follow = tk.BooleanVar(value=True)
        PillSwitch(foot, variable=self._log_follow, bg=CARD).pack(side="left")
        tk.Label(foot, text="自动刷新（跟随最新）", bg=CARD, fg=TEXT,
                 font=_ui_font(9)).pack(side="left", padx=_px(6))
        size_var = tk.StringVar(value="")
        tk.Label(foot, textvariable=size_var, bg=CARD, fg=MUTED,
                 font=_ui_font(9)).pack(side="right", padx=_px(6))
        tk.Button(foot, text="打开文件位置", bg=CARD, fg=TEXT, activebackground=LINE,
                  activeforeground=TEXT, relief="flat", font=_ui_font(9),
                  padx=_px(8), pady=_px(3), cursor="hand2",
                  command=lambda: self._open_log_dir()).pack(side="right")
        tk.Button(foot, text="清屏", bg=CARD, fg=TEXT, activebackground=LINE,
                  activeforeground=TEXT, relief="flat", font=_ui_font(9),
                  padx=_px(8), pady=_px(3), cursor="hand2",
                  command=lambda: self._log_clear()).pack(side="right", padx=(0, _px(6)))
        tk.Button(foot, text="清除日志", bg=CARD, fg=DANGER, activebackground=LINE,
                  activeforeground=DANGER, relief="flat", font=_ui_font(9),
                  padx=_px(8), pady=_px(3), cursor="hand2",
                  command=self._log_purge).pack(side="right", padx=(0, _px(6)))

        state = {"pos": 0, "buffer": "", "job": None, "size": 0}
        self._log_state = state
        self._log_text = txt
        self._log_size_var = size_var

        self._log_reload()
        self._log_tick()
        try:
            # 再补一次：此刻 Text 才有真实布局，确保打开就停在最新一行
            win.after(30, self._log_scroll_end)
        except Exception:
            pass
        # 新窗口要显式置前：它是独立顶层窗，紧跟设置窗创建时可能被设置窗压住
        # （设置窗在 show_now 后 300ms 内还是置顶的），用户会以为「点了没反应」
        try:
            win.lift()
            win.focus_force()
        except Exception:
            pass
        win.protocol("WM_DELETE_WINDOW", self._close_log)

    def _log_append(self, text: str) -> None:
        txt = getattr(self, "_log_text", None)
        if txt is None or not text:
            return
        follow = bool(getattr(self, "_log_follow", tk.BooleanVar(value=True)).get())
        try:
            at_bottom = txt.yview()[1] >= 0.999
        except Exception:
            at_bottom = True
        try:
            txt.configure(state="normal")
            txt.insert("end", text)
            # 只保留最近 3000 行，避免长时间挂着日志窗口无限增长
            lines_n = int(txt.index("end-1c").split(".")[0])
            if lines_n > 3000:
                txt.delete("1.0", f"{lines_n - 3000}.0")
            txt.configure(state="disabled")
            if follow and at_bottom:
                txt.see("end")
        except Exception:
            pass

    def _log_reload(self) -> None:
        """从头读一遍（打开窗口 / 清屏 / 文件被截断时用）。

        默认只显示最近三天：日志文件会一直追加，全量读出来既慢又没人看。
        """
        state = getattr(self, "_log_state", None)
        if state is None:
            return
        txt = getattr(self, "_log_text", None)
        if txt is not None:
            try:
                txt.configure(state="normal")
                txt.delete("1.0", "end")
                txt.configure(state="disabled")
            except Exception:
                pass
        state["pos"] = 0
        state["buffer"] = ""
        try:
            size = LOG_PATH.stat().st_size
        except Exception:
            size = 0
        kept = 0
        if size:
            try:
                text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
                lines = [ln for ln in text.splitlines() if self._log_line_recent(ln)]
                kept = len(lines)
                if lines:
                    self._log_append("\n".join(lines[-3000:]) + "\n")
                state["pos"] = size
            except Exception:
                state["pos"] = 0
        if not kept:
            self._log_append(f"（最近三天（{self._log_cutoff()} 起）暂无日志）\n")
        # 载入完必须显式滚到底：_log_reload 是在 Text 还没布局完时插入的，
        # 那一刻 yview() 还是 (0.0, 1.0)、插入后变成 (0.0, 0.05)，
        # `_log_append` 里的 at_bottom 判定因此为假、see("end") 被跳过 ——
        # 窗口会停在**最旧**的一行，用户以为「今天的日志没记上」。
        self._log_scroll_end()
        try:
            self._log_size_var.set(f"{size // 1024} KB" if size else "0 字节")
        except Exception:
            pass

    def _log_scroll_end(self) -> None:
        """把日志视图滚到最底部（跟随最新）。"""
        txt = getattr(self, "_log_text", None)
        if txt is None:
            return
        try:
            txt.see("end")
            txt.yview_moveto(1.0)
        except Exception:
            pass

    @staticmethod
    def _log_cutoff() -> str:
        """只显示最近三天的日期下限（含今天）。"""
        return (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")

    def _log_line_recent(self, line: str) -> bool:
        """按行首的 [YYYY-MM-DD …] 时间戳判断是否在最近三天内。"""
        text = (line or "").lstrip()
        if text.startswith("[") and len(text) >= 11:
            stamp = text[1:11]
            if len(stamp) == 10 and stamp[4] == "-" and stamp[7] == "-":
                return stamp >= self._log_cutoff()
        return True                     # 没有时间戳的行（引擎原始输出等）保留

    def _log_purge(self) -> None:
        """清空日志文件（与「清屏」不同：会真的把文件内容删掉）。"""
        if not self._dialog("清除日志", "确定清空日志文件？此操作不可恢复。", ask=True):
            return
        try:
            LOG_PATH.write_text("", encoding="utf-8")
        except Exception as e:
            self._dialog("清除日志", f"清除失败：{e}", kind="error")
            return
        state = getattr(self, "_log_state", None)
        if state is not None:
            state["pos"] = 0
            state["buffer"] = ""
        self._log_clear()
        self._log_append("（日志已清除）\n")
        try:
            self._log_size_var.set("0 字节")
        except Exception:
            pass

    def _log_clear(self) -> None:
        """只清空显示，不动日志文件。"""
        txt = getattr(self, "_log_text", None)
        if txt is None:
            return
        try:
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            txt.configure(state="disabled")
        except Exception:
            pass

    def _log_poll(self, first: bool = False) -> None:
        """增量读取新增内容（按行，半行留在缓冲里）。"""
        state = getattr(self, "_log_state", None)
        if state is None:
            return
        try:
            size = LOG_PATH.stat().st_size
        except Exception:
            size = 0
        try:
            if size < state["pos"]:              # 文件被截断/轮转：从头来
                state["pos"] = 0
                state["buffer"] = ""
                self._log_clear()
            if size > state["pos"]:
                with LOG_PATH.open("r", encoding="utf-8", errors="replace") as f:
                    f.seek(state["pos"])
                    chunk = f.read()
                    state["pos"] = f.tell()
                text = state["buffer"] + chunk
                if text.endswith("\n"):
                    state["buffer"] = ""
                    body = text
                else:
                    cut = text.rfind("\n")
                    if cut < 0:
                        state["buffer"] = text
                        body = ""
                    else:
                        state["buffer"] = text[cut + 1:]
                        body = text[:cut + 1]
                if first and not body.strip():
                    self._log_append("（暂无日志）\n")
                else:
                    # 增量内容同样只保留最近三天
                    keep = "".join(ln + "\n" for ln in body.splitlines()
                                   if self._log_line_recent(ln))
                    if keep:
                        self._log_append(keep)
            state["size"] = size
            if hasattr(self, "_log_size_var"):
                self._log_size_var.set(f"{size // 1024} KB" if size else "0 字节")
        except Exception:
            pass

    def _log_tick(self) -> None:
        state = getattr(self, "_log_state", None)
        win = getattr(self, "_log_win", None)
        if state is None or win is None:
            return
        try:
            if not win.winfo_exists():
                self._close_log()
                return
        except Exception:
            self._close_log()
            return
        state["job"] = None
        # 始终轮询：跟随开关只管「要不要自动滚动」，不该连刷新一起停 ——
        # 关掉它会让窗口冻结在旧内容上，看起来就像「日志没记上」。
        self._log_poll()
        try:
            state["job"] = self.root.after(700, self._log_tick)
        except Exception:
            state["job"] = None

    def _close_log(self) -> None:
        state = getattr(self, "_log_state", None)
        if state is not None and state.get("job") is not None:
            try:
                self.root.after_cancel(state["job"])
            except Exception:
                pass
            state["job"] = None
        win = getattr(self, "_log_win", None)
        self._log_win = None
        self._log_text = None
        try:
            if win is not None and win.winfo_exists():
                win.destroy()
        except Exception:
            pass

    def _open_log_dir(self) -> None:
        try:
            import subprocess
            subprocess.Popen(["explorer", "/select,", str(LOG_PATH)])
        except Exception:
            pass

    def _minimize_to_tray(self) -> None:
        self._stop_waiting()
        self.cfg.minimize_to_tray = True
        try:
            self.var_min_tray.set(True)
        except Exception:
            pass
        self.cfg.save()
        self._snapshot_before_hide()     # 先把当前画面留成封面，再隐藏
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
            self._snapshot_before_hide()     # 隐藏前留封面，下次打开才不会拿着旧图
            self.root.withdraw()
            return
        self.quit_requested = True
        if self.on_request_quit:
            self.on_request_quit()
        else:
            self.root.destroy()

    def _is_open_on_screen(self) -> bool:
        """窗口已经开着、就在屏幕上（既不是隐藏到托盘，也不是最小化到任务栏）。"""
        try:
            return self.root.state() == "normal" and bool(self.root.winfo_viewable())
        except Exception:
            return False

    def _note_hidden_change(self, size: tuple[int, int] | None = None) -> None:
        """记下「界面在隐藏/最小化期间变过」。

        隐藏时窗口没渲染，抓不到新快照；只能记住「变过」，打开时退回纯底色封面，
        免得把一张旧布局的模糊图拉伸上去（用户看到的底图就和实际界面对不上）。

        只认**尺寸变化**：尺寸变了说明布局真的不一样了（签到横幅出现/收起、密度换档、
        改过缩放/看板）；只是状态文字变了的话，模糊图里本来就看不清字，不值得为此
        把模糊效果换成纯色。看得见的时候每次重排都会重抓快照，所以只记不在屏幕上时。
        """
        try:
            if self._is_open_on_screen():
                return
            st = self._overlay
            if st is not None and size is not None and st.get("img_size") == tuple(size):
                return                  # 尺寸没变：旧快照仍然对得上
            self._hidden_changed = True
        except Exception:
            pass

    def _raise_window(self) -> None:
        """把窗口叫到最前面。不重画界面，也就不该盖遮罩。"""
        try:
            self.root.lift()
            self.root.focus_force()
            self.root.attributes("-topmost", True)
            self.root.after(300, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass

    def show_now(self) -> None:
        self._show_flag = False
        self._refresh_last_run()
        self._refresh_account_status()
        if self._is_open_on_screen():
            # 已经开着的时候点托盘/再次启动 = 「把它叫到前面」。
            # 这条路径界面不用重画，再盖一次模糊只会让用户看到界面莫名糊一下。
            self._raise_window()
            return
        # 这一段自己会盖遮罩：期间（deiconify 引发的）<Map> 回调别重复盖一遍
        self._in_show_now = True
        try:
            # 先把遮罩贴好再显示窗口：显示出来 → 在遮罩底下把界面画完 → 一次性撤罩。
            # 这样用户看不到「窗口先空着、控件逐个冒出来」的绘制过程。
            covered = self._mask_cover_for_show()
            self.root.deiconify()
            self._restyle_window()          # 显示后补一次边框样式（DWM 常丢）
            # 注意：`_install_hittest_guard()`（边框点不动）当前**不启用** —— 它在显示时
            # 有效（实测边缘全部 HTCAPTION），但缩放一档后回调开始抛异常、边缘变成
            # HTNOWHERE(0)，而回调里的 except 会把它掩盖掉；HTNOWHERE 有可能连窗口拖拽
            # 一起弄坏，风险未查清前不上线。详见该方法 docstring。
            self._raise_window()
            if covered:
                self._mask_paint()          # 显示后立刻把封面画出来
                self.root.update()          # 在遮罩底下把界面画完
                self._force_repaint()
                self._mask_fit()
                self._mask_paint()
                self._mask_schedule_hide()
            # 界面画完之后补一张「当前尺寸的清晰快照」留给下次当封面。
            # 为什么必须有：快照只在上次隐藏/重排时才会拍，首次打开时窗口从没渲染过、
            # 一张都没有 —— 不补的话「最小化到任务栏再点任务栏按钮还原」只能退化成
            # 纯色封面（用户实测「任务栏打开没有模糊」就是这个）。
            # 延后 350ms：遮罩撤掉后窗口还在继续重绘（160 个控件约 250ms），
            # 太早拍会拍到一张几乎空白的图（见 _snapshot_if_sharp 的自检）。
            self.root.after(self._MASK_HOLD_MS + 350, self._snapshot_if_sharp)
        except Exception:
            pass
        finally:
            self._in_show_now = False

    _SNAP_MIN_STDDEV = 6.0      # 快照至少要有这么多明暗起伏，才算「拍到界面了」

    def _warmup_snapshot(self) -> None:
        """启动时把界面「隐形渲染」一次并抓下来，供第一次打开当封面。

        为什么必须预热：快照只在上次隐藏/重排时才会拍，而**静默启动后窗口从没渲染过**
        —— 第一次打开时一张都没有，只能退化成纯色封面（用户实测「静默启动后的第一次
        从任务栏启动没有模糊」）。

        做法：`alpha=0` 映射（用户看不到）→ 让 Tk 把界面画完 → `PrintWindow` 抓窗口
        自身内容（实测 alpha=0 与 alpha=1 抓到的逐像素一致）→ 收回并恢复 alpha。
        期间临时加 `WS_EX_TOOLWINDOW`，避免任务栏闪出一个按钮。
        """
        st = getattr(self, "_overlay", None)
        if st is not None and st.get("img") is not None:
            return                          # 已经有快照了，不用预热
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32")     # 独立实例，别动全局 argtypes
            user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
            user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                                 ctypes.c_ssize_t]
            hwnd = user32.GetAncestor(self.root.winfo_id(), 2) or self.root.winfo_id()
            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED = (
                0x0001, 0x0002, 0x0004, 0x0010, 0x0020)
            ex0 = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex0 | WS_EX_TOOLWINDOW)
            self._in_show_now = True        # 期间别让 <Map> 回调把封面盖上来
            try:
                self.root.attributes("-alpha", 0.0)
                self.root.deiconify()
                self.root.update_idletasks()
                # 让 Tk 真把控件画一遍（画面不可见，但窗口表面已经有了）
                end = time.time() + 0.5
                while time.time() < end:
                    self.root.update()
                    time.sleep(0.02)
                self._snapshot_from_window(hwnd)
            finally:
                self.root.withdraw()
                try:
                    self.root.attributes("-alpha", 1.0)
                except Exception:
                    pass
                user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex0)
                user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                                    SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER
                                    | SWP_NOACTIVATE | SWP_FRAMECHANGED)
                self._in_show_now = False
        except Exception:
            pass

    def _snapshot_from_window(self, hwnd) -> None:
        """用 PrintWindow 抓窗口自身内容（不需要窗口可见），存成封面快照。"""
        try:
            import ctypes
            from ctypes import wintypes

            from PIL import Image, ImageFilter

            # 用**独立的 WinDLL 实例**：argtypes 是挂在函数对象上的全局状态，
            # 而 `ctypes.windll.user32` 是共享缓存的 —— 在这里给 GetWindowRect /
            # GetClientRect 设 argtypes，会让 dpi.py 用自己 _RECT 结构调同一批函数时
            # 抛 ArgumentError（被它的 except 吞掉 → 跨屏判定永远 False → 边界抖动
            # 反复重排）。独立实例互不影响。
            user32 = ctypes.WinDLL("user32")
            gdi32 = ctypes.WinDLL("gdi32")
            user32.GetDC.restype = wintypes.HDC
            user32.GetDC.argtypes = [wintypes.HWND]
            user32.ReleaseDC.restype = ctypes.c_int
            user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
            gdi32.CreateCompatibleDC.restype = wintypes.HDC
            gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
            gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
            gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int,
                                                     ctypes.c_int]
            gdi32.SelectObject.restype = wintypes.HGDIOBJ
            gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
            gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
            gdi32.DeleteDC.argtypes = [wintypes.HDC]
            user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.c_uint]
            user32.PrintWindow.restype = wintypes.BOOL
            user32.GetWindowRect.argtypes = [wintypes.HWND,
                                             ctypes.POINTER(wintypes.RECT)]
            user32.GetClientRect.argtypes = [wintypes.HWND,
                                             ctypes.POINTER(wintypes.RECT)]
            user32.ClientToScreen.argtypes = [wintypes.HWND,
                                              ctypes.POINTER(wintypes.POINT)]

            class BMIH(ctypes.Structure):
                _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                            ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                            ("biBitCount", ctypes.c_uint16),
                            ("biCompression", ctypes.c_uint32),
                            ("biSizeImage", ctypes.c_uint32),
                            ("biXPelsPerMeter", ctypes.c_int32),
                            ("biYPelsPerMeter", ctypes.c_int32),
                            ("biClrUsed", ctypes.c_uint32),
                            ("biClrImportant", ctypes.c_uint32)]

            class BMI(ctypes.Structure):
                _fields_ = [("bmiHeader", BMIH), ("bmiColors", ctypes.c_uint32 * 3)]

            gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, ctypes.c_uint,
                                        ctypes.c_uint, ctypes.c_void_p,
                                        ctypes.POINTER(BMI), ctypes.c_uint]
            gdi32.GetDIBits.restype = ctypes.c_int

            wr = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(wr))
            ow, oh = wr.right - wr.left, wr.bottom - wr.top
            if ow < 50 or oh < 50:
                return
            hdc = user32.GetDC(None)
            mem = gdi32.CreateCompatibleDC(hdc)
            bmp = gdi32.CreateCompatibleBitmap(hdc, ow, oh)
            old = gdi32.SelectObject(mem, bmp)
            try:
                if not user32.PrintWindow(hwnd, mem, 0x00000002):   # RENDERFULLCONTENT
                    return
                info = BMI()
                info.bmiHeader.biSize = ctypes.sizeof(BMIH)
                info.bmiHeader.biWidth = ow
                info.bmiHeader.biHeight = -oh
                info.bmiHeader.biPlanes = 1
                info.bmiHeader.biBitCount = 32
                buf = ctypes.create_string_buffer(ow * oh * 4)
                gdi32.GetDIBits(mem, bmp, 0, oh, buf, ctypes.byref(info), 0)
                full = Image.frombuffer("RGB", (ow, oh), buf, "raw", "BGRX", 0, 1)
                # 只要客户区：PrintWindow 抓的是整窗（含标题栏）
                pt = wintypes.POINT(0, 0)
                user32.ClientToScreen(hwnd, ctypes.byref(pt))
                ox, oy = pt.x - wr.left, pt.y - wr.top
                cr = wintypes.RECT()
                user32.GetClientRect(hwnd, ctypes.byref(cr))
                cw, ch = cr.right - cr.left, cr.bottom - cr.top
                if cw < 50 or ch < 50:
                    return
                img = full.crop((ox, oy, ox + cw, oy + ch)).copy()
            finally:
                gdi32.SelectObject(mem, old)
                gdi32.DeleteObject(bmp)
                gdi32.DeleteDC(mem)
                user32.ReleaseDC(None, hdc)

            st = self._mask_ensure_overlay()
            small = img.resize((max(1, cw // 3), max(1, ch // 3)))
            st["img"] = small.filter(ImageFilter.GaussianBlur(3))
            st["img_size"] = (cw, ch)
            st["solid"] = False
            st["placed_size"] = None
            self._hidden_changed = False
        except Exception:
            pass

    def _snapshot_if_sharp(self, tries: int = 0) -> None:
        """窗口清晰可见时补一张封面快照（给下次打开/还原当底图）。

        拍到的图必须「有内容」才留：实测遮罩撤掉后窗口仍在继续重绘，这时候抓到的
        是一张几乎空白的图 —— 留着当封面就是一块纯色，用户看到的就是「没有模糊」。
        太空就作废、稍后重拍（最多几次）。
        """
        try:
            if not self._is_open_on_screen():
                return
            from PIL import ImageStat

            self._mask_ensure_overlay()
            self._mask_snapshot()
            st = self._overlay
            img = st.get("img") if st else None
            _sd = ImageStat.Stat(img.convert("L")).stddev[0] if img is not None else -1.0
            if img is not None and _sd >= self._SNAP_MIN_STDDEV:
                return                              # 这张够清楚，留着用
            if st is not None:
                st["img"] = None                    # 空白图不如没有
                st["img_size"] = None
            if tries < 3:
                self.root.after(250, lambda t=tries + 1: self._snapshot_if_sharp(t))
        except Exception:
            pass

    def _snapshot_before_hide(self) -> None:
        """关窗前抓一张模糊快照留着：下次打开时用它当封面，把绘制过程盖住。

        必须**每次**隐藏前都抓，而不是只在重排时抓：用户可能切过页签、改过看板
        顺序/隐藏项、翻过列表，这些都不触发重排 —— 不抓就会拿着上一次重排时的
        旧图当封面，看起来就是「模糊底图和实际界面对不上」。
        """
        try:
            # 最小化/隐藏状态下窗口矩形里是别的应用，抓下来会闪出一块不相干的图
            if self._window_visible() and self.root.state() == "normal":
                self._mask_ensure_overlay()
                self._mask_snapshot()
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
        self._sync_runtime()
        self._check_dirty()
        self._watch_dpi()

    def destroy(self) -> None:
        self._stop_waiting()
        try:
            self._mask_hide()      # 遮罩挂在窗口里，别留下残影
        except Exception:
            pass
        try:
            for win in list(getattr(self, "_float_wins", [])):
                try:
                    win.destroy()
                except Exception:
                    pass
            self._float_wins = []
        except Exception:
            pass
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



class _ReflowMask:
    """重排期间用模糊快照盖住窗口，结束后一次性撤掉。

    Tk 重建界面必然要销毁并重画全部控件，肉眼会看到明显的闪一下；
    盖一层当前界面的模糊图，用户看到的只是「缩放」本身。
    """

    def __init__(self, win: "SettingsWindow") -> None:
        self._win = win

    def __enter__(self) -> "_ReflowMask":
        try:
            self._win._mask_show()
        except Exception:
            pass
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        win = self._win
        try:
            win.root.update_idletasks()
        except Exception:
            pass
        try:
            win._mask_fit()
        except Exception:
            pass
        # 关键：遮罩把窗口挡得严严实实时，Windows 不会给被遮挡的窗口重画；
        # 等遮罩撤掉才重画 160+ 个控件，中间那段就是「背景一片白 + 元素逐个冒出来」。
        # 用 InvalidateRect + UpdateWindow 强制在遮罩底下同步重画一遍。
        for _ in range(2):
            try:
                win.root.update()
            except Exception:
                pass
            try:
                win._force_repaint()
            except Exception:
                pass
        try:
            win._mask_fit()
        except Exception:
            pass
        # 画完之后再保持一小段，然后*一次性撤掉*：
        # 慢慢淡出会把「模糊图」和「刚画好的界面」叠在一起，看起来就是元素发白发虚。
        try:
            st = win._overlay
            if st is not None:
                win._mask_cancel_hide()          # 本次重排重新计时
                token = id(st["lbl"])
                st["token"] = token
                st["hide_job"] = win.root.after(
                    win._MASK_HOLD_MS, lambda t=token: win._mask_hide_if(t))
        except Exception:
            win._mask_hide()
        return False