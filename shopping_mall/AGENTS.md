# Shopping Mall (`shopping_mall/`) Development Rules (`AGENTS.md`)

> 이 파일은 `shopping_mall/` 폴더가 단독 Git 저장소로 분리될 때도 함께 유지되는 개발·디자인 규칙 파일입니다.

## 1. 디자인 일관성 필수 준수
새로운 기능, 탭, 모달, 폼, 배지 또는 페이지를 추가·수정할 때는 **작업 시작 전에 반드시 [`DESIGN.md`](DESIGN.md)를 먼저 읽고** 아래 규칙을 따르세요:

1. **단일 CSS 시스템 사용**: 모든 스타일은 [`static/css/astryx_mall.css`](static/css/astryx_mall.css)의 `--ax-*` 토큰과 `.ax-*` 컴포넌트 클래스를 재사용합니다. 템플릿 내부에 임의 색상(`#e11d48` 그라데이션 등)이나 중복 레이아웃 `<style>`을 새로 하드코딩하지 마세요.
2. **컬러 계층 (Astryx Neutral & Indigo)**:
   - 배경 `#f8fafc`, 카드 `#ffffff`, 보더 `#e2e8f0`, 주요 CTA/활성 상태 `#4f46e5`(Indigo), 보조 다크 버튼 `#0f172a`(Charcoal).
   - Rose(`#e11d48`)는 LIVE 펄싱 점, 할인율(`%`), 품절/오류/환불 배지에만 제한적으로 사용합니다.
3. **브랜드 & 아이콘 & 숫자 정렬**:
   - 브랜드 표기는 항상 **`골라방 LIVE`** 로 통일합니다.
   - 장식 이모지 대신 stroke SVG 아이콘(`.ax-icon`, `.ax-icon-sm`)을 사용하고, 금액·수량·재고·일시에 `.ax-num`, 번호/코드에 `.ax-code-chip`을 적용합니다.
4. **반응형 & 헤더 규칙**:
   - 모든 페이지 컨테이너는 `.ax-container` (`1320px`) 정렬을 유지합니다.
   - 상단 1단 슬림 헤더에 카테고리나 2차 필터를 추가하지 말고, 필요한 필터는 본문 툴바의 `.ax-segmented-bar`에 배치합니다.
   - 모바일(`<= 768px`)에서 2단 레이아웃이 1열 스택으로 전환되며 가로 오버플로우가 없는지 검증합니다.

## 2. 기능·보안·무에러 템플릿 규칙
- 모든 템플릿은 `{% extends "base.html" %}`를 상속합니다.
- POST 폼은 `<input type="hidden" name="csrf_token" value="{{ csrf_token }}">`을 유지합니다.
- 주문 품목 목록 순회 시 `ord.order_items if ord.order_items is defined else ord['items']` 패턴을 유지합니다.
- `/live` 템플릿에는 서버 영상 스트림(`/api/camera/stream`)을 절대 포함하지 않습니다.
- **일반 스토어 ↔ `/live` UI·가격 분리**: 일반 스토어 페이지(`/`, `/product/<num>`, `/cart`, `/checkout`, `/orders`, `/auth/*`)에는 상단 헤더의 `라이브 커머스` 메뉴 버튼 외에 다른 라이브·OBS 문구·배너·배지·버튼을 추가하지 마세요. 일반 스토어는 항상 일반 판매가(`price`)를 표시·결제하고, `/live` 진입 시에만 라이브 특가(`live_price`)를 적용합니다.
- **IDE 파서 무에러 원칙**: HTML `style="..."`, `<script>`, `onclick`/`onchange`, 태그 내부(`<option ... {% if ... %}>`)에 Jinja2 표현식을 직접 섞지 마세요. 조건부 스타일은 CSS 클래스(`.ax-is-hidden` 등)로, JS 데이터 전달은 `hidden data-*` (`| tojson | forceescape`) + `JSON.parse(el.dataset.*)`로 처리하세요.
- UI 변경 후에는 정적 HTML/CSS/JS 구문 검사, Flask 렌더링 테스트(HTTP 200, DOM ID 보존, 태그 균형), Playwright 데스크톱/모바일 뷰포트 검증을 통과한 뒤 작업을 완료하세요.
