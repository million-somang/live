@echo off
chcp 65001 > nul
title 웹방송 관제탑 (주소창 없는 전용 앱 창)

echo ========================================================
echo  📡 웹방송 관제탑 - 주소창 없는 전용 앱 창 실행 중...
echo ========================================================
echo.

set TARGET_URL=http://127.0.0.1:5000/broadcast?mode=window

:: 1. Google Chrome 시도
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --app="%TARGET_URL%" --start-maximized
    goto :done
)

if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
    start "" "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" --app="%TARGET_URL%" --start-maximized
    goto :done
)

:: 2. Microsoft Edge 시도
if exist "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" (
    start "" "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --app="%TARGET_URL%" --start-maximized
    goto :done
)

if exist "C:\Program Files\Microsoft\Edge\Application\msedge.exe" (
    start "" "C:\Program Files\Microsoft\Edge\Application\msedge.exe" --app="%TARGET_URL%" --start-maximized
    goto :done
)

:: 3. 기본 명령어 시도
start msedge --app="%TARGET_URL%" --start-maximized

:done
echo ✅ 웹방송 관제탑 전용 창이 화면 가득 실행되었습니다!
timeout /t 2 > nul
exit
