"""Tier 2 opaque-box boundary, negative, auth, and security E2E tests for R1–F5."""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import BrowserContext, Page, expect

from e2e.helpers import (
    assert_no_horizontal_overflow,
    assert_no_main_internal_vertical_scroll,
    attach_page_diagnostics,
    create_order_via_api,
    extract_csrf_from_html,
    seed_product_via_sync,
)

MALL_CART_KEY = "gollabang_mall_cart_v1"


def _login_browser_context(
    context: BrowserContext,
    base_url: str,
    email: str,
    password: str,
) -> Page:
    page = context.new_page()
    page.goto(f"{base_url}/auth/login", wait_until="domcontentloaded")
    res = page.evaluate(
        """async ({ email, password }) => {
            const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
            const resp = await fetch('/auth/login', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': csrf
                },
                body: JSON.stringify({ email, password, next: '/' })
            });
            return { status: resp.status, body: await resp.text() };
        }""",
        {"email": email, "password": password},
    )
    assert res["status"] == 200, f"Browser login failed for {email}: {res}"
    return page


def _seed_cart_local_storage(page: Page, base_url: str, items: list[dict[str, Any]]) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.evaluate(
        """({ key, items }) => {
            localStorage.setItem(key, JSON.stringify(items));
        }""",
        {"key": MALL_CART_KEY, "items": items},
    )


def _update_store_info(server, api_client_factory, updates: dict[str, Any]) -> dict[str, Any]:
    admin = api_client_factory(server)
    admin.login(server.admin_email, server.admin_password)
    resp = admin.post("/api/admin/store-info", json=updates)
    assert resp.status_code == 200, resp.text
    return admin.json_or_raise(resp)["store_info"]


# =============================================================================
# F1 Tier 2: Auth, CSRF, fallback, length/XSS escaping, empty orders isolation
# =============================================================================


@pytest.mark.tier2
@pytest.mark.f1
def test_f1_store_info_auth_and_csrf_boundaries(
    live_server,
    guest_api,
    member_api,
    admin_api,
):
    assert guest_api.get("/api/admin/store-info").status_code == 401
    assert guest_api.post("/api/admin/store-info", json={"bank_name": "해킹은행"}).status_code == 401

    assert member_api.get("/api/admin/store-info").status_code == 403
    assert member_api.post("/api/admin/store-info", json={"bank_name": "해킹은행"}).status_code == 403

    no_csrf = admin_api.post(
        "/api/admin/store-info",
        json={"bank_name": "CSRF누락은행"},
        include_csrf=False,
    )
    assert no_csrf.status_code == 403


@pytest.mark.tier2
@pytest.mark.f1
def test_f1_blank_bank_fields_fallback_to_default_and_preserve_shipping(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    saved1 = _update_store_info(
        srv,
        api_client_factory,
        {
            "bank_name": "   ",
            "bank_account": "999-111-222333",
            "bank_holder": "테스트예금주",
            "shipping_fee": 2500,
        },
    )
    assert saved1["bank_name"] == "카카오뱅크"
    assert saved1["bank_account"] == "999-111-222333"
    assert saved1["bank_holder"] == "테스트예금주"
    assert saved1["shipping_fee"] == 2500

    # Partial update preserves previously configured fields
    saved2 = _update_store_info(
        srv,
        api_client_factory,
        {"free_shipping_threshold": 60000},
    )
    assert saved2["bank_name"] == "카카오뱅크"
    assert saved2["bank_account"] == "999-111-222333"
    assert saved2["bank_holder"] == "테스트예금주"
    assert saved2["shipping_fee"] == 2500
    assert saved2["free_shipping_threshold"] == 60000


@pytest.mark.tier2
@pytest.mark.f1
def test_f1_bank_fields_max_length_truncation_and_xss_escaping(
    server_factory,
    api_client_factory,
    desktop_page: Page,
):
    srv = server_factory(seed_products=True)
    long_bank = "은행" * 50  # > 60 chars
    long_acc = "1" * 100  # > 80 chars
    long_holder = "예" * 50  # > 60 chars
    truncated = _update_store_info(
        srv,
        api_client_factory,
        {
            "bank_name": long_bank,
            "bank_account": long_acc,
            "bank_holder": long_holder,
        },
    )
    assert len(truncated["bank_name"]) <= 60
    assert len(truncated["bank_account"]) <= 80
    assert len(truncated["bank_holder"]) <= 60

    xss_payload = "<script>window.__f1_xss=1;</script>안전은행"
    _update_store_info(
        srv,
        api_client_factory,
        {
            "bank_name": xss_payload,
            "bank_account": "123-456-7890",
            "bank_holder": "홍길동",
        },
    )

    dialogs: list[str] = []
    desktop_page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    diag = attach_page_diagnostics(desktop_page)
    for route in ["/", "/policy/terms"]:
        desktop_page.goto(f"{srv.base_url}{route}", wait_until="domcontentloaded")
        assert desktop_page.evaluate("() => window.__f1_xss") is None
        assert "<script>window.__f1_xss=1;</script>" in desktop_page.locator("body").inner_text()
    assert dialogs == []
    diag.assert_clean("f1_xss_escaping")


@pytest.mark.tier2
@pytest.mark.f1
def test_f1_orders_page_empty_hides_bank_box_and_wrong_guest_pw_isolated(
    server_factory,
    api_client_factory,
    desktop_page: Page,
):
    srv = server_factory(seed_products=True)
    creator = api_client_factory(srv)
    ord_data = create_order_via_api(
        creator,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_phone="010-4455-6677",
        guest_password="rightpw99",
    )

    diag = attach_page_diagnostics(desktop_page)
    desktop_page.goto(f"{srv.base_url}/orders", wait_until="domcontentloaded")
    expect(desktop_page.locator(".ax-order-card")).to_have_count(0)
    expect(desktop_page.locator("main .ax-bank-box")).to_have_count(0)

    desktop_page.locator("#lookupOrderCode").fill(ord_data["order_code"])
    desktop_page.locator("#lookupBuyerPhone").fill("010-4455-6677")
    desktop_page.locator("#lookupGuestPassword").fill("wrongpw11")
    desktop_page.locator("#guestOrderLookupForm button[type='submit']").click()
    desktop_page.wait_for_load_state("domcontentloaded")

    expect(desktop_page.locator(".ax-order-card")).to_have_count(0)
    expect(desktop_page.locator("main .ax-bank-box")).to_have_count(0)
    diag.assert_clean("f1_empty_orders_isolated")


@pytest.mark.tier2
@pytest.mark.f1
def test_f1_checkout_and_modal_copy_button_without_clipboard_permission(
    server_factory,
    api_client_factory,
    desktop_page: Page,
):
    srv = server_factory(seed_products=True)
    _update_store_info(
        srv,
        api_client_factory,
        {
            "bank_name": "농협은행",
            "bank_account": "301-0000-1111-21",
            "bank_holder": "폴백테스트",
        },
    )
    p1 = api_client_factory(srv).json_or_raise(
        api_client_factory(srv).get("/api/products/1")
    )["product"]

    diag = attach_page_diagnostics(desktop_page)
    # Force clipboard.writeText rejection to verify graceful toast fallback
    desktop_page.add_init_script(
        """() => {
            Object.defineProperty(navigator, 'clipboard', {
                value: { writeText: () => Promise.reject(new Error('Clipboard denied')) },
                configurable: true
            });
        }"""
    )

    _seed_cart_local_storage(
        desktop_page,
        srv.base_url,
        [
            {
                "num": p1["num"],
                "name": p1["name"],
                "price": p1["price"],
                "consumer_price": p1["consumer_price"],
                "image_url": "",
                "color": "블랙",
                "size": "M",
                "quantity": 1,
            }
        ],
    )
    desktop_page.goto(f"{srv.base_url}/checkout", wait_until="domcontentloaded")
    desktop_page.locator("#checkoutForm .ax-bank-box").get_by_role("button", name="계좌번호 복사").click()
    expect(desktop_page.locator("#mallToastContainer .ax-toast").last).to_contain_text("301-0000-1111-21")

    desktop_page.goto(f"{srv.base_url}/product/1", wait_until="domcontentloaded")
    desktop_page.get_by_role("button", name="바로 구매하기").click()
    desktop_page.locator("#instantBuyModal .ax-bank-box").get_by_role("button", name="계좌 복사").click()
    expect(desktop_page.locator("#mallToastContainer .ax-toast").last).to_contain_text("폴백테스트")
    diag.assert_clean("f1_clipboard_fallback")


# =============================================================================
# F2 Tier 2: Single product, no self-inclusion, sold-out, 404, mobile layout
# =============================================================================


@pytest.mark.tier2
@pytest.mark.f2
def test_f2_single_product_catalog_hides_related_section_and_anchor(
    server_factory,
    desktop_page: Page,
):
    srv = server_factory(seed_products=False)
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=1,
        name="단독상품",
        category="TOP",
        price=19000,
    )

    diag = attach_page_diagnostics(desktop_page)
    resp = desktop_page.goto(f"{srv.base_url}/product/1", wait_until="domcontentloaded")
    assert resp is not None and resp.status == 200
    expect(desktop_page.locator("#section-related")).to_have_count(0)
    expect(desktop_page.locator('a[href="#section-related"]')).to_have_count(0)
    diag.assert_clean("f2_single_product")


@pytest.mark.tier2
@pytest.mark.f2
def test_f2_related_products_never_includes_self_even_when_all_same_category(
    server_factory,
    desktop_page: Page,
):
    srv = server_factory(seed_products=False)
    for n in range(1, 7):
        seed_product_via_sync(
            srv.base_url,
            srv.sync_secret,
            num=n,
            name=f"동일카테고리상의_{n}",
            category="TOP",
            price=20000 + n * 1000,
        )

    diag = attach_page_diagnostics(desktop_page)
    for current_num in range(1, 7):
        desktop_page.goto(f"{srv.base_url}/product/{current_num}", wait_until="domcontentloaded")
        cards = desktop_page.locator("#section-related article.ax-product-card")
        expect(cards).to_have_count(4)
        nums = [
            int(cards.nth(i).locator("[data-csr-stock-num]").get_attribute("data-csr-stock-num") or 0)
            for i in range(4)
        ]
        assert current_num not in nums
        assert len(set(nums)) == 4
    diag.assert_clean("f2_no_self_inclusion")


@pytest.mark.tier2
@pytest.mark.f2
def test_f2_related_products_all_sold_out_still_renders_and_links(
    server_factory,
    desktop_page: Page,
):
    srv = server_factory(seed_products=False)
    for n in (1, 2, 3):
        seed_product_via_sync(
            srv.base_url,
            srv.sync_secret,
            num=n,
            name=f"품절상품_{n}",
            category="TOP",
            price=25000,
            options=[{"color": "블랙", "size": "M", "stock": 0}],
        )

    diag = attach_page_diagnostics(desktop_page)
    desktop_page.goto(f"{srv.base_url}/product/1", wait_until="domcontentloaded")
    cards = desktop_page.locator("#section-related article.ax-product-card")
    expect(cards).to_have_count(2)
    expect(cards.first).to_contain_text("잔여 0개")
    cards.first.locator("a", has_text="상세").click()
    desktop_page.wait_for_url("**/product/2")
    expect(desktop_page.locator("h1")).to_contain_text("품절상품_2")
    diag.assert_clean("f2_all_sold_out")


@pytest.mark.tier2
@pytest.mark.f2
def test_f2_nonexistent_product_detail_redirects_home(
    live_server,
    guest_api,
):
    resp_html = guest_api.get("/product/999999", allow_redirects=False)
    assert resp_html.status_code == 302
    assert resp_html.headers.get("Location", "").endswith("/")

    resp_api = guest_api.get("/api/products/999999")
    assert resp_api.status_code == 404


@pytest.mark.tier2
@pytest.mark.f2
def test_f2_related_products_mobile_viewport_layout_clean(
    live_server,
    mobile_page: Page,
):
    diag = attach_page_diagnostics(mobile_page)
    resp = mobile_page.goto(f"{live_server.base_url}/product/1", wait_until="domcontentloaded")
    assert resp is not None and resp.status == 200
    expect(mobile_page.locator("#section-related article.ax-product-card")).to_have_count(4)
    assert_no_horizontal_overflow(mobile_page)
    assert_no_main_internal_vertical_scroll(mobile_page)
    diag.assert_clean("f2_mobile_layout")


# =============================================================================
# F3 Tier 2: Offline Daum fallback, normalization, truncation, member override, rate limit
# =============================================================================


@pytest.mark.tier2
@pytest.mark.f3
def test_f3_daum_cdn_blocked_offline_fallback_register_checkout_instant_buy(
    server_factory,
    api_client_factory,
    desktop_page: Page,
):
    srv = server_factory(seed_products=True)
    p1 = api_client_factory(srv).json_or_raise(
        api_client_factory(srv).get("/api/products/1")
    )["product"]

    desktop_page.route("**/*daumcdn.net/**", lambda route: route.abort())
    diag = attach_page_diagnostics(desktop_page)

    # 1) /auth/register offline fallback + manual registration succeeds
    desktop_page.goto(f"{srv.base_url}/auth/register", wait_until="domcontentloaded")
    desktop_page.locator("#btnRegPostcode").click()
    expect(desktop_page.locator("#mallToastContainer .ax-toast").last).to_contain_text(
        "직접 입력해 주세요"
    )
    assert diag.page_errors == []
    diag.clear()

    desktop_page.locator("#regEmail").fill("offline_zip@e2e.local")
    desktop_page.locator("#regPassword").fill("OfflinePass123!")
    desktop_page.locator("#regName").fill("오프라인회원")
    desktop_page.locator("#regPhone").fill("010-1212-3434")
    desktop_page.locator("#regZipcode").fill("04524")
    desktop_page.locator("#regAddress").fill("서울특별시 중구 세종대로 110")
    desktop_page.locator("#regAgreeAll").check()
    desktop_page.get_by_role("button", name="회원가입 완료하기").click()
    desktop_page.wait_for_url(f"{srv.base_url}/")

    # 2) /checkout offline fallback + manual order succeeds
    _seed_cart_local_storage(
        desktop_page,
        srv.base_url,
        [
            {
                "num": p1["num"],
                "name": p1["name"],
                "price": p1["price"],
                "consumer_price": p1["consumer_price"],
                "image_url": "",
                "color": "블랙",
                "size": "M",
                "quantity": 1,
            }
        ],
    )
    desktop_page.goto(f"{srv.base_url}/checkout", wait_until="domcontentloaded")
    desktop_page.locator("#btnCoPostcode").click()
    expect(desktop_page.locator("#mallToastContainer .ax-toast").last).to_contain_text(
        "직접 입력해 주세요"
    )
    assert diag.page_errors == []
    diag.clear()

    desktop_page.locator("#coZipcode").fill("06236")
    desktop_page.locator("#coAddress").fill("서울특별시 강남구 테헤란로 152")
    desktop_page.locator("#coSubmitBtn").click()
    desktop_page.wait_for_url("**/order-complete/**")
    expect(desktop_page.locator("#orderCompleteCode")).to_be_visible()
    desktop_page.locator('a[href="/orders"]').first.click()
    desktop_page.wait_for_url("**/orders")
    expect(desktop_page.locator(".ax-order-card-meta").first).to_contain_text("[06236]")

    # 3) #instantBuyModal offline fallback
    desktop_page.goto(f"{srv.base_url}/product/1", wait_until="domcontentloaded")
    desktop_page.get_by_role("button", name="바로 구매하기").click()
    desktop_page.locator("#btnIbPostcode").click()
    expect(desktop_page.locator("#mallToastContainer .ax-toast").last).to_contain_text(
        "직접 입력해 주세요"
    )
    assert diag.page_errors == []


@pytest.mark.tier2
@pytest.mark.f3
def test_f3_zipcode_optional_empty_and_whitespace_normalization(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    c = api_client_factory(srv)
    reg_empty = c.register("zip_empty@e2e.local", "ZipPass123!", zipcode="")
    assert reg_empty["user"]["zipcode"] == ""

    ord_empty = create_order_via_api(
        c,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        zipcode="",
    )
    assert ord_empty["zipcode"] == ""

    c2 = api_client_factory(srv)
    reg_space = c2.register("zip_space@e2e.local", "ZipPass123!", zipcode=" 062 36\t")
    assert reg_space["user"]["zipcode"] == "06236"

    ord_space = create_order_via_api(
        c2,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        zipcode=" 135 29 ",
    )
    assert ord_space["zipcode"] == "13529"


@pytest.mark.tier2
@pytest.mark.f3
def test_f3_zipcode_overlong_truncation_16_chars(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    c = api_client_factory(srv)
    overlong = "12345678901234567890"
    reg = c.register("zip_long@e2e.local", "ZipPass123!", zipcode=overlong)
    assert reg["user"]["zipcode"] == "1234567890123456"

    ord_res = create_order_via_api(
        c,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        zipcode=overlong,
    )
    assert ord_res["zipcode"] == "1234567890123456"


@pytest.mark.tier2
@pytest.mark.f3
def test_f3_logged_in_member_overrides_guest_last_buyer_zipcode(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=True)
    reg_client = api_client_factory(srv)
    reg_client.register("member_zip@e2e.local", "MemberPass123!", zipcode="06236")
    p1 = reg_client.json_or_raise(reg_client.get("/api/products/1"))["product"]

    ctx = desktop_context_factory()
    page = _login_browser_context(ctx, srv.base_url, "member_zip@e2e.local", "MemberPass123!")
    diag = attach_page_diagnostics(page)
    page.evaluate(
        """({ cartKey, item }) => {
            localStorage.setItem('mall_last_buyer_v1', JSON.stringify({
                name: '비회원', phone: '010-0000-0000', zipcode: '99999',
                address: '부산광역시', address_detail: '99호'
            }));
            localStorage.setItem(cartKey, JSON.stringify([item]));
        }""",
        {
            "cartKey": MALL_CART_KEY,
            "item": {
                "num": p1["num"],
                "name": p1["name"],
                "price": p1["price"],
                "consumer_price": p1["consumer_price"],
                "image_url": "",
                "color": "블랙",
                "size": "M",
                "quantity": 1,
            },
        },
    )
    page.goto(f"{srv.base_url}/checkout", wait_until="domcontentloaded")
    expect(page.locator("#coZipcode")).to_have_value("06236")

    page.goto(f"{srv.base_url}/product/1", wait_until="domcontentloaded")
    page.get_by_role("button", name="바로 구매하기").click()
    expect(page.locator("#ibZipcode")).to_have_value("06236")
    diag.assert_clean("f3_member_overrides_guest_zip")


@pytest.mark.tier2
@pytest.mark.f3
def test_f3_register_rate_limit_and_invalid_password_preserves_zipcode(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    c = api_client_factory(srv)
    reg_page = c.get("/auth/register")
    assert reg_page.status_code == 200
    csrf = extract_csrf_from_html(reg_page.text)

    bad_pw_resp = c.post(
        "/auth/register",
        data={
            "csrf_token": csrf,
            "email": "weakpw@e2e.local",
            "password": "short1",
            "name": "약한비번",
            "phone": "010-1111-2222",
            "zipcode": "13529",
            "address": "경기도 성남시",
            "address_detail": "1층",
            "agree_terms": "on",
            "agree_privacy": "on",
        },
        include_csrf=False,
    )
    assert bad_pw_resp.status_code == 400
    assert 'value="13529"' in bad_pw_resp.text

    # Trigger rate limit (8/60s per IP on /auth/register)
    hit_429 = False
    for idx in range(10):
        r = c.post(
            "/auth/register",
            json={
                "email": f"rl_{idx}@e2e.local",
                "password": "ValidPass123!",
                "name": "속도제한",
                "phone": "010-1111-2222",
                "agree_terms": True,
                "agree_privacy": True,
            },
            allow_rate_limit=True,
        )
        if r.status_code == 429:
            hit_429 = True
            break
    assert hit_429, "Expected /auth/register rate limit (HTTP 429) after >8 requests"


# =============================================================================
# F4 Tier 2: Input validation, price tamper resistance, empty cart, order snapshot
# =============================================================================


@pytest.mark.tier2
@pytest.mark.f4
def test_f4_admin_store_info_rejects_negative_bool_float_string_overflow_shipping(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    _update_store_info(srv, api_client_factory, {"shipping_fee": 3000, "free_shipping_threshold": 50000})

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)

    for bad_fee in (-1, True, False, 12.5, "abc", 1_000_001):
        resp = admin.post("/api/admin/store-info", json={"shipping_fee": bad_fee})
        assert resp.status_code == 400, f"Expected 400 for shipping_fee={bad_fee!r}, got {resp.status_code}"

    for bad_th in (-1, True, False, 99.9, "xyz", 100_000_001):
        resp = admin.post("/api/admin/store-info", json={"free_shipping_threshold": bad_th})
        assert resp.status_code == 400, f"Expected 400 for threshold={bad_th!r}, got {resp.status_code}"

    info = admin.json_or_raise(admin.get("/api/admin/store-info"))["store_info"]
    assert info["shipping_fee"] == 3000
    assert info["free_shipping_threshold"] == 50000


@pytest.mark.tier2
@pytest.mark.f4
def test_f4_client_tampered_cart_price_cannot_bypass_server_shipping(
    server_factory,
    api_client_factory,
    desktop_page: Page,
):
    srv = server_factory(seed_products=True)
    _update_store_info(
        srv,
        api_client_factory,
        {"shipping_fee": 4000, "free_shipping_threshold": 50000},
    )

    diag = attach_page_diagnostics(desktop_page)
    # Tamper client cart price to 100,000 to try tricking free shipping threshold (>= 50,000)
    _seed_cart_local_storage(
        desktop_page,
        srv.base_url,
        [
            {
                "num": 1,
                "name": "골라방 클래식 코튼 티셔츠",
                "price": 100000,
                "consumer_price": 100000,
                "image_url": "",
                "color": "블랙",
                "size": "M",
                "quantity": 1,
            }
        ],
    )
    desktop_page.goto(f"{srv.base_url}/checkout", wait_until="domcontentloaded")
    desktop_page.locator("#coBuyerName").fill("가격위변조테스트")
    desktop_page.locator("#coBuyerPhone").fill("010-2222-4444")
    desktop_page.locator("#coZipcode").fill("06236")
    desktop_page.locator("#coAddress").fill("서울특별시 강남구 테헤란로 152")
    desktop_page.locator("#coGuestPassword").fill("tamper44")
    desktop_page.locator("#coSubmitBtn").click()
    desktop_page.wait_for_url("**/order-complete/**")
    expect(desktop_page.locator("#orderCompleteCode")).to_be_visible()
    desktop_page.locator('a[href="/orders"]').first.click()
    desktop_page.wait_for_url("**/orders")

    order_code = desktop_page.locator(".ax-order-card .ax-code-chip").first.inner_text().strip()
    clean = api_client_factory(srv)
    lookup = clean.post(
        "/api/orders/lookup",
        json={
            "order_code": order_code,
            "buyer_phone": "010-2222-4444",
            "guest_password": "tamper44",
        },
        include_csrf=False,
    )
    ord_obj = clean.json_or_raise(lookup)["orders"][0]
    # Server uses real DB price (29,000 < 50,000), so shipping_fee must be 4,000 and total 33,000
    assert ord_obj["subtotal_amount"] == 29000
    assert ord_obj["shipping_fee"] == 4000
    assert ord_obj["total_amount"] == 33000
    diag.assert_clean("f4_tamper_price_ignored")


@pytest.mark.tier2
@pytest.mark.f4
def test_f4_empty_cart_shows_zero_and_checkout_redirects_to_cart(
    server_factory,
    api_client_factory,
    desktop_page: Page,
):
    srv = server_factory(seed_products=True)
    _update_store_info(srv, api_client_factory, {"shipping_fee": 3500, "free_shipping_threshold": 50000})

    diag = attach_page_diagnostics(desktop_page)
    _seed_cart_local_storage(desktop_page, srv.base_url, [])
    desktop_page.goto(f"{srv.base_url}/cart", wait_until="domcontentloaded")
    expect(desktop_page.locator("#cartSubtotalText")).to_have_text("0원")
    expect(desktop_page.locator("#cartShippingText")).to_have_text("0원")
    expect(desktop_page.locator("#cartTotalText")).to_have_text("0원")
    assert "ax-is-hidden" in (desktop_page.locator("#cartShippingGuide").get_attribute("class") or "")

    desktop_page.goto(f"{srv.base_url}/checkout", wait_until="domcontentloaded")
    desktop_page.wait_for_url("**/cart")
    diag.assert_clean("f4_empty_cart")


@pytest.mark.tier2
@pytest.mark.f4
def test_f4_order_snapshot_shipping_fee_unchanged_when_admin_updates_later(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=True)
    _update_store_info(srv, api_client_factory, {"shipping_fee": 2500, "free_shipping_threshold": 0})

    member = api_client_factory(srv)
    member.register("snapshot@e2e.local", "SnapPass123!")
    ord_res = create_order_via_api(
        member,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
    )
    assert ord_res["shipping_fee"] == 2500

    # Admin raises shipping fee later
    _update_store_info(srv, api_client_factory, {"shipping_fee": 6000, "free_shipping_threshold": 0})

    lookup = member.json_or_raise(member.get("/api/orders/lookup"))["orders"]
    assert len(lookup) == 1
    assert lookup[0]["shipping_fee"] == 2500
    assert lookup[0]["total_amount"] == 31500

    ctx = desktop_context_factory()
    page = _login_browser_context(ctx, srv.base_url, "snapshot@e2e.local", "SnapPass123!")
    diag = attach_page_diagnostics(page)
    page.goto(f"{srv.base_url}/orders", wait_until="domcontentloaded")
    expect(page.locator(".ax-order-total-row").first).to_contain_text("택배비 2,500원")
    expect(page.locator(".ax-order-total-row").first).to_contain_text("31,500원")
    diag.assert_clean("f4_order_snapshot")


@pytest.mark.tier2
@pytest.mark.f4
def test_f4_empty_order_rejection_and_zero_fee_not_reverted(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    _update_store_info(srv, api_client_factory, {"shipping_fee": 0, "free_shipping_threshold": 0})

    guest = api_client_factory(srv)
    empty_resp = guest.post(
        "/api/orders/create",
        json={
            "buyer_name": "빈주문",
            "buyer_phone": "010-1234-5678",
            "address": "서울시 강남구",
            "items": [],
        },
    )
    assert empty_resp.status_code == 400

    valid_ord = create_order_via_api(
        guest,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
    )
    assert valid_ord["shipping_fee"] == 0
    assert valid_ord["total_amount"] == 29000


# =============================================================================
# F5 Tier 2: BVA (49999/50000/50001), threshold=0, fee=0, cart delete, mobile layout
# =============================================================================


@pytest.mark.tier2
@pytest.mark.f5
def test_f5_threshold_exact_boundary_minus1_exact_plus1(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=False)
    _update_store_info(
        srv,
        api_client_factory,
        {"shipping_fee": 3000, "free_shipping_threshold": 50000},
    )
    for num, price in ((101, 49999), (102, 50000), (103, 50001)):
        seed_product_via_sync(
            srv.base_url,
            srv.sync_secret,
            num=num,
            name=f"경계값상품_{price}",
            category="TOP",
            price=price,
            options=[{"color": "블랙", "size": "M", "stock": 10}],
        )

    guest = api_client_factory(srv)
    o_below = create_order_via_api(guest, items=[{"num": 101, "color": "블랙", "size": "M", "quantity": 1}])
    o_exact = create_order_via_api(guest, items=[{"num": 102, "color": "블랙", "size": "M", "quantity": 1}])
    o_above = create_order_via_api(guest, items=[{"num": 103, "color": "블랙", "size": "M", "quantity": 1}])

    assert o_below["shipping_fee"] == 3000 and o_below["total_amount"] == 52999
    assert o_exact["shipping_fee"] == 0 and o_exact["total_amount"] == 50000
    assert o_above["shipping_fee"] == 0 and o_above["total_amount"] == 50001


@pytest.mark.tier2
@pytest.mark.f5
def test_f5_threshold_zero_disables_free_shipping_and_hides_guide(
    server_factory,
    api_client_factory,
    desktop_page: Page,
):
    srv = server_factory(seed_products=True)
    _update_store_info(
        srv,
        api_client_factory,
        {"shipping_fee": 3000, "free_shipping_threshold": 0},
    )
    guest = api_client_factory(srv)
    ord_high = create_order_via_api(
        guest,
        items=[{"num": 3, "color": "차콜", "size": "M", "quantity": 3}],  # 89,000 * 3 = 267,000
    )
    assert ord_high["subtotal_amount"] == 267000
    assert ord_high["shipping_fee"] == 3000

    p3 = guest.json_or_raise(guest.get("/api/products/3"))["product"]
    diag = attach_page_diagnostics(desktop_page)
    _seed_cart_local_storage(
        desktop_page,
        srv.base_url,
        [
            {
                "num": p3["num"],
                "name": p3["name"],
                "price": p3["price"],
                "consumer_price": p3["consumer_price"],
                "image_url": "",
                "color": "차콜",
                "size": "M",
                "quantity": 3,
            }
        ],
    )
    desktop_page.goto(f"{srv.base_url}/cart", wait_until="domcontentloaded")
    assert "ax-is-hidden" in (desktop_page.locator("#cartShippingGuide").get_attribute("class") or "")
    expect(desktop_page.locator("#cartShippingText")).to_have_text("3,000원")

    desktop_page.goto(f"{srv.base_url}/checkout", wait_until="domcontentloaded")
    assert "ax-is-hidden" in (desktop_page.locator("#coShippingGuide").get_attribute("class") or "")
    expect(desktop_page.locator("#coShippingAmount")).to_have_text("+3,000원")

    desktop_page.goto(f"{srv.base_url}/product/3", wait_until="domcontentloaded")
    desktop_page.get_by_role("button", name="바로 구매하기").click()
    assert "ax-is-hidden" in (desktop_page.locator("#ibShippingGuide").get_attribute("class") or "")
    diag.assert_clean("f5_threshold_zero_disabled")


@pytest.mark.tier2
@pytest.mark.f5
def test_f5_shipping_fee_zero_with_positive_threshold_always_free(
    server_factory,
    api_client_factory,
    desktop_page: Page,
):
    srv = server_factory(seed_products=True)
    _update_store_info(
        srv,
        api_client_factory,
        {"shipping_fee": 0, "free_shipping_threshold": 50000},
    )
    guest = api_client_factory(srv)
    ord_low = create_order_via_api(
        guest,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],  # 29,000 < 50,000
    )
    assert ord_low["shipping_fee"] == 0
    assert ord_low["total_amount"] == 29000

    p1 = guest.json_or_raise(guest.get("/api/products/1"))["product"]
    diag = attach_page_diagnostics(desktop_page)
    _seed_cart_local_storage(
        desktop_page,
        srv.base_url,
        [
            {
                "num": p1["num"],
                "name": p1["name"],
                "price": p1["price"],
                "consumer_price": p1["consumer_price"],
                "image_url": "",
                "color": "블랙",
                "size": "M",
                "quantity": 1,
            }
        ],
    )
    desktop_page.goto(f"{srv.base_url}/cart", wait_until="domcontentloaded")
    expect(desktop_page.locator("#cartShippingText")).to_have_text("무료배송 (0원)")
    expect(desktop_page.locator("#cartShippingGuide")).to_contain_text("무료배송 적용 (0원)")
    assert "더 담으면" not in desktop_page.locator("#cartShippingGuide").inner_text()
    diag.assert_clean("f5_fee_zero_with_threshold")


@pytest.mark.tier2
@pytest.mark.f5
def test_f5_cart_item_removal_to_empty_hides_guide_and_resets_totals(
    server_factory,
    api_client_factory,
    desktop_page: Page,
):
    srv = server_factory(seed_products=True)
    _update_store_info(
        srv,
        api_client_factory,
        {"shipping_fee": 3000, "free_shipping_threshold": 50000},
    )
    p1 = api_client_factory(srv).json_or_raise(
        api_client_factory(srv).get("/api/products/1")
    )["product"]

    diag = attach_page_diagnostics(desktop_page)
    _seed_cart_local_storage(
        desktop_page,
        srv.base_url,
        [
            {
                "num": p1["num"],
                "name": p1["name"],
                "price": p1["price"],
                "consumer_price": p1["consumer_price"],
                "image_url": "",
                "color": "블랙",
                "size": "M",
                "quantity": 1,
            }
        ],
    )
    desktop_page.goto(f"{srv.base_url}/cart", wait_until="domcontentloaded")
    expect(desktop_page.locator("#cartShippingGuide")).to_be_visible()

    desktop_page.locator("#cartItemsContainer").get_by_role("button", name="삭제", exact=True).click()
    expect(desktop_page.locator("#cartSubtotalText")).to_have_text("0원")
    expect(desktop_page.locator("#cartShippingText")).to_have_text("0원")
    expect(desktop_page.locator("#cartTotalText")).to_have_text("0원")
    assert "ax-is-hidden" in (desktop_page.locator("#cartShippingGuide").get_attribute("class") or "")
    diag.assert_clean("f5_cart_remove_to_empty")


@pytest.mark.tier2
@pytest.mark.f5
def test_f5_cart_checkout_mobile_layout_and_no_internal_scroll_with_guide(
    server_factory,
    api_client_factory,
    desktop_context_factory,
    mobile_context_factory,
):
    srv = server_factory(seed_products=True)
    _update_store_info(
        srv,
        api_client_factory,
        {"shipping_fee": 3000, "free_shipping_threshold": 50000},
    )
    p1 = api_client_factory(srv).json_or_raise(
        api_client_factory(srv).get("/api/products/1")
    )["product"]

    for label, ctx_factory in [("desktop", desktop_context_factory), ("mobile", mobile_context_factory)]:
        ctx = ctx_factory()
        page = ctx.new_page()
        diag = attach_page_diagnostics(page)
        for qty in (1, 2):
            _seed_cart_local_storage(
                page,
                srv.base_url,
                [
                    {
                        "num": p1["num"],
                        "name": p1["name"],
                        "price": p1["price"],
                        "consumer_price": p1["consumer_price"],
                        "image_url": "",
                        "color": "블랙",
                        "size": "M",
                        "quantity": qty,
                    }
                ],
            )
            for route in ["/cart", "/checkout"]:
                page.goto(f"{srv.base_url}{route}", wait_until="domcontentloaded")
                assert_no_horizontal_overflow(page)
                assert_no_main_internal_vertical_scroll(page)
                diag.assert_clean(f"{label}:{route}:qty={qty}")
