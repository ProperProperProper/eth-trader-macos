# -*- mode: python ; coding: utf-8 -*-
# macOS build — mirrors unified_combo_trader.spec (the Windows spec) but adds a BUNDLE()
# to produce a real .app instead of a bare onedir folder, and drops the upx=True/winget
# requirement (UPX is a Windows/Linux-oriented compressor here; skipping it just means a
# quietly larger, uncompressed app — same tradeoff PyInstaller silently falls back to on
# Windows if upx isn't on PATH).
from PyInstaller.utils.hooks import collect_all, collect_data_files

pybit_d, pybit_b, pybit_h = collect_all('pybit')
ws_d,   ws_b,   ws_h   = collect_all('websocket')
cert_d = collect_data_files('certifi')

numba_d, numba_b, numba_h = collect_all('numba')
llvmlite_d, llvmlite_b, llvmlite_h = collect_all('llvmlite')

ws_h = [m for m in ws_h if not m.startswith('websocket.tests')]
ws_d = [d for d in ws_d if 'websocket/tests' not in d[0].replace('\\', '/')]

EXCLUDES = [
    'numpy.f2py', 'numpy.distutils', 'numpy.testing', 'numpy.array_api', 'numpy.tests',
    'pandas.tests',
    'tkinter', 'matplotlib', 'IPython', 'pytest',
    'torch', 'torchvision', 'torchaudio', 'scipy', 'lxml', 'sklearn',
    'jax', 'sympy', 'networkx', 'tqdm', 'fsspec', 'triton',
]

# README.md (the thesis, shown in the app's own Readme tab) and docs/SETUP.md (the
# Setup tab) — added 2026-09-04, explicit user ask. Destination dirs ('.', 'docs') are
# relative to the bundle root, the same place _DIR (eth_trader.py's own frozen-app
# path base) resolves to for a onedir/BUNDLE build — see _load_bundled_text.
docs_d = [('README.md', '.'), ('docs/SETUP.md', 'docs')]

a = Analysis(
    ['eth_trader.py'],
    pathex=['.'],
    binaries=pybit_b + ws_b + numba_b + llvmlite_b,
    datas=pybit_d + ws_d + cert_d + numba_d + llvmlite_d + docs_d,
    hiddenimports=pybit_h + ws_h + numba_h + llvmlite_h +
                  ['PyQt6.sip', 'numpy', 'pandas', 'multiprocessing', 'concurrent.futures'],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)

# Same 'unified_combo_trader_grid' name as the Windows spec, deliberately — this Grid
# fork's own image name must stay distinct from the source unified_combo_gui repo's
# 'unified_combo_trader', not from itself across platforms. Keep in sync with the
# Windows spec's own naming-rationale comment if either is ever renamed.
exe = EXE(
    pyz, a.scripts,
    exclude_binaries=True,
    name='unified_combo_trader_grid',
    debug=False, bootloader_ignore_signals=False, strip=False,
    upx=False, console=False,
)
coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False,
    name='unified_combo_trader_grid',
)
app = BUNDLE(
    # User-facing product name, renamed 2026-09-04 (explicit user ask, "Rename this bot
    # to ETH TRADER") — independent of the exe/coll `name` above, which stays
    # 'unified_combo_trader_grid' deliberately (see that block's own comment: a shared
    # process image name with the sibling Windows unified_combo_gui repo would let a
    # kill-by-image-name build step in either repo accidentally kill the other's live
    # trading process — that risk is about the internal exe name, not this Finder/Dock
    # display name, so only this BUNDLE() name/bundle_identifier/CFBundleName changed).
    coll,
    name='ETH Trader.app',
    icon=None,
    bundle_identifier='com.local.ethtrader',
    info_plist={
        'NSHighResolutionCapable': True,
        'LSBackgroundOnly': False,
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleName': 'ETH Trader',
        'CFBundleDisplayName': 'ETH Trader',
    },
)
