"""Tier 1 opaque-box E2E tests for R2 Orders, Logistics, Claims, MyPage & Admin Members (F6–F9)."""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

import pytest
from playwright.sync_api import BrowserContext, Page, expect

from e2e.helpers import (
    MallApiClient,
    assert_no_horizontal_overflow,
    assert_no_main_internal_vertical_scroll,
    attach_page_diagnostics,
    create_order_via_api,
)


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


def _extract_users_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("users", "members", "items", "data"):
        val = payload.get(key)
        if isinstance(val, list):
            return val
    raise AssertionError(f"Could not find user list in payload: {payload!r}")


def _extract_user_order_count(user_obj: dict[str, Any]) -> int:
    for key in ("order_count", "orders_count", "total_orders", "paid_order_count"):
        if key in user_obj and user_obj[key] is not None:
            return int(user_obj[key])
    raise AssertionError(f"Could not find order count field in user object: {user_obj!r}")


def _extract_user_total_spent(user_obj: dict[str, Any]) -> int:
    for key in ("total_spent", "total_purchase_amount", "cumulative_amount", "total_amount", "paid_amount"):
        if key in user_obj and user_obj[key] is not None:
            return int(user_obj[key])
    raise AssertionError(f"Could not find cumulative spend field in user object: {user_obj!r}")


def _extract_notifications_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("logs", "notifications", "items", "data"):
        val = payload.get(key)
        if isinstance(val, list):
            return val
    raise AssertionError(f"Could not find notification logs list in payload: {payload!r}")


def _extract_addresses_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("addresses", "items", "data"):
        val = payload.get(key)
        if isinstance(val, list):
            return val
    raise AssertionError(f"Could not find addresses list in payload: {payload!r}")


# =============================================================================
# F6: Logistics (Carrier, Tracking, CSV Export/Import, 1-Click Tracking)
# =============================================================================


@pytest.mark.tier1
@pytest.mark.f6
def test_f6_t1_admin_single_tracking_save_and_mark_shipped(
    live_server,
    member_api,
    admin_api,
):
    order = create_order_via_api(
        member_api,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_phone="010-6100-0001",
    )

    resp = admin_api.post(
        "/api/admin/orders/tracking",
        json={
            "order_id": order["order_id"],
            "carrier_code": "cj",
            "carrier_name": "CJ대한통운",
            "tracking_number": "123456789012",
            "mark_shipped": True,
        },
    )
    assert resp.status_code == 200, resp.text
    data = admin_api.json_or_raise(resp, "save_tracking")
    assert data.get("status") == "ok"

    lookup = member_api.get("/api/orders/lookup")
    assert lookup.status_code == 200
    orders = member_api.json_or_raise(lookup)["orders"]
    found = next(o for o in orders if o["order_code"] == order["order_code"])
    assert found["tracking_number"] == "123456789012"
    assert found["carrier_name"] == "CJ대한통운"
    assert str(found.get("carrier_code", "")).lower() == "cj"
    assert found["order_status"] == "배송중"


@pytest.mark.tier1
@pytest.mark.f6
def test_f6_t1_export_shipping_csv_bom_and_required_columns(
    live_server,
    admin_api,
    api_client_factory,
):
    creator = api_client_factory(live_server)
    order = create_order_via_api(
        creator,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 2}],
        buyer_name="CSV발송수령인",
        buyer_phone="010-6200-0002",
        zipcode="06236",
        address="서울특별시 강남구 테헤란로 152",
        address_detail="303호",
        delivery_memo="문 앞 배송 부탁드립니다",
        guest_password="csv6200",
    )

    resp = admin_api.get("/api/admin/orders/export-shipping.csv")
    assert resp.status_code == 200, resp.text
    assert "text/csv" in (resp.headers.get("Content-Type") or "").lower()
    assert resp.content.startswith(b"\xef\xbb\xbf")

    decoded = resp.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(decoded))
    expected_cols = [
        "order_code",
        "buyer_name",
        "buyer_phone",
        "zipcode",
        "address",
        "address_detail",
        "delivery_memo",
        "items_summary",
        "carrier_code",
        "tracking_number",
    ]
    assert reader.fieldnames == expected_cols

    rows = [r for r in reader if r["order_code"] == order["order_code"]]
    assert len(rows) == 1
    row = rows[0]
    assert row["buyer_name"] == "CSV발송수령인"
    assert row["zipcode"] == "06236"
    assert row["address"] == "서울특별시 강남구 테헤란로 152"
    assert row["address_detail"] == "303호"
    assert row["delivery_memo"] == "문 앞 배송 부탁드립니다"
    assert "코튼 티셔츠" in row["items_summary"]


@pytest.mark.tier1
@pytest.mark.f6
def test_f6_t1_import_tracking_bulk_csv_and_json(
    live_server,
    member_api,
    admin_api,
):
    ord_a = create_order_via_api(
        member_api,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_phone="010-6300-0001",
    )
    ord_b = create_order_via_api(
        member_api,
        items=[{"num": 2, "color": "아이보리", "size": "FREE", "quantity": 1}],
        buyer_phone="010-6300-0002",
    )

    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf)
    writer.writerow(["order_code", "carrier_code", "carrier_name", "tracking_number", "mark_shipped"])
    writer.writerow([ord_a["order_code"], "cj", "CJ대한통운", "111122223333", "1"])
    csv_bytes = ("\ufeff" + csv_buf.getvalue()).encode("utf-8")

    csv_resp = admin_api.post(
        "/api/admin/orders/import-tracking",
        files={"file": ("tracking.csv", csv_bytes, "text/csv")},
    )
    assert csv_resp.status_code == 200, csv_resp.text
    assert admin_api.json_or_raise(csv_resp).get("status") == "ok"

    json_resp = admin_api.post(
        "/api/admin/orders/import-tracking",
        json={
            "rows": [
                {
                    "order_code": ord_b["order_code"],
                    "carrier_code": "hanjin",
                    "carrier_name": "한진택배",
                    "tracking_number": "444455556666",
                    "mark_shipped": True,
                }
            ]
        },
    )
    assert json_resp.status_code == 200, json_resp.text
    assert admin_api.json_or_raise(json_resp).get("status") == "ok"

    member_orders = member_api.json_or_raise(member_api.get("/api/orders/lookup"))["orders"]
    r_a = next(o for o in member_orders if o["order_code"] == ord_a["order_code"])
    assert r_a["tracking_number"] == "111122223333"

    r_b = next(o for o in member_orders if o["order_code"] == ord_b["order_code"])
    assert r_b["tracking_number"] == "444455556666"
    assert r_b["order_status"] == "배송중"


@pytest.mark.tier1
@pytest.mark.f6
def test_f6_t1_customer_orders_ui_one_click_tracking_link(
    live_server,
    member_api,
    admin_api,
    desktop_context_factory,
    member_storage_state,
):
    order = create_order_via_api(
        member_api,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
    )
    tr = admin_api.post(
        "/api/admin/orders/tracking",
        json={
            "order_id": order["order_id"],
            "carrier_code": "cj",
            "carrier_name": "CJ대한통운",
            "tracking_number": "123456789012",
            "mark_shipped": True,
        },
    )
    assert tr.status_code == 200

    ctx = desktop_context_factory(storage_state=member_storage_state)
    page = ctx.new_page()
    diag = attach_page_diagnostics(page)
    page.goto(f"{live_server.base_url}/orders", wait_until="domcontentloaded")

    card = page.locator(".ax-order-card", has_text=order["order_code"])
    expect(card).to_be_visible()
    expect(card).to_contain_text("CJ대한통운")
    expect(card).to_contain_text("123456789012")

    track_link = card.locator('a[href*="123456789012"]').first
    expect(track_link).to_be_visible()
    href = track_link.get_attribute("href") or ""
    assert href.startswith("http://") or href.startswith("https://")
    assert "123456789012" in href

    assert_no_horizontal_overflow(page)
    assert_no_main_internal_vertical_scroll(page)
    diag.assert_clean("f6_customer_tracking_ui")


@pytest.mark.tier1
@pytest.mark.f6
def test_f6_t1_admin_orders_ui_tracking_and_csv_controls(
    live_server,
    api_client_factory,
    desktop_context_factory,
    admin_storage_state,
):
    creator = api_client_factory(live_server)
    order = create_order_via_api(
        creator,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_name="어드민송장UI",
    )

    ctx = desktop_context_factory(storage_state=admin_storage_state)
    page = ctx.new_page()
    diag = attach_page_diagnostics(page)
    page.goto(f"{live_server.base_url}/admin", wait_until="domcontentloaded")
    page.locator("#adminTabOrders").click()
    expect(page.locator("#adminPanelOrders")).to_be_visible()

    csv_ctrl = page.locator(
        '#adminPanelOrders a[href*="export-shipping.csv"], #adminPanelOrders button:has-text("CSV")'
    ).first
    expect(csv_ctrl).to_be_visible()

    card = page.locator("#adminOrdersList .ax-admin-order-card", has_text=order["order_code"])
    expect(card).to_be_visible()

    carrier_select = card.locator(
        'select[name*="carrier"], select[id*="carrier"], select[aria-label*="택배"]'
    ).first
    if carrier_select.count() == 0:
        for sel in card.locator("select").all():
            opt_texts = " ".join(
                f"{o.get_attribute('value') or ''} {o.inner_text()}".lower()
                for o in sel.locator("option").all()
            )
            if any(tok in opt_texts for tok in ("cj", "hanjin", "대한통운", "한진")):
                carrier_select = sel
                break
    assert carrier_select.count() > 0, (
        f"Expected carrier <select> inside order card for {order['order_code']}, found none"
    )
    expect(carrier_select).to_be_visible()
    selected_carrier = False
    for opt in carrier_select.locator("option").all():
        val = opt.get_attribute("value") or ""
        label = opt.inner_text() or ""
        if "cj" in val.lower() or "대한통운" in label:
            carrier_select.select_option(val)
            selected_carrier = True
            break
    assert selected_carrier, "Expected CJ대한통운 option in carrier <select>"

    track_input = card.locator(
        'input[name*="tracking"], input[placeholder*="운송장"], input[placeholder*="송장"]'
    ).first
    expect(track_input).to_be_visible()
    track_input.fill("777788889999")

    save_btn = card.get_by_role("button", name=re.compile(r"송장|운송장|저장")).first
    save_btn.click()
    expect(page.locator("#mallToastContainer .ax-toast").last).to_be_visible()

    page.reload(wait_until="domcontentloaded")
    page.locator("#adminTabOrders").click()
    card_after = page.locator("#adminOrdersList .ax-admin-order-card", has_text=order["order_code"])
    expect(card_after).to_contain_text("777788889999")

    assert_no_horizontal_overflow(page)
    assert_no_main_internal_vertical_scroll(page)
    diag.assert_clean("f6_admin_tracking_ui")


# =============================================================================
# F7: Customer Self-Cancel & Exchange/Refund Claims
# =============================================================================


@pytest.mark.tier1
@pytest.mark.f7
def test_f7_t1_member_self_cancel_unpaid_order_and_stock_restore(
    live_server,
    member_api,
):
    before_prod = member_api.json_or_raise(member_api.get("/api/products/1"))["product"]
    initial_stock = next(
        int(o["stock"]) for o in before_prod["options"] if o["color"] == "블랙" and o["size"] == "M"
    )

    order = create_order_via_api(
        member_api,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 2}],
    )
    mid_prod = member_api.json_or_raise(member_api.get("/api/products/1"))["product"]
    mid_stock = next(
        int(o["stock"]) for o in mid_prod["options"] if o["color"] == "블랙" and o["size"] == "M"
    )
    assert mid_stock == initial_stock - 2

    cancel_resp = member_api.post(
        "/api/orders/self-cancel",
        json={"order_id": order["order_id"], "order_code": order["order_code"]},
    )
    assert cancel_resp.status_code == 200, cancel_resp.text
    cancel_data = member_api.json_or_raise(cancel_resp, "self_cancel")
    assert cancel_data.get("status") == "ok"
    updated_ord = cancel_data.get("order") or member_api.json_or_raise(
        member_api.get("/api/orders/lookup")
    )["orders"][0]
    assert updated_ord["order_status"] == "주문취소"
    assert int(updated_ord.get("stock_restored", 1)) == 1

    after_prod = member_api.json_or_raise(member_api.get("/api/products/1"))["product"]
    after_stock = next(
        int(o["stock"]) for o in after_prod["options"] if o["color"] == "블랙" and o["size"] == "M"
    )
    assert after_stock == initial_stock


@pytest.mark.tier1
@pytest.mark.f7
def test_f7_t1_guest_self_cancel_via_three_factor_credentials(
    live_server,
    api_client_factory,
):
    before_prod = api_client_factory(live_server).json_or_raise(
        api_client_factory(live_server).get("/api/products/1")
    )["product"]
    initial_stock = next(
        int(o["stock"]) for o in before_prod["options"] if o["color"] == "화이트" and o["size"] == "L"
    )

    creator = api_client_factory(live_server)
    order = create_order_via_api(
        creator,
        items=[{"num": 1, "color": "화이트", "size": "L", "quantity": 1}],
        buyer_phone="010-7788-9900",
        guest_password="guest7788",
    )

    fresh_guest = api_client_factory(live_server)
    fresh_guest.refresh_csrf("/orders")
    resp = fresh_guest.post(
        "/api/orders/self-cancel",
        json={
            "order_code": order["order_code"],
            "buyer_phone": "010-7788-9900",
            "guest_password": "guest7788",
        },
    )
    assert resp.status_code == 200, resp.text
    data = fresh_guest.json_or_raise(resp, "guest_self_cancel")
    assert data.get("status") == "ok"
    assert data["order"]["order_status"] == "주문취소"

    after_prod = fresh_guest.json_or_raise(fresh_guest.get("/api/products/1"))["product"]
    after_stock = next(
        int(o["stock"]) for o in after_prod["options"] if o["color"] == "화이트" and o["size"] == "L"
    )
    assert after_stock == initial_stock


@pytest.mark.tier1
@pytest.mark.f7
def test_f7_t1_self_claim_refund_request_and_admin_complete_restores_stock(
    live_server,
    member_api,
    admin_api,
):
    before_prod = member_api.json_or_raise(member_api.get("/api/products/2"))["product"]
    initial_stock = int(before_prod["options"][0]["stock"])

    order = create_order_via_api(
        member_api,
        items=[{"num": 2, "color": "아이보리", "size": "FREE", "quantity": 2}],
    )
    paid_resp = admin_api.post(
        "/api/admin/order-status",
        json={
            "order_id": order["order_id"],
            "payment_status": "입금완료",
            "order_status": "배송완료",
        },
    )
    assert paid_resp.status_code == 200

    claim_resp = member_api.post(
        "/api/orders/self-claim",
        json={
            "order_code": order["order_code"],
            "claim_type": "환불요청",
            "claim_reason": "사이즈가 맞지 않아 반품 요청합니다.",
        },
    )
    assert claim_resp.status_code == 200, claim_resp.text
    claim_data = member_api.json_or_raise(claim_resp, "self_claim_refund")
    assert claim_data["order"]["order_status"] == "환불요청"
    assert "사이즈가 맞지 않아 반품 요청합니다." in claim_data["order"]["claim_reason"]
    assert int(claim_data["order"].get("stock_restored", 0)) == 0

    mid_prod = member_api.json_or_raise(member_api.get("/api/products/2"))["product"]
    assert int(mid_prod["options"][0]["stock"]) == initial_stock - 2

    complete_resp = admin_api.post(
        "/api/admin/order-status",
        json={
            "order_id": order["order_id"],
            "payment_status": "환불완료",
            "order_status": "환불완료",
        },
    )
    assert complete_resp.status_code == 200
    complete_data = admin_api.json_or_raise(complete_resp)
    assert complete_data["stock_restored_now"] is True

    after_prod = member_api.json_or_raise(member_api.get("/api/products/2"))["product"]
    assert int(after_prod["options"][0]["stock"]) == initial_stock


@pytest.mark.tier1
@pytest.mark.f7
def test_f7_t1_self_claim_exchange_request_to_admin_claims_workflow(
    live_server,
    member_api,
    admin_api,
    desktop_context_factory,
    admin_storage_state,
):
    order = create_order_via_api(
        member_api,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
    )
    assert (
        admin_api.post(
            "/api/admin/order-status",
            json={
                "order_id": order["order_id"],
                "payment_status": "입금완료",
                "order_status": "배송완료",
            },
        ).status_code
        == 200
    )

    ex_resp = member_api.post(
        "/api/orders/self-claim",
        json={
            "order_code": order["order_code"],
            "claim_type": "교환요청",
            "claim_reason": "다른 색상으로 교환 원합니다",
        },
    )
    assert ex_resp.status_code == 200, ex_resp.text
    assert member_api.json_or_raise(ex_resp)["order"]["order_status"] == "교환요청"

    ctx = desktop_context_factory(storage_state=admin_storage_state)
    page = ctx.new_page()
    diag = attach_page_diagnostics(page)
    page.goto(f"{live_server.base_url}/admin", wait_until="domcontentloaded")
    page.locator("#adminTabClaims").click()
    claims_list = page.locator("#adminClaimsList")
    expect(claims_list).to_contain_text(order["order_code"])
    expect(claims_list).to_contain_text("교환요청")
    expect(claims_list).to_contain_text("다른 색상으로 교환 원합니다")
    diag.assert_clean("f7_admin_claims_list")

    done_resp = admin_api.post(
        "/api/admin/order-status",
        json={
            "order_id": order["order_id"],
            "payment_status": "입금완료",
            "order_status": "교환완료",
        },
    )
    assert done_resp.status_code == 200
    done_data = admin_api.json_or_raise(done_resp)
    assert done_data["stock_restored_now"] is False
    assert int(done_data["order"].get("stock_restored", 0)) == 0


@pytest.mark.tier1
@pytest.mark.f7
def test_f7_t1_orders_ui_self_cancel_and_claim_playwright_e2e(
    live_server,
    member_api,
    admin_api,
    desktop_context_factory,
    member_storage_state,
):
    ord_cancel = create_order_via_api(
        member_api,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
    )
    ord_claim = create_order_via_api(
        member_api,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
    )
    assert (
        admin_api.post(
            "/api/admin/order-status",
            json={
                "order_id": ord_claim["order_id"],
                "payment_status": "입금완료",
                "order_status": "배송완료",
            },
        ).status_code
        == 200
    )

    ctx = desktop_context_factory(storage_state=member_storage_state)
    page = ctx.new_page()
    diag = attach_page_diagnostics(page)
    page.on("dialog", lambda d: d.accept())

    page.goto(f"{live_server.base_url}/orders", wait_until="domcontentloaded")

    cancel_card = page.locator(".ax-order-card", has_text=ord_cancel["order_code"])
    expect(cancel_card).to_be_visible()
    cancel_card.get_by_role("button", name=re.compile(r"주문\s*취소|즉시\s*취소")).first.click()
    # Handle custom .ax-dialog-card confirmation if present
    confirm_modal_btn = page.locator(".ax-dialog-card").get_by_role(
        "button", name=re.compile(r"확인|취소하기|진행")
    )
    if confirm_modal_btn.count() > 0 and confirm_modal_btn.first.is_visible():
        confirm_modal_btn.first.click()
    expect(cancel_card).to_contain_text("주문취소", timeout=5000)

    claim_card = page.locator(".ax-order-card", has_text=ord_claim["order_code"])
    expect(claim_card).to_be_visible()
    claim_card.get_by_role("button", name=re.compile(r"교환|반품|환불")).first.click()

    # Claim form may be inline inside card or inside an .ax-dialog-card modal
    claim_scope = page.locator(".ax-dialog-card:visible")
    if claim_scope.count() == 0:
        claim_scope = claim_card

    claim_select = claim_scope.locator('select[name*="claim"], select').first
    if claim_select.count() > 0:
        claim_select.select_option("환불요청")

    reason_input = claim_scope.locator('textarea, input[name*="reason"], input[type="text"]').last
    reason_input.fill("실제 상품 색상이 상이하여 환불 요청합니다.")
    claim_scope.get_by_role("button", name=re.compile(r"접수|요청|제출|신청")).last.click()

    expect(claim_card).to_contain_text("환불요청", timeout=5000)
    assert_no_horizontal_overflow(page)
    assert_no_main_internal_vertical_scroll(page)
    diag.assert_clean("f7_orders_ui_cancel_claim")


# =============================================================================
# F8: Member MyPage, Address Book, Password Change/Reset, Withdrawal
# =============================================================================


@pytest.mark.tier1
@pytest.mark.f8
def test_f8_t1_mypage_profile_view_and_update_persists_to_checkout(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    member = api_client_factory(srv)
    member.register("f8_profile@e2e.local", "MemberPass123!")

    mp_resp = member.get("/mypage")
    assert mp_resp.status_code == 200

    upd = member.post(
        "/api/mypage/profile",
        json={
            "name": "수정된이름",
            "phone": "010-8888-7777",
            "zipcode": "04524",
            "address": "서울특별시 중구 세종대로 110",
            "address_detail": "시청별관 3층",
        },
    )
    assert upd.status_code == 200, upd.text
    assert member.json_or_raise(upd).get("status") == "ok"

    mp_after = member.get("/mypage")
    assert mp_after.status_code == 200
    assert "수정된이름" in mp_after.text
    assert "04524" in mp_after.text

    co_html = member.get("/checkout").text
    assert "수정된이름" in co_html
    assert "010-8888-7777" in co_html
    assert "04524" in co_html
    assert "서울특별시 중구 세종대로 110" in co_html


@pytest.mark.tier1
@pytest.mark.f8
def test_f8_t1_mypage_address_book_crud_and_default_switch(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    member = api_client_factory(srv)
    member.register(
        "f8_addr@e2e.local",
        "MemberPass123!",
        address="",
        zipcode="",
        address_detail="",
    )

    r1 = member.post(
        "/api/mypage/addresses/save",
        json={
            "label": "집",
            "recipient": "김집",
            "phone": "010-1111-2222",
            "zipcode": "06236",
            "address": "서울특별시 강남구 테헤란로 152",
            "address_detail": "101호",
            "is_default": True,
        },
    )
    assert r1.status_code == 200, r1.text

    r2 = member.post(
        "/api/mypage/addresses/save",
        json={
            "label": "회사",
            "recipient": "김회사",
            "phone": "010-3333-4444",
            "zipcode": "04524",
            "address": "서울특별시 중구 세종대로 110",
            "address_detail": "5층",
            "is_default": False,
        },
    )
    assert r2.status_code == 200, r2.text

    list_resp = member.get("/api/mypage/addresses")
    assert list_resp.status_code == 200
    addrs = _extract_addresses_list(member.json_or_raise(list_resp))
    assert len(addrs) == 2
    addr_home = next(a for a in addrs if a.get("label") == "집" or a.get("zipcode") == "06236")
    addr_work = next(a for a in addrs if a.get("label") == "회사" or a.get("zipcode") == "04524")

    def_resp = member.post(
        "/api/mypage/addresses/default",
        json={"id": addr_work["id"], "address_id": addr_work["id"]},
    )
    assert def_resp.status_code == 200, def_resp.text

    addrs_after_def = _extract_addresses_list(
        member.json_or_raise(member.get("/api/mypage/addresses"))
    )
    w_after = next(a for a in addrs_after_def if a["id"] == addr_work["id"])
    h_after = next(a for a in addrs_after_def if a["id"] == addr_home["id"])
    assert bool(w_after.get("is_default")) is True
    assert bool(h_after.get("is_default")) is False

    del_resp = member.post(
        "/api/mypage/addresses/delete",
        json={"id": addr_home["id"], "address_id": addr_home["id"]},
    )
    assert del_resp.status_code == 200, del_resp.text

    addrs_final = _extract_addresses_list(
        member.json_or_raise(member.get("/api/mypage/addresses"))
    )
    assert len(addrs_final) == 1
    assert addrs_final[0]["id"] == addr_work["id"]


@pytest.mark.tier1
@pytest.mark.f8
def test_f8_t1_mypage_password_change_and_relogin(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    member = api_client_factory(srv)
    member.register("f8_pw@e2e.local", "OldPass123!")

    chg = member.post(
        "/api/mypage/password",
        json={"current_password": "OldPass123!", "new_password": "NewPass456!"},
    )
    assert chg.status_code == 200, chg.text
    assert member.json_or_raise(chg).get("status") == "ok"

    member.logout()

    fresh = api_client_factory(srv)
    fresh.refresh_csrf("/auth/login")
    old_login = fresh.post(
        "/auth/login",
        json={"email": "f8_pw@e2e.local", "password": "OldPass123!"},
    )
    assert old_login.status_code == 401

    new_login = fresh.login("f8_pw@e2e.local", "NewPass456!")
    assert new_login["status"] == "ok"


@pytest.mark.tier1
@pytest.mark.f8
def test_f8_t1_forgot_and_reset_password_via_notification_log_fallback(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    user_client = api_client_factory(srv)
    user_client.register("reset_user@e2e.local", "InitPass123!")
    user_client.logout()

    forgot_client = api_client_factory(srv)
    forgot_client.refresh_csrf("/auth/forgot-password")
    forgot_resp = forgot_client.post(
        "/auth/forgot-password",
        json={"email": "reset_user@e2e.local"},
    )
    assert forgot_resp.status_code == 200, forgot_resp.text

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    logs_resp = admin.get("/api/admin/notifications/logs")
    assert logs_resp.status_code == 200, logs_resp.text
    logs = _extract_notifications_list(admin.json_or_raise(logs_resp))

    matching_logs = [
        l for l in logs if "reset_user@e2e.local" in json.dumps(l, ensure_ascii=False)
    ]
    assert matching_logs, f"No notification log recorded for reset_user@e2e.local: {logs!r}"
    latest_blob = json.dumps(matching_logs[0], ensure_ascii=False)
    assert "LOGGED" in latest_blob.upper() or matching_logs[0].get("status") in (
        "LOGGED_ONLY",
        "LOGGED",
        "SENT",
    )

    code_match = re.search(r"\b(\d{6})\b", latest_blob)
    assert code_match, f"Expected 6-digit temporary reset code in notification log: {latest_blob}"
    temp_code = code_match.group(1)

    token_match = re.search(r"token=([A-Za-z0-9._-]+)", latest_blob)
    reset_payload: dict[str, Any] = {
        "email": "reset_user@e2e.local",
        "code": temp_code,
        "temp_code": temp_code,
        "new_password": "ResetPass789!",
        "password": "ResetPass789!",
    }
    if token_match:
        reset_payload["token"] = token_match.group(1)

    reset_client = api_client_factory(srv)
    reset_client.refresh_csrf("/auth/reset-password")
    reset_resp = reset_client.post("/auth/reset-password", json=reset_payload)
    assert reset_resp.status_code == 200, reset_resp.text

    verify_client = api_client_factory(srv)
    login_res = verify_client.login("reset_user@e2e.local", "ResetPass789!")
    assert login_res["status"] == "ok"


@pytest.mark.tier1
@pytest.mark.f8
def test_f8_t1_member_withdraw_and_mypage_playwright_e2e(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=True)
    member = api_client_factory(srv)
    member.register("withdraw_me@e2e.local", "WithdrawPass123!", name="탈퇴예정회원")
    ord_data = create_order_via_api(
        member,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_name="탈퇴예정회원",
    )

    ctx = desktop_context_factory()
    page = _login_browser_context(ctx, srv.base_url, "withdraw_me@e2e.local", "WithdrawPass123!")
    diag = attach_page_diagnostics(page)
    resp = page.goto(f"{srv.base_url}/mypage", wait_until="domcontentloaded")
    assert resp is not None and resp.status == 200
    expect(page.locator("body")).to_contain_text("withdraw_me@e2e.local")
    assert_no_horizontal_overflow(page)
    assert_no_main_internal_vertical_scroll(page)

    # Update profile via /mypage UI using semantic locators (no silent skip)
    name_input = (
        page.locator('input[name="name"]')
        .or_(page.get_by_label(re.compile(r"^(?:회원\s*)?(?:성함|이름)")))
        .or_(page.locator('input[id*="ProfileName"], input[id*="mpName"], input[id*="Name"]'))
        .first
    )
    expect(name_input).to_be_visible()
    name_input.fill("탈퇴직전수정")
    profile_form = page.locator("form").filter(has=name_input).first
    btn_scope = profile_form if profile_form.count() > 0 else page
    save_btn = btn_scope.get_by_role(
        "button", name=re.compile(r"정보\s*저장|프로필\s*저장|내\s*정보\s*수정|저장|수정")
    ).first
    expect(save_btn).to_be_visible()
    save_btn.click()
    expect(page.locator("#mallToastContainer .ax-toast").last).to_be_visible()

    page.reload(wait_until="domcontentloaded")
    name_input_after = (
        page.locator('input[name="name"]')
        .or_(page.get_by_label(re.compile(r"^(?:회원\s*)?(?:성함|이름)")))
        .or_(page.locator('input[id*="ProfileName"], input[id*="mpName"], input[id*="Name"]'))
        .first
    )
    expect(name_input_after).to_have_value("탈퇴직전수정")

    diag.assert_clean("f8_mypage_ui")

    # Withdraw account via /api/mypage/withdraw (or UI)
    w_resp = member.post("/api/mypage/withdraw", json={"password": "WithdrawPass123!"})
    assert w_resp.status_code == 200, w_resp.text
    assert member.json_or_raise(w_resp).get("status") == "ok"

    # Session cleared -> /mypage redirects to /auth/login
    mp_after = member.get("/mypage", allow_redirects=False)
    assert mp_after.status_code in (302, 401)
    if mp_after.status_code == 302:
        assert "/auth/login" in (mp_after.headers.get("Location") or "")

    # Re-login blocked
    fresh = api_client_factory(srv)
    fresh.refresh_csrf("/auth/login")
    login_after = fresh.post(
        "/auth/login",
        json={"email": "withdraw_me@e2e.local", "password": "WithdrawPass123!"},
    )
    assert login_after.status_code in (401, 403)

    # Policy retention: order record remains preserved for admin
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    admin_orders = admin.json_or_raise(admin.get("/api/admin/orders"))["orders"]
    assert any(o["order_code"] == ord_data["order_code"] for o in admin_orders)


# =============================================================================
# F9: Admin Member Management Tab
# =============================================================================


@pytest.mark.tier1
@pytest.mark.f9
def test_f9_t1_admin_users_list_returns_members_without_password_hash(
    live_server,
    admin_api,
    member_account,
):
    resp = admin_api.get("/api/admin/users")
    assert resp.status_code == 200, resp.text
    payload = admin_api.json_or_raise(resp, "admin_users")
    assert payload.get("status") == "ok"

    users = _extract_users_list(payload)
    emails = {u.get("email") for u in users}
    assert member_account["email"] in emails
    assert live_server.admin_email in emails

    for u in users:
        assert "password_hash" not in u
        assert "guest_password_hash" not in u
        assert "id" in u and "email" in u and "role" in u and "is_active" in u
        _ = _extract_user_order_count(u)
        _ = _extract_user_total_spent(u)


@pytest.mark.tier1
@pytest.mark.f9
def test_f9_t1_admin_users_order_count_and_cumulative_amount_accuracy(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    member = api_client_factory(srv)
    member.register("stats_user@e2e.local", "StatsPass123!", name="통계집계회원")

    ord1 = create_order_via_api(
        member,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],  # 29,000 + 4,000 = 33,000
    )
    ord2 = create_order_via_api(
        member,
        items=[{"num": 2, "color": "아이보리", "size": "FREE", "quantity": 1}],  # 42,000 + 4,000 = 46,000
    )

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    for o in (ord1, ord2):
        r = admin.post(
            "/api/admin/order-status",
            json={"order_id": o["order_id"], "payment_status": "입금완료", "order_status": "상품준비중"},
        )
        assert r.status_code == 200

    users = _extract_users_list(admin.json_or_raise(admin.get("/api/admin/users")))
    target = next(u for u in users if u["email"] == "stats_user@e2e.local")
    assert _extract_user_order_count(target) == 2
    assert _extract_user_total_spent(target) == 79000


@pytest.mark.tier1
@pytest.mark.f9
def test_f9_t1_admin_suspend_member_blocks_login_and_existing_session(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    member = api_client_factory(srv)
    reg = member.register("target_suspend@e2e.local", "TargetPass123!")
    uid = reg["user"]["id"]

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    susp_resp = admin.post(
        "/api/admin/users/status",
        json={"user_id": uid, "is_active": 0},
    )
    assert susp_resp.status_code == 200, susp_resp.text

    # Existing session immediately blocked on protected member routes
    assert member.get("/mypage", allow_redirects=False).status_code in (302, 401)
    assert member.post("/api/mypage/profile", json={"name": "차단됨"}).status_code == 401
    assert member.post("/api/wishlist/toggle", json={"product_num": 1}).status_code == 401

    # Fresh login blocked
    fresh = api_client_factory(srv)
    fresh.refresh_csrf("/auth/login")
    login_resp = fresh.post(
        "/auth/login",
        json={"email": "target_suspend@e2e.local", "password": "TargetPass123!"},
    )
    assert login_resp.status_code in (401, 403)


@pytest.mark.tier1
@pytest.mark.f9
def test_f9_t1_admin_reactivate_member_restores_login(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    member = api_client_factory(srv)
    reg = member.register("target_reactivate@e2e.local", "TargetPass123!")
    uid = reg["user"]["id"]

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    assert admin.post("/api/admin/users/status", json={"user_id": uid, "is_active": 0}).status_code == 200
    assert admin.post("/api/admin/users/status", json={"user_id": uid, "is_active": 1}).status_code == 200

    fresh = api_client_factory(srv)
    login_res = fresh.login("target_reactivate@e2e.local", "TargetPass123!")
    assert login_res["status"] == "ok"
    assert fresh.get("/mypage").status_code == 200


@pytest.mark.tier1
@pytest.mark.f9
def test_f9_t1_admin_users_tab_ui_playwright_e2e(
    server_factory,
    api_client_factory,
    desktop_context_factory,
):
    srv = server_factory(seed_products=True)
    member = api_client_factory(srv)
    member.register("ui_member@e2e.local", "UiPass123!", name="어드민UI회원")
    ord_data = create_order_via_api(
        member,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
    )
    admin_api = api_client_factory(srv)
    admin_api.login(srv.admin_email, srv.admin_password)
    admin_api.post(
        "/api/admin/order-status",
        json={"order_id": ord_data["order_id"], "payment_status": "입금완료", "order_status": "배송완료"},
    )

    ctx = desktop_context_factory()
    page = _login_browser_context(ctx, srv.base_url, srv.admin_email, srv.admin_password)
    diag = attach_page_diagnostics(page)
    page.on("dialog", lambda d: d.accept())

    page.goto(f"{srv.base_url}/admin", wait_until="domcontentloaded")
    member_tab = page.locator(".ax-admin-tab-bar .ax-tab-btn", has_text="회원").first
    expect(member_tab).to_be_visible()
    member_tab.click()

    row_selector = (
        "tbody tr, .ax-admin-user-card, .ax-admin-user-row, "
        ".ax-admin-order-card, .ax-admin-prod-row, [data-user-id]"
    )
    member_row = page.locator(row_selector, has_text="ui_member@e2e.local").first
    expect(member_row).to_be_visible()
    expect(member_row).to_contain_text("어드민UI회원")
    expect(member_row).to_contain_text("33,000")

    before_users = _extract_users_list(admin_api.json_or_raise(admin_api.get("/api/admin/users")))
    target_before = next(u for u in before_users if u["email"] == "ui_member@e2e.local")
    assert int(target_before["is_active"]) == 1

    suspend_btn = member_row.get_by_role("button", name=re.compile(r"정지|비활성")).first
    expect(suspend_btn).to_be_visible()
    reactivate_btn = member_row.get_by_role("button", name=re.compile(r"(?<!비)활성|해제|복구")).first
    inactive_badge = member_row.locator(".ax-badge, [data-user-status]", has_text=re.compile(r"정지|비활성")).first
    expect(reactivate_btn).to_have_count(0)
    expect(inactive_badge).to_have_count(0)

    suspend_btn.click()
    confirm_modal_btn = page.locator(".ax-dialog-card").get_by_role(
        "button", name=re.compile(r"확인|정지|진행")
    )
    if confirm_modal_btn.count() > 0 and confirm_modal_btn.first.is_visible():
        confirm_modal_btn.first.click()

    expect(reactivate_btn.or_(inactive_badge).first).to_be_visible(timeout=5000)

    after_users = _extract_users_list(admin_api.json_or_raise(admin_api.get("/api/admin/users")))
    target_after = next(u for u in after_users if u["email"] == "ui_member@e2e.local")
    assert int(target_after["is_active"]) == 0

    page.reload(wait_until="domcontentloaded")
    page.locator(".ax-admin-tab-bar .ax-tab-btn", has_text="회원").first.click()
    member_row_after = page.locator(row_selector, has_text="ui_member@e2e.local").first
    expect(member_row_after).to_be_visible()
    reactivate_btn_after = member_row_after.get_by_role(
        "button", name=re.compile(r"(?<!비)활성|해제|복구")
    ).first
    inactive_badge_after = member_row_after.locator(
        ".ax-badge, [data-user-status]", has_text=re.compile(r"정지|비활성")
    ).first
    expect(reactivate_btn_after.or_(inactive_badge_after).first).to_be_visible(timeout=5000)

    reloaded_users = _extract_users_list(admin_api.json_or_raise(admin_api.get("/api/admin/users")))
    target_reloaded = next(u for u in reloaded_users if u["email"] == "ui_member@e2e.local")
    assert int(target_reloaded["is_active"]) == 0

    assert_no_horizontal_overflow(page)
    assert_no_main_internal_vertical_scroll(page)
    diag.assert_clean("f9_admin_users_ui")
