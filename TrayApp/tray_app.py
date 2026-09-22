from __future__ import annotations

import threading
import tkinter as tk
from typing import Optional

import pystray
from PIL import Image

import autostart
from app_config import APP_NAME, APP_TITLE, HOME, TrayConfig, engine_config_path
from device_identity import ensure_device
from make_icon import ensure_icon
from runner import run_checkin
from scheduler import Scheduler, next_run_time
from settings_ui import SettingsWindow

SHOW_FLAG = HOME / "show_request.flag"


class TrayApp:
    def __init__(self) -> None:
        self.cfg = TrayConfig.load()
        try:
            ensure_device(self.cfg)
        except Exception:
            pass
        try:
            engine_config_path(create=True)
        except Exception:
            pass
        self._busy = False
        self._status = "就绪"
        self._want_settings = False
        self._want_quit = False
        self._image = Image.open(ensure_icon())

        # UI lives on the main thread
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_TITLE)

        self.settings = SettingsWindow(
            cfg=self.cfg,
            on_saved=self._on_saved,
            on_run_now=self._run_blocking_for_ui,
            on_request_quit=self._menu_quit,
            status_text=self._status,
            master=self.root,
            is_running=lambda: self._busy,
        )

        self.icon = pystray.Icon(
            name=APP_NAME,
            icon=self._image,
            title=self._title(),
            menu=pystray.Menu(
                pystray.MenuItem("打开设置", self._menu_settings, default=True),
                pystray.MenuItem("立即签到", self._menu_run),
                pystray.MenuItem(
                    "开机自启",
                    self._menu_autostart,
                    checked=lambda item: autostart.is_enabled(),
                ),
                pystray.MenuItem("退出", self._menu_quit),
            ),
        )
        self.scheduler = Scheduler(
            on_fire=self._run_async,
            is_busy=lambda: self._busy,
            get_cfg=lambda: self.cfg,
            on_status=self._set_status,
        )
        self._tray_thread: Optional[threading.Thread] = None

    def _title(self) -> str:
        nxt = ""
        if self.cfg.schedule_enabled:
            n = next_run_time(self.cfg)
            if n:
                nxt = f" · 下次 {n.strftime('%H:%M')}"
        return f"米游社签到{self._status and ' · ' + self._status}{nxt}"

    def _set_status(self, text: str) -> None:
        self._status = text
        try:
            self.icon.title = self._title()
        except Exception:
            pass
        try:
            self.settings.set_status(text)      # 设置窗状态行 / 签到横幅同步
        except Exception:
            pass

    def _set_running(self, running: bool) -> None:
        """把「正在签到」同步到设置窗横幅（后台线程可调用）。"""
        try:
            self.settings.set_running(running)
        except Exception:
            pass

    def _notify(self, ok: bool, msg: str) -> None:
        try:
            self.icon.notify(msg, APP_TITLE)
        except Exception:
            pass

    def _run_async(self) -> None:
        if self._busy:
            self._set_status("签到进行中…")
            return
        self._busy = True  # 先占位再开线程：避免定时器/菜单连点触发并发重复签到
        self._set_running(True)

        def worker() -> None:
            self._set_status("签到中…")
            try:
                ok, msg = run_checkin(self.cfg)
                self.cfg = TrayConfig.load()
                self._set_status(msg if ok else f"失败：{msg}")
                self._notify(ok, msg if ok else f"失败：{msg}")
            except Exception as e:
                self._set_status(f"异常：{e}")
                self._notify(False, f"异常：{e}")
            finally:
                self._busy = False
                self._set_running(False)

        threading.Thread(target=worker, daemon=True, name="mihoyo-checkin").start()

    def _request_settings(self) -> None:
        self._want_settings = True

    def _menu_settings(self, icon=None, item=None) -> None:
        self._request_settings()

    def _menu_run(self, icon=None, item=None) -> None:
        self._run_async()

    def _run_blocking_for_ui(self) -> bool:
        """Settings 窗口在后台线程调用；返回是否真正执行了签到（未执行=False）。"""
        if self._busy:
            self._set_status("签到进行中…")
            return False
        self._busy = True
        self._set_running(True)
        self._set_status("签到中…")
        try:
            ok, msg = run_checkin(self.cfg)
            self.cfg = TrayConfig.load()
            self._set_status(msg if ok else f"失败：{msg}")
        except Exception as e:
            self._set_status(f"异常：{e}")
        finally:
            self._busy = False
            self._set_running(False)
        return True

    def _on_saved(self, cfg: TrayConfig) -> None:
        self.cfg = cfg
        # 设置窗自己那一份 cfg 可能带着旧的运行状态；调度器直接用 self.cfg 判断
        # 「今天是否已签」，所以这里以磁盘为准刷新一次，避免重启就补签
        try:
            fresh = TrayConfig.load()
            cfg.last_run = fresh.last_run
            cfg.last_ok_run = fresh.last_ok_run
            cfg.last_status = fresh.last_status
        except Exception:
            pass
        self._set_status("设置已保存")

    def _menu_autostart(self, icon=None, item=None) -> None:
        desired = not autostart.is_enabled()
        ok = autostart.set_enabled(desired)
        if ok:
            self.cfg.autostart = desired
            self.cfg.save()
            self._set_status("已开启自启" if desired else "已关闭自启")
        else:
            self._set_status("自启设置失败（注册表写入被拒绝）")

    def _menu_quit(self, icon=None, item=None) -> None:
        self._want_quit = True
        try:
            self.scheduler.stop()
        except Exception:
            pass
        try:
            self.icon.stop()
        except Exception:
            pass

    def _consume_show_request(self) -> bool:
        try:
            if SHOW_FLAG.exists():
                SHOW_FLAG.unlink()
                # 用户在别处再次启动程序 = 明确想看界面，静默设置不应吞掉该请求
                return True
        except Exception:
            pass
        return False

    def _pump(self) -> None:
        if self._want_quit:
            try:
                self.settings.destroy()
            except Exception:
                pass
            try:
                self.root.quit()
                self.root.destroy()
            except Exception:
                pass
            return
        if self._consume_show_request():
            self._want_settings = True
        if self._want_settings:
            self._want_settings = False
            try:
                self.settings.show_now()
            except Exception as e:
                try:
                    self.settings = SettingsWindow(
                        cfg=self.cfg,
                        on_saved=self._on_saved,
                        on_run_now=self._run_blocking_for_ui,
                        on_request_quit=self._menu_quit,
                        status_text=self._status,
                        master=self.root,
                        is_running=lambda: self._busy,
                    )
                    self.settings.show_now()
                except Exception as e2:
                    self._set_status(f"无法打开设置：{e2}")
        try:
            self.settings.pump()
        except Exception:
            pass
        try:
            self.root.after(150, self._pump)
        except Exception:
            pass

    def run(self) -> None:
        self.scheduler.start()
        self._set_status("托盘已就绪")
        self._tray_thread = threading.Thread(
            target=self.icon.run, daemon=True, name="mihoyo-tray"
        )
        self._tray_thread.start()
        if self.cfg.run_on_launch:
            threading.Thread(target=self._run_async, daemon=True).start()
        # 「静默启动」= 启动时不弹窗；未勾选则启动即显示设置窗口
        if not self.cfg.silent_launch:
            self._want_settings = True
        self.root.after(150, self._pump)
        self.root.mainloop()


def main() -> None:
    TrayApp().run()


if __name__ == "__main__":
    main()
