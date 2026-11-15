# PyInstaller spec: pyinstaller --noconfirm --clean packaging/viral_clipper.spec
# Produces dist/ViralClipper/ViralClipper.exe (Windows) or dist/ViralClipper/ViralClipper (Linux/macOS).
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    (os.path.join(ROOT, "app", "static"), os.path.join("app", "static")),
    (os.path.join(ROOT, "app", "assets"), os.path.join("app", "assets")),
]
binaries = []
hiddenimports = collect_submodules("app") + collect_submodules("uvicorn") + collect_submodules("yt_dlp")

# Packages that ship native libraries or data files PyInstaller can't discover on its own.
for pkg in ("ctranslate2", "faster_whisper", "onnxruntime", "av", "yt_dlp_ejs", "tokenizers"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

icon = os.path.join(SPECPATH, "icon.ico")

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "skimage", "scipy", "pandas", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ViralClipper",
    console=True,  # the console window shows the app's address; closing it quits the app
    icon=icon if os.path.exists(icon) else None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="ViralClipper", upx=False)
