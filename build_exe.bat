@echo off
REM === Oasis 予測ツール exeビルド（Windows）===
cd /d "%~dp0"
echo [1/2] 依存とPyInstallerを導入...
python -m pip install -r requirements.txt pyinstaller
echo [2/2] ビルド中（数分かかります）...
python -m PyInstaller --noconfirm --clean oasis.spec
echo.
echo ★完成: dist\OasisYosou\OasisYosou.exe
echo    このフォルダ（dist\OasisYosou\）に OASISUTTI.txt を置いて exe を実行してください。
pause
