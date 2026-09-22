from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "MihoyoBBSAutoSigner"
APP_TITLE = "米游社自动签到器"
APP_VERSION = "2.0.0"


def _exe_or_file_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def app_home() -> Path:
    d = _exe_or_file_dir()
    if d.name.lower() == "dist" and d.parent.exists():
        return d.parent
    return d


def find_bbs_root() -> Path:
    starts = [_exe_or_file_dir(), app_home()]
    seen: set[Path] = set()
    for start in starts:
        cur = start
        for _ in range(6):
            if cur in seen:
                break
            seen.add(cur)
            for candidate in (cur / "MihoyoBBSTools", cur / "engine" / "MihoyoBBSTools"):
                if (candidate / "main.py").exists() and (candidate / "config").exists():
                    return candidate
            if cur.name == "MihoyoBBSTools" and (cur / "main.py").exists():
                return cur
            if cur.parent == cur:
                break
            cur = cur.parent
    return app_home().parent / "MihoyoBBSTools"


HOME = app_home()
BBS_ROOT = find_bbs_root()
CONFIG_PATH = HOME / "tray_config.json"
LOG_PATH = HOME / "tray.log"
ICON_PATH = HOME / "assets" / "icon.ico"

# 引擎 config.yaml 兜底模板（与 config.yaml.example 同构；配置文件缺失且无模板时使用）
ENGINE_CONFIG_FALLBACK = """enable: true
version: 15
push: ''
account:
  cookie: ''
  stuid: ''
  stoken: ''
  mid: ''
device:
  name: Xiaomi MI 6
  model: Mi 6
  id: ''
  fp: ''
mihoyobbs:
  enable: true
  checkin: true
  checkin_list:
  - 1
  - 2
  - 3
  - 4
  - 5
  - 6
  - 8
games:
  cn:
    enable: true
    useragent: ''
    retries: 3
    genshin:
      checkin: true
      black_list: []
    honkai2:
      checkin: false
      black_list: []
    honkai3rd:
      checkin: false
      black_list: []
    tears_of_themis:
      checkin: false
      black_list: []
    honkai_sr:
      checkin: false
      black_list: []
    zzz:
      checkin: false
      black_list: []
cloud_games:
  cn:
    enable: false
    genshin:
      enable: false
      token: ''
    zzz:
      enable: false
      token: ''
    honkai_sr:
      enable: false
      token: ''
"""


def engine_config_path(create: bool = True) -> Path:
    """引擎 config.yaml 路径；缺失时自动从 config.yaml.example 或内置模板生成。"""
    cfg_dir = BBS_ROOT / "config"
    path = cfg_dir / "config.yaml"
    if path.exists() or not create:
        return path
    try:
        cfg_dir.mkdir(parents=True, exist_ok=True)
        example = cfg_dir / "config.yaml.example"
        text = example.read_text(encoding="utf-8") if example.exists() else ENGINE_CONFIG_FALLBACK
        path.write_text(text, encoding="utf-8")
    except Exception:
        pass
    return path

# 统一勾选表：每个板块一行；game_key 为 None 表示该板块没有独立的游戏签到
BOARD_ROWS = [
    (3, "崩坏学院2", "honkai2"),
    (1, "崩坏3", "honkai3rd"),
    (2, "原神", "genshin"),
    (6, "崩坏：星穹铁道", "honkai_sr"),
    (8, "绝区零", "zzz"),
    (4, "未定事件簿", "tears"),
    (9, "因缘精灵", None),
    (10, "星布谷地", None),
    (5, "大别野", None),
]

DEFAULT = {
    "autostart": False,
    "run_on_launch": False,
    "silent_launch": False,
    "minimize_to_tray": True,
    "schedule_enabled": True,
    "schedule_times": ["09:30"],
    "random_delay_sec": 120,
    "enable_bbs": True,
    "enable_honkai_sr": True,
    "enable_zzz": True,
    "enable_genshin": False,
    "enable_honkai3rd": False,
    "enable_honkai2": False,
    "enable_tears": False,
    "checkin_list": [2, 6, 8],
    "cloud_genshin": False,
    "cloud_zzz": False,
    "cloud_sr": False,
    "cloud_genshin_token": "",
    "cloud_zzz_token": "",
    "cloud_sr_token": "",
    "account_nickname": "",
    "board_order": [],
    "board_hidden": [],
    "account_stuid": "",
    "ui_theme": "system",
    "ui_scale": 1.0,
    "device_id": "",
    "device_fp": "",
    "last_run": "",
    "last_ok_run": "",
    "last_status": "",
}


def _run_key(value) -> str:
    """把时间戳规范成可直接比较的 ISO 形式（旧版本写过带空格的形式）。"""
    return str(value or "").replace(" ", "T")


@dataclass
class TrayConfig:
    autostart: bool = False
    run_on_launch: bool = False
    silent_launch: bool = False
    minimize_to_tray: bool = True
    schedule_enabled: bool = True
    schedule_times: list[str] = field(default_factory=lambda: ["09:30"])
    random_delay_sec: int = 120
    enable_bbs: bool = True
    enable_honkai_sr: bool = True
    enable_zzz: bool = True
    enable_genshin: bool = False
    enable_honkai3rd: bool = False
    enable_honkai2: bool = False
    enable_tears: bool = False
    checkin_list: list[int] = field(default_factory=lambda: [2, 6, 8])
    cloud_genshin: bool = False
    cloud_zzz: bool = False
    cloud_sr: bool = False
    cloud_genshin_token: str = ""
    cloud_zzz_token: str = ""
    cloud_sr_token: str = ""
    account_nickname: str = ""
    board_order: list[int] = field(default_factory=list)
    board_hidden: list[int] = field(default_factory=list)
    account_stuid: str = ""
    ui_theme: str = "system"
    ui_scale: float = 1.0
    device_id: str = ""
    device_fp: str = ""
    last_run: str = ""
    last_ok_run: str = ""
    last_status: str = ""

    @classmethod
    def load(cls) -> "TrayConfig":
        data = dict(DEFAULT)
        if CONFIG_PATH.exists():
            try:
                data.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
            except Exception:
                pass
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        data = asdict(self)
        # 「上次运行 / 结果」只允许向前更新：设置窗与托盘各持一份 cfg，
        # 谁后保存谁就会把内存里的旧值写回磁盘 → 重启后误判「今天还没签」而重复补签。
        try:
            disk = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
        except Exception:
            disk = {}
        if isinstance(disk, dict):
            disk_run = _run_key(disk.get("last_run"))
            mem_run = _run_key(data.get("last_run"))
            if disk_run > mem_run:
                for key in ("last_run", "last_ok_run", "last_status"):
                    if key in disk:
                        data[key] = disk[key]
            elif not mem_run and disk.get("last_status"):
                data["last_status"] = disk["last_status"]
        CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def feature_enabled(self) -> bool:
        return any(
            [
                self.enable_bbs,
                self.enable_honkai_sr,
                self.enable_zzz,
                self.enable_genshin,
                self.enable_honkai3rd,
                self.enable_honkai2,
                self.enable_tears,
            ]
        )
