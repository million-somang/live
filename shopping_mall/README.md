# 🛍️ 골라방 LIVE 독립 쇼핑몰 (Port 3000)

이 폴더(`shopping_mall`)는 OBS 라이브 커머스 스튜디오와 실시간 연동되면서도, **외부 인터넷 클라우드/웹 서버에 단독 배포할 수 있도록 완전히 분리된 독립 웹 애플리케이션**입니다.

## 1. 단독 실행 방법 (Port 3000)
```bash
cd shopping_mall
pip install -r requirements.txt
python app.py
```
- 스토어 메인: `http://localhost:3000`
- 실시간 라이브 커머스 전용관 (URL 고정 상세화면 자동전환): `http://localhost:3000/live`

## 2. 쇼핑몰 폴더만 별도 Git 저장소로 올리는 방법
`shopping_mall` 폴더 내부에서 바로 `git init`을 실행하거나 `init_mall_git.bat`을 실행하면 OBS 스튜디오 코드 없이 **오직 쇼핑몰 코드만** 별도의 Git 저장소로 관리·푸시할 수 있습니다.

```bash
cd shopping_mall
git init
git add .
git commit -m "Initial commit: standalone live commerce shopping mall"
git branch -M main
git remote add origin <쇼핑몰_전용_깃허브_저장소_URL>
git push -u origin main
```

## 3. 외부 서버 배포 시 환경변수 설정
- `MALL_PORT`: 기본값 `3000`
- `OBS_SERVER_URL`: OBS 스튜디오 서버 주소 (로컬 기본값: `http://127.0.0.1:5000`)
- OBS 스튜디오 서버에서는 환경변수 `MALL_SERVER_URL=https://내쇼핑몰도메인.com`을 설정하면 상품 등록/수정/선택(Pin) 시 외부 인터넷 쇼핑몰 서버의 `/api/sync/from-obs`로 실시간 푸시됩니다.
