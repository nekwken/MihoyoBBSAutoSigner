"""验证码自动解法器接入（社区签到 / 游戏签到共用）。

在 `config.yaml` 里配好 captcha 段即可**自动过验证码**：

    captcha:
      enable: true
      type: rrocr          # 支持 rrocr / yescaptcha
      api: ""              # 打码平台接口地址（留空用该类型的默认地址）
      key: ""              # 你的平台密钥

行为：
  · 未启用 / 缺 key → 返回 None，调用方走「提示人工完成」分支（与改造前一致）；
  · 启用后 → 把 gt/challenge 交给打码平台，拿回 validate，签到自动重试并完成；
  · 只在本地把结果交回调用方，不打印密钥、不落盘。

返回格式（调用方要求）：成功 {"challenge": str, "validate": str}；失败 None。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from request import http

# 常见打码平台的默认接口（可被 config 的 api 覆盖）
_DEFAULT_API = {
    "rrocr": "https://api.rrocr.com/api/recognize.html",
    "yescaptcha": "https://api.yescaptcha.com",
}

# 米游社的极验 itemid（rrocr 用）
_RROCR_ITEMID = "38888"
_REFERER = "https://webstatic.mihoyo.com/"


def _log(msg: str) -> None:
    try:
        from loghelper import log
        log.info(msg)
    except Exception:
        pass


def _solver_config() -> dict:
    """读 config.yaml 里的 captcha 段。"""
    try:
        import config as _config
        cfg = _config.config or {}
        return cfg.get("captcha") or {}
    except Exception:
        return {}


def _resolve(cfg: dict) -> tuple[str, str, str] | None:
    """返回 (类型, 接口地址, 密钥)；未启用或配置不全时返回 None。"""
    if not cfg.get("enable"):
        return None
    kind = str(cfg.get("type") or "rrocr").strip().lower()
    key = str(cfg.get("key") or "").strip()
    api = str(cfg.get("api") or "").strip() or _DEFAULT_API.get(kind, "")
    if not key or not api:
        return None
    return kind, api, key


def _solve_rrocr(api: str, key: str, gt: str, challenge: str) -> dict | None:
    """rrocr：POST 表单，result=success 时返回 validate。"""
    resp = http.post(url=api, data={
        "appkey": key,
        "gt": gt,
        "challenge": challenge,
        "itemid": _RROCR_ITEMID,
        "referer": _REFERER,
    })
    data = resp.json()
    if str(data.get("status")) == "0" or data.get("result") == "success":
        inner = data.get("data") or {}
        validate = inner.get("validate") or data.get("validate")
        new_challenge = inner.get("challenge") or challenge
        if validate:
            return {"challenge": str(new_challenge), "validate": str(validate)}
    return None


def _solve_yescaptcha(api: str, key: str, gt: str, challenge: str) -> dict | None:
    """yescaptcha：createTask + 轮询 getTaskResult。"""
    base = api.rstrip("/")
    payload = {
        "clientKey": key,
        "task": {
            "type": "GeetestTask",
            "websiteURL": _REFERER,
            "gt": gt,
            "challenge": challenge,
        },
    }
    created = http.post(url=base + "/createTask",
                        headers={"Content-Type": "application/json"},
                        data=json.dumps(payload)).json()
    task_id = created.get("taskId")
    if not task_id:
        return None
    for _ in range(60):                      # 最多等约 2 分钟
        time.sleep(2)
        got = http.post(url=base + "/getTaskResult",
                        headers={"Content-Type": "application/json"},
                        data=json.dumps({"clientKey": key, "taskId": task_id})).json()
        state = got.get("status")
        if state == "ready":
            sol = got.get("solution") or {}
            if sol.get("validate"):
                return {"challenge": str(sol.get("challenge") or challenge),
                        "validate": str(sol["validate"])}
            return None
        if state == "failed":
            return None
    return None


# ── 人工求解接力（托盘没在监听时完全不生效，行为与改造前一致）──────────────
_WATCH_NAME = "captcha_watch.json"      # 托盘在监听时会不断刷新这个心跳文件
_REQUEST_NAME = "captcha_request.json"  # 引擎写：请弹窗让用户过这个验证码
_RESULT_NAME = "captcha_result.json"    # 托盘写：用户过完了（或取消了）
_WATCH_FRESH_SEC = 15.0                 # 心跳多久算新鲜


def _engine_dir():
    """引擎配置所在目录（请求/结果/心跳文件都放这里，托盘用同一路径）。

    没配置时不慎创建的临时目录也不影响：目录不存在就直接返回 None。
    """
    try:
        import config as _config
        p = getattr(_config, "config_Path", "") or ""
        if p:
            return Path(p).parent
    except Exception:
        pass
    try:
        return Path(__file__).resolve().parent / "config"
    except Exception:
        return None


def _tray_is_watching(d) -> bool:
    """托盘是否正在监听（心跳文件新鲜）。"""
    try:
        f = d / _WATCH_NAME
        return f.exists() and (time.time() - f.stat().st_mtime) < _WATCH_FRESH_SEC
    except Exception:
        return False


def _cleanup(d) -> None:
    for name in (_REQUEST_NAME, _RESULT_NAME):
        try:
            (d / name).unlink()
        except Exception:
            pass


def _solve_manual(gt: str, challenge: str, kind: str) -> dict | None:
    """交给托盘弹窗人工过验证码；拿回 validate 后交给调用方继续签到。

    只有托盘在监听（心跳新鲜）时才会走这条路 —— 否则返回 None，
    调用方按原逻辑提示人工处理，**不会有任何等待或行为变化**。
    """
    d = _engine_dir()
    if d is None or not _tray_is_watching(d):
        return None
    cfg = _solver_config()
    wait_sec = float(cfg.get("manual_wait_sec") or 120)
    req_id = "%d-%d" % (int(time.time()), id(gt) % 100000)

    def _write_request() -> bool:
        try:
            tmp = d / (_REQUEST_NAME + ".tmp")
            tmp.write_text(json.dumps({
                "id": req_id, "gt": gt, "challenge": challenge,
                "kind": kind, "created": time.time(),
            }, ensure_ascii=False), encoding="utf-8")
            tmp.replace(d / _REQUEST_NAME)          # 原子替换，托盘不会读到半个文件
            return True
        except Exception as e:
            _log(f"写验证码请求失败：{type(e).__name__}")
            return False

    if not _write_request():
        return None
    _log(f"已请求人工过验证码（最多等 {wait_sec:.0f} 秒）…")
    deadline = time.time() + wait_sec
    try:
        while time.time() < deadline:
            time.sleep(0.5)
            res_path = d / _RESULT_NAME
            if not res_path.exists():
                continue
            try:
                res = json.loads(res_path.read_text(encoding="utf-8"))
            except Exception:
                continue                                 # 半截文件：下一轮再读
            if str(res.get("id") or "") != req_id:
                continue                                 # 不是这一次的，忽略
            if res.get("cancelled"):
                _log("用户取消了解验证码")
                return None
            validate = str(res.get("validate") or "").strip()
            if validate:
                _log("已拿到人工解出的 validate，继续签到")
                return {"challenge": str(res.get("challenge") or challenge),
                        "validate": validate}
            return None
    finally:
        _cleanup(d)
    return None


def _solve(gt: str, challenge: str, kind: str = "bbs") -> dict | None:
    resolved = _resolve(_solver_config())
    if resolved is None:
        # 没配打码平台 → 看托盘在不在监听，在就交给它弹窗人工过
        return _solve_manual(gt, challenge, kind)
    api_kind, api, key = resolved
    try:
        if api_kind == "rrocr":
            out = _solve_rrocr(api, key, gt, challenge)
        elif api_kind == "yescaptcha":
            out = _solve_yescaptcha(api, key, gt, challenge)
        else:
            _log(f"未知的打码平台类型：{api_kind}（支持 rrocr / yescaptcha）")
            return None
    except Exception as e:                    # 网络/解析异常不该中断整个签到
        _log(f"打码平台调用失败：{type(e).__name__}（请检查 captcha.api 与网络）")
        return None
    if out is None:
        _log("打码平台未返回 validate（余额不足或参数不对？）")
    return out


def game_captcha(gt: str, challenge: str) -> dict:
    # challenge 不要直接用传入的：平台可能返回新的 challenge
    return _solve(gt, challenge, "game")


def bbs_captcha(gt: str, challenge: str) -> dict:
    return _solve(gt, challenge, "bbs")
