"""Tier 3 pairwise cross-feature E2E tests (F1-F18).

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


def _attach_dialog_auto_accept(page: Page) -> list[str]:
    messages: list[str] = []

    def _on_dialog(dialog: Any) -> None:
        messages.append(dialog.message)
        dialog.accept()

    page.on("dialog", _on_dialog)
    return messages


def _update_store_info(admin_client: MallApiClient, **updates: Any) -> dict[str, Any]:
    get_resp = admin_client.get("/api/admin/store-info")
    assert get_resp.status_code == 200
    current = dict(admin_client.json_or_raise(get_resp, "get_store_info").get("store_info") or {})
    current.update(updates)
    post_resp = admin_client.post("/api/admin/store-info", json=current)
    assert post_resp.status_code == 200, post_resp.text
    data = admin_client.json_or_raise(post_resp, "set_store_info")
    assert data["status"] == "ok"
    return dict(data["store_info"])


def _mark_paid_and_delivered(admin_client: MallApiClient, order_id: int) -> dict[str, Any]:
    r1 = admin_client.post(
        "/api/admin/order-status",
        json={"order_id": order_id, "payment_status": "입금완료", "order_status": "상품준비중"},
    )
    assert r1.status_code == 200, r1.text
    r2 = admin_client.post(
        "/api/admin/order-status",
        json={"order_id": order_id, "payment_status": "입금완료", "order_status": "배송완료"},
    )
    assert r2.status_code == 200, r2.text
    return admin_client.json_or_raise(r2, "deliver_order")["order"]


def _get_product_total_stock(client: MallApiClient, product_num: int) -> int:
    resp = client.get(f"/api/products/{product_num}")
    assert resp.status_code == 200, resp.text
    data = client.json_or_raise(resp, f"product_{product_num}")
    return int(data["product"]["total_stock"])


def _get_points_balance(member_client: MallApiClient) -> int:
    resp = member_client.get("/api/mypage/points")
    assert resp.status_code == 200, resp.text
    data = member_client.json_or_raise(resp, "mypage_points")
    for key in ("balance", "points", "total_points", "available_points"):
        if key in data and data[key] is not None:
            return int(data[key])
    wallet = data.get("summary") or data.get("account") or {}
    for key in ("balance", "points", "total_points"):
        if key in wallet and wallet[key] is not None:
            return int(wallet[key])
    raise AssertionError(f"Unexpected points payload keys: {data!r}")


def _extract_coupons_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("coupons", "items", "user_coupons"):
        val = payload.get(key)
        if isinstance(val, list):
            return val
    return []


def _extract_notification_logs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("logs", "notifications", "items"):
        val = payload.get(key)
        if isinstance(val, list):
            return val
    return []


def _extract_audit_logs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("logs", "audit_logs", "items"):
        val = payload.get(key)
        if isinstance(val, list):
            return val
    return []


def _create_coupon_as_admin(
    admin_client: MallApiClient,
    *,
    code: str,
    name: str,
    discount_type: str = "FIXED",
    discount_value: int = 5000,
    min_order_amount: int = 0,
) -> dict[str, Any]:
    resp = admin_client.post(
        "/api/admin/coupons/save",
        json={
            "code": code,
            "name": name,
            "discount_type": discount_type,
            "discount_value": discount_value,
            "min_order_amount": min_order_amount,
            "is_active": True,
        },
    )
    assert resp.status_code == 200, resp.text
    data = admin_client.json_or_raise(resp, f"save_coupon_{code}")
    assert data["status"] == "ok"
    return data


def _earn_points_via_completed_order(
    member_client: MallApiClient,
    admin_client: MallApiClient,
    *,
    product_num: int = 1,
    quantity: int = 1,
    color: str = "블랙",
    size: str = "M",
) -> int:
    order = create_order_via_api(
        member_client,
        items=[{"num": product_num, "color": color, "size": size, "quantity": quantity}],
        buyer_name="포인트적립회원",
        buyer_phone="010-5555-7777",
    )
    _mark_paid_and_delivered(admin_client, _order_id(order))
    return _get_points_balance(member_client)


# 1. F1 x F10
@pytest.mark.tier3
@pytest.mark.f1
@pytest.mark.f10
def test_t3_f1_f10_dynamic_bank_on_order_complete_and_orders(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)

    _update_store_info(
        admin,
        bank_name="신한은행",
        bank_account="110-999-888777",
        bank_holder="골라방법인",
    )

    guest_order_client = api_client_factory(srv)
    resp = guest_order_client.post(
        "/api/orders/create",
        json={
            "items": [{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
            "buyer_name": "홍비회원",
            "buyer_phone": "010-3101-1010",
            "zipcode": "06236",
            "address": "서울특별시 강남구 테헤란로 152",
            "address_detail": "101호",
            "guest_password": "pw1010",
            "source_page": "CART_CHECKOUT",
            "payment_method": "BANK",
            "depositor_name": "홍입금",
            "cash_receipt_type": "INCOME",
            "cash_receipt_no": "01012345678",
        },
    )
    assert resp.status_code == 200, resp.text
    created = guest_order_client.json_or_raise(resp, "f1_f10_order")
    order = created["order"]
    order_code = order["order_code"]
    redirect_url = created.get("redirect_url") or f"/order-complete/{order_code}"
    assert f"/order-complete/{order_code}" in redirect_url

    complete_page = guest_order_client.get(redirect_url)
    assert complete_page.status_code == 200
    assert "신한은행" in complete_page.text
    assert "110-999-888777" in complete_page.text
    assert "골라방법인" in complete_page.text
    assert "홍입금" in complete_page.text
    assert "01012345678" in complete_page.text
    assert ("소득공제" in complete_page.text) or ("INCOME" in complete_page.text)
    assert order_code in complete_page.text
    assert "복사" in complete_page.text

    fresh_guest = api_client_factory(srv)
    csrf = fresh_guest.refresh_csrf("/orders")
    orders_html = fresh_guest.post(
        "/orders",
        data={
            "csrf_token": csrf,
            "order_code": order_code,
            "buyer_phone": "010-3101-1010",
            "guest_password": "pw1010",
        },
    )
    assert orders_html.status_code == 200
    assert "신한은행" in orders_html.text
    assert "110-999-888777" in orders_html.text
    assert "골라방법인" in orders_html.text
    assert "3333 233 094904" not in orders_html.text


# 2. F1 x F6
@pytest.mark.tier3
@pytest.mark.f1
@pytest.mark.f6
def test_t3_f1_f6_dynamic_bank_preserved_with_tracking_on_orders(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=True)
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)

    _update_store_info(
        admin,
        bank_name="우리은행",
        bank_account="1002-111-222333",
        bank_holder="박운영",
    )

    guest_client = api_client_factory(srv)
    order = create_order_via_api(
        guest_client,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_name="김운송장",
        buyer_phone="010-3106-1006",
        guest_password="trackpass1",
    )

    trk_resp = admin.post(
        "/api/admin/orders/tracking",
        json={
            "order_id": _order_id(order),
            "order_code": order["order_code"],
            "carrier_code": "cj",
            "carrier_name": "CJ대한통운",
            "tracking_number": "123456789012",
            "mark_shipped": True,
        },
    )
    assert trk_resp.status_code == 200, trk_resp.text

    ctx = desktop_context_factory()
    page = ctx.new_page()
    diag = attach_page_diagnostics(page)
    page.goto(f"{srv.base_url}/orders", wait_until="domcontentloaded")
    page.fill("#lookupOrderCode", order["order_code"])
    page.fill("#lookupBuyerPhone", "010-3106-1006")
    page.fill("#lookupGuestPassword", "trackpass1")
    with page.expect_navigation(wait_until="domcontentloaded"):
        page.click("#guestOrderLookupForm button[type='submit']")

    body = page.locator("body").inner_text()
    assert "CJ대한통운" in body
    assert "123456789012" in body
    assert "우리은행" in body
    assert "1002-111-222333" in body
    assert "박운영" in body
    assert page.locator("a[href*='123456789012'], a[target='_blank'][href*='http']").count() >= 1
    diag.assert_clean("f1_f6_orders")


# 3. F2 x F16
@pytest.mark.tier3
@pytest.mark.f2
@pytest.mark.f16
def test_t3_f2_f16_related_products_stock_sync_and_recent_bar(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=False)
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=309,
        name="T3 최근본 악세사리",
        category="ACC",
        price=33000,
        options=[{"color": "실버", "size": "ONE", "stock": 8}],
    )
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=310,
        name="T3 연관기준 아우터",
        category="OUTER",
        price=72000,
        options=[{"color": "블랙", "size": "M", "stock": 10}],
    )
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=311,
        name="T3 연관품절 아우터",
        category="OUTER",
        price=68000,
        options=[{"color": "블랙", "size": "M", "stock": 3}],
    )
    for extra_num in (321, 322, 323):
        seed_product_via_sync(
            srv.base_url,
            srv.sync_secret,
            num=extra_num,
            name=f"T3 추가아우터_{extra_num}",
            category="OUTER",
            price=70000,
            options=[{"color": "블랙", "size": "M", "stock": 5}],
        )

    ctx = desktop_context_factory()
    desktop_page = ctx.new_page()
    diag = attach_page_diagnostics(desktop_page)
    # Visit distinct-category product (#309 ACC) first, then #310 (OUTER) so #309 is excluded from #section-related.
    desktop_page.goto(f"{srv.base_url}/product/309", wait_until="domcontentloaded")
    desktop_page.goto(f"{srv.base_url}/product/310", wait_until="domcontentloaded")

    related_section = desktop_page.locator("#section-related")
    assert related_section.is_visible()
    related_text = related_section.inner_text()
    assert "T3 연관품절 아우터" in related_text
    assert "T3 최근본 악세사리" not in related_text
    assert desktop_page.locator('[data-csr-stock-num="311"]').count() >= 1

    body_text = desktop_page.locator("body").inner_text()
    assert "T3 최근본 악세사리" in body_text
    assert_no_horizontal_overflow(desktop_page)
    assert_no_main_internal_vertical_scroll(desktop_page)

    # Mark #311 sold out via stock=0 options and verify both exclude_sold_out=1 and CSR hook.
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=311,
        name="T3 연관품절 아우터",
        category="OUTER",
        price=68000,
        options=[{"color": "블랙", "size": "M", "stock": 0}],
    )

    client = api_client_factory(srv)
    filtered = client.get("/api/products?category=OUTER&exclude_sold_out=1")
    assert filtered.status_code == 200
    filtered_nums = {int(p["num"]) for p in client.json_or_raise(filtered)["products"]}
    assert 310 in filtered_nums
    assert 311 not in filtered_nums

    desktop_page.reload(wait_until="domcontentloaded")
    stock_text = desktop_page.locator('#section-related [data-csr-stock-num="311"]').first.inner_text()
    assert ("0개" in stock_text) or ("품절" in stock_text)
    diag.assert_clean("f2_f16")


# 4. F2 x F4
@pytest.mark.tier3
@pytest.mark.f2
@pytest.mark.f4
def test_t3_f2_f4_related_product_to_checkout_uses_dynamic_shipping_fee(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=False)
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    _update_store_info(admin, shipping_fee=3500, free_shipping_threshold=0)

    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=312,
        name="T3 시작 상의",
        category="TOP",
        price=29000,
        options=[{"color": "블랙", "size": "M", "stock": 10}],
    )
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=313,
        name="T3 연관 상의",
        category="TOP",
        price=31000,
        options=[{"color": "화이트", "size": "M", "stock": 10}],
    )

    ctx = desktop_context_factory()
    page = ctx.new_page()
    diag = attach_page_diagnostics(page)
    _attach_dialog_auto_accept(page)
    mock_daum_postcode(page, zipcode="06236", address="서울특별시 강남구 테헤란로 152")

    page.goto(f"{srv.base_url}/product/312", wait_until="domcontentloaded")
    page.locator('#section-related a[href="/product/313"]').first.click()
    page.wait_for_url("**/product/313")
    page.get_by_role("button", name="장바구니 담기").click()

    page.goto(f"{srv.base_url}/cart", wait_until="domcontentloaded")
    assert "3,500원" in page.locator("#cartShippingText").inner_text()
    assert "34,500원" in page.locator("#cartTotalText").inner_text()

    page.click("#btnGoCheckout")
    page.wait_for_url("**/checkout")
    assert "3,500원" in page.locator("#coShippingAmount").inner_text()
    assert "34,500원" in page.locator("#coTotalAmount").inner_text()
    assert "4,000원" not in page.locator("#coShippingAmount").inner_text()

    page.fill("#coBuyerName", "연관구매자")
    page.fill("#coBuyerPhone", "010-2400-3130")
    page.click("#btnCoPostcode")
    page.fill("#coAddressDetail", "313호")
    page.fill("#coGuestPassword", "rel313")

    order_json = _submit_checkout_and_capture_order(page)
    assert order_json["status"] == "ok"
    order = order_json["order"]
    assert int(order["shipping_fee"]) == 3500
    assert int(order["total_amount"]) == 34500

    lookup_client = api_client_factory(srv)
    lookup_resp = lookup_client.post(
        "/api/orders/lookup",
        json={
            "order_code": order["order_code"],
            "buyer_phone": "010-2400-3130",
            "guest_password": "rel313",
        },
    )
    assert lookup_resp.status_code == 200
    found = lookup_client.json_or_raise(lookup_resp)["orders"][0]
    assert int(found["shipping_fee"]) == 3500
    assert int(found["total_amount"]) == 34500
    diag.assert_clean("f2_f4")


# 5. F3 x F8
@pytest.mark.tier3
@pytest.mark.f3
@pytest.mark.f8
def test_t3_f3_f8_mypage_address_book_with_daum_postcode_and_checkout(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=True)
    member_api = api_client_factory(srv)
    member_api.register(
        "t3_f3_f8@e2e.local",
        "AddrPass123!",
        name="주소록회원",
        phone="010-3800-3800",
        zipcode="06236",
        address="서울특별시 강남구 테헤란로 152",
        address_detail="101호",
    )

    ctx = desktop_context_factory()
    page = ctx.new_page()
    diag = attach_page_diagnostics(page)
    _attach_dialog_auto_accept(page)
    mock_daum_postcode(page, zipcode="04524", address="서울특별시 중구 세종대로 110")

    page.goto(f"{srv.base_url}/auth/login", wait_until="domcontentloaded")
    page.fill("#loginEmail", "t3_f3_f8@e2e.local")
    page.fill("#loginPassword", "AddrPass123!")
    with page.expect_navigation(wait_until="domcontentloaded"):
        page.click("form[action='/auth/login'] button[type='submit']")

    page.goto(f"{srv.base_url}/mypage", wait_until="domcontentloaded")
    assert page.url.endswith("/mypage")

    # Save a new default address in address book (via UI if Daum button present, verifying via API).
    postcode_btn = page.locator("button:has-text('우편번호'), button:has-text('주소 검색')").first
    if postcode_btn.count() > 0:
        postcode_btn.click()

    save_resp = member_api.post(
        "/api/mypage/addresses/save",
        json={
            "label": "회사",
            "recipient_name": "주소록회원",
            "recipient_phone": "010-3800-3800",
            "zipcode": "04524",
            "address": "서울특별시 중구 세종대로 110",
            "address_detail": "5층",
            "is_default": True,
        },
    )
    assert save_resp.status_code == 200, save_resp.text

    addr_list_resp = member_api.get("/api/mypage/addresses")
    assert addr_list_resp.status_code == 200
    addrs = addr_list_resp.json().get("addresses") or []
    assert any(a.get("zipcode") == "04524" and a.get("is_default") for a in addrs)

    # Seed cart via window.saveMallCart (never raw localStorage key), open /checkout, ensure 04524 used.
    page.goto(f"{srv.base_url}/", wait_until="domcontentloaded")
    page.evaluate(
        """() => window.saveMallCart([{
            num: 1,
            name: '골라방 클래식 코튼 티셔츠',
            price: 29000,
            consumer_price: 39000,
            image_url: '',
            color: '블랙',
            size: 'M',
            quantity: 1
        }])"""
    )
    page.goto(f"{srv.base_url}/checkout", wait_until="domcontentloaded")
    _apply_checkout_saved_address(page, "04524", "회사")

    order_json = _submit_checkout_and_capture_order(page)
    assert order_json["status"] == "ok"
    created_order = order_json["order"]
    assert created_order["zipcode"] == "04524"
    diag.assert_clean("f3_f8")


# 6. F3 x F6
@pytest.mark.tier3
@pytest.mark.f3
@pytest.mark.f6
def test_t3_f3_f6_shipping_csv_export_includes_zipcode_and_tracking_import(
    live_server,
    admin_api,
    api_client_factory,
):
    seed_product_via_sync(
        live_server.base_url,
        live_server.sync_secret,
        num=324,
        name="T3 CSV배송 상품",
        category="TOP",
        price=29000,
        options=[{"color": "블랙", "size": "M", "stock": 20}],
        image_url="",
    )
    guest_client = api_client_factory(live_server)
    order = create_order_via_api(
        guest_client,
        items=[{"num": 324, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_name="판교수령인",
        buyer_phone="010-3600-1352",
        zipcode="13529",
        address="경기도 성남시 분당구 판교역로 166",
        address_detail="A동 301호",
        delivery_memo="경비실",
        guest_password="csvpw1",
    )

    export_resp = admin_api.get("/api/admin/orders/export-shipping.csv")
    assert export_resp.status_code == 200
    raw_bytes = export_resp.content
    assert raw_bytes.startswith(b"\xef\xbb\xbf")
    csv_text = raw_bytes.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    matching = [r for r in rows if r.get("order_code") == order["order_code"]]
    assert len(matching) == 1
    row = matching[0]
    assert row["zipcode"] == "13529"
    assert "판교역로 166" in row["address"]
    assert row["address_detail"] == "A동 301호"
    assert row["delivery_memo"] == "경비실"

    upload_buf = io.StringIO()
    writer = csv.writer(upload_buf)
    writer.writerow(["order_code", "carrier_code", "carrier_name", "tracking_number"])
    writer.writerow([order["order_code"], "hanjin", "한진택배", "9988776655"])
    imp_resp = admin_api.post(
        "/api/admin/orders/import-tracking",
        files={"file": ("tracking.csv", upload_buf.getvalue().encode("utf-8-sig"), "text/csv")},
    )
    assert imp_resp.status_code == 200, imp_resp.text

    fresh_guest = api_client_factory(live_server)
    lookup = fresh_guest.post(
        "/api/orders/lookup",
        json={
            "order_code": order["order_code"],
            "buyer_phone": "010-3600-1352",
            "guest_password": "csvpw1",
        },
    )
    assert lookup.status_code == 200
    found = lookup.json()["orders"][0]
    assert found["tracking_number"] == "9988776655"
    assert found["zipcode"] == "13529"


# 7. F4 x F5
@pytest.mark.tier3
@pytest.mark.f4
@pytest.mark.f5
def test_t3_f4_f5_dynamic_shipping_fee_and_free_threshold_transition(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=True)
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    _update_store_info(admin, shipping_fee=3200, free_shipping_threshold=50000)

    guest1 = api_client_factory(srv)
    o1 = create_order_via_api(
        guest1,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_phone="010-4500-0001",
        guest_password="f45p1",
    )
    assert int(o1["shipping_fee"]) == 3200
    assert int(o1["total_amount"]) == 32200

    guest2 = api_client_factory(srv)
    o2 = create_order_via_api(
        guest2,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 2}],
        buyer_phone="010-4500-0002",
        guest_password="f45p2",
    )
    assert int(o2["shipping_fee"]) == 0
    assert int(o2["total_amount"]) == 58000

    ctx = desktop_context_factory()
    page = ctx.new_page()
    diag = attach_page_diagnostics(page)
    page.goto(f"{srv.base_url}/", wait_until="domcontentloaded")
    page.evaluate(
        """() => window.saveMallCart([{
            num: 1,
            name: '골라방 클래식 코튼 티셔츠',
            price: 29000,
            consumer_price: 39000,
            image_url: '',
            color: '블랙',
            size: 'M',
            quantity: 1
        }])"""
    )
    page.goto(f"{srv.base_url}/cart", wait_until="domcontentloaded")
    assert "3,200원" in page.locator("#cartShippingText").inner_text()
    assert "21,000원" in page.locator("#cartShippingGuide").inner_text()
    assert "32,200원" in page.locator("#cartTotalText").inner_text()

    page.locator('button[aria-label="수량 증가"]').first.click()
    assert "무료배송 (0원)" in page.locator("#cartShippingText").inner_text()
    assert "58,000원" in page.locator("#cartTotalText").inner_text()
    diag.assert_clean("f4_f5")


# 8. F4 x F14
@pytest.mark.tier3
@pytest.mark.f4
@pytest.mark.f14
def test_t3_f4_f14_dynamic_shipping_with_coupon_and_points_total(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    _update_store_info(admin, shipping_fee=3500, free_shipping_threshold=60000, point_earn_rate=5)
    _create_coupon_as_admin(
        admin,
        code="T3SAVE5K",
        name="5천원 할인",
        discount_type="FIXED",
        discount_value=5000,
        min_order_amount=20000,
    )

    member = api_client_factory(srv)
    member.register("t3_f4_f14@e2e.local", "PointPass123!", name="할인포인트회원")
    pts = _earn_points_via_completed_order(member, admin, product_num=1, quantity=1)
    assert pts >= 1000

    order = create_order_via_api(
        member,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_name="할인포인트회원",
        buyer_phone="010-4140-4140",
        coupon_code="T3SAVE5K",
        points_used=1000,
    )
    assert int(order["subtotal_amount"]) == 29000
    assert int(order["shipping_fee"]) == 3500
    assert int(order["coupon_discount"] if "coupon_discount" in order else order["discount_amount"]) == 5000
    assert int(order["points_used"]) == 1000
    assert int(order["total_amount"]) == 26500


# 9. F5 x F15
@pytest.mark.tier3
@pytest.mark.f5
@pytest.mark.f15
def test_t3_f5_f15_cart_selection_option_change_updates_free_shipping_and_revalidates(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=True)
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    _update_store_info(admin, shipping_fee=3000, free_shipping_threshold=50000)

    ctx = desktop_context_factory()
    page = ctx.new_page()
    diag = attach_page_diagnostics(page)
    page.goto(f"{srv.base_url}/", wait_until="domcontentloaded")
    page.evaluate(
        """() => window.saveMallCart([
            {
                num: 1,
                name: '골라방 클래식 코튼 티셔츠',
                price: 29000,
                consumer_price: 39000,
                image_url: '',
                color: '블랙',
                size: 'M',
                quantity: 1,
                selected: true
            },
            {
                num: 2,
                name: '골라방 데일리 리브드 니트',
                price: 42000,
                consumer_price: 54000,
                image_url: '',
                color: '아이보리',
                size: 'FREE',
                quantity: 1,
                selected: true
            }
        ])"""
    )
    page.goto(f"{srv.base_url}/cart", wait_until="domcontentloaded")
    assert "무료배송 (0원)" in page.locator("#cartShippingText").inner_text()

    # Uncheck 2nd item in cart UI -> selected subtotal 29,000 < 50,000 -> shipping 3,000, remaining 21,000.
    row_2 = page.locator("#cartItemsContainer .ax-cart-row", has_text="데일리 리브드 니트").first
    assert row_2.count() == 1
    cb_2 = row_2.locator("input[type='checkbox']").first
    assert cb_2.count() == 1
    cb_2.uncheck()
    page.wait_for_timeout(150)
    assert "3,000원" in page.locator("#cartShippingText").inner_text()
    assert "21,000원" in page.locator("#cartShippingGuide").inner_text()

    # Change 1st item option in cart UI from 블랙/M to 화이트/L.
    _select_cart_row_option(page, "클래식 코튼 티셔츠", "화이트", "L")

    cart_after = page.evaluate("() => window.getMallCart()")
    assert cart_after[0]["color"] == "화이트"
    assert cart_after[0]["size"] == "L"

    guest = api_client_factory(srv)
    reval = guest.post(
        "/api/cart/revalidate",
        json={"items": [{"num": 1, "color": "화이트", "size": "L", "quantity": 1}]},
    )
    assert reval.status_code == 200, reval.text
    rdata = guest.json_or_raise(reval, "cart_revalidate")
    assert rdata.get("valid", True) is True
    assert int(rdata["subtotal"]) == 29000
    assert int(rdata["shipping_fee"]) == 3000
    assert int(rdata["total"]) == 32000
    diag.assert_clean("f5_f15")


# 10. F5 x F14
@pytest.mark.tier3
@pytest.mark.f5
@pytest.mark.f14
def test_t3_f5_f14_free_shipping_threshold_evaluated_with_coupon_and_points(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    _update_store_info(admin, shipping_fee=4500, free_shipping_threshold=50000)
    _create_coupon_as_admin(
        admin,
        code="T3HALF10K",
        name="1만원 할인",
        discount_type="FIXED",
        discount_value=10000,
        min_order_amount=40000,
    )

    member = api_client_factory(srv)
    member.register("t3_f5_f14@e2e.local", "ThreshPass123!", name="무배기준회원")

    reval = member.post(
        "/api/cart/revalidate",
        json={
            "items": [{"num": 1, "color": "블랙", "size": "M", "quantity": 2}],
            "coupon_code": "T3HALF10K",
        },
    )
    assert reval.status_code == 200, reval.text
    rdata = member.json_or_raise(reval, "revalidate_with_coupon")

    order = create_order_via_api(
        member,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 2}],
        buyer_name="무배기준회원",
        buyer_phone="010-5140-5140",
        coupon_code="T3HALF10K",
    )
    assert int(order["subtotal_amount"]) == 58000
    assert int(order["coupon_discount"] if "coupon_discount" in order else order["discount_amount"]) == 10000
    # Guarantee parity between /api/cart/revalidate and /api/orders/create.
    assert int(order["shipping_fee"]) == int(rdata["shipping_fee"])
    assert int(order["total_amount"]) == int(rdata["total"])
    assert int(order["total_amount"]) == (58000 - 10000 + int(order["shipping_fee"]))


# 11. F6 x F12
@pytest.mark.tier3
@pytest.mark.f6
@pytest.mark.f12
def test_t3_f6_f12_tracking_shipment_logs_customer_notification(
    live_server,
    admin_api,
    api_client_factory,
):
    seed_product_via_sync(
        live_server.base_url,
        live_server.sync_secret,
        num=325,
        name="T3 출고알림 상품",
        category="TOP",
        price=29000,
        options=[{"color": "블랙", "size": "M", "stock": 20}],
        image_url="",
    )
    guest = api_client_factory(live_server)
    order = create_order_via_api(
        guest,
        items=[{"num": 325, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_name="출고알림고객",
        buyer_phone="010-2222-3333",
        guest_password="shipnotify1",
    )
    r_paid = admin_api.post(
        "/api/admin/order-status",
        json={"order_id": _order_id(order), "payment_status": "입금완료"},
    )
    assert r_paid.status_code == 200, r_paid.text

    trk_num = f"5544{uuid.uuid4().hex[:8]}"
    r_trk = admin_api.post(
        "/api/admin/orders/tracking",
        json={
            "order_id": _order_id(order),
            "order_code": order["order_code"],
            "carrier_code": "lotte",
            "carrier_name": "롯데택배",
            "tracking_number": trk_num,
            "mark_shipped": True,
        },
    )
    assert r_trk.status_code == 200, r_trk.text

    logs_resp = admin_api.get("/api/admin/notifications/logs")
    assert logs_resp.status_code == 200, logs_resp.text
    logs = _extract_notification_logs(admin_api.json_or_raise(logs_resp, "notif_logs"))
    matching = [
        log
        for log in logs
        if order["order_code"] in json.dumps(log, ensure_ascii=False)
        and trk_num in json.dumps(log, ensure_ascii=False)
    ]
    assert len(matching) >= 1
    assert any("ERROR" not in str(m.get("status", "")).upper() for m in matching)


# 12. F6 x F7
@pytest.mark.tier3
@pytest.mark.f6
@pytest.mark.f7
def test_t3_f6_f7_shipped_blocks_self_cancel_delivered_allows_claim_with_tracking(
    live_server,
    admin_api,
    api_client_factory,
):
    seed_product_via_sync(
        live_server.base_url,
        live_server.sync_secret,
        num=314,
        name="T3 배송클레임 상품",
        category="ACC",
        price=35000,
        options=[{"color": "블랙", "size": "ONE", "stock": 10}],
    )

    order_client = api_client_factory(live_server)
    order = create_order_via_api(
        order_client,
        items=[{"num": 314, "color": "블랙", "size": "ONE", "quantity": 1}],
        buyer_name="배송클레임",
        buyer_phone="010-6700-3140",
        guest_password="claim314",
    )
    assert _get_product_total_stock(order_client, 314) == 9

    admin_api.post(
        "/api/admin/order-status",
        json={"order_id": _order_id(order), "payment_status": "입금완료"},
    )
    trk_resp = admin_api.post(
        "/api/admin/orders/tracking",
        json={
            "order_id": _order_id(order),
            "order_code": order["order_code"],
            "carrier_code": "cj",
            "carrier_name": "CJ대한통운",
            "tracking_number": "7788990011",
            "mark_shipped": True,
        },
    )
    assert trk_resp.status_code == 200, trk_resp.text

    fresh_guest = api_client_factory(live_server)
    cancel_resp = fresh_guest.post(
        "/api/orders/self-cancel",
        json={
            "order_code": order["order_code"],
            "buyer_phone": "010-6700-3140",
            "guest_password": "claim314",
        },
    )
    assert cancel_resp.status_code == 400
    assert _get_product_total_stock(fresh_guest, 314) == 9

    deliv_resp = admin_api.post(
        "/api/admin/order-status",
        json={"order_id": _order_id(order), "payment_status": "입금완료", "order_status": "배송완료"},
    )
    assert deliv_resp.status_code == 200

    claim_resp = fresh_guest.post(
        "/api/orders/self-claim",
        json={
            "order_code": order["order_code"],
            "buyer_phone": "010-6700-3140",
            "guest_password": "claim314",
            "claim_type": "교환요청",
            "claim_reason": "사이즈 불일치",
        },
    )
    assert claim_resp.status_code == 200, claim_resp.text
    claim_data = fresh_guest.json_or_raise(claim_resp, "self_claim")
    assert claim_data["order"]["order_status"] == "교환요청"
    assert claim_data["order"]["tracking_number"] == "7788990011"


# 13. F7 x F14
@pytest.mark.tier3
@pytest.mark.f7
@pytest.mark.f14
def test_t3_f7_f14_self_cancel_restores_coupon_points_and_stock(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=False)
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=315,
        name="T3 셀프취소복구 상품",
        category="TOP",
        price=30000,
        options=[{"color": "블랙", "size": "M", "stock": 5}],
    )
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    _update_store_info(admin, shipping_fee=0, free_shipping_threshold=0, point_earn_rate=10)
    _create_coupon_as_admin(
        admin,
        code="T3RET3K",
        name="취소복구쿠폰",
        discount_type="FIXED",
        discount_value=3000,
        min_order_amount=10000,
    )

    member = api_client_factory(srv)
    member.register("t3_f7_f14@e2e.local", "RestorePass123!", name="복구회원")
    pts_before = _earn_points_via_completed_order(member, admin, product_num=315, quantity=1)
    assert pts_before >= 3000
    assert _get_product_total_stock(member, 315) == 4

    order_b = create_order_via_api(
        member,
        items=[{"num": 315, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_name="복구회원",
        buyer_phone="010-7140-7140",
        coupon_code="T3RET3K",
        points_used=1000,
    )
    assert _get_product_total_stock(member, 315) == 3
    assert _get_points_balance(member) == pts_before - 1000

    reval_used = member.post("/api/coupons/validate", json={"code": "T3RET3K", "subtotal": 30000})
    assert reval_used.status_code in (400, 409) or reval_used.json().get("valid") is False

    cancel_resp = member.post(
        "/api/orders/self-cancel",
        json={"order_code": order_b["order_code"]},
    )
    assert cancel_resp.status_code == 200, cancel_resp.text
    assert cancel_resp.json()["order"]["order_status"] == "주문취소"
    assert _get_product_total_stock(member, 315) == 4
    assert _get_points_balance(member) == pts_before

    reval_restored = member.post("/api/coupons/validate", json={"code": "T3RET3K", "subtotal": 30000})
    assert reval_restored.status_code == 200, reval_restored.text
    assert reval_restored.json().get("valid", True) is True


# 14. F7 x F12
@pytest.mark.tier3
@pytest.mark.f7
@pytest.mark.f12
def test_t3_f7_f12_self_claim_triggers_admin_notification_log(
    live_server,
    member_api,
    admin_api,
):
    seed_product_via_sync(
        live_server.base_url,
        live_server.sync_secret,
        num=326,
        name="T3 클레임알림 상품",
        category="TOP",
        price=29000,
        options=[{"color": "블랙", "size": "M", "stock": 20}],
        image_url="",
    )
    order = create_order_via_api(
        member_api,
        items=[{"num": 326, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_name="클레임알림회원",
        buyer_phone="010-7120-7120",
    )
    _mark_paid_and_delivered(admin_api, _order_id(order))

    claim_resp = member_api.post(
        "/api/orders/self-claim",
        json={
            "order_code": order["order_code"],
            "claim_type": "환불요청",
            "claim_reason": "T3-단순변심-환불요청",
        },
    )
    assert claim_resp.status_code == 200, claim_resp.text
    assert claim_resp.json()["order"]["order_status"] == "환불요청"

    logs_resp = admin_api.get("/api/admin/notifications/logs")
    assert logs_resp.status_code == 200
    logs = _extract_notification_logs(admin_api.json_or_raise(logs_resp, "claim_notif"))
    matched = [
        log
        for log in logs
        if order["order_code"] in json.dumps(log, ensure_ascii=False)
        and ("환불" in json.dumps(log, ensure_ascii=False) or "CLAIM" in json.dumps(log, ensure_ascii=False).upper())
    ]
    assert len(matched) >= 1


# 15. F8 x F9
@pytest.mark.tier3
@pytest.mark.f8
@pytest.mark.f9
def test_t3_f8_f9_admin_suspend_blocks_mypage_and_withdraw_updates_admin_list(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    user_a = api_client_factory(srv)
    reg_a = user_a.register("user_a_t3@e2e.local", "UserAPass123!", name="정지대상")
    user_b = api_client_factory(srv)
    reg_b = user_b.register("user_b_t3@e2e.local", "UserBPass123!", name="탈퇴대상")

    create_order_via_api(
        user_b,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_name="탈퇴대상",
        buyer_phone="010-8900-8900",
    )

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    users_resp = admin.get("/api/admin/users")
    assert users_resp.status_code == 200
    users = users_resp.json().get("users") or []
    ua_row = next(u for u in users if u["email"] == "user_a_t3@e2e.local")
    ub_row = next(u for u in users if u["email"] == "user_b_t3@e2e.local")
    assert int(ua_row["is_active"]) == 1
    assert int(ub_row["is_active"]) == 1

    susp_resp = admin.post(
        "/api/admin/users/status",
        json={"user_id": reg_a["user"]["id"], "is_active": False},
    )
    assert susp_resp.status_code == 200, susp_resp.text

    # Existing session for user_a must be blocked from /mypage and profile API.
    mp_blocked = user_a.get("/mypage", allow_redirects=False)
    assert mp_blocked.status_code in (302, 401, 403)
    prof_blocked = user_a.post("/api/mypage/profile", json={"name": "변경시도"})
    assert prof_blocked.status_code in (401, 403)

    login_blocked_client = api_client_factory(srv)
    login_blocked = login_blocked_client.post(
        "/auth/login",
        json={"email": "user_a_t3@e2e.local", "password": "UserAPass123!"},
    )
    assert login_blocked.status_code in (401, 403)

    # Reactivate user_a.
    react_resp = admin.post(
        "/api/admin/users/status",
        json={"user_id": reg_a["user"]["id"], "is_active": True},
    )
    assert react_resp.status_code == 200
    relogin = api_client_factory(srv)
    relogin.login("user_a_t3@e2e.local", "UserAPass123!")
    assert relogin.get("/mypage").status_code == 200

    # user_b withdraws.
    wd_resp = user_b.post("/api/mypage/withdraw", json={"password": "UserBPass123!"})
    assert wd_resp.status_code == 200, wd_resp.text

    relogin_b = api_client_factory(srv)
    login_b_fail = relogin_b.post(
        "/auth/login",
        json={"email": "user_b_t3@e2e.local", "password": "UserBPass123!"},
    )
    assert login_b_fail.status_code in (401, 403)

    users_after = admin.get("/api/admin/users").json().get("users") or []
    ub_after = next(u for u in users_after if u["email"] == "user_b_t3@e2e.local")
    assert int(ub_after["is_active"]) == 0 or bool(ub_after.get("withdrawn_at"))
    assert int(ub_after.get("order_count", 0)) >= 1


# 16. F8 x F14
@pytest.mark.tier3
@pytest.mark.f8
@pytest.mark.f14
def test_t3_f8_f14_register_grants_welcome_coupon_and_mypage_shows_points(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=True)
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    _update_store_info(admin, point_earn_rate=5)

    member = api_client_factory(srv)
    member.register("welcome_t3@e2e.local", "WelcomePass123!", name="웰컴회원")

    coupons_resp = member.get("/api/mypage/coupons")
    assert coupons_resp.status_code == 200, coupons_resp.text
    coupons = _extract_coupons_list(member.json_or_raise(coupons_resp, "welcome_coupons"))
    assert len(coupons) >= 1

    pts = _earn_points_via_completed_order(member, admin, product_num=1, quantity=1)
    assert pts > 0

    ctx = desktop_context_factory()
    page = ctx.new_page()
    diag = attach_page_diagnostics(page)
    page.goto(f"{srv.base_url}/auth/login", wait_until="domcontentloaded")
    page.fill("#loginEmail", "welcome_t3@e2e.local")
    page.fill("#loginPassword", "WelcomePass123!")
    with page.expect_navigation(wait_until="domcontentloaded"):
        page.click("form[action='/auth/login'] button[type='submit']")

    page.goto(f"{srv.base_url}/mypage", wait_until="domcontentloaded")
    page.click("#mypageTabCoupons")
    page.locator("#mypageCouponsList .ax-coupon-card").first.wait_for(state="visible", timeout=10000)
    body = page.locator("body").inner_text()
    assert str(coupons[0].get("code") or "웰컴") in body
    assert f"{pts:,}" in body or str(pts) in body
    assert_no_horizontal_overflow(page)
    assert_no_main_internal_vertical_scroll(page)
    diag.assert_clean("f8_f14_mypage")


# 17. F9 x F18
@pytest.mark.tier3
@pytest.mark.f9
@pytest.mark.f18
def test_t3_f9_f18_admin_member_status_toggle_writes_audit_log(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    member = api_client_factory(srv)
    reg = member.register("audit_mem@e2e.local", "AuditPass123!", name="감사대상회원")
    uid = int(reg["user"]["id"])

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    r1 = admin.post("/api/admin/users/status", json={"user_id": uid, "is_active": False})
    assert r1.status_code == 200
    r2 = admin.post("/api/admin/users/status", json={"user_id": uid, "is_active": True})
    assert r2.status_code == 200

    logs_resp = admin.get("/api/admin/audit-logs")
    assert logs_resp.status_code == 200, logs_resp.text
    logs = _extract_audit_logs(admin.json_or_raise(logs_resp, "audit_logs"))
    serialized = [json.dumps(entry, ensure_ascii=False) for entry in logs]
    matched = [s for s in serialized if str(uid) in s or "audit_mem@e2e.local" in s]
    assert len(matched) >= 2
    assert any(srv.admin_email in s or "ADMIN" in s.upper() for s in matched)


# 18. F10 x F12
@pytest.mark.tier3
@pytest.mark.f10
@pytest.mark.f12
def test_t3_f10_f12_bank_and_pg_confirm_record_notification_logs(
    live_server,
    admin_api,
    api_client_factory,
):
    seed_product_via_sync(
        live_server.base_url,
        live_server.sync_secret,
        num=327,
        name="T3 결제알림 상품",
        category="TOP",
        price=29000,
        options=[{"color": "블랙", "size": "M", "stock": 20}],
        image_url="",
    )
    guest_bank = api_client_factory(live_server)
    bank_order = create_order_via_api(
        guest_bank,
        items=[{"num": 327, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_name="김알림",
        buyer_phone="010-1012-1012",
        guest_password="notif1012",
        payment_method="BANK",
        depositor_name="김알림입금",
        cash_receipt_type="EXPENSE",
        cash_receipt_no="123-45-67890",
    )

    pg_guest = api_client_factory(live_server)
    pg_order = create_order_via_api(
        pg_guest,
        items=[{"num": 327, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_name="PG알림구매자",
        buyer_phone="010-1012-9999",
        guest_password="pgpass12",
        payment_method="PORTONE_V2",
    )

    prep_resp = pg_guest.post(
        "/api/payments/prepare",
        json={
            "order_code": pg_order["order_code"],
            "payment_method": "PORTONE_V2",
        },
    )
    assert prep_resp.status_code == 200, prep_resp.text
    prep_data = pg_guest.json_or_raise(prep_resp, "pg_prepare")
    prep_payload = prep_data.get("payment", prep_data)
    tx_id = (
        prep_payload.get("payment_key")
        or prep_payload.get("payment_id")
        or prep_payload.get("merchant_uid")
        or prep_payload.get("transaction_id")
        or f"test_portone_v2_{pg_order['order_code']}"
    )

    conf_resp = pg_guest.post(
        "/api/payments/confirm",
        json={
            "order_code": pg_order["order_code"],
            "payment_method": "PORTONE_V2",
            "payment_key": tx_id,
            "payment_id": tx_id,
            "merchant_uid": prep_payload.get("merchant_uid") or tx_id,
            "amount": pg_order["total_amount"],
        },
    )
    assert conf_resp.status_code == 200, conf_resp.text
    conf_data = pg_guest.json_or_raise(conf_resp, "pg_confirm")
    pg_order_code = (
        conf_data.get("order_code")
        or (conf_data.get("order") or {}).get("order_code")
        or pg_order["order_code"]
    )

    logs_resp = admin_api.get("/api/admin/notifications/logs")
    assert logs_resp.status_code == 200
    logs = _extract_notification_logs(admin_api.json_or_raise(logs_resp, "f10_f12_logs"))
    all_dump = [json.dumps(entry, ensure_ascii=False) for entry in logs]
    assert any(bank_order["order_code"] in s for s in all_dump)
    assert any(pg_order_code in s for s in all_dump)


# 19. F11 x F18
@pytest.mark.tier3
@pytest.mark.f11
@pytest.mark.f18
def test_t3_f11_f18_cs_admin_answers_and_notice_save_write_audit_logs(
    live_server,
    member_api,
    admin_api,
):
    tok = uuid.uuid4().hex[:6]
    qna_resp = member_api.post(
        "/api/products/1/qna",
        json={"question": f"T3-비밀문의-{tok}", "is_secret": True},
    )
    assert qna_resp.status_code == 200, qna_resp.text
    qna_data = member_api.json_or_raise(qna_resp, "qna_create")
    qna_id = (qna_data.get("qna") or qna_data.get("item") or {}).get("id") or qna_data.get("id")
    assert qna_id

    inq_resp = member_api.post(
        "/api/inquiries/create",
        json={"category": "배송", "title": f"T3-일대일-{tok}", "content": "배송 일정 확인 부탁드립니다."},
    )
    assert inq_resp.status_code == 200, inq_resp.text
    inq_data = member_api.json_or_raise(inq_resp, "inq_create")
    inq_id = (inq_data.get("inquiry") or inq_data.get("item") or {}).get("id") or inq_data.get("id")
    assert inq_id

    ans_qna = admin_api.post("/api/admin/qna/answer", json={"id": qna_id, "qna_id": qna_id, "answer": f"답변-{tok}"})
    assert ans_qna.status_code == 200, ans_qna.text
    ans_inq = admin_api.post("/api/admin/inquiries/answer", json={"id": inq_id, "inquiry_id": inq_id, "answer": f"문의답변-{tok}"})
    assert ans_inq.status_code == 200, ans_inq.text
    save_notice = admin_api.post(
        "/api/admin/notices/save",
        json={"title": f"T3-공지사항-{tok}", "content": "공지 테스트 본문", "is_pinned": False, "is_active": True},
    )
    assert save_notice.status_code == 200, save_notice.text
    notice_data = admin_api.json_or_raise(save_notice, "notice_save")
    notice_id = (notice_data.get("notice") or {}).get("id") or notice_data.get("id")

    logs_resp = admin_api.get("/api/admin/audit-logs")
    assert logs_resp.status_code == 200
    logs = _extract_audit_logs(admin_api.json_or_raise(logs_resp, "cs_audit_logs"))
    serialized = [json.dumps(x, ensure_ascii=False) for x in logs]
    assert any(str(qna_id) in s and ("QNA" in s.upper() or "qna" in s.lower()) for s in serialized)
    assert any(str(inq_id) in s and ("INQUIR" in s.upper() or "문의" in s) for s in serialized)
    assert any((str(notice_id) in s or tok in s) and ("NOTICE" in s.upper() or "공지" in s) for s in serialized)


# 20. F12 x F16
@pytest.mark.tier3
@pytest.mark.f12
@pytest.mark.f16
def test_t3_f12_f16_restock_alert_subscription_logs_notification_on_restock(
    live_server,
    admin_api,
    guest_api,
):
    seed_product_via_sync(
        live_server.base_url,
        live_server.sync_secret,
        num=316,
        name="T3 재입고알림 테스트 상품",
        category="TOP",
        price=34000,
        options=[{"color": "네이비", "size": "M", "stock": 0}],
    )

    sub_resp = guest_api.post(
        "/api/restock-alerts/subscribe",
        json={
            "product_num": 316,
            "color": "네이비",
            "size": "M",
            "contact": "restock316@e2e.local",
            "email": "restock316@e2e.local",
            "phone": "010-9876-5432",
        },
    )
    assert sub_resp.status_code == 200, sub_resp.text

    alerts_resp = admin_api.get("/api/admin/restock-alerts")
    assert alerts_resp.status_code == 200, alerts_resp.text
    alerts_dump = json.dumps(alerts_resp.json(), ensure_ascii=False)
    assert "316" in alerts_dump
    assert "네이비" in alerts_dump

    seed_product_via_sync(
        live_server.base_url,
        live_server.sync_secret,
        num=316,
        name="T3 재입고알림 테스트 상품",
        category="TOP",
        price=34000,
        options=[{"color": "네이비", "size": "M", "stock": 12}],
    )

    logs_resp = admin_api.get("/api/admin/notifications/logs")
    assert logs_resp.status_code == 200
    logs = _extract_notification_logs(admin_api.json_or_raise(logs_resp, "restock_logs"))
    matched = [
        log
        for log in logs
        if "316" in json.dumps(log, ensure_ascii=False)
        and ("네이비" in json.dumps(log, ensure_ascii=False) or "재입고" in json.dumps(log, ensure_ascii=False) or "RESTOCK" in json.dumps(log, ensure_ascii=False).upper())
    ]
    assert len(matched) >= 1


# 21. F13 x F7
@pytest.mark.tier3
@pytest.mark.f13
@pytest.mark.f7
def test_t3_f13_f7_verified_buyer_badge_respects_order_and_cancel_state(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=False)
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=317,
        name="T3 구매인증리뷰 상품",
        category="TOP",
        price=32000,
        options=[{"color": "블랙", "size": "M", "stock": 10}],
    )
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)

    buyer_ok = api_client_factory(srv)
    buyer_ok.register("buyer_ok_317@e2e.local", "ReviewOk123!", name="정상구매자")
    ok_order = create_order_via_api(
        buyer_ok,
        items=[{"num": 317, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_name="정상구매자",
        buyer_phone="010-1307-0001",
    )
    _mark_paid_and_delivered(admin, _order_id(ok_order))

    buyer_cancel = api_client_factory(srv)
    buyer_cancel.register("buyer_cancel_317@e2e.local", "ReviewCancel123!", name="취소구매자")
    cancel_order = create_order_via_api(
        buyer_cancel,
        items=[{"num": 317, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_name="취소구매자",
        buyer_phone="010-1307-0002",
    )
    assert buyer_cancel.post("/api/orders/self-cancel", json={"order_code": cancel_order["order_code"]}).status_code == 200

    uploaded_urls: list[str] = []
    try:
        up_resp = buyer_ok.post(
            "/api/reviews/upload-image",
            files={"image": ("review.png", create_tiny_png_bytes(), "image/png")},
        )
        assert up_resp.status_code == 200, up_resp.text
        img_url = buyer_ok.json_or_raise(up_resp, "upload_rev_img")["url"]
        uploaded_urls.append(img_url)

        rev_ok = buyer_ok.post(
            "/api/reviews/add",
            json={"product_num": 317, "rating": 5, "content": "실제 구매 후 만족합니다!", "image_url": img_url},
        )
        assert rev_ok.status_code == 200, rev_ok.text

        rev_cancel = buyer_cancel.post(
            "/api/reviews/add",
            json={"product_num": 317, "rating": 4, "content": "주문 취소한 사용자 리뷰입니다."},
        )
        assert rev_cancel.status_code == 200, rev_cancel.text

        prod_resp = buyer_ok.get("/api/products/317")
        assert prod_resp.status_code == 200
        reviews = prod_resp.json()["reviews"]
        ok_item = next(r for r in reviews if "실제 구매 후 만족합니다" in r["content"])
        cancel_item = next(r for r in reviews if "주문 취소한 사용자 리뷰" in r["content"])
        assert bool(ok_item.get("is_verified_buyer")) is True
        assert bool(cancel_item.get("is_verified_buyer")) is False

        html_resp = buyer_ok.get("/product/317")
        assert html_resp.status_code == 200
        assert img_url in html_resp.text
        assert "구매" in html_resp.text
    finally:
        for url in uploaded_urls:
            _cleanup_uploaded_review_png(url)


# 22. F13 x F18
@pytest.mark.tier3
@pytest.mark.f13
@pytest.mark.f18
def test_t3_f13_f18_admin_review_moderation_writes_audit_log(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    member = api_client_factory(srv)
    member.register("t3_f13_f18@e2e.local", "ReviewMod123!", name="리뷰모더회원")
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)

    uploaded_urls: list[str] = []
    try:
        tok = uuid.uuid4().hex[:6]
        up = member.post(
            "/api/reviews/upload-image",
            files={"image": ("mod.png", create_tiny_png_bytes(), "image/png")},
        )
        assert up.status_code == 200, up.text
        img_url = member.json_or_raise(up, "upload_mod_img")["url"]
        uploaded_urls.append(img_url)

        add_resp = member.post(
            "/api/reviews/add",
            json={"product_num": 1, "rating": 5, "content": f"T3-리뷰모더레이션-{tok}", "image_url": img_url},
        )
        assert add_resp.status_code == 200, add_resp.text

        rev_list = admin.get("/api/admin/reviews").json().get("reviews") or []
        target = next(r for r in rev_list if tok in str(r.get("content", "")))
        rev_id = int(target["id"])

        mod_reply = admin.post(
            "/api/admin/reviews/moderate",
            json={"review_id": rev_id, "id": rev_id, "action": "reply", "admin_reply": f"감사합니다-{tok}"},
        )
        assert mod_reply.status_code == 200, mod_reply.text

        mod_hide = admin.post(
            "/api/admin/reviews/moderate",
            json={"review_id": rev_id, "id": rev_id, "action": "hide", "is_hidden": True},
        )
        assert mod_hide.status_code == 200, mod_hide.text

        logs_resp = admin.get("/api/admin/audit-logs")
        assert logs_resp.status_code == 200
        logs = _extract_audit_logs(admin.json_or_raise(logs_resp, "review_audit"))
        matched = [
            json.dumps(entry, ensure_ascii=False)
            for entry in logs
            if str(rev_id) in json.dumps(entry, ensure_ascii=False)
            and "REVIEW" in json.dumps(entry, ensure_ascii=False).upper()
        ]
        assert len(matched) >= 2
    finally:
        for url in uploaded_urls:
            _cleanup_uploaded_review_png(url)


# 23. F14 x F18
@pytest.mark.tier3
@pytest.mark.f14
@pytest.mark.f18
def test_t3_f14_f18_unpaid_auto_cancel_restores_coupon_points_stock_and_audits(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=False)
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=318,
        name="T3 미입금자동취소 상품",
        category="OUTER",
        price=40000,
        options=[{"color": "차콜", "size": "M", "stock": 5}],
    )
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    _update_store_info(admin, shipping_fee=0, free_shipping_threshold=0, unpaid_cancel_hours=1, point_earn_rate=10)
    _create_coupon_as_admin(
        admin,
        code="T3EXPIRE",
        name="미입금만료복구",
        discount_type="FIXED",
        discount_value=5000,
        min_order_amount=10000,
    )

    member = api_client_factory(srv)
    member.register("t3_f14_f18@e2e.local", "ExpirePass123!", name="만료복구회원")
    pts_before = _earn_points_via_completed_order(member, admin, product_num=318, quantity=1, color="차콜")
    assert pts_before >= 500
    assert _get_product_total_stock(member, 318) == 4

    order_exp = create_order_via_api(
        member,
        items=[{"num": 318, "color": "차콜", "size": "M", "quantity": 1}],
        buyer_name="만료복구회원",
        buyer_phone="010-1418-1418",
        coupon_code="T3EXPIRE",
        points_used=500,
    )
    assert _get_product_total_stock(member, 318) == 3
    assert _get_points_balance(member) == pts_before - 500

    maint_resp = admin.post(
        "/api/admin/system/run-maintenance",
        json={
            "backdate_order_code": order_exp["order_code"],
            "backdate_hours": 5,
            "backdate_orders": [{"order_code": order_exp["order_code"], "hours": 5}],
        },
    )
    assert maint_resp.status_code == 200, maint_resp.text

    orders_lookup = member.get("/api/orders/lookup").json()["orders"]
    exp_after = next(o for o in orders_lookup if o["order_code"] == order_exp["order_code"])
    assert exp_after["order_status"] == "주문취소"
    assert _get_product_total_stock(member, 318) == 4
    assert _get_points_balance(member) == pts_before

    reval_coupon = member.post("/api/coupons/validate", json={"code": "T3EXPIRE", "subtotal": 40000})
    assert reval_coupon.status_code == 200
    assert reval_coupon.json().get("valid", True) is True

    audit_resp = admin.get("/api/admin/audit-logs")
    assert audit_resp.status_code == 200
    audit_dump = json.dumps(audit_resp.json(), ensure_ascii=False)
    assert order_exp["order_code"] in audit_dump or "MAINTENANCE" in audit_dump.upper() or "AUTO_CANCEL" in audit_dump.upper()


# 24. F15 x F14
@pytest.mark.tier3
@pytest.mark.f15
@pytest.mark.f14
def test_t3_f15_f14_price_change_blocks_checkout_without_consuming_coupon_or_points(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=False)
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=319,
        name="T3 가격변동차단 상품",
        category="TOP",
        price=30000,
        options=[{"color": "블랙", "size": "M", "stock": 10}],
    )
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    _update_store_info(admin, shipping_fee=0, free_shipping_threshold=0, point_earn_rate=10)
    _create_coupon_as_admin(
        admin,
        code="T3REVAL",
        name="재검증보호쿠폰",
        discount_type="FIXED",
        discount_value=4000,
        min_order_amount=20000,
    )

    member = api_client_factory(srv)
    member.register("t3_f15_f14@e2e.local", "RevalPass123!", name="재검증회원")
    pts_before = _earn_points_via_completed_order(member, admin, product_num=319, quantity=1)
    assert pts_before >= 500

    # Price increases from 30,000 -> 35,000 before checkout submit.
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=319,
        name="T3 가격변동차단 상품",
        category="TOP",
        price=35000,
        options=[{"color": "블랙", "size": "M", "stock": 9}],
    )

    reval_resp = member.post(
        "/api/cart/revalidate",
        json={
            "items": [{"num": 319, "color": "블랙", "size": "M", "quantity": 1, "expected_price": 30000}],
            "coupon_code": "T3REVAL",
            "points_used": 500,
        },
    )
    assert reval_resp.status_code in (200, 409)
    reval_data = member.json_or_raise(reval_resp, "price_changed_reval")
    assert reval_data.get("price_changed") is True or reval_data.get("valid") is False

    create_resp = member.post(
        "/api/orders/create",
        json={
            "items": [{"num": 319, "color": "블랙", "size": "M", "quantity": 1, "expected_price": 30000}],
            "expected_items": [{"num": 319, "color": "블랙", "size": "M", "quantity": 1, "expected_price": 30000}],
            "expected_total": 25500,
            "buyer_name": "재검증회원",
            "buyer_phone": "010-1514-1514",
            "zipcode": "06236",
            "address": "서울특별시 강남구 테헤란로 152",
            "address_detail": "101호",
            "source_page": "CART_CHECKOUT",
            "payment_method": "BANK",
            "coupon_code": "T3REVAL",
            "points_used": 500,
        },
    )
    assert create_resp.status_code in (400, 409)
    assert _get_points_balance(member) == pts_before

    coupon_still_ok = member.post("/api/coupons/validate", json={"code": "T3REVAL", "subtotal": 35000})
    assert coupon_still_ok.status_code == 200
    assert coupon_still_ok.json().get("valid", True) is True


# 25. F17 x F16
@pytest.mark.tier3
@pytest.mark.f17
@pytest.mark.f16
def test_t3_f17_f16_seo_sitemap_robots_and_opengraph_for_dynamic_products(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=False)
    guest = api_client_factory(srv)
    uploads_dir = Path(__file__).resolve().parents[2] / "static" / "uploads" / "products"
    created_png = uploads_dir / "t3_seo_320.png"
    try:
        seeded = seed_product_via_sync(
            srv.base_url,
            srv.sync_secret,
            num=320,
            name="T3 SEO 테스트 블라우스",
            category="TOP",
            price=48000,
            options=[{"color": "아이보리", "size": "M", "stock": 8}],
            image_url="/static/uploads/products/t3_seo_320.png",
            image_bytes=create_tiny_png_bytes(),
        )

        sm_resp = guest.get("/sitemap.xml")
        assert sm_resp.status_code == 200
        root = ET.fromstring(sm_resp.text)
        locs = [el.text or "" for el in root.iter() if el.tag.endswith("loc")]
        assert any("/product/320" in loc for loc in locs)
        assert not any("/admin" in loc for loc in locs)
        assert not any("/checkout" in loc for loc in locs)

        rb_resp = guest.get("/robots.txt")
        assert rb_resp.status_code == 200
        assert "Sitemap:" in rb_resp.text
        assert "Disallow: /admin" in rb_resp.text
        assert "Disallow: /api/" in rb_resp.text

        detail_resp = guest.get("/product/320")
        assert detail_resp.status_code == 200
        assert 'property="og:title"' in detail_resp.text or "property='og:title'" in detail_resp.text
        assert "T3 SEO 테스트 블라우스" in detail_resp.text
        assert 'property="og:image"' in detail_resp.text or "property='og:image'" in detail_resp.text
        assert seeded["image_url"] in detail_resp.text

        cat_resp = guest.get("/api/products?exclude_sold_out=1&min_price=40000&max_price=60000")
        assert cat_resp.status_code == 200
        nums = {int(p["num"]) for p in guest.json_or_raise(cat_resp)["products"]}
        assert 320 in nums
    finally:
        created_png.unlink(missing_ok=True)
