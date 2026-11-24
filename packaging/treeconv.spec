# PyInstaller spec for treeconv.  Build with:  python packaging/build.py
#
# Produces a one-folder bundle (fast start-up, no temp extraction):
#   Linux    dist/treeconv/treeconv             (console; GUI when double-clicked)
#   Windows  dist/treeconv/treeconv.exe         (console: TUI / command line)
#            dist/treeconv/treeconv-gui.exe     (windowed: GUI, used by shortcuts)
#   macOS    dist/treeconv.app                  (GUI from Finder, TUI from Terminal)
import os
import sys

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
sys.path.insert(0, ROOT)
from core import __version__  # noqa: E402

datas = [(os.path.join(ROOT, "assets", "icon-256.png"), "assets")]
binaries = []
hiddenimports = ["gui"]
for pkg in ("textual", "rich"):  # textual loads widgets, CSS and syntax data lazily
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [os.path.join(ROOT, "treeconv.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "IPython", "numpy", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)

icon = os.path.join(ROOT, "assets", "icon.ico" if sys.platform == "win32" else "icon.png")
target_arch = os.environ.get("TREECONV_TARGET_ARCH") or None

exes = [EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="treeconv",
    console=sys.platform != "darwin",   # macOS: windowed .app; stdio still works from Terminal
    icon=icon,
    target_arch=target_arch,
    upx=False,
)]
if sys.platform == "win32":
    exes.append(EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name="treeconv-gui",
        console=False,
        icon=icon,
        upx=False,
    ))

coll = COLLECT(*exes, a.binaries, a.datas, name="treeconv", upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="treeconv.app",
        icon=icon,
        bundle_identifier="io.github.yhhshb.treeconv",
        version=__version__,
        info_plist={
            "CFBundleName": "treeconv",
            "CFBundleDisplayName": "treeconv",
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "NSHumanReadableCopyright": "MIT License",
        },
    )
