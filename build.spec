# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import copy_metadata
import os
import platform
import tomllib

# Read version from pyproject.toml
with open('pyproject.toml', 'rb') as f:
    pyproject = tomllib.load(f)
    VERSION = pyproject['project']['version']

# Determine platform suffix
PLATFORM = 'windows' if platform.system() == 'Windows' else 'linux'
BINARY_NAME = f'saka-cli-{VERSION}-{PLATFORM}'

datas = []
datas += copy_metadata('playwright')
datas += copy_metadata('tqdm')
datas += copy_metadata('aiohttp')

project_dir = os.path.abspath(os.getcwd())

# Use local pysaka if available (dev), otherwise rely on installed package
pysaka_local = os.path.abspath(os.path.join(project_dir, '../pysaka/src'))
pathex = ['src']
if os.path.exists(pysaka_local):
    pathex.append(pysaka_local)

a = Analysis(
    ['src/saka_cli/cli.py'],
    pathex=pathex,
    binaries=[],
    datas=datas,
    hiddenimports=[
        'pysaka', 'pysaka.auth', 'pysaka.client', 'pysaka.utils', 
        'pysaka.manager', 'pysaka.credentials', 'pysaka.logging',
        'saka_cli', 'saka_cli.logging_setup', 'saka_cli.strings',
        'structlog', 'keyrings.alt', 'playwright.__main__'
    ],
    hookspath=['.'],
    hooksconfig={},
    runtime_hooks=['scripts/rthook_encoding.py'],
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
    name=BINARY_NAME,
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
