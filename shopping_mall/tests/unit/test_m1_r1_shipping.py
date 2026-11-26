# -*- coding: utf-8 -*-
"""
Milestone 1 (M1) — R1 Fixes & Dynamic Shipping / Postcode Foundation Tests
Covers:
- set_db_path() isolation & MALL_SCHEDULER_ENABLED hook
- mall_shipping.calculate_shipping_fee() edge cases
- DEFAULT_STORE_INFO & /api/admin/store-info auth/CSRF/validation/0-preservation
- create_mall_order dynamic shipping + zipcode + guest 3-factor + legacy NULL shipping_fee
- get_related_mall_products() ranking & regular price only
- GET/POST /auth/register zipcode prefill/persistence + mocked OBS webhook zipcode
- /api/live/state-lite < 35% payload ratio
- Playwright desktop (1440x900) & mobile (390x844) E2E checks for F1-F5
"""

import json
import os
import socket
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

ADMIN_EMAIL = "m1admin@golabang.test"
ADMIN_PASS = "M1AdminPass!2026"


@pytest.fixture(scope="session", autouse=True)
def _bootstrap_isolated_env(tmp_path_factory):
    """모듈 최초 임포트 전부터 프로덕션 DB(shopping_mall.db) 접근을 완전히 차단합니다."""
    boot_dir = tmp_path_factory.mktemp("m1_boot")
    os.environ["MALL_DB_PATH"] = str(boot_dir / "boot_isolated.db")
    os.environ["OBS_PULL_ENABLED"] = "0"
    os.environ["MALL_MODE"] = "cloud"
    os.environ["MALL_SCHEDULER_ENABLED"] = "0"
    os.environ["MALL_ADMIN_EMAIL"] = ADMIN_EMAIL
    os.environ["MALL_ADMIN_PASSWORD"] = ADMIN_PASS

    import mall_database  # noqa: F401
    import mall_security  # noqa: F401
    import mall_shipping  # noqa: F401
    import app as mall_app  # noqa: F401
    return boot_dir


@pytest.fixture()
def isolated_mall_db(tmp_path, monkeypatch):
    import mall_database
    import mall_security

    db_file = tmp_path / "m1_unit.db"
    monkeypatch.setenv("MALL_ADMIN_EMAIL", ADMIN_EMAIL)
    monkeypatch.setenv("MALL_ADMIN_PASSWORD", ADMIN_PASS)
    monkeypatch.setenv("OBS_PULL_ENABLED", "0")
    monkeypatch.setenv("MALL_MODE", "cloud")
    monkeypatch.setenv("MALL_SCHEDULER_ENABLED", "0")
    mall_security.rate_limiter._hits.clear()
    mall_database.set_db_path(str(db_file), init=True)
    mall_database.ensure_default_mall_admin()
    return db_file


def _seed_product(
    num: int,
    name: str,
    category: str = "TOP",
    price: int = 25000,
    live_price: int = 19000,
    consumer_price: int = 35000,
    stock: int = 10,
):
    import mall_database

    mall_database.upsert_product_from_obs(
        {
            "obs_product_id": num + 1000,
            "num": num,
            "name": name,
            "category": category,
            "price": price,
            "live_price": live_price,
            "consumer_price": consumer_price,
            "image_url": "",
            "specs": f"{name} 상세 사양",
            "options": [{"color": "블랙", "size": "FREE", "stock": stock}],
        }
    )


def _login_client(client, email: str, password: str) -> str:
    res = client.post("/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.get_json()
    data = res.get_json()
    return data["csrf_token"]


def _get_csrf_from_html(html: str) -> str:
    import re

    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert m, "csrf_token hidden input not found"
    return m.group(1)


# ==============================================================================
# 1. Unit & Flask API Tests
# ==============================================================================


def test_set_db_path_isolation(tmp_path, isolated_mall_db):
    import mall_database

    prod_db = MALL_DIR / "shopping_mall.db"
    prod_mtime_before = prod_db.stat().st_mtime_ns if prod_db.exists() else None

    nested_db = tmp_path / "nested" / "sub" / "custom_m1.db"
    resolved = mall_database.set_db_path(str(nested_db), init=True)
    assert resolved == str(nested_db.resolve())
    assert nested_db.exists()
    assert mall_database.get_db_path() == str(nested_db.resolve())

    _seed_product(901, "격리테스트상품", category="TOP", price=15000, stock=5)
    assert mall_database.get_mall_product_by_num(901) is not None

    second_db = tmp_path / "second.db"
    mall_database.set_db_path(str(second_db), init=True)
    assert mall_database.get_mall_product_by_num(901) is None

    prod_mtime_after = prod_db.stat().st_mtime_ns if prod_db.exists() else None
    assert prod_mtime_after == prod_mtime_before


def test_scheduler_env_hook(monkeypatch):
    import app as mall_app

    for falsy in ("0", "false", "FALSE", "no", "off"):
        monkeypatch.setenv("MALL_SCHEDULER_ENABLED", falsy)
        assert mall_app.is_scheduler_enabled() is False

    for truthy in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("MALL_SCHEDULER_ENABLED", truthy)
        assert mall_app.is_scheduler_enabled() is True
        assert mall_app.maybe_start_mall_scheduler() is True
        assert mall_app.app.config["MALL_SCHEDULER_ENABLED"] is True

    monkeypatch.delenv("MALL_SCHEDULER_ENABLED", raising=False)
    # pytest sets PYTEST_CURRENT_TEST -> False by default
    assert mall_app.is_scheduler_enabled() is False
    assert mall_app.maybe_start_mall_scheduler() is False


def test_calculate_shipping_fee_edges(isolated_mall_db):
    import mall_database
    import mall_shipping

    # subtotal <= 0 -> 0
    assert mall_shipping.calculate_shipping_fee(0, {"shipping_fee": 4000, "free_shipping_threshold": 0}) == 0
    assert mall_shipping.calculate_shipping_fee(-5000, {"shipping_fee": 4000, "free_shipping_threshold": 0}) == 0
    assert mall_shipping.calculate_shipping_fee("-3,000", {"shipping_fee": 4000, "free_shipping_threshold": 50000}) == 0

    # threshold == 0 -> always shipping_fee for positive subtotal
    assert mall_shipping.calculate_shipping_fee(1000, {"shipping_fee": 4000, "free_shipping_threshold": 0}) == 4000
    assert mall_shipping.calculate_shipping_fee(500000, {"shipping_fee": 3500, "free_shipping_threshold": 0}) == 3500

    # shipping_fee == 0 -> free shipping for all
    assert mall_shipping.calculate_shipping_fee(10000, {"shipping_fee": 0, "free_shipping_threshold": 0}) == 0
    assert mall_shipping.calculate_shipping_fee(10000, {"shipping_fee": "0", "free_shipping_threshold": 50000}) == 0

    # threshold boundary checks
    cfg = {"shipping_fee": 3000, "free_shipping_threshold": 50000}
    assert mall_shipping.calculate_shipping_fee(49999, cfg) == 3000
    assert mall_shipping.calculate_shipping_fee(50000, cfg) == 0
    assert mall_shipping.calculate_shipping_fee(50001, cfg) == 0

    # coercion safety
    assert mall_shipping._coerce_non_negative_int(None, 4000) == 4000
    assert mall_shipping._coerce_non_negative_int(True, 4000) == 4000
    assert mall_shipping._coerce_non_negative_int(False, 4000) == 4000
    assert mall_shipping._coerce_non_negative_int("-500", 4000) == 0
    assert mall_shipping._coerce_non_negative_int("abc", 4000) == 4000
    assert mall_shipping._coerce_non_negative_int("3,000", 4000) == 3000
    assert mall_shipping._coerce_non_negative_int(float("nan"), 4000) == 4000
    assert mall_shipping._coerce_non_negative_int(float("inf"), 4000) == 4000
    assert mall_shipping._coerce_non_negative_int(float("-inf"), 4000) == 4000
    assert mall_shipping._parse_signed_int(float("inf"), 0) == 0
    assert mall_shipping._parse_signed_int(float("-inf"), 0) == 0
    assert mall_shipping._parse_signed_int(float("nan"), 0) == 0
    assert mall_shipping.calculate_shipping_fee(float("inf"), {"shipping_fee": float("inf")}) == 0
    assert mall_shipping.calculate_shipping_fee(float("-inf"), {"shipping_fee": float("-inf")}) == 0
    assert mall_shipping.calculate_shipping_fee(1000, {"shipping_fee": float("inf")}) == 4000
    assert mall_shipping.calculate_shipping_fee(1000, {"shipping_fee": float("-inf")}) == 4000

    # lazy import via DB when store_info is None
    mall_database.set_mall_store_info({"shipping_fee": 2500, "free_shipping_threshold": 30000})
    assert mall_shipping.calculate_shipping_fee(20000) == 2500
    assert mall_shipping.calculate_shipping_fee(30000) == 0
    assert mall_database.calculate_shipping_fee(29999) == 2500
    assert mall_database.calculate_shipping_fee(30000) == 0


def test_store_info_defaults_and_admin_api(isolated_mall_db):
    import app as mall_app
    import mall_database

    assert mall_database.DEFAULT_STORE_INFO["shipping_fee"] == 4000
    assert mall_database.DEFAULT_STORE_INFO["free_shipping_threshold"] == 0
    default_info = mall_database.get_mall_store_info()
    assert default_info["shipping_fee"] == 4000
    assert default_info["free_shipping_threshold"] == 0

    client = mall_app.app.test_client()

    # Unauthenticated -> 401
    r_unauth = client.post("/api/admin/store-info", json={"shipping_fee": 3000})
    assert r_unauth.status_code == 401

    # Member -> 403
    reg_res = mall_database.create_mall_user("member1@test.com", "MemberPass123", "김회원", "010-2222-3333")
    assert reg_res["ok"]
    member_csrf = _login_client(client, "member1@test.com", "MemberPass123")
    r_member = client.post(
        "/api/admin/store-info",
        json={"shipping_fee": 3000},
        headers={"X-CSRF-Token": member_csrf},
    )
    assert r_member.status_code == 403

    # Admin missing CSRF -> 403
    client.get("/auth/logout")
    admin_csrf = _login_client(client, ADMIN_EMAIL, ADMIN_PASS)
    r_no_csrf = client.post("/api/admin/store-info", json={"shipping_fee": 3000})
    assert r_no_csrf.status_code == 403

    # Invalid values -> 400
    for bad_payload in [
        {"shipping_fee": -100},
        {"shipping_fee": "abc"},
        {"shipping_fee": True},
        {"free_shipping_threshold": -1},
        {"free_shipping_threshold": "not_a_num"},
        {"free_shipping_threshold": False},
    ]:
        r_bad = client.post(
            "/api/admin/store-info",
            json=bad_payload,
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert r_bad.status_code == 400, f"Expected 400 for {bad_payload}, got {r_bad.status_code}"

    # Valid update including 0 preservation (never overwritten back to 4000)
    r_zero = client.post(
        "/api/admin/store-info",
        json={
            "bank_name": "신한은행",
            "bank_account": "110-999-888777",
            "bank_holder": "골라방상사",
            "shipping_fee": 0,
            "free_shipping_threshold": 0,
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert r_zero.status_code == 200
    zero_info = r_zero.get_json()["store_info"]
    assert zero_info["shipping_fee"] == 0
    assert zero_info["free_shipping_threshold"] == 0
    assert zero_info["bank_name"] == "신한은행"
    assert "전 상품 무료배송" in mall_database.get_mall_policy_defaults()["shipping_info"]

    # Update with dynamic shipping_fee & threshold
    r_upd = client.post(
        "/api/admin/store-info",
        json={"shipping_fee": 3000, "free_shipping_threshold": 50000},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert r_upd.status_code == 200
    upd_info = r_upd.get_json()["store_info"]
    assert upd_info["shipping_fee"] == 3000
    assert upd_info["free_shipping_threshold"] == 50000
    assert "3,000원" in mall_database.get_mall_policy_defaults()["shipping_info"]
    assert "50,000원 이상 무료배송" in mall_database.get_mall_policy_defaults()["shipping_info"]


def test_create_order_dynamic_shipping_and_zipcode(isolated_mall_db):
    import app as mall_app
    import mall_database

    _seed_product(1, "테스트 니트", category="TOP", price=20000, stock=20)
    mall_database.set_mall_store_info({"shipping_fee": 3000, "free_shipping_threshold": 50000})

    # Below threshold: 20,000 * 2 = 40,000 -> shipping 3,000 -> total 43,000
    ord_low = mall_database.create_mall_order(
        {
            "buyer_name": "홍길동",
            "buyer_phone": "010-1234-5678",
            "zipcode": "062 36",
            "address": "서울시 강남구 테헤란로 1",
            "address_detail": "101호",
            "delivery_memo": "문앞",
            "is_guest": True,
            "guest_password": "pass1234",
            "items": [{"num": 1, "color": "블랙", "size": "FREE", "quantity": 2}],
        }
    )
    assert ord_low["ok"]
    assert ord_low["zipcode"] == "06236"
    assert ord_low["address"] == "서울시 강남구 테헤란로 1"
    assert ord_low["subtotal_amount"] == 40000
    assert ord_low["shipping_fee"] == 3000
    assert ord_low["total_amount"] == 43000

    # At/above threshold: 20,000 * 3 = 60,000 -> shipping 0 -> total 60,000
    ord_free = mall_database.create_mall_order(
        {
            "buyer_name": "홍길동",
            "buyer_phone": "010-1234-5678",
            "zipcode": "13529",
            "address": "경기도 성남시 분당구",
            "is_guest": True,
            "guest_password": "pass1234",
            "items": [{"num": 1, "color": "블랙", "size": "FREE", "quantity": 3}],
        }
    )
    assert ord_free["ok"]
    assert ord_free["shipping_fee"] == 0
    assert ord_free["total_amount"] == 60000

    # Guest 3-factor lookup preserves zipcode and hides guest_password_hash
    found = mall_database.lookup_guest_mall_order(ord_low["order_code"], "01012345678", "pass1234")
    assert len(found) == 1
    assert found[0]["zipcode"] == "06236"
    assert "guest_password_hash" not in found[0]
    assert mall_database.lookup_guest_mall_order(ord_low["order_code"], "01012345678", "wrong") == []


def test_legacy_null_shipping_fee_and_migrations(tmp_path, monkeypatch):
    import sqlite3
    import app as mall_app
    import mall_database
    import mall_security

    monkeypatch.setenv("MALL_ADMIN_EMAIL", ADMIN_EMAIL)
    monkeypatch.setenv("MALL_ADMIN_PASSWORD", ADMIN_PASS)
    mall_security.rate_limiter._hits.clear()

    legacy_db = tmp_path / "legacy_pre_m1.db"
    now_str = "2026-01-01 00:00:00"
    conn = sqlite3.connect(str(legacy_db))
    conn.execute(
        """
        CREATE TABLE mall_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_code TEXT UNIQUE NOT NULL,
            buyer_name TEXT NOT NULL,
            buyer_phone TEXT NOT NULL,
            address TEXT NOT NULL,
            address_detail TEXT DEFAULT '',
            delivery_memo TEXT DEFAULT '',
            payment_method TEXT NOT NULL DEFAULT 'BANK',
            payment_status TEXT NOT NULL DEFAULT '입금대기',
            order_status TEXT NOT NULL DEFAULT '결제대기',
            subtotal_amount INTEGER NOT NULL DEFAULT 0,
            shipping_fee INTEGER DEFAULT NULL,
            total_amount INTEGER NOT NULL DEFAULT 0,
            source_page TEXT NOT NULL DEFAULT 'MALL',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE mall_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'MEMBER',
            is_active INTEGER NOT NULL DEFAULT 1,
            failed_login_count INTEGER NOT NULL DEFAULT 0,
            locked_until REAL NOT NULL DEFAULT 0,
            last_login_at TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    # Running set_db_path(..., init=True) runs ALTER TABLE migrations cleanly
    mall_database.set_db_path(str(legacy_db), init=True)
    mall_database.ensure_default_mall_admin()

    with mall_database.mall_db_lock:
        c2 = mall_database.get_connection()
        try:
            c2.execute(
                """
                INSERT INTO mall_orders
                (order_code, buyer_name, buyer_phone, zipcode, address, address_detail,
                 payment_method, payment_status, order_status, subtotal_amount, shipping_fee, total_amount,
                 source_page, is_guest, guest_password_hash, created_at)
                VALUES (?, ?, ?, NULL, ?, '', 'BANK', '입금대기', '결제대기', 20000, NULL, 23000, 'MALL', 1, ?, ?)
                """,
                (
                    "ORD-LEGACY-NULL",
                    "레거시",
                    "010-9999-8888",
                    "서울시 종로구",
                    mall_security.hash_password("legacy99"),
                    now_str,
                ),
            )
            c2.commit()
        finally:
            c2.close()

    legacy_rows = mall_database.lookup_guest_mall_order("ORD-LEGACY-NULL", "010-9999-8888", "legacy99")
    assert len(legacy_rows) == 1
    assert legacy_rows[0]["zipcode"] == ""
    assert legacy_rows[0]["shipping_fee"] == 3000

    client = mall_app.app.test_client()
    r_lookup = client.post(
        "/api/orders/lookup",
        json={"order_code": "ORD-LEGACY-NULL", "buyer_phone": "010-9999-8888", "guest_password": "legacy99"},
    )
    assert r_lookup.status_code == 200
    assert r_lookup.get_json()["orders"][0]["shipping_fee"] == 3000

    # /orders HTML render for legacy row
    r_page_get = client.get("/orders")
    csrf = _get_csrf_from_html(r_page_get.get_data(as_text=True))
    r_page_post = client.post(
        "/orders",
        data={
            "csrf_token": csrf,
            "order_code": "ORD-LEGACY-NULL",
            "buyer_phone": "010-9999-8888",
            "guest_password": "legacy99",
        },
    )
    assert r_page_post.status_code == 200
    assert "3,000원" in r_page_post.get_data(as_text=True)


def test_related_products_ranking_and_regular_price(isolated_mall_db):
    import app as mall_app
    import mall_database

    # Seed current product (TOP, #10) + same-cat sold-out (#11) + same-cat active (#12, #13) + other-cat active (#14, #15)
    _seed_product(10, "메인 상의", category="TOP", price=39000, live_price=29000, stock=10)
    _seed_product(11, "품절 상의", category="TOP", price=31000, live_price=21000, stock=0)
    _seed_product(12, "인기 상의A", category="TOP", price=42000, live_price=32000, stock=8)
    _seed_product(13, "인기 상의B", category="TOP", price=44000, live_price=34000, stock=5)
    _seed_product(14, "아우터 상품", category="OUTER", price=89000, live_price=69000, stock=7)
    _seed_product(15, "팬츠 상품", category="BOTTOM", price=55000, live_price=45000, stock=4)

    cur = mall_database.get_mall_product_by_num(10)
    related = mall_database.get_related_mall_products(cur, limit=4)
    assert [p["num"] for p in related] == [12, 13, 11, 14]
    assert 10 not in [p["num"] for p in related]

    client = mall_app.app.test_client()
    res = client.get("/product/10")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert 'id="section-related"' in html
    assert "함께 보기 좋은 연관 상품" in html
    assert "42,000원" in html
    # Store page must never expose live_price in rendered related section
    section_part = html.split('id="section-related"')[1].split("</section>")[0]
    assert "32,000원" not in section_part
    assert "21,000원" not in section_part
    assert "69,000원" not in section_part


def test_store_pages_strip_live_and_cost_fields_from_full_html(isolated_mall_db):
    import app as mall_app
    import mall_database

    # Seed products with distinct live_price, cost_price, and obs_product_id values
    mall_database.upsert_product_from_obs(
        {
            "obs_product_id": 7701,
            "num": 1,
            "name": "스토어 분리 메인 상품",
            "category": "TOP",
            "price": 50000,
            "live_price": 13579,
            "consumer_price": 70000,
            "cost_price": 9876,
            "image_url": "",
            "specs": "분리 검증 메인",
            "options": [{"color": "블랙", "size": "FREE", "stock": 10}],
        }
    )
    for idx in range(2, 6):
        mall_database.upsert_product_from_obs(
            {
                "obs_product_id": 7700 + idx,
                "num": idx,
                "name": f"스토어 분리 연관 상품 {idx}",
                "category": "TOP",
                "price": 40000 + idx * 1000,
                "live_price": 13579,
                "consumer_price": 60000,
                "cost_price": 9876,
                "image_url": "",
                "specs": "분리 검증 연관",
                "options": [{"color": "블랙", "size": "FREE", "stock": 5}],
            }
        )

    client = mall_app.app.test_client()

    # 1. GET /
    r_home = client.get("/")
    assert r_home.status_code == 200
    home_html = r_home.get_data(as_text=True)
    for forbidden_key in ("live_price", "live_discount_rate", "cost_price", "obs_product_id"):
        assert forbidden_key not in home_html, f"{forbidden_key} leaked in GET / HTML"
    assert "13579" not in home_html
    assert "9876" not in home_html

    # 2. GET /product/1 (full HTML including #mallProductDetailBootstrap & #mallRelatedProductsBootstrap)
    r_detail = client.get("/product/1")
    assert r_detail.status_code == 200
    detail_html = r_detail.get_data(as_text=True)
    for forbidden_key in ("live_price", "live_discount_rate", "cost_price", "obs_product_id"):
        assert forbidden_key not in detail_html, f"{forbidden_key} leaked in GET /product/1 HTML"
    assert "13579" not in detail_html
    assert "9876" not in detail_html

    # 3. GET /wishlist & GET /api/wishlist
    reg_res = mall_database.create_mall_user("wishsep@test.com", "WishPass123", "위시회원", "010-1212-3434")
    assert reg_res["ok"]
    wish_csrf = _login_client(client, "wishsep@test.com", "WishPass123")
    r_toggle = client.post("/api/wishlist/toggle", json={"product_num": 1}, headers={"X-CSRF-Token": wish_csrf})
    assert r_toggle.status_code == 200

    r_wish_page = client.get("/wishlist")
    assert r_wish_page.status_code == 200
    wish_html = r_wish_page.get_data(as_text=True)
    for forbidden_key in ("live_price", "live_discount_rate", "cost_price", "obs_product_id"):
        assert forbidden_key not in wish_html, f"{forbidden_key} leaked in GET /wishlist HTML"
    assert "13579" not in wish_html
    assert "9876" not in wish_html

    r_wish_api = client.get("/api/wishlist")
    assert r_wish_api.status_code == 200
    wish_prods = r_wish_api.get_json()["products"]
    assert len(wish_prods) == 1
    for forbidden_key in ("live_price", "live_discount_rate", "cost_price", "obs_product_id"):
        assert forbidden_key not in wish_prods[0]

    # 4. /live and /api/live/state remain intact with live_price
    r_live_state = client.get("/api/live/state")
    assert r_live_state.status_code == 200
    assert r_live_state.get_json()["all_products"][0]["live_price"] == 13579


def test_register_zipcode_prefill_and_webhook(isolated_mall_db, monkeypatch):
    import app as mall_app
    import mall_database

    _seed_product(2, "웹훅 테스트 상품", category="TOP", price=30000, stock=10)

    client = mall_app.app.test_client()
    r_get = client.get("/auth/register?zipcode=06236&address=%EC%84%9C%EC%9A%B8")
    assert r_get.status_code == 200
    html = r_get.get_data(as_text=True)
    assert 'id="regZipcode"' in html
    assert 'value="06236"' in html
    csrf = _get_csrf_from_html(html)

    # Form POST register
    r_form = client.post(
        "/auth/register",
        data={
            "csrf_token": csrf,
            "email": "zipform@test.com",
            "password": "ZipUser1234",
            "name": "우편폼",
            "phone": "010-1111-2222",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "address_detail": "3층",
            "agree_terms": "on",
            "agree_privacy": "on",
        },
    )
    assert r_form.status_code in (200, 302)
    auth1 = mall_database.authenticate_mall_user("zipform@test.com", "ZipUser1234")
    assert auth1["ok"]
    assert auth1["user"]["zipcode"] == "06236"
    client.get("/auth/logout")

    # JSON POST register
    r_json = client.post(
        "/auth/register",
        json={
            "email": "zipjson@test.com",
            "password": "ZipUser1234",
            "name": "우편제이슨",
            "phone": "010-3333-4444",
            "zipcode": "13529",
            "address": "경기도 성남시 분당구 판교역로 166",
            "address_detail": "A동",
            "agree_terms": True,
            "agree_privacy": True,
        },
    )
    assert r_json.status_code == 200
    assert r_json.get_json()["user"]["zipcode"] == "13529"

    # Mock OBS order webhook and verify zipcode included in payload
    captured = []

    class _ImmediateThread:
        def __init__(self, target=None, daemon=None):
            self._target = target

        def start(self):
            if self._target:
                self._target()

    class _DummyResp:
        def read(self):
            return b"ok"

    def _fake_urlopen(req, timeout=1.2):
        captured.append(json.loads(req.data.decode("utf-8")))
        return _DummyResp()

    monkeypatch.setenv("MALL_MODE", "local")
    monkeypatch.setenv("OBS_PULL_ENABLED", "1")
    monkeypatch.setattr(mall_app.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(mall_app.urllib.request, "urlopen", _fake_urlopen)

    csrf2 = r_json.get_json()["csrf_token"]
    r_ord = client.post(
        "/api/orders/create",
        json={
            "buyer_name": "웹훅테스터",
            "buyer_phone": "010-5555-6666",
            "zipcode": "04524",
            "address": "서울시 중구 세종대로 110",
            "address_detail": "본관",
            "source_page": "CART_CHECKOUT",
            "items": [{"num": 2, "color": "블랙", "size": "FREE", "quantity": 1}],
        },
        headers={"X-CSRF-Token": csrf2},
    )
    assert r_ord.status_code == 200
    assert len(captured) == 1
    assert captured[0]["zipcode"] == "04524"


def test_state_lite_payload_ratio(isolated_mall_db):
    import app as mall_app

    for idx in range(1, 22):
        _seed_product(
            idx,
            f"대용량 카탈로그 상품 {idx}번 - 상세 설명 및 옵션 포함 테스트 아이템",
            category="TOP" if idx % 2 else "OUTER",
            price=35000 + idx * 1000,
            stock=15,
        )

    client = mall_app.app.test_client()
    r_full = client.get("/api/live/state")
    r_lite = client.get("/api/live/state-lite")
    assert r_full.status_code == 200
    assert r_lite.status_code == 200
    full_bytes = len(r_full.get_data())
    lite_bytes = len(r_lite.get_data())
    assert lite_bytes < full_bytes * 0.35, f"state-lite too large: {lite_bytes} vs {full_bytes}"


# ==============================================================================
# 2. Live Subprocess Server + Playwright Desktop & Mobile E2E Tests
# ==============================================================================


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
def live_mall_server(tmp_path):
    db_file = tmp_path / "m1_e2e.db"
    port = _find_free_port()
    runner_code = f"""
import os, sys
sys.path.insert(0, {str(MALL_DIR)!r})
os.environ['MALL_DB_PATH'] = {str(db_file)!r}
os.environ['OBS_PULL_ENABLED'] = '0'
os.environ['MALL_MODE'] = 'cloud'
os.environ['MALL_SCHEDULER_ENABLED'] = '0'
os.environ['MALL_ADMIN_EMAIL'] = {ADMIN_EMAIL!r}
os.environ['MALL_ADMIN_PASSWORD'] = {ADMIN_PASS!r}

import mall_database
mall_database.set_db_path({str(db_file)!r}, init=True)
mall_database.ensure_default_mall_admin()
for i in range(1, 7):
    mall_database.upsert_product_from_obs({{
        'obs_product_id': 2000 + i,
        'num': i,
        'name': f'E2E 상품 {{i}}호',
        'category': 'TOP' if i <= 4 else 'OUTER',
        'price': 20000 if i == 1 else (30000 + i * 1000),
        'live_price': 15000,
        'consumer_price': 45000,
        'image_url': '',
        'specs': 'Playwright 검증용 상품',
        'options': [{{'color': '네이비', 'size': 'FREE', 'stock': 20}}]
    }})

from werkzeug.serving import make_server
import app as mall_app
srv = make_server('127.0.0.1', {port}, mall_app.app, threaded=True)
print('READY_PORT=' + str(srv.server_port), flush=True)
srv.serve_forever()
"""
    server_log_path = tmp_path / "mall_server.log"
    server_log_file = open(server_log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, "-c", runner_code],
        cwd=str(MALL_DIR),
        stdout=server_log_file,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 15.0
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                server_log_file.flush()
                out = server_log_path.read_text(encoding="utf-8", errors="replace")
                raise RuntimeError(f"Mall test server exited early: {out}")
            try:
                with urllib.request.urlopen(f"{base_url}/api/live/state-lite", timeout=1.0) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.15)
        if not ready:
            proc.terminate()
            server_log_file.flush()
            out = server_log_path.read_text(encoding="utf-8", errors="replace")
            raise RuntimeError(f"Timed out waiting for live_mall_server: {out}")
        yield base_url, db_file
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        server_log_file.close()


def _attach_console_error_collector(page):
    errors = []

    def _on_console(msg):
        if msg.type == "error":
            errors.append(msg.text)

    def _on_pageerror(err):
        errors.append(str(err))

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)
    return errors


def _assert_layout_clean(page):
    metrics = page.evaluate(
        """() => {
            const doc = document.documentElement;
            const overflow = doc.scrollWidth > window.innerWidth + 1;
            const badScrollEls = [];
            document.querySelectorAll('main section, main .ax-card, .ax-split-layout, .ax-split-layout-wide').forEach(el => {
                if (el.closest('.ax-dialog-card, .ax-dialog-body, .ax-lightbox-card')) return;
                const st = window.getComputedStyle(el);
                if ((st.overflowY === 'auto' || st.overflowY === 'scroll') && st.maxHeight !== 'none') {
                    badScrollEls.push(el.id || el.className);
                }
            });
            return {
                scrollWidth: doc.scrollWidth,
                innerWidth: window.innerWidth,
                overflow,
                badScrollEls
            };
        }"""
    )
    assert not metrics["overflow"], f"Horizontal overflow detected: {metrics}"
    assert metrics["badScrollEls"] == [], f"Internal vertical scroll detected: {metrics['badScrollEls']}"


def test_e2e_m1_all_features_desktop_and_mobile(live_mall_server):
    base_url, _db_file = live_mall_server

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            # 1. Desktop (1440x900): Admin store settings + F1 + F2 + F3 + F4/F5
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            # Stub external Daum CDN with empty JS by default to test offline/unavailable SDK fallback deterministically without network hangs
            ctx.route("**://t1.daumcdn.net/**", lambda route: route.fulfill(status=200, content_type="application/javascript", body="/* offline stub */"))
            page = ctx.new_page()
            console_errors = _attach_console_error_collector(page)

            # Admin login via UI & update store_info (F1 + F5)
            page.goto(f"{base_url}/auth/login?next=/admin", wait_until="domcontentloaded")
            page.fill('input[name="email"]', ADMIN_EMAIL)
            page.fill('input[name="password"]', ADMIN_PASS)
            page.click('button[type="submit"]')
            page.wait_for_url(f"{base_url}/admin**")
            _assert_layout_clean(page)

            page.click('#adminTabStore')
            page.fill('#asBankName', '우리은행')
            page.fill('#asBankAccount', '1002-777-654321')
            page.fill('#asBankHolder', '골라방스토어')
            page.fill('#asShippingFee', '3000')
            page.fill('#asFreeShippingThreshold', '50000')
            with page.expect_response(lambda r: r.url.endswith('/api/admin/store-info') and r.request.method == 'POST') as resp_info:
                page.click('#btnSaveAdminStoreInfo')
            assert resp_info.value.status == 200
            page.wait_for_timeout(150)

            # Logout so guest flows (F3 guest checkout/instant-buy) can run cleanly
            page.goto(f"{base_url}/auth/logout", wait_until="domcontentloaded")

            # F2: Product detail page related products + regular price + instant-buy F1/F3/F5
            page.goto(f"{base_url}/product/1", wait_until="domcontentloaded")
            _assert_layout_clean(page)
            rel_cards = page.locator('#section-related .ax-product-card')
            assert rel_cards.count() == 4
            rel_text = page.locator('#section-related').inner_text()
            assert '15,000원' not in rel_text  # live_price must never appear on store page

            # Open instant-buy modal (1 * 20,000 = 20,000 -> 30,000원 더 담으면 무료배송!)
            page.click('button:has-text("바로 구매하기")')
            page.wait_for_selector('#instantBuyModal', state='visible')
            assert '30,000원 더 담으면 무료배송!' in page.locator('#ibShippingGuide').inner_text()
            assert '택배비 3,000원' in page.locator('#instantBuyItemSummary').inner_text()

            # Mock Daum Postcode on instant buy modal (F3)
            page.evaluate(
                """() => {
                    window.daum = {
                        Postcode: function (opts) {
                            return {
                                open: function () {
                                    opts.oncomplete({
                                        zonecode: '06236',
                                        userSelectedType: 'R',
                                        roadAddress: '서울 강남구 테헤란로 152',
                                        bname: '역삼동',
                                        buildingName: '강남파이낸스센터',
                                        apartment: 'N'
                                    });
                                }
                            };
                        }
                    };
                }"""
            )
            page.click('#btnIbPostcode')
            assert page.input_value('#ibZipcode') == '06236'
            assert '서울 강남구 테헤란로 152' in page.input_value('#ibAddress')

            # Submit instant-buy order & verify F1 toast has custom bank info (no hardcoded old bank)
            page.fill('#ibBuyerName', '테스트구매자')
            page.fill('#ibBuyerPhone', '010-1234-9999')
            page.fill('#ibAddressDetail', '10층')
            page.fill('#ibGuestPassword', '9999')
            page.click('#ibSubmitBtn')
            page.wait_for_selector('#mallToastContainer .ax-toast')
            toast_txt = page.locator('#mallToastContainer .ax-toast').first.inner_text()
            assert '우리은행 1002-777-654321 골라방스토어' in toast_txt
            assert '3333 233 094904' not in toast_txt

            # Redirects to /order-complete/ORD-... (M3) then verify /orders F1 bank sync, F3 zipcode chip, F4 dynamic shipping 3,000원
            page.wait_for_url(f"{base_url}/order-complete/ORD-**", timeout=5000)
            _assert_layout_clean(page)
            page.goto(f"{base_url}/orders", wait_until="domcontentloaded")
            page.wait_for_selector('.ax-order-card')
            _assert_layout_clean(page)
            orders_main_text = page.locator('main').inner_text()
            footer_text = page.locator('footer').inner_text()
            assert '우리은행 1002-777-654321' in orders_main_text
            assert '골라방스토어' in orders_main_text
            assert '3333 233 094904' not in orders_main_text
            assert '우리은행 1002-777-654321' in footer_text
            assert '3333 233 094904' not in footer_text
            assert '[06236]' in page.locator('.ax-order-card').first.inner_text()
            assert '택배비 3,000원' in page.locator('.ax-order-card').first.inner_text()

            # F4/F5: Cart below & above 50,000 threshold
            page.goto(f"{base_url}/product/1", wait_until="domcontentloaded")
            page.click('button:has-text("장바구니 담기")')
            page.goto(f"{base_url}/cart", wait_until="domcontentloaded")
            _assert_layout_clean(page)
            assert page.locator('#cartShippingText').inner_text() == '3,000원'
            assert page.locator('#cartTotalText').inner_text() == '23,000원'
            assert '30,000원 더 담으면 무료배송!' in page.locator('#cartShippingGuide').inner_text()

            # Increase qty to 3 -> 60,000 >= 50,000 -> shipping 0원
            page.click('.ax-qty-btn[aria-label="수량 증가"]')
            page.click('.ax-qty-btn[aria-label="수량 증가"]')
            assert page.locator('#cartSubtotalText').inner_text() == '60,000원'
            assert page.locator('#cartShippingText').inner_text() == '무료배송 (0원)'
            assert page.locator('#cartTotalText').inner_text() == '60,000원'
            assert '무료배송 적용 (0원)' in page.locator('#cartShippingGuide').inner_text()

            # F3 + F4 + F5: Checkout with offline Daum Postcode fallback (no window.daum)
            page.click('#btnGoCheckout')
            page.wait_for_url(f"{base_url}/checkout**")
            _assert_layout_clean(page)
            assert page.locator('#coShippingAmount').inner_text() == '무료배송 (0원)'
            assert page.locator('#coTotalAmount').inner_text() == '60,000원'
            assert '무료배송 적용 (0원)' in page.locator('#coShippingGuide').inner_text()

            # Click postcode button when external script is aborted -> toast fallback & editable input
            page.evaluate("() => { delete window.daum; }")
            page.click('#btnCoPostcode')
            page.wait_for_selector('#mallToastContainer .ax-toast')
            assert '직접 입력' in page.locator('#mallToastContainer .ax-toast').last.inner_text()

            page.fill('#coBuyerName', '무료배송고객')
            page.fill('#coBuyerPhone', '010-7777-8888')
            page.fill('#coZipcode', '04524')
            page.fill('#coAddress', '서울시 중구 세종대로 110')
            page.fill('#coAddressDetail', '1층')
            page.fill('#coGuestPassword', '7777')
            page.click('#coSubmitBtn')
            page.wait_for_url(f"{base_url}/order-complete/ORD-**", timeout=5000)
            _assert_layout_clean(page)
            page.goto(f"{base_url}/orders", wait_until="domcontentloaded")
            page.wait_for_selector('.ax-order-card')
            card_txt = page.locator('.ax-order-card').first.inner_text()
            assert '[04524]' in card_txt
            assert '무료배송 0원' in card_txt

            # F3: /auth/register Daum Postcode mock
            page.goto(f"{base_url}/auth/register", wait_until="domcontentloaded")
            _assert_layout_clean(page)
            page.evaluate(
                """() => {
                    window.daum = {
                        Postcode: function (opts) {
                            return {
                                open: function () {
                                    opts.oncomplete({
                                        zonecode: '13529',
                                        userSelectedType: 'R',
                                        roadAddress: '경기 성남시 분당구 판교역로 166',
                                        bname: '백현동',
                                        buildingName: '카카오',
                                        apartment: 'N'
                                    });
                                }
                            };
                        }
                    };
                }"""
            )
            page.click('#btnRegPostcode')
            assert page.input_value('#regZipcode') == '13529'
            assert '경기 성남시 분당구 판교역로 166' in page.input_value('#regAddress')

            assert console_errors == [], f"Desktop console errors: {console_errors}"
            ctx.close()

            # 2. Mobile (390x844): Verify responsive layout, zero overflow, zero internal vertical scroll
            m_ctx = browser.new_context(viewport={"width": 390, "height": 844})
            m_ctx.route("**://t1.daumcdn.net/**", lambda route: route.fulfill(status=200, content_type="application/javascript", body="/* offline stub */"))
            m_page = m_ctx.new_page()
            m_errors = _attach_console_error_collector(m_page)

            for path in ("/", "/product/1", "/cart", "/auth/register", "/orders"):
                if path == "/cart":
                    m_page.goto(f"{base_url}/product/1", wait_until="domcontentloaded")
                    m_page.click('button:has-text("장바구니 담기")')
                m_page.goto(f"{base_url}{path}", wait_until="domcontentloaded")
                _assert_layout_clean(m_page)

            m_page.goto(f"{base_url}/checkout", wait_until="domcontentloaded")
            _assert_layout_clean(m_page)

            assert m_errors == [], f"Mobile console errors: {m_errors}"
            m_ctx.close()
        finally:
            browser.close()


def test_e2e_dom_xss_product_fields_blocked(tmp_path):
    db_file = tmp_path / "m1_xss_e2e.db"
    port = _find_free_port()
    xss_name = '테스트니트<img src=x onerror="window.__xssProd=1">'
    xss_color = '<svg onload="window.__xssProd=2">'
    xss_size = '<img src=x onerror="window.__xssProd=3">'
    xss_img = 'https://example.com/a.jpg" onerror="window.__xssProd=4'

    runner_code = f"""
import os, sys
sys.path.insert(0, {str(MALL_DIR)!r})
os.environ['MALL_DB_PATH'] = {str(db_file)!r}
os.environ['OBS_PULL_ENABLED'] = '0'
os.environ['MALL_MODE'] = 'cloud'
os.environ['MALL_SCHEDULER_ENABLED'] = '0'
os.environ['MALL_ADMIN_EMAIL'] = {ADMIN_EMAIL!r}
os.environ['MALL_ADMIN_PASSWORD'] = {ADMIN_PASS!r}

import mall_database
mall_database.set_db_path({str(db_file)!r}, init=True)
mall_database.ensure_default_mall_admin()
mall_database.upsert_product_from_obs({{
    'obs_product_id': 9101,
    'num': 1,
    'name': {xss_name!r},
    'category': 'TOP',
    'price': 25000,
    'live_price': 15000,
    'consumer_price': 35000,
    'image_url': {xss_img!r},
    'specs': 'XSS 방어 검증용 상품',
    'options': [{{'color': {xss_color!r}, 'size': {xss_size!r}, 'stock': 10}}]
}})

from werkzeug.serving import make_server
import app as mall_app
srv = make_server('127.0.0.1', {port}, mall_app.app, threaded=True)
srv.serve_forever()
"""
    server_log_path = tmp_path / "xss_server.log"
    server_log_file = open(server_log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, "-c", runner_code],
        cwd=str(MALL_DIR),
        stdout=server_log_file,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 15.0
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                server_log_file.flush()
                raise RuntimeError(f"XSS test server exited early: {server_log_path.read_text(encoding='utf-8', errors='replace')}")
            try:
                with urllib.request.urlopen(f"{base_url}/api/live/state-lite", timeout=1.0) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.15)
        if not ready:
            raise RuntimeError("Timed out waiting for XSS test server")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(viewport={"width": 1440, "height": 900})
                ctx.route("**://example.com/**", lambda route: route.fulfill(status=204, body=""))
                page = ctx.new_page()
                console_errors = _attach_console_error_collector(page)

                # 1. Open #instantBuyModal from /
                page.goto(f"{base_url}/", wait_until="domcontentloaded")
                page.click('button:has-text("바로구매")')
                page.wait_for_selector('#instantBuyModal', state='visible')
                assert page.evaluate("() => window.__xssProd") is None
                assert '<img' in page.locator('#instantBuyItemSummary').inner_text()
                page.click('#instantBuyModal button:has-text("취소")')

                # 2. Add to cart and visit /cart
                page.click('button:has-text("담기")')
                page.goto(f"{base_url}/cart", wait_until="domcontentloaded")
                page.wait_for_selector('.ax-cart-row')
                assert page.evaluate("() => window.__xssProd") is None
                assert '<img' in page.locator('#cartItemsContainer').inner_text()

                # 3. Visit /checkout
                page.goto(f"{base_url}/checkout", wait_until="domcontentloaded")
                page.wait_for_selector('#coOrderItemsSummary')
                assert page.evaluate("() => window.__xssProd") is None
                assert '<img' in page.locator('#coOrderItemsSummary').inner_text()

                assert console_errors == [], f"XSS test console errors: {console_errors}"
                ctx.close()

                # 4. Mobile 390x844 check on /product/1 with unbreakable XSS title
                m_ctx = browser.new_context(viewport={"width": 390, "height": 844})
                m_ctx.route("**://example.com/**", lambda route: route.fulfill(status=204, body=""))
                m_page = m_ctx.new_page()
                m_page.goto(f"{base_url}/product/1", wait_until="domcontentloaded")
                assert m_page.evaluate("() => window.__xssProd") is None
                _assert_layout_clean(m_page)
                m_ctx.close()
            finally:
                browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        server_log_file.close()
