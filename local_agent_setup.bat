@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ====================================================================
echo  [골라방 LIVE] 방송자 PC 전용 로컬 라이브 에이전트 설치 마법사
echo ====================================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [오류] Python이 설치되어 있지 않거나 PATH에 없습니다. Python 3.11+ 설치 후 다시 실행해 주세요.
    pause
    exit /b 1
)

if not exist "venv\Scripts\python.exe" (
    echo [1/4] Python 가상환경(venv) 생성 중...
    python -m venv venv
) else (
    echo [1/4] 기존 Python 가상환경(venv) 확인됨.
)

echo [2/4] 필수 패키지(requirements.txt) 설치 중...
call venv\Scripts\python.exe -m pip install --upgrade pip
call venv\Scripts\pip.exe install -r requirements.txt

echo [3/4] 백그라운드 자동화용 Playwright Chromium 점검/설치 중...
call venv\Scripts\python.exe -m playwright install chromium

echo [4/4] FFmpeg 설치 여부 점검 중...
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo [안내] 시스템 PATH에서 ffmpeg.exe를 찾지 못했습니다.
    echo        winget으로 자동 설치를 시도합니다...
    winget install --id Gyan.FFmpeg -e --source winget --accept-package-agreements --accept-source-agreements
) else (
    echo [확인] FFmpeg 정상 감지됨.
)

if not exist ".env" (
    if exist ".env.example" copy /y ".env.example" ".env" >nul
)

echo [5/5] 웹 원클릭 실행 프로토콜(golabang-agent://) 등록 중...
call "%~dp0register_agent_protocol.bat" silent

echo.
echo ====================================================================
echo  설치가 완료되었습니다!
echo  - 단독 에이전트 실행: start_local_agent.bat (또는 웹에서 원클릭 실행)
echo  - 실행 후 상단 [클라우드몰 설정] 버튼에서 외부 쇼핑몰 주소와 시크릿 키를 입력하세요.
echo ====================================================================
pause
