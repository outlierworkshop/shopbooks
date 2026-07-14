# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the standalone ShopBooks build. Cross-platform:
#   macOS   -> dist/ShopBooks.app   (built by build-mac.sh; icon build/ShopBooks.icns)
#   Windows -> dist/ShopBooks/       (onedir; ShopBooks.exe; icon build/ShopBooks.ico)
#             then wrapped into ShopBooks-Setup.exe by installer.iss (Inno Setup, in CI).
# Entry point is desktop.py: serve in-process, open the app-mode window, stop on close.
#
# datas/hiddenimports are single-sourced below so the two platforms never drift. templates/
# and static/ are bundled NEXT TO the code, so webutil.BASE and db.REPO_DIR (both
# `Path(__file__).parent`-relative) resolve unchanged inside the bundle. The books themselves
# live in the per-OS data dir (~/Library/Application Support/ShopBooks on mac,
# %USERPROFILE%\ShopBooks on Windows) — never inside the bundle, so updates never touch them.
import sys

from PyInstaller.utils.hooks import collect_data_files

datas = [("templates", "templates"), ("static", "static"), ("docs", "docs")]
# docs/ is bundled because the in-app Help menu (helpdocs.py) renders docs/*.md at runtime
# (DOCS_DIR = Path(__file__).parent / "docs"); without it the Help guides 404 in the bundle.
# package data loaded at runtime that PyInstaller can't see statically:
datas += collect_data_files("pdfminer")    # cmap tables for PDF text extraction
datas += collect_data_files("anthropic")   # SDK data files (tokenizer etc.)

hiddenimports = [
    # uvicorn resolves these dynamically from its Config defaults:
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

a = Analysis(
    ["desktop.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ShopBooks",
              console=False, upx=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="ShopBooks", upx=False)
    app = BUNDLE(
        coll,
        name="ShopBooks.app",
        icon="build/ShopBooks.icns",
        bundle_identifier="com.outlierworkshop.shopbooks",
        info_plist={
            "CFBundleName": "ShopBooks",
            "CFBundleDisplayName": "ShopBooks",
            "CFBundleShortVersionString": "1.0.0",
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.finance",
        },
    )
elif sys.platform == "win32":
    # Windowed EXE (no console window on launch); build/ShopBooks.ico is generated from
    # static/app-icon.png by the build-windows workflow before PyInstaller runs.
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ShopBooks",
              console=False, upx=False, icon="build/ShopBooks.ico")
    coll = COLLECT(exe, a.binaries, a.datas, name="ShopBooks", upx=False)
else:  # linux / other: plain onedir, no platform icon
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ShopBooks",
              console=False, upx=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="ShopBooks", upx=False)
