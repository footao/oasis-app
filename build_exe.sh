#!/bin/bash
# === Oasis 予測ツール exe/実行ファイル ビルド（macOS / Linux）===
cd "$(dirname "$0")"
echo "[1/2] 依存とPyInstallerを導入..."
python3 -m pip install -r requirements.txt pyinstaller
echo "[2/2] ビルド中（数分かかります）..."
python3 -m PyInstaller --noconfirm --clean oasis.spec
echo ""
echo "★完成: dist/OasisYosou/OasisYosou"
echo "   このフォルダ（dist/OasisYosou/）に OASISUTTI.txt を置いて実行してください。"
