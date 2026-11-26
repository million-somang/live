"""Tier 2 opaque-box boundary, negative, auth, and security E2E tests for R3 (F10–F13)."""

from __future__ import annotations

import pytest

from e2e.helpers import (
    attach_page_diagnostics,
    create_order_via_api,
    seed_product_via_sync,
)


# ---------------------------------------------------------------------------
# Feature F10 Boundaries & Security
# ---------------------------------------------------------------------------


@pytest.mark.tier2
@pytest.mark.f10
def test_t2_f10_01_stranger_and_other_member_blocked_from_order_complete(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    member_a = api_client_factory(srv)
    member_a.register("f10_a@e2e.local", "PassA123!", name="회원A")
    order_a = create_order_via_api(
        member_a,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_name="수령인A비밀",
        buyer_phone="010-1000-2000",
        address="서울시 비밀구 비밀로 111",
    )

    guest_g = api_client_factory(srv)
    order_g = create_order_via_api(
        guest_g,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_name="비회원G비밀",
        buyer_phone="010-2000-3000",
        address="부산시 비밀구 비밀로 222",
        guest_password="guestpw99",
    )

    member_b = api_client_factory(srv)
    member_b.register("f10_b@e2e.local", "PassB123!", name="회원B")
    fresh_guest = api_client_factory(srv)

    for client in (member_b, fresh_guest):
        for target_code, secret_name, secret_addr in (
            (order_a["order_code"], "수령인A비밀", "비밀로 111"),
            (order_g["order_code"], "비회원G비밀", "비밀로 222"),
        ):
            resp = client.get(f"/order-complete/{target_code}", allow_redirects=False)
            assert resp.status_code in (302, 401, 403, 404)
            assert secret_name not in resp.text
            assert secret_addr not in resp.text


@pytest.mark.tier2
@pytest.mark.f10
def test_t2_f10_02_cash_receipt_income_or_expense_requires_number(
    live_server,
    member_api,
):
    invalid_cases = [
        ("INCOME", ""),
        ("EXPENSE", ""),
        ("UNKNOWN", "01012345678"),
    ]
    for r_type, r_no in invalid_cases:
        resp = member_api.post(
            "/api/orders/create",
            json={
                "items": [{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
                "buyer_name": "영수증검증",
                "buyer_phone": "010-3333-4444",
                "zipcode": "06236",
                "address": "서울시 강남구 테헤란로 152",
                "source_page": "CART_CHECKOUT",
                "payment_method": "BANK",
                "cash_receipt_type": r_type,
                "cash_receipt_no": r_no,
            },
        )
        assert resp.status_code == 400, f"Expected 400 for {(r_type, r_no)}, got {resp.status_code}"
        data = member_api.json_or_raise(resp)
        assert data.get("status") == "error"


@pytest.mark.tier2
@pytest.mark.f10
def test_t2_f10_03_unknown_payment_method_and_pg_prepare_rejected(
    live_server,
    member_api,
):
    create_resp = member_api.post(
        "/api/orders/create",
        json={
            "items": [{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
            "buyer_name": "결제수단검증",
            "buyer_phone": "010-3333-5555",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "source_page": "CART_CHECKOUT",
            "payment_method": "INVALID_PG",
        },
    )
    assert create_resp.status_code == 400
    assert member_api.json_or_raise(create_resp).get("status") == "error"

    order = create_order_via_api(
        member_api,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        payment_method="BANK",
    )
    prep_resp = member_api.post(
        "/api/payments/prepare",
        json={"order_code": order["order_code"], "payment_method": "INVALID_PG"},
    )
    assert prep_resp.status_code == 400
    assert member_api.json_or_raise(prep_resp).get("status") == "error"


@pytest.mark.tier2
@pytest.mark.f10
def test_t2_f10_04_pg_confirm_amount_mismatch_or_other_user_blocked(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    member_a = api_client_factory(srv)
    member_a.register("pg_a@e2e.local", "PassA123!")
    member_b = api_client_factory(srv)
    member_b.register("pg_b@e2e.local", "PassB123!")

    order = create_order_via_api(
        member_a,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        payment_method="PORTONE_V2",
    )
    assert order["total_amount"] > 1

    other_resp = member_b.post(
        "/api/payments/confirm",
        json={
            "order_code": order["order_code"],
            "payment_method": "PORTONE_V2",
            "payment_key": "forged_key",
            "amount": order["total_amount"],
        },
    )
    assert other_resp.status_code in (403, 404)

    mismatch_resp = member_a.post(
        "/api/payments/confirm",
        json={
            "order_code": order["order_code"],
            "payment_method": "PORTONE_V2",
            "payment_key": "valid_key",
            "amount": 1,
        },
    )
    assert mismatch_resp.status_code == 400

    orders = member_a.json_or_raise(member_a.get("/api/orders/lookup"))["orders"]
    matched = [o for o in orders if o["order_code"] == order["order_code"]]
    assert matched[0]["payment_status"] == "입금대기"


@pytest.mark.tier2
@pytest.mark.f10
def test_t2_f10_05_pg_webhook_forged_signature_and_amount_mismatch_blocked(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    member = api_client_factory(srv)
    member.register("webhook_u@e2e.local", "PassW123!")

    ord_portone = create_order_via_api(
        member,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        payment_method="PORTONE_V2",
    )
    ord_toss = create_order_via_api(
        member,
        items=[{"num": 2, "color": "아이보리", "size": "FREE", "quantity": 1}],
        payment_method="TOSS_PAYMENTS",
    )

    attacker = api_client_factory(srv)
    r_p1 = attacker.post(
        "/api/payments/webhook/portone_v2",
        json={"order_code": ord_portone["order_code"], "amount": ord_portone["total_amount"], "status": "PAID"},
        headers={"X-PortOne-Signature": "forged-bad-sig"},
        include_csrf=False,
    )
    assert r_p1.status_code in (400, 401, 403)

    r_p2 = attacker.post(
        "/api/payments/webhook/portone_v2",
        json={"order_code": ord_portone["order_code"], "amount": 1, "status": "PAID"},
        include_csrf=False,
    )
    assert r_p2.status_code in (400, 401, 403)

    r_t1 = attacker.post(
        "/api/payments/webhook/toss_payments",
        json={"order_code": ord_toss["order_code"], "amount": ord_toss["total_amount"], "status": "DONE"},
        headers={"TossPayments-Signature": "forged-bad-sig"},
        include_csrf=False,
    )
    assert r_t1.status_code in (400, 401, 403)

    r_t2 = attacker.post(
        "/api/payments/webhook/toss_payments",
        json={"order_code": ord_toss["order_code"], "amount": 1, "status": "DONE"},
        include_csrf=False,
    )
    assert r_t2.status_code in (400, 401, 403)

    orders = member.json_or_raise(member.get("/api/orders/lookup"))["orders"]
    by_code = {o["order_code"]: o for o in orders}
    assert by_code[ord_portone["order_code"]]["payment_status"] == "입금대기"
    assert by_code[ord_toss["order_code"]]["payment_status"] == "입금대기"


# ---------------------------------------------------------------------------
# Feature F11 Boundaries & Security
# ---------------------------------------------------------------------------


@pytest.mark.tier2
@pytest.mark.f11
def test_t2_f11_01_secret_qna_masked_from_guest_and_other_member(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    member_a = api_client_factory(srv)
    member_a.register("qna_a@e2e.local", "PassA123!")
    member_b = api_client_factory(srv)
    member_b.register("qna_b@e2e.local", "PassB123!")
    guest = api_client_factory(srv)

    c_resp = member_a.post(
        "/api/products/1/qna",
        json={"title": "비밀문의제목", "content": "SECRET_Q_TEXT_991", "is_secret": True},
    )
    assert c_resp.status_code == 200
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    q_list = admin.json_or_raise(admin.get("/api/products/1/qna"))
    items = q_list.get("qnas") or q_list.get("items") or q_list.get("qna") or []
    qid = items[0]["id"]
    assert (
        admin.post(
            "/api/admin/qna/answer",
            json={"qna_id": qid, "answer": "SECRET_A_TEXT_992"},
        ).status_code
        == 200
    )

    for client in (guest, member_b):
        api_resp = client.get("/api/products/1/qna")
        assert api_resp.status_code == 200
        assert "SECRET_Q_TEXT_991" not in api_resp.text
        assert "SECRET_A_TEXT_992" not in api_resp.text
        assert "비밀글" in api_resp.text

        html_resp = client.get("/product/1")
        assert html_resp.status_code == 200
        assert "SECRET_Q_TEXT_991" not in html_resp.text
        assert "SECRET_A_TEXT_992" not in html_resp.text


@pytest.mark.tier2
@pytest.mark.f11
def test_t2_f11_02_unauthenticated_qna_and_inquiry_blocked(
    live_server,
    guest_api,
):
    assert (
        guest_api.post(
            "/api/products/1/qna",
            json={"title": "질문", "content": "비회원 차단 테스트", "is_secret": False},
        ).status_code
        == 401
    )
    assert (
        guest_api.post(
            "/api/inquiries/create",
            json={"category": "배송", "title": "문의", "content": "비회원 문의 차단"},
        ).status_code
        == 401
    )
    assert guest_api.get("/api/inquiries").status_code == 401


@pytest.mark.tier2
@pytest.mark.f11
def test_t2_f11_03_member_cannot_view_other_members_inquiry(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    member_a = api_client_factory(srv)
    member_a.register("inq_a@e2e.local", "PassA123!")
    member_b = api_client_factory(srv)
    member_b.register("inq_b@e2e.local", "PassB123!")

    r = member_a.post(
        "/api/inquiries/create",
        json={"category": "결제", "title": "A_PRIVATE_INQUIRY_777", "content": "A 전용 비밀 문의 내용"},
    )
    assert r.status_code == 200

    b_api = member_b.get("/api/inquiries")
    assert b_api.status_code == 200
    assert "A_PRIVATE_INQUIRY_777" not in b_api.text
    b_data = member_b.json_or_raise(b_api)
    inquiries = b_data.get("inquiries") or b_data.get("items") or []
    assert len(inquiries) == 0

    b_cs = member_b.get("/cs")
    assert b_cs.status_code == 200
    assert "A_PRIVATE_INQUIRY_777" not in b_cs.text


@pytest.mark.tier2
@pytest.mark.f11
def test_t2_f11_04_empty_fields_and_missing_product_qna_rejected(
    live_server,
    member_api,
    admin_api,
):
    assert (
        member_api.post(
            "/api/products/1/qna",
            json={"title": "", "content": "", "is_secret": False},
        ).status_code
        == 400
    )
    assert (
        member_api.post(
            "/api/products/999999/qna",
            json={"title": "없는 상품", "content": "존재하지 않는 상품 문의", "is_secret": False},
        ).status_code
        == 404
    )
    assert (
        member_api.post(
            "/api/inquiries/create",
            json={"category": "배송", "title": "", "content": ""},
        ).status_code
        == 400
    )
    assert (
        admin_api.post(
            "/api/admin/notices/save",
            json={"title": "", "content": ""},
        ).status_code
        == 400
    )
    assert (
        admin_api.post(
            "/api/admin/faqs/save",
            json={"category": "배송", "question": "", "answer": ""},
        ).status_code
        == 400
    )


@pytest.mark.tier2
@pytest.mark.f11
def test_t2_f11_05_non_admin_blocked_from_admin_cs_endpoints(
    live_server,
    guest_api,
    member_api,
):
    endpoints = [
        ("/api/admin/qna/answer", {"qna_id": 1, "answer": "hack"}),
        ("/api/admin/qna/delete", {"qna_id": 1}),
        ("/api/admin/inquiries/answer", {"inquiry_id": 1, "answer": "hack"}),
        ("/api/admin/notices/save", {"title": "hack", "content": "hack"}),
        ("/api/admin/notices/delete", {"id": 1}),
        ("/api/admin/faqs/save", {"question": "hack", "answer": "hack"}),
        ("/api/admin/faqs/delete", {"id": 1}),
    ]
    for path, body in endpoints:
        assert guest_api.post(path, json=body).status_code == 401, f"guest not 401 on {path}"
        assert member_api.post(path, json=body).status_code == 403, f"member not 403 on {path}"


@pytest.mark.tier2
@pytest.mark.f11
def test_t2_f11_06_csrf_missing_on_cs_mutations_rejected(
    live_server,
    member_api,
    admin_api,
):
    assert (
        member_api.post(
            "/api/products/1/qna",
            json={"title": "CSRF", "content": "CSRF 누락 테스트", "is_secret": False},
            include_csrf=False,
        ).status_code
        == 403
    )
    assert (
        member_api.post(
            "/api/inquiries/create",
            json={"category": "기타", "title": "CSRF", "content": "CSRF 누락 문의"},
            include_csrf=False,
        ).status_code
        == 403
    )
    assert (
        admin_api.post(
            "/api/admin/notices/save",
            json={"title": "CSRF 공지", "content": "본문"},
            include_csrf=False,
        ).status_code
        == 403
    )
    assert (
        admin_api.post(
            "/api/admin/qna/answer",
            json={"qna_id": 1, "answer": "답변"},
            include_csrf=False,
        ).status_code
        == 403
    )


# ---------------------------------------------------------------------------
# Feature F12 Boundaries & Security
# ---------------------------------------------------------------------------


@pytest.mark.tier2
@pytest.mark.f12
def test_t2_f12_01_unset_channel_env_vars_never_break_business_flows(
    server_factory,
    api_client_factory,
):
    srv = server_factory(
        env_overrides={
            "SMTP_HOST": "",
            "TELEGRAM_BOT_TOKEN": "",
            "KAKAO_ALIMTALK_KEY": "",
            "SMS_API_KEY": "",
        }
    )
    member = api_client_factory(srv)
    member.register("noti_flow@e2e.local", "FlowPass123!")

    order = create_order_via_api(
        member,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        payment_method="TOSS_PAYMENTS",
    )
    assert (
        member.post(
            "/api/payments/confirm",
            json={
                "order_code": order["order_code"],
                "payment_method": "TOSS_PAYMENTS",
                "payment_key": "flow_key_01",
                "amount": order["total_amount"],
            },
        ).status_code
        == 200
    )

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    assert (
        admin.post(
            "/api/admin/orders/tracking",
            json={
                "order_id": order["order_id"],
                "carrier_code": "CJ",
                "carrier_name": "CJ대한통운",
                "tracking_number": "999888777666",
                "mark_shipped": True,
            },
        ).status_code
        == 200
    )
    assert (
        admin.post(
            "/api/admin/order-status",
            json={"order_id": order["order_id"], "order_status": "배송완료"},
        ).status_code
        == 200
    )
    assert (
        member.post(
            "/api/orders/self-claim",
            json={"order_code": order["order_code"], "claim_type": "교환요청", "claim_reason": "사이즈 변경"},
        ).status_code
        == 200
    )

    logs_resp = admin.get("/api/admin/notifications/logs")
    assert logs_resp.status_code == 200
    logs = admin.json_or_raise(logs_resp).get("logs", [])
    order_logs = [l for l in logs if order["order_code"] in str(l)]
    assert len(order_logs) >= 3
    assert any("LOGGED_ONLY" in str(l.get("status", "")).upper() for l in order_logs)


@pytest.mark.tier2
@pytest.mark.f12
def test_t2_f12_02_non_admin_blocked_from_notification_logs(
    live_server,
    guest_api,
    member_api,
):
    assert guest_api.get("/api/admin/notifications/logs").status_code == 401
    assert member_api.get("/api/admin/notifications/logs").status_code == 403


@pytest.mark.tier2
@pytest.mark.f12
def test_t2_f12_03_notification_logs_never_leak_passwords_or_hashes(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    member = api_client_factory(srv)
    member.register("secret_check@e2e.local", "MemberSecret88!")

    guest = api_client_factory(srv)
    create_order_via_api(
        guest,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        guest_password="guest8888",
    )

    forgot = api_client_factory(srv)
    forgot.post(
        "/auth/forgot-password",
        json={"email": "secret_check@e2e.local"},
    )

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    logs_resp = admin.get("/api/admin/notifications/logs")
    assert logs_resp.status_code == 200
    raw = logs_resp.text
    for forbidden in ("MemberSecret88!", "guest8888", "guest_password_hash", "password_hash", "scrypt:"):
        assert forbidden not in raw


@pytest.mark.tier2
@pytest.mark.f12
def test_t2_f12_04_repeated_status_update_and_guest_without_email_safe(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    guest = api_client_factory(srv)
    order = create_order_via_api(
        guest,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        guest_password="noemail99",
    )

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    r1 = admin.post(
        "/api/admin/order-status",
        json={"order_id": order["order_id"], "payment_status": "입금완료"},
    )
    r2 = admin.post(
        "/api/admin/order-status",
        json={"order_id": order["order_id"], "payment_status": "입금완료"},
    )
    assert r1.status_code == 200
    assert r2.status_code == 200


@pytest.mark.tier2
@pytest.mark.f12
def test_t2_f12_05_unreachable_smtp_env_non_blocking_for_orders(
    server_factory,
    api_client_factory,
):
    srv = server_factory(
        env_overrides={
            "SMTP_HOST": "127.0.0.2",
            "SMTP_PORT": "1",
            "SMTP_USER": "test@e2e.local",
            "SMTP_PASSWORD": "x",
        }
    )
    guest = api_client_factory(srv)
    order = create_order_via_api(
        guest,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        guest_password="smtpfail1",
    )
    assert order["order_code"].startswith("ORD-")

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    logs_resp = admin.get("/api/admin/notifications/logs")
    assert logs_resp.status_code == 200
    assert order["order_code"] in logs_resp.text


# ---------------------------------------------------------------------------
# Feature F13 Boundaries & Security
# ---------------------------------------------------------------------------


@pytest.mark.tier2
@pytest.mark.f13
def test_t2_f13_01_other_member_cannot_edit_or_delete_authors_review(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    member_a = api_client_factory(srv)
    member_a.register("rev_a@e2e.local", "PassA123!")
    member_b = api_client_factory(srv)
    member_b.register("rev_b@e2e.local", "PassB123!")

    add_resp = member_a.post(
        "/api/reviews/add",
        json={"product_num": 1, "rating": 5, "content": "회원A 원본 리뷰입니다."},
    )
    assert add_resp.status_code == 200
    reviews = member_a.json_or_raise(member_a.get("/api/products/1"))["reviews"]
    review_id = [r for r in reviews if r["content"] == "회원A 원본 리뷰입니다."][0]["id"]

    edit_resp = member_b.post(
        "/api/reviews/update",
        json={"review_id": review_id, "rating": 1, "content": "타인 변조 시도"},
    )
    assert edit_resp.status_code in (403, 404)

    del_resp = member_b.post(
        "/api/reviews/delete",
        json={"review_id": review_id},
    )
    assert del_resp.status_code in (403, 404)

    after_reviews = member_a.json_or_raise(member_a.get("/api/products/1"))["reviews"]
    matched = [r for r in after_reviews if r["id"] == review_id]
    assert len(matched) == 1
    assert matched[0]["content"] == "회원A 원본 리뷰입니다."


@pytest.mark.tier2
@pytest.mark.f13
def test_t2_f13_02_guest_blocked_from_review_upload_add_update_delete(
    live_server,
    guest_api,
):
    assert (
        guest_api.post(
            "/api/reviews/upload-image",
            files={"image": ("a.png", b"png", "image/png")},
        ).status_code
        == 401
    )
    assert (
        guest_api.post(
            "/api/reviews/add",
            json={"product_num": 1, "rating": 5, "content": "비회원 리뷰"},
        ).status_code
        == 401
    )
    assert (
        guest_api.post(
            "/api/reviews/update",
            json={"review_id": 1, "rating": 4, "content": "수정 시도"},
        ).status_code
        == 401
    )
    assert (
        guest_api.post(
            "/api/reviews/delete",
            json={"review_id": 1},
        ).status_code
        == 401
    )


@pytest.mark.tier2
@pytest.mark.f13
def test_t2_f13_03_invalid_review_image_upload_rejected(
    live_server,
    member_api,
):
    assert (
        member_api.post(
            "/api/reviews/upload-image",
            files={"image": ("xss.html", b"<script>alert(1)</script>", "text/html")},
        ).status_code
        == 400
    )
    assert (
        member_api.post(
            "/api/reviews/upload-image",
            files={"image": ("empty.png", b"", "image/png")},
        ).status_code
        == 400
    )
    assert (
        member_api.post(
            "/api/reviews/upload-image",
            files={"image": ("bad.png", b"not-a-png", "image/png")},
        ).status_code
        == 400
    )
    assert (
        member_api.post(
            "/api/reviews/upload-image",
            data={"dummy": "no-file"},
        ).status_code
        == 400
    )


@pytest.mark.tier2
@pytest.mark.f13
def test_t2_f13_04_review_content_length_and_rating_boundaries(
    live_server,
    member_api,
):
    seed_product_via_sync(
        live_server.base_url,
        live_server.sync_secret,
        num=9137,
        name="F13 경계값 테스트 상품",
        price=25000,
    )

    assert (
        member_api.post(
            "/api/reviews/add",
            json={"product_num": 9137, "rating": 5, "content": "굿"},
        ).status_code
        == 400
    )
    assert (
        member_api.post(
            "/api/reviews/add",
            json={"product_num": 9137, "rating": 5, "content": "가" * 1001},
        ).status_code
        == 400
    )
    for bad_rating in (0, 6, "abc"):
        assert (
            member_api.post(
                "/api/reviews/add",
                json={"product_num": 9137, "rating": bad_rating, "content": "유효한 리뷰 본문"},
            ).status_code
            == 400
        )

    assert (
        member_api.post(
            "/api/reviews/add",
            json={"product_num": 9137, "rating": 1, "content": "좋음"},
        ).status_code
        == 200
    )
    assert (
        member_api.post(
            "/api/reviews/add",
            json={"product_num": 9137, "rating": 5, "content": "가" * 1000},
        ).status_code
        == 200
    )


@pytest.mark.tier2
@pytest.mark.f13
def test_t2_f13_05_non_admin_and_csrf_blocked_on_admin_review_moderation(
    live_server,
    guest_api,
    member_api,
    admin_api,
):
    assert guest_api.get("/api/admin/reviews").status_code == 401
    assert member_api.get("/api/admin/reviews").status_code == 403

    assert (
        guest_api.post(
            "/api/admin/reviews/moderate",
            json={"review_id": 1, "is_hidden": True},
        ).status_code
        == 401
    )
    assert (
        member_api.post(
            "/api/admin/reviews/moderate",
            json={"review_id": 1, "is_hidden": True},
        ).status_code
        == 403
    )
    assert (
        admin_api.post(
            "/api/admin/reviews/moderate",
            json={"review_id": 1, "is_hidden": True},
            include_csrf=False,
        ).status_code
        == 403
    )


@pytest.mark.tier2
@pytest.mark.f13
def test_t2_f13_06_cancelled_order_no_verified_badge_and_xss_escaped(
    server_factory,
    api_client_factory,
    desktop_page,
):
    srv = server_factory()
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=9138,
        name="F13 취소주문 및 XSS 상품",
        price=33000,
    )

    member = api_client_factory(srv)
    member.register("xss_rev@e2e.local", "PassX123!")
    order = create_order_via_api(
        member,
        items=[{"num": 9138, "color": "블랙", "size": "M", "quantity": 1}],
    )
    assert (
        member.post(
            "/api/orders/self-cancel",
            json={"order_code": order["order_code"], "claim_reason": "취소"},
        ).status_code
        == 200
    )

    xss_payload = "후기 <img src=x onerror=alert(1)> 끝"
    assert (
        member.post(
            "/api/reviews/add",
            json={"product_num": 9138, "rating": 4, "content": xss_payload},
        ).status_code
        == 200
    )

    prod_data = member.json_or_raise(member.get("/api/products/9138"))
    assert len(prod_data["reviews"]) == 1
    assert bool(prod_data["reviews"][0].get("is_verified_buyer")) is False

    diag = attach_page_diagnostics(desktop_page)
    desktop_page.goto(f"{srv.base_url}/product/9138", wait_until="domcontentloaded")
    assert desktop_page.locator("img[src='x']").count() == 0
    assert "<img src=x onerror=alert(1)>" in desktop_page.locator("#pdReviewList").inner_text()
    diag.assert_clean("f13_06_xss_escaped")
