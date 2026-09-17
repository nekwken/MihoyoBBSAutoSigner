from __future__ import annotations

import hashlib
import json
import random
import string
import time
import uuid
import base64
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app_config import BBS_ROOT
from aigis_solver import parse_aigis_header, solve_aigis_challenge

PASSPORT = "https://passport-api.mihoyo.com"
SALT_PROD = "JwYDpKvLj6MrMqqYU6jTKF17KNO2PXoS"
APP_ID = "bll8iq97cem8"
APP_VERSION = "2.114.0"
SDK_VERSION = "2.42.0"
RSA_SPKI_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDDvekdPMHN3AYhm/vktJT+YJr7cI5"
    "DcsNKqdsx5DZX0gDuWFuIjzdwButrIYPNmRJ1G8ybDIF7oDW2eEpm5sMbL9zs9ExXCdv"
    "qrn51qELbqj0XxtMTIpaCHFSI50PfPpTFV9Xt/hmyVwokoOXFlAEgCn+QCgGs52bFoYM"
    "tyi+xEQIDAQAB"
)


def _public_key():
    return serialization.load_der_public_key(base64.b64decode(RSA_SPKI_B64))


def rsa_encrypt(plain: str) -> str:
    cipher = _public_key().encrypt(plain.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(cipher).decode("ascii")


def ds2(body_obj: dict, salt: str = SALT_PROD) -> str:
    t = str(int(time.time()))
    r = "".join(random.choices(string.ascii_letters + string.digits, k=6))
    b = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False)
    raw = f"salt={salt}&t={t}&r={r}&b={b}&q="
    return f"{t},{r},{hashlib.md5(raw.encode('utf-8')).hexdigest()}"


def _body_str(body_obj: dict) -> str:
    return json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False)


def passport_headers(device_id: str, device_fp: str, aigis: str | None = None) -> dict:
    h = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "okhttp/4.9.3",
        "x-rpc-app_id": APP_ID,
        "x-rpc-client_type": "2",
        "x-rpc-device_id": device_id,
        "x-rpc-device_fp": device_fp,
        "x-rpc-device_name": quote("OnePlus PJX110"),
        "x-rpc-device_model": "PJX110",
        "x-rpc-sys_version": "16",
        "x-rpc-game_biz": "bbs_cn",
        "x-rpc-app_version": APP_VERSION,
        "x-rpc-sdk_version": SDK_VERSION,
        "x-rpc-lifecycle_id": str(uuid.uuid4()),
        "x-rpc-account_version": SDK_VERSION,
        "Host": "passport-api.mihoyo.com",
    }
    if aigis is not None:
        h["x-rpc-aigis"] = aigis
    return h


class StokenResult:
    def __init__(self) -> None:
        self.ok = False
        self.message = ""
        self.stoken = ""
        self.mid = ""
        self.stuid = ""
        self.cookie_token = ""
        self.ltoken = ""
        self.aigis = ""
        self.aigis_challenge: dict = {}
        self.retcode: int | None = None
        self.sms_countdown = 0


def _parse_login(data: dict, res: StokenResult) -> None:
    token_obj = data.get("token") or {}
    user = data.get("user_info") or {}
    res.stoken = token_obj.get("token") or ""
    res.mid = user.get("mid") or ""
    res.stuid = str(user.get("aid") or user.get("uid") or "")
    if res.stoken:
        res.ok = True
        res.message = f"登录成功 stuid={res.stuid} mid={res.mid}"
    else:
        res.ok = False
        res.message = "登录响应无 stoken（可能触发风控）"


def _post(path: str, headers: dict, body_obj: dict, timeout: float = 30.0):
    headers = dict(headers)
    headers["DS"] = ds2(body_obj)
    r = httpx.post(
        f"{PASSPORT}/{path}",
        headers=headers,
        content=_body_str(body_obj).encode("utf-8"),
        timeout=timeout,
    )
    return r.json(), {k: v for k, v in r.headers.items()}


def _fill_aigis(res: StokenResult, raw_headers: dict) -> None:
    aigis = raw_headers.get("x-rpc-aigis") or raw_headers.get("X-Rpc-Aigis") or ""
    res.aigis = aigis
    res.aigis_challenge = parse_aigis_header(aigis)


def send_sms(
    mobile: str,
    device_id: str,
    device_fp: str,
    aigis: str = "",
    open_window: bool = True,
) -> StokenResult:
    res = StokenResult()
    body = {"area_code": rsa_encrypt("+86"), "mobile": rsa_encrypt(mobile)}

    def _once(aigis_val: str):
        headers = passport_headers(device_id, device_fp, aigis=aigis_val)
        return _post("account/ma-cn-verifier/verifier/createLoginCaptcha", headers, body)

    try:
        data, raw_h = _once(aigis)
    except Exception as e:
        res.message = f"网络错误：{e}"
        return res
    res.retcode = data.get("retcode")
    if data.get("retcode") == 0:
        payload = data.get("data") or {}
        try:
            res.sms_countdown = int(payload.get("countdown") or 60)
        except Exception:
            res.sms_countdown = 60
        if res.sms_countdown <= 0:
            res.sms_countdown = 60
        res.ok = True
        res.message = "验证码已发送，请查收短信"
        return res
    if data.get("retcode") == -3101:
        _fill_aigis(res, raw_h)
        challenge = res.aigis_challenge
        if challenge.get("gt") and challenge.get("session_id") and open_window:
            res.message = "需要图形验证，请在弹出窗口中完成…"
            solved = solve_aigis_challenge(challenge)
            if not solved:
                res.message = "图形验证未完成或窗口打开失败，可立即重试"
                res.ok = False
                res.sms_countdown = 0
                return res
            try:
                data2, _ = _once(solved)
            except Exception as e:
                res.message = f"验证后重试失败：{e}"
                res.sms_countdown = 0
                return res
            res.retcode = data2.get("retcode")
            if data2.get("retcode") == 0:
                payload = data2.get("data") or {}
                try:
                    res.sms_countdown = int(payload.get("countdown") or 60)
                except Exception:
                    res.sms_countdown = 60
                if res.sms_countdown <= 0:
                    res.sms_countdown = 60
                res.ok = True
                res.message = "图形验证通过，验证码已发送"
                return res
            res.message = f"验证后发送失败：{data2.get('retcode')} {data2.get('message')}"
            res.sms_countdown = 0
            return res
        res.message = "需要图形验证，但未获取到挑战数据，可立即重试"
        res.sms_countdown = 0
        return res
    res.message = f"发送失败：{data.get('retcode')} {data.get('message')}"
    res.sms_countdown = 0
    return res


def login_by_sms(
    mobile: str,
    captcha: str,
    device_id: str,
    device_fp: str,
    aigis: str = "",
    open_window: bool = True,
) -> StokenResult:
    res = StokenResult()
    body = {
        "area_code": rsa_encrypt("+86"),
        "mobile": rsa_encrypt(mobile),
        "captcha": captcha,
        "action_type": "login_by_mobile_captcha",
    }

    def _once(aigis_val: str):
        headers = passport_headers(
            device_id, device_fp, aigis=aigis_val if aigis_val else None
        )
        if not aigis_val:
            headers.pop("x-rpc-aigis", None)
        return _post("account/ma-cn-passport/app/loginByMobileCaptcha", headers, body)

    try:
        data, raw_h = _once(aigis)
    except Exception as e:
        res.message = f"网络错误：{e}"
        return res
    res.retcode = data.get("retcode")
    if data.get("retcode") == 0:
        _parse_login(data.get("data") or {}, res)
        return res
    if data.get("retcode") == -3101:
        _fill_aigis(res, raw_h)
        challenge = res.aigis_challenge
        if challenge.get("gt") and challenge.get("session_id") and open_window:
            res.message = "登录需要图形验证，请在窗口中完成…"
            solved = solve_aigis_challenge(challenge)
            if not solved:
                res.message = "图形验证未完成"
                return res
            try:
                data2, _ = _once(solved)
            except Exception as e:
                res.message = f"验证后登录失败：{e}"
                return res
            res.retcode = data2.get("retcode")
            if data2.get("retcode") == 0:
                _parse_login(data2.get("data") or {}, res)
                return res
            res.message = f"验证后登录失败：{data2.get('retcode')} {data2.get('message')}"
            return res
        res.message = "登录需要图形验证，但未获取到挑战"
        return res
    if data.get("retcode") == -3235:
        res.message = "新设备验证（-3235），请重新发送短信后再登录"
        return res
    res.message = f"短信登录失败：{data.get('retcode')} {data.get('message')}"
    return res


def login_verify(stoken: str, mid: str, device_id: str, device_fp: str) -> StokenResult:
    res = StokenResult()
    body = {
        "mid": mid,
        "token": {"token": stoken, "token_type": 1},
        "refresh": True,
    }
    headers = passport_headers(device_id, device_fp)
    try:
        data, _ = _post("account/ma-cn-session/app/verify", headers, body)
    except Exception as e:
        res.message = f"网络错误：{e}"
        res.stoken, res.mid, res.ok = stoken, mid, True
        return res
    if data.get("retcode") == 0:
        d = data.get("data") or {}
        token = ((d.get("token") or {}).get("token")) or ""
        res.stoken = token or stoken
        res.mid = mid
        res.ok = True
        res.message = "会话激活成功" + ("（stoken 已刷新）" if token else "")
        return res
    res.stoken, res.mid, res.ok = stoken, mid, True
    res.message = f"login_verify 跳过：{data.get('retcode')} {data.get('message')}"
    return res


def exchange_cookie(
    stoken: str, mid: str, stuid: str, device_id: str, device_fp: str
) -> tuple[str, str]:
    cookie_token = ""
    ltoken = ""
    for dst, attr in ((4, "cookie_token"), (2, "ltoken")):
        body = {
            "src_token": {"token": stoken, "token_type": 1},
            "mid": mid,
            "dst_token_type": dst,
        }
        headers = passport_headers(device_id, device_fp)
        headers["Cookie"] = f"stoken={stoken};mid={mid}"
        try:
            data, _ = _post("account/ma-cn-session/app/exchange", headers, body)
        except Exception:
            continue
        if data.get("retcode") != 0:
            if dst == 4:
                try:
                    h2 = passport_headers(device_id, device_fp)
                    h2["Cookie"] = f"stoken={stoken};mid={mid}"
                    h2["DS"] = ds2({})
                    r = httpx.get(
                        "https://api-takumi.mihoyo.com/auth/api/getCookieAccountInfoBySToken",
                        headers=h2,
                        params={"uid": stuid},
                        timeout=30,
                    )
                    j = r.json()
                    if j.get("retcode") == 0:
                        cookie_token = (j.get("data") or {}).get("cookie_token") or ""
                except Exception:
                    pass
            continue
        d = data.get("data") or {}
        tok = d.get("token") or d.get("cookie_token") or d.get("ltoken") or ""
        if isinstance(tok, dict):
            tok = tok.get("token") or ""
        if attr == "cookie_token":
            cookie_token = str(tok)
        else:
            ltoken = str(tok)
    return cookie_token, ltoken


def write_bbs_config(res: StokenResult, web_cookie: str | None = None):
    import yaml
    from pathlib import Path

    path = BBS_ROOT / "config" / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    acc = data.setdefault("account", {})
    acc["stuid"] = res.stuid
    acc["stoken"] = res.stoken
    acc["mid"] = res.mid
    cookie = acc.get("cookie") or ""
    kv = {}
    for p in cookie.split(";"):
        p = p.strip()
        if "=" in p:
            k, v = p.split("=", 1)
            kv[k.strip()] = v.strip()
    if res.stuid:
        kv["ltuid"] = res.stuid
        kv["ltuid_v2"] = res.stuid
        kv["account_id"] = res.stuid
        kv["account_id_v2"] = res.stuid
        kv["stuid"] = res.stuid
    if res.mid:
        kv["account_mid_v2"] = res.mid
        kv["ltmid_v2"] = res.mid
        kv["mid"] = res.mid
    if res.cookie_token:
        kv["cookie_token"] = res.cookie_token
        kv["cookie_token_v2"] = res.cookie_token
    if res.ltoken:
        kv["ltoken"] = res.ltoken
        kv["ltoken_v2"] = res.ltoken
    acc["cookie"] = (
        web_cookie
        if web_cookie
        else "; ".join(f"{k}={v}" for k, v in kv.items() if v)
    )
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return path
