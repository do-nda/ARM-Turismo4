# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['gt4_recomp_gui.py'],
    pathex=[],
    binaries=[],
    datas=[('tools', 'tools'), ('runtime', 'runtime'), ('tests', 'tests'), ('android', 'android'), ('r5900lift.py', '.'), ('gt4_recomp_apk.py', '.'), ('blobs.txt', '.'), ('missing_entries.txt', '.')],
    hiddenimports=[],
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
    name='gt4-recomp-gui',
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
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='gt4-recomp-gui',
)
