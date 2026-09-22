from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from app_config import BBS_ROOT, HOME, LOG_PATH, TrayConfig, engine_config_path
from device_identity import ensure_device

try:
    import yaml
except ImportError:
    yaml = None

FAIL_MARKERS = (
    "请填入 Cookies",
    "无 Stoken",
    "账号 Cookie",
    "账号 Stoken",
    "Cookie 出错",
    "Stoken 出错",
    "Cookie 有问题",
    "Stoken 有问题",
    "Cookie 已删除",
    "Stoken 已删除",
    "CookieError",
    "StokenError",
    "Cookie expires",
    "Stoken expires",
    "cookie 可能已过期",
    "stoken/cookie 可能已过期",
    "cookie 缺少 UID",
    "登录失败",
    "登录失效",
    "请重新设置 cookie",
    "请重新抓取",
    "并没有绑定任何",
    "账号没有绑定任何",
    "Traceback (most recent call last)",
    "UnboundLocalError",
)

SUCCESS_MARKERS = (
    "签到成功",
    "已经签到过了",
    "今天已经全部完成了",
    "打卡成功",
    "RESULT: SUCCESS",
)


def _bbs_config_path() -> Path:
    return engine_config_path(create=True)


def _log(text: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {text}\n")
    except Exception:
        pass


ENGINE_DEPS_PROBE = (
    "import yaml\n"
    "try:\n"
    "    import httpx\n"
    "except ImportError:\n"
    "    import requests\n"
)


def find_engine_python() -> str:
    candidates = []
    if getattr(sys, "frozen", False):
        # 发布包内置运行时优先，其次用户显式指定，最后系统 PATH
        bundled = HOME / "runtime" / "python.exe"
        if bundled.exists():
            candidates.append(str(bundled))
        override = os.environ.get("MIHOYO_PYTHON")
        if override:
            candidates.append(override)
        for name in ("python", "python3"):
            p = shutil.which(name)
            if p and "WindowsApps" not in p:
                candidates.append(p)
        p = shutil.which("py")
        if p:
            candidates.append(p + " -3")
    else:
        candidates.append(sys.executable)

    for cand in candidates:
        parts = cand.split()
        if _deps_ok(parts):
            return cand
    return ""


def _deps_ok(parts: list[str]) -> bool:
    try:
        proc = subprocess.run(
            [*parts, "-c", ENGINE_DEPS_PROBE],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode == 0
    except Exception:
        return False


def _account_snapshot() -> dict:
    """与设置页共用 account_store 的登录态判定，避免两套逻辑各说各话。"""
    from account_store import load_account_info
    return load_account_info()


def _account_logged_in() -> bool:
    return bool(_account_snapshot().get("logged_in"))


def apply_features(cfg: TrayConfig) -> Path:
    path = _bbs_config_path()
    if not path.exists():
        raise FileNotFoundError(f"未找到配置：{path}")
    if yaml is None:
        raise RuntimeError("缺少 PyYAML")

    ensure_device(cfg)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # 兼容缺键的手工配置：补齐引擎必需字段，避免引擎下标取值报 KeyError
    data.setdefault("version", 15)
    data.setdefault("enable", True)
    data.setdefault("push", "")
    acc = data.setdefault("account", {})

    # repair missing stuid from mid/cookie when possible
    if not str(acc.get("stuid") or "").strip():
        cookie = str(acc.get("cookie") or "")
        for key in ("stuid=", "ltuid=", "account_id_v2=", "account_id="):
            if key in cookie:
                try:
                    acc["stuid"] = cookie.split(key, 1)[1].split(";", 1)[0].strip()
                    break
                except Exception:
                    pass
        if not str(acc.get("stuid") or "").strip() and cfg.device_id:
            pass
        _log(f"repair stuid -> {acc.get('stuid')!r}")

    bbs = data.setdefault("mihoyobbs", {})
    bbs["enable"] = bool(cfg.enable_bbs)
    bbs["checkin"] = bool(cfg.enable_bbs)
    bbs["checkin_list"] = list(cfg.checkin_list)

    cloud = data.setdefault("cloud_games", {}).setdefault("cn", {})
    cloud["enable"] = bool(cfg.cloud_genshin or cfg.cloud_zzz or cfg.cloud_sr)
    for key, flag, token_field in (("genshin", cfg.cloud_genshin, "cloud_genshin_token"),
                                   ("zzz", cfg.cloud_zzz, "cloud_zzz_token"),
                                   ("honkai_sr", cfg.cloud_sr, "cloud_sr_token")):
        node = cloud.setdefault(key, {"enable": False, "token": ""})
        node["enable"] = bool(flag)
        token = str(getattr(cfg, token_field) or "").strip()
        if token:
            node["token"] = token

    device = data.setdefault("device", {})
    device["id"] = cfg.device_id
    device["fp"] = cfg.device_fp
    device.setdefault("name", "OnePlus PJX110")
    device.setdefault("model", "PJX110")

    games = data.setdefault("games", {})
    cn = games.setdefault("cn", {})
    cn.setdefault("useragent", "")
    cn.setdefault("retries", 3)
    any_game = any(
        [
            cfg.enable_genshin,
            cfg.enable_honkai2,
            cfg.enable_honkai3rd,
            cfg.enable_tears,
            cfg.enable_honkai_sr,
            cfg.enable_zzz,
        ]
    )
    cn["enable"] = any_game

    def _g(key: str, on: bool) -> None:
        node = cn.setdefault(key, {})
        node["checkin"] = bool(on)
        node.setdefault("black_list", [])

    _g("genshin", cfg.enable_genshin)
    _g("honkai2", cfg.enable_honkai2)
    _g("honkai3rd", cfg.enable_honkai3rd)
    _g("tears_of_themis", cfg.enable_tears)
    _g("honkai_sr", cfg.enable_honkai_sr)
    _g("zzz", cfg.enable_zzz)

    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    cfg.save()
    _log(f"device_id={cfg.device_id} device_fp={cfg.device_fp} stuid={acc.get('stuid')!r}")
    return path


def summarize_run(returncode: int, out: str) -> tuple[bool, str]:
    text = out or ""
    result_line = ""
    for line in text.splitlines():
        if "RESULT:" in line:
            result_line = line
            break

    # 登录有效但查不到角色：比 RESULT: PARTIAL 更具体，优先展示
    if "并没有绑定任何" in text or "账号没有绑定任何" in text:
        return False, "未查询到已绑定的游戏角色。请确认米哈游账号已绑定对应游戏，或在「账号」页重新登录"

    if result_line:
        if "RESULT: SUCCESS" in result_line:
            if "已签到" in result_line or "已完成" in result_line:
                return True, "今日已签到/任务已完成"
            if "签到成功" in result_line:
                return True, "签到成功"
            return True, "签到任务完成"
        if "RESULT: PARTIAL" in result_line:
            return False, "部分任务失败，请查看日志"
        if "RESULT: FAILURE" in result_line:
            detail = result_line.split("RESULT: FAILURE", 1)[-1].lstrip(" |")
            if "验证码" in detail:
                return False, "签到触发验证码，请手动处理"
            if "Cookie" in detail:
                return False, "未登录或登录状态失效，请在账号页重新登录"
            if "Stoken" in detail:
                return False, "登录状态无效，请在账号页重新登录"
            if "绑定" in detail:
                return False, "未查询到已绑定的游戏角色。请确认米哈游账号已绑定对应游戏，或在「账号」页重新登录"
            detail = detail.strip()
            if not detail or detail.endswith(":") or detail.endswith("："):
                return False, "签到未完成，请查看日志"
            return False, detail[:80]

    fail_hit = next((m for m in FAIL_MARKERS if m in text), "")
    success_hit = next((m for m in SUCCESS_MARKERS if m in text), "")

    if fail_hit:
        if "Cookies" in fail_hit or "Cookie" in fail_hit or "UID" in fail_hit:
            msg = "未登录或账号信息不完整，请在账号页重新登录"
        elif "Stoken" in fail_hit:
            msg = "登录状态无效，请在账号页重新登录"
        elif "Traceback" in fail_hit or "UnboundLocal" in fail_hit:
            msg = "签到引擎异常退出，请查看日志"
        else:
            msg = f"签到失败：{fail_hit}"
        return False, msg

    if returncode != 0:
        return False, f"签到失败（退出码 {returncode}），请查看日志"

    if success_hit == "今天已经全部完成了":
        return True, "今日任务已完成"
    if success_hit in ("签到成功", "打卡成功", "RESULT: SUCCESS"):
        return True, "签到成功"
    if success_hit == "已经签到过了":
        return True, "今日已签到"

    if not text.strip():
        return False, "签到引擎无输出（可能未正确调用 Python），请查看日志"
    if "Config 加载完毕" in text and not success_hit:
        return False, "签到未完成，请查看日志"

    return False, "签到结果不明确，请查看日志"


def refresh_cloud_tokens(cfg: TrayConfig) -> list[str]:
    """签到时按需刷新勾选的云游戏凭证，返回提示信息列表（失败不阻断签到）。"""
    notes: list[str] = []
    if not (cfg.cloud_genshin or cfg.cloud_sr or cfg.cloud_zzz):
        return notes
    path = _bbs_config_path()
    if not path.exists() or yaml is None:
        return ["云游戏凭证未刷新：未找到签到配置"]
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return [f"云游戏凭证未刷新：{e}"]
    acc = data.get("account") or {}
    stoken = str(acc.get("stoken") or "").strip()
    mid = str(acc.get("mid") or "").strip()
    stuid = str(acc.get("stuid") or "").strip()
    if not (stoken and mid and stuid):
        return ["云游戏凭证未刷新：账号信息不完整"]

    from stoken_login import acquire_cloud_token, write_cloud_token

    changed = False
    for game, label, on in (("genshin", "云原神", cfg.cloud_genshin),
                            ("sr", "云星穹铁道", cfg.cloud_sr),
                            ("zzz", "云绝区零", cfg.cloud_zzz)):
        if not on:
            continue
        try:
            ok, msg = acquire_cloud_token(game, stoken, mid, stuid)
        except Exception as e:
            notes.append(f"{label}凭证刷新异常：{e}")
            continue
        if ok:
            write_cloud_token(data, game, msg)
            changed = True
            _log(f"{label}凭证已刷新")
        else:
            notes.append(f"{label}凭证刷新失败：{msg}")

    if changed:
        try:
            path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8")
        except Exception as e:
            notes.append(f"云游戏凭证写入失败：{e}")
    return notes


def repair_account_identity(cfg: TrayConfig) -> list[str]:
    """补全账号 uid / 米游社昵称（登录取不到 uid 时用 stoken 换取）。失败不阻断签到。"""
    notes: list[str] = []
    path = _bbs_config_path()
    if not path.exists() or yaml is None:
        return notes
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return [f"账号信息检查失败：{e}"]
    acc = data.setdefault("account", {})
    stoken = str(acc.get("stoken") or "").strip()
    mid = str(acc.get("mid") or "").strip()
    stuid = str(acc.get("stuid") or "").strip()
    if not stoken:
        return notes

    from stoken_login import StokenResult, exchange_cookie, fetch_account_profile, write_bbs_config

    did, fp = ensure_device(cfg)
    cookie = str(acc.get("cookie") or "")
    res = StokenResult()
    res.ok = True
    res.stoken = stoken
    res.mid = mid
    res.stuid = stuid
    for key, attr in (("cookie_token_v2", "cookie_token"), ("cookie_token", "cookie_token"),
                      ("ltoken_v2", "ltoken"), ("ltoken", "ltoken")):
        if getattr(res, attr):
            continue
        m = re.search(rf"(?:^|;\s*){key}=([^;]+)", cookie)
        if m:
            setattr(res, attr, m.group(1))

    if not stuid:
        uid, nick = fetch_account_profile(stoken, mid, did, fp, "")
        if not uid:
            return ["账号 UID 缺失且补全失败，请在「账号」页重新登录"]
        res.stuid = uid
        res.nickname = nick
        notes.append("已补全账号 UID（此前缺失导致签到失败）")
        if nick:
            cfg.account_nickname = nick
        cfg.account_stuid = str(uid)

    # 游戏签到依赖 cookie_token / ltoken，缺失时用 stoken 重新换取
    if not res.cookie_token or not res.ltoken:
        try:
            ct, lt = exchange_cookie(res.stoken, res.mid, res.stuid, did, fp)
            if ct or lt:
                res.cookie_token = ct or res.cookie_token
                res.ltoken = lt or res.ltoken
                notes.append("已补全 cookie_token / ltoken")
        except Exception as e:
            notes.append(f"cookie_token 补全失败：{e}")

    if notes:
        write_bbs_config(res)
        try:
            cfg.save()
        except Exception:
            pass
    elif not cfg.account_nickname:
        _, nick = fetch_account_profile(stoken, mid, did, fp, stuid)
        if nick:
            cfg.account_nickname = nick
            cfg.account_stuid = stuid
            try:
                cfg.save()
            except Exception:
                pass
    return notes


def run_checkin(cfg: TrayConfig | None = None) -> tuple[bool, str]:
    cfg = cfg or TrayConfig.load()
    if not cfg.feature_enabled():
        msg = "未启用任何签到功能，请先在设置中打开"
        _log(msg)
        cfg.last_status = msg
        cfg.save()
        return False, msg

    snap = _account_snapshot()
    _log(
        f"account snapshot path={snap.get('path')!r} "
        f"logged_in={snap.get('logged_in')} "
        f"stuid={snap.get('stuid')!r} mid={snap.get('mid')!r} "
        f"stoken_set={snap.get('stoken_set')} cookie_has_uid={snap.get('cookie_has_uid')} "
        f"cookie_ok={snap.get('cookie_ok')} reason={snap.get('reason')!r} error={snap.get('error')!r}"
    )
    if not snap.get("logged_in"):
        msg = (
            snap.get("reason")
            or snap.get("error")
            or "未登录，请先在「账号」页短信登录"
        )
        _log(msg)
        cfg.last_run = datetime.now().isoformat(timespec="seconds")
        cfg.last_status = msg
        cfg.save()
        return False, msg

    try:
        apply_features(cfg)
    except Exception as e:
        msg = f"写入签到配置失败：{e}"
        _log(msg)
        return False, msg

    identity_notes = repair_account_identity(cfg)
    for note in identity_notes:
        _log(note)
    cloud_notes = refresh_cloud_tokens(cfg)
    for note in cloud_notes:
        _log(note)

    main_py = BBS_ROOT / "main.py"
    if not main_py.exists():
        msg = f"未找到 {main_py}"
        _log(msg)
        return False, msg

    python = find_engine_python()
    if not python:
        msg = "未找到可运行签到引擎的 Python（需已安装 pyyaml 和 httpx/requests）"
        _log(msg)
        return False, msg
    _log(f"engine python={python} frozen={getattr(sys, 'frozen', False)} main={main_py}")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    _log("开始签到子进程…")
    # 内置运行时（嵌入式 Python）处于隔离模式，脚本目录不会进入 sys.path，
    # 因此显式插入引擎目录后再执行 main.py，避免 import 同级模块失败
    boot = (
        "import runpy, sys; "
        f"sys.path.insert(0, {str(BBS_ROOT)!r}); "
        f"sys.argv = [{str(main_py)!r}]; "
        f"runpy.run_path({str(main_py)!r}, run_name='__main__')"
    )
    cmd = [python, "-c", boot] if " " not in python else [python, str(main_py)]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(BBS_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=420,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        msg = "签到超时（420s）"
        _log(msg)
        cfg.last_run = datetime.now().isoformat(timespec="seconds")
        cfg.last_status = msg
        cfg.save()
        return False, msg
    except Exception as e:
        msg = f"签到进程异常：{e}"
        _log(msg)
        return False, msg

    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    _log(f"engine returncode={proc.returncode} out_len={len(out)}")
    if not out.strip():
        _log("engine output empty")
    for line in out.splitlines():
        if line.strip():
            _log(line.strip())

    ok, summary = summarize_run(proc.returncode, out)
    if identity_notes:
        summary += "；" + "；".join(identity_notes)
    if cloud_notes:
        summary += "；" + "；".join(cloud_notes)
    cfg.last_run = datetime.now().isoformat(timespec="seconds")
    if ok:
        # 供调度器判断"当天是否已成功签到"：失败不计，便于稍后自动重试
        cfg.last_ok_run = cfg.last_run
    cfg.last_status = summary
    cfg.save()
    _log(f"结论：ok={ok} {summary}")
    return ok, summary


def read_log_tail(n: int = 80) -> str:
    if not LOG_PATH.exists():
        return "（暂无日志）"
    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])
