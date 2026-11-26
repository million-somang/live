"""Tier 1 opaque-box E2E tests for R1 Foundation & Dynamic Shipping (F1–F5)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from playwright.sync_api import BrowserContext, Page, expect

from e2e.helpers import (
    MallApiClient,
    attach_page_diagnostics,
    create_order_via_api,
    mock_daum_postcode,
    seed_product_via_sync,
)

MALL_CART_KEY = "gollabang_mall_cart_v1"


def _login_browser_context(
    context: BrowserContext,
    base_url: str,
    email: str,
    password: str,
) -> Page:
    """Authenticate a Playwright context against any ServerHandle via /auth/login."""
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
    """Seed localStorage['gollabang_mall_cart_v1'] on origin before visiting /cart or /checkout."""
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.evaluate(
        """({ key, items }) => {
            localStorage.setItem(key, JSON.stringify(items));
        }""",
        {"key": MALL_CART_KEY, "items": items},
    )


def _cart_item_from_product(
    product: dict[str, Any],
    *,
    color: str | None = None,
    size: str | None = None,
    quantity: int = 1,
) -> dict[str, Any]:
    opts = product.get("options") or [{"color": "블랙", "size": "M", "stock": 10}]
    opt = opts[0]
    return {
        "num": int(product["num"]),
        "name": product["name"],
        "price": int(product["price"]),
        "consumer_price": int(product.get("consumer_price") or product["price"]),
        "image_url": product.get("image_url") or "",
        "color": color or opt["color"],
        "size": size or opt["size"],
        "quantity": quantity,
    }


def _update_store_info(
    server,
    api_client_factory,
    updates: dict[str, Any],
) -> dict[str, Any]:
    admin = api_client_factory(server)
    admin.login(server.admin_email, server.admin_password)
    resp = admin.post("/api/admin/store-info", json=updates)
    assert resp.status_code == 200, resp.text
    data = admin.json_or_raise(resp, "update_store_info")
    assert data["status"] == "ok"
    return data["store_info"]


# =============================================================================
# F1: Bank Account Dynamic Binding (store_info -> base, footer, policy, checkout, modal, orders)
# =============================================================================


@pytest.mark.tier1
@pytest.mark.f1
def test_f1_admin_ui_updates_store_bank_and_persists_in_api(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=True)
    ctx = desktop_context_factory()
    page = _login_browser_context(ctx, srv.base_url, srv.admin_email, srv.admin_password)
    diag = attach_page_diagnostics(page)

    page.goto(f"{srv.base_url}/admin", wait_until="domcontentloaded")
    page.locator("#adminTabStore").click()
    expect(page.locator("#adminPanelStore")).to_be_visible()

    page.locator("#asBankName").fill("신한은행")
    page.locator("#asBankAccount").fill("110-555-777888")
    page.locator("#asBankHolder").fill("(주)골라방테스트")
    page.locator("#btnSaveAdminStoreInfo").click()

    expect(page.locator("#mallToastContainer .ax-toast")).to_contain_text("저장")
    expect(page.locator("#adminStoreSaveHint")).to_contain_text("저장 완료")

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    info_resp = admin.get("/api/admin/store-info")
    assert info_resp.status_code == 200
    info = admin.json_or_raise(info_resp)["store_info"]
    assert info["bank_name"] == "신한은행"
    assert info["bank_account"] == "110-555-777888"
    assert info["bank_holder"] == "(주)골라방테스트"

    page.reload(wait_until="domcontentloaded")
    page.locator("#adminTabStore").click()
    expect(page.locator("#asBankName")).to_have_value("신한은행")
    expect(page.locator("#asBankAccount")).to_have_value("110-555-777888")
    expect(page.locator("#asBankHolder")).to_have_value("(주)골라방테스트")
    diag.assert_clean("f1_admin_ui_bank_update")


@pytest.mark.tier1
@pytest.mark.f1
def test_f1_updated_bank_renders_in_footer_and_policy_pages(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=True)
    _update_store_info(
        srv,
        api_client_factory,
        {
            "bank_name": "우리은행",
            "bank_account": "1002-999-112233",
            "bank_holder": "골라방주식회사",
        },
    )

    ctx = desktop_context_factory()
    page = ctx.new_page()
    diag = attach_page_diagnostics(page)

    for route in ["/", "/policy/terms", "/policy/guide"]:
        diag.clear()
        resp = page.goto(f"{srv.base_url}{route}", wait_until="domcontentloaded")
        assert resp is not None and resp.status == 200
        body = page.locator("body").inner_text()
        assert "우리은행" in body
        assert "1002-999-112233" in body
        assert "골라방주식회사" in body
        assert "3333 233 094904" not in body
        diag.assert_clean(f"f1_footer_policy:{route}")


@pytest.mark.tier1
@pytest.mark.f1
def test_f1_updated_bank_renders_in_checkout_and_copy_toast(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=True)
    _update_store_info(
        srv,
        api_client_factory,
        {
            "bank_name": "국민은행",
            "bank_account": "004-8888-999900",
            "bank_holder": "김골라",
        },
    )

    client = api_client_factory(srv)
    p1 = client.json_or_raise(client.get("/api/products/1"))["product"]

    ctx = desktop_context_factory()
    page = ctx.new_page()
    diag = attach_page_diagnostics(page)
    _seed_cart_local_storage(page, srv.base_url, [_cart_item_from_product(p1)])

    page.goto(f"{srv.base_url}/checkout", wait_until="domcontentloaded")
    opt_text = page.locator('#coPaymentMethod option[value="BANK"]').inner_text()
    assert "국민은행" in opt_text
    assert "004-8888-999900" in opt_text
    assert "김골라" in opt_text

    bank_box = page.locator("#checkoutForm .ax-bank-box")
    expect(bank_box).to_contain_text("국민은행")
    expect(bank_box).to_contain_text("004-8888-999900")
    expect(bank_box).to_contain_text("김골라")

    bank_box.get_by_role("button", name="계좌번호 복사").click()
    toast = page.locator("#mallToastContainer .ax-toast").last
    expect(toast).to_contain_text("국민은행")
    expect(toast).to_contain_text("004-8888-999900")
    expect(toast).to_contain_text("김골라")
    diag.assert_clean("f1_checkout_bank")


@pytest.mark.tier1
@pytest.mark.f1
def test_f1_updated_bank_renders_in_instant_buy_modal_and_order_toast(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=True)
    _update_store_info(
        srv,
        api_client_factory,
        {
            "bank_name": "하나은행",
            "bank_account": "222-333444-55501",
            "bank_holder": "박라이브",
        },
    )

    ctx = desktop_context_factory()
    page = ctx.new_page()
    diag = attach_page_diagnostics(page)

    page.goto(f"{srv.base_url}/product/1", wait_until="domcontentloaded")
    page.get_by_role("button", name="바로 구매하기").click()

    modal = page.locator("#instantBuyModal")
    expect(modal).to_be_visible()
    expect(modal.locator('#ibPaymentMethod option[value="BANK"]')).to_contain_text("하나은행")
    expect(modal.locator(".ax-bank-box")).to_contain_text("222-333444-55501")
    expect(modal.locator(".ax-bank-box")).to_contain_text("박라이브")

    modal.locator(".ax-bank-box").get_by_role("button", name="계좌 복사").click()
    expect(page.locator("#mallToastContainer .ax-toast").last).to_contain_text("222-333444-55501")

    page.locator("#ibBuyerName").fill("바로구매테스트")
    page.locator("#ibBuyerPhone").fill("010-2222-3333")
    page.locator("#ibZipcode").fill("06236")
    page.locator("#ibAddress").fill("서울특별시 강남구 테헤란로 152")
    page.locator("#ibAddressDetail").fill("5층")
    page.locator("#ibGuestPassword").fill("guest4321")
    page.locator("#ibSubmitBtn").click()

    order_toast = page.locator("#mallToastContainer .ax-toast", has_text="주문 완료")
    expect(order_toast).to_contain_text("하나은행")
    expect(order_toast).to_contain_text("222-333444-55501")
    expect(order_toast).to_contain_text("박라이브")
    diag.assert_clean("f1_instant_buy_bank")


@pytest.mark.tier1
@pytest.mark.f1
def test_f1_updated_bank_renders_in_orders_page_for_member_and_guest(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=True)
    _update_store_info(
        srv,
        api_client_factory,
        {
            "bank_name": "기업은행",
            "bank_account": "010-7777-6666-01",
            "bank_holder": "이운영",
        },
    )

    member = api_client_factory(srv)
    member.register("f1_member@e2e.local", "MemberPass123!")
    create_order_via_api(
        member,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_name="F1회원",
    )

    guest_creator = api_client_factory(srv)
    guest_ord = create_order_via_api(
        guest_creator,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_name="F1비회원",
        buyer_phone="010-9191-8282",
        guest_password="gpass9191",
    )

    # Member view on /orders
    m_ctx = desktop_context_factory()
    m_page = _login_browser_context(m_ctx, srv.base_url, "f1_member@e2e.local", "MemberPass123!")
    m_diag = attach_page_diagnostics(m_page)
    m_page.goto(f"{srv.base_url}/orders", wait_until="domcontentloaded")
    m_bank_box = m_page.locator(".ax-order-card .ax-bank-box").first
    expect(m_bank_box).to_contain_text("기업은행")
    expect(m_bank_box).to_contain_text("010-7777-6666-01")
    expect(m_bank_box).to_contain_text("이운영")
    m_bank_box.get_by_role("button", name="계좌 복사").click()
    expect(m_page.locator("#mallToastContainer .ax-toast").last).to_contain_text("010-7777-6666-01")
    m_diag.assert_clean("f1_orders_member")

    # Clean guest 3-factor lookup on /orders
    g_ctx = desktop_context_factory()
    g_page = g_ctx.new_page()
    g_diag = attach_page_diagnostics(g_page)
    g_page.goto(f"{srv.base_url}/orders", wait_until="domcontentloaded")
    g_page.locator("#lookupOrderCode").fill(guest_ord["order_code"])
    g_page.locator("#lookupBuyerPhone").fill("010-9191-8282")
    g_page.locator("#lookupGuestPassword").fill("gpass9191")
    g_page.locator("#guestOrderLookupForm button[type='submit']").click()
    g_page.wait_for_load_state("domcontentloaded")

    g_bank_box = g_page.locator(".ax-order-card .ax-bank-box").first
    expect(g_bank_box).to_contain_text("기업은행")
    expect(g_bank_box).to_contain_text("010-7777-6666-01")
    expect(g_bank_box).to_contain_text("이운영")
    assert "3333 233 094904" not in g_bank_box.inner_text()
    g_diag.assert_clean("f1_orders_guest")


# =============================================================================
# F2: Related Products UI (product_detail.html)
# =============================================================================


@pytest.mark.tier1
@pytest.mark.f2
def test_f2_related_products_same_category_priority_and_max_four(
    server_factory,
    desktop_page: Page,
):
    srv = server_factory(seed_products=True)
    # Baseline already has TOP nums 1 and 2; add 4 more TOP products (21..24)
    for num in (21, 22, 23, 24):
        seed_product_via_sync(
            srv.base_url,
            srv.sync_secret,
            num=num,
            name=f"연관TOP상품_{num}",
            category="TOP",
            price=25000 + num * 100,
            options=[{"color": "화이트", "size": "FREE", "stock": 5}],
        )

    diag = attach_page_diagnostics(desktop_page)
    desktop_page.goto(f"{srv.base_url}/product/1", wait_until="domcontentloaded")

    expect(desktop_page.locator('a.ax-tab-btn[href="#section-related"]')).to_be_visible()
    section = desktop_page.locator("#section-related")
    expect(section).to_be_visible()
    expect(section.locator(".ax-section-head .ax-badge")).to_have_text("4개")

    cards = section.locator("article.ax-product-card")
    expect(cards).to_have_count(4)

    rendered_nums = [
        int(cards.nth(i).locator("[data-csr-stock-num]").get_attribute("data-csr-stock-num") or 0)
        for i in range(4)
    ]
    assert 1 not in rendered_nums
    assert rendered_nums == [2, 21, 22, 23]
    for i in range(4):
        assert cards.nth(i).locator("[data-csr-cat]").get_attribute("data-csr-cat") in (
            "상의/탑",
            "상의",
            "TOP",
        )
    diag.assert_clean("f2_same_category_max_4")


@pytest.mark.tier1
@pytest.mark.f2
def test_f2_related_products_cross_category_and_sold_out_fallback(
    server_factory,
    desktop_page: Page,
):
    srv = server_factory(seed_products=True)
    # Baseline has only 1 OUTER (#3). Add a sold-out OUTER (#31).
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=31,
        name="품절아우터테스트",
        category="OUTER",
        price=95000,
        options=[{"color": "블랙", "size": "M", "stock": 0}],
    )

    diag = attach_page_diagnostics(desktop_page)
    desktop_page.goto(f"{srv.base_url}/product/3", wait_until="domcontentloaded")

    cards = desktop_page.locator("#section-related article.ax-product-card")
    expect(cards).to_have_count(4)
    rendered_nums = [
        int(cards.nth(i).locator("[data-csr-stock-num]").get_attribute("data-csr-stock-num") or 0)
        for i in range(4)
    ]
    # Same-category sold-out (#31) must still rank before cross-category in-stock (#1, #2, #4)
    assert rendered_nums == [31, 1, 2, 4]
    diag.assert_clean("f2_cross_category_and_sold_out")


@pytest.mark.tier1
@pytest.mark.f2
def test_f2_related_products_show_regular_price_never_live_price(
    server_factory,
    desktop_page: Page,
):
    srv = server_factory(seed_products=True)
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=32,
        name="라이브가격분리테스트상의",
        category="TOP",
        price=35000,
        consumer_price=50000,
        live_price=19000,
        options=[{"color": "네이비", "size": "L", "stock": 7}],
    )

    diag = attach_page_diagnostics(desktop_page)
    desktop_page.goto(f"{srv.base_url}/product/1", wait_until="domcontentloaded")

    card = desktop_page.locator(
        "#section-related article.ax-product-card",
        has=desktop_page.locator('[data-csr-stock-num="32"]'),
    )
    expect(card).to_be_visible()
    card_text = card.inner_text()
    assert "35,000원" in card_text
    assert "50,000원" in card_text
    assert "30%" in card_text
    assert "19,000원" not in card_text
    assert "LIVE 특가" not in card_text
    diag.assert_clean("f2_regular_price_only")


@pytest.mark.tier1
@pytest.mark.f2
def test_f2_related_products_cart_buy_and_link_navigation(
    live_server,
    desktop_page: Page,
):
    diag = attach_page_diagnostics(desktop_page)
    desktop_page.goto(f"{live_server.base_url}/product/1", wait_until="domcontentloaded")

    first_card = desktop_page.locator("#section-related article.ax-product-card").first
    target_num = int(first_card.locator("[data-csr-stock-num]").get_attribute("data-csr-stock-num") or 0)
    assert target_num != 1

    # Add to cart via related card button
    first_card.locator(f'button[data-product-num="{target_num}"][onclick*="relatedCartByNum"]').click()
    expect(desktop_page.locator("#navCartBadge")).to_have_text("1")
    cart_raw = desktop_page.evaluate(
        "(k) => localStorage.getItem(k)",
        MALL_CART_KEY,
    )
    cart_items = json.loads(cart_raw or "[]")
    assert len(cart_items) == 1 and int(cart_items[0]["num"]) == target_num

    # Instant buy via related card button
    first_card.locator(f'button[data-product-num="{target_num}"][onclick*="relatedBuyByNum"]').click()
    expect(desktop_page.locator("#instantBuyModal")).to_be_visible()
    expect(desktop_page.locator("#instantBuyItemSummary")).to_contain_text(f"#{target_num}")
    desktop_page.locator("#instantBuyModal button[aria-label='주문서 닫기']").click()
    expect(desktop_page.locator("#instantBuyModal")).to_be_hidden()

    # Title link navigation
    first_card.locator("a.ax-product-title").click()
    desktop_page.wait_for_url(f"**/product/{target_num}")
    expect(desktop_page.locator("h1")).to_be_visible()
    diag.assert_clean("f2_cart_buy_nav")


@pytest.mark.tier1
@pytest.mark.f2
def test_f2_related_products_wishlist_and_sse_stock_hook(
    server_factory,
    desktop_page: Page,
):
    srv = server_factory(seed_products=True)
    diag = attach_page_diagnostics(desktop_page)

    desktop_page.goto(f"{srv.base_url}/product/1", wait_until="domcontentloaded")
    wish_btn = desktop_page.locator('#section-related .ax-wish-btn[data-wish-num="2"]')
    expect(wish_btn).to_be_visible()
    wish_btn.click()
    expect(wish_btn).to_have_class(r"ax-wish-btn is-active")
    expect(desktop_page.locator("#navWishlistBadge")).to_have_text("1")

    stock_el = desktop_page.locator('#section-related [data-csr-stock-num="2"]')
    expect(stock_el).to_contain_text("잔여 12개")

    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=2,
        name="골라방 데일리 리브드 니트",
        category="TOP",
        price=42000,
        consumer_price=54000,
        live_price=36000,
        options=[{"color": "아이보리", "size": "FREE", "stock": 4}],
    )
    expect(stock_el).to_contain_text("잔여 4개", timeout=7000)
    diag.assert_clean("f2_wishlist_and_sse")


# =============================================================================
# F3: Zipcode & Daum Postcode Integration
# =============================================================================


@pytest.mark.tier1
@pytest.mark.f3
def test_f3_register_form_daum_postcode_and_member_zipcode_persistence(
    server_factory,
    api_client_factory,
    desktop_page: Page,
):
    srv = server_factory(seed_products=True)
    diag = attach_page_diagnostics(desktop_page)
    mock_daum_postcode(desktop_page, zipcode="04524", address="서울특별시 중구 세종대로 110")

    desktop_page.goto(f"{srv.base_url}/auth/register", wait_until="domcontentloaded")
    desktop_page.locator("#regEmail").fill("f3_reg@e2e.local")
    desktop_page.locator("#regPassword").fill("RegPass123!")
    desktop_page.locator("#regName").fill("우편번호회원")
    desktop_page.locator("#regPhone").fill("010-3030-4040")
    desktop_page.locator("#btnRegPostcode").click()

    expect(desktop_page.locator("#regZipcode")).to_have_value("04524")
    expect(desktop_page.locator("#regAddress")).to_have_value("서울특별시 중구 세종대로 110")
    desktop_page.locator("#regAddressDetail").fill("본관 2층")
    desktop_page.locator("#regAgreeAll").check()
    desktop_page.get_by_role("button", name="회원가입 완료하기").click()
    desktop_page.wait_for_url(f"{srv.base_url}/")

    # Verify persisted zipcode prefills both /checkout and #instantBuyModal
    p1 = api_client_factory(srv).json_or_raise(
        api_client_factory(srv).get("/api/products/1")
    )["product"]
    desktop_page.evaluate(
        "({ k, items }) => localStorage.setItem(k, JSON.stringify(items))",
        {"k": MALL_CART_KEY, "items": [_cart_item_from_product(p1)]},
    )
    desktop_page.goto(f"{srv.base_url}/checkout", wait_until="domcontentloaded")
    expect(desktop_page.locator("#coZipcode")).to_have_value("04524")
    expect(desktop_page.locator("#coAddress")).to_have_value("서울특별시 중구 세종대로 110")

    desktop_page.goto(f"{srv.base_url}/product/1", wait_until="domcontentloaded")
    desktop_page.get_by_role("button", name="바로 구매하기").click()
    expect(desktop_page.locator("#ibZipcode")).to_have_value("04524")
    diag.assert_clean("f3_register_daum_postcode")


@pytest.mark.tier1
@pytest.mark.f3
def test_f3_register_get_zipcode_query_prefill_and_json_api(
    server_factory,
    api_client_factory,
    desktop_page: Page,
):
    srv = server_factory(seed_products=True)
    diag = attach_page_diagnostics(desktop_page)
    desktop_page.goto(f"{srv.base_url}/auth/register?zipcode=13529", wait_until="domcontentloaded")
    expect(desktop_page.locator("#regZipcode")).to_have_value("13529")
    diag.assert_clean("f3_register_query_prefill")

    client = api_client_factory(srv)
    reg = client.register(
        "f3_json@e2e.local",
        "JsonPass123!",
        zipcode="13529",
        address="경기도 성남시 분당구 판교역로 166",
        address_detail="A동",
    )
    assert reg["user"]["zipcode"] == "13529"
    co_html = client.get("/checkout").text
    assert 'value="13529"' in co_html


@pytest.mark.tier1
@pytest.mark.f3
def test_f3_checkout_daum_postcode_order_and_orders_display(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=True)
    p1 = api_client_factory(srv).json_or_raise(
        api_client_factory(srv).get("/api/products/1")
    )["product"]

    ctx = desktop_context_factory()
    page = ctx.new_page()
    diag = attach_page_diagnostics(page)
    mock_daum_postcode(page, zipcode="06236", address="서울특별시 강남구 테헤란로 152")
    _seed_cart_local_storage(page, srv.base_url, [_cart_item_from_product(p1)])

    page.goto(f"{srv.base_url}/checkout", wait_until="domcontentloaded")
    page.locator("#coBuyerName").fill("체크아웃우편")
    page.locator("#coBuyerPhone").fill("010-5151-6262")
    page.locator("#btnCoPostcode").click()
    expect(page.locator("#coZipcode")).to_have_value("06236")
    expect(page.locator("#coAddress")).to_have_value("서울특별시 강남구 테헤란로 152")
    page.locator("#coAddressDetail").fill("1001호")
    page.locator("#coGuestPassword").fill("zip5151")
    page.locator("#coSubmitBtn").click()

    page.wait_for_url("**/order-complete/**")
    expect(page.locator("#orderCompleteCode")).to_be_visible()
    page.locator('a[href="/orders"]').first.click()
    page.wait_for_url("**/orders")
    expect(page.locator(".ax-order-card")).to_be_visible()
    expect(page.locator(".ax-order-card-meta").first).to_contain_text("[06236]")
    order_code = page.locator(".ax-order-card .ax-code-chip").first.inner_text().strip()

    clean_client = api_client_factory(srv)
    lookup = clean_client.post(
        "/api/orders/lookup",
        json={
            "order_code": order_code,
            "buyer_phone": "010-5151-6262",
            "guest_password": "zip5151",
        },
        include_csrf=False,
    )
    assert lookup.status_code == 200
    orders = clean_client.json_or_raise(lookup)["orders"]
    assert len(orders) == 1
    assert orders[0]["zipcode"] == "06236"
    diag.assert_clean("f3_checkout_postcode")


@pytest.mark.tier1
@pytest.mark.f3
def test_f3_instant_buy_daum_postcode_and_admin_order_display(
    live_server,
    api_client_factory,
    desktop_context_factory,
    admin_storage_state,
):
    ctx = desktop_context_factory()
    page = ctx.new_page()
    diag = attach_page_diagnostics(page)
    mock_daum_postcode(page, zipcode="07320", address="서울특별시 영등포구 여의대로 108")

    page.goto(f"{live_server.base_url}/product/1", wait_until="domcontentloaded")
    page.get_by_role("button", name="바로 구매하기").click()
    expect(page.locator("#instantBuyModal")).to_be_visible()

    page.locator("#ibBuyerName").fill("바로우편")
    page.locator("#ibBuyerPhone").fill("010-7320-1111")
    page.locator("#btnIbPostcode").click()
    expect(page.locator("#ibZipcode")).to_have_value("07320")
    expect(page.locator("#ibAddress")).to_have_value("서울특별시 영등포구 여의대로 108")
    page.locator("#ibAddressDetail").fill("파크원 12층")
    page.locator("#ibGuestPassword").fill("ib7320")
    page.locator("#ibSubmitBtn").click()

    page.wait_for_url("**/order-complete/**", timeout=10000)
    expect(page.locator("#orderCompleteCode")).to_be_visible()
    page.locator('a[href="/orders"]').first.click()
    page.wait_for_url("**/orders")
    expect(page.locator(".ax-order-card-meta").first).to_contain_text("[07320]")
    order_code = page.locator(".ax-order-card .ax-code-chip").first.inner_text().strip()
    diag.assert_clean("f3_instant_buy_postcode_guest")

    # Check admin orders tab renders [07320]
    admin_ctx = desktop_context_factory(storage_state=admin_storage_state)
    admin_page = admin_ctx.new_page()
    admin_diag = attach_page_diagnostics(admin_page)
    admin_page.goto(f"{live_server.base_url}/admin", wait_until="domcontentloaded")
    admin_page.locator("#adminTabOrders").click()
    card = admin_page.locator("#adminOrdersList .ax-admin-order-card", has_text=order_code)
    expect(card).to_be_visible()
    expect(card).to_contain_text("[07320]")
    admin_diag.assert_clean("f3_admin_order_zipcode")


@pytest.mark.tier1
@pytest.mark.f3
def test_f3_guest_last_buyer_localstorage_prefill_across_checkout_and_modal(
    live_server,
    api_client_factory,
    desktop_context_factory,
):
    p1 = api_client_factory(live_server).json_or_raise(
        api_client_factory(live_server).get("/api/products/1")
    )["product"]

    ctx = desktop_context_factory()
    page = ctx.new_page()
    diag = attach_page_diagnostics(page)
    _seed_cart_local_storage(page, live_server.base_url, [_cart_item_from_product(p1)])

    page.goto(f"{live_server.base_url}/checkout", wait_until="domcontentloaded")
    page.locator("#coBuyerName").fill("재방문비회원")
    page.locator("#coBuyerPhone").fill("010-8080-9090")
    page.locator("#coZipcode").fill("06236")
    page.locator("#coAddress").fill("서울특별시 강남구 테헤란로 152")
    page.locator("#coAddressDetail").fill("901호")
    page.locator("#coGuestPassword").fill("guest8080")
    page.locator("#coSubmitBtn").click()
    page.wait_for_url("**/order-complete/**")

    saved_buyer = page.evaluate("() => JSON.parse(localStorage.getItem('mall_last_buyer_v1') || '{}')")
    assert saved_buyer.get("zipcode") == "06236"

    # Re-seed cart and verify /checkout & #instantBuyModal prefill zipcode
    _seed_cart_local_storage(page, live_server.base_url, [_cart_item_from_product(p1)])
    page.goto(f"{live_server.base_url}/checkout", wait_until="domcontentloaded")
    expect(page.locator("#coZipcode")).to_have_value("06236")

    page.goto(f"{live_server.base_url}/product/1", wait_until="domcontentloaded")
    page.get_by_role("button", name="바로 구매하기").click()
    expect(page.locator("#ibZipcode")).to_have_value("06236")
    diag.assert_clean("f3_last_buyer_prefill")


# =============================================================================
# F4: Global 4,000 KRW Hardcode Removal (Dynamic shipping_fee)
# =============================================================================


@pytest.mark.tier1
@pytest.mark.f4
def test_f4_custom_shipping_fee_replaces_4000_in_api_order_and_orders_page(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=True)
    _update_store_info(
        srv,
        api_client_factory,
        {"shipping_fee": 2500, "free_shipping_threshold": 0},
    )

    member = api_client_factory(srv)
    member.register("f4_ship@e2e.local", "ShipPass123!")
    order = create_order_via_api(
        member,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
    )
    assert order["subtotal_amount"] == 29000
    assert order["shipping_fee"] == 2500
    assert order["total_amount"] == 31500

    ctx = desktop_context_factory()
    page = _login_browser_context(ctx, srv.base_url, "f4_ship@e2e.local", "ShipPass123!")
    diag = attach_page_diagnostics(page)
    page.goto(f"{srv.base_url}/orders", wait_until="domcontentloaded")
    total_row = page.locator(".ax-order-total-row").first
    expect(total_row).to_contain_text("택배비 2,500원")
    expect(total_row).to_contain_text("31,500원")
    assert "4,000원" not in total_row.inner_text()
    diag.assert_clean("f4_orders_custom_fee")


@pytest.mark.tier1
@pytest.mark.f4
def test_f4_custom_shipping_fee_reflected_in_cart_html(
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
    p1 = api_client_factory(srv).json_or_raise(
        api_client_factory(srv).get("/api/products/1")
    )["product"]

    diag = attach_page_diagnostics(desktop_page)
    _seed_cart_local_storage(desktop_page, srv.base_url, [_cart_item_from_product(p1)])
    desktop_page.goto(f"{srv.base_url}/cart", wait_until="domcontentloaded")

    expect(desktop_page.locator("#cartSubtotalText")).to_have_text("29,000원")
    expect(desktop_page.locator("#cartShippingText")).to_have_text("3,000원")
    expect(desktop_page.locator("#cartTotalText")).to_have_text("32,000원")
    diag.assert_clean("f4_cart_custom_fee")


@pytest.mark.tier1
@pytest.mark.f4
def test_f4_custom_shipping_fee_reflected_in_checkout_html(
    server_factory,
    api_client_factory,
    desktop_page: Page,
):
    srv = server_factory(seed_products=True)
    _update_store_info(
        srv,
        api_client_factory,
        {"shipping_fee": 3500, "free_shipping_threshold": 0},
    )
    p1 = api_client_factory(srv).json_or_raise(
        api_client_factory(srv).get("/api/products/1")
    )["product"]

    diag = attach_page_diagnostics(desktop_page)
    _seed_cart_local_storage(desktop_page, srv.base_url, [_cart_item_from_product(p1)])
    desktop_page.goto(f"{srv.base_url}/checkout", wait_until="domcontentloaded")

    expect(desktop_page.locator("#coSubtotalAmount")).to_have_text("29,000원")
    expect(desktop_page.locator("#coShippingAmount")).to_have_text("+3,500원")
    expect(desktop_page.locator("#coBankShippingNotice")).to_contain_text("3,500원")
    expect(desktop_page.locator("#coTotalAmount")).to_have_text("32,500원")
    diag.assert_clean("f4_checkout_custom_fee")


@pytest.mark.tier1
@pytest.mark.f4
def test_f4_custom_shipping_fee_reflected_in_instant_buy_modal(
    server_factory,
    api_client_factory,
    desktop_page: Page,
):
    srv = server_factory(seed_products=True)
    _update_store_info(
        srv,
        api_client_factory,
        {"shipping_fee": 2000, "free_shipping_threshold": 0},
    )

    diag = attach_page_diagnostics(desktop_page)
    desktop_page.goto(f"{srv.base_url}/product/1", wait_until="domcontentloaded")
    desktop_page.get_by_role("button", name="바로 구매하기").click()

    expect(desktop_page.locator("#instantBuyModal")).to_be_visible()
    expect(desktop_page.locator("#instantBuyItemSummary")).to_contain_text("택배비 2,000원")
    expect(desktop_page.locator("#instantBuyItemSummary")).to_contain_text("31,000원")
    expect(desktop_page.locator("#ibBankShippingNotice")).to_contain_text("2,000원")
    diag.assert_clean("f4_instant_buy_custom_fee")


@pytest.mark.tier1
@pytest.mark.f4
def test_f4_zero_shipping_fee_persists_as_all_free_shipping(
    server_factory,
    api_client_factory,
    desktop_page: Page,
):
    srv = server_factory(seed_products=True)
    saved = _update_store_info(
        srv,
        api_client_factory,
        {"shipping_fee": 0, "free_shipping_threshold": 0},
    )
    assert saved["shipping_fee"] == 0

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    fetched = admin.json_or_raise(admin.get("/api/admin/store-info"))["store_info"]
    assert fetched["shipping_fee"] == 0

    guest = api_client_factory(srv)
    order = create_order_via_api(
        guest,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
    )
    assert order["shipping_fee"] == 0
    assert order["total_amount"] == 29000

    p1 = guest.json_or_raise(guest.get("/api/products/1"))["product"]
    diag = attach_page_diagnostics(desktop_page)
    _seed_cart_local_storage(desktop_page, srv.base_url, [_cart_item_from_product(p1)])

    desktop_page.goto(f"{srv.base_url}/cart", wait_until="domcontentloaded")
    expect(desktop_page.locator("#cartShippingText")).to_have_text("무료배송 (0원)")
    expect(desktop_page.locator("#cartTotalText")).to_have_text("29,000원")

    desktop_page.goto(f"{srv.base_url}/checkout", wait_until="domcontentloaded")
    expect(desktop_page.locator("#coShippingAmount")).to_have_text("무료배송 (0원)")
    expect(desktop_page.locator("#coTotalAmount")).to_have_text("29,000원")

    desktop_page.goto(f"{srv.base_url}/product/1", wait_until="domcontentloaded")
    desktop_page.get_by_role("button", name="바로 구매하기").click()
    expect(desktop_page.locator("#instantBuyItemSummary")).to_contain_text("무료배송 0원")
    expect(desktop_page.locator("#instantBuyItemSummary")).to_contain_text("29,000원")
    diag.assert_clean("f4_zero_shipping_fee")


# =============================================================================
# F5: Store Shipping Fee + Free-Shipping Threshold & Cart Remaining Guide
# =============================================================================


@pytest.mark.tier1
@pytest.mark.f5
def test_f5_admin_sets_shipping_fee_and_threshold_and_persists(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=True)
    ctx = desktop_context_factory()
    page = _login_browser_context(ctx, srv.base_url, srv.admin_email, srv.admin_password)
    diag = attach_page_diagnostics(page)

    page.goto(f"{srv.base_url}/admin", wait_until="domcontentloaded")
    page.locator("#adminTabStore").click()
    page.locator("#asShippingFee").fill("3000")
    page.locator("#asFreeShippingThreshold").fill("50000")
    page.locator("#btnSaveAdminStoreInfo").click()

    expect(page.locator("#mallToastContainer .ax-toast")).to_contain_text("저장")

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    info = admin.json_or_raise(admin.get("/api/admin/store-info"))["store_info"]
    assert info["shipping_fee"] == 3000
    assert info["free_shipping_threshold"] == 50000

    page.reload(wait_until="domcontentloaded")
    page.locator("#adminTabStore").click()
    expect(page.locator("#asShippingFee")).to_have_value("3000")
    expect(page.locator("#asFreeShippingThreshold")).to_have_value("50000")
    diag.assert_clean("f5_admin_threshold_persist")


@pytest.mark.tier1
@pytest.mark.f5
def test_f5_cart_below_threshold_shows_remaining_amount_and_updates_on_qty_increase(
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
    _seed_cart_local_storage(desktop_page, srv.base_url, [_cart_item_from_product(p1, quantity=1)])
    desktop_page.goto(f"{srv.base_url}/cart", wait_until="domcontentloaded")

    guide = desktop_page.locator("#cartShippingGuide")
    expect(guide).to_be_visible()
    expect(guide).to_contain_text("21,000원")
    expect(guide).to_contain_text("50,000원 이상 무료배송")
    expect(desktop_page.locator("#cartShippingText")).to_have_text("3,000원")
    expect(desktop_page.locator("#cartTotalText")).to_have_text("32,000원")

    desktop_page.locator('.ax-qty-btn[aria-label="수량 증가"]').click()
    expect(desktop_page.locator("#cartSubtotalText")).to_have_text("58,000원")
    expect(guide).to_contain_text("무료배송 적용 (0원)")
    expect(desktop_page.locator("#cartShippingText")).to_have_text("무료배송 (0원)")
    expect(desktop_page.locator("#cartTotalText")).to_have_text("58,000원")
    diag.assert_clean("f5_cart_threshold_guide")


@pytest.mark.tier1
@pytest.mark.f5
def test_f5_checkout_below_and_at_threshold_shipping(
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
    _seed_cart_local_storage(desktop_page, srv.base_url, [_cart_item_from_product(p1, quantity=1)])
    desktop_page.goto(f"{srv.base_url}/checkout", wait_until="domcontentloaded")
    expect(desktop_page.locator("#coShippingGuide")).to_contain_text("21,000원")
    expect(desktop_page.locator("#coShippingAmount")).to_have_text("+3,000원")
    expect(desktop_page.locator("#coTotalAmount")).to_have_text("32,000원")

    _seed_cart_local_storage(desktop_page, srv.base_url, [_cart_item_from_product(p1, quantity=2)])
    desktop_page.goto(f"{srv.base_url}/checkout", wait_until="domcontentloaded")
    expect(desktop_page.locator("#coShippingGuide")).to_contain_text("무료배송 적용 (0원)")
    expect(desktop_page.locator("#coShippingAmount")).to_have_text("무료배송 (0원)")
    expect(desktop_page.locator("#coTotalAmount")).to_have_text("58,000원")
    diag.assert_clean("f5_checkout_threshold")


@pytest.mark.tier1
@pytest.mark.f5
def test_f5_instant_buy_modal_threshold_updates_on_qty_change(
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

    diag = attach_page_diagnostics(desktop_page)
    desktop_page.goto(f"{srv.base_url}/product/1", wait_until="domcontentloaded")
    desktop_page.get_by_role("button", name="바로 구매하기").click()

    expect(desktop_page.locator("#ibShippingGuide")).to_contain_text("21,000원")
    expect(desktop_page.locator("#instantBuyItemSummary")).to_contain_text("32,000원")

    qty_input = desktop_page.locator("#ibQtyInput")
    qty_input.fill("2")
    qty_input.dispatch_event("change")

    expect(desktop_page.locator("#ibShippingGuide")).to_contain_text("무료배송 적용 (0원)")
    expect(desktop_page.locator("#instantBuyItemSummary")).to_contain_text("무료배송 0원")
    expect(desktop_page.locator("#instantBuyItemSummary")).to_contain_text("58,000원")
    diag.assert_clean("f5_instant_buy_threshold")


@pytest.mark.tier1
@pytest.mark.f5
def test_f5_order_create_applies_threshold_and_live_pricing_respects_threshold(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    _update_store_info(
        srv,
        api_client_factory,
        {"shipping_fee": 3000, "free_shipping_threshold": 50000},
    )

    guest = api_client_factory(srv)
    # Regular price #2 = 42,000 < 50,000 -> shipping 3,000
    below_ord = create_order_via_api(
        guest,
        items=[{"num": 2, "color": "아이보리", "size": "FREE", "quantity": 1}],
        source_page="CART_CHECKOUT",
    )
    assert below_ord["subtotal_amount"] == 42000
    assert below_ord["shipping_fee"] == 3000
    assert below_ord["total_amount"] == 45000

    # Regular price #1 * 2 = 58,000 >= 50,000 -> shipping 0
    above_ord = create_order_via_api(
        guest,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 2}],
        source_page="CART_CHECKOUT",
    )
    assert above_ord["subtotal_amount"] == 58000
    assert above_ord["shipping_fee"] == 0
    assert above_ord["total_amount"] == 58000

    # LIVE pricing #1 * 2 uses live_price=24,000 * 2 = 48,000 (< 50,000 threshold -> shipping 3,000)
    sync_resp = guest.sync_from_obs(
        {"action": "live_state", "is_live": True, "pinned_product_num": 1},
        srv.sync_secret,
    )
    assert sync_resp.status_code == 200
    live_ord = create_order_via_api(
        guest,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 2}],
        source_page="LIVE",
    )
    assert live_ord["subtotal_amount"] == 48000
    assert live_ord["shipping_fee"] == 3000
    assert live_ord["total_amount"] == 51000
