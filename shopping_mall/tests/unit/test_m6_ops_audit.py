# -*- coding: utf-8 -*-
"""
Milestone 6 (M6) Unit, Concurrency, Security & Playwright UI Tests:
- F18: Auto-Cancel Expired Unpaid Orders + Stock/OBS/Coupon/Points Restoration + Notifications
- F18: SQLite Online Backup API + Rotation + Download & Path-Traversal Defense
- F18: Admin Audit Logs (mall_audit_logs) + Secret Redaction + 32 Mutation Endpoints Wiring + Pagination/Filters
- F18: Scheduler Lifecycle (MALL_SCHEDULER_ENABLED, wait-first loop, idempotent start/stop)
- M4/M5 Alignments: Q&A "question" alias + 40-char auto-title, >19-digit negative quantity, >4300-digit leading-zero ints
- Playwright Desktop (1440x900) & Mobile (390x844): #adminTabOps / #adminPanelOps checks
"""

import json
import os
import sqlite3
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

MALL_DIR = Path(__file__).resolve().parents[2]
if str(MALL_DIR) not in sys.path:
    sys.path.insert(0, str(MALL_DIR))

ADMIN_EMAIL = "m6admin@golabang.test"
ADMIN_PASS = "M6AdminPass!2026"


@pytest.fixture(scope="session", autouse=True)
def _bootstrap_isolated_env(tmp_path_factory):
    boot_dir = tmp_path_factory.mktemp("m6_boot")
    os.environ["MALL_DB_PATH"] = str(boot_dir / "boot_m6.db")
    os.environ["MALL_BACKUP_DIR"] = str(boot_dir / "boot_backups")
    os.environ["OBS_PULL_ENABLED"] = "0"
    os.environ["MALL_MODE"] = "cloud"
    os.environ["MALL_SCHEDULER_ENABLED"] = "0"
    os.environ["MALL_ADMIN_EMAIL"] = ADMIN_EMAIL
    os.environ["MALL_ADMIN_PASSWORD"] = ADMIN_PASS

    import mall_database  # noqa: F401
    import mall_security  # noqa: F401
    import mall_promotions  # noqa: F401
    import mall_notifications  # noqa: F401
    import mall_ops  # noqa: F401
    import app as mall_app  # noqa: F401
    return boot_dir


@pytest.fixture()
def isolated_mall_db(tmp_path, monkeypatch):
    import mall_database
    import mall_ops
    import mall_security

    db_file = tmp_path / "m6_unit.db"
    backup_dir = tmp_path / "backups"
    monkeypatch.setenv("MALL_ADMIN_EMAIL", ADMIN_EMAIL)
    monkeypatch.setenv("MALL_ADMIN_PASSWORD", ADMIN_PASS)
    monkeypatch.setenv("MALL_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("OBS_PULL_ENABLED", "0")
    monkeypatch.setenv("MALL_MODE", "cloud")
    monkeypatch.setenv("MALL_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("MALL_ALLOW_TEST_BACKDATE", "1")
    mall_ops.stop_maintenance_scheduler(timeout_sec=1.0)
    mall_security.rate_limiter._hits.clear()
    mall_database.set_db_path(str(db_file), init=True)
    mall_database.ensure_default_mall_admin()
    return db_file, backup_dir


def _seed_product(num: int, name: str, price: int = 25000, stock: int = 5):
    import mall_database

    return mall_database.upsert_product_from_obs(
        {
            "obs_product_id": num + 6000,
            "num": num,
            "name": name,
            "category": "TOP",
            "price": price,
            "live_price": price - 3000,
            "consumer_price": price + 10000,
            "image_url": "",
            "specs": f"{name} 스펙",
            "options": [{"color": "블랙", "size": "M", "stock": stock}],
        }
    )


def _create_member(email: str, password: str = "MemberPass123!", name: str = "김육차", phone: str = "010-6666-7777"):
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


def _set_user_points(user_id: int, balance: int) -> None:
    import mall_database

    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            conn.execute("UPDATE mall_users SET points_balance = ? WHERE id = ?", (int(balance), int(user_id)))
            conn.commit()
        finally:
            conn.close()


def _login_client(client, email: str, password: str) -> str:
    res = client.post("/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.get_json()
    return res.get_json()["csrf_token"]


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_server(url: str, timeout: float = 12.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.15)
    raise RuntimeError(f"Test server did not become ready: {url}")


# ==============================================================================
# 1. Auto-Cancel Expired Unpaid Orders + Stock/Coupon/Points/Notification/Audit
# ==============================================================================


def test_f18_auto_cancel_restores_stock_coupon_points_and_logs(isolated_mall_db):
    import mall_database
    import mall_notifications
    import mall_ops
    import mall_promotions

    _seed_product(601, "자동취소 테스트 셔츠", price=25000, stock=5)
    member = _create_member("m6_autocancel@golabang.test")
    uid = int(member["id"])

    mall_database.set_mall_store_info({"unpaid_cancel_hours": 24, "shipping_fee": 0, "free_shipping_threshold": 0})
    _set_user_points(uid, 1000)
    c_res = mall_promotions.save_admin_coupon(
        {
            "code": "M6SAVE3000",
            "name": "M6 3000원 할인",
            "discount_type": "FIXED",
            "discount_value": 3000,
            "min_order_amount": 10000,
            "usage_limit": 5,
            "is_active": True,
        }
    )
    assert c_res["ok"]

    # 1) Expired unpaid order with coupon + 500 points + qty 2 (stock 5 -> 3)
    o1 = mall_database.create_mall_order(
        {
            "buyer_name": member["name"],
            "buyer_phone": member["phone"],
            "address": "서울시 강남구 테헤란로 152",
            "payment_method": "BANK",
            "user_id": uid,
            "coupon_code": "M6SAVE3000",
            "points_used": 500,
            "items": [{"num": 601, "color": "블랙", "size": "M", "quantity": 2}],
        }
    )
    assert o1["ok"], o1
    o1_code = o1["order_code"]

    # 2) Fresh unpaid order qty 1 (stock 3 -> 2)
    o2 = mall_database.create_mall_order(
        {
            "buyer_name": "신규미입금",
            "buyer_phone": "010-2222-3333",
            "address": "서울시 서초구",
            "payment_method": "BANK",
            "items": [{"num": 601, "color": "블랙", "size": "M", "quantity": 1}],
        }
    )
    assert o2["ok"], o2
    o2_code = o2["order_code"]

    # 3) Expired paid order qty 1 (stock 2 -> 1) -> must NOT be cancelled
    o3 = mall_database.create_mall_order(
        {
            "buyer_name": "기한경과입금완료",
            "buyer_phone": "010-4444-5555",
            "address": "서울시 송파구",
            "payment_method": "BANK",
            "items": [{"num": 601, "color": "블랙", "size": "M", "quantity": 1}],
        }
    )
    assert o3["ok"], o3
    o3_id = o3["order_id"]
    o3_code = o3["order_code"]
    mall_database.update_mall_order_workflow(o3_id, payment_status="입금완료", order_status="상품준비중")

    # Backdate o1 and o3 by 30 hours directly
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            from datetime import datetime, timedelta

            old_ts = (datetime.now() - timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("UPDATE mall_orders SET created_at = ? WHERE order_code IN (?, ?)", (old_ts, o1_code, o3_code))
            conn.commit()
        finally:
            conn.close()

    assert mall_promotions.get_user_points_summary(uid)["balance"] == 500
    assert mall_database.get_mall_product_by_num(601)["options"][0]["stock"] == 1

    # Run auto-cancel
    res1 = mall_ops.cancel_expired_unpaid_orders()
    assert res1["ok"] is True
    assert res1["cancelled_count"] == 1
    assert res1["cancelled_orders"] == [o1_code]

    # Verify o1 cancelled, o2 & o3 untouched
    o1_after = mall_database.get_mall_order_raw_by_code_or_id(order_code=o1_code)
    o2_after = mall_database.get_mall_order_raw_by_code_or_id(order_code=o2_code)
    o3_after = mall_database.get_mall_order_raw_by_code_or_id(order_code=o3_code)
    assert o1_after["order_status"] == "주문취소"
    assert "미입금 자동 취소" in o1_after["claim_reason"]
    assert o1_after["stock_restored"] == 1
    assert o2_after["order_status"] == "결제대기"
    assert o3_after["order_status"] == "상품준비중"
    assert o3_after["payment_status"] == "입금완료"

    # Stock restored (1 + 2 = 3)
    assert mall_database.get_mall_product_by_num(601)["options"][0]["stock"] == 3
    # Points refunded (500 + 500 = 1000)
    assert mall_promotions.get_user_points_summary(uid)["balance"] == 1000
    # Coupon restored (used_count -> 0, user coupon AVAILABLE)
    coupons = mall_promotions.list_admin_coupons()
    c_row = next(c for c in coupons if c["code"] == "M6SAVE3000")
    assert c_row["used_count"] == 0
    u_coupons = mall_promotions.list_user_coupons(uid)
    assert any(uc["code"] == "M6SAVE3000" and uc["status"] == "AVAILABLE" for uc in u_coupons)

    # Notification log recorded
    n_logs = mall_notifications.list_notification_logs(event_type="ORDER_AUTO_CANCELLED")
    assert any(l["ref_code"] == o1_code for l in n_logs)

    # Audit log recorded
    a_res = mall_ops.list_admin_audit_logs(action="AUTO_CANCEL_UNPAID_ORDERS")
    assert a_res["total_count"] >= 1
    assert any(o1_code in json.dumps(row, ensure_ascii=False) for row in a_res["logs"])

    # Second run is strictly idempotent
    res2 = mall_ops.cancel_expired_unpaid_orders()
    assert res2["cancelled_count"] == 0
    assert mall_database.get_mall_product_by_num(601)["options"][0]["stock"] == 3
    assert mall_promotions.get_user_points_summary(uid)["balance"] == 1000


def test_f18_unpaid_cancel_hours_zero_disables_autocancel(isolated_mall_db):
    import app as mall_app
    import mall_database

    _seed_product(602, "비활성 테스트 상품", price=20000, stock=4)
    o = mall_database.create_mall_order(
        {
            "buyer_name": "비활성테스트",
            "buyer_phone": "010-1234-5678",
            "address": "서울시",
            "payment_method": "BANK",
            "items": [{"num": 602, "color": "블랙", "size": "M", "quantity": 1}],
        }
    )
    assert o["ok"], o
    ocode = o["order_code"]

    with mall_app.app.test_client() as client:
        csrf = _login_client(client, ADMIN_EMAIL, ADMIN_PASS)
        r_set = client.post(
            "/api/admin/store-info",
            headers={"X-CSRF-Token": csrf},
            json={"unpaid_cancel_hours": 0},
        )
        assert r_set.status_code == 200

        r_maint = client.post(
            "/api/admin/system/run-maintenance",
            headers={"X-CSRF-Token": csrf},
            json={"backdate_order_code": ocode, "backdate_hours": 100},
        )
        assert r_maint.status_code == 200
        j = r_maint.get_json()
        assert j["enabled"] is False
        assert j["cancelled_count"] == 0

    ord_after = mall_database.get_mall_order_raw_by_code_or_id(order_code=ocode)
    assert ord_after["order_status"] == "결제대기"
    assert mall_database.get_mall_product_by_num(602)["options"][0]["stock"] == 3


# ==============================================================================
# 2. Backdate Gate & RBAC & CSRF Enforcement
# ==============================================================================


def test_f18_backdate_gate_and_rbac_csrf(isolated_mall_db, monkeypatch):
    import app as mall_app
    import mall_database

    _seed_product(603, "게이트 테스트 상품", price=15000, stock=5)
    o = mall_database.create_mall_order(
        {
            "buyer_name": "백데이트차단",
            "buyer_phone": "010-9999-0000",
            "address": "서울시",
            "payment_method": "BANK",
            "items": [{"num": 603, "color": "블랙", "size": "M", "quantity": 1}],
        }
    )
    assert o["ok"], o
    ocode = o["order_code"]
    _create_member("m6_member@golabang.test")

    # Guest -> 401
    with mall_app.app.test_client() as guest:
        assert guest.get("/api/admin/audit-logs").status_code == 401
        assert guest.get("/api/admin/system/backups").status_code == 401
        assert guest.get("/api/admin/system/backup/sample.db").status_code == 401
        assert guest.post("/api/admin/system/backup", json={}).status_code == 401
        assert guest.post("/api/admin/system/run-maintenance", json={}).status_code == 401

    # Member -> 403
    with mall_app.app.test_client() as member_c:
        m_csrf = _login_client(member_c, "m6_member@golabang.test", "MemberPass123!")
        assert member_c.get("/api/admin/audit-logs").status_code == 403
        assert member_c.get("/api/admin/system/backups").status_code == 403
        assert member_c.get("/api/admin/system/backup/sample.db").status_code == 403
        assert member_c.post("/api/admin/system/backup", headers={"X-CSRF-Token": m_csrf}, json={}).status_code == 403
        assert member_c.post("/api/admin/system/run-maintenance", headers={"X-CSRF-Token": m_csrf}, json={}).status_code == 403

    # Admin without CSRF -> 403; with MALL_ALLOW_TEST_BACKDATE=0 -> 403 BACKDATE_DISABLED
    monkeypatch.setenv("MALL_ALLOW_TEST_BACKDATE", "0")
    with mall_app.app.test_client() as admin_c:
        csrf = _login_client(admin_c, ADMIN_EMAIL, ADMIN_PASS)
        assert admin_c.post("/api/admin/system/backup", json={}).status_code == 403
        assert admin_c.post("/api/admin/system/run-maintenance", json={}).status_code == 403

        r1 = admin_c.post(
            "/api/admin/system/run-maintenance",
            headers={"X-CSRF-Token": csrf},
            json={"backdate_orders": [{"order_code": ocode, "hours": 100}]},
        )
        assert r1.status_code == 403
        assert r1.get_json()["code"] == "BACKDATE_DISABLED"

        r2 = admin_c.post(
            "/api/admin/system/run-maintenance",
            headers={"X-CSRF-Token": csrf},
            json={"backdate_order_code": ocode, "backdate_hours": 100},
        )
        assert r2.status_code == 403

        # Malformed backdate_orders or zero/false backdate_hours while disabled must still fail closed with 403 (never 200)
        for bad_orders in ("abc", {}, 123):
            r_mal = admin_c.post(
                "/api/admin/system/run-maintenance",
                headers={"X-CSRF-Token": csrf},
                json={"backdate_orders": bad_orders},
            )
            assert r_mal.status_code == 403, f"{bad_orders!r} -> {r_mal.status_code}"
            assert r_mal.get_json()["code"] == "BACKDATE_DISABLED"

        for zero_hrs in (0, "0", False):
            r_zero = admin_c.post(
                "/api/admin/system/run-maintenance",
                headers={"X-CSRF-Token": csrf},
                json={"backdate_hours": zero_hrs},
            )
            assert r_zero.status_code == 403, f"disabled backdate_hours={zero_hrs!r} -> {r_zero.status_code}"
            assert r_zero.get_json()["code"] == "BACKDATE_DISABLED"

        # When MALL_ALLOW_TEST_BACKDATE=1, malformed types / overflow / non-finite / out-of-range hours -> 400 JSON
        monkeypatch.setenv("MALL_ALLOW_TEST_BACKDATE", "1")
        r_bad_type = admin_c.post(
            "/api/admin/system/run-maintenance",
            headers={"X-CSRF-Token": csrf},
            json={"backdate_orders": "abc"},
        )
        assert r_bad_type.status_code == 400
        assert r_bad_type.get_json()["ok"] is False

        for bad_hrs in ("Infinity", "-Infinity", "NaN", "9" * 5000, 1e12, 87601, 0, "0", -5, True, False):
            r_top = admin_c.post(
                "/api/admin/system/run-maintenance",
                headers={"X-CSRF-Token": csrf},
                json={"backdate_order_code": ocode, "backdate_hours": bad_hrs},
            )
            assert r_top.status_code == 400, f"top backdate_hours={bad_hrs!r} -> {r_top.status_code}"
            assert r_top.get_json()["ok"] is False
            assert r_top.get_json()["status"] == "error"

            r_top_only = admin_c.post(
                "/api/admin/system/run-maintenance",
                headers={"X-CSRF-Token": csrf},
                json={"backdate_hours": bad_hrs},
            )
            assert r_top_only.status_code == 400, f"solo backdate_hours={bad_hrs!r} -> {r_top_only.status_code}"
            assert r_top_only.get_json()["ok"] is False
            assert r_top_only.get_json()["status"] == "error"

            r_list = admin_c.post(
                "/api/admin/system/run-maintenance",
                headers={"X-CSRF-Token": csrf},
                json={"backdate_orders": [{"order_code": ocode, "hours": bad_hrs}]},
            )
            assert r_list.status_code == 400, f"entry hours={bad_hrs!r} -> {r_list.status_code}"
            assert r_list.get_json()["ok"] is False
            assert r_list.get_json()["status"] == "error"

    ord_after = mall_database.get_mall_order_raw_by_code_or_id(order_code=ocode)
    assert ord_after["order_status"] == "결제대기"


# ==============================================================================
# 3. Concurrency Race: Auto-Cancel vs Self-Cancel & Payment Confirm
# ==============================================================================


def test_f18_auto_cancel_vs_self_cancel_and_payment_concurrency(isolated_mall_db):
    import mall_database
    import mall_ops
    import mall_promotions

    _seed_product(604, "동시성 테스트 상품", price=30000, stock=5)
    member = _create_member("m6_race@golabang.test")
    uid = int(member["id"])
    mall_database.set_mall_store_info({"unpaid_cancel_hours": 24, "shipping_fee": 0, "free_shipping_threshold": 0})
    _set_user_points(uid, 500)
    mall_promotions.save_admin_coupon(
        {
            "code": "M6RACE",
            "name": "레이스 쿠폰",
            "discount_type": "FIXED",
            "discount_value": 2000,
            "is_active": True,
        }
    )

    o = mall_database.create_mall_order(
        {
            "buyer_name": member["name"],
            "buyer_phone": member["phone"],
            "address": "서울시",
            "payment_method": "BANK",
            "user_id": uid,
            "coupon_code": "M6RACE",
            "points_used": 300,
            "items": [{"num": 604, "color": "블랙", "size": "M", "quantity": 2}],
        }
    )
    assert o["ok"], o
    ocode = o["order_code"]
    oid = o["order_id"]

    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            from datetime import datetime, timedelta

            old_ts = (datetime.now() - timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("UPDATE mall_orders SET created_at = ? WHERE id = ?", (old_ts, oid))
            conn.commit()
        finally:
            conn.close()

    barrier = threading.Barrier(2)
    results = {}

    def run_auto():
        barrier.wait()
        results["auto"] = mall_ops.cancel_expired_unpaid_orders()

    def run_self():
        barrier.wait()
        results["self"] = mall_database.cancel_mall_order_by_customer(order_id=oid, claim_reason="고객 동시 취소")

    t1 = threading.Thread(target=run_auto)
    t2 = threading.Thread(target=run_self)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    # Stock must be restored AT MOST ONCE (5 - 2 + 2 = 5, never 7!)
    assert mall_database.get_mall_product_by_num(604)["options"][0]["stock"] == 5
    assert mall_promotions.get_user_points_summary(uid)["balance"] == 500
    c_row = next(c for c in mall_promotions.list_admin_coupons() if c["code"] == "M6RACE")
    assert c_row["used_count"] == 0
    assert mall_database.get_mall_order_raw_by_code_or_id(order_code=ocode)["stock_restored"] == 1

    # Race 2: Auto-cancel vs Admin Payment Confirm
    o2 = mall_database.create_mall_order(
        {
            "buyer_name": "입금레이스",
            "buyer_phone": "010-3333-4444",
            "address": "서울시",
            "payment_method": "BANK",
            "items": [{"num": 604, "color": "블랙", "size": "M", "quantity": 1}],
        }
    )
    assert o2["ok"], o2
    oid2 = o2["order_id"]
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            from datetime import datetime, timedelta

            old_ts = (datetime.now() - timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("UPDATE mall_orders SET created_at = ? WHERE id = ?", (old_ts, oid2))
            conn.commit()
        finally:
            conn.close()

    barrier2 = threading.Barrier(2)

    def run_auto2():
        barrier2.wait()
        mall_ops.cancel_expired_unpaid_orders()

    def run_pay2():
        barrier2.wait()
        mall_database.update_mall_order_workflow(oid2, payment_status="입금완료", order_status="상품준비중")

    t3 = threading.Thread(target=run_auto2)
    t4 = threading.Thread(target=run_pay2)
    t3.start()
    t4.start()
    t3.join(timeout=10)
    t4.join(timeout=10)

    final_o2 = mall_database.get_mall_order_raw_by_code_or_id(order_id=oid2)
    cur_stock = mall_database.get_mall_product_by_num(604)["options"][0]["stock"]
    if final_o2["order_status"] == "주문취소":
        assert final_o2["stock_restored"] == 1
        assert cur_stock == 5
    else:
        assert final_o2["payment_status"] == "입금완료"
        assert final_o2["stock_restored"] == 0
        assert cur_stock == 4


# ==============================================================================
# 4. SQLite Online Backup, Rotation, Download & Path Traversal Defense
# ==============================================================================


def test_f18_sqlite_online_backup_download_and_path_traversal(isolated_mall_db):
    import app as mall_app
    import mall_ops

    _, backup_dir = isolated_mall_db
    _seed_product(605, "백업 검증 상품", price=19000, stock=8)

    with mall_app.app.test_client() as client:
        csrf = _login_client(client, ADMIN_EMAIL, ADMIN_PASS)
        r_create = client.post("/api/admin/system/backup", headers={"X-CSRF-Token": csrf}, json={})
        assert r_create.status_code == 200, r_create.get_json()
        data = r_create.get_json()
        fname = data["filename"]
        assert fname.endswith(".db")
        assert (backup_dir / fname).is_file()

        # Verify SQLite integrity
        b_conn = sqlite3.connect(str(backup_dir / fname))
        try:
            chk = b_conn.execute("PRAGMA integrity_check").fetchone()[0]
            assert chk == "ok"
            p_cnt = b_conn.execute("SELECT COUNT(*) FROM mall_products WHERE num = 605").fetchone()[0]
            assert p_cnt == 1
        finally:
            b_conn.close()

        # List backups
        r_list = client.get("/api/admin/system/backups")
        assert r_list.status_code == 200
        assert any(b["filename"] == fname and b["size"] > 4096 for b in r_list.get_json()["backups"])

        # Download backup
        r_dl = client.get(f"/api/admin/system/backup/{fname}")
        assert r_dl.status_code == 200
        assert "attachment" in (r_dl.headers.get("Content-Disposition") or "").lower()
        assert r_dl.data.startswith(b"SQLite format 3\x00")
        assert len(r_dl.data) > 4096
        r_dl.close()

        # Path traversal & invalid filename rejection (never leak "Flask")
        for bad_path in (
            "../../app.py",
            "..%2F..%2Fapp.py",
            "foo/bar.db",
            "bad\\name.db",
            ".hidden.db",
            "nonexistent_backup.db",
        ):
            r_bad = client.get(f"/api/admin/system/backup/{bad_path}")
            assert r_bad.status_code in (400, 403, 404), f"{bad_path} -> {r_bad.status_code}"
            body_text = r_bad.get_data(as_text=True)
            assert "Flask" not in body_text

    # Rotation test: keep_last=3
    for i in range(4):
        time.sleep(0.02)
        mall_ops.backup_mall_database(keep_last=3)
    assert len(mall_ops.list_mall_backups()) == 3


def test_f18_backup_cleans_up_tmp_file_on_failure(isolated_mall_db, monkeypatch):
    import mall_ops

    _, backup_dir = isolated_mall_db

    def _fail_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(mall_ops.os, "replace", _fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        mall_ops.backup_mall_database()

    tmp_leftovers = list(backup_dir.glob("*.tmp")) if backup_dir.exists() else []
    assert tmp_leftovers == [], f"Leftover .tmp files found: {tmp_leftovers}"


# ==============================================================================
# 5. Audit Log Wiring, Filters, Pagination, Secret Redaction & Resilience
# ==============================================================================


def test_f18_audit_logs_wiring_filters_pagination_and_no_secrets(isolated_mall_db):
    import app as mall_app
    import mall_cs_service
    import mall_database
    import mall_ops

    _seed_product(606, "감사로그 상품", price=22000, stock=10)
    member = _create_member("m6_audit_member@golabang.test")
    uid = int(member["id"])

    # Create Q&A, Inquiry, Review to moderate/answer
    qna_res = mall_cs_service.create_product_qna(606, member, {"content": "감사로그 테스트 질문입니다."})
    qna_id = qna_res["item"]["id"]
    inq_res = mall_cs_service.create_inquiry(member, {"category": "배송", "title": "문의", "content": "배송 언제 되나요?"})
    inq_id = inq_res["item"]["id"]
    rev = mall_database.add_mall_review(606, member["name"], 5, "좋은 상품입니다.", user_id=uid)
    rev_id = rev["id"]

    # Direct redaction & non-crashing check
    redacted_id = mall_ops.log_admin_action(
        admin_user={"id": 1, "email": ADMIN_EMAIL, "name": "관리자"},
        action="SECURITY_CHECK",
        target_type="test",
        target_id="sec-1",
        detail_dict={
            "password": "PlainPassword123!",
            "nested": {"api_secret": "SuperSecretVal", "csrf_token": "tok-xyz", "safe_key": "ok"},
        },
        ip_addr="127.0.0.1",
    )
    assert redacted_id > 0

    with mall_app.app.test_client() as client:
        csrf = _login_client(client, ADMIN_EMAIL, ADMIN_PASS)
        hdrs = {"X-CSRF-Token": csrf}

        assert client.post(
            "/api/admin/products/add",
            headers=hdrs,
            json={"num": 607, "name": "신규감사상품", "category": "TOP", "price": 18000, "options": [{"color": "블랙", "size": "FREE", "stock": 5}]},
        ).status_code == 200
        assert client.post("/api/admin/store-info", headers=hdrs, json={"unpaid_cancel_hours": 48}).status_code == 200
        assert client.post(
            "/api/admin/coupons/save",
            headers=hdrs,
            json={"code": "AUDIT500", "name": "감사쿠폰", "discount_type": "FIXED", "discount_value": 500, "is_active": True},
        ).status_code == 200
        assert client.post("/api/admin/users/status", headers=hdrs, json={"user_id": uid, "is_active": 0}).status_code == 200
        assert client.post("/api/admin/qna/answer", headers=hdrs, json={"qna_id": qna_id, "answer_content": "답변 완료"}).status_code == 200
        assert client.post("/api/admin/inquiries/answer", headers=hdrs, json={"inquiry_id": inq_id, "answer_content": "오늘 발송됩니다."}).status_code == 200
        r_notice = client.post("/api/admin/notices/save", headers=hdrs, json={"title": "감사 공지", "content": "공지 본문입니다.", "is_pinned": True})
        assert r_notice.status_code == 200
        notice_id = r_notice.get_json()["id"]
        assert client.post("/api/admin/reviews/moderate", headers=hdrs, json={"review_id": rev_id, "action": "reply", "admin_reply": "감사합니다!"}).status_code == 200
        assert client.post("/api/admin/system/backup", headers=hdrs, json={}).status_code == 200
        assert client.post("/api/admin/system/run-maintenance", headers=hdrs, json={}).status_code == 200

        # Query all logs
        r_all = client.get("/api/admin/audit-logs?per_page=100")
        assert r_all.status_code == 200
        payload = r_all.get_json()
        assert payload["total_count"] >= 11
        dump = json.dumps(payload["logs"], ensure_ascii=False)
        assert ADMIN_EMAIL in dump
        assert "AUDIT500" in dump
        assert str(uid) in dump
        assert f'"qna_id": {qna_id}' in dump and "QNA" in dump
        assert f'"inquiry_id": {inq_id}' in dump and "INQUIR" in dump
        assert str(notice_id) in dump and "NOTICE" in dump
        assert str(rev_id) in dump and "REVIEW" in dump
        assert "BACKUP" in dump
        assert "MAINTENANCE" in dump
        assert "PlainPassword123!" not in dump
        assert "SuperSecretVal" not in dump
        assert "tok-xyz" not in dump
        assert "[REDACTED]" in dump

        # Category filter & search & pagination
        r_cs = client.get("/api/admin/audit-logs?action=CS&per_page=10")
        assert r_cs.status_code == 200
        cs_actions = {row["action"] for row in r_cs.get_json()["logs"]}
        assert {"QNA_ANSWER", "INQUIRY_ANSWER", "NOTICE_SAVE"}.issubset(cs_actions)

        r_q = client.get("/api/admin/audit-logs?q=AUDIT500")
        assert r_q.status_code == 200 and len(r_q.get_json()["logs"]) >= 1

        r_p1 = client.get("/api/admin/audit-logs?page=1&per_page=3")
        r_p2 = client.get("/api/admin/audit-logs?page=2&per_page=3")
        assert r_p1.status_code == 200 and r_p2.status_code == 200
        assert len(r_p1.get_json()["logs"]) == 3
        assert len(r_p2.get_json()["logs"]) == 3
        assert r_p1.get_json()["logs"][0]["id"] != r_p2.get_json()["logs"][0]["id"]

        # Huge page/limit strings must not 500
        assert client.get(f"/api/admin/audit-logs?page={'9'*5000}&limit={'0'*5000 + '5'}").status_code == 200


# ==============================================================================
# 6. Scheduler Lifecycle & Env Gate
# ==============================================================================


def test_f18_scheduler_env_gate_and_lifecycle(isolated_mall_db, monkeypatch):
    import app as mall_app
    import mall_ops

    monkeypatch.setenv("MALL_SCHEDULER_ENABLED", "0")
    assert mall_app.is_scheduler_enabled() is False
    assert mall_app.maybe_start_mall_scheduler() is False
    assert mall_ops.get_scheduler_status()["running"] is False

    monkeypatch.setenv("MALL_SCHEDULER_ENABLED", "1")
    monkeypatch.setenv("MALL_MAINTENANCE_INTERVAL_SEC", "60")
    assert mall_app.is_scheduler_enabled() is True
    assert mall_app.maybe_start_mall_scheduler() is True
    assert mall_ops.get_scheduler_status()["running"] is True
    # Idempotent second call does not spawn duplicate
    assert mall_ops.start_maintenance_scheduler() is False
    assert mall_ops.stop_maintenance_scheduler(timeout_sec=2.0) is True
    assert mall_ops.get_scheduler_status()["running"] is False


# ==============================================================================
# 7. M4/M5 Minor Alignments (Q&A "question" alias, >19-digit & >4300-digit ints)
# ==============================================================================


def test_m6_minor_alignments_qna_and_int_bounds(isolated_mall_db):
    import app as mall_app
    import mall_database
    import mall_promotions

    mall_database.upsert_product_from_obs(
        {
            "obs_product_id": 6608,
            "num": 608,
            "name": "정렬 테스트 상품",
            "category": "TOP",
            "price": 25000,
            "live_price": 22000,
            "consumer_price": 35000,
            "image_url": "",
            "specs": "정렬 테스트 스펙",
            "options": [
                {"color": "블랙", "size": "M", "stock": 5},
                {"color": "화이트", "size": "L", "stock": 0},
            ],
        }
    )
    member = _create_member("m6_align@golabang.test")
    sub1 = mall_promotions.subscribe_restock_alert(
        {"product_num": 608, "color": "화이트", "size": "L", "contact_type": "EMAIL", "contact_value": "r@golabang.test"},
        user=member,
    )
    assert sub1["ok"], sub1

    with mall_app.app.test_client() as client:
        m_csrf = _login_client(client, "m6_align@golabang.test", "MemberPass123!")

        # Q&A "question" alias + 40-char auto-title
        long_question = "가나다라마바사아자차카타파하가나다라마바사아자차카타파하가나다라마바사아자차카타파하12345"
        r_qna = client.post(
            "/api/products/608/qna",
            headers={"X-CSRF-Token": m_csrf},
            json={"question": long_question},
        )
        assert r_qna.status_code == 200, r_qna.get_json()
        qna_obj = r_qna.get_json()["qna"]
        assert qna_obj["content"] == long_question
        assert len(qna_obj["title"]) == 40
        assert qna_obj["title"] == long_question[:40]

        # Negative 5000-digit quantity must fail with 400 (not clamp to 1)
        r_neg = client.post(
            "/api/orders/create",
            headers={"X-CSRF-Token": m_csrf},
            json={
                "buyer_name": member["name"],
                "buyer_phone": member["phone"],
                "address": "서울시",
                "items": [{"num": 608, "color": "블랙", "size": "M", "quantity": "-" + ("9" * 5000)}],
            },
        )
        assert r_neg.status_code == 400

        # >4300-digit leading-zero expected_price & expected_items parse accurately -> 200
        r_lz = client.post(
            "/api/orders/create",
            headers={"X-CSRF-Token": m_csrf},
            json={
                "buyer_name": member["name"],
                "buyer_phone": member["phone"],
                "address": "서울시",
                "items": [{"num": 608, "color": "블랙", "size": "M", "quantity": 1, "expected_price": ("0" * 5000) + "25000"}],
                "expected_items": [
                    {"num": ("0" * 5000) + "608", "color": "블랙", "size": "M", "quantity": ("0" * 5000) + "1", "unit_price": ("0" * 5000) + "25000"}
                ],
            },
        )
        assert r_lz.status_code == 200, r_lz.get_json()

        # >19 significant digits in expected_price fails closed with 409
        r_big = client.post(
            "/api/orders/create",
            headers={"X-CSRF-Token": m_csrf},
            json={
                "buyer_name": member["name"],
                "buyer_phone": member["phone"],
                "address": "서울시",
                "items": [{"num": 608, "color": "블랙", "size": "M", "quantity": 1, "expected_price": "9" * 5000}],
            },
        )
        assert r_big.status_code == 409

    # Admin restock-alerts >4300-digit leading-zero limit ("0"*5000 + "1") clamps to 1
    sub2 = mall_promotions.subscribe_restock_alert(
        {"product_num": 608, "color": "화이트", "size": "L", "contact_type": "SMS", "contact_value": "010-1111-9999"}
    )
    assert sub2["ok"], sub2
    with mall_app.app.test_client() as admin_c:
        _login_client(admin_c, ADMIN_EMAIL, ADMIN_PASS)
        r_lim = admin_c.get(f"/api/admin/restock-alerts?limit={'0'*5000 + '1'}")
        assert r_lim.status_code == 200
        assert len(r_lim.get_json()["alerts"]) == 1


# ==============================================================================
# 8. Playwright Desktop (1440x900) & Mobile (390x844) Admin Ops Panel Checks
# ==============================================================================


def _assert_no_overflow_or_main_vscroll(page, label: str):
    dims = page.evaluate(
        """() => {
            const doc = document.documentElement;
            const issues = [];
            const els = document.querySelectorAll('body *');
            for (const el of els) {
                if (el.closest('.ax-dialog-card, .ax-dialog-body, .ax-lightbox-card')) continue;
                const cs = window.getComputedStyle(el);
                if (!cs || cs.display === 'none' || cs.visibility === 'hidden') continue;
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                if (cs.overflowY === 'auto' || cs.overflowY === 'scroll') {
                    const hasMax = cs.maxHeight && cs.maxHeight !== 'none';
                    if (hasMax || el.scrollHeight > el.clientHeight + 2) {
                        issues.push(el.tagName + '#' + (el.id || '') + '.' + (el.className || ''));
                    }
                }
            }
            return { sw: doc.scrollWidth, iw: window.innerWidth, issues };
        }"""
    )
    assert dims["sw"] <= dims["iw"], f"[{label}] horizontal overflow: {dims['sw']} > {dims['iw']}"
    assert dims["issues"] == [], f"[{label}] main internal vertical scroll detected: {dims['issues']}"


def test_m6_playwright_admin_ops_desktop_and_mobile(tmp_path):
    import mall_database
    import mall_ops

    db_file = tmp_path / "m6_pw.db"
    backup_dir = tmp_path / "pw_backups"
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["MALL_DB_PATH"] = str(db_file)
    env["MALL_BACKUP_DIR"] = str(backup_dir)
    env["OBS_PULL_ENABLED"] = "0"
    env["MALL_MODE"] = "cloud"
    env["MALL_SCHEDULER_ENABLED"] = "0"
    env["MALL_ADMIN_EMAIL"] = ADMIN_EMAIL
    env["MALL_ADMIN_PASSWORD"] = ADMIN_PASS

    mall_database.set_db_path(str(db_file), init=True)
    mall_database.ensure_default_mall_admin()
    _seed_product(609, "PW 운영 테스트 상품", price=29000, stock=5)
    # Seed XSS payload in audit log to ensure safe HTML escaping in #adminAuditLogsTbody
    mall_ops.log_admin_action(
        admin_user={"id": 1, "email": ADMIN_EMAIL, "name": "관리자"},
        action="PRODUCT_UPDATE",
        target_type="product",
        target_id="609",
        detail_dict={"note": '<img src=x onerror="window.__m6XssHit=1">'},
        ip_addr="127.0.0.1",
    )

    server_code = (
        f"import sys; sys.path.insert(0, r'{MALL_DIR}'); "
        f"import app; app.app.run(host='127.0.0.1', port={port}, debug=False, use_reloader=False, threaded=True)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", server_code],
        env=env,
        cwd=str(MALL_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_server(f"{base_url}/")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(viewport={"width": 1440, "height": 900})
                page = ctx.new_page()
                console_errs = []
                page_errs = []
                bad_resps = []
                page.on("console", lambda m: console_errs.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: page_errs.append(str(e)))
                page.on("response", lambda r: bad_resps.append((r.status, r.url)) if r.status >= 400 else None)

                # Login as admin
                page.goto(f"{base_url}/auth/login?next=/admin", wait_until="networkidle")
                page.fill("#loginEmail", ADMIN_EMAIL)
                page.fill("#loginPassword", ADMIN_PASS)
                with page.expect_navigation(wait_until="networkidle"):
                    page.click("button[type='submit']")
                assert "/admin" in page.url

                # Click Ops Tab
                page.click("#adminTabOps")
                page.wait_for_selector("#adminPanelOps:not(.ax-is-hidden)")
                page.wait_for_timeout(200)
                _assert_no_overflow_or_main_vscroll(page, "desktop-admin-ops-initial")
                assert page.evaluate("Boolean(window.__m6XssHit)") is False

                # Trigger backup creation via UI button
                page.click("#btnCreateDbBackup")
                page.wait_for_timeout(300)
                assert page.locator("#adminBackupsTbody tr").count() >= 1
                assert page.locator("#adminBackupsTbody a[href^='/api/admin/system/backup/']").count() >= 1

                # Trigger maintenance via UI button
                page.click("#btnRunMaintenanceNow")
                page.wait_for_timeout(300)
                assert page.locator("#adminOpsMaintenanceResult").is_visible()

                # Filter audit logs
                page.select_option("#adminAuditActionFilter", "SYSTEM")
                page.wait_for_timeout(250)
                assert "SYSTEM_" in page.locator("#adminAuditLogsTbody").inner_text()

                page.select_option("#adminAuditActionFilter", "ALL")
                page.wait_for_timeout(250)
                _assert_no_overflow_or_main_vscroll(page, "desktop-admin-ops-after")

                # Mobile viewport (390x844)
                page.set_viewport_size({"width": 390, "height": 844})
                page.wait_for_timeout(200)
                _assert_no_overflow_or_main_vscroll(page, "mobile-admin-ops")

                assert console_errs == [], f"Console errors: {console_errs}"
                assert page_errs == [], f"Page errors: {page_errs}"
                assert bad_resps == [], f"HTTP >= 400 responses: {bad_resps}"
            finally:
                browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
