# 골라방 LIVE 쇼핑몰 디자인 시스템 (`DESIGN.md`)

> **적용 범위**: `shopping_mall/` 독립 쇼핑몰의 모든 페이지, 컴포넌트, 모달, 동적 JS 렌더링 UI.  
> **기반 시스템**: Astryx Enterprise Design System — **Neutral & Indigo** (Pure Vanilla HTML + CSS Custom Properties)  
> **단일 진실 원천(SSOT)**: [`static/css/astryx_mall.css`](static/css/astryx_mall.css), [`templates/base.html`](templates/base.html)

---

## 1. 핵심 디자인 원칙 (반드시 준수)

1. **브랜드 표기 통일**: 모든 타이틀, 로고, 배지, 푸터는 **`골라방 LIVE`** (Indigo 스퀘어 `G` 로고 마크)를 사용합니다. (`THE VINE LIVE MALL` / `V` 표기 금지)
2. **순수 Astryx Neutral & Indigo 톤 유지**:
   - 화면은 밝은 Slate 캔버스(`#f8fafc`) 위에 순백 카드(`#ffffff`)와 1px 보더(`#e2e8f0`)로 정돈합니다.
   - 주 브랜드·활성 탭·주요 구매/완료 CTA는 **Indigo(`#4f46e5`)**와 **Charcoal(`#0f172a`)**을 사용합니다.
   - **Rose(`#e11d48`) 사용 엄격 제한**: `LIVE` 상태 펄싱 점(dot), 할인율(`%`), 품절/오류/환불 배지 등 최소 시맨틱 표시만 허용합니다. 대형 핑크/로즈 그라데이션 배너나 버튼은 금지합니다.
3. **인라인 하드코딩 금지**: 색상·여백·모서리·그림자를 `style="..."`에 임의 하드코딩하지 말고 [`static/css/astryx_mall.css`](static/css/astryx_mall.css)의 `--ax-*` 토큰 및 `.ax-*` 클래스를 우선 재사용합니다.
4. **아이콘 및 숫자 정렬**:
   - 장식성 이모지(🏠, 🛒, ⚡, 📦 등) 대신 깔끔한 **stroke SVG 아이콘**(`class="ax-icon"` / `ax-icon-sm`)을 사용합니다.
   - 가격, 수량, 재고, 할인율, 전화번호, 날짜에는 `.ax-num` (`tabular-nums`)을, 상품번호(`#1`)와 주문번호(`ORD-...`)에는 `.ax-code-chip` (`monospace`)을 적용합니다.
5. **레이아웃 수직 정렬 & 반응형**:
   - 헤더·본문·푸터는 모두 `.ax-container` (`max-width: 1320px`)에 맞춰 수직 정렬합니다.
   - 상단 1단 슬림 헤더는 핵심 메뉴(`스토어 홈`, `라이브 커머스`, `주문조회`)만 유지하고, 카테고리 필터는 홈 툴바의 `.ax-segmented-bar`에 배치합니다.
   - 모바일(`<= 768px`)에서 가로 스크롤 오버플로우(`scrollWidth > innerWidth`)가 발생하지 않도록 모든 2단 그리드는 1열로 전환합니다.
   - `/live` 영상 뷰포트(`#liveVideoStage`) 내부만 다크 슬레이트(`#0f172a`)를 사용하여 임베드 영상과 대기 텍스트 가독성을 보장합니다.

---

## 2. 디자인 토큰 (`:root` CSS Variables)

| 토큰명 | 값 | 용도 |
| :--- | :--- | :--- |
| `--ax-bg-canvas` | `#f8fafc` | 페이지 전체 배경 캔버스 |
| `--ax-surface-primary` | `#ffffff` | 기본 카드·모달·헤더 배경 |
| `--ax-surface-secondary` | `#f8fafc` | 입력창·요약 박스·테이블 헤더 |
| `--ax-surface-tertiary` | `#f1f5f9` | Segmented Bar 트랙·푸터·칩 배경 |
| `--ax-border-subtle` | `#e2e8f0` | 기본 카드·테이블·구분선 보더 |
| `--ax-border-strong` | `#cbd5e1` | 입력창·보조 버튼·호버 보더 |
| `--ax-accent-primary` | `#4f46e5` | Primary CTA·활성 탭·라이브 가격·로고 마크 |
| `--ax-accent-hover` | `#4338ca` | Primary 버튼 호버 |
| `--ax-accent-soft` | `#eef2ff` | 영문 Eyebrow 배지·선택된 옵션 칩·안내 콜아웃 |
| `--ax-accent-border` | `#c7d2fe` | Indigo 소프트 영역 보더 |
| `--ax-charcoal` | `#0f172a` | 보조 다크 버튼(`검색`·`조회`)·활성 네비게이션 |
| `--ax-text-primary` | `#0f172a` | 제목·본문 핵심 텍스트 |
| `--ax-text-secondary` | `#475569` | 설명·보조 텍스트 |
| `--ax-text-muted` | `#94a3b8` | 취소선 정가·타임스탬프·비활성 텍스트 |
| `--ax-live` | `#e11d48` | **[제한적 시맨틱]** LIVE 점·할인율(%)·품절/오류/환불 |
| `--ax-success` | `#059669` | 재고 있음·비회원 구매 가능 안내·입금완료 |
| `--ax-warning` | `#b45309` | 계좌입금 안내·입금대기·비회원 배지 |
| `--ax-radius-card` | `14px` | 표준 카드 라운드 |
| `--ax-radius-control` | `10px` | 입력창·버튼 라운드 |

---

## 3. 타이포그래피 & 아이콘 규칙

- **기본 폰트**: `Pretendard Variable` (`base.html`에서 로드), 폴백 `-apple-system, BlinkMacSystemFont, 'Noto Sans KR', sans-serif`.
- **계층 규격**:
  - 영문 Eyebrow 배지: `.ax-badge-eyebrow` (`0.6875rem`, 대문자, `letter-spacing: 0.05em`)
  - 페이지 제목(`h1.ax-page-title`): `1.5rem`, `font-weight: 800`, `letter-spacing: -0.02em`
  - 카드 섹션 제목(`h3`): `1.05rem`, `font-weight: 800`
  - 숫자 정렬: 가격/재고/수량/할인율/연락처/일시에 `.ax-num` 필수
  - 식별 코드: 상품 번호(`#1`), 주문 코드(`ORD-...`), URL 코드에 `.ax-code-chip` 사용
- **SVG 아이콘 규격**:
  - 기본: `class="ax-icon"` (`16×16px`), 소형: `class="ax-icon-sm"` (`14×14px`), 대형: `class="ax-icon-lg"` (`20×20px`)
  - 속성: `fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"`

---

## 4. 표준 컴포넌트 카탈로그 & 마크업 스니펫

### 4.1 페이지 상단 헤더
```html
<div class="ax-page-header">
    <div>
        <span class="ax-badge-eyebrow">FEATURE SECTION</span>
        <div class="ax-page-title-row">
            <h1 class="ax-page-title">새 기능 페이지 제목</h1>
        </div>
        <p class="ax-page-sub">페이지 핵심 설명 문구</p>
    </div>
    <div>
        <button type="button" class="ax-btn ax-btn-primary">주요 액션</button>
    </div>
</div>
```

### 4.2 카드 & KPI 카드
- 기본 컨테이너 카드: `<section class="ax-card ax-card-pad">...</section>`
- 호버 리프트 카드(상품/요약 지표): `<article class="ax-kpi-card">...</article>`
- 내부 보조 박스: `<div class="ax-surface-muted">...</div>`

### 4.3 버튼 계층
| 용도 | 클래스 | 비고 |
| :--- | :--- | :--- |
| 핵심 구매/저장/제출 | `.ax-btn .ax-btn-primary` | Indigo `#4f46e5` |
| 검색/조회/일괄적용 | `.ax-btn .ax-btn-charcoal` | Charcoal `#0f172a` |
| 장바구니 담기(강조 아웃라인) | `.ax-btn .ax-btn-outline-charcoal` | White + 1.5px `#0f172a` |
| 취소/목록/보조 액션 | `.ax-btn .ax-btn-secondary` | White + `#cbd5e1` |
| 삭제/취소 경고 | `.ax-btn .ax-btn-danger-soft` | Soft Rose |
| 크기/너비 옵션 | `.ax-btn-sm`, `.ax-btn-lg`, `.ax-btn-block` | 조합 사용 |

### 4.4 폼 입력창 & 검색 아이콘 입력창
> ⚠️ **중요 함정 방지**: `.ax-input`과 검색 아이콘을 함께 쓸 때는 반드시 `.ax-search-wrap` 내부에 `.ax-input.ax-input-search`를 사용하여 왼쪽 패딩(`2.25rem`)이 덮어써지지 않도록 합니다.
```html
<div class="ax-field">
    <label class="ax-label" for="exampleInput">라벨 *</label>
    <input type="text" id="exampleInput" class="ax-input" placeholder="입력하세요">
</div>

<div class="ax-search-wrap">
    <svg class="ax-icon-sm ax-search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
    <input type="text" class="ax-input ax-input-search" placeholder="검색어 입력...">
</div>
```

### 4.5 Segmented Bar (필터/탭)
```html
<nav class="ax-segmented-bar" aria-label="필터 탭">
    <a href="?tab=all" class="ax-tab-btn active">전체</a>
    <a href="?tab=sub" class="ax-tab-btn">세부 항목</a>
</nav>
```

### 4.6 테이블 & 배지
```html
<div class="ax-table-wrap">
    <table class="ax-table">
        <thead><tr><th>항목</th><th>수량</th><th>상태</th></tr></thead>
        <tbody>
            <tr>
                <td><span class="ax-code-chip">#1</span></td>
                <td class="ax-num">12개</td>
                <td><span class="ax-badge ax-badge-success">주문가능</span></td>
            </tr>
        </tbody>
    </table>
</div>
```

### 4.7 모달 (Astryx Dialog) & 토스트
- 새 모달은 반드시 `<body>` 직속(예: `base.html` 또는 페이지 최상위 루트)에 `.ax-dialog-backdrop` / `.ax-dialog-card` 구조로 배치하고, 배경 클릭 닫기와 `Escape` 키 닫기를 지원해야 합니다.
- 토스트 알림은 직접 구현하지 말고 `base.html` 전역 함수 `showMallToast('메시지')`를 호출합니다.

### 4.8 모바일 하단 고정 구매 바 (구매 전환 페이지)
- 상품 구매 전환이 일어나는 페이지(`/live`, `/product/<num>` 등)에 모바일 고정 바를 둘 때는 `.ax-mobile-sticky-bar`를 사용하고, 페이지 스크립트에서 `document.body.classList.add('has-mobile-sticky-bar')`를 추가하여 본문 하단 및 토스트가 가려지지 않도록 합니다.

---

## 5. 새 기능·페이지 추가 시 필수 체크리스트

1. **템플릿 상속**: 반드시 `{% extends "base.html" %}`를 사용하고, 별도 `<style>` 블록을 만들기 전에 `static/css/astryx_mall.css`의 `.ax-*` 클래스를 우선 사용합니다. 새 전용 클래스가 꼭 필요하면 `astryx_mall.css`에 `--ax-*` 토큰을 참조해 추가합니다.
2. **무에러 템플릿 작성 규칙 (IDE 파서 & 자동 포맷 손상 원천 차단)**:
   - HTML `style="..."`, `<script>`, `onclick`/`onchange` 이벤트 핸들러, 태그 속성 나열 영역(`<option ...>`) 내부에 Jinja2(`{{ ... }}`, `{% ... %}`) 문법을 직접 삽입하지 않습니다.
   - 조건부 표시/스타일은 `astryx_mall.css` 클래스(`.ax-is-hidden` 등) 조합으로 처리합니다.
   - 서버 데이터를 JS로 전달할 때는 `<div id="..." hidden data-foo="{{ val | tojson | forceescape }}"></div>` 브릿지와 순수 JS `JSON.parse(el.dataset.foo)`를 사용하며, 이벤트 핸들러 인자는 `data-*` 속성과 `this.dataset.*`으로 전달합니다.
3. **기존 기능/DOM ID/보안 계약 보존**:
   - `base.html`의 `#instantBuyModal`, `#navCartBadge`, `#mallNavLiveSlot`, `window.__mallApplyLiteState`, `showMallToast` 계약 유지.
   - `orders.html`, `admin.html` 등에서 주문 품목 순회 시 Python dict `.items()` 충돌 방지를 위해 `ord.order_items if ord.order_items is defined else ord['items']` 패턴 유지.
   - POST 폼에는 `<input type="hidden" name="csrf_token" value="{{ csrf_token }}">` 포함.
4. **완료 전 검증 필수**:
   - 데스크톱(`1440×900`) 및 모바일(`390×844`)에서 가로 넘침(`scrollWidth <= innerWidth`) 0건 확인.
   - 브라우저 콘솔 에러 0건, CSS/이미지 404 0건, HTML 태그 균형, 원문 템플릿 기준 IDE HTML/CSS/JS 구문 에러 0건 확인.
   - `THE VINE LIVE MALL` 또는 삭제된 구 테마(`mall_light_theme.css`) 재도입 금지.
5. **일반 쇼핑몰 ↔ 라이브 커머스(`/live`) UI·가격 분리 원칙**:
   - 일반 스토어 페이지(`/`, `/product/<num>`, `/cart`, `/checkout`, `/orders`, `/auth/*`)에는 상단 내비게이션의 `라이브 커머스` 메뉴 진입점을 제외하고 일체의 라이브·OBS 관련 문구, 배너, 배지, 스튜디오 버튼을 노출하지 않습니다.
   - 일반 스토어는 방송 진행 여부와 무관하게 항상 일반 판매가(`price`)로 표시·결제하며, 라이브 특가(`live_price`)와 실시간 방송 UI는 `/live` 전용관 내부에만 한정합니다.
