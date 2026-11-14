@echo off
chcp 65001 > nul
title 라이브 커머스 실시간 AI 주문 및 자막 서버

echo ========================================================
echo   라이브 커머스 실시간 AI 주문 접수 & OBS 자막 서버
echo ========================================================
echo.

:: 1. 파이썬 경로 탐색 (방화벽 인바운드 허용된 기본 Python313 최우선 -> 가상환경 -> PATH 순서)
set "PYTHON_EXE=python"
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
    set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
) else if exist "%~dp0venv\Scripts\python.exe" (
    set "PYTHON_EXE=%~dp0venv\Scripts\python.exe"
) else (
    where python >nul 2>nul
    if %errorlevel% neq 0 (
        echo [오류] Python이 설치되어 있지 않거나 환경 변수 PATH에 등록되지 않았습니다.
        echo Python 3.10 이상을 설치하신 후 다시 시도해 주세요.
        pause
        exit /b
    )
)

:: 2. 필수 라이브러리 확인 및 설치
echo [1/2] 필수 라이브러리 점검 중...
"%PYTHON_EXE%" -m pip install -r requirements.txt >nul 2>nul

:: 3. 서버 실행
echo [2/2] 라이브 커머스 서버 구동 시작!
echo.
echo * 웹훅 수신 주소: http://127.0.0.1:5000/chat
echo * 상태 확인 주소: http://127.0.0.1:5000/status
echo.
echo * Social Stream Ninja 확장 프로그램 Webhook URL에 위 주소를 등록하세요.
echo * OBS Studio 텍스트 소스에서 obs_ai_reply.txt 파일을 연결하세요.
echo.
echo 서버를 종료하려면 이 창에서 Ctrl+C 를 누르세요.
echo ========================================================
echo.

"%PYTHON_EXE%" live_commerce_server.py

pause
