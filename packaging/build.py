#!/usr/bin/env python3
"""Build treeconv's release packages for the current platform.

    pip install -r packaging/requirements.txt
    python packaging/build.py            # build + package + smoke test
    python packaging/build.py build      # PyInstaller only        -> dist/
    python packaging/build.py package    # archives / installers   -> dist/release/
    python packaging/build.py smoke      # run the built program on a tiny folder tree
    python packaging/build.py smoke --source   # same, but run treeconv.py from the source tree

Outputs (dist/release/):
    Linux    treeconv-<ver>-linux-<arch>.AppImage, treeconv-<ver>-linux-<arch>.tar.gz
    Windows  treeconv-<ver>-windows-<arch>-setup.exe (needs Inno Setup), treeconv-<ver>-windows-<arch>.zip
    macOS    treeconv-<ver>-macos-<arch>.dmg

The GitHub workflow (.github/workflows/release.yml) runs exactly these steps.
"""
from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
RELEASE = DIST / "release"
sys.path.insert(0, str(ROOT))
from core import __version__ as VERSION  # noqa: E402

APPIMAGETOOL_URL = ("https://github.com/AppImage/appimagetool/releases/download/continuous/"
                    "appimagetool-{arch}.AppImage")


def log(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def run(cmd, **kw) -> None:
    print("   $", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def os_name() -> str:
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def arch() -> str:
    target = os.environ.get("TREECONV_TARGET_ARCH")
    m = (target or platform.machine()).lower()
    return {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64",
            "universal2": "universal"}.get(m, m)


def stem() -> str:
    return f"treeconv-{VERSION}-{os_name()}-{arch()}"


def executable() -> Path:
    if sys.platform == "darwin":
        return DIST / "treeconv.app" / "Contents" / "MacOS" / "treeconv"
    return DIST / "treeconv" / ("treeconv.exe" if sys.platform == "win32" else "treeconv")


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #

def build() -> None:
    log(f"Building treeconv {VERSION} for {os_name()}-{arch()}")
    for p in (DIST / "treeconv", DIST / "treeconv.app", BUILD / "pyinstaller"):
        shutil.rmtree(p, ignore_errors=True)
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", DIST, "--workpath", BUILD / "pyinstaller",
         ROOT / "packaging" / "treeconv.spec"])
    log(f"Built {executable()}")


# --------------------------------------------------------------------------- #
# package
# --------------------------------------------------------------------------- #

def package() -> None:
    RELEASE.mkdir(parents=True, exist_ok=True)
    {"linux": package_linux, "windows": package_windows, "macos": package_macos}[os_name()]()
    log("Release files:")
    for f in sorted(RELEASE.iterdir()):
        print(f"   {f.name}  ({f.stat().st_size / 1e6:.1f} MB)")


def package_linux() -> None:
    src = DIST / "treeconv"
    tgz = RELEASE / f"{stem()}.tar.gz"
    log(f"Creating {tgz.name}")
    with tarfile.open(tgz, "w:gz") as tf:
        tf.add(src, arcname=stem())

    log("Creating AppImage")
    appdir = BUILD / "AppDir"
    shutil.rmtree(appdir, ignore_errors=True)
    shutil.copytree(src, appdir / "usr" / "lib" / "treeconv", symlinks=True)
    apprun = appdir / "AppRun"
    apprun.write_text('#!/bin/sh\nHERE="$(dirname "$(readlink -f "$0")")"\n'
                      'exec "$HERE/usr/lib/treeconv/treeconv" "$@"\n')
    apprun.chmod(0o755)
    (appdir / "treeconv.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=treeconv\n"
        "Comment=Batch-convert videos while keeping the folder structure\n"
        "Exec=treeconv\nIcon=treeconv\nTerminal=false\nCategories=AudioVideo;Video;\n"
        "StartupWMClass=Treeconv\n")
    shutil.copy(ROOT / "assets" / "icon-256.png", appdir / "treeconv.png")
    shutil.copy(ROOT / "assets" / "icon-256.png", appdir / ".DirIcon")

    machine = {"x64": "x86_64", "arm64": "aarch64"}[arch()]
    tool = Path(os.environ.get("APPIMAGETOOL", BUILD / f"appimagetool-{machine}.AppImage"))
    if not tool.exists():
        log(f"Downloading {tool.name}")
        urllib.request.urlretrieve(APPIMAGETOOL_URL.format(arch=machine), tool)
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    out = RELEASE / f"{stem()}.AppImage"
    env = dict(os.environ, ARCH=machine, APPIMAGE_EXTRACT_AND_RUN="1")  # no FUSE needed on CI
    run([tool, "--no-appstream", appdir, out], env=env)


def package_windows() -> None:
    src = DIST / "treeconv"
    zp = RELEASE / f"{stem()}.zip"
    log(f"Creating {zp.name}")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(src.rglob("*")):
            zf.write(f, Path(stem()) / f.relative_to(src))

    iscc = os.environ.get("ISCC") or shutil.which("iscc")
    for guess in (r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe", r"C:\Program Files\Inno Setup 6\ISCC.exe"):
        if not iscc and Path(guess).exists():
            iscc = guess
    if not iscc:
        msg = "Inno Setup (ISCC.exe) not found: skipping the installer"
        if os.environ.get("CI"):
            sys.exit(msg)
        log(msg)
        return
    log("Creating installer")
    run([iscc, f"/DAppVersion={VERSION}", f"/DSourceDir={src}", f"/DOutputDir={RELEASE}",
         f"/DOutputName={stem()}-setup", ROOT / "packaging" / "windows" / "treeconv.iss"])


def package_macos() -> None:
    app = DIST / "treeconv.app"
    dmg = RELEASE / f"{stem()}.dmg"
    log(f"Creating {dmg.name}")
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "treeconv"
        stage.mkdir()
        run(["ditto", app, stage / "treeconv.app"])      # keeps signatures and symlinks
        (stage / "Applications").symlink_to("/Applications")
        dmg.unlink(missing_ok=True)
        run(["hdiutil", "create", "-volname", f"treeconv {VERSION}", "-srcfolder", stage,
             "-fs", "HFS+", "-format", "UDZO", "-ov", dmg])


# --------------------------------------------------------------------------- #
# smoke test
# --------------------------------------------------------------------------- #

def smoke(source: bool = False) -> None:
    exe = [sys.executable, str(ROOT / "treeconv.py")] if source else [str(executable())]
    log(f"Smoke test: {' '.join(exe)}")
    out = subprocess.run([*exe, "--version"], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == f"treeconv {VERSION}", out.stdout
    print("   " + out.stdout.strip())
    if not shutil.which("ffmpeg"):
        log("ffmpeg not on PATH: skipping the conversion test")
        return
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        (t / "in" / "Show" / "S1").mkdir(parents=True)
        (t / "in" / "Show" / "Empty").mkdir()
        run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=d=2:s=320x240",
             "-f", "lavfi", "-i", "sine=d=2", "-c:v", "mpeg4", "-c:a", "aac",
             t / "in" / "Show" / "S1" / "ep1.mkv"])
        (t / "in" / "Show" / "S1" / "ep1.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        run([*exe, "-i", t / "in" / "Show", "-o", t / "out", "--preset", "ultrafast"])
        for rel in ("Show/S1/ep1.mkv", "Show/S1/ep1.srt", "Show/Empty"):
            assert (t / "out" / rel).exists(), f"missing output: {rel}"
    log("Smoke test passed")


def main() -> None:
    args = sys.argv[1:]
    source = "--source" in args
    steps = [a for a in args if not a.startswith("--")] or ["build", "package", "smoke"]
    for step in steps:
        if step == "smoke":
            smoke(source)
        else:
            {"build": build, "package": package}[step]()


if __name__ == "__main__":
    main()
