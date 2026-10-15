# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_dynamic_libs

datas = [('ui/brand.svg', 'ui'), ('ui/bento_style.qss', 'ui'), ('yolov8n.pt', '.'), ('fuckensound.wav', '.'), ('data/models/face_landmarker.task', 'data/models')]
binaries = []
datas += collect_data_files('mediapipe')
datas += collect_data_files('ultralytics')
binaries += collect_dynamic_libs('mediapipe')

# Меньше вес: strip символов + без отладочных модулей.
# scipy/matplotlib/pandas НЕ исключаем — нужны EEG-фильтрам и отчётам.


a = Analysis(
    ['main_gui.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['brainflow', 'torch.testing',
              'tkinter', 'unittest', 'test', 'pydoc', 'doctest'],
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
    name='BaqylauStudent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
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
