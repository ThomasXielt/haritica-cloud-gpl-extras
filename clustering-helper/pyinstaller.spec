# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for haritica-clustering-helper.

Build with:
    pyinstaller --clean pyinstaller.spec --noconfirm

Output:
    dist/haritica-clustering-helper/        - onedir bundle
      haritica-clustering-helper.exe        - launcher
      _internal/                            - native DLLs + python311.dll

v1.0.4 — upx disabled on every platform. UPX corrupts macOS Mach-O
shared libs and invalidates the ad-hoc code signature PyInstaller
applies (mandatory on Apple Silicon — an unsigned arm64 binary is
SIGKILLed by the kernel), and trips Windows Defender heuristics on the
unsigned PyInstaller tree. Mirrors server/haritica-server.spec, which
already gates UPX to Windows-only for the same reason. This is the
release that publishes onedir .zip bundles for all four platforms
(Windows, macOS arm64, macOS x86_64, Linux x86_64) rather than
Windows-only.

v1.0.3 — switched from --onefile to --onedir packaging. The onefile
bootloader re-extracts ~240 MB of bundled libs into %TEMP%\_MEI* on
every invocation (30s cold start), making the binary unusable for
status checks (10s timeout) and clustering invocations (the SC wizard
calls the helper on every clustering action). With --onedir the .exe
sits next to its _internal/ tree and starts in <1s.

v1.0.2 — dropped scanpy/numba/llvmlite dep chain (see helper.py docstring).
Direct deps now: anndata, sklearn (sklearn.neighbors only), igraph,
leidenalg, louvain, h5py (for anndata's H5AD reader), numpy, scipy.
"""

import os
import platform as _platform
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs

block_cipher = None

ROOT = Path(SPECPATH)

# h5py's native HDF5 DLLs and igraph's C-core DLLs (igraph.dll +
# transitive glpk/gmp) live in CONDA_PREFIX/Library/bin under conda.
# PyInstaller's stock hooks don't walk that conda-specific layout, so
# without explicit bundling the .exe import-errors with
# "DLL load failed while importing _errors" (h5py) or
# "DLL load failed while importing _igraph" (igraph).
_extra_binaries = list(collect_dynamic_libs('h5py'))
_conda_prefix = os.environ.get('CONDA_PREFIX', '')
if _conda_prefix and _platform.system() == 'Windows':
    _libbin = Path(_conda_prefix) / 'Library' / 'bin'
    if _libbin.is_dir():
        for _dll in _libbin.glob('hdf5*.dll'):
            _extra_binaries.append((str(_dll), '.'))
        for _name in (
            # HDF5 transitive
            'zlib.dll', 'zlib1.dll', 'szip.dll', 'libszip.dll',
            # igraph C core + its solvers + linalg
            'igraph.dll',
            'glpk.dll', 'glpk_5_0.dll',
            'libgmp-10.dll', 'gmp.dll',
            'libxml2.dll', 'libxml2-2.dll',
            'libblas.dll', 'libcblas.dll', 'liblapack.dll',
            'vcomp140.dll',  # OpenMP, hard dep of igraph.dll
            # leidenalg's separate C lib (not in the .pyd, lives in Library/bin)
            'libleidenalg.dll',
        ):
            _candidate = _libbin / _name
            if _candidate.is_file():
                _extra_binaries.append((str(_candidate), '.'))

a = Analysis(
    ['helper.py'],
    pathex=[str(ROOT)],
    binaries=_extra_binaries,
    datas=[],
    hiddenimports=[
        'anndata',
        'h5py',
        'h5py.defs',
        'h5py.utils',
        'h5py._proxy',
        'h5py.h5ac',
        'sklearn.neighbors',
        'sklearn.neighbors._partition_nodes',
        'sklearn.utils._cython_blas',
        'sklearn.utils._typedefs',
        'sklearn.utils._heap',
        'sklearn.utils._sorting',
        'sklearn.utils._vector_sentinel',
        'leidenalg',
        'louvain',
        'igraph',
        'numpy',
        'pandas',
        'scipy',
        'scipy.sparse',
        'scipy.sparse.csgraph',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
        # Explicitly exclude the dropped scanpy/numba/llvmlite chain so a
        # stray transitive import (e.g., sklearn pulling in joblib which
        # tries numba on some paths) doesn't drag them back in.
        'scanpy',
        'numba',
        'llvmlite',
        'pynndescent',
        'umap',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# --onedir packaging: EXE is just the launcher; COLLECT bundles the
# launcher with binaries/datas/zipfiles into dist/haritica-clustering-helper/.
# Pre-v1.0.3 this was a single EXE block with binaries=a.binaries inlined
# (--onefile), which forced the bootloader to extract the entire bundle
# to %TEMP%\_MEI* on every invocation — 30s cold start, called per-status-
# check and per-clustering action. --onedir extracts once at install time
# and runs the launcher directly each call.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='haritica-clustering-helper',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX disabled: it corrupts macOS Mach-O segments and invalidates the
    # ad-hoc code signature (mandatory on Apple Silicon), and trips Windows
    # Defender on the unsigned tree. See the v1.0.4 docstring note and
    # server/haritica-server.spec (Windows-only UPX convention).
    upx=False,
    upx_exclude=[],
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,  # see EXE block — UPX corrupts macOS Mach-O / signatures
    upx_exclude=[],
    name='haritica-clustering-helper',
)
