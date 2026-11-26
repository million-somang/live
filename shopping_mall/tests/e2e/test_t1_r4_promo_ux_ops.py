"""Tier 1 opaque-box E2E tests for R4 features (F14–F18): happy paths."""

from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from typing import Any

import pytest

from e2e.helpers import (
    assert_no_horizontal_overflow,
    assert_no_main_internal_vertical_scroll,
    attach_page_diagnostics,
    create_order_via_api,
    create_tiny_png_bytes,
    seed_product_via_sync,
)


def _get_points_balance(data: dict[str, Any]) -> int:
    for key in ("balance", "points_balance", "points", "total_points"):
        if key in data and data[key] is not None:
            return int(data[key])
    raise KeyError(f"Points balance key not found in {data!r}")


def _get_points_ledger(data: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("ledger", "history", "logs", "items"):
        val = data.get(key)
        if isinstance(val, list):
            return val
    return []


def _extract_meta_content(html: str, prop: str) -> str:
    pat1 = re.compile(
        rf'<meta\s+[^>]*property=["\']{re.escape(prop)}["\'][^>]*content=["\']([^"\']*)["\']',
        re.IGNORECASE,
    )
    pat2 = re.compile(
        rf'<meta\s+[^>]*content=["\']([^"\']*)["\'][^>]*property=["\']{re.escape(prop)}["\']',
        re.IGNORECASE,
    )
    m = pat1.search(html) or pat2.search(html)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# Feature F14: R4 웰컴 쿠폰, 라이브 할인 코드, 적립금(포인트)
# ---------------------------------------------------------------------------


@pytest.mark.tier1
@pytest.mark.f14
def test_t1_f14_01_welcome_coupon_auto_issued_on_register(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    member = api_client_factory(srv)
    member.register("welcome@e2e.local", "WelcomePass1!", name="웰컴회원")

    resp = member.get("/api/mypage/coupons")
    assert resp.status_code == 200
    data = member.json_or_raise(resp, "f14_01_coupons")
    assert data["status"] == "ok"
    coupons = data.get("coupons", [])
    assert len(coupons) >= 1
    assert any(
        (c.get("status") in (None, "AVAILABLE", "UNUSED"))
        and int(c.get("discount_value", 0)) > 0
        for c in coupons
    )


@pytest.mark.tier1
@pytest.mark.f14
def test_t1_f14_02_admin_live_promo_code_crud_and_validate(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)

    save_resp = admin.post(
        "/api/admin/coupons/save",
        json={
            "code": "LIVE5000",
            "name": "라이브 전용 5000원 할인",
            "discount_type": "FIXED",
            "discount_value": 5000,
            "min_order_amount": 20000,
            "live_only": True,
            "is_active": True,
        },
    )
    assert save_resp.status_code == 200

    list_resp = admin.get("/api/admin/coupons")
    assert list_resp.status_code == 200
    coupons = admin.json_or_raise(list_resp)["coupons"]
    matched = [c for c in coupons if c.get("code") == "LIVE5000"]
    assert len(matched) == 1
    coupon_id = matched[0]["id"]

    member = api_client_factory(srv)
    member.register("live_promo@e2e.local", "PromoPass1!")
    val_resp = member.post(
        "/api/coupons/validate",
        json={"code": "LIVE5000", "subtotal": 29000, "source_page": "LIVE"},
    )
    assert val_resp.status_code == 200
    val_data = member.json_or_raise(val_resp, "f14_02_validate")
    assert val_data["status"] == "ok"
    discount_amt = val_data.get("discount_amount")
    if discount_amt is None and isinstance(val_data.get("coupon"), dict):
        discount_amt = val_data["coupon"].get("discount_amount")
    assert int(discount_amt) == 5000

    del_resp = admin.post("/api/admin/coupons/delete", json={"id": coupon_id, "coupon_id": coupon_id})
    assert del_resp.status_code == 200
    after_coupons = admin.json_or_raise(admin.get("/api/admin/coupons"))["coupons"]
    assert all(c.get("code") != "LIVE5000" for c in after_coupons)


@pytest.mark.tier1
@pytest.mark.f14
def test_t1_f14_03_checkout_applies_coupon_and_points_server_total(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=9143,
        name="F14 쿠폰 적립금 테스트 상품",
        price=30000,
        options=[{"color": "블랙", "size": "M", "stock": 20}],
    )

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    assert (
        admin.post(
            "/api/admin/coupons/save",
            json={
                "code": "SAVE5000",
                "name": "5천원 할인 쿠폰",
                "discount_type": "FIXED",
                "discount_value": 5000,
                "min_order_amount": 10000,
                "live_only": False,
                "is_active": True,
            },
        ).status_code
        == 200
    )
    assert (
        admin.post(
            "/api/admin/store-info",
            json={
                "shipping_fee": 3000,
                "free_shipping_threshold": 0,
                "point_earn_rate": 10,
            },
        ).status_code
        == 200
    )

    member = api_client_factory(srv)
    member.register("pts_buyer@e2e.local", "PointsPass1!")

    order1 = create_order_via_api(
        member,
        items=[{"num": 9143, "color": "블랙", "size": "M", "quantity": 1}],
    )
    assert order1["total_amount"] == 33000

    assert (
        admin.post(
            "/api/admin/order-status",
            json={"order_id": order1["order_id"], "payment_status": "입금완료"},
        ).status_code
        == 200
    )
    assert (
        admin.post(
            "/api/admin/order-status",
            json={"order_id": order1["order_id"], "order_status": "배송완료"},
        ).status_code
        == 200
    )

    pts_before = member.json_or_raise(member.get("/api/mypage/points"))
    assert _get_points_balance(pts_before) == 3000

    order2 = create_order_via_api(
        member,
        items=[{"num": 9143, "color": "블랙", "size": "M", "quantity": 1}],
        coupon_code="SAVE5000",
        points_used=2000,
    )
    assert order2["coupon_discount"] == 5000
    assert order2["points_used"] == 2000
    assert order2["total_amount"] == 26000

    pts_after = member.json_or_raise(member.get("/api/mypage/points"))
    assert _get_points_balance(pts_after) == 1000


@pytest.mark.tier1
@pytest.mark.f14
def test_t1_f14_04_points_earned_on_delivery_complete_and_ledger_visible(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    assert (
        admin.post("/api/admin/store-info", json={"point_earn_rate": 5}).status_code
        == 200
    )

    member = api_client_factory(srv)
    member.register("ledger_u@e2e.local", "LedgerPass1!")
    order = create_order_via_api(
        member,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
    )

    admin.post(
        "/api/admin/order-status",
        json={"order_id": order["order_id"], "payment_status": "입금완료"},
    )
    pts_mid = member.json_or_raise(member.get("/api/mypage/points"))
    assert _get_points_balance(pts_mid) == 0

    admin.post(
        "/api/admin/order-status",
        json={"order_id": order["order_id"], "order_status": "배송완료"},
    )
    pts_after = member.json_or_raise(member.get("/api/mypage/points"))
    assert _get_points_balance(pts_after) == 1450
    ledger = _get_points_ledger(pts_after)
    assert any(
        order["order_code"] in str(entry) and "1450" in str(entry)
        for entry in ledger
    )


@pytest.mark.tier1
@pytest.mark.f14
def test_t1_f14_05_cancel_and_refund_restore_points_and_coupon(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=9145,
        name="F14 복원 테스트 상품",
        price=30000,
        options=[{"color": "블랙", "size": "M", "stock": 20}],
    )
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    admin.post("/api/admin/store-info", json={"point_earn_rate": 10})
    admin.post(
        "/api/admin/coupons/save",
        json={
            "code": "ONEUSE3000",
            "name": "1회용 쿠폰",
            "discount_type": "FIXED",
            "discount_value": 3000,
            "min_order_amount": 10000,
            "usage_limit": 1,
            "live_only": False,
            "is_active": True,
        },
    )

    member = api_client_factory(srv)
    member.register("restore_u@e2e.local", "RestorePass1!")

    order1 = create_order_via_api(
        member,
        items=[{"num": 9145, "color": "블랙", "size": "M", "quantity": 1}],
    )
    admin.post(
        "/api/admin/order-status",
        json={"order_id": order1["order_id"], "payment_status": "입금완료", "order_status": "배송완료"},
    )
    assert _get_points_balance(member.json_or_raise(member.get("/api/mypage/points"))) == 3000

    order2 = create_order_via_api(
        member,
        items=[{"num": 9145, "color": "블랙", "size": "M", "quantity": 1}],
        coupon_code="ONEUSE3000",
        points_used=1500,
    )
    assert _get_points_balance(member.json_or_raise(member.get("/api/mypage/points"))) == 1500

    cancel_resp = member.post(
        "/api/orders/self-cancel",
        json={"order_code": order2["order_code"], "claim_reason": "단순변심"},
    )
    assert cancel_resp.status_code == 200

    assert _get_points_balance(member.json_or_raise(member.get("/api/mypage/points"))) == 3000
    reval_coupon = member.post(
        "/api/coupons/validate",
        json={"code": "ONEUSE3000", "subtotal": 30000, "source_page": "CART_CHECKOUT"},
    )
    assert reval_coupon.status_code == 200


# ---------------------------------------------------------------------------
# Feature F15: R4 장바구니 선택 주문, 옵션 변경, 결제 전 실시간 재검증
# ---------------------------------------------------------------------------


@pytest.mark.tier1
@pytest.mark.f15
def test_t1_f15_01_cart_checkbox_selective_order_keeps_unselected(
    live_server,
    desktop_page,
):
    diag = attach_page_diagnostics(desktop_page)
    desktop_page.goto(f"{live_server.base_url}/", wait_until="domcontentloaded")
    desktop_page.evaluate(
        "items => window.saveMallCart(items)",
        [
            {
                "num": 1,
                "name": "골라방 클래식 코튼 티셔츠",
                "price": 29000,
                "consumer_price": 39000,
                "image_url": "",
                "color": "블랙",
                "size": "M",
                "quantity": 1,
            },
            {
                "num": 6,
                "name": "골라방 미니멀 레더 크로스백",
                "price": 35000,
                "consumer_price": 45000,
                "image_url": "",
                "color": "블랙",
                "size": "ONE",
                "quantity": 1,
            },
        ],
    )

    resp = desktop_page.goto(f"{live_server.base_url}/cart", wait_until="domcontentloaded")
    assert resp is not None and resp.status == 200

    row_6 = desktop_page.locator("#cartItemsContainer .ax-cart-row", has_text="크로스백")
    assert row_6.count() == 1
    cb_6 = row_6.locator("input[type='checkbox']").first
    assert cb_6.count() == 1
    cb_6.uncheck()

    desktop_page.wait_for_timeout(200)
    assert "29,000원" in desktop_page.locator("#cartSubtotalText").inner_text()

    desktop_page.locator("#btnGoCheckout").click()
    desktop_page.wait_for_url(re.compile(r"/checkout"), timeout=8000)

    desktop_page.locator("#coBuyerName").fill("선택주문자")
    desktop_page.locator("#coBuyerPhone").fill("010-7777-1111")
    desktop_page.locator("#coZipcode").fill("06236")
    desktop_page.locator("#coAddress").fill("서울특별시 강남구 테헤란로 152")
    desktop_page.locator("#coGuestPassword").fill("sel1234")
    desktop_page.locator("#coSubmitBtn").click()
    desktop_page.wait_for_url(re.compile(r"/(order-complete/ORD-|orders)"), timeout=10000)

    remaining_cart = desktop_page.evaluate("() => window.getMallCart()")
    remaining_nums = [int(it["num"]) for it in remaining_cart]
    assert 6 in remaining_nums
    assert 1 not in remaining_nums
    diag.assert_clean("f15_01_selective_checkout")


@pytest.mark.tier1
@pytest.mark.f15
def test_t1_f15_02_cart_option_change_updates_item_and_subtotal(
    live_server,
    desktop_page,
):
    diag = attach_page_diagnostics(desktop_page)
    desktop_page.goto(f"{live_server.base_url}/", wait_until="domcontentloaded")
    desktop_page.evaluate(
        "items => window.saveMallCart(items)",
        [
            {
                "num": 1,
                "name": "골라방 클래식 코튼 티셔츠",
                "price": 29000,
                "consumer_price": 39000,
                "image_url": "",
                "color": "블랙",
                "size": "M",
                "quantity": 1,
            }
        ],
    )

    resp = desktop_page.goto(f"{live_server.base_url}/cart", wait_until="domcontentloaded")
    assert resp is not None and resp.status == 200

    row_1 = desktop_page.locator("#cartItemsContainer .ax-cart-row").first
    selects = row_1.locator("select")
    assert selects.count() >= 1
    if selects.count() == 1:
        options_text = selects.first.locator("option").all_inner_texts()
        target_idx = next(
            i for i, txt in enumerate(options_text) if "화이트" in txt and "L" in txt
        )
        selects.first.select_option(index=target_idx)
    else:
        selects.nth(0).select_option("화이트")
        selects.nth(1).select_option("L")

    desktop_page.wait_for_timeout(250)
    cart_after = desktop_page.evaluate("() => window.getMallCart()")
    assert len(cart_after) == 1
    assert cart_after[0]["color"] == "화이트"
    assert cart_after[0]["size"] == "L"
    assert "29,000원" in desktop_page.locator("#cartSubtotalText").inner_text()
    diag.assert_clean("f15_02_option_change")


@pytest.mark.tier1
@pytest.mark.f15
def test_t1_f15_03_api_cart_revalidate_returns_current_prices_and_stock(
    live_server,
    guest_api,
):
    resp = guest_api.post(
        "/api/cart/revalidate",
        json={
            "items": [
                {"num": 1, "color": "블랙", "size": "M", "quantity": 2, "price": 29000}
            ],
            "source_page": "CART_CHECKOUT",
        },
    )
    assert resp.status_code == 200
    data = guest_api.json_or_raise(resp, "f15_03_revalidate")
    assert data.get("status") == "ok"
    assert (data.get("ok") is True) or (data.get("valid") is True)
    assert bool(data.get("price_changed")) is False
    assert bool(data.get("sold_out")) is False
    items = data.get("items", [])
    assert len(items) == 1
    assert int(items[0].get("unit_price", items[0].get("price", 0))) == 29000


@pytest.mark.tier1
@pytest.mark.f15
def test_t1_f15_04_price_change_detected_by_revalidate_and_order_create(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    prod = seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=9154,
        name="F15 가격변동 검증 상품",
        price=30000,
        options=[{"color": "블랙", "size": "M", "stock": 10}],
    )

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    upd = admin.post(
        "/api/admin/products/update",
        json={
            "id": prod["id"],
            "num": 9154,
            "name": "F15 가격변동 검증 상품",
            "price": 35000,
            "category": "TOP",
            "options": [{"color": "블랙", "size": "M", "stock": 10}],
        },
    )
    assert upd.status_code == 200

    guest = api_client_factory(srv)
    reval = guest.post(
        "/api/cart/revalidate",
        json={
            "items": [
                {"num": 9154, "color": "블랙", "size": "M", "quantity": 1, "price": 30000}
            ],
            "source_page": "CART_CHECKOUT",
        },
    )
    assert reval.status_code == 200
    reval_data = guest.json_or_raise(reval, "f15_04_revalidate")
    assert bool(reval_data.get("price_changed")) is True
    assert int(reval_data["items"][0].get("unit_price", reval_data["items"][0].get("price", 0))) == 35000

    stale_order = guest.post(
        "/api/orders/create",
        json={
            "items": [{"num": 9154, "color": "블랙", "size": "M", "quantity": 1}],
            "expected_items": [
                {"num": 9154, "color": "블랙", "size": "M", "quantity": 1, "unit_price": 30000}
            ],
            "expected_total": 34000,
            "buyer_name": "가격변동차단",
            "buyer_phone": "010-2222-9999",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "guest_password": "price1234",
            "source_page": "CART_CHECKOUT",
            "payment_method": "BANK",
        },
    )
    assert stale_order.status_code in (400, 409)


@pytest.mark.tier1
@pytest.mark.f15
def test_t1_f15_05_live_token_applies_live_price_while_store_uses_normal_price(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    guest = api_client_factory(srv)
    sync_resp = guest.sync_from_obs(
        {"action": "live_state", "is_live": True, "pinned_product_num": 1},
        srv.sync_secret,
    )
    assert sync_resp.status_code == 200

    live_html = guest.get("/live")
    assert live_html.status_code == 200
    m = re.search(
        r'id=["\']mallBaseBootstrap["\'][^>]*data-live-order-token=["\']([^"\']+)["\']',
        live_html.text,
    )
    if m:
        raw_token = html.unescape(m.group(1)).strip()
        live_token = json.loads(raw_token) if raw_token.startswith('"') else raw_token
    else:
        m = re.search(r'"live_order_token"\s*:\s*"([^"]+)"', live_html.text)
        assert m is not None
        live_token = m.group(1)
    assert isinstance(live_token, str) and len(live_token) > 0

    live_order = create_order_via_api(
        guest,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        source_page="LIVE",
        live_order_token=live_token,
        guest_password="live1234",
    )
    assert live_order["subtotal_amount"] == 24000

    store_order = create_order_via_api(
        guest,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        source_page="CART_CHECKOUT",
        guest_password="store1234",
    )
    assert store_order["subtotal_amount"] == 29000


# ---------------------------------------------------------------------------
# Feature F16: R4 상품 필터/페이지네이션, 최근 본 상품, 재입고 알림
# ---------------------------------------------------------------------------


@pytest.mark.tier1
@pytest.mark.f16
def test_t1_f16_01_exclude_sold_out_filter_on_home_and_api(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=False)
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=101,
        name="재고있는상품_101",
        price=25000,
        options=[{"color": "블랙", "size": "M", "stock": 5}],
    )
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=102,
        name="품절상품_102",
        price=30000,
        options=[{"color": "블랙", "size": "M", "stock": 0}],
    )

    guest = api_client_factory(srv)
    api_resp = guest.get("/api/products?exclude_sold_out=1")
    assert api_resp.status_code == 200
    products = guest.json_or_raise(api_resp)["products"]
    assert [p["num"] for p in products] == [101]

    home_resp = guest.get("/?exclude_sold_out=1")
    assert home_resp.status_code == 200
    assert "재고있는상품_101" in home_resp.text
    assert "품절상품_102" not in home_resp.text


@pytest.mark.tier1
@pytest.mark.f16
def test_t1_f16_02_min_price_max_price_filter_on_home_and_api(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=False)
    seed_product_via_sync(srv.base_url, srv.sync_secret, num=201, name="저가상품_201", price=15000)
    seed_product_via_sync(srv.base_url, srv.sync_secret, num=202, name="중가상품_202", price=35000)
    seed_product_via_sync(srv.base_url, srv.sync_secret, num=203, name="고가상품_203", price=75000)

    guest = api_client_factory(srv)
    api_resp = guest.get("/api/products?min_price=20000&max_price=50000")
    assert api_resp.status_code == 200
    products = guest.json_or_raise(api_resp)["products"]
    assert [p["num"] for p in products] == [202]

    home_resp = guest.get("/?min_price=20000&max_price=50000")
    assert home_resp.status_code == 200
    assert "중가상품_202" in home_resp.text
    assert "저가상품_201" not in home_resp.text
    assert "고가상품_203" not in home_resp.text


@pytest.mark.tier1
@pytest.mark.f16
def test_t1_f16_03_pagination_page_and_per_page_no_duplicates(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=False)
    for num in range(301, 308):
        seed_product_via_sync(
            srv.base_url,
            srv.sync_secret,
            num=num,
            name=f"페이지상품_{num}",
            price=20000 + num,
        )

    guest = api_client_factory(srv)
    p1 = guest.json_or_raise(guest.get("/api/products?page=1&per_page=3"))["products"]
    p2 = guest.json_or_raise(guest.get("/api/products?page=2&per_page=3"))["products"]
    p3 = guest.json_or_raise(guest.get("/api/products?page=3&per_page=3"))["products"]

    assert len(p1) == 3
    assert len(p2) == 3
    assert len(p3) == 1

    seen = [p["num"] for p in (p1 + p2 + p3)]
    assert len(seen) == 7
    assert set(seen) == set(range(301, 308))

    home_p2 = guest.get("/?page=2&per_page=3")
    assert home_p2.status_code == 200


@pytest.mark.tier1
@pytest.mark.f16
def test_t1_f16_04_recently_viewed_floating_bar_records_visits(
    live_server,
    desktop_page,
):
    diag = attach_page_diagnostics(desktop_page)
    desktop_page.goto(f"{live_server.base_url}/product/1", wait_until="domcontentloaded")
    desktop_page.goto(f"{live_server.base_url}/product/3", wait_until="domcontentloaded")
    desktop_page.goto(f"{live_server.base_url}/", wait_until="domcontentloaded")

    storage_dump = desktop_page.evaluate(
        """() => {
            const out = {};
            for (let i = 0; i < localStorage.length; i++) {
                const k = localStorage.key(i);
                out[k] = localStorage.getItem(k);
            }
            return out;
        }"""
    )
    joined = json.dumps(storage_dump, ensure_ascii=False)
    assert "1" in joined and "3" in joined

    recent_bar = (
        desktop_page.locator("aside, nav, section, div")
        .filter(has=desktop_page.locator("a[href='/product/3']"))
        .filter(has=desktop_page.locator("a[href='/product/1']"))
        .filter(has_not=desktop_page.locator(".ax-product-grid"))
        .filter(has_not=desktop_page.locator("a[href='/product/6']"))
    )
    assert recent_bar.count() >= 1
    recent_bar.first.wait_for(state="visible", timeout=3000)

    assert_no_horizontal_overflow(desktop_page)
    assert_no_main_internal_vertical_scroll(desktop_page)
    diag.assert_clean("f16_04_recent_viewed")


@pytest.mark.tier1
@pytest.mark.f16
def test_t1_f16_05_restock_alert_subscribe_and_replenish_notification(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    prod = seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=9165,
        name="F16 재입고 알림 상품",
        price=48000,
        options=[{"color": "네이비", "size": "M", "stock": 0}],
    )

    guest = api_client_factory(srv)
    sub_resp = guest.post(
        "/api/restock-alerts/subscribe",
        json={
            "product_num": 9165,
            "color": "네이비",
            "size": "M",
            "contact_type": "EMAIL",
            "contact_value": "restock@e2e.local",
        },
    )
    assert sub_resp.status_code == 200

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    alerts_before = admin.json_or_raise(admin.get("/api/admin/restock-alerts")).get("alerts", [])
    pending = [
        a
        for a in alerts_before
        if int(a.get("product_num", 0)) == 9165
        and a.get("contact_value") == "restock@e2e.local"
    ]
    assert len(pending) == 1
    assert str(pending[0].get("status", "")).upper() == "PENDING"

    replenish = admin.post(
        "/api/admin/products/update",
        json={
            "id": prod["id"],
            "num": 9165,
            "name": "F16 재입고 알림 상품",
            "price": 48000,
            "category": "TOP",
            "options": [{"color": "네이비", "size": "M", "stock": 10}],
        },
    )
    assert replenish.status_code == 200

    alerts_after = admin.json_or_raise(admin.get("/api/admin/restock-alerts")).get("alerts", [])
    notified = [
        a
        for a in alerts_after
        if int(a.get("product_num", 0)) == 9165
        and a.get("contact_value") == "restock@e2e.local"
    ]
    assert len(notified) == 1
    assert str(notified[0].get("status", "")).upper() == "NOTIFIED"

    logs = admin.json_or_raise(admin.get("/api/admin/notifications/logs")).get("logs", [])
    assert any(
        "restock@e2e.local" in str(entry) and "RESTOCK" in str(entry).upper()
        for entry in logs
    )


# ---------------------------------------------------------------------------
# Feature F17: R4 SEO (OpenGraph, sitemap.xml, robots.txt)
# ---------------------------------------------------------------------------


@pytest.mark.tier1
@pytest.mark.f17
def test_t1_f17_01_home_opengraph_meta_tags(live_server, guest_api):
    resp = guest_api.get("/")
    assert resp.status_code == 200
    og_title = _extract_meta_content(resp.text, "og:title")
    og_desc = _extract_meta_content(resp.text, "og:description")
    og_image = _extract_meta_content(resp.text, "og:image")
    og_url = _extract_meta_content(resp.text, "og:url")
    assert "골라방 LIVE" in og_title
    assert len(og_desc) > 0
    assert len(og_image) > 0
    assert len(og_url) > 0


@pytest.mark.tier1
@pytest.mark.f17
def test_t1_f17_02_product_detail_opengraph_meta_tags(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)

    up_resp = admin.post(
        "/api/admin/upload/image",
        files={"image": ("seo_test.png", create_tiny_png_bytes(), "image/png")},
    )
    assert up_resp.status_code == 200
    up_data = admin.json_or_raise(up_resp)
    image_url = up_data.get("image_url") or up_data.get("url")
    assert image_url

    add_resp = admin.post(
        "/api/admin/products/add",
        json={
            "num": 9172,
            "name": "SEO 테스트 코트",
            "price": 99000,
            "category": "OUTER",
            "image_url": image_url,
            "options": [{"color": "블랙", "size": "M", "stock": 5}],
        },
    )
    assert add_resp.status_code == 200
    added_num = admin.json_or_raise(add_resp)["product"]["num"]

    guest = api_client_factory(srv)
    detail = guest.get(f"/product/{added_num}")
    assert detail.status_code == 200
    assert "SEO 테스트 코트" in _extract_meta_content(detail.text, "og:title")
    assert image_url in _extract_meta_content(detail.text, "og:image")
    assert len(_extract_meta_content(detail.text, "og:description")) > 0
    assert f"/product/{added_num}" in _extract_meta_content(detail.text, "og:url")


@pytest.mark.tier1
@pytest.mark.f17
def test_t1_f17_03_sitemap_xml_valid_and_dynamic_add_delete(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    guest = api_client_factory(srv)
    sm1 = guest.get("/sitemap.xml")
    assert sm1.status_code == 200
    assert "xml" in (sm1.headers.get("Content-Type") or "").lower()
    root1 = ET.fromstring(sm1.text)
    locs1 = [el.text or "" for el in root1.iter() if el.tag.endswith("loc")]
    assert any(loc.endswith("/") for loc in locs1)
    for num in range(1, 7):
        assert any(loc.endswith(f"/product/{num}") for loc in locs1)

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    add_resp = admin.post(
        "/api/admin/products/add",
        json={
            "num": 9173,
            "name": "사이트맵 동적 상품",
            "price": 31000,
            "category": "TOP",
            "options": [{"color": "블랙", "size": "M", "stock": 5}],
        },
    )
    assert add_resp.status_code == 200
    new_prod = admin.json_or_raise(add_resp)["product"]
    new_id = new_prod["id"]
    new_num = new_prod["num"]

    sm2 = guest.get("/sitemap.xml")
    assert sm2.status_code == 200
    assert f"/product/{new_num}" in sm2.text

    del_resp = admin.post("/api/admin/products/delete", json={"id": new_id, "num": new_num})
    assert del_resp.status_code == 200

    sm3 = guest.get("/sitemap.xml")
    assert sm3.status_code == 200
    assert f"/product/{new_num}" not in sm3.text


@pytest.mark.tier1
@pytest.mark.f17
def test_t1_f17_04_robots_txt_directives_and_sitemap(live_server, guest_api):
    resp = guest_api.get("/robots.txt")
    assert resp.status_code == 200
    assert "text/plain" in (resp.headers.get("Content-Type") or "").lower()
    body = resp.text
    assert "User-agent: *" in body
    assert "Allow: /" in body
    assert "Disallow: /admin" in body
    assert "Disallow: /mypage" in body
    assert "Disallow: /orders" in body
    assert "Sitemap:" in body and "/sitemap.xml" in body


@pytest.mark.tier1
@pytest.mark.f17
def test_t1_f17_05_opengraph_and_layout_on_cs_and_policy(
    live_server,
    desktop_page,
):
    diag = attach_page_diagnostics(desktop_page)
    for route in ("/cs", "/policy/terms"):
        resp = desktop_page.goto(f"{live_server.base_url}{route}", wait_until="domcontentloaded")
        assert resp is not None and resp.status == 200
        html = desktop_page.content()
        assert len(_extract_meta_content(html, "og:title")) > 0
        assert len(_extract_meta_content(html, "og:url")) > 0
        assert_no_horizontal_overflow(desktop_page)
        assert_no_main_internal_vertical_scroll(desktop_page)
        diag.assert_clean(f"f17_05_{route}")


# ---------------------------------------------------------------------------
# Feature F18: R4 운영 자동화 (미입금 자동취소, DB 백업, 관리자 감사 로그)
# ---------------------------------------------------------------------------


@pytest.mark.tier1
@pytest.mark.f18
def test_t1_f18_01_unpaid_order_auto_cancel_restores_stock_coupon_points(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=9181,
        name="F18 자동취소 복구 상품",
        price=30000,
        options=[{"color": "블랙", "size": "M", "stock": 10}],
    )

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    admin.post(
        "/api/admin/coupons/save",
        json={
            "code": "AUTO3000",
            "name": "자동취소 복구 쿠폰",
            "discount_type": "FIXED",
            "discount_value": 3000,
            "min_order_amount": 10000,
            "usage_limit": 1,
            "live_only": False,
            "is_active": True,
        },
    )
    admin.post(
        "/api/admin/store-info",
        json={"point_earn_rate": 10, "unpaid_cancel_hours": 24},
    )

    member = api_client_factory(srv)
    member.register("autocancel_u@e2e.local", "AutoPass1!")

    order1 = create_order_via_api(
        member,
        items=[{"num": 9181, "color": "블랙", "size": "M", "quantity": 1}],
    )
    admin.post(
        "/api/admin/order-status",
        json={"order_id": order1["order_id"], "payment_status": "입금완료", "order_status": "배송완료"},
    )
    assert _get_points_balance(member.json_or_raise(member.get("/api/mypage/points"))) == 3000

    order2 = create_order_via_api(
        member,
        items=[{"num": 9181, "color": "블랙", "size": "M", "quantity": 2}],
        coupon_code="AUTO3000",
        points_used=1000,
    )
    assert member.json_or_raise(member.get("/api/products/9181"))["product"]["total_stock"] == 7
    assert _get_points_balance(member.json_or_raise(member.get("/api/mypage/points"))) == 2000

    maint_resp = admin.post(
        "/api/admin/system/run-maintenance",
        json={"backdate_orders": [{"order_code": order2["order_code"], "hours": 30}]},
    )
    assert maint_resp.status_code == 200

    orders = member.json_or_raise(member.get("/api/orders/lookup"))["orders"]
    ord2_after = [o for o in orders if o["order_code"] == order2["order_code"]][0]
    assert ord2_after["order_status"] == "주문취소"

    assert member.json_or_raise(member.get("/api/products/9181"))["product"]["total_stock"] == 9
    assert _get_points_balance(member.json_or_raise(member.get("/api/mypage/points"))) == 3000
    assert (
        member.post(
            "/api/coupons/validate",
            json={"code": "AUTO3000", "subtotal": 30000, "source_page": "CART_CHECKOUT"},
        ).status_code
        == 200
    )

    logs = admin.json_or_raise(admin.get("/api/admin/notifications/logs")).get("logs", [])
    assert any(order2["order_code"] in str(entry) for entry in logs)


@pytest.mark.tier1
@pytest.mark.f18
def test_t1_f18_02_manual_backup_creates_and_lists_backup(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)

    bk_resp = admin.post("/api/admin/system/backup", json={})
    assert bk_resp.status_code == 200
    bk_data = admin.json_or_raise(bk_resp, "f18_02_backup")
    assert bk_data["status"] == "ok"
    filename = bk_data.get("filename") or (bk_data.get("backup") or {}).get("filename")
    assert isinstance(filename, str) and (filename.endswith(".db") or ".sqlite" in filename)

    list_resp = admin.get("/api/admin/system/backups")
    assert list_resp.status_code == 200
    backups = admin.json_or_raise(list_resp).get("backups", [])
    matched = [b for b in backups if b.get("filename") == filename]
    assert len(matched) == 1
    assert int(matched[0].get("size", 0)) > 0


@pytest.mark.tier1
@pytest.mark.f18
def test_t1_f18_03_download_backup_returns_valid_sqlite_binary(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)

    bk_data = admin.json_or_raise(admin.post("/api/admin/system/backup", json={}))
    filename = bk_data.get("filename") or (bk_data.get("backup") or {}).get("filename")
    assert filename

    dl_resp = admin.get(f"/api/admin/system/backup/{filename}")
    assert dl_resp.status_code == 200
    assert dl_resp.content[:16] == b"SQLite format 3\x00"


@pytest.mark.tier1
@pytest.mark.f18
def test_t1_f18_04_admin_mutations_record_audit_logs(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)

    assert (
        admin.post(
            "/api/admin/products/add",
            json={
                "num": 9184,
                "name": "감사로그 상품",
                "price": 22000,
                "category": "TOP",
                "options": [{"color": "블랙", "size": "M", "stock": 5}],
            },
        ).status_code
        == 200
    )
    assert (
        admin.post(
            "/api/admin/store-info",
            json={"shipping_fee": 3500},
        ).status_code
        == 200
    )
    assert (
        admin.post(
            "/api/admin/coupons/save",
            json={
                "code": "AUDIT500",
                "name": "감사로그 쿠폰",
                "discount_type": "FIXED",
                "discount_value": 500,
                "is_active": True,
            },
        ).status_code
        == 200
    )

    logs_resp = admin.get("/api/admin/audit-logs")
    assert logs_resp.status_code == 200
    logs_data = admin.json_or_raise(logs_resp, "f18_04_audit")
    logs = logs_data.get("logs", [])
    assert len(logs) >= 3
    joined = json.dumps(logs, ensure_ascii=False)
    assert srv.admin_email in joined
    assert "AUDIT500" in joined


@pytest.mark.tier1
@pytest.mark.f18
def test_t1_f18_05_admin_ops_tab_renders_audit_backups_and_maintenance(
    live_server,
    desktop_context_factory,
    admin_storage_state,
):
    ctx = desktop_context_factory(storage_state=admin_storage_state)
    page = ctx.new_page()
    diag = attach_page_diagnostics(page)
    resp = page.goto(f"{live_server.base_url}/admin", wait_until="domcontentloaded")
    assert resp is not None and resp.status == 200

    ops_tab = page.get_by_role("button", name=re.compile(r"운영|백업|감사|시스템"))
    assert ops_tab.count() > 0
    ops_tab.first.click()
    page.wait_for_timeout(300)

    body_text = page.locator("body").inner_text()
    assert any(k in body_text for k in ("백업", "감사", "자동"))

    assert_no_horizontal_overflow(page)
    assert_no_main_internal_vertical_scroll(page)
    diag.assert_clean("f18_05_admin_ops_tab")
