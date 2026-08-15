@echo off
rem ============================================================
rem  おあしすっち レース自動採取（Windows タスクスケジューラ用）
rem  1日1回、その日のレースを timeline ごと races.jsonl に追記する。
rem  トークンは不要。guild と user だけ。
rem
rem  ? 当日中に回すこと。翌日以降だとステータスが「今」の値に化ける。
rem  出力は harvest.log（.gitignore 済み）。
rem ============================================================
setlocal
set PY=C:\Users\hizik\AppData\Local\Python\pythoncore-3.14-64\python.exe
set REPO=C:\oasissfable\repo
set GUILD=1310885590094450739
set USER_ID=613283912105590784

cd /d "%REPO%" || exit /b 1
echo(>> harvest.log
echo ===== %DATE% %TIME% =====>> harvest.log
"%PY%" harvest_results.py --guild %GUILD% --user %USER_ID% --forward --count 30 >> harvest.log 2>&1
exit /b %ERRORLEVEL%
