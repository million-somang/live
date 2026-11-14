@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================================
echo 🛍️ 쇼핑몰(shopping_mall) 단독 Git 저장소 초기화 스크립트
echo ========================================================
if not exist ".git" (
    git init
    git branch -M main
    echo ✅ shopping_mall 폴더 전용 독립 Git 저장소(.git)가 생성되었습니다.
) else (
    echo ℹ️ 이미 shopping_mall 전용 .git 저장소가 존재합니다.
)
git add .
git status
echo.
echo [안내] 아래 명령어로 쇼핑몰 전용 원격 Git 저장소에 단독 업로드할 수 있습니다:
echo   git commit -m "Update shopping mall"
echo   git remote add origin https://github.com/계정명/쇼핑몰저장소.git
echo   git push -u origin main
pause
