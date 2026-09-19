from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from app_config import BBS_ROOT, LOG_PATH, TrayConfig
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
    return BBS_ROOT / "config" / "config.yaml"


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
    path = _bbs_config_path()
    empty = {
        "logged_in": False,
        "stuid": "",
        "mid": "",
        "stoken_set": False,
        "error": "未找到 config.yaml",
    }
    if not path.exists() or yaml is None:
        return empty
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        empty["error"] = f"读取失败：{e}"
        return empty
    acc = data.get("account") or {}
    stoken = str(acc.get("stoken") or "").strip()
    stuid = str(acc.get("stuid") or "").strip()
    mid = str(acc.get("mid") or "").strip()
    cookie = str(acc.get("cookie") or "")
    return {
        "logged_in": bool(stoken) and stoken not in ("StokenError", "CookieError") and bool(stuid or mid),
        "stuid": stuid,
        "mid": mid,
        "stoken_set": bool(stoken) and stoken not in ("StokenError", "CookieError"),
        "cookie_has_uid": any(k in cookie for k in ("ltuid=", "account_id=", "stuid=")),
        "error": "",
    }


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

    data.setdefault("mihoyobbs", {})
    data["mihoyobbs"]["enable"] = bool(cfg.enable_bbs)
    data["mihoyobbs"]["checkin"] = bool(cfg.enable_bbs)
    data["mihoyobbs"]["checkin_list"] = list(cfg.checkin_list)
    data["mihoyobbs"]["read"] = bool(cfg.bbs_read)
    data["mihoyobbs"]["like"] = bool(cfg.bbs_like)
    data["mihoyobbs"]["share"] = bool(cfg.bbs_share)

    device = data.setdefault("device", {})
    device["id"] = cfg.device_id
    device["fp"] = cfg.device_fp
    device.setdefault("name", "OnePlus PJX110")
    device.setdefault("model", "PJX110")

    games = data.setdefault("games", {})
    cn = games.setdefault("cn", {})
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
                return False, "未登录或 Cookie 无效，请在账号页短信登录"
            if "Stoken" in detail:
                return False, "未登录或 Stoken 无效，请在账号页短信登录"
            return False, detail[:80] if detail else "签到失败，请查看日志"

    fail_hit = next((m for m in FAIL_MARKERS if m in text), "")
    success_hit = next((m for m in SUCCESS_MARKERS if m in text), "")

    if fail_hit:
        if "Cookies" in fail_hit or "Cookie" in fail_hit or "UID" in fail_hit:
            msg = "未登录或 Cookie/UID 不完整，请在账号页重新短信登录"
        elif "Stoken" in fail_hit:
            msg = "未登录或 Stoken 无效，请在账号页短信登录"
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
        f"account snapshot logged_in={snap.get('logged_in')} "
        f"stuid={snap.get('stuid')!r} mid={snap.get('mid')!r} "
        f"stoken_set={snap.get('stoken_set')} cookie_has_uid={snap.get('cookie_has_uid')}"
    )
    if not snap.get("logged_in"):
        msg = "未登录，请先在「账号 / Stoken」短信登录"
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
    try:
        proc = subprocess.run(
            [python, str(main_py)],
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
    cfg.last_run = datetime.now().isoformat(timespec="seconds")
    cfg.last_status = summary
    cfg.save()
    _log(f"结论：ok={ok} {summary}")
    return ok, summary


def read_log_tail(n: int = 80) -> str:
    if not LOG_PATH.exists():
        return "（暂无日志）"
    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])
