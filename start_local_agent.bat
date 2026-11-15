@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set AGENT_ONLY_MODE=1

echo ====================================================================
echo  [골라방 LIVE] 로컬 라이브 에이전트 단독 기동 (Port 5000)
echo  - 카메라/WebRTC/FFmpeg 인코딩은 현재 PC에서만 처리됩니다.
echo  - 외부 클라우드 CSR 쇼핑몰과는 경량 JSON 상태/주문만 동기화됩니다.
echo ====================================================================

set "PY_EXE=python"
if exist "venv\Scripts\python.exe" (
    set "PY_EXE=venv\Scripts\python.exe"
) else if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
    set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
)

start "" cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:5000/obs-studio"
"%PY_EXE%" live_commerce_server.py
pause
