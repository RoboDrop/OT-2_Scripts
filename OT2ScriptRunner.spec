# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['windows_script_runner.py'],
    pathex=[],
    binaries=[],
    datas=[('apply_standard_offsets.py', '.'), ('ot2_ensure_ssh_key.py', '.'), ('ot2_pull_calibrations.py', '.'), ('ot2_resolve_host.py', '.'), ('pull_rpi_offsets.py', '.'), ('offsets', 'offsets'), ('definitions', 'definitions')],
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
    a.binaries,
    a.datas,
    [],
    name='OT2ScriptRunner',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
