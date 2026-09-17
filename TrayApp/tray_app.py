from __future__ import annotations

import threading
import tkinter as tk
from typing import Optional

import pystray
from PIL import Image

import autostart
from app_config import APP_NAME, TrayConfig
from device_identity import ensure_device
from make_icon import ensure_icon
from runner import read_log_tail, run_checkin
from scheduler import Scheduler, next_run_time
from settings_ui import SettingsWindow


class TrayApp:
    def __init__(self) -> None:
        self.cfg = TrayConfig.load()
        try:
            ensure_device(self.cfg)
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
        self.root.title("MihoyoBBSTray")

        self.settings = SettingsWindow(
            cfg=self.cfg,
            on_saved=self._on_saved,
            on_run_now=self._run_blocking_for_ui,
            on_request_quit=self._menu_quit,
            status_text=self._status,
            master=self.root,
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
                    checked=lambda item: bool(self.cfg.autostart or autostart.is_enabled()),
                ),
                pystray.MenuItem("关于", self._menu_about),
                pystray.MenuItem("查看日志", self._menu_log),
                pystray.MenuItem("退出", self._menu_quit),
            ),
        )
        self.scheduler = Scheduler(
            on_fire=self._run_async,
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

    def _run_async(self) -> None:
        if self._busy:
            self._set_status("签到进行中…")
            return

        def worker() -> None:
            self._busy = True
            self._set_status("签到中…")
            try:
                ok, msg = run_checkin(self.cfg)
                self.cfg = TrayConfig.load()
                self._set_status(msg if ok else f"失败：{msg}")
            except Exception as e:
                self._set_status(f"异常：{e}")
            finally:
                self._busy = False

        threading.Thread(target=worker, daemon=True, name="mihoyo-checkin").start()

    def _request_settings(self) -> None:
        self._want_settings = True

    def _menu_settings(self, icon=None, item=None) -> None:
        self._request_settings()

    def _menu_about(self, icon=None, item=None) -> None:
        self._request_settings()

    def _menu_log(self, icon=None, item=None) -> None:
        self._request_settings()

    def _menu_run(self, icon=None, item=None) -> None:
        self._run_async()

    def _run_blocking_for_ui(self) -> None:
        """Settings 窗口在后台线程调用；此处只跑签到，不做 UI。"""
        if self._busy:
            return
        self._busy = True
        self._set_status("签到中…")
        try:
            ok, msg = run_checkin(self.cfg)
            self.cfg = TrayConfig.load()
            self._set_status(msg if ok else f"失败：{msg}")
        except Exception as e:
            self._set_status(f"异常：{e}")
        finally:
            self._busy = False

    def _on_saved(self, cfg: TrayConfig) -> None:
        self.cfg = cfg
        self._set_status("设置已保存")

    def _menu_autostart(self, icon=None, item=None) -> None:
        self.cfg.autostart = not bool(self.cfg.autostart or autostart.is_enabled())
        self.cfg.save()
        autostart.set_enabled(self.cfg.autostart)
        self._set_status("已开启自启" if self.cfg.autostart else "已关闭自启")

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
        self.root.after(150, self._pump)
        self.root.mainloop()


def main() -> None:
    TrayApp().run()


if __name__ == "__main__":
    main()
