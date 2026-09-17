from __future__ import annotations

import random
import threading
from datetime import datetime, timedelta
from typing import Callable

from app_config import TrayConfig


def parse_hhmm(text: str) -> tuple[int, int] | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        hh, mm = text.split(":")
        h, m = int(hh), int(mm)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        return h, m
    except Exception:
        return None


def next_run_time(cfg: TrayConfig, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now()
    times = []
    for t in cfg.schedule_times:
        parsed = parse_hhmm(t)
        if not parsed:
            continue
        h, m = parsed
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        times.append(candidate)
    if not times:
        return None
    return min(times)


class Scheduler(threading.Thread):
    def __init__(
        self,
        on_fire: Callable[[], None],
        get_cfg: Callable[[], TrayConfig],
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(daemon=True, name="mihoyo-tray-scheduler")
        self._on_fire = on_fire
        self._get_cfg = get_cfg
        self._on_status = on_status or (lambda s: None)
        self._stop = threading.Event()
        self._last_fire_date = ""
        self._fired_times_today: set[str] = set()

    def stop(self) -> None:
        self._stop.set()

    def _status(self, text: str) -> None:
        try:
            self._on_status(text)
        except Exception:
            pass

    def run(self) -> None:
        self._stop.wait(3)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                self._status(f"调度异常：{e}")
            self._stop.wait(20)

    def _tick(self) -> None:
        cfg = self._get_cfg()
        if not cfg.schedule_enabled or not cfg.feature_enabled():
            self._status("定时关闭")
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if today != self._last_fire_date:
            self._last_fire_date = today
            self._fired_times_today.clear()
        for label in cfg.schedule_times:
            parsed = parse_hhmm(label)
            if not parsed:
                continue
            h, m = parsed
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            delta = (now - target).total_seconds()
            if 0 <= delta <= 90 and label not in self._fired_times_today:
                delay = random.randint(0, max(0, int(cfg.random_delay_sec)))
                if delay:
                    self._status(f"{delay}s 后执行 {label} 签到")
                    if self._stop.wait(delay):
                        return
                self._fired_times_today.add(label)
                self._status(f"定时触发 {label}")
                self._on_fire()
                return
        nxt = next_run_time(cfg, now)
        if nxt:
            self._status(f"下次 {nxt.strftime('%m-%d %H:%M')}")
