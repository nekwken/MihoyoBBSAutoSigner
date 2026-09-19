from __future__ import annotations

from pathlib import Path

from app_config import BBS_ROOT, LOG_PATH, TrayConfig
from device_identity import ensure_device
from runner import _log

try:
    import yaml
except ImportError:
    yaml = None


def bbs_config_path() -> Path:
    return BBS_ROOT / "config" / "config.yaml"


def load_account_info() -> dict:
    path = bbs_config_path()
    empty = {"logged_in": False, "stuid": "", "mid": "", "stoken_set": False, "error": ""}
    if not path.exists():
        empty["error"] = "未找到 config.yaml"
        return empty
    if yaml is None:
        empty["error"] = "缺少 PyYAML"
        return empty
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        empty["error"] = f"读取失败：{e}"
        return empty
    acc = data.get("account") or {}
    stoken = (acc.get("stoken") or "").strip()
    stuid = str(acc.get("stuid") or "").strip()
    mid = str(acc.get("mid") or "").strip()
    logged_in = bool(stoken) and bool(stuid or mid)
    return {
        "logged_in": logged_in,
        "stuid": stuid,
        "mid": mid,
        "stoken_set": bool(stoken),
        "stoken_len": len(stoken),
        "error": "",
    }


def format_account_status(info: dict | None = None) -> str:
    info = info or load_account_info()
    if info.get("error"):
        return info["error"]
    if not info.get("logged_in"):
        return "未登录，请使用短信验证码登录"
    stuid = info.get("stuid") or "-"
    mid = info.get("mid") or "-"
    return f"UID：{stuid}\n米游社ID：{mid}"


def logout_and_clear(cfg: TrayConfig | None = None) -> tuple[bool, str]:
    path = bbs_config_path()
    if not path.exists():
        return False, "未找到 config.yaml"
    if yaml is None:
        return False, "缺少 PyYAML"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return False, f"读取配置失败：{e}"

    acc = data.setdefault("account", {})
    acc["cookie"] = ""
    acc["stuid"] = ""
    acc["stoken"] = ""
    acc["mid"] = ""

    cloud = data.setdefault("cloud_games", {}).setdefault("cn", {})
    cloud["enable"] = False
    for node in ("genshin", "zzz", "honkai_sr"):
        item = cloud.setdefault(node, {"enable": False, "token": ""})
        item["enable"] = False
        item["token"] = ""

    if cfg is not None:
        cfg.device_id = ""
        cfg.device_fp = ""
        cfg.cloud_genshin = False
        cfg.cloud_sr = False
        cfg.cloud_zzz = False
        cfg.cloud_genshin_token = ""
        cfg.cloud_sr_token = ""
        cfg.cloud_zzz_token = ""
        try:
            ensure_device(cfg)
        except Exception:
            pass
        device = data.setdefault("device", {})
        device["id"] = cfg.device_id
        device["fp"] = cfg.device_fp
        cfg.save()

    try:
        path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    except Exception as e:
        return False, f"写入配置失败：{e}"

    try:
        if LOG_PATH.exists():
            LOG_PATH.write_text("", encoding="utf-8")
    except Exception:
        pass

    _log("已退出登录并清除账号数据、云游戏凭证与运行日志")
    return True, "已退出登录，账号数据、云游戏凭证与运行日志已清除"
