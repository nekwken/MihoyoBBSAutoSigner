from __future__ import annotations

from pathlib import Path

from app_config import BBS_ROOT, LOG_PATH, TrayConfig, engine_config_path
from device_identity import ensure_device
from runner import _log

try:
    import yaml
except ImportError:
    yaml = None


def bbs_config_path() -> Path:
    return engine_config_path(create=True)


_BAD_TOKENS = {"", "StokenError", "CookieError", "None", "null"}


def load_account_info() -> dict:
    """读取引擎侧登录态。UI 与签到共用此判定，避免「界面已登录 / 签到说无账号」。"""
    path = bbs_config_path()
    empty = {
        "logged_in": False,
        "stuid": "",
        "mid": "",
        "stoken_set": False,
        "stoken_len": 0,
        "cookie_has_uid": False,
        "reason": "",
        "error": "",
        "path": str(path),
    }
    if yaml is None:
        empty["error"] = "缺少 PyYAML，无法读取签到配置"
        return empty
    if not path.exists():
        empty["error"] = f"未找到签到配置：{path}"
        empty["reason"] = "未登录，请先在「账号」页短信登录"
        return empty
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        empty["error"] = f"读取签到配置失败：{e}"
        empty["reason"] = "签到配置无法解析，请在「账号」页重新登录"
        return empty
    acc = data.get("account") or {}
    stoken = str(acc.get("stoken") or "").strip()
    stuid = str(acc.get("stuid") or "").strip()
    mid = str(acc.get("mid") or "").strip()
    cookie = str(acc.get("cookie") or "")
    stoken_ok = stoken not in _BAD_TOKENS
    cookie_ok = cookie not in _BAD_TOKENS
    has_id = bool(stuid or mid)
    cookie_has_uid = any(k in cookie for k in ("ltuid=", "account_id=", "stuid="))
    logged_in = stoken_ok and has_id

    reason = ""
    if stoken == "StokenError":
        reason = "登录状态已失效，请在「账号」页重新登录"
    elif cookie == "CookieError":
        reason = "Cookie 已失效，请在「账号」页重新登录"
    elif not stoken_ok and not has_id:
        reason = "未登录，请先在「账号」页短信登录"
    elif not stoken_ok and has_id:
        reason = "签到配置缺少 stoken，请在「账号」页重新登录"
    elif stoken_ok and not has_id:
        reason = "登录凭证缺少 UID/mid，请在「账号」页重新登录"
    elif logged_in and not cookie_ok:
        reason = "Cookie 缺失或无效，请在「账号」页重新登录"
        logged_in = False

    return {
        "logged_in": logged_in,
        "stuid": stuid,
        "mid": mid,
        "stoken_set": stoken_ok,
        "stoken_len": len(stoken),
        "cookie_has_uid": cookie_has_uid,
        "cookie_ok": cookie_ok,
        "reason": reason,
        "error": "",
        "path": str(path),
    }


def format_account_status(info: dict | None = None, nickname: str = "") -> str:
    info = info or load_account_info()
    if info.get("error"):
        path = info.get("path") or ""
        return f"{info['error']}\n配置路径：{path}" if path else str(info["error"])
    if not info.get("logged_in"):
        return info.get("reason") or "未登录，请使用短信验证码登录"
    stuid = info.get("stuid") or "-"
    nick = (nickname or "").strip()
    return f"UID：{stuid}\n米游社昵称：{nick or '（获取中…）'}"


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
        # 昵称/UID 仅作展示缓存；引擎凭证已清空时必须一并清掉，
        # 否则界面仍像「登录过」，签到却报无账号
        cfg.account_nickname = ""
        cfg.account_stuid = ""
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
