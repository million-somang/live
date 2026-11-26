"""Tier 2 opaque-box boundary, negative, IDOR, auth, and security E2E tests for R2 (F6–F9)."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from playwright.sync_api import BrowserContext, Page, expect

from e2e.helpers import (
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
# F6 Tier 2: Logistics Auth/CSRF, Bad Payloads, XSS, Cancelled Order Guard
# =============================================================================


@pytest.mark.tier2
@pytest.mark.f6
def test_f6_t2_admin_logistics_endpoints_auth_and_csrf_enforced(
    live_server,
    guest_api,
    member_api,
    admin_api,
):
    payload = {
        "order_id": 1,
        "carrier_code": "cj",
        "carrier_name": "CJ대한통운",
        "tracking_number": "123456789012",
    }
    # Guest -> 401
    assert guest_api.post("/api/admin/orders/tracking", json=payload).status_code == 401
    assert guest_api.get("/api/admin/orders/export-shipping.csv").status_code == 401
    assert guest_api.post("/api/admin/orders/import-tracking", json={"rows": []}).status_code == 401

    # Member -> 403
    assert member_api.post("/api/admin/orders/tracking", json=payload).status_code == 403
    assert member_api.get("/api/admin/orders/export-shipping.csv").status_code == 403
    assert member_api.post("/api/admin/orders/import-tracking", json={"rows": []}).status_code == 403

    # Admin without CSRF -> 403
    assert (
        admin_api.post("/api/admin/orders/tracking", json=payload, include_csrf=False).status_code
        == 403
    )
    assert (
        admin_api.post(
            "/api/admin/orders/import-tracking",
            json={"rows": []},
            include_csrf=False,
        ).status_code
        == 403
    )


@pytest.mark.tier2
@pytest.mark.f6
def test_f6_t2_tracking_invalid_order_and_empty_tracking_rejected(
    live_server,
    member_api,
    admin_api,
):
    missing_resp = admin_api.post(
        "/api/admin/orders/tracking",
        json={
            "order_id": 999999,
            "order_code": "ORD-NONEXISTENT-999",
            "carrier_code": "cj",
            "carrier_name": "CJ대한통운",
            "tracking_number": "123456789012",
        },
    )
    assert missing_resp.status_code in (400, 404)

    order = create_order_via_api(
        member_api,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_phone="010-6500-1111",
    )
    empty_resp = admin_api.post(
        "/api/admin/orders/tracking",
        json={
            "order_id": order["order_id"],
            "carrier_code": "cj",
            "carrier_name": "CJ대한통운",
            "tracking_number": "   ",
            "mark_shipped": True,
        },
    )
    assert empty_resp.status_code == 400

    orders = member_api.json_or_raise(member_api.get("/api/orders/lookup"))["orders"]
    found = next(o for o in orders if o["order_code"] == order["order_code"])
    assert found["order_status"] == "결제대기"
    assert not (found.get("tracking_number") or "").strip()


@pytest.mark.tier2
@pytest.mark.f6
def test_f6_t2_import_tracking_malformed_and_unknown_rows(
    live_server,
    admin_api,
    api_client_factory,
):
    # Empty multipart file -> 400 (never 500)
    empty_file_resp = admin_api.post(
        "/api/admin/orders/import-tracking",
        files={"file": ("empty.csv", b"", "text/csv")},
    )
    assert empty_file_resp.status_code in (400, 422)

    # Unknown order_code only -> never 500
    unknown_resp = admin_api.post(
        "/api/admin/orders/import-tracking",
        json={
            "rows": [
                {
                    "order_code": "ORD-UNKNOWN-0000",
                    "carrier_code": "cj",
                    "carrier_name": "CJ대한통운",
                    "tracking_number": "999900001111",
                }
            ]
        },
    )
    assert unknown_resp.status_code < 500
    if unknown_resp.status_code == 200:
        body = admin_api.json_or_raise(unknown_resp)
        updated = body.get("updated_count", body.get("updated", 0))
        assert int(updated) == 0


@pytest.mark.tier2
@pytest.mark.f6
def test_f6_t2_tracking_number_xss_and_boundary_sanitization(
    live_server,
    member_api,
    admin_api,
    desktop_context_factory,
    member_storage_state,
    admin_storage_state,
):
    order = create_order_via_api(
        member_api,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
    )
    resp = admin_api.post(
        "/api/admin/orders/tracking",
        json={
            "order_id": order["order_id"],
            "carrier_code": "cj",
            "carrier_name": "<script>window.__f6_xss=1;</script>CJ대한통운",
            "tracking_number": "  9876-5432-10  ",
            "mark_shipped": True,
        },
    )
    assert resp.status_code == 200, resp.text

    lookup = member_api.json_or_raise(member_api.get("/api/orders/lookup"))["orders"]
    target = next(o for o in lookup if o["order_code"] == order["order_code"])
    assert target["tracking_number"] == "9876-5432-10"

    for role_label, storage in [("member", member_storage_state), ("admin", admin_storage_state)]:
        ctx = desktop_context_factory(storage_state=storage)
        page = ctx.new_page()
        dialogs: list[str] = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        diag = attach_page_diagnostics(page)
        url = f"{live_server.base_url}/orders" if role_label == "member" else f"{live_server.base_url}/admin"
        page.goto(url, wait_until="domcontentloaded")
        if role_label == "admin":
            page.locator("#adminTabOrders").click()
        assert page.evaluate("() => window.__f6_xss") is None
        assert dialogs == []
        diag.assert_clean(f"f6_xss_{role_label}")


@pytest.mark.tier2
@pytest.mark.f6
def test_f6_t2_tracking_without_mark_shipped_preserves_status_and_blocks_cancelled(
    live_server,
    member_api,
    admin_api,
):
    ord_prep = create_order_via_api(
        member_api,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_phone="010-6600-1111",
    )
    admin_api.post(
        "/api/admin/order-status",
        json={"order_id": ord_prep["order_id"], "payment_status": "입금완료", "order_status": "상품준비중"},
    )
    tr_resp = admin_api.post(
        "/api/admin/orders/tracking",
        json={
            "order_id": ord_prep["order_id"],
            "carrier_code": "cj",
            "carrier_name": "CJ대한통운",
            "tracking_number": "555566667777",
            "mark_shipped": False,
        },
    )
    assert tr_resp.status_code == 200
    orders = member_api.json_or_raise(member_api.get("/api/orders/lookup"))["orders"]
    prep_after = next(o for o in orders if o["order_code"] == ord_prep["order_code"])
    assert prep_after["tracking_number"] == "555566667777"
    assert prep_after["order_status"] == "상품준비중"

    # Cancelled order must reject shipping/tracking transition to 배송중
    ord_cancelled = create_order_via_api(
        member_api,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_phone="010-6600-2222",
    )
    admin_api.post(
        "/api/admin/order-status",
        json={"order_id": ord_cancelled["order_id"], "payment_status": "입금대기", "order_status": "주문취소"},
    )
    bad_tr = admin_api.post(
        "/api/admin/orders/tracking",
        json={
            "order_id": ord_cancelled["order_id"],
            "carrier_code": "cj",
            "carrier_name": "CJ대한통운",
            "tracking_number": "888899990000",
            "mark_shipped": True,
        },
    )
    assert bad_tr.status_code == 400


# =============================================================================
# F7 Tier 2: IDOR, Guest 3-Factor Failures, State Guards, Validation, XSS/CSRF
# =============================================================================


@pytest.mark.tier2
@pytest.mark.f7
def test_f7_t2_idor_cross_member_self_cancel_and_claim_blocked(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    member_a = api_client_factory(srv)
    member_a.register("member_a@e2e.local", "PassA1234!")
    member_b = api_client_factory(srv)
    member_b.register("member_b@e2e.local", "PassB1234!")

    before_stock = next(
        int(o["stock"])
        for o in member_a.json_or_raise(member_a.get("/api/products/1"))["product"]["options"]
        if o["color"] == "블랙" and o["size"] == "M"
    )
    ord_a = create_order_via_api(
        member_a,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
    )

    c_resp = member_b.post(
        "/api/orders/self-cancel",
        json={"order_id": ord_a["order_id"], "order_code": ord_a["order_code"]},
    )
    assert c_resp.status_code in (403, 404)

    cl_resp = member_b.post(
        "/api/orders/self-claim",
        json={
            "order_id": ord_a["order_id"],
            "order_code": ord_a["order_code"],
            "claim_type": "환불요청",
            "claim_reason": "타인 주문 침해 테스트",
        },
    )
    assert cl_resp.status_code in (403, 404)

    ord_a_after = member_a.json_or_raise(member_a.get("/api/orders/lookup"))["orders"][0]
    assert ord_a_after["order_status"] == "결제대기"
    after_stock = next(
        int(o["stock"])
        for o in member_a.json_or_raise(member_a.get("/api/products/1"))["product"]["options"]
        if o["color"] == "블랙" and o["size"] == "M"
    )
    assert after_stock == before_stock - 1


@pytest.mark.tier2
@pytest.mark.f7
def test_f7_t2_guest_three_factor_wrong_or_missing_factors_blocked(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    creator = api_client_factory(srv)
    ord_data = create_order_via_api(
        creator,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_phone="010-7200-3344",
        guest_password="rightpw72",
    )

    bad_cases = [
        {"order_code": ord_data["order_code"], "buyer_phone": "010-7200-3344", "guest_password": "wrongpw"},
        {"order_code": ord_data["order_code"], "buyer_phone": "010-0000-0000", "guest_password": "rightpw72"},
        {"order_code": ord_data["order_code"], "buyer_phone": "010-7200-3344"},
    ]
    for case in bad_cases:
        fg = api_client_factory(srv)
        fg.refresh_csrf("/orders")
        r_cancel = fg.post("/api/orders/self-cancel", json=case)
        assert r_cancel.status_code in (400, 401, 403, 404)
        assert "guest_password_hash" not in r_cancel.text

        r_claim = fg.post(
            "/api/orders/self-claim",
            json={**case, "claim_type": "환불요청", "claim_reason": "무단 요청"},
        )
        assert r_claim.status_code in (400, 401, 403, 404)
        assert "guest_password_hash" not in r_claim.text


@pytest.mark.tier2
@pytest.mark.f7
def test_f7_t2_self_cancel_rejected_when_paid_or_shipped_or_already_cancelled(
    live_server,
    member_api,
    admin_api,
):
    # (a) Paid order (입금완료 + 상품준비중) -> cannot self-cancel
    ord_paid = create_order_via_api(
        member_api,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
    )
    admin_api.post(
        "/api/admin/order-status",
        json={"order_id": ord_paid["order_id"], "payment_status": "입금완료", "order_status": "상품준비중"},
    )
    assert (
        member_api.post(
            "/api/orders/self-cancel",
            json={"order_code": ord_paid["order_code"]},
        ).status_code
        == 400
    )

    # (b) Shipped order (배송중) -> cannot self-cancel
    admin_api.post(
        "/api/admin/order-status",
        json={"order_id": ord_paid["order_id"], "payment_status": "입금완료", "order_status": "배송중"},
    )
    assert (
        member_api.post(
            "/api/orders/self-cancel",
            json={"order_code": ord_paid["order_code"]},
        ).status_code
        == 400
    )

    # (c) Double self-cancel on already cancelled order -> 400 and stock not restored twice
    before_stock = next(
        int(o["stock"])
        for o in member_api.json_or_raise(member_api.get("/api/products/1"))["product"]["options"]
        if o["color"] == "블랙" and o["size"] == "M"
    )
    ord_dup = create_order_via_api(
        member_api,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 2}],
    )
    first_c = member_api.post(
        "/api/orders/self-cancel",
        json={"order_code": ord_dup["order_code"]},
    )
    assert first_c.status_code == 200
    second_c = member_api.post(
        "/api/orders/self-cancel",
        json={"order_code": ord_dup["order_code"]},
    )
    assert second_c.status_code == 400

    after_stock = next(
        int(o["stock"])
        for o in member_api.json_or_raise(member_api.get("/api/products/1"))["product"]["options"]
        if o["color"] == "블랙" and o["size"] == "M"
    )
    assert after_stock == before_stock


@pytest.mark.tier2
@pytest.mark.f7
def test_f7_t2_self_claim_validation_empty_reason_invalid_type_and_cancelled_order(
    live_server,
    member_api,
    admin_api,
):
    ord_paid = create_order_via_api(
        member_api,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
    )
    admin_api.post(
        "/api/admin/order-status",
        json={"order_id": ord_paid["order_id"], "payment_status": "입금완료", "order_status": "배송완료"},
    )

    assert (
        member_api.post(
            "/api/orders/self-claim",
            json={"order_code": ord_paid["order_code"], "claim_type": "환불요청", "claim_reason": "   "},
        ).status_code
        == 400
    )

    for bad_type in ("환불완료", "주문취소", "INVALID"):
        assert (
            member_api.post(
                "/api/orders/self-claim",
                json={"order_code": ord_paid["order_code"], "claim_type": bad_type, "claim_reason": "사유"},
            ).status_code
            == 400
        )

    ord_cancelled = create_order_via_api(
        member_api,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
    )
    member_api.post("/api/orders/self-cancel", json={"order_code": ord_cancelled["order_code"]})
    assert (
        member_api.post(
            "/api/orders/self-claim",
            json={
                "order_code": ord_cancelled["order_code"],
                "claim_type": "환불요청",
                "claim_reason": "이미 취소된 주문 클레임 시도",
            },
        ).status_code
        == 400
    )


@pytest.mark.tier2
@pytest.mark.f7
def test_f7_t2_member_csrf_enforced_and_claim_reason_xss_escaped(
    live_server,
    member_api,
    admin_api,
    desktop_context_factory,
    member_storage_state,
    admin_storage_state,
):
    order = create_order_via_api(
        member_api,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
    )
    assert (
        member_api.post(
            "/api/orders/self-cancel",
            json={"order_code": order["order_code"]},
            include_csrf=False,
        ).status_code
        == 403
    )

    ord_xss = create_order_via_api(
        member_api,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
    )
    admin_api.post(
        "/api/admin/order-status",
        json={"order_id": ord_xss["order_id"], "payment_status": "입금완료", "order_status": "배송완료"},
    )
    assert (
        member_api.post(
            "/api/orders/self-claim",
            json={
                "order_code": ord_xss["order_code"],
                "claim_type": "환불요청",
                "claim_reason": "<script>window.__f7_xss=1;</script>불량",
            },
            include_csrf=False,
        ).status_code
        == 403
    )

    ok_claim = member_api.post(
        "/api/orders/self-claim",
        json={
            "order_code": ord_xss["order_code"],
            "claim_type": "환불요청",
            "claim_reason": "<script>window.__f7_xss=1;</script>불량",
        },
    )
    assert ok_claim.status_code == 200

    for label, storage in [("member", member_storage_state), ("admin", admin_storage_state)]:
        ctx = desktop_context_factory(storage_state=storage)
        page = ctx.new_page()
        dialogs: list[str] = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        diag = attach_page_diagnostics(page)
        url = f"{live_server.base_url}/orders" if label == "member" else f"{live_server.base_url}/admin"
        page.goto(url, wait_until="domcontentloaded")
        if label == "admin":
            page.locator("#adminTabClaims").click()
        assert page.evaluate("() => window.__f7_xss") is None
        assert dialogs == []
        diag.assert_clean(f"f7_xss_{label}")


# =============================================================================
# F8 Tier 2: Auth/CSRF, Address IDOR, Password Policy, Reset Replay, Stale Session
# =============================================================================


@pytest.mark.tier2
@pytest.mark.f8
def test_f8_t2_mypage_unauthenticated_and_csrf_protection(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    guest = api_client_factory(srv)

    mp_html = guest.get("/mypage", allow_redirects=False)
    assert mp_html.status_code == 302
    assert "/auth/login" in (mp_html.headers.get("Location") or "")

    assert guest.get("/api/mypage/addresses").status_code == 401
    assert guest.post("/api/mypage/profile", json={"name": "게스트"}).status_code == 401
    assert guest.post("/api/mypage/password", json={"current_password": "a", "new_password": "b"}).status_code == 401
    assert guest.post("/api/mypage/withdraw", json={"password": "a"}).status_code == 401
    assert guest.post("/api/mypage/addresses/save", json={"address": "서울"}).status_code == 401

    member = api_client_factory(srv)
    member.register("f8_csrf@e2e.local", "MemberPass123!")
    assert member.post("/api/mypage/profile", json={"name": "노CSRF"}, include_csrf=False).status_code == 403
    assert member.post("/api/mypage/password", json={"current_password": "MemberPass123!", "new_password": "NewPass123!"}, include_csrf=False).status_code == 403
    assert member.post("/api/mypage/addresses/save", json={"label": "집", "recipient": "홍", "phone": "010-1-2", "address": "서울"}, include_csrf=False).status_code == 403
    assert member.post("/api/mypage/withdraw", json={"password": "MemberPass123!"}, include_csrf=False).status_code == 403


@pytest.mark.tier2
@pytest.mark.f8
def test_f8_t2_address_book_idor_and_validation(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    member_a = api_client_factory(srv)
    member_a.register("addr_a@e2e.local", "AddrPass123!", address="", zipcode="", address_detail="")
    member_b = api_client_factory(srv)
    member_b.register("addr_b@e2e.local", "AddrPass123!", address="", zipcode="", address_detail="")

    save_a = member_a.post(
        "/api/mypage/addresses/save",
        json={
            "label": "A의집",
            "recipient": "회원A",
            "phone": "010-1111-2222",
            "zipcode": "06236",
            "address": "서울특별시 강남구 테헤란로 152",
            "address_detail": "101호",
        },
    )
    assert save_a.status_code == 200
    addr_a_id = _extract_addresses_list(
        member_a.json_or_raise(member_a.get("/api/mypage/addresses"))
    )[0]["id"]

    # Member B cannot delete or default Member A's address
    assert (
        member_b.post(
            "/api/mypage/addresses/delete",
            json={"id": addr_a_id, "address_id": addr_a_id},
        ).status_code
        in (403, 404)
    )
    assert (
        member_b.post(
            "/api/mypage/addresses/default",
            json={"id": addr_a_id, "address_id": addr_a_id},
        ).status_code
        in (403, 404)
    )
    addrs_a = _extract_addresses_list(member_a.json_or_raise(member_a.get("/api/mypage/addresses")))
    assert len(addrs_a) == 1

    # Validation: empty required address / recipient -> 400
    bad_save = member_a.post(
        "/api/mypage/addresses/save",
        json={"label": "빈주소", "recipient": "", "phone": "", "address": ""},
    )
    assert bad_save.status_code == 400


@pytest.mark.tier2
@pytest.mark.f8
def test_f8_t2_password_change_wrong_current_and_policy_boundaries(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    member = api_client_factory(srv)
    member.register("pw_policy@e2e.local", "ValidPass123!")

    wrong_cur = member.post(
        "/api/mypage/password",
        json={"current_password": "WrongCur123!", "new_password": "NewValid123!"},
    )
    assert wrong_cur.status_code in (400, 401)

    for weak_pw in ("Ab12345", "12345678", "abcdefgh", "a1" * 65):
        r = member.post(
            "/api/mypage/password",
            json={"current_password": "ValidPass123!", "new_password": weak_pw},
        )
        assert r.status_code == 400, f"Expected 400 for weak password {weak_pw!r}, got {r.status_code}"


@pytest.mark.tier2
@pytest.mark.f8
def test_f8_t2_password_reset_invalid_code_tampered_token_and_replay_blocked(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    u = api_client_factory(srv)
    u.register("reset_sec@e2e.local", "InitPass123!")
    u.logout()

    # Non-existent email should not crash (no 500)
    g = api_client_factory(srv)
    g.refresh_csrf("/auth/forgot-password")
    nonexistent_resp = g.post("/auth/forgot-password", json={"email": "nobody@e2e.local"})
    assert nonexistent_resp.status_code < 500

    req_resp = g.post("/auth/forgot-password", json={"email": "reset_sec@e2e.local"})
    assert req_resp.status_code == 200

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    logs = _extract_notifications_list(admin.json_or_raise(admin.get("/api/admin/notifications/logs")))
    blob = json.dumps(
        next(l for l in logs if "reset_sec@e2e.local" in json.dumps(l, ensure_ascii=False)),
        ensure_ascii=False,
    )
    code_match = re.search(r"\b(\d{6})\b", blob)
    assert code_match
    real_code = code_match.group(1)
    wrong_code = "000000" if real_code != "000000" else "999999"

    token_match = re.search(r"token=([A-Za-z0-9._-]+)", blob)
    real_token = token_match.group(1) if token_match else ""

    # Wrong code rejected
    bad_code_resp = g.post(
        "/auth/reset-password",
        json={
            "email": "reset_sec@e2e.local",
            "code": wrong_code,
            "temp_code": wrong_code,
            "token": real_token,
            "new_password": "ResetNew123!",
            "password": "ResetNew123!",
        },
    )
    assert bad_code_resp.status_code in (400, 403)

    # Wrong email / tampered token rejected
    bad_email_resp = g.post(
        "/auth/reset-password",
        json={
            "email": "other_user@e2e.local",
            "code": real_code,
            "temp_code": real_code,
            "token": "tampered.token.value",
            "new_password": "ResetNew123!",
            "password": "ResetNew123!",
        },
    )
    assert bad_email_resp.status_code in (400, 403)

    # Valid reset succeeds once
    valid_payload: dict[str, Any] = {
        "email": "reset_sec@e2e.local",
        "code": real_code,
        "temp_code": real_code,
        "new_password": "ResetNew123!",
        "password": "ResetNew123!",
    }
    if real_token:
        valid_payload["token"] = real_token
    ok_resp = g.post("/auth/reset-password", json=valid_payload)
    assert ok_resp.status_code == 200, ok_resp.text

    # Replay of same code/token rejected
    replay_resp = g.post(
        "/auth/reset-password",
        json={**valid_payload, "new_password": "ResetReplay999!", "password": "ResetReplay999!"},
    )
    assert replay_resp.status_code in (400, 403)


@pytest.mark.tier2
@pytest.mark.f8
def test_f8_t2_withdraw_wrong_password_rejected_and_stale_session_blocked(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    member = api_client_factory(srv)
    member.register("stale_withdraw@e2e.local", "WithdrawPass123!")
    cloned = member.clone_with_cookies(refresh_csrf=False)

    bad_w = member.post("/api/mypage/withdraw", json={"password": "WrongPassword99!"})
    assert bad_w.status_code in (400, 401, 403)
    assert member.get("/mypage").status_code == 200

    ok_w = member.post("/api/mypage/withdraw", json={"password": "WithdrawPass123!"})
    assert ok_w.status_code == 200

    # Cloned stale session must be rejected
    assert cloned.get("/mypage", allow_redirects=False).status_code in (302, 401)
    assert cloned.post("/api/mypage/profile", json={"name": "유령"}).status_code == 401
    assert cloned.post("/api/wishlist/toggle", json={"product_num": 1}).status_code == 401


# =============================================================================
# F9 Tier 2: Admin Users Auth/CSRF, Self-Suspend Guard, Validation, Stats Hygiene
# =============================================================================


@pytest.mark.tier2
@pytest.mark.f9
def test_f9_t2_admin_users_endpoints_auth_and_csrf_enforced(
    live_server,
    guest_api,
    member_api,
    admin_api,
):
    assert guest_api.get("/api/admin/users").status_code == 401
    assert guest_api.post("/api/admin/users/status", json={"user_id": 1, "is_active": 0}).status_code == 401

    assert member_api.get("/api/admin/users").status_code == 403
    assert member_api.post("/api/admin/users/status", json={"user_id": 1, "is_active": 0}).status_code == 403

    assert (
        admin_api.post(
            "/api/admin/users/status",
            json={"user_id": 1, "is_active": 0},
            include_csrf=False,
        ).status_code
        == 403
    )


@pytest.mark.tier2
@pytest.mark.f9
def test_f9_t2_admin_cannot_suspend_self_or_primary_admin(
    live_server,
    admin_api,
):
    users = _extract_users_list(admin_api.json_or_raise(admin_api.get("/api/admin/users")))
    admin_user = next(u for u in users if u["email"] == live_server.admin_email)

    self_susp = admin_api.post(
        "/api/admin/users/status",
        json={"user_id": admin_user["id"], "is_active": 0},
    )
    assert self_susp.status_code in (400, 403)

    check_after = admin_api.get("/api/admin/users")
    assert check_after.status_code == 200
    admin_after = next(
        u for u in _extract_users_list(admin_api.json_or_raise(check_after)) if u["id"] == admin_user["id"]
    )
    assert bool(admin_after["is_active"]) is True


@pytest.mark.tier2
@pytest.mark.f9
def test_f9_t2_admin_users_status_invalid_id_and_invalid_payload(
    live_server,
    admin_api,
):
    assert (
        admin_api.post(
            "/api/admin/users/status",
            json={"user_id": 999999, "is_active": 0},
        ).status_code
        in (400, 404)
    )
    for bad_body in (
        {"user_id": "not_an_int", "is_active": 0},
        {"is_active": 0},
        {"user_id": 1, "is_active": "invalid_flag"},
    ):
        resp = admin_api.post("/api/admin/users/status", json=bad_body)
        assert resp.status_code == 400, f"Expected 400 for {bad_body!r}, got {resp.status_code}"


@pytest.mark.tier2
@pytest.mark.f9
def test_f9_t2_admin_users_no_secret_leak_and_safe_search_filter(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    m = api_client_factory(srv)
    reg = m.register("safe_filter@e2e.local", "FilterPass123!", name="검색테스트회원")

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)

    sqli_resp = admin.get("/api/admin/users", params={"q": "%';DROP TABLE mall_users;--"})
    assert sqli_resp.status_code == 200
    sqli_data = admin.json_or_raise(sqli_resp)
    assert "password_hash" not in json.dumps(sqli_data)

    status_resp = admin.post(
        "/api/admin/users/status",
        json={"user_id": reg["user"]["id"], "is_active": 0},
    )
    assert status_resp.status_code == 200
    assert "password_hash" not in status_resp.text


@pytest.mark.tier2
@pytest.mark.f9
def test_f9_t2_guest_and_cancelled_orders_do_not_inflate_member_stats(
    server_factory,
    api_client_factory,
):
    srv = server_factory(seed_products=True)
    member = api_client_factory(srv)
    member.register(
        "clean_stats@e2e.local",
        "StatsPass123!",
        name="통계회원",
        phone="010-4444-5555",
    )

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)

    # Two PAID guest orders sharing identical buyer_name and buyer_phone must NOT count toward member stats
    for idx in range(2):
        guest = api_client_factory(srv)
        g_ord = create_order_via_api(
            guest,
            items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 2}],
            buyer_name="통계회원",
            buyer_phone="010-4444-5555",
        )
        paid_resp = admin.post(
            "/api/admin/order-status",
            json={
                "order_id": g_ord["order_id"],
                "payment_status": "입금완료",
                "order_status": "배송완료" if idx == 0 else "상품준비중",
            },
        )
        assert paid_resp.status_code == 200, paid_resp.text

    # Member order that is immediately cancelled must NOT count toward cumulative spend
    m_ord = create_order_via_api(
        member,
        items=[{"num": 2, "color": "아이보리", "size": "FREE", "quantity": 1}],
        buyer_name="통계회원",
        buyer_phone="010-4444-5555",
    )
    assert (
        member.post("/api/orders/self-cancel", json={"order_code": m_ord["order_code"]}).status_code
        == 200
    )

    users = _extract_users_list(admin.json_or_raise(admin.get("/api/admin/users")))
    target = next(u for u in users if u["email"] == "clean_stats@e2e.local")
    assert _extract_user_order_count(target) <= 1
    assert _extract_user_total_spent(target) == 0
