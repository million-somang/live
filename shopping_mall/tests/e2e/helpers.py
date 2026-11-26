"""Opaque-box HTTP, Playwright, layout, and seeding helpers for E2E tests.

Strictly opaque-box: MUST NOT import any ``mall_*`` or ``app`` backend modules.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any

import requests
from playwright.sync_api import ConsoleMessage, Page, Response, Route

_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

_CSRF_META_RE_1 = re.compile(
    r'<meta\s+[^>]*name=["\']csrf-token["\'][^>]*content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_CSRF_META_RE_2 = re.compile(
    r'<meta\s+[^>]*content=["\']([^"\']*)["\'][^>]*name=["\']csrf-token["\']',
    re.IGNORECASE,
)


def extract_csrf_from_html(html: str) -> str:
    """Extract CSRF token from ``<meta name="csrf-token" content="...">``."""
    match = _CSRF_META_RE_1.search(html) or _CSRF_META_RE_2.search(html)
    return match.group(1).strip() if match else ""


def create_tiny_png_bytes() -> bytes:
    """Return a valid 68-byte 1x1 transparent PNG image."""
    return base64.b64decode(_TINY_PNG_B64)


class MallApiClient:
    """Requests session wrapper that automatically tracks & attaches CSRF tokens."""

    _MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.csrf_token: str = ""

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{self.base_url}{path}"

    def _update_csrf_from_response(self, resp: requests.Response) -> None:
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "text/html" in content_type and resp.text:
            token = extract_csrf_from_html(resp.text)
            if token:
                self.csrf_token = token
        elif "application/json" in content_type and resp.text:
            try:
                data = resp.json()
            except ValueError:
                return
            if isinstance(data, dict) and data.get("csrf_token"):
                self.csrf_token = str(data["csrf_token"])

    @staticmethod
    def json_or_raise(resp: requests.Response, label: str = "") -> dict[str, Any]:
        """Parse JSON response or raise a clear diagnostic RuntimeError."""
        prefix = f"[{label}] " if label else ""
        try:
            data = resp.json()
        except ValueError as exc:
            raise RuntimeError(
                f"{prefix}Expected JSON from {resp.request.method} {resp.url} "
                f"(status={resp.status_code}, content_type={resp.headers.get('Content-Type')!r}), "
                f"body={resp.text[:400]!r}"
            ) from exc
        if not isinstance(data, dict):
            raise RuntimeError(
                f"{prefix}Expected JSON object from {resp.request.method} {resp.url}, "
                f"got {type(data).__name__}: {data!r}"
            )
        return data

    def refresh_csrf(self, path: str = "/") -> str:
        """Fetch an HTML page to obtain or refresh the session CSRF token."""
        resp = self.request("GET", path, include_csrf=False)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Failed to refresh CSRF from {path}: HTTP {resp.status_code} ({resp.text[:200]!r})"
            )
        token = extract_csrf_from_html(resp.text)
        if token:
            self.csrf_token = token
        return self.csrf_token

    def request(
        self,
        method: str,
        path: str,
        *,
        include_csrf: bool = True,
        allow_rate_limit: bool = False,
        **kwargs: Any,
    ) -> requests.Response:
        upper_method = method.upper()
        headers = dict(kwargs.pop("headers", None) or {})
        kwargs.setdefault("timeout", self.timeout)

        if upper_method in self._MUTATING_METHODS and include_csrf:
            has_explicit_csrf = any(
                k.lower() in ("x-csrf-token", "x-csrftoken") for k in headers
            )
            if not has_explicit_csrf:
                if not self.csrf_token:
                    self.refresh_csrf("/")
                if self.csrf_token:
                    headers["X-CSRF-Token"] = self.csrf_token

        url = self._build_url(path)
        resp = self.session.request(upper_method, url, headers=headers, **kwargs)
        if not allow_rate_limit and resp.status_code == 429:
            raise RuntimeError(
                f"Unexpected HTTP 429 rate limit on {upper_method} {url}: {resp.text[:300]!r}"
            )
        self._update_csrf_from_response(resp)
        return resp

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", path, include_csrf=False, **kwargs)

    def post(self, path: str, *, include_csrf: bool = True, **kwargs: Any) -> requests.Response:
        return self.request("POST", path, include_csrf=include_csrf, **kwargs)

    def put(self, path: str, *, include_csrf: bool = True, **kwargs: Any) -> requests.Response:
        return self.request("PUT", path, include_csrf=include_csrf, **kwargs)

    def patch(self, path: str, *, include_csrf: bool = True, **kwargs: Any) -> requests.Response:
        return self.request("PATCH", path, include_csrf=include_csrf, **kwargs)

    def delete(self, path: str, *, include_csrf: bool = True, **kwargs: Any) -> requests.Response:
        return self.request("DELETE", path, include_csrf=include_csrf, **kwargs)

    def login(self, email: str, password: str, *, next_url: str = "/") -> dict[str, Any]:
        """Log in via JSON /auth/login and update rotated CSRF token."""
        if not self.csrf_token:
            self.refresh_csrf("/auth/login")
        resp = self.post(
            "/auth/login",
            json={"email": email, "password": password, "next": next_url},
            include_csrf=True,
        )
        data = self.json_or_raise(resp, label="login")
        if resp.status_code != 200 or data.get("status") != "ok":
            raise RuntimeError(
                f"Login failed for {email!r}: status={resp.status_code}, payload={data!r}"
            )
        if data.get("csrf_token"):
            self.csrf_token = str(data["csrf_token"])
        else:
            self.refresh_csrf("/")
        return data

    def register(
        self,
        email: str,
        password: str,
        *,
        name: str = "E2E회원",
        phone: str = "010-1111-2222",
        zipcode: str = "06236",
        address: str = "서울특별시 강남구 테헤란로 152",
        address_detail: str = "101호",
        agree_terms: bool = True,
        agree_privacy: bool = True,
        **extra: Any,
    ) -> dict[str, Any]:
        """Register a member via JSON /auth/register and update rotated CSRF token."""
        if not self.csrf_token:
            self.refresh_csrf("/auth/register")
        payload: dict[str, Any] = {
            "email": email,
            "password": password,
            "name": name,
            "phone": phone,
            "zipcode": zipcode,
            "address": address,
            "address_detail": address_detail,
            "agree_terms": agree_terms,
            "agree_privacy": agree_privacy,
        }
        payload.update(extra)
        resp = self.post("/auth/register", json=payload, include_csrf=True)
        data = self.json_or_raise(resp, label="register")
        if resp.status_code != 200 or data.get("status") != "ok":
            raise RuntimeError(
                f"Register failed for {email!r}: status={resp.status_code}, payload={data!r}"
            )
        if data.get("csrf_token"):
            self.csrf_token = str(data["csrf_token"])
        else:
            self.refresh_csrf("/")
        return data

    def logout(self) -> dict[str, Any]:
        """Log out via JSON /auth/logout and clear local CSRF state."""
        resp = self.post("/auth/logout", json={}, include_csrf=True)
        self.csrf_token = ""
        if "application/json" in (resp.headers.get("Content-Type") or "").lower():
            return self.json_or_raise(resp, label="logout")
        return {"status": "ok", "http_status": resp.status_code}

    def sync_from_obs(self, payload: dict[str, Any], sync_secret: str) -> requests.Response:
        """Call POST /api/sync/from-obs with X-Mall-Sync-Secret header."""
        return self.post(
            "/api/sync/from-obs",
            json=payload,
            headers={"X-Mall-Sync-Secret": sync_secret},
            include_csrf=False,
        )

    def clone_with_cookies(self, *, refresh_csrf: bool = True) -> MallApiClient:
        """Create an isolated client sharing this session's cookies (safe across tests)."""
        clone = MallApiClient(self.base_url, timeout=self.timeout)
        for cookie in self.session.cookies:
            clone.session.cookies.set_cookie(cookie)
        if refresh_csrf:
            clone.refresh_csrf("/")
        return clone


def seed_product_via_sync(
    base_url: str,
    sync_secret: str,
    *,
    num: int = 1,
    name: str = "E2E 테스트 상품",
    price: int = 29000,
    consumer_price: int | None = 39000,
    live_price: int | None = None,
    category: str = "TOP",
    options: list[dict[str, Any]] | None = None,
    image_url: str = "",
    specs: str = "E2E 테스트 스펙",
    detail_content: str = "E2E 테스트 상세 설명",
    **extra: Any,
) -> dict[str, Any]:
    """Seed or update a product through the public OBS sync webhook contract.

    Always defaults ``options`` to a non-empty list so ``total_stock > 0`` unless
    explicitly overridden, and defaults ``image_url=""`` so inline SVG fallback
    is used without creating repo disk artifacts or image 404s.
    """
    resolved_options = (
        [{"color": "블랙", "size": "M", "stock": 20}] if options is None else options
    )
    resolved_consumer_price = price if consumer_price is None else consumer_price
    resolved_live_price = price if live_price is None else live_price

    image_bytes = extra.pop("image_bytes", None)
    product_payload: dict[str, Any] = {
        "num": num,
        "name": name,
        "category": category,
        "price": price,
        "consumer_price": resolved_consumer_price,
        "live_price": resolved_live_price,
        "image_url": image_url,
        "options": resolved_options,
        "specs": specs,
        "detail_content": detail_content,
    }
    product_payload.update(extra)

    body: dict[str, Any] = {
        "action": "upsert_product",
        "product": product_payload,
    }
    if image_bytes is not None:
        body["image_b64"] = base64.b64encode(image_bytes).decode("ascii")

    resp = requests.post(
        f"{base_url.rstrip('/')}/api/sync/from-obs",
        json=body,
        headers={"X-Mall-Sync-Secret": sync_secret},
        timeout=10.0,
    )
    data = MallApiClient.json_or_raise(resp, label="seed_product_via_sync")
    if resp.status_code != 200 or data.get("status") != "ok":
        raise RuntimeError(
            f"seed_product_via_sync failed (num={num}): status={resp.status_code}, payload={data!r}"
        )
    product = data.get("product")
    if not isinstance(product, dict):
        raise RuntimeError(f"seed_product_via_sync returned non-dict product: {data!r}")
    return product


def create_order_via_api(
    api_client: MallApiClient,
    *,
    items: list[dict[str, Any]],
    buyer_name: str = "테스트구매자",
    buyer_phone: str = "010-1234-5678",
    zipcode: str = "06236",
    address: str = "서울특별시 강남구 테헤란로 152",
    address_detail: str = "101호",
    delivery_memo: str = "",
    guest_password: str = "",
    source_page: str = "CART_CHECKOUT",
    payment_method: str = "BANK",
    **extra: Any,
) -> dict[str, Any]:
    """Create an order via POST /api/orders/create and return the order payload."""
    payload: dict[str, Any] = {
        "items": items,
        "buyer_name": buyer_name,
        "buyer_phone": buyer_phone,
        "zipcode": zipcode,
        "address": address,
        "address_detail": address_detail,
        "delivery_memo": delivery_memo,
        "guest_password": guest_password,
        "source_page": source_page,
        "payment_method": payment_method,
    }
    payload.update(extra)
    resp = api_client.post("/api/orders/create", json=payload)
    data = MallApiClient.json_or_raise(resp, label="create_order_via_api")
    if resp.status_code != 200 or data.get("status") != "ok":
        raise RuntimeError(
            f"create_order_via_api failed: status={resp.status_code}, payload={data!r}"
        )
    order = data.get("order")
    if not isinstance(order, dict):
        raise RuntimeError(f"create_order_via_api returned invalid order payload: {data!r}")
    return order


@dataclass
class PageDiagnostics:
    """Collector for browser console errors, uncaught pageerrors, and HTTP >=400 responses."""

    console_errors: list[dict[str, Any]] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    http_errors: list[dict[str, Any]] = field(default_factory=list)

    def clear(self) -> None:
        self.console_errors.clear()
        self.page_errors.clear()
        self.http_errors.clear()

    def assert_clean(self, label: str = "") -> None:
        problems: list[str] = []
        if self.console_errors:
            problems.append(f"console_errors={self.console_errors!r}")
        if self.page_errors:
            problems.append(f"page_errors={self.page_errors!r}")
        if self.http_errors:
            problems.append(f"http_errors={self.http_errors!r}")
        if problems:
            prefix = f"[{label}] " if label else ""
            raise AssertionError(prefix + "Page diagnostics not clean: " + "; ".join(problems))


def attach_page_diagnostics(page: Page) -> PageDiagnostics:
    """Attach console.error, pageerror, and HTTP >=400 listeners to a Playwright Page."""
    diag = PageDiagnostics()

    def _on_console(msg: ConsoleMessage) -> None:
        if msg.type == "error":
            diag.console_errors.append(
                {
                    "text": msg.text,
                    "location": dict(msg.location) if msg.location else {},
                }
            )

    def _on_pageerror(exc: Exception) -> None:
        diag.page_errors.append(str(exc))

    def _on_response(resp: Response) -> None:
        if resp.status >= 400:
            diag.http_errors.append(
                {
                    "status": resp.status,
                    "url": resp.url,
                    "method": resp.request.method,
                }
            )

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)
    page.on("response", _on_response)
    return diag


def assert_no_horizontal_overflow(page: Page, tolerance_px: int = 1) -> None:
    """Verify the rendered page has zero horizontal overflow beyond viewport width."""
    result = page.evaluate(
        """(tolerance) => {
            const docWidth = document.documentElement.scrollWidth;
            const winWidth = window.innerWidth;
            if (docWidth <= winWidth + tolerance) {
                return { ok: true, docWidth, winWidth, offenders: [] };
            }
            const offenders = [];
            for (const el of document.querySelectorAll('*')) {
                const cs = window.getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) continue;
                if (rect.right > winWidth + tolerance || rect.left < -tolerance) {
                    offenders.push({
                        tag: el.tagName,
                        id: el.id || '',
                        className: typeof el.className === 'string' ? el.className : '',
                        left: Math.round(rect.left),
                        right: Math.round(rect.right),
                        width: Math.round(rect.width)
                    });
                    if (offenders.length >= 10) break;
                }
            }
            return { ok: false, docWidth, winWidth, offenders };
        }""",
        tolerance_px,
    )
    if not result["ok"]:
        raise AssertionError(
            f"Horizontal overflow detected: scrollWidth={result['docWidth']} > "
            f"innerWidth={result['winWidth']} + {tolerance_px}; "
            f"offenders={result['offenders']!r}"
        )


def assert_no_main_internal_vertical_scroll(page: Page) -> None:
    """Verify main page body sections do not create internal vertical scroll containers.

    Exempts document root (HTML/BODY), native form/media controls, and popup
    modals (.ax-dialog-card, .ax-dialog-body, .ax-lightbox-card, .ax-dialog-backdrop).
    """
    violations = page.evaluate(
        """() => {
            const skipTags = new Set([
                'HTML', 'BODY', 'SELECT', 'TEXTAREA', 'INPUT', 'OPTION', 'BUTTON', 'SVG', 'IFRAME'
            ]);
            const modalSelector = '.ax-dialog-card, .ax-dialog-body, .ax-lightbox-card';
            const results = [];
            for (const el of document.querySelectorAll('*')) {
                if (skipTags.has(el.tagName)) continue;
                if (el.matches('.ax-dialog-backdrop') || el.closest(modalSelector)) continue;
                const cs = window.getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                const rect = el.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) continue;
                if (el.offsetParent === null && cs.position !== 'fixed') continue;
                if (cs.overflowY === 'auto' || cs.overflowY === 'scroll') {
                    const hasMaxHeight = cs.maxHeight && cs.maxHeight !== 'none';
                    const hasScrollableOverflow = el.scrollHeight > el.clientHeight + 2;
                    if (hasMaxHeight || hasScrollableOverflow) {
                        results.push({
                            tag: el.tagName,
                            id: el.id || '',
                            className: typeof el.className === 'string' ? el.className : '',
                            overflowY: cs.overflowY,
                            maxHeight: cs.maxHeight,
                            scrollHeight: el.scrollHeight,
                            clientHeight: el.clientHeight
                        });
                        if (results.length >= 10) break;
                    }
                }
            }
            return results;
        }"""
    )
    if violations:
        raise AssertionError(
            f"Prohibited internal vertical scroll detected in main page sections: {violations!r}"
        )


def mock_daum_postcode(
    page: Page,
    zipcode: str = "06236",
    address: str = "서울특별시 강남구 테헤란로 152",
    road_address: str | None = None,
    jibun_address: str = "",
) -> None:
    """Stub Daum/Kakao Postcode CDN & ``window.daum.Postcode`` deterministically."""
    resolved_road = address if road_address is None else road_address
    payload = {
        "zonecode": zipcode,
        "address": address,
        "roadAddress": resolved_road,
        "jibunAddress": jibun_address,
        "userSelectedType": "R",
        "addressType": "R",
        "bname": "",
        "buildingName": "",
        "apartment": "N",
    }
    payload_json = json.dumps(payload, ensure_ascii=False)
    stub_js = (
        "(() => {\n"
        f"  window.__E2E_DAUM_MOCK__ = {payload_json};\n"
        "  window.daum = window.daum || {};\n"
        "  window.daum.Postcode = function(opts) {\n"
        "    this._opts = opts || {};\n"
        "  };\n"
        "  window.daum.Postcode.prototype.open = function() {\n"
        "    if (this._opts && typeof this._opts.oncomplete === 'function') {\n"
        "      this._opts.oncomplete(Object.assign({}, window.__E2E_DAUM_MOCK__));\n"
        "    }\n"
        "  };\n"
        "})();"
    )

    page.add_init_script(stub_js)

    def _fulfill_daum(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/javascript; charset=utf-8",
            body=stub_js,
        )

    page.route("**/*daumcdn.net/**", _fulfill_daum)
    try:
        page.evaluate(stub_js)
    except Exception:
        # Page may still be at about:blank before first navigation; init_script covers it.
        pass
