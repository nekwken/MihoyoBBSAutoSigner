from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from typing import Any
from pathlib import Path

import httpx

from app_config import LOG_PATH


def _log(text: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {text}\n")
    except Exception:
        pass


def generate_device_id() -> str:
    return str(uuid.uuid4())


def _initial_fp(device_id: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_DNS, device_id).hex[:13]


def register_device_fp(device_id: str) -> str:
    seed_id = str(uuid.uuid4())
    seed_time = str(int(time.time() * 1000))
    initial = _initial_fp(device_id)
    hex_id = device_id.replace("-", "")[:16]
    body: dict[str, Any] = {
        "device_id": hex_id,
        "seed_id": seed_id,
        "seed_time": seed_time,
        "platform": "2",
        "device_fp": initial,
        "app_name": "bbs_cn",
        "ext_fields": json.dumps(
            {
                "model": "PJX110",
                "brand": "OnePlus",
                "androidId": hex_id,
                "osVersion": "16",
                "deviceType": "PHONE",
            },
            separators=(",", ":"),
        ),
        "bbs_device_id": device_id,
    }
    try:
        r = httpx.post(
            "https://public-data-api.mihoyo.com/device-fp/api/getFp",
            json=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "okhttp/4.9.3",
                "x-rpc-app_id": "bll8iq97cem8",
                "x-rpc-client_type": "2",
            },
            timeout=20,
        )
        data = r.json()
        fp = str((data.get("data") or {}).get("device_fp") or "")
        if fp:
            return fp
        _log(f"getFp 未返回 fp ret={data.get('retcode')} msg={data.get('message')}")
    except Exception as e:
        _log(f"getFp 请求失败：{e}")
    return initial


def ensure_device(cfg) -> tuple[str, str]:
    did = (cfg.device_id or "").strip()
    fp = (cfg.device_fp or "").strip()
    if not did:
        did = generate_device_id()
        _log(f"生成独立 device_id={did}")
    if not fp:
        fp = register_device_fp(did)
        _log(f"获取/生成 device_fp={fp}")
    if cfg.device_id != did or cfg.device_fp != fp:
        cfg.device_id = did
        cfg.device_fp = fp
        try:
            cfg.save()
        except Exception:
            pass
    return did, fp
