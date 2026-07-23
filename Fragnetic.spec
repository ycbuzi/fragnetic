# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('fragroute_ui.html', '.'), ('fragroute_lancers.json', '.'), ('fragroute_weapons.json', '.'), ('fragroute_cards.json', '.'), ('assets\\fragroute.ico', 'assets'), ('assets\\fragroute.png', 'assets'), ('ship_assets\\fragroute_icons.json', '.')]
binaries = []
hiddenimports = ['fragroute', 'fragroute_ai', 'fragroute_capture', 'fragroute_modes', 'fragroute_learning', 'fragroute_knowledge', 'fragroute_llm', 'fragroute_imagegen', 'fragroute_voice', 'fragroute_yolo', 'fragroute_dataset', 'fragroute_embed', 'fragroute_video', 'fragroute_setup', 'fragroute_license', 'fragroute_auth', 'fragroute_hardware', 'fragroute_tts', 'fragroute_persona', 'fragroute_audio', 'fragroute_regionlock', 'fragroute_proc', 'fragroute_procaudio', 'fragroute_wgc', 'pyaudiowpatch', 'maxminddb', 'fragroute_live', 'clr']
tmp_ret = collect_all('pyaudiowpatch')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('maxminddb')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pystray')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('onnxruntime')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('cryptography')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('PIL')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('webview')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('clr_loader')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['fragroute_app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'pandas', 'scipy', 'tkinter', 'pytest', 'IPython', 'notebook',
              # DEV-ONLY: torch/open_clip/transformers exist on this box solely to RE-EXPORT
              # clip_vitb32.onnx. The app infers with onnxruntime (kept) and never imports
              # torch -- without these excludes PyInstaller vacuums in torch\lib\*.dll and the
              # exe balloons ~72MB -> ~210MB of dead weight in a product we sell.
              'torch', 'torchvision', 'transformers', 'open_clip', 'open_clip_torch',
              'onnx', 'onnxscript', 'sympy', 'networkx'],
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
    name='Fragnetic',
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
    uac_admin=True,
    icon=['assets\\fragroute.ico'],
)
