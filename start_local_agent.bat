@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set AGENT_ONLY_MODE=1

echo ====================================================================
echo  [골라방 LIVE] 로컬 라이브 에이전트 단독 기동 (Port 5000 / HTTPS 5443)
echo  - 카메라/WebRTC/FFmpeg 인코딩은 현재 PC에서만 처리됩니다.
echo  - 호스팅 서버의 영상 트래픽은 0 Byte (0원)로 유지됩니다.
echo ====================================================================

netstat -ano | findstr ":5000 " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [확인] 이미 로컬 에이전트(포트 5000)가 실행 중입니다.
    timeout /t 2 /nobreak >nul
    exit /b 0
)

set "PY_EXE=python"
if exist "venv\Scripts\python.exe" (
    set "PY_EXE=venv\Scripts\python.exe"
) else if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
    set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
)

if "%~1"=="" (
    start "" cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:5000/overlay/studio"
)

"%PY_EXE%" live_commerce_server.py
pause
