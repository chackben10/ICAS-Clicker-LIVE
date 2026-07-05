# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('/Users/icas/Github/ICAS-Clicker-LIVE/Production_Hub_App/build/macos/remote_pages', 'remote_pages'), ('/Users/icas/Github/ICAS-Clicker-LIVE/Production_Hub_App/assets/ProductionHub.icns', 'assets')]
binaries = []
hiddenimports = ['uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on']
tmp_ret = collect_all('PySide6')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['/Users/icas/Github/ICAS-Clicker-LIVE/Production_Hub_App/main.py'],
    pathex=['/Users/icas/Github/ICAS-Clicker-LIVE/Production_Hub_App'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Production Hub',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['/Users/icas/Github/ICAS-Clicker-LIVE/Production_Hub_App/assets/ProductionHub.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Production Hub',
)
app = BUNDLE(
    coll,
    name='Production Hub.app',
    icon='/Users/icas/Github/ICAS-Clicker-LIVE/Production_Hub_App/assets/ProductionHub.icns',
    bundle_identifier='org.icas.productionhub',
)
