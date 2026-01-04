# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import copy_metadata

datas = []
datas += copy_metadata('playwright')
datas += copy_metadata('tqdm')
datas += copy_metadata('aiohttp')

a = Analysis(
    ['src/pyhako_cli/cli.py'],
    pathex=['src'],
    binaries=[],
    datas=datas,
    hiddenimports=[
        'pyhako', 'pyhako.auth', 'pyhako.client', 'pyhako.utils', 
        'pyhako.manager', 'pyhako.credentials', 'pyhako.logging',
        'pyhako_cli', 'pyhako_cli.logging_setup', 'pyhako_cli.strings',
        'structlog', 'keyrings.alt'
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='pyhako-cli',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
