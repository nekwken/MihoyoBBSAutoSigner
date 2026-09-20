"""把官方 embeddable Python + 引擎依赖捆绑进发布包，使 release 免装 Python。"""
from __future__ import annotations

import subprocess
import urllib.request
import zipfile
from pathlib import Path

PY_EMBED_URL = "https://www.python.org/ftp/python/3.13.5/python-3.13.5-embed-amd64.zip"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
DEPS = ["httpx", "requests", "PyYAML"]

CACHE = Path(__file__).resolve().parent / "cache"


def download(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print("downloading", url)
    urllib.request.urlretrieve(url, dest)
    return dest


def bundle(stage: Path) -> Path:
    runtime = stage / "runtime"
    if (runtime / "python.exe").exists():
        print("runtime already bundled:", runtime)
        return runtime
    runtime.mkdir(parents=True, exist_ok=True)
    embed_zip = download(PY_EMBED_URL, CACHE / "python-embed-amd64.zip")
    with zipfile.ZipFile(embed_zip) as zf:
        zf.extractall(runtime)

    # 嵌入式 Python 一旦存在 ._pth 就进入隔离模式：脚本所在目录不会进入 sys.path，
    # 必须显式写入引擎目录，否则引擎 import 同级模块（如 push）会失败
    pth = runtime / "python313._pth"
    pth.write_text(
        "python313.zip\n.\nLib\\site-packages\n..\\engine\\MihoyoBBSTools\nimport site\n",
        encoding="ascii",
    )

    py = runtime / "python.exe"
    get_pip = download(GET_PIP_URL, CACHE / "get-pip.py")
    # 参数列表调用（不经 shell），参数均为本地固定路径与包名
    subprocess.run([str(py), str(get_pip), "--no-warn-script-location", "-q"],
                   check=True, shell=False)
    subprocess.run([str(py), "-m", "pip", "install", "--no-warn-script-location", "-q", *DEPS],
                   check=True, shell=False)

    probe = subprocess.run(
        [str(py), "-c", "import yaml, httpx, requests; print('bundled runtime ok')"],
        capture_output=True, text=True, timeout=60,
    )
    if probe.returncode != 0:
        raise SystemExit(f"bundled runtime probe failed:\n{probe.stdout}\n{probe.stderr}")
    print(probe.stdout.strip())
    return runtime
