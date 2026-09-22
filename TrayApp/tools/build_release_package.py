from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(r"E:\米游社自动签到")
PUB = ROOT / "publish" / "MihoyoBBSAutoSigner"
TRAY = ROOT / "TrayApp"
PY = r"C:\Users\nekwken\AppData\Local\Programs\Python\Python313\python.exe"
DIST = PUB / "dist"
# 构建产物放到仓库外：内置运行时含第三方库，留在仓库树里既臃肿又会被安全扫描误报
OUT = ROOT / "out"
STAGE = OUT / "MihoyoBBSAutoSigner"
ZIP_PATH = OUT / "MihoyoBBSAutoSigner-win64.zip"


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print("+", cmd)                      # 直接打印参数列表，不拼命令字符串
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def patch_runtime_pth(stage: Path) -> None:
    """确保内置运行时的 ._pth 包含引擎目录（隔离模式下脚本目录不入 sys.path）。"""
    pth = stage / "runtime" / "python313._pth"
    if not pth.exists():
        return
    try:
        lines = [l for l in pth.read_text(encoding="ascii").splitlines() if l.strip()]
    except Exception:
        return
    keep = [l for l in lines if l.strip() != "import site"]
    if not any("MihoyoBBSTools" in l for l in keep):
        keep.append("..\\engine\\MihoyoBBSTools")
    keep.append("import site")
    pth.write_text("\n".join(keep) + "\n", encoding="ascii")
    print("runtime ._pth:", keep)


def main() -> None:
    # refresh TrayApp sources in publish tree
    for name in [
        "app_config.py",
        "settings_ui.py",
        "tray_app.py",
        "main.py",
        "make_icon.py",
        "runner.py",
        "device_identity.py",
        "account_store.py",
        "aigis_solver.py",
        "stoken_login.py",
        "scheduler.py",
        "autostart.py",
        "dpi.py",
        "theme.py",
        "widgets.py",
        "requirements.txt",
        "build.ps1",
    ]:
        src = TRAY / name
        if src.exists():
            (PUB / "TrayApp" / name).write_bytes(src.read_bytes())
    # assets used by runtime icon
    assets_src = TRAY / "assets"
    assets_dst = PUB / "TrayApp" / "assets"
    assets_dst.mkdir(parents=True, exist_ok=True)
    for p in assets_src.glob("icon.*"):
        shutil.copy2(p, assets_dst / p.name)

    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)
    run(
        [
            PY,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--onefile",
            "--noconsole",
            "--name",
            "MihoyoBBSAutoSigner",
            "--icon",
            str(assets_dst / "icon.ico"),
            "--add-data",
            f"{assets_dst};assets",
            "main.py",
        ],
        cwd=PUB / "TrayApp",
    )
    exe_src = PUB / "TrayApp" / "dist" / "MihoyoBBSAutoSigner.exe"
    if not exe_src.exists():
        # pyinstaller may write under publish TrayApp/dist
        candidates = list(PUB.rglob("MihoyoBBSAutoSigner.exe"))
        if not candidates:
            raise SystemExit("exe not found")
        exe_src = candidates[0]
    shutil.copy2(exe_src, DIST / "MihoyoBBSAutoSigner.exe")

    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)
    shutil.copy2(DIST / "MihoyoBBSAutoSigner.exe", STAGE / "MihoyoBBSAutoSigner.exe")
    # engine（不打包 config.yaml：含账号凭证会覆盖老用户配置，由程序首次运行自动生成）
    _SKIP = {"config.yaml", "tray_config.json", "tray.log"}
    shutil.copytree(
        PUB / "engine", STAGE / "engine",
        ignore=shutil.ignore_patterns(*_SKIP, "__pycache__", "*.pyc"),
    )
    # bundled python runtime（依赖内置，免装 Python）
    sys.path.insert(0, str(TRAY / "tools"))
    from bundle_runtime import bundle
    bundle(STAGE)
    # 嵌入式 Python 的 ._pth 若不含引擎目录，引擎 import 同级模块会失败（隔离模式）
    patch_runtime_pth(STAGE)
    # docs / licenses
    for name in ["README.md", "LICENSE", "SECURITY.md"]:
        shutil.copy2(PUB / name, STAGE / name)
    # example config already under engine
    # quick start file
    (STAGE / "使用说明.txt").write_text(
        "米游社自动签到器 MihoyoBBSAutoSigner\n"
        "====================================\n"
        "1. 首次运行会自动生成 engine\\MihoyoBBSTools\\config\\config.yaml\n"
        "2. 双击 MihoyoBBSAutoSigner.exe（已内置运行环境，无需安装 Python）\n"
        "3. 托盘图标 → 打开设置 → 「账号」页短信登录（云游戏凭证在签到时自动获取）\n"
        "4. 在「功能」页勾选游戏签到 / 社区打卡 / 云游戏签到 → 保存 → 立即签到\n"
        "\n说明：非官方工具，仅供个人学习自用；不会自动绕过图形验证。\n"
        "配置文件勿提交到公开仓库。\n",
        encoding="utf-8",
    )

    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    ZIP_PATH.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in STAGE.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(STAGE).as_posix())
    print("exe", DIST / "MihoyoBBSAutoSigner.exe", DIST.joinpath("MihoyoBBSAutoSigner.exe").stat().st_size)
    print("zip", ZIP_PATH, ZIP_PATH.stat().st_size)
    print("stage_files", sum(1 for p in STAGE.rglob("*") if p.is_file()))


if __name__ == "__main__":
    main()
