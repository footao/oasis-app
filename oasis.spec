# -*- mode: python ; coding: utf-8 -*-
"""
oasis.spec — PyInstaller ビルド設定（onedir / フォルダ配布・安定重視）。
ビルド:  python -m PyInstaller --noconfirm --clean oasis.spec
出力  :  dist/OasisYosou/OasisYosou(.exe)
※ exeはビルドしたOS専用（Windowsで作ればWindows用、Macで作ればMac用）。
※ PyInstaller 6 系を想定。
"""
from PyInstaller.utils.hooks import collect_all, copy_metadata

datas, binaries, hiddenimports = [], [], []

# --- Streamlit本体（データ・隠しimport・バイナリ）を丸ごと収集 ---
for pkg in ("streamlit",):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# --- バージョンをメタデータから読む依存（importlib.metadata 対策）---
for pkg in ("streamlit", "pandas", "numpy", "scikit-learn", "scipy",
            "altair", "pyarrow", "packaging", "requests", "joblib", "threadpoolctl"):
    try:
        datas += copy_metadata(pkg)
    except Exception:
        pass

# --- アプリ本体・コアを同梱（実行時 _MEIPASS 直下に配置）---
datas += [("oasis_app.py", "."), ("oasis_core.py", ".")]

# --- 不足しがちな隠しimport ---
hiddenimports += [
    "sklearn.ensemble", "sklearn.tree", "sklearn.tree._utils",
    "sklearn.utils._typedefs", "sklearn.utils._heap",
    "sklearn.neighbors._partition_nodes",
    "pandas._libs.tslibs.base",
    "streamlit.runtime.scriptrunner.magic_funcs",
]

a = Analysis(
    ["run_app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name="OasisYosou",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # ローカルサーバのログ表示用。閉じるとアプリ停止。
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="OasisYosou",
)
