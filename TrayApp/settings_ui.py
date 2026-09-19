from __future__ import annotations

import queue
import threading
import tkinter as tk
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
from runner import get_cloud_tokens, read_log_tail, run_checkin
from scheduler import parse_hhmm
from stoken_login import (
    StokenResult,
    exchange_cookie,
    login_by_sms,
    login_verify,
    send_sms,
    write_bbs_config,
)

ACCENT = "#2563EB"
MUTED = "#6B7280"
PANEL = "#F3F4F6"
LINE = "#E5E7EB"
ABOUT_TEXT = f"""米游社自动签到器
MihoyoBBSAutoSigner {APP_VERSION}

米游社 / 米哈游游戏辅助签到托盘
功能：社区打卡 · 游戏签到 · 定时任务 · 开机自启 · 短信登录 Stoken

签到引擎：Womsxd/MihoyoBBSTools
协议参考：公开米游社/通行证接口整理

设备 device_id / device_fp 自动生成，详见日志文件。
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
        on_run_now: Callable[[], None],
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
        if master is not None:
            self.root = tk.Toplevel(master)
        else:
            self.root = tk.Tk()
        self.root.title("米游社自动签到器 · 设置")
        self._apply_window_icon(self.root)
        self.root.geometry("660x780")
        self.root.minsize(560, 620)
        self.root.configure(bg=PANEL)
        self.root.attributes("-topmost", True)
        self.root.withdraw()
        self._board_vars: dict[int, tk.BooleanVar] = {}
        self.var_hour = tk.StringVar(value="09")
        self.var_minute = tk.StringVar(value="30")
        self._build()
        self._fit_default_geometry()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _fit_default_geometry(self) -> None:
        """默认窗口以能完整显示功能页内容为准（超出屏幕时才需要滚动）。"""
        try:
            self.root.update_idletasks()
            inner = getattr(self, "_feat_inner", None)
            if inner is None:
                return
            content_h = inner.winfo_reqheight() + 180
            screen_h = self.root.winfo_screenheight()
            h = min(max(760, content_h), screen_h - 120)
            w = max(680, inner.winfo_reqwidth() + 60)
            self.root.geometry(f"{w}x{h}")
            self.root.resizable(False, False)
        except Exception:
            pass

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

    def _build(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except Exception:
            pass
        style.configure("TCheckbutton", background=PANEL, font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10))
        style.configure("TLabel", background=PANEL, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=PANEL, font=("Segoe UI", 14, "bold"))
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Section.TLabel", background=PANEL, font=("Segoe UI", 10, "bold"))

        header = tk.Frame(self.root, bg=PANEL)
        header.pack(fill="x", padx=16, pady=(10, 4))
        ttk.Label(header, text="米游社自动签到器", style="Title.TLabel").pack(anchor="w")
        self.status_var = tk.StringVar(value=self.status_text or "就绪")
        ttk.Label(header, textvariable=self.status_var, style="Muted.TLabel").pack(anchor="w")

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=12, pady=8)
        self.tab_feat = tk.Frame(nb, bg=PANEL)
        self.tab_acct = tk.Frame(nb, bg=PANEL)
        self.tab_about = tk.Frame(nb, bg=PANEL)
        nb.add(self.tab_feat, text="功能 / 定时")
        nb.add(self.tab_acct, text="账号 / Stoken")
        nb.add(self.tab_about, text="关于")

        self._build_features(self.tab_feat)
        self._build_account(self.tab_acct)
        self._build_about(self.tab_about)

        btns = tk.Frame(self.root, bg=PANEL)
        btns.pack(fill="x", padx=16, pady=(0, 12))
        tk.Button(
            btns, text="立即签到", bg=ACCENT, fg="white", relief="flat",
            font=("Segoe UI", 10, "bold"), padx=14, pady=6, command=self._run_now,
        ).pack(side="left")
        tk.Button(
            btns, text="保存设置", bg="#111827", fg="white", relief="flat",
            font=("Segoe UI", 10, "bold"), padx=14, pady=6, command=self._save,
        ).pack(side="left", padx=8)
        tk.Button(
            btns, text="查看日志", relief="flat", font=("Segoe UI", 10),
            padx=10, pady=6, command=self._show_log,
        ).pack(side="left")
        tk.Button(
            btns, text="关闭", relief="flat", font=("Segoe UI", 10),
            padx=10, pady=6, command=self._on_close,
        ).pack(side="right")

    def _make_scrollable(self, parent: tk.Frame) -> tk.Frame:
        canvas = tk.Canvas(parent, bg=PANEL, highlightthickness=0)
        vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        inner = tk.Frame(canvas, bg=PANEL)
        canvas.create_window((0, 0), window=inner, anchor="nw", tags="inner")

        def on_inner_configure(_e: tk.Event) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure("inner", width=canvas.winfo_width())

        inner.bind("<Configure>", on_inner_configure)

        def on_wheel(event: tk.Event) -> None:
            delta = getattr(event, "delta", 0)
            if delta:
                canvas.yview_scroll(-1 * (1 if delta > 0 else -1), "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", on_wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        canvas.bind("<Button-4>", lambda _e: canvas.yview_scroll(-1, "units"))
        canvas.bind("<Button-5>", lambda _e: canvas.yview_scroll(1, "units"))
        if parent is self.tab_feat:
            self._feat_inner = inner
        return inner

    def _build_features(self, parent: tk.Frame) -> None:
        wrap = self._make_scrollable(parent)
        wrap.pack(fill="both", expand=True, padx=8, pady=6)

        ttk.Label(wrap, text="游戏签到 · 社区打卡", style="Section.TLabel").pack(anchor="w", pady=(4, 4))
        box = tk.Frame(wrap, bg="#FFFFFF", highlightbackground=LINE, highlightthickness=1)
        box.pack(fill="x")

        header_font = ("Segoe UI", 9, "bold")
        tk.Label(box, text="游戏 / 板块", bg="#FFFFFF", fg=MUTED, font=header_font).grid(
            row=0, column=0, sticky="w", padx=(12, 4), pady=(8, 2)
        )
        tk.Label(box, text="游戏签到", bg="#FFFFFF", fg=MUTED, font=header_font).grid(
            row=0, column=1, padx=14, pady=(8, 2)
        )
        tk.Label(box, text="社区打卡", bg="#FFFFFF", fg=MUTED, font=header_font).grid(
            row=0, column=2, padx=14, pady=(8, 2)
        )

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
        self._board_cbs: dict[int, ttk.Checkbutton] = {}
        selected = set(self.cfg.checkin_list)
        for i, (gid, name, game_key) in enumerate(BOARD_ROWS, start=1):
            tk.Label(box, text=name, bg="#FFFFFF", font=("Segoe UI", 10)).grid(
                row=i, column=0, sticky="w", padx=(12, 4), pady=2
            )
            if game_key:
                var = tk.BooleanVar(value=bool(game_flags[game_key]))
                self._game_vars[game_key] = var
                ttk.Checkbutton(box, variable=var).grid(row=i, column=1, padx=14, pady=2)
            else:
                tk.Label(box, text="—", bg="#FFFFFF", fg=MUTED, font=("Segoe UI", 10)).grid(
                    row=i, column=1, padx=14, pady=2
                )
            bvar = tk.BooleanVar(value=gid in selected)
            self._board_vars[gid] = bvar
            cb = ttk.Checkbutton(box, variable=bvar)
            cb.grid(row=i, column=2, padx=14, pady=2)
            self._board_cbs[gid] = cb
        tk.Frame(box, bg="#FFFFFF", height=8).grid(row=len(BOARD_ROWS) + 1, columnspan=3)

        ttk.Label(wrap, text="云游戏签到", style="Section.TLabel").pack(anchor="w", pady=(12, 2))
        cloud = tk.Frame(wrap, bg=PANEL)
        cloud.pack(fill="x")
        ttk.Label(cloud, text="云原神").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Label(cloud, text="云星穹铁道").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Label(cloud, text="云绝区零").grid(row=2, column=0, sticky="w", pady=2)
        self.var_cloud_genshin = tk.BooleanVar(value=self.cfg.cloud_genshin)
        self.var_cloud_sr = tk.BooleanVar(value=self.cfg.cloud_sr)
        self.var_cloud_zzz = tk.BooleanVar(value=self.cfg.cloud_zzz)
        ttk.Checkbutton(cloud, variable=self.var_cloud_genshin).grid(row=0, column=1, sticky="w")
        ttk.Checkbutton(cloud, variable=self.var_cloud_sr).grid(row=1, column=1, sticky="w")
        ttk.Checkbutton(cloud, variable=self.var_cloud_zzz).grid(row=2, column=1, sticky="w")
        self.ent_cloud_genshin = tk.Entry(cloud, font=("Segoe UI", 9), width=44)
        self.ent_cloud_sr = tk.Entry(cloud, font=("Segoe UI", 9), width=44)
        self.ent_cloud_zzz = tk.Entry(cloud, font=("Segoe UI", 9), width=44)
        self.ent_cloud_genshin.grid(row=0, column=2, sticky="we", padx=(6, 0))
        self.ent_cloud_sr.grid(row=1, column=2, sticky="we", padx=(6, 0))
        self.ent_cloud_zzz.grid(row=2, column=2, sticky="we", padx=(6, 0))
        existing_tokens = get_cloud_tokens()
        self.ent_cloud_genshin.insert(0, existing_tokens.get("genshin", ""))
        self.ent_cloud_sr.insert(0, existing_tokens.get("sr", ""))
        self.ent_cloud_zzz.insert(0, existing_tokens.get("zzz", ""))

        ttk.Label(wrap, text="自动定时签到", style="Section.TLabel").pack(anchor="w", pady=(12, 2))
        self.var_sched = tk.BooleanVar(value=self.cfg.schedule_enabled)
        ttk.Checkbutton(wrap, text="启用定时任务", variable=self.var_sched).pack(anchor="w")

        time_row = tk.Frame(wrap, bg=PANEL)
        time_row.pack(fill="x", pady=6)
        ttk.Label(time_row, text="每天签到时间（滚轮调整）").pack(side="left")
        first = (self.cfg.schedule_times or ["09:30"])[0]
        parsed = parse_hhmm(first) or (9, 30)
        self.var_hour.set(f"{parsed[0]:02d}")
        self.var_minute.set(f"{parsed[1]:02d}")
        sp_h = tk.Spinbox(
            time_row,
            from_=0,
            to=23,
            width=3,
            justify="center",
            font=("Segoe UI", 14),
            textvariable=self.var_hour,
            state="readonly",
        )
        sp_h.pack(side="left", padx=(10, 2))
        ttk.Label(time_row, text=":", font=("Segoe UI", 14)).pack(side="left")
        sp_m = tk.Spinbox(
            time_row,
            from_=0,
            to=55,
            increment=5,
            width=3,
            justify="center",
            font=("Segoe UI", 14),
            textvariable=self.var_minute,
            state="readonly",
        )
        sp_m.pack(side="left", padx=(2, 10))
        _bind_wheel(sp_h, self.var_hour.get, self.var_hour.set, 0, 23)
        _bind_wheel(sp_m, self.var_minute.get, self.var_minute.set, 0, 55, "{:02d}")

        extra = tk.Frame(wrap, bg=PANEL)
        extra.pack(fill="x", pady=2)
        ttk.Label(extra, text="随机延迟(秒)").pack(side="left")
        self.ent_delay = tk.Entry(extra, font=("Segoe UI", 10), width=6)
        self.ent_delay.pack(side="left", padx=6)
        self.ent_delay.insert(0, str(self.cfg.random_delay_sec))

        ttk.Label(wrap, text="启动 / 关闭", style="Section.TLabel").pack(anchor="w", pady=(12, 2))
        self.var_autostart = tk.BooleanVar(value=autostart.is_enabled())
        self.var_launch_run = tk.BooleanVar(value=self.cfg.run_on_launch)
        self.var_silent = tk.BooleanVar(value=bool(self.cfg.silent_launch))
        self.var_min_tray = tk.BooleanVar(value=bool(self.cfg.minimize_to_tray))
        ttk.Checkbutton(wrap, text="开机自启动（当前用户）", variable=self.var_autostart).pack(anchor="w")
        ttk.Checkbutton(wrap, text="启动本程序后立即签到一次", variable=self.var_launch_run).pack(anchor="w")
        ttk.Checkbutton(wrap, text="静默启动", variable=self.var_silent).pack(anchor="w")
        ttk.Checkbutton(
            wrap,
            text="关闭窗口时最小化到托盘（不勾选则直接退出程序）",
            variable=self.var_min_tray,
        ).pack(anchor="w")

        btns = tk.Frame(wrap, bg=PANEL)
        btns.pack(fill="x", pady=(8, 0))
        tk.Button(
            btns, text="最小化到托盘", relief="flat", font=("Segoe UI", 10),
            padx=8, pady=4, command=self._minimize_to_tray,
        ).pack(side="left")
        tk.Button(
            btns, text="退出程序", relief="flat", font=("Segoe UI", 10),
            fg="#DC2626", padx=8, pady=4, command=self._quit_app,
        ).pack(side="left", padx=8)

        ttk.Label(
            wrap,
            text=f"签到配置：{BBS_ROOT / 'config' / 'config.yaml'}",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(10, 0))
        self.lastrun_var = tk.StringVar(value=self._last_run_text())
        ttk.Label(wrap, textvariable=self.lastrun_var, style="Muted.TLabel").pack(anchor="w")

    def _last_run_text(self) -> str:
        last = self.cfg.last_run or "从未"
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

    def _build_account(self, parent: tk.Frame) -> None:
        wrap = tk.Frame(parent, bg=PANEL)
        wrap.pack(fill="both", expand=True, padx=8, pady=6)
        ttk.Label(
            wrap,
            text="短信验证码登录获取 Stoken；若触发图形验证会弹出窗口，请手动完成",
            style="Muted.TLabel",
            wraplength=420,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))

        form = tk.Frame(wrap, bg=PANEL)
        form.pack(fill="x")
        ttk.Label(form, text="手机号").grid(row=0, column=0, sticky="w", pady=3)
        self.ent_account = tk.Entry(form, font=("Segoe UI", 10), width=28)
        self.ent_account.grid(row=0, column=1, sticky="w", padx=8, pady=3)
        ttk.Label(form, text="短信验证码").grid(row=1, column=0, sticky="w", pady=3)
        self.ent_sms = tk.Entry(form, font=("Segoe UI", 10), width=16)
        self.ent_sms.grid(row=1, column=1, sticky="w", padx=8, pady=3)

        btns = tk.Frame(wrap, bg=PANEL)
        btns.pack(fill="x", pady=10)
        self._btn_sms = tk.Button(
            btns, text="发送短信验证码", relief="flat", font=("Segoe UI", 10),
            padx=8, pady=4, command=self._send_sms,
        )
        self._btn_sms.pack(side="left")
        tk.Button(
            btns, text="短信登录获取 Stoken", bg=ACCENT, fg="white", relief="flat",
            font=("Segoe UI", 10, "bold"), padx=8, pady=4, command=self._login_sms,
        ).pack(side="left", padx=8)
        tk.Button(
            btns, text="退出登录并清除数据", relief="flat", font=("Segoe UI", 10),
            fg="#DC2626", padx=8, pady=4, command=self._logout,
        ).pack(side="left")

        self.login_var = tk.StringVar(value="")
        self.snap_var = tk.StringVar(value="")
        ttk.Label(
            wrap, textvariable=self.login_var, style="Muted.TLabel",
            wraplength=420, justify="left",
        ).pack(anchor="w", pady=(4, 8))

        box = tk.Frame(wrap, bg="#FFFFFF", highlightbackground=LINE, highlightthickness=1)
        box.pack(fill="x", pady=4)
        tk.Label(box, text="当前签到账号", bg="#FFFFFF", font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=10, pady=(8, 0)
        )
        self.snap_label = tk.Label(
            box, textvariable=self.snap_var, bg="#FFFFFF",
            font=("Segoe UI", 10), fg=MUTED, justify="left",
        )
        self.snap_label.pack(anchor="w", padx=10, pady=(2, 10))
        self._refresh_account_status()

        try:
            ensure_device(self.cfg)
        except Exception as e:
            self.login_var.set(f"设备标识初始化失败：{e}")

        ttk.Label(
            wrap,
            text="device_id / device_fp 自动生成，仅记录在日志中，不在此展示。",
            style="Muted.TLabel",
            wraplength=420,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

    def _refresh_account_status(self) -> None:
        info = load_account_info()
        if info.get("error"):
            self.snap_var.set(info["error"])
            self.login_var.set(info["error"])
            return
        self.snap_var.set(format_account_status(info))
        if info.get("logged_in"):
            self.login_var.set("当前状态：已登录")
        else:
            self.login_var.set("当前状态：未登录")

    def _build_about(self, parent: tk.Frame) -> None:
        wrap = tk.Frame(parent, bg=PANEL)
        wrap.pack(fill="both", expand=True, padx=8, pady=6)
        box = tk.Frame(wrap, bg="#FFFFFF", highlightbackground=LINE, highlightthickness=1)
        box.pack(fill="both", expand=True)
        txt = tk.Text(
            box, bg="#FFFFFF", fg="#1F2937", font=("Segoe UI", 10),
            relief="flat", padx=12, pady=12, wrap="word",
        )
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", ABOUT_TEXT)
        txt.configure(state="disabled")

        btns = tk.Frame(wrap, bg=PANEL)
        btns.pack(fill="x", pady=8)
        tk.Button(
            btns, text="打开配置目录", relief="flat", font=("Segoe UI", 10),
            padx=8, pady=4,
            command=lambda: __import__("os").startfile(str(BBS_ROOT / "config")),
        ).pack(side="left")
        tk.Button(
            btns, text="打开日志", relief="flat", font=("Segoe UI", 10),
            padx=8, pady=4,
            command=lambda: __import__("os").startfile(str(__import__("app_config").LOG_PATH))
            if __import__("app_config").LOG_PATH.exists()
            else messagebox.showinfo("关于", "日志尚未生成"),
        ).pack(side="left", padx=8)

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
            messagebox.showinfo("发送验证码", f"请等待 {self._sms_left} 秒后再发送")
            return
        mobile = self.ent_account.get().strip()
        if not mobile:
            messagebox.showerror("Stoken", "请先填写手机号")
            return
        did, fp = self._device()
        self._login_busy = True
        self._set_sms_button(False, "发送中…")
        self.login_var.set("正在发送验证码…（若需图形验证将弹出窗口）")

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
            messagebox.showerror("Stoken", "请填写手机号和短信验证码")
            return
        did, fp = self._device()
        self._login_busy = True
        self.login_var.set("正在短信登录…（若需图形验证将弹出窗口）")

        def work() -> None:
            res = login_by_sms(mobile, code, did, fp, open_window=True)
            if res.ok and res.stoken:
                res = login_verify(res.stoken, res.mid, did, fp) if res.mid else res
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
                try:
                    from stoken_login import acquire_cloud_genshin_token, write_cloud_token
                    import yaml
                    from app_config import BBS_ROOT
                    okc, msgc = acquire_cloud_genshin_token(res.stoken, res.mid, res.stuid)
                    if okc:
                        cfgp = BBS_ROOT / "config" / "config.yaml"
                        data = yaml.safe_load(cfgp.read_text(encoding="utf-8")) or {}
                        write_cloud_token(data, msgc)
                        cfgp.write_text(
                            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                            encoding="utf-8")
                        res.message += "；云游戏 token 已自动获取"
                    else:
                        res.message += f"；云游戏 token 获取失败：{msgc}"
                except Exception as e:
                    res.message += f"；云游戏 token 获取异常：{e}"
            self._post_ui(lambda: self._login_done(res))

        threading.Thread(target=work, daemon=True).start()

    def _sms_done(self, res: StokenResult) -> None:
        self._login_busy = False
        self.login_var.set(res.message)
        self._refresh_account_status()
        if res.ok:
            self._start_sms_cooldown(res.sms_countdown or 60)
            messagebox.showinfo("Stoken", res.message)
        else:
            self._clear_sms_cooldown()
            if "图形验证" in (res.message or ""):
                self.login_var.set(res.message + "（未进入倒计时，可立即重试）")
            messagebox.showwarning("Stoken", res.message)

    def _login_done(self, res: StokenResult) -> None:
        self._login_busy = False
        self.login_var.set(res.message)
        self._refresh_account_status()
        if res.ok:
            messagebox.showinfo("Stoken", res.message + "\n已写入签到配置")
        else:
            messagebox.showwarning("Stoken", res.message)

    def _collect_times(self) -> list[str] | None:
        return [f"{self.var_hour.get().zfill(2)}:{self.var_minute.get().zfill(2)}"]

    def _logout(self) -> None:
        if not messagebox.askyesno("退出登录", "确定退出登录并清除 Cookie / Stoken 数据？"):
            return
        ok, msg = logout_and_clear(self.cfg)
        self._refresh_account_status()
        if ok:
            self.login_var.set("当前状态：未登录")
            messagebox.showinfo("退出登录", msg)
        else:
            messagebox.showerror("退出登录", msg)

    def _collect(self) -> TrayConfig | None:
        boards = [gid for gid, var in self._board_vars.items() if var.get()]
        any_board = bool(boards)
        any_game = any(var.get() for var in self._game_vars.values())
        if not any_board and not any_game:
            messagebox.showerror("设置", "请至少勾选一项：游戏签到或社区打卡")
            return None
        times = self._collect_times()
        if times is None:
            return None
        if self.var_sched.get() and not times:
            messagebox.showerror("设置", "启用定时至少要有一个时间点")
            return None
        try:
            delay = max(0, int(self.ent_delay.get().strip() or "0"))
        except ValueError:
            messagebox.showerror("设置", "随机延迟必须是整数秒")
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
        cfg.cloud_genshin_token = self.ent_cloud_genshin.get().strip()
        cfg.cloud_zzz_token = self.ent_cloud_zzz.get().strip()
        cfg.cloud_sr_token = self.ent_cloud_sr.get().strip()
        cfg.schedule_enabled = self.var_sched.get()
        cfg.schedule_times = times
        cfg.random_delay_sec = delay
        cfg.autostart = self.var_autostart.get()
        cfg.run_on_launch = self.var_launch_run.get()
        cfg.silent_launch = self.var_silent.get()
        cfg.minimize_to_tray = self.var_min_tray.get()
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
        messagebox.showinfo("设置", "已保存")

    def _run_now(self) -> None:
        if self._checkin_running:
            return
        cfg = self._collect()
        if not cfg:
            return
        cfg.save()
        self._start_waiting()

        def work() -> None:
            try:
                self.on_run_now()
            except Exception as e:
                msg = f"签到异常：{e}"
            else:
                fresh = TrayConfig.load()
                self.cfg = fresh
                msg = fresh.last_status or "就绪"
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

    def _show_log(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("运行日志")
        self._apply_window_icon(win)
        win.geometry("640x420")
        txt = tk.Text(win, wrap="none", font=("Consolas", 10))
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
