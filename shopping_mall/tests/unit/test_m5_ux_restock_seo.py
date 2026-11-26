# -*- coding: utf-8 -*-
"""
Milestone 5 (M5) Unit, Integration & Playwright UI Tests:
- M4 Cleanup: #ibCouponCode label ('쿠폰 / 프로모션 코드')
- F15: Cart checkbox selection, inline option change, /api/cart/revalidate, atomic create_mall_order price/stock checks & concurrency
- F16: Discovery filters (exclude_sold_out, min_price, max_price), pagination, store sanitization, recently viewed bar, restock alerts & transition hooks
- F17: SEO (OpenGraph meta tags, /static/og-default.png, /sitemap.xml, /robots.txt, request.host public URL)
- Playwright Desktop (1440x900) & Mobile (390x844) checks: zero console/404/overflow/internal-scroll/overlap
"""

import html
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

MALL_DIR = Path(__file__).resolve().parents[2]
if str(MALL_DIR) not in sys.path:
    sys.path.insert(0, str(MALL_DIR))

ADMIN_EMAIL = "m5admin@golabang.test"
ADMIN_PASS = "M5AdminPass!2026"


@pytest.fixture(scope="session", autouse=True)
def _bootstrap_isolated_env(tmp_path_factory):
    boot_dir = tmp_path_factory.mktemp("m5_boot")
    os.environ["MALL_DB_PATH"] = str(boot_dir / "boot_m5.db")
    os.environ["OBS_PULL_ENABLED"] = "0"
    os.environ["MALL_MODE"] = "cloud"
    os.environ["MALL_SCHEDULER_ENABLED"] = "0"
    os.environ["MALL_ADMIN_EMAIL"] = ADMIN_EMAIL
    os.environ["MALL_ADMIN_PASSWORD"] = ADMIN_PASS

    import mall_database  # noqa: F401
    import mall_security  # noqa: F401
    import mall_promotions  # noqa: F401
    import mall_notifications  # noqa: F401
    import app as mall_app  # noqa: F401
    return boot_dir


@pytest.fixture()
def isolated_mall_db(tmp_path, monkeypatch):
    import mall_database
    import mall_security

    db_file = tmp_path / "m5_unit.db"
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
    price: int = 30000,
    live_price: int = 24000,
    options=None,
    image_url: str = "",
    specs: str = "",
):
    import mall_database

    if options is None:
        options = [
            {"color": "블랙", "size": "M", "stock": 10},
            {"color": "화이트", "size": "L", "stock": 5},
        ]
    return mall_database.upsert_product_from_obs(
        {
            "obs_product_id": num + 5000,
            "num": num,
            "name": name,
            "category": category,
            "price": price,
            "live_price": live_price,
            "consumer_price": price + 15000,
            "image_url": image_url,
            "specs": specs or f"{name} 상세 스펙",
            "options": options,
        }
    )


def _create_member(email: str, password: str = "MemberPass123!", name: str = "김오차", phone: str = "010-5555-6666"):
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
# 1. M4 Label Cleanup
# ==============================================================================


def test_m4_ib_coupon_label_cleanup(isolated_mall_db):
    import app as mall_app

    _seed_product(501, "라벨점검 니트", price=25000)
    client = mall_app.app.test_client()
    for path in ("/", "/product/501"):
        res = client.get(path)
        assert res.status_code == 200
        body = res.get_data(as_text=True)
        assert "쿠폰 / 라이브 할인 코드" not in body
        assert 'for="ibCouponCode">쿠폰 / 프로모션 코드</label>' in body


# ==============================================================================
# 2. F15 Cart Revalidation, Atomic Price/Stock Check & Concurrency
# ==============================================================================


def test_f15_cart_revalidate_pricing_stock_and_isolation(isolated_mall_db):
    import app as mall_app
    import mall_database

    _seed_product(
        510,
        "재검증 셔츠",
        price=35000,
        live_price=25000,
        options=[
            {"color": "블랙", "size": "M", "stock": 3},
            {"color": "화이트", "size": "L", "stock": 0},
        ],
    )
    mall_database.set_mall_store_info({"shipping_fee": 3000, "free_shipping_threshold": 50000})
    client = mall_app.app.test_client()

    # Valid store cart: ignores live_price, returns regular price & available_options
    r_ok = client.post(
        "/api/cart/revalidate",
        json={
            "source_page": "CART_CHECKOUT",
            "items": [{"num": 510, "color": "블랙", "size": "M", "quantity": 1, "unit_price": 35000}],
        },
    )
    assert r_ok.status_code == 200
    d_ok = r_ok.get_json()
    assert d_ok["status"] == "ok"
    assert d_ok["ok"] is True and d_ok["valid"] is True
    assert d_ok["price_changed"] is False
    assert d_ok["has_blocked"] is False
    assert d_ok["subtotal"] == 35000
    assert d_ok["shipping_fee"] == 3000
    assert d_ok["total"] == 38000 and d_ok["total_amount"] == 38000
    assert "live_price" not in d_ok["items"][0]
    assert len(d_ok["items"][0]["available_options"]) == 2

    # Price changed + sold-out option + duplicate SKU exceeding stock
    r_bad = client.post(
        "/api/cart/revalidate",
        json={
            "source_page": "STORE",
            "items": [
                {"num": 510, "color": "블랙", "size": "M", "quantity": 2, "unit_price": 30000},
                {"num": 510, "color": "블랙", "size": "M", "quantity": 2, "unit_price": 35000},
                {"num": 510, "color": "화이트", "size": "L", "quantity": 1, "unit_price": 35000},
                {"num": 9999, "color": "블랙", "size": "M", "quantity": 1},
            ],
        },
    )
    assert r_bad.status_code == 200
    d_bad = r_bad.get_json()
    assert d_bad["ok"] is False and d_bad["valid"] is False
    assert d_bad["price_changed"] is True
    assert d_bad["sold_out"] is True
    assert d_bad["insufficient_stock"] is True
    assert d_bad["has_changes"] is True and d_bad["has_blocked"] is True
    assert d_bad["items"][0]["price_changed"] is True
    assert d_bad["items"][1]["insufficient_stock"] is True
    assert d_bad["items"][2]["sold_out"] is True
    assert d_bad["items"][3]["exists"] is False


def test_f15_order_create_atomic_revalidation_and_option_fix(isolated_mall_db):
    import app as mall_app
    import mall_database

    _seed_product(
        520,
        "원자검증 재킷",
        price=40000,
        live_price=29000,
        options=[
            {"color": "블랙", "size": "M", "stock": 5},
            {"color": "네이비", "size": "L", "stock": 0},
        ],
    )
    user = _create_member("m5_buyer@test.com")
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        conn.execute("UPDATE mall_users SET points_balance = 5000 WHERE id = ?", (user["id"],))
        conn.commit()
        conn.close()

    client = mall_app.app.test_client()
    csrf = _login_client(client, "m5_buyer@test.com", "MemberPass123!")

    # Nonexistent option on multi-option product must NOT fall back to LIMIT 1 -> strict 400
    r_bad_opt = client.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf},
        json={
            "buyer_name": "김오차",
            "buyer_phone": "010-5555-6666",
            "address": "서울시 강남구",
            "source_page": "CART_CHECKOUT",
            "items": [{"num": 520, "color": "없는색상", "size": "XXXL", "quantity": 1}],
        },
    )
    assert r_bad_opt.status_code == 400
    assert r_bad_opt.get_json()["code"] == "OUT_OF_STOCK"

    # Sold-out option -> 400 OUT_OF_STOCK
    r_soldout = client.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf},
        json={
            "buyer_name": "김오차",
            "buyer_phone": "010-5555-6666",
            "address": "서울시 강남구",
            "source_page": "CART_CHECKOUT",
            "items": [{"num": 520, "color": "네이비", "size": "L", "quantity": 1}],
        },
    )
    assert r_soldout.status_code == 400
    assert r_soldout.get_json()["code"] == "OUT_OF_STOCK"

    # Stale expected_items unit_price -> 409 PRICE_CHANGED, full rollback of stock/coupon/points
    r_stale = client.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf},
        json={
            "buyer_name": "김오차",
            "buyer_phone": "010-5555-6666",
            "address": "서울시 강남구",
            "source_page": "CART_CHECKOUT",
            "coupon_code": "WELCOME3000",
            "points_used": 2000,
            "items": [{"num": 520, "color": "블랙", "size": "M", "quantity": 1}],
            "expected_items": [{"num": 520, "color": "블랙", "size": "M", "quantity": 1, "unit_price": 30000}],
            "expected_total": 29000,
        },
    )
    assert r_stale.status_code == 409
    assert r_stale.get_json()["code"] == "PRICE_CHANGED"

    # Verify stock, points, and welcome coupon remain untouched after rollback
    prod_after = mall_database.get_mall_product_by_num(520)
    assert prod_after["options"][0]["stock"] == 5
    user_after = mall_database.get_mall_user_by_id(user["id"])
    assert user_after["points_balance"] == 5000

    # Valid order with exact expected_items & expected_total succeeds (40000 - 3000 - 2000 + 4000 = 39000)
    r_ok = client.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf},
        json={
            "buyer_name": "김오차",
            "buyer_phone": "010-5555-6666",
            "address": "서울시 강남구",
            "source_page": "CART_CHECKOUT",
            "coupon_code": "WELCOME3000",
            "points_used": 2000,
            "items": [{"num": 520, "color": "블랙", "size": "M", "quantity": 1}],
            "expected_items": [{"num": 520, "color": "블랙", "size": "M", "quantity": 1, "unit_price": 40000}],
            "expected_total": 39000,
        },
    )
    assert r_ok.status_code == 200, r_ok.get_json()
    assert r_ok.get_json()["order"]["total_amount"] == 39000


def test_f15_last_unit_concurrency_race(isolated_mall_db):
    import mall_database

    _seed_product(
        525,
        "동시성 테스트 백",
        price=50000,
        options=[{"color": "블랙", "size": "ONE", "stock": 1}],
    )
    results = []
    barrier = threading.Barrier(2)

    def _worker(idx: int):
        barrier.wait()
        res = mall_database.create_mall_order(
            {
                "buyer_name": f"경쟁자{idx}",
                "buyer_phone": f"010-1000-000{idx}",
                "address": "서울시 서초구",
                "guest_password": "race9999",
                "source_page": "CART_CHECKOUT",
                "items": [{"num": 525, "color": "블랙", "size": "ONE", "quantity": 1}],
                "expected_items": [{"num": 525, "color": "블랙", "size": "ONE", "quantity": 1, "unit_price": 50000}],
                "expected_total": 54000,
            }
        )
        results.append(res)

    t1 = threading.Thread(target=_worker, args=(1,))
    t2 = threading.Thread(target=_worker, args=(2,))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    ok_cnt = sum(1 for r in results if r.get("ok"))
    fail_cnt = sum(1 for r in results if not r.get("ok"))
    assert ok_cnt == 1 and fail_cnt == 1
    assert mall_database.get_mall_product_by_num(525)["total_stock"] == 0


# ==============================================================================
# 3. F16 Discovery Filters, Pagination, Store Sanitization & Restock Alerts
# ==============================================================================


def test_f16_discovery_filters_pagination_and_store_sanitization(isolated_mall_db):
    import app as mall_app

    for i in range(1, 16):
        stock = 0 if i == 15 else 5
        _seed_product(
            i,
            f"필터상품 {i:02d}",
            category="TOP" if i % 2 == 1 else "OUTER",
            price=10000 + i * 2000,
            live_price=8000 + i * 1000,
            options=[{"color": "블랙", "size": "FREE", "stock": stock}],
        )

    client = mall_app.app.test_client()

    # Unpaginated /api/products?exclude_sold_out=1 returns all 14 in-stock items and strips live_price/cost_price
    r_all = client.get("/api/products?exclude_sold_out=1")
    assert r_all.status_code == 200
    d_all = r_all.get_json()
    assert d_all["total_count"] == 14
    assert len(d_all["products"]) == 14
    assert all(p["num"] != 15 for p in d_all["products"])
    assert all("live_price" not in p and "cost_price" not in p for p in d_all["products"])

    # Price bounds & invalid bounds
    r_price = client.get("/api/products?min_price=16000&max_price=22000")
    assert r_price.status_code == 200
    prices = [p["price"] for p in r_price.get_json()["products"]]
    assert prices and all(16000 <= pr <= 22000 for pr in prices)

    r_empty = client.get("/api/products?min_price=90000&max_price=10000")
    assert r_empty.status_code == 200
    assert r_empty.get_json()["products"] == []

    # Disjoint pagination slices & OOB page safety
    p1 = client.get("/api/products?exclude_sold_out=1&page=1&per_page=5").get_json()
    p2 = client.get("/api/products?exclude_sold_out=1&page=2&per_page=5").get_json()
    p3 = client.get("/api/products?exclude_sold_out=1&page=3&per_page=5").get_json()
    assert p1["total_count"] == 14 and p1["total_pages"] == 3
    assert len(p1["products"]) == 5 and len(p2["products"]) == 5 and len(p3["products"]) == 4
    nums_all = [x["num"] for x in p1["products"] + p2["products"] + p3["products"]]
    assert len(set(nums_all)) == 14

    r_oob_zero = client.get("/api/products?page=0&per_page=0")
    assert r_oob_zero.status_code == 200 and isinstance(r_oob_zero.get_json()["products"], list)
    r_oob_high = client.get("/api/products?page=999&per_page=10")
    assert r_oob_high.status_code == 200 and r_oob_high.get_json()["products"] == []

    # Home SSR renders pagination bar & filters, never exposes live_price
    r_home = client.get("/?exclude_sold_out=1&per_page=6&page=1")
    assert r_home.status_code == 200
    home_html = r_home.get_data(as_text=True)
    assert "homePaginationBar" in home_html
    assert "live_price" not in home_html


def test_f16_restock_alerts_subscribe_rbac_and_transitions(isolated_mall_db):
    import app as mall_app
    import mall_database
    import mall_notifications

    _seed_product(
        601,
        "재입고 니트",
        price=32000,
        options=[
            {"color": "아이보리", "size": "S", "stock": 0},
            {"color": "블랙", "size": "M", "stock": 4},
        ],
    )
    _seed_product(
        602,
        "OBS 재입고 코트",
        price=65000,
        options=[{"color": "브라운", "size": "L", "stock": 0}],
    )
    _seed_product(
        603,
        "취소복구 백",
        price=45000,
        options=[{"color": "블랙", "size": "ONE", "stock": 1}],
    )

    client = mall_app.app.test_client()

    # Reject in-stock, invalid contact, nonexistent product
    assert client.post(
        "/api/restock-alerts/subscribe",
        json={"product_num": 601, "color": "블랙", "size": "M", "contact_type": "EMAIL", "contact_value": "a@b.com"},
    ).status_code == 400
    assert client.post(
        "/api/restock-alerts/subscribe",
        json={"product_num": 601, "color": "아이보리", "size": "S", "contact_type": "EMAIL", "contact_value": "not-an-email"},
    ).status_code == 400
    assert client.post(
        "/api/restock-alerts/subscribe",
        json={"product_num": 9999, "color": "블랙", "size": "M", "contact_type": "EMAIL", "contact_value": "a@b.com"},
    ).status_code == 404

    # Subscribe via contact/email/phone alias + deduplicate
    r_sub1 = client.post(
        "/api/restock-alerts/subscribe",
        json={
            "product_num": 601,
            "color": "아이보리",
            "size": "S",
            "contact": "restock601@test.local",
            "email": "restock601@test.local",
        },
    )
    assert r_sub1.status_code == 200 and r_sub1.get_json()["already_subscribed"] is False
    r_sub_dup = client.post(
        "/api/restock-alerts/subscribe",
        json={
            "product_num": 601,
            "color": "아이보리",
            "size": "S",
            "contact_type": "EMAIL",
            "contact_value": "RESTOCK601@test.local",
        },
    )
    assert r_sub_dup.status_code == 200 and r_sub_dup.get_json()["already_subscribed"] is True

    # RBAC on GET /api/admin/restock-alerts
    assert client.get("/api/admin/restock-alerts").status_code == 401
    _create_member("m5_member@test.com")
    m_csrf = _login_client(client, "m5_member@test.com", "MemberPass123!")
    assert client.get("/api/admin/restock-alerts").status_code == 403
    # Logged-in member CSRF enforcement on subscribe
    assert client.post(
        "/api/restock-alerts/subscribe",
        json={"product_num": 602, "color": "브라운", "size": "L", "contact_type": "SMS", "contact_value": "010-1234-5678"},
    ).status_code == 403
    assert client.post(
        "/api/restock-alerts/subscribe",
        headers={"X-CSRF-Token": m_csrf},
        json={"product_num": 602, "color": "브라운", "size": "L", "contact_type": "SMS", "contact_value": "010-1234-5678"},
    ).status_code == 200

    # Admin login & inspect alerts (only 1 PENDING row for 601)
    client.get("/auth/logout")
    admin_csrf = _login_client(client, ADMIN_EMAIL, ADMIN_PASS)
    alerts_601 = client.get("/api/admin/restock-alerts?product_num=601").get_json()["alerts"]
    assert len(alerts_601) == 1 and alerts_601[0]["status"] == "PENDING"

    # Transition 1: update_mall_product (0 -> 5, then 5 -> 10 idempotently)
    p601 = mall_database.get_mall_product_by_num(601)
    r_upd = client.post(
        "/api/admin/products/update",
        headers={"X-CSRF-Token": admin_csrf},
        json={
            "id": p601["id"],
            "options": [
                {"color": "아이보리", "size": "S", "stock": 5},
                {"color": "블랙", "size": "M", "stock": 4},
            ],
        },
    )
    assert r_upd.status_code == 200
    alerts_601_after = client.get("/api/admin/restock-alerts?product_num=601").get_json()["alerts"]
    assert alerts_601_after[0]["status"] == "NOTIFIED" and alerts_601_after[0]["notified_at"]
    logs_601 = mall_notifications.list_notification_logs(event_type="RESTOCK_ALERT")
    assert sum(1 for l in logs_601 if l["ref_code"] == "RESTOCK-601") == 1

    # Repeat stock update (5 -> 10) does NOT emit duplicate notification
    mall_database.update_mall_product(
        p601["id"],
        {"options": [{"color": "아이보리", "size": "S", "stock": 10}, {"color": "블랙", "size": "M", "stock": 4}]},
    )
    logs_601_repeat = mall_notifications.list_notification_logs(event_type="RESTOCK_ALERT")
    assert sum(1 for l in logs_601_repeat if l["ref_code"] == "RESTOCK-601") == 1

    # Transition 2: upsert_product_from_obs (0 -> 3)
    mall_database.upsert_product_from_obs(
        {
            "obs_product_id": 5602,
            "num": 602,
            "name": "OBS 재입고 코트",
            "category": "OUTER",
            "price": 65000,
            "options": [{"color": "브라운", "size": "L", "stock": 3}],
        }
    )
    logs_602 = [l for l in mall_notifications.list_notification_logs(event_type="RESTOCK_ALERT") if l["ref_code"] == "RESTOCK-602"]
    assert len(logs_602) == 1 and logs_602[0]["channel"] == "SMS" and logs_602[0]["status"] == "LOGGED_ONLY"

    # Transition 3: order exhausts stock to 0 -> restock subscribe -> customer self-cancel restores stock (0 -> 1)
    ord_res = mall_database.create_mall_order(
        {
            "buyer_name": "취소테스트",
            "buyer_phone": "010-7777-8888",
            "address": "서울시 중구",
            "guest_password": "cancel1234",
            "source_page": "MALL",
            "items": [{"num": 603, "color": "블랙", "size": "ONE", "quantity": 1}],
        }
    )
    assert ord_res["ok"]
    assert mall_database.get_mall_product_by_num(603)["total_stock"] == 0

    r_sub603 = client.post(
        "/api/restock-alerts/subscribe",
        headers={"X-CSRF-Token": admin_csrf},
        json={"product_num": 603, "color": "블랙", "size": "ONE", "contact_type": "EMAIL", "contact_value": "cancel603@test.local"},
    )
    assert r_sub603.status_code == 200
    cancel_res = mall_database.cancel_mall_order_by_customer(order_code=ord_res["order_code"], claim_reason="단순변심")
    assert cancel_res["ok"]
    logs_603 = [l for l in mall_notifications.list_notification_logs(event_type="RESTOCK_ALERT") if l["ref_code"] == "RESTOCK-603"]
    assert len(logs_603) == 1


# ==============================================================================
# 4. F17 SEO (OpenGraph, /sitemap.xml, /robots.txt)
# ==============================================================================


def test_f17_seo_opengraph_sitemap_and_robots(isolated_mall_db):
    import app as mall_app
    import mall_database

    client = mall_app.app.test_client()

    # Empty catalog sitemap must still return valid XML 200
    r_sm_empty = client.get("/sitemap.xml")
    assert r_sm_empty.status_code == 200
    assert "xml" in r_sm_empty.content_type
    root_empty = ET.fromstring(r_sm_empty.get_data(as_text=True))
    assert root_empty.tag.endswith("urlset")

    # Seed product with special characters & empty image_url
    _seed_product(
        701,
        '<테스트> "에디션" & 100%',
        price=29000,
        image_url="",
        specs='<스펙> & "설명"',
    )

    for page_path in ("/", "/cs"):
        r_pg = client.get(page_path, headers={"Host": "shop.golabang.test:8080"})
        assert r_pg.status_code == 200
        pg_html = r_pg.get_data(as_text=True)
        for prop in ("og:title", "og:description", "og:image", "og:url"):
            assert f'property="{prop}"' in pg_html
        assert "http://shop.golabang.test:8080/static/og-default.png" in pg_html

    # Default OG image asset must return HTTP 200
    r_og_img = client.get("/static/og-default.png")
    assert r_og_img.status_code == 200 and len(r_og_img.get_data()) > 100

    # Product OG tags + HTML escaping
    r_prod = client.get("/product/701", headers={"Host": "127.0.0.1:54321"})
    assert r_prod.status_code == 200
    p_html = r_prod.get_data(as_text=True)
    assert '<테스트> "에디션" & 100%' not in p_html
    assert "&lt;테스트&gt;" in p_html and "&amp; 100%" in p_html and ("&#34;에디션&#34;" in p_html or "&quot;에디션&quot;" in p_html)
    assert 'property="og:image" content="http://127.0.0.1:54321/static/og-default.png"' in p_html
    assert 'property="og:url" content="http://127.0.0.1:54321/product/701"' in p_html

    # Dynamic sitemap add & delete + excludes /admin & /checkout
    r_sm = client.get("/sitemap.xml", headers={"Host": "127.0.0.1:54321"})
    assert r_sm.status_code == 200
    sm_text = r_sm.get_data(as_text=True)
    root = ET.fromstring(sm_text)
    locs = [el.text for el in root.iter() if el.tag.endswith("loc")]
    assert "http://127.0.0.1:54321/" in locs
    assert "http://127.0.0.1:54321/product/701" in locs
    assert all("/admin" not in u and "/checkout" not in u for u in locs)

    p701 = mall_database.get_mall_product_by_num(701)
    mall_database.delete_mall_product(p701["id"])
    sm_after_del = client.get("/sitemap.xml").get_data(as_text=True)
    assert "/product/701" not in sm_after_del

    # robots.txt
    r_rob = client.get("/robots.txt", headers={"Host": "127.0.0.1:54321"})
    assert r_rob.status_code == 200
    assert "text/plain" in r_rob.content_type
    rob_txt = r_rob.get_data(as_text=True)
    assert "User-agent: *" in rob_txt
    assert "Allow: /" in rob_txt
    assert "Disallow: /admin" in rob_txt
    assert "Disallow: /mypage" in rob_txt
    assert "Disallow: /orders" in rob_txt
    assert "Sitemap: http://127.0.0.1:54321/sitemap.xml" in rob_txt
    assert not re.search(r"(?m)^Disallow:\s*/\s*$", rob_txt)


# ==============================================================================
# 5. Playwright Desktop (1440x900) & Mobile (390x844) End-to-End UI Checks
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


def _boxes_overlap(a: dict, b: dict) -> bool:
    return not (
        a["x"] + a["width"] <= b["x"] + 1
        or b["x"] + b["width"] <= a["x"] + 1
        or a["y"] + a["height"] <= b["y"] + 1
        or b["y"] + b["height"] <= a["y"] + 1
    )


def test_m5_playwright_desktop_and_mobile_ui(tmp_path):
    import mall_database

    db_file = tmp_path / "m5_pw.db"
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["MALL_DB_PATH"] = str(db_file)
    env["OBS_PULL_ENABLED"] = "0"
    env["MALL_MODE"] = "cloud"
    env["MALL_SCHEDULER_ENABLED"] = "0"
    env["MALL_ADMIN_EMAIL"] = ADMIN_EMAIL
    env["MALL_ADMIN_PASSWORD"] = ADMIN_PASS

    mall_database.set_db_path(str(db_file), init=True)
    mall_database.ensure_default_mall_admin()

    # Seed 15 products: #1..#14 in stock (varying prices/options), #15 sold out variant + in-stock variant
    for i in range(1, 15):
        _seed_product(
            i,
            f"M5 스토어 상품 {i:02d}" + (' <img src=x onerror="window.__m5XssHit=1">' if i == 1 else ""),
            category="TOP" if i % 2 == 1 else "OUTER",
            price=10000 + i * 1000,
            options=[
                {"color": "블랙", "size": "M", "stock": 10},
                {"color": "화이트", "size": "L", "stock": 5},
            ],
        )
    _seed_product(
        15,
        "M5 품절옵션 테스트 코트",
        category="OUTER",
        price=48000,
        options=[
            {"color": "네이비", "size": "ONE", "stock": 0},
            {"color": "베이지", "size": "FREE", "stock": 3},
        ],
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
                # 1. Desktop (1440x900)
                ctx = browser.new_context(viewport={"width": 1440, "height": 900})
                page = ctx.new_page()
                console_errs = []
                page_errs = []
                bad_resps = []
                page.on(
                    "console",
                    lambda m: console_errs.append(m.text)
                    if m.type == "error" and "409 (CONFLICT)" not in m.text
                    else None,
                )
                page.on("pageerror", lambda e: page_errs.append(str(e)))
                page.on(
                    "response",
                    lambda r: bad_resps.append((r.status, r.url))
                    if r.status >= 400 and "/api/orders/create" not in r.url
                    else None,
                )

                # Filter & pagination on /
                page.goto(f"{base_url}/", wait_until="networkidle")
                _assert_no_overflow_or_main_vscroll(page, "desktop-home")
                assert page.locator("#homePaginationBar").is_visible()
                with page.expect_navigation(wait_until="networkidle"):
                    page.check("#filterExcludeSoldOut")
                assert "exclude_sold_out=1" in page.url
                page.fill("#filterMinPrice", "15000")
                page.fill("#filterMaxPrice", "25000")
                with page.expect_navigation(wait_until="networkidle"):
                    page.click("#btnApplyPriceFilter")
                assert "min_price=15000" in page.url and "max_price=25000" in page.url

                # Recently viewed bar + XSS safety
                page.goto(f"{base_url}/product/1", wait_until="networkidle")
                page.goto(f"{base_url}/product/2", wait_until="networkidle")
                page.goto(f"{base_url}/product/3", wait_until="networkidle")
                _assert_no_overflow_or_main_vscroll(page, "desktop-product")
                assert page.evaluate("Boolean(window.__m5XssHit)") is False
                recent_box = page.locator("#mallRecentViewedBar")
                assert recent_box.is_visible()
                assert recent_box.locator("a[href='/product/2']").count() == 1
                assert recent_box.locator("a[href='/product/1']").count() == 1

                # Restock alert subscription on sold-out variant (#15 네이비/ONE)
                page.goto(f"{base_url}/product/15", wait_until="networkidle")
                page.select_option("#pdOptionSelect", "0")
                assert page.locator("#pdRestockAlertBox").is_visible()
                page.click("#pdRestockAlertBtn")
                assert page.locator("#pdRestockModal").is_visible()
                page.select_option("#pdRestockContactType", "EMAIL")
                page.fill("#pdRestockContactValue", "pw_restock@golabang.test")
                page.click("#btnPdRestockSubmit")
                page.wait_for_timeout(300)
                assert not page.locator("#pdRestockModal").is_visible()

                # Cart selection + inline option change + mid-checkout price change -> 409 -> reload -> complete
                page.evaluate(
                    """() => {
                        window.saveMallCart([
                            { num: 1, name: 'M5 스토어 상품 01', price: 11000, consumer_price: 26000, image_url: '', color: '블랙', size: 'M', quantity: 1 },
                            { num: 2, name: 'M5 스토어 상품 02', price: 12000, consumer_price: 27000, image_url: '', color: '블랙', size: 'M', quantity: 1 }
                        ]);
                    }"""
                )
                page.goto(f"{base_url}/cart", wait_until="networkidle")
                _assert_no_overflow_or_main_vscroll(page, "desktop-cart")

                # Inline option change on row 0 to 화이트 / L
                row0 = page.locator("#cartItemsContainer .ax-cart-row").nth(0)
                opt_val = row0.locator("select option", has_text="화이트").first.get_attribute("value")
                row0.locator("select").select_option(opt_val)
                page.wait_for_timeout(250)
                assert page.evaluate("window.getMallCart()[0].color") == "화이트"

                # Uncheck row 1 (product #2)
                page.locator("#cartItemsContainer .ax-cart-row").nth(1).locator("input[type='checkbox']").first.uncheck()
                page.wait_for_timeout(150)
                assert "11,000원" in page.locator("#cartSubtotalText").inner_text()
                page.click("#btnGoCheckout")
                page.wait_for_url("**/checkout")
                page.wait_for_load_state("networkidle")
                _assert_no_overflow_or_main_vscroll(page, "desktop-checkout")

                # Admin updates product #1 price mid-checkout -> 19,000
                p1_row = mall_database.get_mall_product_by_num(1)
                mall_database.update_mall_product(p1_row["id"], {"price": 19000})

                page.fill("#coBuyerName", "플레이라이트구매자")
                page.fill("#coBuyerPhone", "010-2222-3333")
                page.fill("#coAddress", "서울시 강남구 역삼로 10")
                page.fill("#coGuestPassword", "pw1234")

                page.on("dialog", lambda d: d.accept())
                with page.expect_response(lambda r: "/api/orders/create" in r.url and r.request.method == "POST") as blk_resp:
                    page.click("#coSubmitBtn")
                assert blk_resp.value.status == 409

                page.reload(wait_until="networkidle")
                assert "19,000원" in page.locator("#coSubtotalAmount").inner_text()
                page.fill("#coBuyerName", "플레이라이트구매자")
                page.fill("#coBuyerPhone", "010-2222-3333")
                page.fill("#coAddress", "서울시 강남구 역삼로 10")
                page.fill("#coGuestPassword", "pw1234")
                with page.expect_response(lambda r: "/api/orders/create" in r.url and r.request.method == "POST") as ok_resp:
                    page.click("#coSubmitBtn")
                assert ok_resp.value.status == 200
                page.wait_for_url("**/order-complete/**")

                # Unchecked item #2 must still remain in cart
                page.goto(f"{base_url}/cart", wait_until="networkidle")
                rem_cart = page.evaluate("window.getMallCart()")
                assert len(rem_cart) == 1 and int(rem_cart[0]["num"]) == 2

                # Admin #adminTabRestock check
                page.goto(f"{base_url}/auth/login", wait_until="networkidle")
                page.fill('input[name="email"]', ADMIN_EMAIL)
                page.fill('input[name="password"]', ADMIN_PASS)
                page.click('button[type="submit"]')
                page.wait_for_load_state("networkidle")
                page.goto(f"{base_url}/admin", wait_until="networkidle")
                page.click("#adminTabRestock")
                page.wait_for_timeout(300)
                _assert_no_overflow_or_main_vscroll(page, "desktop-admin-restock")
                assert "pw_restock@golabang.test" in page.locator("#adminRestockTbody").inner_text()

                # 2. Mobile (390x844) overlap, overflow & scroll checks
                m_ctx = browser.new_context(viewport={"width": 390, "height": 844})
                m_page = m_ctx.new_page()
                m_page.on(
                    "console",
                    lambda m: console_errs.append(m.text)
                    if m.type == "error" and "409 (CONFLICT)" not in m.text
                    else None,
                )
                m_page.on("pageerror", lambda e: page_errs.append(str(e)))

                m_page.goto(f"{base_url}/product/3", wait_until="networkidle")
                m_page.goto(f"{base_url}/", wait_until="networkidle")
                _assert_no_overflow_or_main_vscroll(m_page, "mobile-home")
                recent_bb = m_page.locator("#mallRecentViewedBar").bounding_box()
                nav_bb = m_page.locator("#mallMobileBottomNav").bounding_box()
                assert recent_bb and nav_bb and not _boxes_overlap(recent_bb, nav_bb)

                m_page.goto(f"{base_url}/product/15", wait_until="networkidle")
                _assert_no_overflow_or_main_vscroll(m_page, "mobile-product")
                recent_pd_bb = m_page.locator("#mallRecentViewedBar").bounding_box()
                sticky_bb = m_page.locator("#pdMobileStickyBar").bounding_box()
                assert recent_pd_bb and sticky_bb and not _boxes_overlap(recent_pd_bb, sticky_bb)

                m_page.goto(f"{base_url}/cart", wait_until="networkidle")
                _assert_no_overflow_or_main_vscroll(m_page, "mobile-cart")

                assert console_errs == [], f"Console errors: {console_errs}"
                assert page_errs == [], f"Page errors: {page_errs}"
                assert bad_resps == [], f"Unexpected 4xx/5xx responses: {bad_resps}"
            finally:
                browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


# ==============================================================================
# 6. M5 Iteration 2 Regression Tests (Defect 1: Promo/Points Sync & Defect 2: int64 Overflow)
# ==============================================================================


def test_m5_sqlite_int64_overflow_boundaries_and_revalidate_promo_parity(isolated_mall_db):
    import app as mall_app
    import mall_database
    import mall_promotions

    _seed_product(
        780,
        "경계값 테스트 상품",
        price=20000,
        options=[
            {"color": "블랙", "size": "M", "stock": 0},
            {"color": "화이트", "size": "L", "stock": 5},
        ],
    )
    user = _create_member("m5_i64@test.com")
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        conn.execute("UPDATE mall_users SET points_balance = 5000 WHERE id = ?", (user["id"],))
        conn.commit()
        conn.close()

    res_coupon = mall_promotions.save_admin_coupon(
        {
            "code": "MIN20K",
            "name": "2만원 이상 5천원 할인",
            "discount_type": "FIXED",
            "discount_value": 5000,
            "min_order_amount": 20000,
            "is_active": True,
        }
    )
    assert res_coupon["ok"], res_coupon

    client = mall_app.app.test_client()
    huge_pos = "999999999999999999999"
    huge_neg = "-999999999999999999999"
    huge_5k_pos = "9" * 5000
    huge_5k_neg = "-" + ("9" * 5000)

    # Discovery & product detail endpoints must never return 500 on int64 overflow or >4300-digit strings
    for h_pos, h_neg in ((huge_pos, huge_neg), (huge_5k_pos, huge_5k_neg)):
        for path in (
            f"/?page={h_pos}",
            f"/?min_price={h_pos}",
            f"/?max_price={h_pos}",
            f"/?min_price={h_neg}&max_price={h_pos}",
            f"/?page={h_neg}&per_page={h_pos}",
            f"/api/products?page={h_pos}",
            f"/api/products?min_price={h_pos}",
            f"/api/products?max_price={h_pos}",
            f"/api/products?max_price={h_neg}",
            f"/api/products?page={h_neg}&per_page={h_pos}",
        ):
            resp = client.get(path)
            assert resp.status_code == 200, f"{path[:60]} -> {resp.status_code}"

        assert client.get(f"/product/{h_pos}").status_code in (302, 404)
        assert client.get(f"/api/products/{h_pos}").status_code == 404

        # /api/cart/revalidate rejects out-of-int64-range & >4300-digit fields with clean 400 (never 500)
        for bad_payload in (
            {"items": [{"num": h_pos, "quantity": 1}], "source_page": "CART_CHECKOUT"},
            {"items": [{"num": h_neg, "quantity": 1}], "source_page": "CART_CHECKOUT"},
            {"items": [{"num": 780, "color": "화이트", "size": "L", "quantity": h_pos}], "source_page": "CART_CHECKOUT"},
            {"items": [{"num": 780, "color": "화이트", "size": "L", "quantity": 1, "unit_price": h_pos}], "source_page": "CART_CHECKOUT"},
            {"items": [{"num": 780, "color": "화이트", "size": "L", "quantity": 1}], "points_used": h_pos, "source_page": "CART_CHECKOUT"},
            {"items": [{"num": 780, "color": "화이트", "size": "L", "quantity": 1}], "points_used": h_neg, "source_page": "CART_CHECKOUT"},
        ):
            r_rev = client.post("/api/cart/revalidate", json=bad_payload)
            assert r_rev.status_code == 400, f"{str(bad_payload)[:80]} -> {r_rev.status_code}"

    # /api/cart/revalidate rejects boolean points_used (False and True) cleanly with 400 (parity with /api/orders/create)
    for bad_pts_bool in (False, True):
        r_rev_bool = client.post(
            "/api/cart/revalidate",
            json={"items": [{"num": 780, "color": "화이트", "size": "L", "quantity": 1}], "points_used": bad_pts_bool, "source_page": "CART_CHECKOUT"},
        )
        assert r_rev_bool.status_code == 400, f"points_used={bad_pts_bool} -> {r_rev_bool.status_code}"

    # /api/orders/create rejects >4300-digit / overflow fields cleanly with 400/409 (never 500)
    base_buyer = {
        "buyer_name": "경계테스터",
        "buyer_phone": "010-1111-2222",
        "address": "서울시 강남구 테헤란로 1",
        "guest_password": "test1234",
        "source_page": "CART_CHECKOUT",
    }
    for ord_patch in (
        {"items": [{"num": huge_5k_pos, "quantity": 1}]},
        {"items": [{"num": huge_5k_neg, "quantity": 1}]},
        {"items": [{"num": 780, "color": "화이트", "size": "L", "quantity": huge_5k_pos}]},
        {"items": [{"num": 780, "color": "화이트", "size": "L", "quantity": 1, "expected_price": huge_5k_pos}]},
        {"items": [{"num": 780, "color": "화이트", "size": "L", "quantity": 1}], "expected_total": huge_5k_pos},
        {"items": [{"num": 780, "color": "화이트", "size": "L", "quantity": 1}], "points_used": huge_5k_pos},
        {"items": [{"num": 780, "color": "화이트", "size": "L", "quantity": 1}], "points_used": huge_5k_neg},
    ):
        r_ord = client.post("/api/orders/create", json={**base_buyer, **ord_patch})
        assert r_ord.status_code in (400, 409), f"{str(ord_patch)[:80]} -> {r_ord.status_code}"

    # /api/restock-alerts/subscribe rejects huge product_num cleanly with 400
    for bad_pnum in (huge_pos, huge_neg, huge_5k_pos, huge_5k_neg, "--5"):
        r_sub = client.post(
            "/api/restock-alerts/subscribe",
            json={"product_num": bad_pnum, "color": "블랙", "size": "M", "contact_type": "EMAIL", "contact_value": "a@b.com"},
        )
        assert r_sub.status_code == 400, f"{str(bad_pnum)[:30]} -> {r_sub.status_code}"

    # /api/admin/restock-alerts handles huge product_num (400) & huge limit (200) cleanly
    admin_csrf = _login_client(client, ADMIN_EMAIL, ADMIN_PASS)
    assert admin_csrf
    assert client.get(f"/api/admin/restock-alerts?product_num={huge_pos}").status_code == 400
    assert client.get(f"/api/admin/restock-alerts?product_num={huge_neg}").status_code == 400
    assert client.get(f"/api/admin/restock-alerts?product_num={huge_5k_pos}").status_code == 400
    assert client.get(f"/api/admin/restock-alerts?product_num={huge_5k_neg}").status_code == 400
    assert client.get(f"/api/admin/restock-alerts?limit={huge_pos}").status_code == 200
    assert client.get(f"/api/admin/restock-alerts?limit={huge_5k_pos}").status_code == 200
    assert client.get(f"/api/admin/restock-alerts?limit={huge_5k_neg}").status_code == 200
    client.get("/auth/logout")

    # Promo parity on /api/cart/revalidate: invalid min-order coupon + points clamping
    _login_client(client, "m5_i64@test.com", "MemberPass123!")
    p780 = mall_database.get_mall_product_by_num(780)
    mall_database.update_mall_product(p780["id"], {"price": 4000})
    r_promo = client.post(
        "/api/cart/revalidate",
        json={
            "source_page": "CART_CHECKOUT",
            "coupon_code": "MIN20K",
            "points_used": 5000,
            "items": [{"num": 780, "color": "화이트", "size": "L", "quantity": 1, "unit_price": 20000}],
        },
    )
    assert r_promo.status_code == 200
    d_promo = r_promo.get_json()
    assert d_promo["subtotal"] == 4000
    assert d_promo["coupon_valid"] is False
    assert d_promo["coupon_discount"] == 0
    assert d_promo["coupon_code"] == ""
    assert d_promo["promo_error"]
    assert d_promo["points_used"] == 4000
    assert d_promo["total"] == 4000  # 4000 - 0 - 4000 + 4000 shipping


def test_f15_checkout_coupon_and_points_409_recovery_playwright(tmp_path):
    import mall_database
    import mall_promotions

    db_file = tmp_path / "m5_pw_coupon409.db"
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["MALL_DB_PATH"] = str(db_file)
    env["OBS_PULL_ENABLED"] = "0"
    env["MALL_MODE"] = "cloud"
    env["MALL_SCHEDULER_ENABLED"] = "0"
    env["MALL_ADMIN_EMAIL"] = ADMIN_EMAIL
    env["MALL_ADMIN_PASSWORD"] = ADMIN_PASS

    mall_database.set_db_path(str(db_file), init=True)
    mall_database.ensure_default_mall_admin()

    _seed_product(
        770,
        "퍼센트 쿠폰 테스트 니트",
        price=10000,
        options=[{"color": "블랙", "size": "M", "stock": 10}],
    )
    _seed_product(
        771,
        "최소주문 쿠폰 포인트 테스트 코트",
        price=25000,
        options=[{"color": "네이비", "size": "L", "stock": 10}],
    )
    assert mall_promotions.save_admin_coupon(
        {
            "code": "PCT10",
            "name": "10% 할인 쿠폰",
            "discount_type": "PERCENT",
            "discount_value": 10,
            "min_order_amount": 0,
            "is_active": True,
        }
    )["ok"]
    assert mall_promotions.save_admin_coupon(
        {
            "code": "MIN20K",
            "name": "2만원 이상 5천원 할인",
            "discount_type": "FIXED",
            "discount_value": 5000,
            "min_order_amount": 20000,
            "is_active": True,
        }
    )["ok"]

    member = _create_member("m5_pw_promo@test.com", name="쿠폰포인트회원", phone="010-9999-8888")
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        conn.execute("UPDATE mall_users SET points_balance = 5000 WHERE id = ?", (member["id"],))
        conn.commit()
        conn.close()

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
                # Scenario 1: Guest + 10% coupon (10,000 -> 20,000) recovers from 409 on 2nd click without reload
                ctx1 = browser.new_context(viewport={"width": 1440, "height": 900})
                page1 = ctx1.new_page()
                page1.on("dialog", lambda d: d.accept())
                page1.goto(f"{base_url}/", wait_until="networkidle")
                page1.evaluate(
                    """() => {
                        window.saveMallCart([
                            { num: 770, name: '퍼센트 쿠폰 테스트 니트', price: 10000, consumer_price: 25000, image_url: '', color: '블랙', size: 'M', quantity: 1 }
                        ]);
                    }"""
                )
                page1.goto(f"{base_url}/checkout", wait_until="networkidle")
                page1.fill("#coCouponCode", "PCT10")
                with page1.expect_response(lambda r: "/api/coupons/validate" in r.url and r.status == 200):
                    page1.click("#btnCoApplyCoupon")
                assert "-1,000원" in page1.locator("#coCouponDiscountAmount").inner_text()
                assert "13,000원" in page1.locator("#coTotalAmount").inner_text()

                # Price changes 10,000 -> 20,000 mid-checkout
                p770 = mall_database.get_mall_product_by_num(770)
                mall_database.update_mall_product(p770["id"], {"price": 20000})

                page1.fill("#coBuyerName", "퍼센트쿠폰구매자")
                page1.fill("#coBuyerPhone", "010-1212-3434")
                page1.fill("#coAddress", "서울시 강남구 테헤란로 100")
                page1.fill("#coGuestPassword", "pct1234")

                with page1.expect_response(lambda r: "/api/cart/revalidate" in r.url and r.request.method == "POST"):
                    with page1.expect_response(lambda r: "/api/orders/create" in r.url and r.request.method == "POST") as resp_409:
                        page1.click("#coSubmitBtn")
                assert resp_409.value.status == 409
                page1.wait_for_timeout(150)

                # Coupon discount & total must be re-synced to 20,000 - 2,000 + 4,000 = 22,000
                assert "20,000원" in page1.locator("#coSubtotalAmount").inner_text()
                assert "-2,000원" in page1.locator("#coCouponDiscountAmount").inner_text()
                assert "22,000원" in page1.locator("#coTotalAmount").inner_text()

                # Second click without reloading or re-applying coupon must succeed (200)
                with page1.expect_response(lambda r: "/api/orders/create" in r.url and r.request.method == "POST") as resp_200:
                    page1.click("#coSubmitBtn")
                assert resp_200.value.status == 200
                page1.wait_for_url("**/order-complete/**")
                ocode1 = page1.url.rstrip("/").split("/")[-1]
                ord1_list = mall_database.get_mall_order_by_code(ocode1)
                assert len(ord1_list) == 1
                ord1 = ord1_list[0]
                assert ord1["coupon_code"] == "PCT10"
                assert ord1["coupon_discount"] == 2000
                assert ord1["total_amount"] == 22000
                ctx1.close()

                # Scenario 2: Member + MIN20K coupon + 5000P, price drops 25,000 -> 4,000
                ctx2 = browser.new_context(viewport={"width": 1440, "height": 900})
                page2 = ctx2.new_page()
                page2.on("dialog", lambda d: d.accept())
                page2.goto(f"{base_url}/auth/login", wait_until="networkidle")
                page2.fill('input[name="email"]', "m5_pw_promo@test.com")
                page2.fill('input[name="password"]', "MemberPass123!")
                page2.click('button[type="submit"]')
                page2.wait_for_load_state("networkidle")

                page2.evaluate(
                    """() => {
                        window.saveMallCart([
                            { num: 771, name: '최소주문 쿠폰 포인트 테스트 코트', price: 25000, consumer_price: 40000, image_url: '', color: '네이비', size: 'L', quantity: 1 }
                        ]);
                    }"""
                )
                page2.goto(f"{base_url}/checkout", wait_until="networkidle")
                page2.fill("#coCouponCode", "MIN20K")
                with page2.expect_response(lambda r: "/api/coupons/validate" in r.url and r.status == 200):
                    page2.click("#btnCoApplyCoupon")
                page2.fill("#coPointsUsed", "5000")
                page2.dispatch_event("#coPointsUsed", "input")
                assert "-5,000원" in page2.locator("#coCouponDiscountAmount").inner_text()
                assert "-5,000P" in page2.locator("#coPointsUsedAmount").inner_text()
                assert "19,000원" in page2.locator("#coTotalAmount").inner_text()

                # Price drops 25,000 -> 4,000 (invalidates MIN20K, clamps points 5000 -> 4000)
                p771 = mall_database.get_mall_product_by_num(771)
                mall_database.update_mall_product(p771["id"], {"price": 4000})

                with page2.expect_response(lambda r: "/api/cart/revalidate" in r.url and r.request.method == "POST"):
                    with page2.expect_response(lambda r: "/api/orders/create" in r.url and r.request.method == "POST") as resp2_409:
                        page2.click("#coSubmitBtn")
                assert resp2_409.value.status == 409
                page2.wait_for_timeout(150)

                assert "4,000원" in page2.locator("#coSubtotalAmount").inner_text()
                assert page2.locator("#coCouponDiscountRow").evaluate("el => el.classList.contains('ax-is-hidden')") is True
                assert page2.locator("#coPointsUsed").input_value() == "4000"
                assert "4,000원" in page2.locator("#coTotalAmount").inner_text()
                assert "최소 주문 금액" in page2.locator("#coCouponMessage").inner_text()

                with page2.expect_response(lambda r: "/api/orders/create" in r.url and r.request.method == "POST") as resp2_200:
                    page2.click("#coSubmitBtn")
                assert resp2_200.value.status == 200
                page2.wait_for_url("**/order-complete/**")
                ocode2 = page2.url.rstrip("/").split("/")[-1]
                ord2_list = mall_database.get_mall_order_by_code(ocode2)
                assert len(ord2_list) == 1
                ord2 = ord2_list[0]
                assert ord2["coupon_code"] == ""
                assert ord2["coupon_discount"] == 0
                assert ord2["points_used"] == 4000
                assert ord2["total_amount"] == 4000
                ctx2.close()
            finally:
                browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
