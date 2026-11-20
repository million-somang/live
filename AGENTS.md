# Repository Guidelines for AI Agents (`AGENTS.md`)

## Shopping Mall (`shopping_mall/`) UI & Design Rules
- `shopping_mall/` 하위의 HTML 템플릿, CSS, JS 동적 렌더링 UI를 수정하거나 새로운 페이지·기능을 추가할 때는 **반드시** 아래 문서를 먼저 읽고 엄격히 준수하세요:
  1. [`shopping_mall/DESIGN.md`](shopping_mall/DESIGN.md) — **골라방 LIVE 쇼핑몰 Astryx Neutral & Indigo 디자인 시스템 공식 명세**
  2. [`shopping_mall/static/css/astryx_mall.css`](shopping_mall/static/css/astryx_mall.css) — 공용 `--ax-*` 토큰 및 `.ax-*` 컴포넌트 클래스
  3. [`shopping_mall/AGENTS.md`](shopping_mall/AGENTS.md) — 쇼핑몰 작업 시 필수 준수 체크리스트
- 브랜드명은 **`골라방 LIVE`** (Indigo `G` 마크)로 통일하며, 구 브랜드명(`THE VINE LIVE MALL`)이나 구 테마(`mall_light_theme.css`), 대형 핑크/로즈 그라데이션 CTA를 사용하지 않습니다.
- 기존 DOM ID, SSE/CSR 동기화 훅(`data-csr-stock-num`, `data-csr-pin-slot`, `window.__mallApplyLiteState`), CSRF 토큰, 비회원 3중 검증 계약을 임의로 제거하거나 깨뜨리지 마세요.
