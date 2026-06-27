# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('icons', 'icons'), ('Runeshape_Combinations.json', '.'), ('tessdata', 'tessdata')]
binaries = []
hiddenimports = ['settings_window', 'overlay', 'scan_engine', 'ocr_scanner', 'ru_translator', 'price_repository', 'screen_capture', 'calibration', 'config', 'pynput.keyboard._win32', 'pynput.mouse._win32', 'mss.windows', 'rapidfuzz.distance.Levenshtein']
tmp_ret = collect_all('tesserocr')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['settings_window.py'],
    pathex=[],
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
    name='RuneshapeChecker',
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
    icon=['icons\\logo.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='RuneshapeChecker',
)
