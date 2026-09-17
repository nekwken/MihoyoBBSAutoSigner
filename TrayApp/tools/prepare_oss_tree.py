from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"E:\米游社自动签到")
TRAY = ROOT / "TrayApp"
ENGINE = ROOT / "MihoyoBBSTools"
PUB = ROOT / "publish" / "MihoyoBBSTray"

SECRET_PATTERNS = [
    re.compile(r"stoken=v2_[A-Za-z0-9._\-]{20,}"),
    re.compile(r"ltoken_v2=v2_[A-Za-z0-9._\-]{20,}"),
    re.compile(r"cookie_token=v2_[A-Za-z0-9._\-]{20,}"),
]

ENGINE_KEEP = [
    "account.py",
    "captcha.py",
    "cloudgames.py",
    "competition.py",
    "config.py",
    "docker.py",
    "error.py",
    "gamecheckin.py",
    "hoyo_checkin.py",
    "index.py",
    "loghelper.py",
    "login.py",
    "main.py",
    "main_multi.py",
    "mihoyobbs.py",
    "os_cloudgames.py",
    "push.py",
    "ql_main.py",
    "request.py",
    "server.py",
    "setting.py",
    "tools.py",
    "web_activity.py",
    "requirements.txt",
    "README.md",
    "LICENSE",
    "config.yaml.example",
]


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def scan_text(path: Path) -> list[str]:
    hits = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return hits
    for pat in SECRET_PATTERNS:
        if pat.search(text):
            hits.append(f"{path}: {pat.pattern[:40]}")
    return hits


def main() -> int:
    if PUB.exists():
        shutil.rmtree(PUB)
    PUB.mkdir(parents=True)

    # TrayApp source (no build artifacts / logs / secrets)
    ignore = shutil.ignore_patterns(
        "__pycache__",
        "*.pyc",
        "dist",
        "build",
        "*.spec",
        "tray.log",
        "tray_config.json",
        "private_*.json",
        "icon_source.png",
        "test_*.py",
        "upstream_run.txt",
        "nologin_run.txt",
        "config.yaml.bak_test",
        ".git",
    )
    shutil.copytree(TRAY, PUB / "TrayApp", ignore=ignore)

    # Engine: patched local copy, no real credentials
    eng_out = PUB / "engine" / "MihoyoBBSTools"
    eng_out.mkdir(parents=True)
    for name in ENGINE_KEEP:
        src = ENGINE / name
        if src.exists():
            copy_file(src, eng_out / name)
    # empty config example already in list; ensure present
    ex = ENGINE / "config" / "config.yaml.example"
    if ex.exists():
        copy_file(ex, eng_out / "config" / "config.yaml.example")

    # Root docs
    for name in ["README.md", "LICENSE", "OSS_PREP.md", "SECURITY.md", ".gitignore"]:
        src = TRAY / name
        if src.exists():
            copy_file(src, PUB / name)

    # Secret scan
    hits = []
    for p in PUB.rglob("*"):
        if p.is_file() and p.suffix.lower() in {".py", ".md", ".json", ".yaml", ".yml", ".txt"}:
            hits.extend(scan_text(p))
    print("secret_hits", len(hits))
    for h in hits[:20]:
        print(" ", h)

    print("publish_root", PUB)
    print("files", sum(1 for _ in PUB.rglob("*") if _.is_file()))
    return 0 if not hits else 2


if __name__ == "__main__":
    sys.exit(main())
