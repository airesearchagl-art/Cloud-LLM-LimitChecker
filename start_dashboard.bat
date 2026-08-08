@echo off
setlocal

cd /d "%~dp0"

echo ==========================================
echo Cloud LLM Limit Checker を起動します
echo ==========================================
echo.

powershell -NoProfile -WindowStyle Hidden -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8001/api/health' -TimeoutSec 2; $json = $r.Content | ConvertFrom-Json; if ($r.StatusCode -eq 200 -and $json.status -eq 'ok') { exit 0 } else { exit 1 } } catch { exit 1 }"
if %ERRORLEVEL% EQU 0 (
    echo Cloud LLM Limit Checker はすでに起動しています。
    echo ブラウザでダッシュボードを開きます: http://127.0.0.1:8001
    start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Process 'http://127.0.0.1:8001'"
    exit /b 0
)

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv が見つかりません。
    echo.
    echo 初回セットアップを実行してください（README.md の Setup / Windows Standard Commands を参照）:
    echo   python -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [WARNING] .env が見つかりません。
    echo 必要に応じて .env.example から作成してください。
    echo.
)

echo URL: http://127.0.0.1:8001
echo 停止: このウィンドウで Ctrl+C を押してください
echo サーバー起動に失敗した場合は、下に表示されるログを確認してください。
echo.

start "" powershell -NoProfile -WindowStyle Hidden -Command "$deadline = (Get-Date).AddSeconds(20); while ((Get-Date) -lt $deadline) { try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8001/api/health' -TimeoutSec 2; if ($r.StatusCode -eq 200) { break } } catch {}; Start-Sleep -Milliseconds 500 }; Start-Process 'http://127.0.0.1:8001'"

".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8001

echo.
echo Server stopped.
pause
