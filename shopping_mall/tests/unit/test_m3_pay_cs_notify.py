# -*- coding: utf-8 -*-
"""
Milestone 3 (M3) — F10 (Bank Transfer Enhancements, Order Complete, PG Adapter),
F11 (Product Q&A, 1:1 Inquiry, Notices, FAQ), F12 (Multi-Channel Notifications),
FIX-1 (Shipping Guide baseFee > 0), FIX-2 (OBS_DB_PATH in apply-discount),
and Desktop/Mobile Playwright UI Verification.
"""

import hashlib
import hmac
import json
import os
import re
import smtplib
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

MALL_DIR = Path(__file__).resolve().parents[2]
if str(MALL_DIR) not in sys.path:
    sys.path.insert(0, str(MALL_DIR))

ADMIN_EMAIL = "m3admin@golabang.test"
ADMIN_PASS = "M3AdminPass!2026"


@pytest.fixture(scope="session", autouse=True)
def _bootstrap_isolated_env(tmp_path_factory):
    boot_dir = tmp_path_factory.mktemp("m3_boot")
    os.environ["MALL_DB_PATH"] = str(boot_dir / "boot_m3.db")
    os.environ["OBS_PULL_ENABLED"] = "0"
    os.environ["MALL_MODE"] = "cloud"
    os.environ["MALL_SCHEDULER_ENABLED"] = "0"
    os.environ["MALL_ADMIN_EMAIL"] = ADMIN_EMAIL
    os.environ["MALL_ADMIN_PASSWORD"] = ADMIN_PASS

    import mall_database  # noqa: F401
    import mall_security  # noqa: F401
    import mall_payment  # noqa: F401
    import mall_cs_service  # noqa: F401
    import mall_notifications  # noqa: F401
    import app as mall_app  # noqa: F401
    return boot_dir


@pytest.fixture()
def isolated_mall_db(tmp_path, monkeypatch):
    import mall_database
    import mall_security

    db_file = tmp_path / "m3_unit.db"
    monkeypatch.setenv("MALL_ADMIN_EMAIL", ADMIN_EMAIL)
    monkeypatch.setenv("MALL_ADMIN_PASSWORD", ADMIN_PASS)
    monkeypatch.setenv("OBS_PULL_ENABLED", "0")
    monkeypatch.setenv("MALL_MODE", "cloud")
    monkeypatch.setenv("MALL_SCHEDULER_ENABLED", "0")
    for env_k in (
        "MALL_SMTP_HOST",
        "SMTP_HOST",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "MALL_TELEGRAM_BOT_TOKEN",
        "MALL_TELEGRAM_CHAT_ID",
        "KAKAO_ALIMTALK_API_KEY",
        "KAKAO_ALIMTALK_KEY",
        "SMS_API_KEY",
        "PORTONE_API_SECRET",
        "PORTONE_WEBHOOK_SECRET",
        "TOSS_SECRET_KEY",
        "TOSS_WEBHOOK_SECRET",
    ):
        monkeypatch.delenv(env_k, raising=False)
    mall_security.rate_limiter._hits.clear()
    mall_database.set_db_path(str(db_file), init=True)
    mall_database.ensure_default_mall_admin()
    return db_file


def _seed_product(num: int = 301, name: str = "M3 프리미엄 코트", price: int = 30000, stock: int = 20):
    import mall_database

    return mall_database.upsert_product_from_obs(
        {
            "obs_product_id": num + 3000,
            "num": num,
            "name": name,
            "category": "OUTER",
            "price": price,
            "live_price": price - 5000,
            "consumer_price": price + 15000,
            "image_url": "",
            "specs": f"{name} 상세 스펙",
            "options": [{"color": "블랙", "size": "FREE", "stock": stock}],
        }
    )


def _create_member(email: str, password: str = "MemberPass123!", name: str = "김테스트", phone: str = "010-2222-3333"):
    import mall_database

    res = mall_database.create_mall_user(
        email=email,
        password=password,
        name=name,
        phone=phone,
        zipcode="06236",
        address="서울시 강남구 테헤란로 152",
        address_detail="10층",
    )
    assert res["ok"], res
    return res["user"]


def _login_client(client, email: str, password: str) -> str:
    res = client.post("/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.get_json()
    return res.get_json()["csrf_token"]


# ==============================================================================
# F10: Bank Transfer Enhancements, Order Complete Page & PG Adapter
# ==============================================================================


def test_f10_bank_transfer_fields_validation_and_order_complete_access(isolated_mall_db):
    import app as mall_app

    _seed_product(num=301, price=30000, stock=20)
    member_a = _create_member("buyer_a@test.com", name="구매자A")
    member_b = _create_member("buyer_b@test.com", name="구매자B")

    client_a = mall_app.app.test_client()
    csrf_a = _login_client(client_a, member_a["email"], "MemberPass123!")

    # Validation: invalid cash_receipt_type rejected
    bad_cr = client_a.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf_a},
        json={
            "buyer_name": "구매자A",
            "buyer_phone": "010-2222-3333",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "payment_method": "BANK",
            "cash_receipt_type": "UNKNOWN",
            "source_page": "CART_CHECKOUT",
            "items": [{"num": 301, "color": "블랙", "size": "FREE", "quantity": 1}],
        },
    )
    assert bad_cr.status_code == 400

    # Validation: INCOME without cash_receipt_no rejected
    missing_no = client_a.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf_a},
        json={
            "buyer_name": "구매자A",
            "buyer_phone": "010-2222-3333",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "payment_method": "BANK",
            "cash_receipt_type": "INCOME",
            "cash_receipt_no": "",
            "source_page": "CART_CHECKOUT",
            "items": [{"num": 301, "color": "블랙", "size": "FREE", "quantity": 1}],
        },
    )
    assert missing_no.status_code == 400

    # Validation: unknown payment_method rejected
    bad_pm = client_a.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf_a},
        json={
            "buyer_name": "구매자A",
            "buyer_phone": "010-2222-3333",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "payment_method": "CRYPTO_COIN",
            "source_page": "CART_CHECKOUT",
            "items": [{"num": 301, "color": "블랙", "size": "FREE", "quantity": 1}],
        },
    )
    assert bad_pm.status_code == 400

    # Valid bank order with depositor_name & cash_receipt_type=INCOME
    ok_res = client_a.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf_a},
        json={
            "buyer_name": "구매자A",
            "buyer_phone": "010-2222-3333",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "address_detail": "10층",
            "payment_method": "BANK",
            "depositor_name": "홍입금",
            "cash_receipt_type": "INCOME",
            "cash_receipt_no": "010-1234-5678",
            "source_page": "CART_CHECKOUT",
            "items": [{"num": 301, "color": "블랙", "size": "FREE", "quantity": 1}],
        },
    )
    assert ok_res.status_code == 200, ok_res.get_json()
    body = ok_res.get_json()
    assert body["status"] == "ok"
    assert body["ok"] is True
    ocode = body["order"]["order_code"]
    assert body["redirect_url"] == f"/order-complete/{ocode}"
    assert body["order"]["depositor_name"] == "홍입금"
    assert body["order"]["cash_receipt_type"] == "INCOME"
    assert body["order"]["cash_receipt_no"] == "010-1234-5678"

    # Owner can view /order-complete/<order_code>
    oc_owner = client_a.get(f"/order-complete/{ocode}")
    assert oc_owner.status_code == 200
    oc_html = oc_owner.get_data(as_text=True)
    assert ocode in oc_html
    assert "홍입금" in oc_html
    assert "010-1234-5678" in oc_html
    assert "btnCopyOrderCode" in oc_html

    # Different member and unauthenticated stranger are blocked with 403
    client_b = mall_app.app.test_client()
    _login_client(client_b, member_b["email"], "MemberPass123!")
    assert client_b.get(f"/order-complete/{ocode}").status_code == 403

    stranger = mall_app.app.test_client()
    assert stranger.get(f"/order-complete/{ocode}").status_code == 403
    assert stranger.get("/order-complete/ORD-DOES-NOT-EXIST").status_code == 404

    # Guest order: creator session can view /order-complete/<guest_code>, stranger cannot
    guest_client = mall_app.app.test_client()
    g_res = guest_client.post(
        "/api/orders/create",
        json={
            "buyer_name": "비회원테스터",
            "buyer_phone": "010-5555-6666",
            "zipcode": "04524",
            "address": "서울시 중구 세종대로 110",
            "guest_password": "4321",
            "payment_method": "BANK",
            "depositor_name": "비회원입금",
            "cash_receipt_type": "EXPENSE",
            "cash_receipt_no": "123-45-67890",
            "source_page": "CART_CHECKOUT",
            "items": [{"num": 301, "color": "블랙", "size": "FREE", "quantity": 1}],
        },
    )
    assert g_res.status_code == 200, g_res.get_json()
    gcode = g_res.get_json()["order"]["order_code"]
    g_oc = guest_client.get(f"/order-complete/{gcode}")
    assert g_oc.status_code == 200
    assert "비회원입금" in g_oc.get_data(as_text=True)
    assert "123-45-67890" in g_oc.get_data(as_text=True)
    assert stranger.get(f"/order-complete/{gcode}").status_code == 403


def test_f10_pg_adapter_prepare_confirm_and_fail_closed_webhooks(isolated_mall_db, monkeypatch):
    import mall_payment
    import mall_notifications
    import app as mall_app

    assert isinstance(mall_payment.get_payment_gateway("BANK"), mall_payment.BankTransferGateway)
    assert isinstance(mall_payment.get_payment_gateway("PORTONE"), mall_payment.PortOneV2Gateway)
    assert isinstance(mall_payment.get_payment_gateway("PORTONE_V2"), mall_payment.PortOneV2Gateway)
    assert isinstance(mall_payment.get_payment_gateway("TOSS"), mall_payment.TossPaymentsGateway)
    assert isinstance(mall_payment.get_payment_gateway("TOSS_PAYMENTS"), mall_payment.TossPaymentsGateway)
    assert isinstance(mall_payment.get_payment_gateway("CARD"), mall_payment.PortOneV2Gateway)
    with pytest.raises(ValueError):
        mall_payment.get_payment_gateway("INVALID_PG")

    _seed_product(num=302, price=45000, stock=10)
    member = _create_member("pgbuyer@test.com")
    client = mall_app.app.test_client()
    csrf = _login_client(client, member["email"], "MemberPass123!")

    # Create PORTONE_V2 order
    ord_res = client.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf},
        json={
            "buyer_name": "PG구매자",
            "buyer_phone": "010-2222-3333",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "payment_method": "PORTONE_V2",
            "source_page": "CART_CHECKOUT",
            "items": [{"num": 302, "color": "블랙", "size": "FREE", "quantity": 1}],
        },
    )
    assert ord_res.status_code == 200
    ord_obj = ord_res.get_json()["order"]
    ocode = ord_obj["order_code"]
    total_amt = int(ord_obj["total_amount"])
    assert ord_obj["payment_method"] == "PORTONE_V2"
    assert ord_obj["pg_provider"] == "PORTONE_V2"

    # Prepare payment
    prep_res = client.post(
        "/api/payments/prepare",
        headers={"X-CSRF-Token": csrf},
        json={"order_code": ocode, "payment_method": "PORTONE_V2"},
    )
    assert prep_res.status_code == 200
    prep_data = prep_res.get_json()
    assert prep_data["provider"] == "PORTONE_V2"
    assert prep_data["amount"] == total_amt

    # Confirm payment with mismatched amount -> 400
    bad_amt = client.post(
        "/api/payments/confirm",
        headers={"X-CSRF-Token": csrf},
        json={"order_code": ocode, "payment_method": "PORTONE_V2", "payment_key": "po2_test_1", "amount": total_amt - 1000},
    )
    assert bad_amt.status_code == 400

    # Confirm payment in sandbox mode -> 200 & 입금완료 + PAYMENT_CONFIRMED log
    good_confirm = client.post(
        "/api/payments/confirm",
        headers={"X-CSRF-Token": csrf},
        json={"order_code": ocode, "payment_method": "PORTONE_V2", "payment_key": "po2_test_1", "amount": total_amt},
    )
    assert good_confirm.status_code == 200, good_confirm.get_json()
    confirmed_ord = good_confirm.get_json()["order"]
    assert confirmed_ord["payment_status"] == "입금완료"
    assert confirmed_ord["order_status"] == "상품준비중"
    assert confirmed_ord["pg_tid"] == "po2_test_1"
    assert confirmed_ord["paid_at"] != ""

    pay_logs = mall_notifications.list_notification_logs(event_type="PAYMENT_CONFIRMED")
    assert any(l["ref_code"] == ocode for l in pay_logs)

    # Create TOSS_PAYMENTS order for webhook security tests
    toss_res = client.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf},
        json={
            "buyer_name": "토스구매자",
            "buyer_phone": "010-2222-3333",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "payment_method": "TOSS_PAYMENTS",
            "source_page": "CART_CHECKOUT",
            "items": [{"num": 302, "color": "블랙", "size": "FREE", "quantity": 1}],
        },
    )
    t_ord = toss_res.get_json()["order"]
    t_code = t_ord["order_code"]
    t_amt = int(t_ord["total_amount"])

    # Fail-closed when secret unset
    wh_payload = json.dumps({"orderId": t_code, "paymentKey": "toss_wh_1", "status": "DONE", "totalAmount": t_amt}).encode("utf-8")
    wh_no_secret = client.post("/api/payments/webhook/toss", data=wh_payload, content_type="application/json")
    assert wh_no_secret.status_code in (401, 403)

    # Fail-closed when invalid signature
    monkeypatch.setenv("TOSS_WEBHOOK_SECRET", "toss_wh_secret_123")
    wh_bad_sig = client.post(
        "/api/payments/webhook/toss",
        data=wh_payload,
        content_type="application/json",
        headers={"X-Toss-Signature": "deadbeef"},
    )
    assert wh_bad_sig.status_code in (401, 403)

    # Valid signature + wrong amount -> 400
    bad_wh_payload = json.dumps({"orderId": t_code, "paymentKey": "toss_wh_1", "status": "DONE", "totalAmount": 100}).encode("utf-8")
    bad_sig = hmac.new(b"toss_wh_secret_123", bad_wh_payload, hashlib.sha256).hexdigest()
    wh_bad_amt = client.post(
        "/api/payments/webhook/toss",
        data=bad_wh_payload,
        content_type="application/json",
        headers={"X-Toss-Signature": bad_sig},
    )
    assert wh_bad_amt.status_code == 400

    # Valid signature + exact amount -> 200
    good_sig = hmac.new(b"toss_wh_secret_123", wh_payload, hashlib.sha256).hexdigest()
    wh_ok = client.post(
        "/api/payments/webhook/toss",
        data=wh_payload,
        content_type="application/json",
        headers={"X-Toss-Signature": good_sig},
    )
    assert wh_ok.status_code == 200, wh_ok.get_json()
    assert wh_ok.get_json()["order"]["payment_status"] == "입금완료"
    assert wh_ok.get_json()["order"]["pg_tid"] == "toss_wh_1"


def test_f10_regression_bank_and_method_switching_blocked(isolated_mall_db, monkeypatch):
    import mall_database
    import app as mall_app

    _seed_product(num=310, price=50000, stock=10)
    member = _create_member("pg_guard@test.com")
    client = mall_app.app.test_client()
    csrf = _login_client(client, member["email"], "MemberPass123!")

    # 1. BANK order cannot be confirmed via /api/payments/confirm (even with payment_method="PORTONE_V2")
    bank_res = client.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf},
        json={
            "buyer_name": "무통장방어",
            "buyer_phone": "010-2222-3333",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "payment_method": "BANK",
            "source_page": "CART_CHECKOUT",
            "items": [{"num": 310, "color": "블랙", "size": "FREE", "quantity": 1}],
        },
    )
    assert bank_res.status_code == 200
    bank_ord = bank_res.get_json()["order"]
    bank_code = bank_ord["order_code"]
    bank_amt = int(bank_ord["total_amount"])

    # /api/payments/prepare unknown method rejected, valid prepare does not mutate order state
    prep_bad = client.post(
        "/api/payments/prepare",
        headers={"X-CSRF-Token": csrf},
        json={"order_code": bank_code, "payment_method": "INVALID_PG"},
    )
    assert prep_bad.status_code == 400
    prep_ok = client.post(
        "/api/payments/prepare",
        headers={"X-CSRF-Token": csrf},
        json={"order_code": bank_code, "payment_method": "PORTONE_V2"},
    )
    assert prep_ok.status_code == 200
    assert mall_database.get_mall_order_raw_by_code_or_id(order_code=bank_code)["payment_status"] == "입금대기"
    assert mall_database.get_mall_order_raw_by_code_or_id(order_code=bank_code)["payment_method"] == "BANK"

    # /api/payments/confirm with BANK (explicit or omitted) or switched to PORTONE_V2 -> 400
    for payload in (
        {"order_code": bank_code, "payment_method": "PORTONE_V2", "payment_key": "po2_hack", "amount": bank_amt},
        {"order_code": bank_code, "payment_method": "BANK", "payment_key": "bank_hack", "amount": bank_amt},
        {"order_code": bank_code, "payment_key": "omitted_hack", "amount": bank_amt},
    ):
        resp = client.post("/api/payments/confirm", headers={"X-CSRF-Token": csrf}, json=payload)
        assert resp.status_code == 400, f"Expected 400 for BANK confirm {payload}, got {resp.status_code}"
        cur = mall_database.get_mall_order_raw_by_code_or_id(order_code=bank_code)
        assert cur["payment_status"] == "입금대기"
        assert cur["order_status"] == "결제대기"

    # Signed PG webhook targeting BANK order -> 400
    monkeypatch.setenv("PORTONE_WEBHOOK_SECRET", "po_wh_sec_1")
    wh_bank_body = json.dumps({"order_code": bank_code, "paymentId": "po_hack", "status": "PAID", "amount": bank_amt}).encode("utf-8")
    wh_bank_sig = hmac.new(b"po_wh_sec_1", wh_bank_body, hashlib.sha256).hexdigest()
    wh_bank = client.post(
        "/api/payments/webhook/portone",
        data=wh_bank_body,
        content_type="application/json",
        headers={"X-PortOne-Signature": wh_bank_sig},
    )
    assert wh_bank.status_code == 400
    assert mall_database.get_mall_order_raw_by_code_or_id(order_code=bank_code)["payment_status"] == "입금대기"

    # 2. TOSS_PAYMENTS order cannot bypass TOSS_SECRET_KEY via PORTONE_V2 confirm or portone webhook
    monkeypatch.setenv("TOSS_SECRET_KEY", "test_sk_live_key_123")
    toss_res = client.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf},
        json={
            "buyer_name": "토스방어",
            "buyer_phone": "010-2222-3333",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "payment_method": "TOSS_PAYMENTS",
            "source_page": "CART_CHECKOUT",
            "items": [{"num": 310, "color": "블랙", "size": "FREE", "quantity": 1}],
        },
    )
    assert toss_res.status_code == 200
    toss_ord = toss_res.get_json()["order"]
    toss_code = toss_ord["order_code"]
    toss_amt = int(toss_ord["total_amount"])

    cross_confirm = client.post(
        "/api/payments/confirm",
        headers={"X-CSRF-Token": csrf},
        json={"order_code": toss_code, "payment_method": "PORTONE_V2", "payment_key": "po2_bypass", "amount": toss_amt},
    )
    assert cross_confirm.status_code == 400
    assert mall_database.get_mall_order_raw_by_code_or_id(order_code=toss_code)["payment_status"] == "입금대기"

    wh_cross_body = json.dumps({"order_code": toss_code, "paymentId": "po_cross", "status": "PAID", "amount": toss_amt}).encode("utf-8")
    wh_cross_sig = hmac.new(b"po_wh_sec_1", wh_cross_body, hashlib.sha256).hexdigest()
    wh_cross = client.post(
        "/api/payments/webhook/portone",
        data=wh_cross_body,
        content_type="application/json",
        headers={"X-PortOne-Signature": wh_cross_sig},
    )
    assert wh_cross.status_code == 400
    assert mall_database.get_mall_order_raw_by_code_or_id(order_code=toss_code)["payment_status"] == "입금대기"

    # 3. CARD order maps to PORTONE_V2 and renders without bank deposit box on /orders
    card_res = client.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf},
        json={
            "buyer_name": "카드호환",
            "buyer_phone": "010-2222-3333",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "payment_method": "CARD",
            "source_page": "CART_CHECKOUT",
            "items": [{"num": 310, "color": "블랙", "size": "FREE", "quantity": 1}],
        },
    )
    assert card_res.status_code == 200
    card_ord = card_res.get_json()["order"]
    card_confirm = client.post(
        "/api/payments/confirm",
        headers={"X-CSRF-Token": csrf},
        json={"order_code": card_ord["order_code"], "payment_method": "PORTONE_V2", "payment_key": "po2_card_1", "amount": int(card_ord["total_amount"])},
    )
    assert card_confirm.status_code == 200
    assert card_confirm.get_json()["order"]["payment_status"] == "입금완료"

    # /orders renders bank box for BANK order only (not for TOSS_PAYMENTS or CARD orders)
    orders_html = client.get("/orders").get_data(as_text=True)
    assert orders_html.count(">입금 계좌:") == 1, f"Expected only 1 order bank box (for BANK order), found {orders_html.count('>입금 계좌:')}"


def test_f10_regression_refund_claim_states_and_idempotency(isolated_mall_db, monkeypatch):
    import mall_database
    import mall_notifications
    import app as mall_app

    _seed_product(num=311, price=40000, stock=10)
    member = _create_member("refund_guard@test.com")
    client = mall_app.app.test_client()
    csrf = _login_client(client, member["email"], "MemberPass123!")

    ord_res = client.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf},
        json={
            "buyer_name": "환불가드",
            "buyer_phone": "010-2222-3333",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "payment_method": "PORTONE_V2",
            "source_page": "CART_CHECKOUT",
            "items": [{"num": 311, "color": "블랙", "size": "FREE", "quantity": 1}],
        },
    )
    assert ord_res.status_code == 200
    ord_obj = ord_res.get_json()["order"]
    ocode = ord_obj["order_code"]
    amt = int(ord_obj["total_amount"])

    # Confirm initially -> 입금완료
    c1 = client.post(
        "/api/payments/confirm",
        headers={"X-CSRF-Token": csrf},
        json={"order_code": ocode, "payment_method": "PORTONE_V2", "payment_key": "po2_same_tid", "amount": amt},
    )
    assert c1.status_code == 200
    logs_before = len([l for l in mall_notifications.list_notification_logs(event_type="PAYMENT_CONFIRMED") if l["ref_code"] == ocode])
    assert logs_before == 1

    # Idempotent replay with same pg_tid -> 200, no duplicate PAYMENT_CONFIRMED
    c_replay = client.post(
        "/api/payments/confirm",
        headers={"X-CSRF-Token": csrf},
        json={"order_code": ocode, "payment_method": "PORTONE_V2", "payment_key": "po2_same_tid", "amount": amt},
    )
    assert c_replay.status_code == 200
    logs_after = len([l for l in mall_notifications.list_notification_logs(event_type="PAYMENT_CONFIRMED") if l["ref_code"] == ocode])
    assert logs_after == 1

    # Different pg_tid on already-paid order -> 400
    c_diff_tid = client.post(
        "/api/payments/confirm",
        headers={"X-CSRF-Token": csrf},
        json={"order_code": ocode, "payment_method": "PORTONE_V2", "payment_key": "po2_different_tid", "amount": amt},
    )
    assert c_diff_tid.status_code == 400

    # Customer submits refund request -> order_status=환불요청, payment_status=환불대기
    claim_res = client.post(
        "/api/orders/self-claim",
        headers={"X-CSRF-Token": csrf},
        json={"order_code": ocode, "claim_type": "환불요청", "claim_reason": "단순 변심 환불"},
    )
    assert claim_res.status_code == 200
    claimed = mall_database.get_mall_order_raw_by_code_or_id(order_code=ocode)
    assert claimed["order_status"] == "환불요청"
    assert claimed["payment_status"] == "환불대기"

    # Confirm replay on 환불요청 / 환불대기 -> 400 & state unchanged
    c_refund = client.post(
        "/api/payments/confirm",
        headers={"X-CSRF-Token": csrf},
        json={"order_code": ocode, "payment_method": "PORTONE_V2", "payment_key": "po2_same_tid", "amount": amt},
    )
    assert c_refund.status_code == 400
    assert mall_database.get_mall_order_raw_by_code_or_id(order_code=ocode)["payment_status"] == "환불대기"

    # Signed webhook replay on 환불요청 / 환불대기 -> 400 & state unchanged
    monkeypatch.setenv("PORTONE_WEBHOOK_SECRET", "po_wh_refund_sec")
    wh_body = json.dumps({"order_code": ocode, "paymentId": "po2_same_tid", "status": "PAID", "amount": amt}).encode("utf-8")
    wh_sig = hmac.new(b"po_wh_refund_sec", wh_body, hashlib.sha256).hexdigest()
    wh_refund = client.post(
        "/api/payments/webhook/portone",
        data=wh_body,
        content_type="application/json",
        headers={"X-PortOne-Signature": wh_sig},
    )
    assert wh_refund.status_code == 400
    after_wh = mall_database.get_mall_order_raw_by_code_or_id(order_code=ocode)
    assert after_wh["payment_status"] == "환불대기"
    assert after_wh["order_status"] == "환불요청"


def test_f10_regression_strict_amount_and_explicit_webhook_status(isolated_mall_db, monkeypatch):
    import mall_database
    import mall_payment
    import app as mall_app

    # Unit check strict integer parser
    assert mall_payment._parse_strict_int_amount(54000) == 54000
    assert mall_payment._parse_strict_int_amount("54000") == 54000
    assert mall_payment._parse_strict_int_amount(0) == 0
    for bad_val in (True, False, 54000.9, "54000.9", "54000.0", "abc", "", None):
        with pytest.raises(ValueError):
            mall_payment._parse_strict_int_amount(bad_val)

    _seed_product(num=312, price=50000, stock=10)
    member = _create_member("strict_amt@test.com")
    client = mall_app.app.test_client()
    csrf = _login_client(client, member["email"], "MemberPass123!")

    toss_res = client.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf},
        json={
            "buyer_name": "엄격검증",
            "buyer_phone": "010-2222-3333",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "payment_method": "TOSS_PAYMENTS",
            "source_page": "CART_CHECKOUT",
            "items": [{"num": 312, "color": "블랙", "size": "FREE", "quantity": 1}],
        },
    )
    assert toss_res.status_code == 200
    t_ord = toss_res.get_json()["order"]
    t_code = t_ord["order_code"]
    t_amt = int(t_ord["total_amount"])  # 54000

    # Confirm with float 54000.9 or "54000.9" or bool -> 400
    for bad_amt in (t_amt + 0.9, f"{t_amt}.9", True):
        res = client.post(
            "/api/payments/confirm",
            headers={"X-CSRF-Token": csrf},
            json={"order_code": t_code, "payment_method": "TOSS_PAYMENTS", "payment_key": "toss_k1", "amount": bad_amt},
        )
        assert res.status_code == 400
        assert mall_database.get_mall_order_raw_by_code_or_id(order_code=t_code)["payment_status"] == "입금대기"

    # Signed Toss webhook with amount 54000.9 -> 400
    monkeypatch.setenv("TOSS_WEBHOOK_SECRET", "toss_strict_sec")
    float_body = json.dumps({"orderId": t_code, "paymentKey": "toss_wh_f", "status": "DONE", "totalAmount": t_amt + 0.9}).encode("utf-8")
    float_sig = hmac.new(b"toss_strict_sec", float_body, hashlib.sha256).hexdigest()
    wh_float = client.post(
        "/api/payments/webhook/toss",
        data=float_body,
        content_type="application/json",
        headers={"X-Toss-Signature": float_sig},
    )
    assert wh_float.status_code == 400
    assert mall_database.get_mall_order_raw_by_code_or_id(order_code=t_code)["payment_status"] == "입금대기"

    # Signed Toss webhook with eventType="PAYMENT_STATUS_CHANGED" (missing status/amount) -> must NOT mark 입금완료
    evt_body = json.dumps({"eventType": "PAYMENT_STATUS_CHANGED", "data": {"orderId": t_code, "paymentKey": "toss_wh_evt"}}).encode("utf-8")
    evt_sig = hmac.new(b"toss_strict_sec", evt_body, hashlib.sha256).hexdigest()
    wh_evt = client.post(
        "/api/payments/webhook/toss",
        data=evt_body,
        content_type="application/json",
        headers={"X-Toss-Signature": evt_sig},
    )
    assert wh_evt.status_code == 200
    assert mall_database.get_mall_order_raw_by_code_or_id(order_code=t_code)["payment_status"] == "입금대기"

    # Signed Toss webhook with explicit DONE but missing amount -> 400, must NOT mark 입금완료
    no_amt_body = json.dumps({"orderId": t_code, "paymentKey": "toss_wh_noamt", "status": "DONE"}).encode("utf-8")
    no_amt_sig = hmac.new(b"toss_strict_sec", no_amt_body, hashlib.sha256).hexdigest()
    wh_no_amt = client.post(
        "/api/payments/webhook/toss",
        data=no_amt_body,
        content_type="application/json",
        headers={"X-Toss-Signature": no_amt_sig},
    )
    assert wh_no_amt.status_code == 400
    assert mall_database.get_mall_order_raw_by_code_or_id(order_code=t_code)["payment_status"] == "입금대기"

    # Signed PortOne webhook with missing status/amount -> must NOT mark 입금완료
    po_res = client.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf},
        json={
            "buyer_name": "포트원엄격",
            "buyer_phone": "010-2222-3333",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "payment_method": "PORTONE_V2",
            "source_page": "CART_CHECKOUT",
            "items": [{"num": 312, "color": "블랙", "size": "FREE", "quantity": 1}],
        },
    )
    po_code = po_res.get_json()["order"]["order_code"]
    monkeypatch.setenv("PORTONE_WEBHOOK_SECRET", "po_strict_sec")
    po_missing_body = json.dumps({"data": {"order_code": po_code, "paymentId": "po_wh_miss"}}).encode("utf-8")
    po_missing_sig = hmac.new(b"po_strict_sec", po_missing_body, hashlib.sha256).hexdigest()
    wh_po_miss = client.post(
        "/api/payments/webhook/portone",
        data=po_missing_body,
        content_type="application/json",
        headers={"X-PortOne-Signature": po_missing_sig},
    )
    assert wh_po_miss.status_code == 200
    assert mall_database.get_mall_order_raw_by_code_or_id(order_code=po_code)["payment_status"] == "입금대기"


# ==============================================================================
# F11: Product Q&A (Secret Posts), 1:1 Inquiry, Notices & FAQ
# ==============================================================================


def test_f11_product_qna_secret_masking_and_admin_controls(isolated_mall_db):
    import app as mall_app

    _seed_product(num=303, price=25000, stock=10)
    user_a = _create_member("qna_a@test.com", name="질문자A")
    user_b = _create_member("qna_b@test.com", name="타인B")

    guest_c = mall_app.app.test_client()
    assert guest_c.post("/api/products/303/qna", json={"content": "로그인없이"}).status_code == 401

    client_a = mall_app.app.test_client()
    csrf_a = _login_client(client_a, user_a["email"], "MemberPass123!")

    # Missing CSRF -> 403
    assert client_a.post("/api/products/303/qna", json={"content": "CSRF 누락 테스트"}).status_code == 403

    # Create secret Q&A + public Q&A
    sec_res = client_a.post(
        "/api/products/303/qna",
        headers={"X-CSRF-Token": csrf_a},
        json={"title": "비밀 문의입니다", "content": "비밀 상세 내용 9988", "is_secret": True},
    )
    assert sec_res.status_code == 200, sec_res.get_json()
    sec_id = sec_res.get_json()["item"]["id"]

    pub_res = client_a.post(
        "/api/products/303/qna",
        headers={"X-CSRF-Token": csrf_a},
        json={"title": "공개 문의", "content": "공개 상세 내용 1122", "is_secret": False},
    )
    assert pub_res.status_code == 200
    pub_id = pub_res.get_json()["item"]["id"]

    # Admin answers secret Q&A
    admin_c = mall_app.app.test_client()
    csrf_adm = _login_client(admin_c, ADMIN_EMAIL, ADMIN_PASS)
    ans_res = admin_c.post(
        "/api/admin/qna/answer",
        headers={"X-CSRF-Token": csrf_adm},
        json={"qna_id": sec_id, "answer_content": "비밀 관리자 답변 7766"},
    )
    assert ans_res.status_code == 200

    # Guest & User B see masked secret Q&A (zero secret leakage)
    client_b = mall_app.app.test_client()
    _login_client(client_b, user_b["email"], "MemberPass123!")
    for viewer_c in (guest_c, client_b):
        qna_list = viewer_c.get("/api/products/303/qna").get_json()["items"]
        sec_item = next(x for x in qna_list if x["id"] == sec_id)
        pub_item = next(x for x in qna_list if x["id"] == pub_id)
        assert sec_item["is_secret"] is True
        assert sec_item["is_masked"] is True
        assert sec_item["user_id"] is None
        assert "9988" not in sec_item["content"]
        assert "7766" not in sec_item["answer_content"]
        assert pub_item["is_masked"] is False
        assert "1122" in pub_item["content"]

    # Author & Admin see unmasked secret Q&A + answer
    for priv_c in (client_a, admin_c):
        qna_list = priv_c.get("/api/products/303/qna").get_json()["items"]
        sec_item = next(x for x in qna_list if x["id"] == sec_id)
        assert sec_item["is_masked"] is False
        assert "9988" in sec_item["content"]
        assert "7766" in sec_item["answer_content"]

    # Non-admin cannot answer or delete Q&A
    assert client_b.post("/api/admin/qna/answer", headers={"X-CSRF-Token": csrf_a}, json={"qna_id": pub_id, "answer_content": "hack"}).status_code == 403
    assert client_b.post("/api/admin/qna/delete", headers={"X-CSRF-Token": csrf_a}, json={"qna_id": pub_id}).status_code == 403

    # Admin can delete public Q&A
    del_res = admin_c.post("/api/admin/qna/delete", headers={"X-CSRF-Token": csrf_adm}, json={"qna_id": pub_id})
    assert del_res.status_code == 200


def test_f11_inquiries_idor_notices_and_faqs(isolated_mall_db):
    import app as mall_app

    user_a = _create_member("inq_a@test.com", name="문의자A")
    user_b = _create_member("inq_b@test.com", name="문의자B")

    guest_c = mall_app.app.test_client()
    assert guest_c.get("/api/inquiries").status_code == 401
    assert guest_c.post("/api/inquiries/create", json={"title": "t", "content": "c"}).status_code == 401

    client_a = mall_app.app.test_client()
    csrf_a = _login_client(client_a, user_a["email"], "MemberPass123!")
    client_b = mall_app.app.test_client()
    csrf_b = _login_client(client_b, user_b["email"], "MemberPass123!")

    # Missing CSRF -> 403
    assert client_a.post("/api/inquiries/create", json={"title": "제목", "content": "내용입니다"}).status_code == 403

    cr_a = client_a.post(
        "/api/inquiries/create",
        headers={"X-CSRF-Token": csrf_a},
        json={"category": "배송문의", "title": "A의 비밀 1:1 문의", "content": "A만 봐야하는 내용 4455", "order_code": "ORD-TEST-1"},
    )
    assert cr_a.status_code == 200
    inq_id = cr_a.get_json()["item"]["id"]

    # User B cannot see User A's inquiries (even with ?scope=all)
    b_list = client_b.get("/api/inquiries?scope=all").get_json()["inquiries"]
    assert all(x["id"] != inq_id for x in b_list)
    assert client_b.get("/api/admin/inquiries").status_code == 403

    # Admin lists and answers inquiry
    admin_c = mall_app.app.test_client()
    csrf_adm = _login_client(admin_c, ADMIN_EMAIL, ADMIN_PASS)
    adm_inqs = admin_c.get("/api/admin/inquiries").get_json()["inquiries"]
    assert any(x["id"] == inq_id for x in adm_inqs)

    ans_res = admin_c.post(
        "/api/admin/inquiries/answer",
        headers={"X-CSRF-Token": csrf_adm},
        json={"inquiry_id": inq_id, "answer_content": "오늘 오후 출고 예정입니다."},
    )
    assert ans_res.status_code == 200
    assert ans_res.get_json()["item"]["status"] == "답변완료"

    # Notices & FAQs CRUD + active filter
    assert client_a.post("/api/admin/notices/save", headers={"X-CSRF-Token": csrf_a}, json={"title": "x", "content": "y"}).status_code == 403
    n_res = admin_c.post(
        "/api/admin/notices/save",
        headers={"X-CSRF-Token": csrf_adm},
        json={"title": "추석 배송 공지", "content": "연휴 기간 배송 안내", "is_pinned": True},
    )
    assert n_res.status_code == 200
    nid = n_res.get_json()["item"]["id"]
    assert any(n["id"] == nid and n["is_pinned"] for n in guest_c.get("/api/notices").get_json()["notices"])

    f1 = admin_c.post(
        "/api/admin/faqs/save",
        headers={"X-CSRF-Token": csrf_adm},
        json={"category": "배송", "question": "당일 출고 기준?", "answer": "오후 2시 이전 입금 확인 건", "sort_order": 1, "is_active": True},
    )
    f2 = admin_c.post(
        "/api/admin/faqs/save",
        headers={"X-CSRF-Token": csrf_adm},
        json={"category": "배송", "question": "비공개 FAQ", "answer": "숨김 답변", "sort_order": 2, "is_active": False},
    )
    assert f1.status_code == 200 and f2.status_code == 200
    public_faqs = guest_c.get("/api/faqs").get_json()["faqs"]
    assert len(public_faqs) == 1
    assert public_faqs[0]["question"] == "당일 출고 기준?"
    assert len(admin_c.get("/api/admin/faqs").get_json()["faqs"]) == 2

    assert admin_c.post("/api/admin/notices/delete", headers={"X-CSRF-Token": csrf_adm}, json={"notice_id": nid}).status_code == 200
    assert admin_c.post("/api/admin/faqs/delete", headers={"X-CSRF-Token": csrf_adm}, json={"faq_id": f2.get_json()["item"]["id"]}).status_code == 200


# ==============================================================================
# F12: Multi-Channel Notifications (Fallback LOGGED_ONLY + Mocked SMTP/Telegram)
# ==============================================================================


def test_f12_notification_triggers_fallback_and_mocked_transports(isolated_mall_db, monkeypatch):
    import mall_notifications
    import app as mall_app

    _seed_product(num=304, price=35000, stock=15)
    member = _create_member("notify_user@test.com", name="알림고객")
    client = mall_app.app.test_client()
    csrf = _login_client(client, member["email"], "MemberPass123!")

    # 1. ORDER_CREATED -> customer + admin LOGGED_ONLY (no password/hash leak)
    ord_res = client.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf},
        json={
            "buyer_name": "알림고객",
            "buyer_phone": "010-2222-3333",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "payment_method": "BANK",
            "source_page": "CART_CHECKOUT",
            "items": [{"num": 304, "color": "블랙", "size": "FREE", "quantity": 1}],
        },
    )
    assert ord_res.status_code == 200
    ord_obj = ord_res.get_json()["order"]
    oid = ord_obj["order_id"]
    ocode = ord_obj["order_code"]

    # Auth guard on /api/admin/notifications/logs
    assert mall_app.app.test_client().get("/api/admin/notifications/logs").status_code == 401
    assert client.get("/api/admin/notifications/logs").status_code == 403

    admin_c = mall_app.app.test_client()
    csrf_adm = _login_client(admin_c, ADMIN_EMAIL, ADMIN_PASS)
    logs_created = admin_c.get("/api/admin/notifications/logs?event_type=ORDER_CREATED").get_json()["logs"]
    assert len(logs_created) >= 2
    assert all(l["status"] == "LOGGED_ONLY" for l in logs_created if l["ref_code"] == ocode)
    assert all("password" not in (l["body"] + l["meta_json"]).lower() for l in logs_created)

    # 2. PAYMENT_CONFIRMED on admin status -> 입금완료
    st_res = admin_c.post(
        "/api/admin/order-status",
        headers={"X-CSRF-Token": csrf_adm},
        json={"order_id": oid, "payment_status": "입금완료", "order_status": "상품준비중"},
    )
    assert st_res.status_code == 200
    logs_paid = admin_c.get("/api/admin/notifications/logs?event_type=PAYMENT_CONFIRMED").get_json()["logs"]
    assert any(l["ref_code"] == ocode and l["status"] == "LOGGED_ONLY" for l in logs_paid)

    # 3. ORDER_SHIPPED on tracking update with mark_shipped=True
    trk_res = admin_c.post(
        "/api/admin/orders/tracking",
        headers={"X-CSRF-Token": csrf_adm},
        json={"order_id": oid, "carrier_code": "CJ", "tracking_number": "123456789012", "mark_shipped": True},
    )
    assert trk_res.status_code == 200
    logs_ship = admin_c.get("/api/admin/notifications/logs?event_type=ORDER_SHIPPED").get_json()["logs"]
    assert any(l["ref_code"] == ocode and "123456789012" in l["body"] for l in logs_ship)

    # 4. CLAIM_REQUESTED on self-claim
    claim_res = client.post(
        "/api/orders/self-claim",
        headers={"X-CSRF-Token": csrf},
        json={"order_code": ocode, "claim_type": "교환요청", "claim_reason": "사이즈 교환 요청합니다"},
    )
    assert claim_res.status_code == 200
    logs_claim = admin_c.get("/api/admin/notifications/logs?event_type=CLAIM_REQUESTED").get_json()["logs"]
    assert any(l["ref_code"] == ocode and "사이즈 교환" in l["body"] for l in logs_claim)

    # 5. Mocked SMTP SENT + Telegram SENT + transport failure graceful FAILED
    sent_messages = []

    class DummySMTP:
        def __init__(self, host, port, timeout=None):
            self.host = host
            self.port = port

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def starttls(self):
            return None

        def login(self, user, pw):
            return None

        def send_message(self, msg):
            sent_messages.append(msg)

    class DummyResp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"ok":true}'

    monkeypatch.setenv("MALL_SMTP_HOST", "smtp.mock.local")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "99999")
    monkeypatch.setattr(smtplib, "SMTP", DummySMTP)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=2.0: DummyResp())

    res_email = mall_notifications.dispatch_notification("ORDER_CREATED", "EMAIL", "c@test.com", "테스트 제목", "테스트 본문", ref_code="ORD-SMTP")
    assert res_email["status"] == "SENT"
    assert len(sent_messages) == 1

    res_tg = mall_notifications.dispatch_notification("ORDER_CREATED", "TELEGRAM", "99999", "텔레그램", "본문", ref_code="ORD-TG")
    assert res_tg["status"] == "SENT"

    def _boom(*args, **kwargs):
        raise RuntimeError("mock transport failure")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    res_fail = mall_notifications.dispatch_notification("ORDER_CREATED", "TELEGRAM", "99999", "실패테스트", "본문", ref_code="ORD-FAIL")
    assert res_fail["status"] == "FAILED"
    assert "mock transport failure" in res_fail["error_msg"]


# ==============================================================================
# FIX-2: apply-discount uses _get_obs_db_path()
# ==============================================================================


def test_fix2_admin_apply_discount_uses_obs_db_path(isolated_mall_db, tmp_path, monkeypatch):
    import app as mall_app

    _seed_product(num=305, price=70000, stock=5)
    obs_db = tmp_path / "mock_obs.db"
    conn = sqlite3.connect(str(obs_db))
    conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, num INTEGER, consumer_price INTEGER, price INTEGER)")
    conn.execute("INSERT INTO products (id, num, consumer_price, price) VALUES (1, 305, 100000, 70000)")
    conn.commit()
    conn.close()

    monkeypatch.setenv("OBS_DB_PATH", str(obs_db))
    admin_c = mall_app.app.test_client()
    csrf = _login_client(admin_c, ADMIN_EMAIL, ADMIN_PASS)
    res = admin_c.post("/api/admin/apply-discount", headers={"X-CSRF-Token": csrf}, json={"discount_pct": 20})
    assert res.status_code == 200
    data = res.get_json()
    assert data["obs_updated"] == 1

    conn2 = sqlite3.connect(str(obs_db))
    row = conn2.execute("SELECT price FROM products WHERE id = 1").fetchone()
    conn2.close()
    assert int(row[0]) == 80000


# ==============================================================================
# Playwright Desktop (1440x900) & Mobile (390x844) E2E Verification
# ==============================================================================


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_server_ready(base_url: str, timeout: float = 15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/", timeout=1.5) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"Mall server did not become ready at {base_url}")


def _attach_console_collector(page):
    errors = []

    def _on_console(msg):
        if msg.type == "error":
            errors.append(f"console.error: {msg.text}")

    def _on_pageerror(err):
        errors.append(f"pageerror: {err}")

    def _on_response(resp):
        if resp.status >= 400 and resp.url.startswith("http://127.0.0.1"):
            if "/api/" not in resp.url:
                errors.append(f"http {resp.status}: {resp.url}")

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)
    page.on("response", _on_response)
    return errors


def _assert_layout_clean(page):
    overflow = page.evaluate(
        """() => ({
            scrollWidth: document.documentElement.scrollWidth,
            innerWidth: window.innerWidth
        })"""
    )
    assert overflow["scrollWidth"] <= overflow["innerWidth"] + 1, f"Horizontal overflow detected: {overflow}"

    internal_scroll = page.evaluate(
        """() => {
            const bad = [];
            for (const el of document.querySelectorAll('*')) {
                if (el.closest('.ax-dialog-card, .ax-dialog-body, .ax-lightbox-card')) continue;
                const st = getComputedStyle(el);
                if ((st.overflowY === 'auto' || st.overflowY === 'scroll') && st.maxHeight && st.maxHeight !== 'none') {
                    bad.push({ tag: el.tagName, id: el.id || '', cls: el.className || '', maxHeight: st.maxHeight });
                }
            }
            return bad;
        }"""
    )
    assert len(internal_scroll) == 0, f"Forbidden internal vertical scroll found: {internal_scroll}"


@pytest.fixture()
def live_mall_server(tmp_path):
    import mall_database

    db_file = tmp_path / "m3_e2e.db"
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["MALL_DB_PATH"] = str(db_file)
    env["OBS_PULL_ENABLED"] = "0"
    env["MALL_MODE"] = "cloud"
    env["MALL_SCHEDULER_ENABLED"] = "0"
    env["MALL_ADMIN_EMAIL"] = ADMIN_EMAIL
    env["MALL_ADMIN_PASSWORD"] = ADMIN_PASS
    env["PYTHONIOENCODING"] = "utf-8"

    mall_database.set_db_path(str(db_file), init=True)
    mall_database.ensure_default_mall_admin()
    _seed_product(num=601, name="M3 E2E 테스트 자켓", price=29000, stock=20)
    _create_member("m3e2e@test.com", name="E2E고객", phone="010-7777-8888")
    _create_member("m3other@test.com", name="타인회원", phone="010-9999-0000")

    log_path = tmp_path / "mall_m3_server.log"
    log_f = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(MALL_DIR / "app.py")],
        cwd=str(MALL_DIR),
        env={**env, "MALL_PORT": str(port)},
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_server_ready(base_url)
        yield base_url, db_file
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        log_f.close()


def test_m3_e2e_fix1_f10_f11_f12_desktop_and_mobile(live_mall_server):
    base_url, _db_file = live_mall_server

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            # 1. Admin configures shipping_fee=0, free_shipping_threshold=50000 (for FIX-1)
            adm_ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            adm_page = adm_ctx.new_page()
            adm_errors = _attach_console_collector(adm_page)
            adm_page.goto(f"{base_url}/auth/login?next=/admin", wait_until="domcontentloaded")
            adm_page.fill('input[name="email"]', ADMIN_EMAIL)
            adm_page.fill('input[name="password"]', ADMIN_PASS)
            adm_page.click('button[type="submit"]')
            adm_page.wait_for_url(f"{base_url}/admin**")
            adm_page.click("#adminTabStore")
            adm_page.fill("#asShippingFee", "0")
            adm_page.fill("#asFreeShippingThreshold", "50000")
            with adm_page.expect_response(lambda r: r.url.endswith("/api/admin/store-info") and r.request.method == "POST"):
                adm_page.click("#btnSaveAdminStoreInfo")

            # 2. Customer context (Desktop 1440x900)
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            errors = _attach_console_collector(page)
            page.route("**/t1.daumcdn.net/**", lambda route: route.abort())

            # Login as member
            page.goto(f"{base_url}/auth/login?next=/product/601", wait_until="domcontentloaded")
            page.fill('input[name="email"]', "m3e2e@test.com")
            page.fill('input[name="password"]', "MemberPass123!")
            page.click('button[type="submit"]')
            page.wait_for_url(f"{base_url}/product/601**")

            # FIX-1 check on /cart: subtotal 29000 < 50000, shipping_fee=0 -> "무료배송 적용 (0원)"
            page.locator('button[onclick="pdAddToCart()"]').first.click()
            page.goto(f"{base_url}/cart", wait_until="domcontentloaded")
            _assert_layout_clean(page)
            guide_txt = page.locator("#cartShippingGuide").inner_text()
            assert "무료배송 적용 (0원)" in guide_txt, f"FIX-1 failed: {guide_txt}"
            assert "더 담으면 무료배송" not in guide_txt
            guide_obj = page.evaluate("() => window.getMallShippingGuide(29000)")
            assert guide_obj["remaining_for_free"] == 0 and guide_obj["is_free"] is True

            # F10 Checkout with depositor_name & cash_receipt_type=INCOME -> /order-complete/<order_code>
            page.click("#btnGoCheckout")
            page.wait_for_url(f"{base_url}/checkout**")
            _assert_layout_clean(page)
            page.fill("#coDepositorName", "김이체")
            page.select_option("#coCashReceiptType", "INCOME")
            page.fill("#coCashReceiptNo", "010-7777-8888")
            page.click("#coSubmitBtn")
            page.wait_for_url(f"{base_url}/order-complete/ORD-**")
            _assert_layout_clean(page)
            oc_text = page.locator("main").inner_text()
            assert "김이체" in oc_text
            assert "010-7777-8888" in oc_text
            page.click("#btnCopyOrderCode")
            page.wait_for_selector("#mallToastContainer .ax-toast")

            # F11 Product Q&A (secret post) on /product/601
            page.goto(f"{base_url}/product/601", wait_until="domcontentloaded")
            _assert_layout_clean(page)
            page.fill("#pdQnaTitle", "비밀글 E2E 문의")
            page.fill("#pdQnaContent", "비밀 E2E 상세 내용 5544")
            page.check("#pdQnaSecret")
            with page.expect_response(lambda r: r.url.endswith("/api/products/601/qna") and r.request.method == "POST"):
                page.click("#pdQnaSubmitBtn")
            page.wait_for_selector('#pdQnaList:has-text("비밀 E2E 상세 내용 5544")')

            # F11 /cs 1:1 Inquiry submit
            page.goto(f"{base_url}/cs", wait_until="domcontentloaded")
            _assert_layout_clean(page)
            page.fill("#csInquiryTitle", "E2E 1:1 문의 제목")
            page.fill("#csInquiryContent", "E2E 1:1 문의 본문입니다")
            with page.expect_response(lambda r: r.url.endswith("/api/inquiries/create") and r.request.method == "POST"):
                page.click("#csInquirySubmitBtn")
            page.wait_for_selector('#csMyInquiriesList:has-text("E2E 1:1 문의 제목")')

            # Admin handles Q&A, Inquiry, Notice, FAQ, and views Notification Logs
            adm_page.click("#adminTabCs")
            adm_page.wait_for_selector('#adminQnaList .js-admin-qna-answer-input')
            adm_page.locator("#adminQnaList .js-admin-qna-answer-input").first.fill("관리자 Q&A 답변 8877")
            adm_page.locator("#adminQnaList .js-admin-qna-answer-btn").first.click()
            adm_page.wait_for_selector("#mallToastContainer .ax-toast")

            adm_page.click('.js-admin-cs-subtab[data-cs-subtab="inquiries"]')
            adm_page.wait_for_selector('#adminInquiriesList .js-admin-inq-answer-input')
            adm_page.locator("#adminInquiriesList .js-admin-inq-answer-input").first.fill("1:1 답변 완료")
            adm_page.locator("#adminInquiriesList .js-admin-inq-answer-btn").first.click()

            adm_page.click('.js-admin-cs-subtab[data-cs-subtab="notices"]')
            adm_page.fill("#adminNoticeTitle", "E2E 공지사항")
            adm_page.fill("#adminNoticeContent", "공지사항 본문입니다")
            adm_page.click("#btnAdminNoticeSave")
            adm_page.wait_for_selector('#adminNoticesTbody:has-text("E2E 공지사항")')

            adm_page.click('.js-admin-cs-subtab[data-cs-subtab="faqs"]')
            adm_page.fill("#adminFaqQuestion", "E2E FAQ 질문?")
            adm_page.fill("#adminFaqAnswer", "E2E FAQ 답변입니다.")
            adm_page.click("#btnAdminFaqSave")
            adm_page.wait_for_selector('#adminFaqsTbody:has-text("E2E FAQ 질문?")')

            adm_page.click("#adminTabNotifications")
            adm_page.wait_for_selector('#adminNotificationsTbody:has-text("ORDER_CREATED")')
            _assert_layout_clean(adm_page)

            # Another logged-out/other viewer on /product/601 cannot see secret Q&A content; /cs shows Notice & FAQ
            other_ctx = browser.new_context(viewport={"width": 390, "height": 844})
            other_page = other_ctx.new_page()
            other_errors = _attach_console_collector(other_page)
            other_page.goto(f"{base_url}/product/601", wait_until="domcontentloaded")
            _assert_layout_clean(other_page)
            qna_txt = other_page.locator("#pdQnaList").inner_text()
            assert "5544" not in qna_txt
            assert "8877" not in qna_txt
            assert "비밀글입니다." in qna_txt

            other_page.goto(f"{base_url}/cs", wait_until="domcontentloaded")
            _assert_layout_clean(other_page)
            assert "E2E 공지사항" in other_page.locator("#csNoticesList").inner_text()
            assert "E2E FAQ 질문?" in other_page.locator("#csFaqList").inner_text()
            other_page.locator("#csFaqList summary").first.click()
            assert "E2E FAQ 답변입니다." in other_page.locator("#csFaqList").inner_text()

            # Mobile viewport check on /order-complete & /orders for buyer
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(f"{base_url}/orders", wait_until="domcontentloaded")
            _assert_layout_clean(page)
            assert "김이체" in page.locator("main").inner_text()
            page.locator(".ax-order-card-head .ax-code-chip").first.click()
            page.wait_for_url(f"{base_url}/order-complete/ORD-**")
            _assert_layout_clean(page)

            assert errors == [], f"Buyer console/http errors: {errors}"
            assert adm_errors == [], f"Admin console/http errors: {adm_errors}"
            assert other_errors == [], f"Mobile other console/http errors: {other_errors}"
        finally:
            browser.close()
