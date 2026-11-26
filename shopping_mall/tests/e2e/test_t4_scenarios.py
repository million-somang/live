"""Tier 4 real-world multi-actor Playwright + HTTP scenarios (S1-S7).

Strictly opaque-box: zero internal imports (no ``mall_*``, ``app``, ``live_commerce``, or ``sqlite3``).
"""

from __future__ import annotations

import csv
import io
import json
import re
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page

from e2e.helpers import (
    MallApiClient,
    assert_no_horizontal_overflow,
    assert_no_main_internal_vertical_scroll,
    attach_page_diagnostics,
    create_order_via_api,
    create_tiny_png_bytes,
    mock_daum_postcode,
    seed_product_via_sync,
)


def _order_id(order: dict[str, Any]) -> int:
    return int(order.get("order_id") or order["id"])


def _cleanup_uploaded_review_png(image_url: str | None) -> None:
    if not image_url:
        return
    reviews_dir = (Path(__file__).resolve().parents[2] / "static" / "uploads" / "reviews").resolve()
    rel = str(image_url).split("?", 1)[0].lstrip("/")
    if not rel.startswith("static/uploads/reviews/"):
        return
    candidate = (Path(__file__).resolve().parents[2] / rel).resolve()
    try:
        candidate.relative_to(reviews_dir)
    except ValueError:
        return
    if candidate != reviews_dir and candidate.suffix.lower() == ".png":
        candidate.unlink(missing_ok=True)


def _submit_checkout_and_capture_order(page: Page) -> dict[str, Any]:
    captured: list[dict[str, Any]] = []

    def _capture_create(route: Any) -> None:
        resp = route.fetch()
        try:
            captured.append(resp.json())
        finally:
            route.fulfill(response=resp)

    page.route("**/api/orders/create", _capture_create)
    try:
        page.click("#coSubmitBtn")
        page.wait_for_url(re.compile(r"/(order-complete/ORD-|orders)"), timeout=10000)
    finally:
        page.unroute("**/api/orders/create", _capture_create)

    assert len(captured) == 1, f"Expected 1 /api/orders/create response, got {len(captured)}"
    return captured[0]


def _apply_checkout_saved_address(page: Page, target_zipcode: str, target_label: str) -> None:
    if page.locator("#coZipcode").input_value() == target_zipcode:
        return
    selects = page.locator("select")
    for idx in range(selects.count()):
        sel = selects.nth(idx)
        sel_id = (sel.get_attribute("id") or "").lower()
        if sel_id in ("cocashreceipttype", "copaymentmethod"):
            continue
        options_text = sel.locator("option").all_inner_texts()
        match_idx = next(
            (i for i, txt in enumerate(options_text) if target_zipcode in txt or target_label in txt),
            None,
        )
        if match_idx is not None:
            sel.select_option(index=match_idx)
            page.wait_for_timeout(150)
            break
    if page.locator("#coZipcode").input_value() != target_zipcode:
        addr_btn = page.locator(
            f"button:has-text('{target_zipcode}'), button:has-text('{target_label}'), "
            f"[data-zipcode='{target_zipcode}']"
        ).first
        if addr_btn.count() > 0:
            addr_btn.click()
            page.wait_for_timeout(150)
    assert page.locator("#coZipcode").input_value() == target_zipcode, (
        f"Expected #coZipcode={target_zipcode!r} from saved address book, "
        f"got {page.locator('#coZipcode').input_value()!r}"
    )


def _select_cart_row_option(page: Page, product_text: str, color: str, size: str) -> None:
    row = page.locator("#cartItemsContainer .ax-cart-row", has_text=product_text).first
    assert row.count() == 1, f"Cart row not found for {product_text!r}"
    selects = row.locator("select")
    assert selects.count() >= 1, f"Option select not found in cart row for {product_text!r}"
    if selects.count() == 1:
        options_text = selects.first.locator("option").all_inner_texts()
        target_idx = next(i for i, txt in enumerate(options_text) if color in txt and size in txt)
        selects.first.select_option(index=target_idx)
    else:
        selects.nth(0).select_option(color)
        selects.nth(1).select_option(size)
    page.wait_for_timeout(150)


def _attach_dialog_recorder(page: Page) -> list[str]:
    messages: list[str] = []

    def _on_dialog(dialog: Any) -> None:
        messages.append(dialog.message)
        dialog.accept()

    page.on("dialog", _on_dialog)
    return messages


def _login_browser(page: Page, base_url: str, email: str, password: str) -> None:
    page.goto(f"{base_url}/auth/login", wait_until="domcontentloaded")
    page.fill("#loginEmail", email)
    page.fill("#loginPassword", password)
    with page.expect_navigation(wait_until="domcontentloaded"):
        page.click("form[action='/auth/login'] button[type='submit']")


def _assert_brand_and_csrf_and_layout(page: Page) -> None:
    body_text = page.locator("body").inner_text()
    html = page.content()
    assert "골라방 LIVE" in body_text
    assert "THE VINE LIVE MALL" not in body_text
    assert "THE VINE LIVE MALL" not in html
    assert "mall_light_theme.css" not in html
    assert page.locator('link[href*="astryx_mall.css"]').count() >= 1

    csrf_state = page.evaluate(
        """() => {
            const meta = document.querySelector('meta[name="csrf-token"]');
            const metaVal = meta ? (meta.getAttribute('content') || '') : '';
            const winVal = window.__CSRF_TOKEN__ || '';
            const postForms = Array.from(document.querySelectorAll('form[method="POST" i], form[method="post"]'));
            const missingForms = postForms
                .filter(f => {
                    const inp = f.querySelector('input[name="csrf_token"]');
                    return !inp || !inp.value || inp.value.length < 16;
                })
                .map(f => f.id || f.action || 'form');
            return { metaVal, winVal, missingForms };
        }"""
    )
    assert len(csrf_state["metaVal"]) >= 32, f"Missing/short CSRF meta on {page.url}"
    assert csrf_state["metaVal"] == csrf_state["winVal"], f"CSRF meta/window mismatch on {page.url}"
    assert not csrf_state["missingForms"], f"POST forms missing csrf_token on {page.url}: {csrf_state['missingForms']}"

    assert_no_horizontal_overflow(page)
    assert_no_main_internal_vertical_scroll(page)


def _assert_global_hooks(page: Page) -> None:
    hooks = page.evaluate(
        """() => ({
            ids: [
                '#instantBuyModal',
                '#instantBuyForm',
                '#navCartBadge',
                '#mobileTabCartBadge',
                '#navWishlistBadge',
                '#mobileTabWishlistBadge',
                '#mallNavLiveSlot',
                '#mallMobileLiveSlot',
                '#mallBaseBootstrap'
            ].filter(sel => !document.querySelector(sel)),
            funcs: [
                '__mallApplyLiteState',
                'showMallToast',
                'openInstantBuyModal',
                'getMallCart',
                'saveMallCart'
            ].filter(fn => typeof window[fn] !== 'function')
        })"""
    )
    assert not hooks["ids"], f"Missing preserved DOM IDs on {page.url}: {hooks['ids']}"
    assert not hooks["funcs"], f"Missing preserved JS hooks on {page.url}: {hooks['funcs']}"


def _seed_cart_in_browser(page: Page, items: list[dict[str, Any]]) -> None:
    page.evaluate("items => window.saveMallCart(items)", items)


def _get_product_stock(client: MallApiClient, num: int) -> int:
    resp = client.get(f"/api/products/{num}")
    assert resp.status_code == 200
    return int(client.json_or_raise(resp)["product"]["total_stock"])


# S1: R1 Store Config -> Guest Zipcode/Bank/Cash-Receipt Purchase -> Order Complete & 3-Factor Lookup
@pytest.mark.tier4
@pytest.mark.f1
@pytest.mark.f2
@pytest.mark.f3
@pytest.mark.f4
@pytest.mark.f5
@pytest.mark.f10
def test_t4_01_r1_store_config_to_guest_purchase_and_order_view(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=True)

    # Actor 1: Admin configures bank info, shipping fee=3000, free_shipping_threshold=50000 via /admin UI
    admin_ctx = desktop_context_factory()
    admin_page = admin_ctx.new_page()
    admin_diag = attach_page_diagnostics(admin_page)
    _attach_dialog_recorder(admin_page)
    _login_browser(admin_page, srv.base_url, srv.admin_email, srv.admin_password)
    admin_page.goto(f"{srv.base_url}/admin", wait_until="domcontentloaded")
    admin_page.click("#adminTabStore")
    admin_page.fill("#asBankName", "국민은행")
    admin_page.fill("#asBankAccount", "123456-01-998877")
    admin_page.fill("#asBankHolder", "골라방스토어")
    admin_page.fill("#asShippingFee", "3000")
    admin_page.fill("#asFreeShippingThreshold", "50000")
    with admin_page.expect_response(lambda r: "/api/admin/store-info" in r.url and r.request.method == "POST") as si_resp:
        admin_page.click("#btnSaveAdminStoreInfo")
    assert si_resp.value.status == 200
    admin_diag.assert_clean("s1_admin")

    # Actor 2: Guest checks footer, related products, instant-buy postcode/guide, cart threshold, and checkout
    guest_ctx = desktop_context_factory()
    guest_page = guest_ctx.new_page()
    guest_diag = attach_page_diagnostics(guest_page)
    _attach_dialog_recorder(guest_page)
    mock_daum_postcode(guest_page, zipcode="06236", address="서울특별시 강남구 테헤란로 152")

    guest_page.goto(f"{srv.base_url}/", wait_until="domcontentloaded")
    home_text = guest_page.locator("body").inner_text()
    assert "국민은행" in home_text
    assert "123456-01-998877" in home_text

    guest_page.goto(f"{srv.base_url}/product/1", wait_until="domcontentloaded")
    related = guest_page.locator("#section-related")
    assert related.is_visible()
    assert related.locator(".ax-product-card").count() <= 4
    assert "골라방 데일리 리브드 니트" in related.inner_text()
    assert "24,000원" not in guest_page.locator(".ax-detail-price-box").inner_text()

    # Check instant buy modal postcode & free-shipping remaining guide
    guest_page.get_by_role("button", name="바로 구매하기").click()
    assert guest_page.locator("#instantBuyModal").is_visible()
    guest_page.click("#btnIbPostcode")
    assert guest_page.locator("#ibZipcode").input_value() == "06236"
    assert "21,000원" in guest_page.locator("#ibShippingGuide").inner_text()
    assert "국민은행" in guest_page.locator("#instantBuyModal").inner_text()
    guest_page.keyboard.press("Escape")

    # Add #1 (29,000) to cart -> /cart -> verify 3,000 shipping & 21,000 remaining -> increment to 2 -> 0 shipping
    guest_page.get_by_role("button", name="장바구니 담기").click()
    guest_page.goto(f"{srv.base_url}/cart", wait_until="domcontentloaded")
    assert "3,000원" in guest_page.locator("#cartShippingText").inner_text()
    assert "21,000원" in guest_page.locator("#cartShippingGuide").inner_text()

    guest_page.locator('button[aria-label="수량 증가"]').first.click()
    assert "무료배송 (0원)" in guest_page.locator("#cartShippingText").inner_text()
    assert "58,000원" in guest_page.locator("#cartTotalText").inner_text()

    # Proceed to /checkout -> postcode -> fill depositor/cash-receipt/guest-pw -> submit
    guest_page.click("#btnGoCheckout")
    guest_page.wait_for_url("**/checkout")
    assert "무료배송 (0원)" in guest_page.locator("#coShippingAmount").inner_text()
    assert "58,000원" in guest_page.locator("#coTotalAmount").inner_text()

    guest_page.fill("#coBuyerName", "김시나리오1")
    guest_page.fill("#coBuyerPhone", "010-1111-0001")
    guest_page.click("#btnCoPostcode")
    assert guest_page.locator("#coZipcode").input_value() == "06236"
    guest_page.fill("#coAddressDetail", "101호")
    guest_page.fill("#coGuestPassword", "s1pass99")

    dep_input = guest_page.locator('input[name="depositor_name"], #coDepositorName').first
    assert dep_input.count() == 1
    dep_input.fill("김입금1")

    cr_type = guest_page.locator('select[name="cash_receipt_type"], #coCashReceiptType').first
    if cr_type.count() == 1:
        cr_type.select_option("INCOME")
    else:
        guest_page.locator('input[name="cash_receipt_type"][value="INCOME"]').check()

    cr_no = guest_page.locator('input[name="cash_receipt_no"], #coCashReceiptNo').first
    assert cr_no.count() == 1
    cr_no.fill("01011110001")

    co_json = _submit_checkout_and_capture_order(guest_page)
    assert co_json["status"] == "ok"
    order = co_json["order"]
    order_code = order["order_code"]
    assert order["zipcode"] == "06236"
    assert int(order["shipping_fee"]) == 0
    assert int(order["total_amount"]) == 58000

    # Ensure we are on /order-complete/<order_code>
    if "/order-complete/" not in guest_page.url:
        guest_page.goto(f"{srv.base_url}/order-complete/{order_code}", wait_until="domcontentloaded")
    comp_text = guest_page.locator("body").inner_text()
    assert order_code in comp_text
    assert "국민은행" in comp_text
    assert "123456-01-998877" in comp_text
    assert "골라방스토어" in comp_text
    assert "김입금1" in comp_text
    assert "01011110001" in comp_text
    assert guest_page.get_by_role("button", name="주문번호 복사").count() >= 1 or "복사" in comp_text
    guest_diag.assert_clean("s1_guest")

    # Actor 3: Clean stranger context verifies isolation + 3-factor lookup on /orders
    stranger_ctx = desktop_context_factory()
    stranger_page = stranger_ctx.new_page()
    stranger_diag = attach_page_diagnostics(stranger_page)
    blocked_resp = stranger_page.goto(f"{srv.base_url}/order-complete/{order_code}", wait_until="domcontentloaded")
    if blocked_resp is not None and blocked_resp.status >= 400:
        stranger_diag.clear()
    else:
        assert (
            "/orders" in stranger_page.url
            or "/auth" in stranger_page.url
            or "김시나리오1" not in stranger_page.locator("body").inner_text()
        )

    stranger_page.goto(f"{srv.base_url}/orders", wait_until="domcontentloaded")
    assert order_code not in stranger_page.locator("body").inner_text()

    # Wrong guest password fails
    stranger_page.fill("#lookupOrderCode", order_code)
    stranger_page.fill("#lookupBuyerPhone", "010-1111-0001")
    stranger_page.fill("#lookupGuestPassword", "wrongpw")
    with stranger_page.expect_navigation(wait_until="domcontentloaded"):
        stranger_page.click("#guestOrderLookupForm button[type='submit']")
    assert order_code not in stranger_page.locator("body").inner_text() or "일치하는 주문" in stranger_page.locator("body").inner_text()

    # Valid 3-factor lookup displays zipcode, dynamic bank, free shipping, total
    stranger_page.fill("#lookupOrderCode", order_code)
    stranger_page.fill("#lookupBuyerPhone", "010-1111-0001")
    stranger_page.fill("#lookupGuestPassword", "s1pass99")
    with stranger_page.expect_navigation(wait_until="domcontentloaded"):
        stranger_page.click("#guestOrderLookupForm button[type='submit']")
    ord_body = stranger_page.locator("body").inner_text()
    assert "[06236]" in ord_body
    assert "국민은행" in ord_body
    assert "123456-01-998877" in ord_body
    assert "무료배송 0원" in ord_body
    assert "58,000원" in ord_body
    stranger_diag.assert_clean("s1_stranger")


# S2: Logistics CSV Export/Import -> 1-Click Tracking -> Self-Cancel & Exchange/Refund Claim Lifecycle
@pytest.mark.tier4
@pytest.mark.f6
@pytest.mark.f7
@pytest.mark.f12
def test_t4_02_logistics_csv_tracking_and_self_cancel_claim(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=True)
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)

    guest = api_client_factory(srv)
    order_a = create_order_via_api(
        guest,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 2}],
        buyer_name="비회원A",
        buyer_phone="010-2000-0001",
        zipcode="06236",
        guest_password="s2guestA",
    )

    member = api_client_factory(srv)
    member.register("s2_member@e2e.local", "S2Member123!", name="S2회원", phone="010-2000-0002")
    stock_4_before = _get_product_stock(member, 4)
    stock_5_before = _get_product_stock(member, 5)
    stock_6_before = _get_product_stock(member, 6)

    order_b = create_order_via_api(
        member,
        items=[{"num": 4, "color": "블랙", "size": "S", "quantity": 3}],
        buyer_name="S2회원",
        buyer_phone="010-2000-0002",
    )
    order_c = create_order_via_api(
        member,
        items=[{"num": 6, "color": "블랙", "size": "ONE", "quantity": 2}],
        buyer_name="S2회원",
        buyer_phone="010-2000-0002",
    )
    order_d = create_order_via_api(
        member,
        items=[{"num": 5, "color": "네이비", "size": "FREE", "quantity": 2}],
        buyer_name="S2회원",
        buyer_phone="010-2000-0002",
    )
    assert _get_product_stock(member, 4) == stock_4_before - 3
    assert _get_product_stock(member, 5) == stock_5_before - 2
    assert _get_product_stock(member, 6) == stock_6_before - 2

    # Admin marks A, C & D paid
    assert admin.post("/api/admin/order-status", json={"order_id": _order_id(order_a), "payment_status": "입금완료"}).status_code == 200
    assert admin.post("/api/admin/order-status", json={"order_id": _order_id(order_c), "payment_status": "입금완료"}).status_code == 200
    assert admin.post("/api/admin/order-status", json={"order_id": _order_id(order_d), "payment_status": "입금완료"}).status_code == 200

    # Export shipping CSV and verify UTF-8 BOM + columns + Order A
    csv_resp = admin.get("/api/admin/orders/export-shipping.csv")
    assert csv_resp.status_code == 200
    assert csv_resp.content.startswith(b"\xef\xbb\xbf")
    csv_rows = list(csv.DictReader(io.StringIO(csv_resp.content.decode("utf-8-sig"))))
    row_a = next(r for r in csv_rows if r["order_code"] == order_a["order_code"])
    assert row_a["zipcode"] == "06236"
    for col in ("order_code", "buyer_name", "buyer_phone", "zipcode", "address", "items_summary", "carrier_code", "tracking_number"):
        assert col in row_a

    # Import tracking CSV for Order A
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["order_code", "carrier_code", "carrier_name", "tracking_number"])
    w.writerow([order_a["order_code"], "cj", "CJ대한통운", "123456789012"])
    imp_resp = admin.post(
        "/api/admin/orders/import-tracking",
        files={"file": ("s2_tracking.csv", buf.getvalue().encode("utf-8-sig"), "text/csv")},
    )
    assert imp_resp.status_code == 200, imp_resp.text

    # Single-order tracking for Order C -> mark shipped -> mark C & D delivered
    trk_c = admin.post(
        "/api/admin/orders/tracking",
        json={
            "order_id": _order_id(order_c),
            "order_code": order_c["order_code"],
            "carrier_code": "hanjin",
            "carrier_name": "한진택배",
            "tracking_number": "999888777666",
            "mark_shipped": True,
        },
    )
    assert trk_c.status_code == 200
    assert admin.post("/api/admin/order-status", json={"order_id": _order_id(order_c), "order_status": "배송완료"}).status_code == 200
    assert admin.post("/api/admin/order-status", json={"order_id": _order_id(order_d), "order_status": "배송완료"}).status_code == 200

    # Guest views Order A on /orders -> sees carrier, tracking number, 1-click tracking link
    guest_ctx = desktop_context_factory()
    guest_page = guest_ctx.new_page()
    guest_diag = attach_page_diagnostics(guest_page)
    guest_page.goto(f"{srv.base_url}/orders", wait_until="domcontentloaded")
    guest_page.fill("#lookupOrderCode", order_a["order_code"])
    guest_page.fill("#lookupBuyerPhone", "010-2000-0001")
    guest_page.fill("#lookupGuestPassword", "s2guestA")
    with guest_page.expect_navigation(wait_until="domcontentloaded"):
        guest_page.click("#guestOrderLookupForm button[type='submit']")
    g_body = guest_page.locator("body").inner_text()
    assert "CJ대한통운" in g_body
    assert "123456789012" in g_body
    assert guest_page.locator("a[target='_blank'][href*='123456789012'], a[target='_blank'][href*='cj']").count() >= 1
    guest_diag.assert_clean("s2_guest_tracking")

    # Member self-cancels Order B (unpaid) -> stock restored (+3), negative self-cancel on shipped/other fails
    mem_ctx = desktop_context_factory()
    mem_page = mem_ctx.new_page()
    mem_diag = attach_page_diagnostics(mem_page)
    _attach_dialog_recorder(mem_page)
    _login_browser(mem_page, srv.base_url, "s2_member@e2e.local", "S2Member123!")
    mem_page.goto(f"{srv.base_url}/orders", wait_until="domcontentloaded")

    cancel_b = member.post("/api/orders/self-cancel", json={"order_code": order_b["order_code"]})
    assert cancel_b.status_code == 200
    assert cancel_b.json()["order"]["order_status"] == "주문취소"
    assert _get_product_stock(member, 4) == stock_4_before

    # Negative self-cancel on delivered Order C/D & stranger's Order A
    assert member.post("/api/orders/self-cancel", json={"order_code": order_c["order_code"]}).status_code in (400, 403)
    assert member.post("/api/orders/self-cancel", json={"order_code": order_d["order_code"]}).status_code in (400, 403)
    assert member.post("/api/orders/self-cancel", json={"order_code": order_a["order_code"]}).status_code in (400, 403, 404)

    # Member submits exchange claim on Order C (duplicate self-claim rejected 400) & refund claim on Order D -> stock restored once
    ex_resp = member.post(
        "/api/orders/self-claim",
        json={"order_code": order_c["order_code"], "claim_type": "교환요청", "claim_reason": "색상 교환 희망"},
    )
    assert ex_resp.status_code == 200
    assert ex_resp.json()["order"]["order_status"] == "교환요청"

    dup_claim_resp = member.post(
        "/api/orders/self-claim",
        json={"order_code": order_c["order_code"], "claim_type": "환불요청", "claim_reason": "교환중 중복 클레임 차단"},
    )
    assert dup_claim_resp.status_code == 400
    assert _get_product_stock(member, 6) == stock_6_before - 2

    rf_resp = member.post(
        "/api/orders/self-claim",
        json={"order_code": order_d["order_code"], "claim_type": "환불요청", "claim_reason": "단순 변심 환불"},
    )
    assert rf_resp.status_code == 200
    assert rf_resp.json()["order"]["order_status"] == "환불요청"

    r_refund1 = admin.post("/api/admin/order-status", json={"order_id": _order_id(order_d), "payment_status": "환불완료", "order_status": "환불완료"})
    assert r_refund1.status_code == 200
    assert _get_product_stock(member, 5) == stock_5_before
    # Second refund call must not double-restore stock
    r_refund2 = admin.post("/api/admin/order-status", json={"order_id": _order_id(order_d), "payment_status": "환불완료", "order_status": "환불완료"})
    assert r_refund2.status_code in (200, 400)
    assert _get_product_stock(member, 5) == stock_5_before

    mem_page.reload(wait_until="domcontentloaded")
    mem_orders_text = mem_page.locator("body").inner_text()
    assert "주문취소" in mem_orders_text
    assert "교환요청" in mem_orders_text
    assert "환불완료" in mem_orders_text
    mem_diag.assert_clean("s2_member")

    # Verify notification logs recorded order, deposit, shipping, claim events with LOGGED_ONLY / non-error
    notif_dump = json.dumps(admin.get("/api/admin/notifications/logs").json(), ensure_ascii=False)
    assert order_a["order_code"] in notif_dump
    assert order_c["order_code"] in notif_dump
    assert order_d["order_code"] in notif_dump
    assert "123456789012" in notif_dump


# S3: Member Registration (Welcome Coupon) -> MyPage Address/Password/Reset -> Admin Suspend -> Withdraw
@pytest.mark.tier4
@pytest.mark.f3
@pytest.mark.f8
@pytest.mark.f9
@pytest.mark.f12
@pytest.mark.f14
def test_t4_03_member_lifecycle_mypage_reset_admin_suspend(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=True)
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)

    email = "s3_lifecycle@e2e.local"
    init_pw = "InitPass123!"
    changed_pw = "ChangedPass123!"
    reset_pw = "ResetPass123!"

    # Register via /auth/register UI with mocked Daum postcode
    mem_ctx = desktop_context_factory()
    page = mem_ctx.new_page()
    diag = attach_page_diagnostics(page)
    _attach_dialog_recorder(page)
    mock_daum_postcode(page, zipcode="06236", address="서울특별시 강남구 테헤란로 152")

    page.goto(f"{srv.base_url}/auth/register", wait_until="domcontentloaded")
    page.fill("#regEmail", email)
    page.fill("#regPassword", init_pw)
    page.fill("#regName", "S3라이프회원")
    page.fill("#regPhone", "010-3333-4444")
    page.click("#btnRegPostcode")
    assert page.locator("#regZipcode").input_value() == "06236"
    page.fill("#regAddressDetail", "101호")
    page.check("#regAgreeAll")
    with page.expect_navigation(wait_until="domcontentloaded"):
        page.click('form[action="/auth/register"] button[type="submit"]')

    # Verify welcome coupon issued via /mypage & API
    mem_api = api_client_factory(srv)
    mem_api.login(email, init_pw)
    coupons = mem_api.get("/api/mypage/coupons").json().get("coupons") or []
    assert len(coupons) >= 1

    # Profile & Address Book CRUD on /mypage
    assert mem_api.post(
        "/api/mypage/profile",
        json={"name": "S3수정회원", "phone": "010-3333-5555", "zipcode": "06236", "address": "서울특별시 강남구 테헤란로 152", "address_detail": "202호"},
    ).status_code == 200
    assert mem_api.post(
        "/api/mypage/addresses/save",
        json={"label": "집", "recipient_name": "S3수정회원", "recipient_phone": "010-3333-5555", "zipcode": "06236", "address": "서울특별시 강남구 테헤란로 152", "address_detail": "202호", "is_default": True},
    ).status_code == 200
    assert mem_api.post(
        "/api/mypage/addresses/save",
        json={"label": "판교오피스", "recipient_name": "S3수정회원", "recipient_phone": "010-3333-5555", "zipcode": "13494", "address": "경기도 성남시 분당구 판교역로 152", "address_detail": "8층", "is_default": True},
    ).status_code == 200

    addrs = mem_api.get("/api/mypage/addresses").json().get("addresses") or []
    addr_1 = next(a for a in addrs if a["zipcode"] == "06236")
    addr_2 = next(a for a in addrs if a["zipcode"] == "13494")
    assert mem_api.post("/api/mypage/addresses/default", json={"address_id": addr_2["id"], "id": addr_2["id"]}).status_code == 200
    assert mem_api.post("/api/mypage/addresses/delete", json={"address_id": addr_1["id"], "id": addr_1["id"]}).status_code == 200

    page.goto(f"{srv.base_url}/mypage", wait_until="domcontentloaded")
    assert page.locator("#mpZipcode").input_value() == "13494"
    page.click("#mypageTabAddresses")
    page.locator("#mypageAddressList").wait_for(state="visible")
    assert "13494" in page.locator("#mypageAddressList").inner_text()

    # Seed cart via window.saveMallCart -> verify default address 13494 on /checkout
    _seed_cart_in_browser(
        page,
        [{"num": 1, "name": "골라방 클래식 코튼 티셔츠", "price": 29000, "consumer_price": 39000, "image_url": "", "color": "블랙", "size": "M", "quantity": 1}],
    )
    page.goto(f"{srv.base_url}/checkout", wait_until="domcontentloaded")
    _apply_checkout_saved_address(page, "13494", "판교오피스")

    # Change password -> old password fails, new works
    pw_resp = mem_api.post(
        "/api/mypage/password",
        json={"current_password": init_pw, "new_password": changed_pw, "confirm_password": changed_pw},
    )
    assert pw_resp.status_code == 200, pw_resp.text

    test_login = api_client_factory(srv)
    assert test_login.post("/auth/login", json={"email": email, "password": init_pw}).status_code in (401, 403)
    test_login.login(email, changed_pw)

    # Forgot/Reset password flow via notification logs
    forgot_client = api_client_factory(srv)
    assert forgot_client.get("/auth/forgot-password").status_code == 200
    csrf_fg = forgot_client.refresh_csrf("/auth/forgot-password")
    fg_post = forgot_client.post("/auth/forgot-password", data={"csrf_token": csrf_fg, "email": email})
    assert fg_post.status_code == 200

    logs = admin.get("/api/admin/notifications/logs").json().get("logs") or []
    reset_logs = [lg for lg in logs if email in json.dumps(lg, ensure_ascii=False)]
    assert len(reset_logs) >= 1
    log_blob = json.dumps(reset_logs[-1], ensure_ascii=False)

    code_match = re.search(
        r"(?:코드|임시\s*코드|인증번호|인증\s*코드|code|temp_code)[^\d]{0,16}(\d{6})",
        log_blob,
        re.IGNORECASE,
    )
    token_match = re.search(r"token=([A-Za-z0-9._-]+)", log_blob)
    assert code_match or token_match, f"Could not find reset code/token in log: {log_blob}"

    reset_payload: dict[str, Any] = {
        "csrf_token": forgot_client.refresh_csrf("/auth/reset-password"),
        "email": email,
        "new_password": reset_pw,
        "confirm_password": reset_pw,
    }
    if code_match:
        reset_payload["code"] = code_match.group(1)
        reset_payload["temp_code"] = code_match.group(1)
    if token_match:
        reset_payload["token"] = token_match.group(1)
    rs_post = forgot_client.post("/auth/reset-password", data=reset_payload)
    assert rs_post.status_code == 200
    api_client_factory(srv).login(email, reset_pw)

    # Member places 1 order -> Admin checks /api/admin/users -> Suspends -> Reactivates
    order = create_order_via_api(
        test_login,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_name="S3수정회원",
        buyer_phone="010-3333-5555",
    )
    users = admin.get("/api/admin/users").json()["users"]
    u_row = next(u for u in users if u["email"] == email)
    assert int(u_row.get("order_count", 0)) >= 1
    assert int(u_row.get("total_spent", u_row.get("total_amount", 0))) >= int(order["total_amount"])

    assert admin.post("/api/admin/users/status", json={"user_id": u_row["id"], "is_active": False}).status_code == 200
    assert api_client_factory(srv).post("/auth/login", json={"email": email, "password": reset_pw}).status_code in (401, 403)
    assert admin.post("/api/admin/users/status", json={"user_id": u_row["id"], "is_active": True}).status_code == 200

    # Withdraw member -> permanently blocked
    mem_final = api_client_factory(srv)
    mem_final.login(email, reset_pw)
    assert mem_final.post("/api/mypage/withdraw", json={"password": reset_pw}).status_code == 200
    assert api_client_factory(srv).post("/auth/login", json={"email": email, "password": reset_pw}).status_code in (401, 403)
    diag.assert_clean("s3_member")


# S4: PG Adapter Prepare/Confirm -> Photo Review (Verified Badge/Edit/Moderate) -> Secret Q&A & CS Inquiry
@pytest.mark.tier4
@pytest.mark.f10
@pytest.mark.f11
@pytest.mark.f12
@pytest.mark.f13
def test_t4_04_pg_adapter_cs_qna_and_photo_review(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=False)
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=401,
        name="S4 포토리뷰 CS 상품",
        category="TOP",
        price=45000,
        options=[{"color": "블랙", "size": "M", "stock": 10}],
    )
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)

    buyer1 = api_client_factory(srv)
    buyer1.register("s4_buyer1@e2e.local", "Buyer1Pass123!", name="인증구매자1")
    buyer2 = api_client_factory(srv)
    buyer2.register("s4_buyer2@e2e.local", "Buyer2Pass123!", name="일반회원2")

    # PG Adapter prepare & confirm for Buyer 1
    pg_order = create_order_via_api(
        buyer1,
        items=[{"num": 401, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_name="인증구매자1",
        buyer_phone="010-4010-0001",
        payment_method="TOSS_PAYMENTS",
    )
    prep = buyer1.post(
        "/api/payments/prepare",
        json={
            "order_code": pg_order["order_code"],
            "payment_method": "TOSS_PAYMENTS",
        },
    )
    assert prep.status_code == 200, prep.text
    prep_data = buyer1.json_or_raise(prep, "s4_pg_prepare")
    prep_payload = prep_data.get("payment", prep_data)
    pay_key = (
        prep_payload.get("payment_key")
        or prep_payload.get("payment_id")
        or prep_payload.get("merchant_uid")
        or f"test_toss_{pg_order['order_code']}"
    )
    conf = buyer1.post(
        "/api/payments/confirm",
        json={
            "order_code": pg_order["order_code"],
            "payment_method": "TOSS_PAYMENTS",
            "payment_key": pay_key,
            "payment_id": pay_key,
            "merchant_uid": prep_payload.get("merchant_uid") or pay_key,
            "amount": pg_order["total_amount"],
        },
    )
    assert conf.status_code == 200, conf.text
    conf_data = buyer1.json_or_raise(conf, "s4_pg_confirm")
    conf_order = conf_data.get("order")
    order_target = (
        conf_order
        if isinstance(conf_order, dict) and ("order_id" in conf_order or "id" in conf_order)
        else pg_order
    )
    assert admin.post(
        "/api/admin/order-status",
        json={"order_id": _order_id(order_target), "payment_status": "입금완료", "order_status": "배송완료"},
    ).status_code == 200

    # Photo Review via Playwright UI (Buyer 1)
    uploaded_review_urls: list[str] = []

    def _on_review_upload_resp(resp: Any) -> None:
        if "/api/reviews/upload-image" in resp.url and resp.request.method == "POST":
            try:
                data = resp.json()
            except Exception:
                return
            if isinstance(data, dict) and data.get("url"):
                uploaded_review_urls.append(str(data["url"]))

    try:
        ctx1 = desktop_context_factory()
        page1 = ctx1.new_page()
        diag1 = attach_page_diagnostics(page1)
        _attach_dialog_recorder(page1)
        page1.on("response", _on_review_upload_resp)
        _login_browser(page1, srv.base_url, "s4_buyer1@e2e.local", "Buyer1Pass123!")
        page1.goto(f"{srv.base_url}/product/401", wait_until="domcontentloaded")

        file_input = page1.locator('#pdReviewForm input[type="file"]').first
        if file_input.count() == 1:
            file_input.set_input_files(
                {"name": "s4_review.png", "mimeType": "image/png", "buffer": create_tiny_png_bytes()}
            )
        else:
            up = buyer1.post("/api/reviews/upload-image", files={"image": ("s4_review.png", create_tiny_png_bytes(), "image/png")})
            assert up.status_code == 200
            up_url = up.json().get("url")
            if up_url:
                uploaded_review_urls.append(str(up_url))

        page1.fill("#pdReviewContent", "S4 포토리뷰 실제 구매 후기입니다.")
        with page1.expect_response(lambda r: "/api/reviews/add" in r.url and r.request.method == "POST") as rev_resp:
            page1.click("#pdReviewSubmitBtn")
        assert rev_resp.value.status == 200

        # Verify verified buyer badge & edit own review
        revs_1 = buyer1.get("/api/products/401").json()["reviews"]
        r1 = next(r for r in revs_1 if "S4 포토리뷰 실제 구매 후기" in r["content"])
        if r1.get("image_url"):
            uploaded_review_urls.append(str(r1["image_url"]))
        assert bool(r1.get("is_verified_buyer")) is True
        assert buyer1.post("/api/reviews/update", json={"review_id": r1["id"], "id": r1["id"], "rating": 5, "content": "S4 포토리뷰 수정된 후기입니다."}).status_code == 200

        # Buyer 2 blocked from editing/deleting Buyer 1's review, posts own unverified review
        assert buyer2.post("/api/reviews/update", json={"review_id": r1["id"], "id": r1["id"], "content": "타인수정시도"}).status_code == 403
        assert buyer2.post("/api/reviews/delete", json={"review_id": r1["id"], "id": r1["id"]}).status_code == 403
        r2_add = buyer2.post("/api/reviews/add", json={"product_num": 401, "rating": 3, "content": "S4 미구매 회원 후기입니다."})
        assert r2_add.status_code == 200
        revs_2 = buyer2.get("/api/products/401").json()["reviews"]
        r2 = next(r for r in revs_2 if "S4 미구매 회원 후기" in r["content"])
        assert bool(r2.get("is_verified_buyer")) is False

        # Admin replies to r1 & hides r2
        assert admin.post("/api/admin/reviews/moderate", json={"review_id": r1["id"], "id": r1["id"], "action": "reply", "admin_reply": "소중한 후기 감사합니다!"}).status_code == 200
        assert admin.post("/api/admin/reviews/moderate", json={"review_id": r2["id"], "id": r2["id"], "action": "hide", "is_hidden": True}).status_code == 200
        page1.reload(wait_until="domcontentloaded")
        rev_list_text = page1.locator("#pdReviewList").inner_text()
        assert "소중한 후기 감사합니다!" in rev_list_text
        assert "S4 미구매 회원 후기" not in rev_list_text

        # Secret Q&A + CS 1:1 Inquiry + Notice + FAQ
        qna_res = buyer1.post("/api/products/401/qna", json={"question": "S4-비밀질문-원문", "is_secret": True}).json()
        qna_id = (qna_res.get("qna") or qna_res.get("item") or {}).get("id") or qna_res.get("id")
        inq_res = buyer1.post("/api/inquiries/create", json={"category": "상품", "title": "S4-1대1문의", "content": "S4-문의상세"}).json()
        inq_id = (inq_res.get("inquiry") or inq_res.get("item") or {}).get("id") or inq_res.get("id")

        other_qna = json.dumps(buyer2.get("/api/products/401/qna").json(), ensure_ascii=False)
        assert "S4-비밀질문-원문" not in other_qna
        assert "비밀" in other_qna
        other_inq = json.dumps(buyer2.get("/api/inquiries").json(), ensure_ascii=False)
        assert "S4-1대1문의" not in other_inq

        assert admin.post("/api/admin/notices/save", json={"title": "S4-필독공지", "content": "공지내용", "is_active": True}).status_code == 200
        assert admin.post("/api/admin/faqs/save", json={"category": "배송", "question": "S4-자주묻는질문", "answer": "FAQ답변", "is_active": True}).status_code == 200
        assert admin.post("/api/admin/qna/answer", json={"id": qna_id, "qna_id": qna_id, "answer": "S4-비밀답변"}).status_code == 200
        assert admin.post("/api/admin/inquiries/answer", json={"id": inq_id, "inquiry_id": inq_id, "answer": "S4-1대1답변"}).status_code == 200

        page1.goto(f"{srv.base_url}/cs", wait_until="domcontentloaded")
        cs_text = page1.locator("body").inner_text()
        assert "S4-필독공지" in cs_text
        assert "S4-자주묻는질문" in cs_text
        assert "S4-1대1문의" in cs_text
        assert "S4-1대1답변" in cs_text
        assert_no_horizontal_overflow(page1)
        assert_no_main_internal_vertical_scroll(page1)
        diag1.assert_clean("s4_buyer1")

        notif_dump = json.dumps(admin.get("/api/admin/notifications/logs").json(), ensure_ascii=False)
        assert pg_order["order_code"] in notif_dump
    finally:
        for url in set(uploaded_review_urls):
            _cleanup_uploaded_review_png(url)


# S5: Catalog Filter/Pagination/Related/Recent Bar -> Cart Option Change & Select -> Price Revalidate Block -> Restock & SEO
@pytest.mark.tier4
@pytest.mark.f2
@pytest.mark.f4
@pytest.mark.f5
@pytest.mark.f15
@pytest.mark.f16
@pytest.mark.f17
def test_t4_05_discovery_cart_revalidate_restock_seo(
    server_factory,
    api_client_factory,
    desktop_context_factory,
    mobile_context_factory,
):
    uploads_dir = Path(__file__).resolve().parents[2] / "static" / "uploads" / "products"
    created_pngs = [uploads_dir / f"s5_{idx}.png" for idx in range(1, 15)]
    try:
        srv = server_factory(seed_products=False)
        admin = api_client_factory(srv)
        admin.login(srv.admin_email, srv.admin_password)
        admin.post(
            "/api/admin/store-info",
            json={**admin.get("/api/admin/store-info").json()["store_info"], "shipping_fee": 3000, "free_shipping_threshold": 50000},
        )

        for idx in range(1, 15):
            seed_product_via_sync(
                srv.base_url,
                srv.sync_secret,
                num=idx,
                name=f"S5 상품 {idx:02d}",
                category="TOP" if idx <= 6 else "OUTER",
                price=10000 + idx * 5000,
                options=[
                    {"color": "블랙", "size": "M", "stock": 10},
                    {"color": "화이트", "size": "L", "stock": 10},
                ],
                image_url=f"/static/uploads/products/s5_{idx}.png",
                image_bytes=create_tiny_png_bytes(),
            )
        seed_product_via_sync(
            srv.base_url,
            srv.sync_secret,
            num=15,
            name="S5 품절 테스트 상품",
            category="ACC",
            price=25000,
            options=[{"color": "네이비", "size": "ONE", "stock": 0}],
        )

        # Discovery filters & pagination via /api/products & /
        guest = api_client_factory(srv)
        ex_sold = guest.get("/api/products?exclude_sold_out=1").json()["products"]
        assert 15 not in {int(p["num"]) for p in ex_sold}
        ranged = guest.get("/api/products?min_price=25000&max_price=40000&exclude_sold_out=1").json()["products"]
        assert all(25000 <= int(p["price"]) <= 40000 for p in ranged)
        p1 = guest.get("/api/products?page=1&per_page=8").json()["products"]
        p2 = guest.get("/api/products?page=2&per_page=8").json()["products"]
        assert len(p1) == 8 and len(p2) >= 6
        assert {int(p["num"]) for p in p1}.isdisjoint({int(p["num"]) for p in p2})

        # Desktop: visit /product/1 & /product/2 (TOP), then /product/7 (OUTER) so #1/#2 are excluded from #section-related.
        d_ctx = desktop_context_factory()
        d_page = d_ctx.new_page()
        d_diag = attach_page_diagnostics(d_page)
        _attach_dialog_recorder(d_page)
        mock_daum_postcode(d_page, zipcode="06236", address="서울특별시 강남구 테헤란로 152")

        d_page.goto(f"{srv.base_url}/product/1", wait_until="domcontentloaded")
        assert d_page.locator("#section-related").is_visible()
        assert "S5 상품 02" in d_page.locator("#section-related").inner_text()
        d_page.get_by_role("button", name="장바구니 담기").click()

        d_page.goto(f"{srv.base_url}/product/2", wait_until="domcontentloaded")
        d_page.get_by_role("button", name="장바구니 담기").click()

        d_page.goto(f"{srv.base_url}/product/7", wait_until="domcontentloaded")
        related_7_text = d_page.locator("#section-related").inner_text()
        assert "S5 상품 01" not in related_7_text and "S5 상품 02" not in related_7_text
        recent_text = d_page.locator("body").inner_text()
        assert "S5 상품 01" in recent_text and "S5 상품 02" in recent_text

        # Mobile check for recently viewed bar without horizontal overflow or vertical scroll violations
        m_ctx = mobile_context_factory()
        m_page = m_ctx.new_page()
        m_diag = attach_page_diagnostics(m_page)
        m_page.goto(f"{srv.base_url}/product/1", wait_until="domcontentloaded")
        m_page.goto(f"{srv.base_url}/product/7", wait_until="domcontentloaded")
        assert_no_horizontal_overflow(m_page)
        assert_no_main_internal_vertical_scroll(m_page)
        m_diag.assert_clean("s5_mobile_recent")

        # Cart: change #1 option to 화이트/L, uncheck #2, proceed to /checkout
        with d_page.expect_response(lambda r: "/api/cart/revalidate" in r.url and r.request.method == "POST"):
            d_page.goto(f"{srv.base_url}/cart", wait_until="domcontentloaded")
        _select_cart_row_option(d_page, "S5 상품 01", "화이트", "L")
        row_2 = d_page.locator("#cartItemsContainer .ax-cart-row", has_text="S5 상품 02").first
        assert row_2.count() == 1
        cb_2 = row_2.locator("input[type='checkbox']").first
        assert cb_2.count() == 1
        cb_2.uncheck()
        d_page.wait_for_timeout(150)
        assert "15,000원" in d_page.locator("#cartSubtotalText").inner_text()

        d_page.click("#btnGoCheckout")
        d_page.wait_for_url("**/checkout")

        # Admin raises #1 price mid-checkout (15,000 -> 25,000)
        prod_1 = guest.get("/api/products/1").json()["product"]
        upd = admin.post(
            "/api/admin/products/update",
            json={
                "id": prod_1["id"],
                "num": 1,
                "name": prod_1["name"],
                "category": prod_1["category"],
                "price": 25000,
                "consumer_price": 39000,
                "live_price": 25000,
                "options": prod_1["options"],
            },
        )
        assert upd.status_code == 200, upd.text

        d_page.fill("#coBuyerName", "S5가격검증")
        d_page.fill("#coBuyerPhone", "010-5555-1515")
        d_page.click("#btnCoPostcode")
        d_page.fill("#coAddressDetail", "501호")
        d_page.fill("#coGuestPassword", "s5pass99")
        with d_page.expect_response(lambda r: "/api/cart/revalidate" in r.url and r.request.method == "POST") as reval_resp:
            with d_page.expect_response(lambda r: "/api/orders/create" in r.url and r.request.method == "POST") as blk_resp:
                d_page.click("#coSubmitBtn")
        assert blk_resp.value.status in (400, 409)
        assert reval_resp.value.status == 200
        d_page.wait_for_timeout(150)
        d_diag.clear()

        # Reload /checkout -> sees updated 25,000 price -> completes order -> #2 remains in cart!
        d_page.reload(wait_until="domcontentloaded")
        assert "25,000원" in d_page.locator("#coSubtotalAmount").inner_text()
        d_page.fill("#coBuyerName", "S5가격검증")
        d_page.fill("#coBuyerPhone", "010-5555-1515")
        d_page.click("#btnCoPostcode")
        d_page.fill("#coAddressDetail", "501호")
        d_page.fill("#coGuestPassword", "s5pass99")
        with d_page.expect_response(lambda r: "/api/orders/create" in r.url and r.request.method == "POST") as ok_resp:
            d_page.click("#coSubmitBtn")
        assert ok_resp.value.status == 200
        d_page.wait_for_url(re.compile(r"/(order-complete/ORD-|orders)"), timeout=10000)

        d_page.goto(f"{srv.base_url}/cart", wait_until="domcontentloaded")
        remaining_cart = d_page.evaluate("() => window.getMallCart()")
        assert len(remaining_cart) == 1
        assert int(remaining_cart[0]["num"]) == 2
        d_diag.assert_clean("s5_desktop")

        # Restock alert + SEO verification
        assert guest.post(
            "/api/restock-alerts/subscribe",
            json={"product_num": 15, "color": "네이비", "size": "ONE", "contact": "s5_restock@e2e.local", "email": "s5_restock@e2e.local"},
        ).status_code == 200
        seed_product_via_sync(
            srv.base_url,
            srv.sync_secret,
            num=15,
            name="S5 품절 테스트 상품",
            category="ACC",
            price=25000,
            options=[{"color": "네이비", "size": "ONE", "stock": 5}],
        )
        notif_dump = json.dumps(admin.get("/api/admin/notifications/logs").json(), ensure_ascii=False)
        assert "15" in notif_dump and ("네이비" in notif_dump or "재입고" in notif_dump or "RESTOCK" in notif_dump.upper())

        home_html = guest.get("/").text
        p1_html = guest.get("/product/1").text
        assert 'property="og:title"' in home_html or "property='og:title'" in home_html
        assert 'property="og:image"' in p1_html or "property='og:image'" in p1_html
        sm = guest.get("/sitemap.xml")
        assert sm.status_code == 200
        locs = [el.text or "" for el in ET.fromstring(sm.text).iter() if el.tag.endswith("loc")]
        assert any("/product/1" in u for u in locs)
        rb = guest.get("/robots.txt")
        assert rb.status_code == 200
        assert "Sitemap:" in rb.text
        for disallowed in ("/admin", "/mypage", "/orders"):
            assert f"Disallow: {disallowed}" in rb.text
    finally:
        for png_path in created_pngs:
            png_path.unlink(missing_ok=True)


# S6: Unpaid Order Auto-Cancel (Coupon/Point/Stock Restore) -> SQLite Backup Download -> Audit Logs & /live Separation
@pytest.mark.tier4
@pytest.mark.f14
@pytest.mark.f18
@pytest.mark.regression
def test_t4_06_ops_autocancel_backup_audit_and_live_separation(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    si = admin.get("/api/admin/store-info").json()["store_info"]
    si.update({"shipping_fee": 0, "free_shipping_threshold": 0, "unpaid_cancel_hours": 1, "point_earn_rate": 10})
    assert admin.post("/api/admin/store-info", json=si).status_code == 200

    assert admin.post(
        "/api/admin/coupons/save",
        json={"code": "S6LIVE10", "name": "S6 테스트 쿠폰", "discount_type": "FIXED", "discount_value": 5000, "min_order_amount": 10000, "is_active": True},
    ).status_code == 200

    member = api_client_factory(srv)
    member.register("s6_member@e2e.local", "S6Pass123!", name="S6운영회원")

    # Complete initial order to earn points
    earned_order = create_order_via_api(
        member,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_name="S6운영회원",
        buyer_phone="010-6000-0001",
    )
    assert admin.post("/api/admin/order-status", json={"order_id": _order_id(earned_order), "payment_status": "입금완료", "order_status": "배송완료"}).status_code == 200
    pts_before = int(member.get("/api/mypage/points").json().get("balance", member.get("/api/mypage/points").json().get("points", 0)))
    assert pts_before >= 1000

    stock_3_before = _get_product_stock(member, 3)
    unpaid_order = create_order_via_api(
        member,
        items=[{"num": 3, "color": "차콜", "size": "M", "quantity": 2}],
        buyer_name="S6운영회원",
        buyer_phone="010-6000-0001",
        coupon_code="S6LIVE10",
        points_used=500,
    )
    recent_paid_control = create_order_via_api(
        member,
        items=[{"num": 2, "color": "아이보리", "size": "FREE", "quantity": 1}],
        buyer_name="S6운영회원",
        buyer_phone="010-6000-0001",
    )
    assert admin.post("/api/admin/order-status", json={"order_id": _order_id(recent_paid_control), "payment_status": "입금완료"}).status_code == 200
    assert _get_product_stock(member, 3) == stock_3_before - 2

    # Run maintenance with test-backdate hook (MALL_ALLOW_TEST_BACKDATE=1 enabled by conftest.py)
    m1 = admin.post(
        "/api/admin/system/run-maintenance",
        json={
            "backdate_order_code": unpaid_order["order_code"],
            "backdate_hours": 5,
            "backdate_orders": [{"order_code": unpaid_order["order_code"], "hours": 5}],
        },
    )
    assert m1.status_code == 200, m1.text

    my_orders = member.get("/api/orders/lookup").json()["orders"]
    u_after = next(o for o in my_orders if o["order_code"] == unpaid_order["order_code"])
    c_after = next(o for o in my_orders if o["order_code"] == recent_paid_control["order_code"])
    assert u_after["order_status"] == "주문취소"
    assert c_after["order_status"] != "주문취소"
    assert _get_product_stock(member, 3) == stock_3_before
    pts_after = int(member.get("/api/mypage/points").json().get("balance", member.get("/api/mypage/points").json().get("points", 0)))
    assert pts_after == pts_before
    assert admin.post("/api/admin/system/run-maintenance", json={}).status_code == 200
    assert _get_product_stock(member, 3) == stock_3_before
    assert member.post("/api/coupons/validate", json={"code": "S6LIVE10", "subtotal": 89000}).json().get("valid", True) is True

    # SQLite online backup + download header/binary check (NO sqlite3 import) + path traversal rejection
    bk_resp = admin.post("/api/admin/system/backup", json={})
    assert bk_resp.status_code == 200, bk_resp.text
    bks = admin.get("/api/admin/system/backups").json().get("backups") or []
    assert len(bks) >= 1
    fname = bks[0]["filename"] if isinstance(bks[0], dict) else str(bks[0])

    dl_resp = admin.get(f"/api/admin/system/backup/{fname}")
    assert dl_resp.status_code == 200
    assert dl_resp.content.startswith(b"SQLite format 3\x00")
    assert len(dl_resp.content) > 4096
    assert "attachment" in (dl_resp.headers.get("Content-Disposition") or "").lower()

    trav_resp = admin.get("/api/admin/system/backup/..%2F..%2Fmall_test.db")
    assert trav_resp.status_code in (400, 403, 404)

    audit_dump = json.dumps(admin.get("/api/admin/audit-logs").json(), ensure_ascii=False)
    assert unpaid_order["order_code"] in audit_dump or "BACKUP" in audit_dump.upper()

    # Store vs /live separation: #10 with price=50000, live_price=37000, is_live=True
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=10,
        name="S6 라이브분리 상품",
        category="TOP",
        price=50000,
        live_price=37000,
        options=[{"color": "블랙", "size": "M", "stock": 10}],
    )
    obs_sync = admin.sync_from_obs({"action": "state", "is_live": True, "pinned_product_num": 10}, srv.sync_secret)
    assert obs_sync.status_code == 200

    guest = api_client_factory(srv)
    for store_route in ("/", "/product/10", "/cart", "/checkout", "/orders"):
        html = guest.get(store_route).text
        assert "37,000원" not in html
        assert "LIVE 특가" not in html
        assert "OBS 스튜디오" not in html
    assert "50,000원" in guest.get("/product/10").text

    live_html = guest.get("/live").text
    assert "37,000원" in live_html
    assert "LIVE 특가" in live_html


# S7: Full Desktop (1440x900) & Mobile (390x844) Layout, Zero Internal Vertical Scroll, Console/404 & CSRF Regression
@pytest.mark.tier4
@pytest.mark.layout
@pytest.mark.regression
def test_t4_07_desktop_mobile_layout_and_csrf_regression(
    live_server,
    admin_storage_state,
    member_storage_state,
    desktop_context_factory,
    mobile_context_factory,
):
    seed_product_via_sync(
        live_server.base_url,
        live_server.sync_secret,
        num=410,
        name="S7 레이아웃 회귀 상품",
        category="TOP",
        price=39000,
        options=[{"color": "블랙", "size": "M", "stock": 10}],
    )

    # Prepare one guest context with a real guest order so /order-complete/<code> renders HTTP 200 in both viewports
    guest_seed_ctx = desktop_context_factory()
    guest_seed_page = guest_seed_ctx.new_page()
    guest_seed_page.goto(f"{live_server.base_url}/", wait_until="domcontentloaded")
    order_res = guest_seed_page.evaluate(
        """async () => {
            const resp = await fetch('/api/orders/create', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    items: [{ num: 410, color: '블랙', size: 'M', quantity: 1 }],
                    buyer_name: 'S7레이아웃',
                    buyer_phone: '010-7777-4100',
                    zipcode: '06236',
                    address: '서울특별시 강남구 테헤란로 152',
                    address_detail: '101호',
                    guest_password: 's7layout',
                    source_page: 'CART_CHECKOUT',
                    payment_method: 'BANK'
                })
            });
            return await resp.json();
        }"""
    )
    assert order_res["status"] == "ok"
    guest_order_code = order_res["order"]["order_code"]
    guest_state = guest_seed_ctx.storage_state()

    cart_seed_item = [
        {
            "num": 410,
            "name": "S7 레이아웃 회귀 상품",
            "price": 39000,
            "consumer_price": 49000,
            "image_url": "",
            "color": "블랙",
            "size": "M",
            "quantity": 1,
        }
    ]

    public_routes = [
        "/",
        "/product/410",
        "/wishlist",
        "/cart",
        "/checkout",
        "/orders",
        f"/order-complete/{guest_order_code}",
        "/cs",
        "/policy/terms",
        "/policy/privacy",
        "/policy/guide",
        "/auth/login",
        "/auth/register",
        "/auth/forgot-password",
        "/auth/reset-password",
    ]

    for vp_name, ctx_factory in [("desktop", desktop_context_factory), ("mobile", mobile_context_factory)]:
        # 1. Public & guest routes
        g_ctx = ctx_factory(storage_state=guest_state)
        g_page = g_ctx.new_page()
        g_diag = attach_page_diagnostics(g_page)
        g_page.goto(f"{live_server.base_url}/", wait_until="domcontentloaded")
        _seed_cart_in_browser(g_page, cart_seed_item)

        for route in public_routes:
            g_diag.clear()
            resp = g_page.goto(f"{live_server.base_url}{route}", wait_until="domcontentloaded")
            assert resp is not None and resp.status == 200, f"{vp_name}:{route} returned {resp and resp.status}"
            _assert_brand_and_csrf_and_layout(g_page)
            _assert_global_hooks(g_page)
            g_diag.assert_clean(f"{vp_name}:guest:{route}")

        # 2. Member routes
        m_ctx = ctx_factory(storage_state=member_storage_state)
        m_page = m_ctx.new_page()
        m_diag = attach_page_diagnostics(m_page)
        for route in ("/mypage", "/orders", "/product/410"):
            m_diag.clear()
            resp = m_page.goto(f"{live_server.base_url}{route}", wait_until="domcontentloaded")
            assert resp is not None and resp.status == 200, f"{vp_name}:member:{route}"
            _assert_brand_and_csrf_and_layout(m_page)
            _assert_global_hooks(m_page)
            m_diag.assert_clean(f"{vp_name}:member:{route}")

        # 3. Admin tabs + /live (via admin_storage_state so live_server is_live stays clean)
        a_ctx = ctx_factory(storage_state=admin_storage_state)
        a_page = a_ctx.new_page()
        a_diag = attach_page_diagnostics(a_page)

        adm_resp = a_page.goto(f"{live_server.base_url}/admin", wait_until="domcontentloaded")
        assert adm_resp is not None and adm_resp.status == 200
        _assert_brand_and_csrf_and_layout(a_page)
        _assert_global_hooks(a_page)

        tab_btns = a_page.locator(".ax-admin-tab-bar .ax-tab-btn")
        tab_count = tab_btns.count()
        assert tab_count >= 7
        for i in range(tab_count):
            tab_btns.nth(i).click()
            a_page.wait_for_timeout(80)
            assert_no_horizontal_overflow(a_page)
            assert_no_main_internal_vertical_scroll(a_page)
        try:
            a_page.wait_for_load_state("networkidle", timeout=2000)
        except Exception:
            pass
        a_page.wait_for_timeout(200)
        a_diag.assert_clean(f"{vp_name}:admin")

        a_diag.clear()
        live_resp = a_page.goto(f"{live_server.base_url}/live", wait_until="domcontentloaded")
        assert live_resp is not None and live_resp.status == 200
        _assert_brand_and_csrf_and_layout(a_page)
        _assert_global_hooks(a_page)
        assert a_page.locator("#liveVideoStage").count() == 1
        assert a_page.locator("#liveLineupList").count() == 1
        a_diag.assert_clean(f"{vp_name}:live")
