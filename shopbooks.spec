# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the ShopBooks.app bundle (built by build-mac.sh; see docs/standalone-app.md).
# Entry point is desktop.py: serve in-process, open the app-mode window, stop on close.
#
# datas: templates/ and static/ are bundled NEXT TO the code, so webutil.BASE and db.REPO_DIR
# (both `Path(__file__).parent`-relative) resolve unchanged inside the bundle. The books
# themselves live in ~/Library/Application Support/ShopBooks — never inside the .app.
from PyInstaller.utils.hooks import collect_data_files

datas = [("templates", "templates"), ("static", "static")]
# package data loaded at runtime that PyInstaller can't see statically:
datas += collect_data_files("pdfminer")    # cmap tables for PDF text extraction
datas += collect_data_files("anthropic")   # SDK data files (tokenizer etc.)

a = Analysis(
    ["desktop.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        # uvicorn resolves these dynamically from its Config defaults:
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ShopBooks",
    console=False,          # windowed: no Terminal appears on double-click
    upx=False,
)
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
