"""Tier 2 opaque-box boundary, negative, auth, and security E2E tests for R4 (F14–F18)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

import pytest

from e2e.helpers import (
    assert_no_horizontal_overflow,
    assert_no_main_internal_vertical_scroll,
    attach_page_diagnostics,
    create_order_via_api,
    seed_product_via_sync,
)


def _get_points_balance(data: dict[str, Any]) -> int:
    for key in ("balance", "points_balance", "points", "total_points"):
        if key in data and data[key] is not None:
            return int(data[key])
    raise KeyError(f"Points balance key not found in {data!r}")


# ---------------------------------------------------------------------------
# Feature F14 Boundaries & Security
# ---------------------------------------------------------------------------


@pytest.mark.tier2
@pytest.mark.f14
def test_t2_f14_01_expired_inactive_min_order_and_used_coupon_rejected(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)

    admin.post(
        "/api/admin/coupons/save",
        json={
            "code": "EXPIRED1",
            "name": "만료 쿠폰",
            "discount_type": "FIXED",
            "discount_value": 2000,
            "end_at": "2020-01-01 00:00:00",
            "is_active": True,
        },
    )
    admin.post(
        "/api/admin/coupons/save",
        json={
            "code": "INACTIVE1",
            "name": "비활성 쿠폰",
            "discount_type": "FIXED",
            "discount_value": 2000,
            "is_active": False,
        },
    )
    admin.post(
        "/api/admin/coupons/save",
        json={
            "code": "MIN50K",
            "name": "최소주문금액 5만",
            "discount_type": "FIXED",
            "discount_value": 3000,
            "min_order_amount": 50000,
            "is_active": True,
        },
    )
    admin.post(
        "/api/admin/coupons/save",
        json={
            "code": "ONCE1",
            "name": "1회 제한 쿠폰",
            "discount_type": "FIXED",
            "discount_value": 2000,
            "usage_limit": 1,
            "is_active": True,
        },
    )

    member = api_client_factory(srv)
    member.register("coupon_neg@e2e.local", "PassNeg1!")

    # Consume ONCE1 once
    create_order_via_api(
        member,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        coupon_code="ONCE1",
    )

    for code in ("EXPIRED1", "INACTIVE1", "MIN50K", "ONCE1"):
        v_resp = member.post(
            "/api/coupons/validate",
            json={"code": code, "subtotal": 29000, "source_page": "CART_CHECKOUT"},
        )
        assert v_resp.status_code == 400, f"Expected 400 validating {code}, got {v_resp.status_code}"

        o_resp = member.post(
            "/api/orders/create",
            json={
                "items": [{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
                "buyer_name": "쿠폰거부",
                "buyer_phone": "010-1234-5678",
                "zipcode": "06236",
                "address": "서울시 강남구 테헤란로 152",
                "source_page": "CART_CHECKOUT",
                "payment_method": "BANK",
                "coupon_code": code,
            },
        )
        assert o_resp.status_code == 400, f"Expected 400 creating order with {code}"


@pytest.mark.tier2
@pytest.mark.f14
def test_t2_f14_02_live_only_coupon_rejected_on_standard_store(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    admin.post(
        "/api/admin/coupons/save",
        json={
            "code": "ONLYLIVE",
            "name": "라이브 전용 쿠폰",
            "discount_type": "FIXED",
            "discount_value": 4000,
            "live_only": True,
            "is_active": True,
        },
    )

    member = api_client_factory(srv)
    member.register("liveonly_chk@e2e.local", "PassL123!")

    v_resp = member.post(
        "/api/coupons/validate",
        json={"code": "ONLYLIVE", "subtotal": 29000, "source_page": "CART_CHECKOUT"},
    )
    assert v_resp.status_code == 400

    o_resp = member.post(
        "/api/orders/create",
        json={
            "items": [{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
            "buyer_name": "일반결제",
            "buyer_phone": "010-2222-3333",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "source_page": "CART_CHECKOUT",
            "payment_method": "BANK",
            "coupon_code": "ONLYLIVE",
        },
    )
    assert o_resp.status_code == 400


@pytest.mark.tier2
@pytest.mark.f14
def test_t2_f14_03_excess_negative_points_and_guest_points_rejected(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    admin.post("/api/admin/store-info", json={"point_earn_rate": 10})

    member = api_client_factory(srv)
    member.register("pts_neg@e2e.local", "PassP123!")

    # Zero-balance member trying to spend 500 or -100
    for bad_pts in (500, -100, 999999):
        resp = member.post(
            "/api/orders/create",
            json={
                "items": [{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
                "buyer_name": "포인트오류",
                "buyer_phone": "010-3333-4444",
                "zipcode": "06236",
                "address": "서울시 강남구 테헤란로 152",
                "source_page": "CART_CHECKOUT",
                "payment_method": "BANK",
                "points_used": bad_pts,
            },
        )
        assert resp.status_code == 400

    # Guest trying to spend points
    guest = api_client_factory(srv)
    g_resp = guest.post(
        "/api/orders/create",
        json={
            "items": [{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
            "buyer_name": "비회원포인트",
            "buyer_phone": "010-4444-5555",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "guest_password": "guest1234",
            "source_page": "CART_CHECKOUT",
            "payment_method": "BANK",
            "points_used": 500,
        },
    )
    assert g_resp.status_code in (400, 401)


@pytest.mark.tier2
@pytest.mark.f14
def test_t2_f14_04_repeated_delivery_complete_idempotent_points(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=9148,
        name="F14 멱등 적립금 상품",
        price=30000,
        options=[{"color": "블랙", "size": "M", "stock": 20}],
    )
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    admin.post("/api/admin/store-info", json={"point_earn_rate": 10})

    member = api_client_factory(srv)
    member.register("idem_pts@e2e.local", "PassI123!")

    order1 = create_order_via_api(
        member,
        items=[{"num": 9148, "color": "블랙", "size": "M", "quantity": 1}],
    )
    admin.post(
        "/api/admin/order-status",
        json={"order_id": order1["order_id"], "payment_status": "입금완료", "order_status": "배송완료"},
    )
    # Repeat identical transition
    admin.post(
        "/api/admin/order-status",
        json={"order_id": order1["order_id"], "order_status": "배송완료"},
    )

    assert _get_points_balance(member.json_or_raise(member.get("/api/mypage/points"))) == 3000

    # Order 2 uses 1000 points, then cancel/refund twice -> refunded exactly once
    order2 = create_order_via_api(
        member,
        items=[{"num": 9148, "color": "블랙", "size": "M", "quantity": 1}],
        points_used=1000,
    )
    assert _get_points_balance(member.json_or_raise(member.get("/api/mypage/points"))) == 2000

    admin.post(
        "/api/admin/order-status",
        json={"order_id": order2["order_id"], "order_status": "주문취소"},
    )
    admin.post(
        "/api/admin/order-status",
        json={"order_id": order2["order_id"], "order_status": "주문취소"},
    )
    assert _get_points_balance(member.json_or_raise(member.get("/api/mypage/points"))) == 3000


@pytest.mark.tier2
@pytest.mark.f14
def test_t2_f14_05_coupon_admin_rbac_and_csrf_enforced(
    live_server,
    guest_api,
    member_api,
    admin_api,
):
    assert guest_api.get("/api/admin/coupons").status_code == 401
    assert member_api.get("/api/admin/coupons").status_code == 403

    for path, body in (
        ("/api/admin/coupons/save", {"code": "HACK100", "discount_type": "FIXED", "discount_value": 100}),
        ("/api/admin/coupons/delete", {"id": 1}),
    ):
        assert guest_api.post(path, json=body).status_code == 401
        assert member_api.post(path, json=body).status_code == 403
        assert admin_api.post(path, json=body, include_csrf=False).status_code == 403


# ---------------------------------------------------------------------------
# Feature F15 Boundaries & Security
# ---------------------------------------------------------------------------


@pytest.mark.tier2
@pytest.mark.f15
def test_t2_f15_01_cart_zero_checked_items_blocks_checkout(
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

    desktop_page.goto(f"{live_server.base_url}/cart", wait_until="domcontentloaded")
    cb = desktop_page.locator("#cartItemsContainer input[type='checkbox']").first
    assert cb.count() == 1
    cb.uncheck()
    desktop_page.wait_for_timeout(200)

    btn = desktop_page.locator("#btnGoCheckout")
    if btn.is_disabled():
        assert btn.is_disabled()
    else:
        dialog_seen = []
        desktop_page.on("dialog", lambda d: (dialog_seen.append(d.message), d.accept()))
        btn.click()
        desktop_page.wait_for_timeout(300)
        assert "/checkout" not in desktop_page.url
    diag.assert_clean("f15_01_zero_checked")


@pytest.mark.tier2
@pytest.mark.f15
def test_t2_f15_02_tampered_expected_total_or_expected_items_blocked(
    live_server,
    guest_api,
):
    stock_before = guest_api.json_or_raise(guest_api.get("/api/products/1"))["product"]["total_stock"]

    r1 = guest_api.post(
        "/api/orders/create",
        json={
            "items": [{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
            "expected_total": 1000,
            "buyer_name": "금액변조",
            "buyer_phone": "010-1234-9999",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "guest_password": "tamper1",
            "source_page": "CART_CHECKOUT",
            "payment_method": "BANK",
        },
    )
    assert r1.status_code in (400, 409)

    r2 = guest_api.post(
        "/api/orders/create",
        json={
            "items": [{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
            "expected_items": [
                {"num": 1, "color": "블랙", "size": "M", "quantity": 1, "unit_price": 100}
            ],
            "buyer_name": "단가변조",
            "buyer_phone": "010-1234-9999",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "guest_password": "tamper2",
            "source_page": "CART_CHECKOUT",
            "payment_method": "BANK",
        },
    )
    assert r2.status_code in (400, 409)

    stock_after = guest_api.json_or_raise(guest_api.get("/api/products/1"))["product"]["total_stock"]
    assert stock_after == stock_before


@pytest.mark.tier2
@pytest.mark.f15
def test_t2_f15_03_out_of_stock_and_qty_exceeding_stock_blocked(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=9158,
        name="F15 소량 재고 상품",
        price=25000,
        options=[{"color": "블랙", "size": "M", "stock": 2}],
    )
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=9159,
        name="F15 완전 품절 상품",
        price=25000,
        options=[{"color": "블랙", "size": "M", "stock": 0}],
    )

    guest = api_client_factory(srv)
    rev_qty = guest.json_or_raise(
        guest.post(
            "/api/cart/revalidate",
            json={
                "items": [{"num": 9158, "color": "블랙", "size": "M", "quantity": 5, "price": 25000}],
                "source_page": "CART_CHECKOUT",
            },
        )
    )
    assert (rev_qty.get("ok") is False) or (rev_qty.get("valid") is False) or bool(rev_qty.get("insufficient_stock"))

    rev_zero = guest.json_or_raise(
        guest.post(
            "/api/cart/revalidate",
            json={
                "items": [{"num": 9159, "color": "블랙", "size": "M", "quantity": 1, "price": 25000}],
                "source_page": "CART_CHECKOUT",
            },
        )
    )
    assert (rev_zero.get("ok") is False) or (rev_zero.get("valid") is False) or bool(rev_zero.get("sold_out"))

    for num, qty in ((9158, 5), (9159, 1)):
        o_resp = guest.post(
            "/api/orders/create",
            json={
                "items": [{"num": num, "color": "블랙", "size": "M", "quantity": qty}],
                "buyer_name": "품절차단",
                "buyer_phone": "010-5555-0000",
                "zipcode": "06236",
                "address": "서울시 강남구 테헤란로 152",
                "guest_password": "stock123",
                "source_page": "CART_CHECKOUT",
                "payment_method": "BANK",
            },
        )
        assert o_resp.status_code == 400


@pytest.mark.tier2
@pytest.mark.f15
def test_t2_f15_04_nonexistent_product_or_option_flagged_on_revalidate(
    live_server,
    guest_api,
):
    for bad_item in (
        {"num": 999999, "color": "블랙", "size": "M", "quantity": 1, "price": 10000},
        {"num": 1, "color": "없는색상", "size": "XXXL", "quantity": 1, "price": 29000},
    ):
        r = guest_api.post(
            "/api/cart/revalidate",
            json={"items": [bad_item], "source_page": "CART_CHECKOUT"},
        )
        assert r.status_code in (200, 400)
        if r.status_code == 200:
            d = guest_api.json_or_raise(r)
            assert (d.get("ok") is False) or (d.get("valid") is False) or bool(d.get("sold_out")) or bool(d.get("errors"))

        o = guest_api.post(
            "/api/orders/create",
            json={
                "items": [bad_item],
                "buyer_name": "없는옵션",
                "buyer_phone": "010-1234-1234",
                "zipcode": "06236",
                "address": "서울시 강남구 테헤란로 152",
                "guest_password": "badopt1",
                "source_page": "CART_CHECKOUT",
                "payment_method": "BANK",
            },
        )
        assert o.status_code == 400


@pytest.mark.tier2
@pytest.mark.f15
def test_t2_f15_05_forged_live_order_token_rejected_403(
    live_server,
    guest_api,
):
    resp = guest_api.post(
        "/api/orders/create",
        json={
            "items": [{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
            "buyer_name": "토큰위조",
            "buyer_phone": "010-8888-7777",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "guest_password": "token123",
            "source_page": "LIVE",
            "live_order_token": "forged.sig.token",
            "payment_method": "BANK",
        },
    )
    assert resp.status_code == 403
    data = guest_api.json_or_raise(resp)
    assert data.get("code") == "INVALID_LIVE_TOKEN"


# ---------------------------------------------------------------------------
# Feature F16 Boundaries & Security
# ---------------------------------------------------------------------------


@pytest.mark.tier2
@pytest.mark.f16
def test_t2_f16_01_min_price_gt_max_price_and_negative_price_safe(
    live_server,
    guest_api,
):
    r1 = guest_api.get("/api/products?min_price=90000&max_price=10000")
    assert r1.status_code in (200, 400)
    if r1.status_code == 200:
        assert guest_api.json_or_raise(r1)["products"] == []

    r2 = guest_api.get("/api/products?min_price=-500&max_price=-10")
    assert r2.status_code in (200, 400)

    home = guest_api.get("/?min_price=90000&max_price=10000")
    assert home.status_code == 200


@pytest.mark.tier2
@pytest.mark.f16
def test_t2_f16_02_invalid_and_oob_pagination_bounds(
    live_server,
    guest_api,
):
    r_zero = guest_api.get("/api/products?page=0&per_page=0")
    assert r_zero.status_code == 200
    assert isinstance(guest_api.json_or_raise(r_zero)["products"], list)

    r_oob = guest_api.get("/api/products?page=999&per_page=10")
    assert r_oob.status_code == 200
    assert guest_api.json_or_raise(r_oob)["products"] == []


@pytest.mark.tier2
@pytest.mark.f16
def test_t2_f16_03_restock_subscribe_instock_or_invalid_contact_rejected(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=9168,
        name="F16 재입고 검증 상품",
        price=29000,
        options=[{"color": "블랙", "size": "M", "stock": 0}],
    )
    guest = api_client_factory(srv)

    # In-stock product rejected
    assert (
        guest.post(
            "/api/restock-alerts/subscribe",
            json={
                "product_num": 1,
                "color": "블랙",
                "size": "M",
                "contact_type": "EMAIL",
                "contact_value": "ok@e2e.local",
            },
        ).status_code
        == 400
    )

    # Invalid contact on sold-out product
    for bad_contact in ("", "not-an-email"):
        assert (
            guest.post(
                "/api/restock-alerts/subscribe",
                json={
                    "product_num": 9168,
                    "color": "블랙",
                    "size": "M",
                    "contact_type": "EMAIL",
                    "contact_value": bad_contact,
                },
            ).status_code
            == 400
        )

    # Nonexistent product
    assert (
        guest.post(
            "/api/restock-alerts/subscribe",
            json={
                "product_num": 999999,
                "color": "블랙",
                "size": "M",
                "contact_type": "EMAIL",
                "contact_value": "ok@e2e.local",
            },
        ).status_code
        in (400, 404)
    )


@pytest.mark.tier2
@pytest.mark.f16
def test_t2_f16_04_duplicate_restock_subscription_idempotent(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=9169,
        name="F16 중복 구독 상품",
        price=32000,
        options=[{"color": "화이트", "size": "L", "stock": 0}],
    )
    guest = api_client_factory(srv)
    payload = {
        "product_num": 9169,
        "color": "화이트",
        "size": "L",
        "contact_type": "EMAIL",
        "contact_value": "dup@e2e.local",
    }
    assert guest.post("/api/restock-alerts/subscribe", json=payload).status_code == 200
    r2 = guest.post("/api/restock-alerts/subscribe", json=payload)
    assert r2.status_code in (200, 409)

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    alerts = admin.json_or_raise(admin.get("/api/admin/restock-alerts")).get("alerts", [])
    pending_matches = [
        a
        for a in alerts
        if int(a.get("product_num", 0)) == 9169
        and a.get("contact_value") == "dup@e2e.local"
        and str(a.get("status", "")).upper() == "PENDING"
    ]
    assert len(pending_matches) == 1


@pytest.mark.tier2
@pytest.mark.f16
def test_t2_f16_05_mobile_recently_viewed_bar_no_overflow_or_scroll(
    live_server,
    mobile_page,
):
    diag = attach_page_diagnostics(mobile_page)
    for route in ("/product/1", "/product/2", "/", "/product/3"):
        resp = mobile_page.goto(f"{live_server.base_url}{route}", wait_until="domcontentloaded")
        assert resp is not None and resp.status == 200
        assert_no_horizontal_overflow(mobile_page)
        assert_no_main_internal_vertical_scroll(mobile_page)
    diag.assert_clean("f16_05_mobile_recent_bar")


# ---------------------------------------------------------------------------
# Feature F17 Boundaries & Security
# ---------------------------------------------------------------------------


@pytest.mark.tier2
@pytest.mark.f17
def test_t2_f17_01_product_without_image_renders_valid_og_image(
    live_server,
    guest_api,
):
    resp = guest_api.get("/product/1")
    assert resp.status_code == 200
    m = re.search(
        r'<meta\s+[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
        resp.text,
        re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r'<meta\s+[^>]*content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']',
            resp.text,
            re.IGNORECASE,
        )
    assert m is not None
    assert len(m.group(1).strip()) > 0


@pytest.mark.tier2
@pytest.mark.f17
def test_t2_f17_02_special_chars_escaped_in_og_and_sitemap(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=9179,
        name='특가 <테스트> "에디션" & 100%',
        price=45000,
    )
    guest = api_client_factory(srv)
    detail = guest.get("/product/9179")
    assert detail.status_code == 200
    assert '<테스트> "에디션"' not in detail.text

    sm = guest.get("/sitemap.xml")
    assert sm.status_code == 200
    root = ET.fromstring(sm.text)
    assert any((el.text or "").endswith("/product/9179") for el in root.iter() if el.tag.endswith("loc"))


@pytest.mark.tier2
@pytest.mark.f17
def test_t2_f17_03_empty_catalog_sitemap_still_valid_xml(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=False)
    guest = api_client_factory(srv)
    sm = guest.get("/sitemap.xml")
    assert sm.status_code == 200
    root = ET.fromstring(sm.text)
    locs = [el.text or "" for el in root.iter() if el.tag.endswith("loc")]
    assert any(loc.endswith("/") for loc in locs)


@pytest.mark.tier2
@pytest.mark.f17
def test_t2_f17_04_robots_txt_disallows_private_and_admin_paths(
    live_server,
    guest_api,
):
    resp = guest_api.get("/robots.txt")
    assert resp.status_code == 200
    lines = [ln.strip() for ln in resp.text.splitlines() if ln.strip()]
    assert "Disallow: /" not in lines
    assert any(ln == "Disallow: /admin" or ln.startswith("Disallow: /admin") for ln in lines)
    assert any(ln == "Disallow: /mypage" or ln.startswith("Disallow: /mypage") for ln in lines)
    assert any(ln == "Disallow: /orders" or ln.startswith("Disallow: /orders") for ln in lines)
    assert any(ln.startswith("Sitemap:") and "/sitemap.xml" in ln for ln in lines)


@pytest.mark.tier2
@pytest.mark.f17
def test_t2_f17_05_deleted_product_404_or_redirect_and_absent_from_sitemap(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    prod = seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=9175,
        name="삭제 SEO 테스트 상품",
        price=29000,
    )
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    assert admin.post("/api/admin/products/delete", json={"id": prod["id"], "num": 9175}).status_code == 200

    guest = api_client_factory(srv)
    sm = guest.get("/sitemap.xml")
    assert sm.status_code == 200
    assert "/product/9175" not in sm.text

    page_resp = guest.get("/product/9175", allow_redirects=False)
    assert page_resp.status_code in (302, 404)
    assert guest.get("/api/products/9175").status_code == 404


# ---------------------------------------------------------------------------
# Feature F18 Boundaries & Security
# ---------------------------------------------------------------------------


@pytest.mark.tier2
@pytest.mark.f18
def test_t2_f18_01_paid_and_non_expired_unpaid_orders_not_auto_cancelled(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    admin.post("/api/admin/store-info", json={"unpaid_cancel_hours": 24})

    guest = api_client_factory(srv)
    ord_a = create_order_via_api(
        guest,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        guest_password="ordA123",
    )
    ord_b = create_order_via_api(
        guest,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        guest_password="ordB123",
    )
    ord_c = create_order_via_api(
        guest,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        guest_password="ordC123",
    )
    admin.post(
        "/api/admin/order-status",
        json={"order_id": ord_c["order_id"], "payment_status": "입금완료"},
    )

    maint = admin.post(
        "/api/admin/system/run-maintenance",
        json={
            "backdate_orders": [
                {"order_code": ord_a["order_code"], "hours": 30},
                {"order_code": ord_c["order_code"], "hours": 30},
            ]
        },
    )
    assert maint.status_code == 200

    all_orders = admin.json_or_raise(admin.get("/api/orders/lookup")).get("orders", [])
    by_code = {o["order_code"]: o for o in all_orders}
    assert by_code[ord_a["order_code"]]["order_status"] == "주문취소"
    assert by_code[ord_b["order_code"]]["order_status"] == "결제대기"
    assert by_code[ord_b["order_code"]]["payment_status"] == "입금대기"
    assert by_code[ord_c["order_code"]]["payment_status"] == "입금완료"
    assert by_code[ord_c["order_code"]]["order_status"] != "주문취소"


@pytest.mark.tier2
@pytest.mark.f18
def test_t2_f18_02_unpaid_cancel_hours_zero_disables_auto_cancel(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    assert (
        admin.post("/api/admin/store-info", json={"unpaid_cancel_hours": 0}).status_code
        == 200
    )

    guest = api_client_factory(srv)
    ord_unpaid = create_order_via_api(
        guest,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        guest_password="zero123",
    )

    maint = admin.post(
        "/api/admin/system/run-maintenance",
        json={"backdate_orders": [{"order_code": ord_unpaid["order_code"], "hours": 100}]},
    )
    assert maint.status_code == 200

    all_orders = admin.json_or_raise(admin.get("/api/orders/lookup")).get("orders", [])
    matched = [o for o in all_orders if o["order_code"] == ord_unpaid["order_code"]][0]
    assert matched["order_status"] == "결제대기"
    assert matched["payment_status"] == "입금대기"


@pytest.mark.tier2
@pytest.mark.f18
def test_t2_f18_03_run_maintenance_twice_idempotent_stock_and_points(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=9183,
        name="F18 멱등 자동취소 상품",
        price=30000,
        options=[{"color": "블랙", "size": "M", "stock": 10}],
    )
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    admin.post(
        "/api/admin/store-info",
        json={"point_earn_rate": 10, "unpaid_cancel_hours": 24},
    )

    member = api_client_factory(srv)
    member.register("idem_maint@e2e.local", "PassM123!")

    ord1 = create_order_via_api(
        member,
        items=[{"num": 9183, "color": "블랙", "size": "M", "quantity": 1}],
    )
    admin.post(
        "/api/admin/order-status",
        json={"order_id": ord1["order_id"], "payment_status": "입금완료", "order_status": "배송완료"},
    )
    assert _get_points_balance(member.json_or_raise(member.get("/api/mypage/points"))) == 3000

    ord2 = create_order_via_api(
        member,
        items=[{"num": 9183, "color": "블랙", "size": "M", "quantity": 2}],
        points_used=1000,
    )
    assert member.json_or_raise(member.get("/api/products/9183"))["product"]["total_stock"] == 7

    assert (
        admin.post(
            "/api/admin/system/run-maintenance",
            json={"backdate_orders": [{"order_code": ord2["order_code"], "hours": 30}]},
        ).status_code
        == 200
    )
    assert admin.post("/api/admin/system/run-maintenance", json={}).status_code == 200

    assert member.json_or_raise(member.get("/api/products/9183"))["product"]["total_stock"] == 9
    assert _get_points_balance(member.json_or_raise(member.get("/api/mypage/points"))) == 3000


@pytest.mark.tier2
@pytest.mark.f18
def test_t2_f18_04_backup_download_path_traversal_blocked(
    live_server,
    admin_api,
):
    for bad_name in ("../../app.py", "..%2F..%2Fapp.py", "nonexistent.db"):
        resp = admin_api.get(f"/api/admin/system/backup/{bad_name}")
        assert resp.status_code in (400, 404)
        assert "Flask" not in resp.text


@pytest.mark.tier2
@pytest.mark.f18
def test_t2_f18_05_ops_rbac_csrf_and_backdate_disabled_in_prod(
    live_server,
    guest_api,
    member_api,
    admin_api,
    server_factory,
    api_client_factory,
):
    for path in ("/api/admin/audit-logs", "/api/admin/system/backups"):
        assert guest_api.get(path).status_code == 401
        assert member_api.get(path).status_code == 403

    for path in ("/api/admin/system/backup", "/api/admin/system/run-maintenance"):
        assert guest_api.post(path, json={}).status_code == 401
        assert member_api.post(path, json={}).status_code == 403
        assert admin_api.post(path, json={}, include_csrf=False).status_code == 403

    prod_srv = server_factory(env_overrides={"MALL_ALLOW_TEST_BACKDATE": "0"})
    prod_admin = api_client_factory(prod_srv)
    prod_admin.login(prod_srv.admin_email, prod_srv.admin_password)
    prod_admin.post("/api/admin/store-info", json={"unpaid_cancel_hours": 24})

    prod_guest = api_client_factory(prod_srv)
    fresh_order = create_order_via_api(
        prod_guest,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        guest_password="prod123",
    )

    resp = prod_admin.post(
        "/api/admin/system/run-maintenance",
        json={"backdate_orders": [{"order_code": fresh_order["order_code"], "hours": 100}]},
    )
    assert resp.status_code in (200, 403)

    orders = prod_admin.json_or_raise(prod_admin.get("/api/orders/lookup")).get("orders", [])
    matched = [o for o in orders if o["order_code"] == fresh_order["order_code"]][0]
    assert matched["order_status"] == "결제대기"
