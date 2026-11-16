@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "AGENT_BAT=%~dp0start_local_agent.bat"

echo ====================================================================
echo  [골라방 LIVE] 로컬 에이전트 원클릭 웹 실행 프로토콜(golabang-agent://) 등록
echo ====================================================================

reg add "HKCU\Software\Classes\golabang-agent" /ve /d "URL:Golabang Live Local Agent Protocol" /f >nul
reg add "HKCU\Software\Classes\golabang-agent" /v "URL Protocol" /d "" /f >nul
reg add "HKCU\Software\Classes\golabang-agent\DefaultIcon" /ve /d "cmd.exe,0" /f >nul
reg add "HKCU\Software\Classes\golabang-agent\shell\open\command" /ve /d "\"%AGENT_BAT%\" \"%%1\"" /f >nul

echo [완료] Windows 사용자 레지스트리에 golabang-agent:// 프로토콜이 등록되었습니다!
echo        이제 클라우드 스튜디오 웹페이지에서 [로컬 에이전트 원클릭 실행] 버튼으로 즉시 기동할 수 있습니다.
if "%~1"=="" pause
