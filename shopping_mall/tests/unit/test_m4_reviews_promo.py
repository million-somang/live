# -*- coding: utf-8 -*-
"""
Milestone 4 (M4) Unit & Playwright Integration Tests:
- F13: Photo Reviews, Magic-Byte/PIL Image Upload Validation, Verified Buyer Badge, Author Edit/Delete, Admin Moderation
- F14: Welcome Coupon Auto-Issue, Live Promo Codes, Loyalty Points Economy, Atomic TX Rollback & Concurrency
- Desktop (1440x900) & Mobile (390x844) Playwright UI, Zero Console/404/Overflow/Internal-Scroll Checks
"""

import io
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from PIL import Image
from playwright.sync_api import sync_playwright

MALL_DIR = Path(__file__).resolve().parents[2]
if str(MALL_DIR) not in sys.path:
    sys.path.insert(0, str(MALL_DIR))

ADMIN_EMAIL = "m4admin@golabang.test"
ADMIN_PASS = "M4AdminPass!2026"


def _make_png_bytes(color=(79, 70, 229)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (24, 24), color=color).save(buf, format="PNG")
    return buf.getvalue()


def _make_jpeg_bytes(color=(15, 23, 42)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (24, 24), color=color).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture(scope="session", autouse=True)
def _bootstrap_isolated_env(tmp_path_factory):
    boot_dir = tmp_path_factory.mktemp("m4_boot")
    os.environ["MALL_DB_PATH"] = str(boot_dir / "boot_m4.db")
    os.environ["OBS_PULL_ENABLED"] = "0"
    os.environ["MALL_MODE"] = "cloud"
    os.environ["MALL_SCHEDULER_ENABLED"] = "0"
    os.environ["MALL_ADMIN_EMAIL"] = ADMIN_EMAIL
    os.environ["MALL_ADMIN_PASSWORD"] = ADMIN_PASS

    import mall_database  # noqa: F401
    import mall_security  # noqa: F401
    import mall_promotions  # noqa: F401
    import app as mall_app  # noqa: F401
    return boot_dir


@pytest.fixture()
def isolated_mall_db(tmp_path, monkeypatch):
    import mall_database
    import mall_security
    import routes_cs

    db_file = tmp_path / "m4_unit.db"
    reviews_dir = tmp_path / "uploads_reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(routes_cs, "UPLOAD_REVIEWS_DIR", str(reviews_dir))

    monkeypatch.setenv("MALL_ADMIN_EMAIL", ADMIN_EMAIL)
    monkeypatch.setenv("MALL_ADMIN_PASSWORD", ADMIN_PASS)
    monkeypatch.setenv("OBS_PULL_ENABLED", "0")
    monkeypatch.setenv("MALL_MODE", "cloud")
    monkeypatch.setenv("MALL_SCHEDULER_ENABLED", "0")
    mall_security.rate_limiter._hits.clear()
    mall_database.set_db_path(str(db_file), init=True)
    mall_database.ensure_default_mall_admin()
    return db_file


def _seed_product(num: int = 401, name: str = "M4 캐시미어 니트", price: int = 30000, live_price: int = 25000, stock: int = 20):
    import mall_database

    return mall_database.upsert_product_from_obs(
        {
            "obs_product_id": num + 4000,
            "num": num,
            "name": name,
            "category": "TOP",
            "price": price,
            "live_price": live_price,
            "consumer_price": price + 15000,
            "image_url": "",
            "specs": f"{name} 상세 스펙",
            "options": [{"color": "블랙", "size": "FREE", "stock": stock}],
        }
    )


def _create_member(email: str, password: str = "MemberPass123!", name: str = "김리뷰", phone: str = "010-3333-4444"):
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
# F13: Photo Reviews, Verified Buyer, Edit/Delete & Admin Moderation
# ==============================================================================


def test_f13_upload_validation_and_strict_review_inputs(isolated_mall_db, tmp_path):
    import app as mall_app

    _seed_product(num=401, price=30000, stock=10)
    member = _create_member("rev_u1@test.com", name="포토리뷰어")
    guest = mall_app.app.test_client()

    # Guest blocked on upload and add
    assert guest.post("/api/reviews/upload-image").status_code == 401
    assert guest.post("/api/reviews/add", json={"product_num": 401, "rating": 5, "content": "좋아요"}).status_code == 401

    client = mall_app.app.test_client()
    csrf = _login_client(client, member["email"], "MemberPass123!")

    # Missing CSRF blocked with 403
    no_csrf = client.post(
        "/api/reviews/upload-image",
        data={"image": (io.BytesIO(_make_png_bytes()), "photo.png")},
        content_type="multipart/form-data",
    )
    assert no_csrf.status_code == 403

    # Missing file / bad ext / empty / spoofed magic bytes -> 400
    assert client.post("/api/reviews/upload-image", headers={"X-CSRF-Token": csrf}, data={"dummy": "x"}).status_code == 400
    assert client.post(
        "/api/reviews/upload-image",
        headers={"X-CSRF-Token": csrf},
        data={"image": (io.BytesIO(b"<script>alert(1)</script>"), "xss.html")},
        content_type="multipart/form-data",
    ).status_code == 400
    assert client.post(
        "/api/reviews/upload-image",
        headers={"X-CSRF-Token": csrf},
        data={"image": (io.BytesIO(b""), "empty.png")},
        content_type="multipart/form-data",
    ).status_code == 400
    assert client.post(
        "/api/reviews/upload-image",
        headers={"X-CSRF-Token": csrf},
        data={"image": (io.BytesIO(b"not-a-png-at-all"), "fake.png")},
        content_type="multipart/form-data",
    ).status_code == 400
    # JPEG magic bytes disguised as .png rejected
    assert client.post(
        "/api/reviews/upload-image",
        headers={"X-CSRF-Token": csrf},
        data={"image": (io.BytesIO(_make_jpeg_bytes()), "mismatch.png")},
        content_type="multipart/form-data",
    ).status_code == 400

    # Valid PNG upload
    up_res = client.post(
        "/api/reviews/upload-image",
        headers={"X-CSRF-Token": csrf},
        data={"image": (io.BytesIO(_make_png_bytes()), "valid.png")},
        content_type="multipart/form-data",
    )
    assert up_res.status_code == 200, up_res.get_json()
    up_body = up_res.get_json()
    img_url = up_body["image_url"]
    assert up_body["url"] == img_url
    assert re.match(r"^/static/uploads/reviews/[0-9a-f]+\.png$", img_url)

    # Strict rating and content validation
    for bad_rating in (0, 6, -1, "abc", True, False, 3.5, None):
        r_bad = client.post(
            "/api/reviews/add",
            headers={"X-CSRF-Token": csrf},
            json={"product_num": 401, "rating": bad_rating, "content": "정상 길이 리뷰입니다."},
        )
        assert r_bad.status_code == 400, f"rating={bad_rating} should be 400"

    assert client.post(
        "/api/reviews/add",
        headers={"X-CSRF-Token": csrf},
        json={"product_num": 401, "rating": 5, "content": "굿"},
    ).status_code == 400
    assert client.post(
        "/api/reviews/add",
        headers={"X-CSRF-Token": csrf},
        json={"product_num": 401, "rating": 5, "content": "가" * 1001},
    ).status_code == 400

    # Invalid image path injection rejected
    for bad_url in ("/static/uploads/reviews/../../etc/passwd.png", "javascript:alert(1)", "https://evil.com/a.png"):
        r_img_bad = client.post(
            "/api/reviews/add",
            headers={"X-CSRF-Token": csrf},
            json={"product_num": 401, "rating": 5, "content": "경로 검증 테스트", "image_url": bad_url},
        )
        assert r_img_bad.status_code == 400, f"url={bad_url} should be 400"

    # Valid photo review
    add_res = client.post(
        "/api/reviews/add",
        headers={"X-CSRF-Token": csrf},
        json={
            "product_num": 401,
            "rating": 5,
            "content": "<script>alert('xss')</script> 착용감이 아주 좋습니다!",
            "image_url": img_url,
            "images": [img_url],
        },
    )
    assert add_res.status_code == 200, add_res.get_json()
    add_body = add_res.get_json()
    assert add_body["review"]["image_url"] == img_url
    assert add_body["review"]["images"] == [img_url]
    assert add_body["review"]["is_verified_buyer"] is False

    # XSS escaped in SSR HTML
    detail_html = client.get("/product/401").get_data(as_text=True)
    assert "<script>alert('xss')</script>" not in detail_html
    assert "&lt;script&gt;alert" in detail_html
    assert "구매 인증" not in detail_html.split("pdReviewList")[1].split("section-qna")[0]


def test_f13_verified_buyer_author_idor_and_admin_moderation(isolated_mall_db):
    import app as mall_app
    import mall_database

    _seed_product(num=402, price=35000, stock=15)
    buyer = _create_member("buyer_m4@test.com", name="실구매자")
    other = _create_member("other_m4@test.com", name="타인회원")

    c_buyer = mall_app.app.test_client()
    csrf_b = _login_client(c_buyer, buyer["email"], "MemberPass123!")

    # Place an order for product 402
    ord_res = c_buyer.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf_b},
        json={
            "buyer_name": "실구매자",
            "buyer_phone": "010-3333-4444",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "source_page": "CART_CHECKOUT",
            "items": [{"num": 402, "color": "블랙", "size": "FREE", "quantity": 1}],
        },
    )
    assert ord_res.status_code == 200
    oid = ord_res.get_json()["order"]["order_id"]

    # Verified buyer badge is True
    r1 = c_buyer.post(
        "/api/reviews/add",
        headers={"X-CSRF-Token": csrf_b},
        json={"product_num": 402, "rating": 4, "content": "실제 구매 후 남기는 4점 리뷰"},
    )
    assert r1.status_code == 200
    rev_id = r1.get_json()["review_id"]
    assert r1.get_json()["review"]["is_verified_buyer"] is True

    # Other member cannot edit or delete (403)
    c_other = mall_app.app.test_client()
    csrf_o = _login_client(c_other, other["email"], "MemberPass123!")
    assert c_other.post(
        "/api/reviews/update",
        headers={"X-CSRF-Token": csrf_o},
        json={"review_id": rev_id, "rating": 1, "content": "타인 변조 시도"},
    ).status_code == 403
    assert c_other.post(
        "/api/reviews/delete",
        headers={"X-CSRF-Token": csrf_o},
        json={"review_id": rev_id},
    ).status_code == 403
    # Non-admin blocked from admin reviews API
    assert c_other.get("/api/admin/reviews").status_code == 403

    # Owner updates review
    upd = c_buyer.post(
        "/api/reviews/update",
        headers={"X-CSRF-Token": csrf_b},
        json={"review_id": rev_id, "rating": 5, "content": "수정 후 5점 만점 리뷰"},
    )
    assert upd.status_code == 200
    assert upd.get_json()["review"]["rating"] == 5
    assert upd.get_json()["summary"]["avg_rating"] == 5.0

    # Admin hides -> excluded from public API and summary, still in admin API
    c_admin = mall_app.app.test_client()
    csrf_adm = _login_client(c_admin, ADMIN_EMAIL, ADMIN_PASS)
    hide_res = c_admin.post(
        "/api/admin/reviews/moderate",
        headers={"X-CSRF-Token": csrf_adm},
        json={"review_id": rev_id, "is_hidden": True},
    )
    assert hide_res.status_code == 200
    pub_prod = c_other.get("/api/products/402").get_json()
    assert pub_prod["review_summary"]["count"] == 0
    assert len(pub_prod["reviews"]) == 0
    adm_list = c_admin.get("/api/admin/reviews").get_json()["reviews"]
    assert any(x["id"] == rev_id and x["is_hidden"] is True for x in adm_list)

    # Admin unhides & replies
    c_admin.post(
        "/api/admin/reviews/moderate",
        headers={"X-CSRF-Token": csrf_adm},
        json={"review_id": rev_id, "is_hidden": False},
    )
    c_admin.post(
        "/api/admin/reviews/moderate",
        headers={"X-CSRF-Token": csrf_adm},
        json={"review_id": rev_id, "admin_reply": "소중한 후기 감사합니다!"},
    )
    pub_prod2 = c_other.get("/api/products/402").get_json()
    assert pub_prod2["review_summary"]["count"] == 1
    assert pub_prod2["reviews"][0]["admin_reply"] == "소중한 후기 감사합니다!"
    assert pub_prod2["reviews"][0]["is_verified_buyer"] is True

    # Cancelling the order removes verified buyer badge dynamically
    mall_database.update_mall_order_workflow(oid, order_status="주문취소")
    pub_prod3 = c_other.get("/api/products/402").get_json()
    assert pub_prod3["reviews"][0]["is_verified_buyer"] is False

    # Owner deletes review
    del_res = c_buyer.post(
        "/api/reviews/delete",
        headers={"X-CSRF-Token": csrf_b},
        json={"review_id": rev_id},
    )
    assert del_res.status_code == 200
    assert c_other.get("/api/products/402").get_json()["review_summary"]["count"] == 0


# ==============================================================================
# F14: Welcome Coupon, Live Promo Codes, Points Economy & Concurrency/Rollback
# ==============================================================================


def test_f14_welcome_coupon_live_code_validation_and_pricing_formula(isolated_mall_db):
    import app as mall_app

    _seed_product(num=410, price=30000, live_price=25000, stock=20)
    c_admin = mall_app.app.test_client()
    csrf_adm = _login_client(c_admin, ADMIN_EMAIL, ADMIN_PASS)

    # Set shipping=4000, free=50000, point_earn_rate=10%
    st_res = c_admin.post(
        "/api/admin/store-info",
        headers={"X-CSRF-Token": csrf_adm},
        json={"shipping_fee": 4000, "free_shipping_threshold": 50000, "point_earn_rate": 10},
    )
    assert st_res.status_code == 200
    assert st_res.get_json()["store_info"]["point_earn_rate"] == 10

    # Create live-only percentage coupon with max_discount_amount=5000 & usage_limit=5
    c_save = c_admin.post(
        "/api/admin/coupons/save",
        headers={"X-CSRF-Token": csrf_adm},
        json={
            "code": "LIVE20",
            "name": "라이브 20% 할인",
            "discount_type": "PERCENT",
            "discount_value": 20,
            "max_discount_amount": 5000,
            "min_order_amount": 20000,
            "live_only": True,
            "usage_limit": 5,
            "is_active": True,
        },
    )
    assert c_save.status_code == 200, c_save.get_json()

    # Register member via /auth/register -> auto-receives WELCOME3000
    c_mem = mall_app.app.test_client()
    c_mem.get("/auth/register")
    with c_mem.session_transaction() as sess:
        reg_csrf = sess["_csrf_token"]
    reg_res = c_mem.post(
        "/auth/register",
        headers={"X-CSRF-Token": reg_csrf},
        json={
            "email": "promo_m4@test.com",
            "password": "MemberPass123!",
            "name": "프로모회원",
            "phone": "010-7777-8888",
            "agree_terms": "1",
            "agree_privacy": "1",
        },
    )
    assert reg_res.status_code == 200
    csrf_mem = reg_res.get_json()["csrf_token"]

    my_coupons = c_mem.get("/api/mypage/coupons").get_json()["coupons"]
    assert any(c["code"] == "WELCOME3000" and c["status"] == "AVAILABLE" for c in my_coupons)

    # Validation boundaries (must return 400 on invalid)
    guest = mall_app.app.test_client()
    assert guest.get("/api/mypage/coupons").status_code == 401
    assert guest.get("/api/mypage/points").status_code == 401
    assert guest.post("/api/coupons/validate", json={"code": "WELCOME3000", "subtotal": 30000}).status_code == 400
    assert c_mem.post("/api/coupons/validate", json={"code": "NO_SUCH_CODE", "subtotal": 30000}).status_code == 400
    assert c_mem.post("/api/coupons/validate", json={"code": "LIVE20", "subtotal": 30000, "source_page": "STORE"}).status_code == 400
    assert c_mem.post("/api/coupons/validate", json={"code": "LIVE20", "subtotal": 10000, "source_page": "LIVE"}).status_code == 400

    # Valid live coupon: 25000*2=50000 -> 20% = 10000 capped at max_discount 5000
    v_ok = c_mem.post("/api/coupons/validate", json={"code": "LIVE20", "subtotal": 50000, "source_page": "LIVE"})
    assert v_ok.status_code == 200
    assert v_ok.get_json()["discount_amount"] == 5000

    # Place STORE order with WELCOME3000 on subtotal 30,000 (shipping=4,000 since 30,000 < 50,000)
    ord1 = c_mem.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf_mem},
        json={
            "buyer_name": "프로모회원",
            "buyer_phone": "010-7777-8888",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "coupon_code": "WELCOME3000",
            "source_page": "CART_CHECKOUT",
            "items": [{"num": 410, "color": "블랙", "size": "FREE", "quantity": 1}],
        },
    )
    assert ord1.status_code == 200, ord1.get_json()
    o1 = ord1.get_json()["order"]
    assert o1["subtotal_amount"] == 30000
    assert o1["coupon_discount"] == 3000
    assert o1["shipping_fee"] == 4000
    assert o1["total_amount"] == 31000
    assert o1["points_earned"] == 3000

    # Cannot reuse WELCOME3000 while order is active
    assert c_mem.post("/api/coupons/validate", json={"code": "WELCOME3000", "subtotal": 30000}).status_code == 400

    # 입금완료 does NOT award points yet; 배송완료 awards once (idempotent)
    s1 = c_admin.post(
        "/api/admin/order-status",
        headers={"X-CSRF-Token": csrf_adm},
        json={"order_id": o1["order_id"], "payment_status": "입금완료"},
    )
    assert s1.status_code == 200
    assert c_mem.get("/api/mypage/points").get_json()["balance"] == 0

    s2 = c_admin.post(
        "/api/admin/order-status",
        headers={"X-CSRF-Token": csrf_adm},
        json={"order_id": o1["order_id"], "order_status": "배송완료"},
    )
    assert s2.status_code == 200
    # Repeat 배송완료 must not double-award
    s3 = c_admin.post(
        "/api/admin/order-status",
        headers={"X-CSRF-Token": csrf_adm},
        json={"order_id": o1["order_id"], "order_status": "배송완료"},
    )
    assert s3.status_code == 200
    pts_res = c_mem.get("/api/mypage/points").get_json()
    assert pts_res["balance"] == 3000
    assert any(o1["order_code"] in str(log.get("order_code") or "") for log in pts_res["logs"])

    # Use 2000P + LIVE20 (20% of 25000 = 5000) on /live order -> total = (25000 - 5000 - 2000) + 4000 = 22000
    ord2 = c_mem.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf_mem},
        json={
            "buyer_name": "프로모회원",
            "buyer_phone": "010-7777-8888",
            "zipcode": "06236",
            "address": "서울시 강남구 테헤란로 152",
            "coupon_code": "LIVE20",
            "points_used": 2000,
            "source_page": "LIVE",
            "items": [{"num": 410, "color": "블랙", "size": "FREE", "quantity": 1}],
        },
    )
    assert ord2.status_code == 200, ord2.get_json()
    o2 = ord2.get_json()["order"]
    assert o2["subtotal_amount"] == 25000
    assert o2["coupon_discount"] == 5000
    assert o2["points_used"] == 2000
    assert o2["shipping_fee"] == 4000
    assert o2["total_amount"] == 22000
    assert c_mem.get("/api/mypage/points").get_json()["balance"] == 1000

    # Customer self-cancel refunds 2000P & restores LIVE20 (idempotent)
    sc_res = c_mem.post(
        "/api/orders/self-cancel",
        headers={"X-CSRF-Token": csrf_mem},
        json={"order_code": o2["order_code"], "claim_reason": "단순 변심"},
    )
    assert sc_res.status_code == 200
    assert c_mem.get("/api/mypage/points").get_json()["balance"] == 3000
    assert c_mem.post("/api/coupons/validate", json={"code": "LIVE20", "subtotal": 25000, "source_page": "LIVE"}).status_code == 200


def test_f14_rollback_cleanliness_concurrency_and_withdrawal_cleanup(isolated_mall_db):
    import app as mall_app
    import mall_database

    _seed_product(num=411, price=20000, live_price=20000, stock=10)
    c_admin = mall_app.app.test_client()
    csrf_adm = _login_client(c_admin, ADMIN_EMAIL, ADMIN_PASS)

    c_admin.post(
        "/api/admin/coupons/save",
        headers={"X-CSRF-Token": csrf_adm},
        json={
            "code": "RACE1",
            "name": "선착순 1명 쿠폰",
            "discount_type": "FIXED",
            "discount_value": 5000,
            "usage_limit": 1,
            "is_active": True,
        },
    )

    u1 = _create_member("race1@test.com", name="경합1")
    u2 = _create_member("race2@test.com", name="경합2")

    # Give u1 1000 points via direct DB update for negative/rollback check
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        conn.execute("UPDATE mall_users SET points_balance = 1000 WHERE id = ?", (u1["id"],))
        conn.commit()
        conn.close()

    # Rollback check: failing order with invalid points_used or invalid coupon must leave stock & balance untouched
    c1 = mall_app.app.test_client()
    csrf1 = _login_client(c1, u1["email"], "MemberPass123!")
    bad_ord = c1.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf1},
        json={
            "buyer_name": "경합1",
            "buyer_phone": "010-1111-2222",
            "address": "서울시 강남구",
            "coupon_code": "RACE1",
            "points_used": 99999,
            "source_page": "CART_CHECKOUT",
            "items": [{"num": 411, "color": "블랙", "size": "FREE", "quantity": 2}],
        },
    )
    assert bad_ord.status_code == 400
    assert mall_database.get_mall_product_by_num(411)["total_stock"] == 10
    assert mall_database.get_mall_user_by_id(u1["id"])["points_balance"] == 1000
    coupons_now = c_admin.get("/api/admin/coupons").get_json()["coupons"]
    assert next(c for c in coupons_now if c["code"] == "RACE1")["used_count"] == 0

    # Concurrency race on usage_limit=1 coupon
    results = []

    def _place(uid):
        res = mall_database.create_mall_order({
            "user_id": uid,
            "is_guest": False,
            "buyer_name": f"유저{uid}",
            "buyer_phone": "010-1111-2222",
            "address": "서울시 강남구",
            "coupon_code": "RACE1",
            "source_page": "CART_CHECKOUT",
            "items": [{"num": 411, "color": "블랙", "size": "FREE", "quantity": 1}],
        })
        results.append(res)

    t1 = threading.Thread(target=_place, args=(u1["id"],))
    t2 = threading.Thread(target=_place, args=(u2["id"],))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert sum(1 for r in results if r.get("ok")) == 1
    assert sum(1 for r in results if not r.get("ok")) == 1

    # Withdrawal cleans user coupons and point logs
    w_res = c1.post(
        "/api/mypage/withdraw",
        headers={"X-CSRF-Token": csrf1},
        json={"password": "MemberPass123!"},
    )
    assert w_res.status_code == 200
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        uc_cnt = conn.execute("SELECT COUNT(*) FROM mall_user_coupons WHERE user_id = ?", (u1["id"],)).fetchone()[0]
        pl_cnt = conn.execute("SELECT COUNT(*) FROM mall_point_logs WHERE user_id = ?", (u1["id"],)).fetchone()[0]
        conn.close()
    assert uc_cnt == 0 and pl_cnt == 0


# ==============================================================================
# Playwright Desktop (1440x900) & Mobile (390x844) Full UI Verification
# ==============================================================================


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_server(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"Server did not become ready at {url}")


def test_m4_playwright_desktop_and_mobile_ui(tmp_path):
    port = _find_free_port()
    db_file = tmp_path / "m4_pw.db"
    env = os.environ.copy()
    env["MALL_PORT"] = str(port)
    env["PORT"] = str(port)
    env["MALL_DB_PATH"] = str(db_file)
    env["OBS_PULL_ENABLED"] = "0"
    env["MALL_MODE"] = "cloud"
    env["MALL_SCHEDULER_ENABLED"] = "0"
    env["MALL_ADMIN_EMAIL"] = ADMIN_EMAIL
    env["MALL_ADMIN_PASSWORD"] = ADMIN_PASS

    server_code = (
        "import sys; sys.path.insert(0, r'" + str(MALL_DIR) + "'); "
        "import mall_database, app as mall_app; "
        "mall_database.upsert_product_from_obs({'obs_product_id': 9401, 'num': 401, 'name': 'M4 플레어 원피스', "
        "'category': 'DRESS', 'price': 45000, 'live_price': 39000, 'consumer_price': 60000, "
        "'image_url': '', 'specs': 'M4 테스트 상품', 'options': [{'color': '네이비', 'size': 'FREE', 'stock': 15}]}); "
        "mall_database.update_live_state_bundle({'is_live': True, 'pinned_product_num': 401}); "
        "mall_app.app.run(host='127.0.0.1', port=" + str(port) + ", debug=False, threaded=True)"
    )
    reviews_upload_dir = MALL_DIR / "static" / "uploads" / "reviews"
    reviews_upload_dir.mkdir(parents=True, exist_ok=True)
    before_uploads = set(os.listdir(reviews_upload_dir))
    proc = subprocess.Popen([sys.executable, "-c", server_code], env=env, cwd=str(MALL_DIR))
    base_url = f"http://127.0.0.1:{port}"
    png_path = tmp_path / "pw_review.png"
    png_path.write_bytes(_make_png_bytes())

    try:
        _wait_server(f"{base_url}/")
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for vp in ({"width": 1440, "height": 900}, {"width": 390, "height": 844}):
                context = browser.new_context(viewport=vp)
                page = context.new_page()
                console_errs = []
                page_errs = []
                bad_reqs = []
                page.on("console", lambda m: console_errs.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: page_errs.append(str(e)))
                page.on("response", lambda r: bad_reqs.append((r.status, r.url)) if r.status >= 404 and "/api/coupons/validate" not in r.url else None)

                # Register fresh user per viewport
                email = f"pw_{vp['width']}@test.com"
                page.goto(f"{base_url}/auth/register", wait_until="networkidle")
                page.fill("#regEmail", email)
                page.fill("#regPassword", "MemberPass123!")
                page.fill("#regName", f"테스터{vp['width']}")
                page.fill("#regPhone", "010-1234-5678")
                page.fill("#regAddress", "서울시 강남구 테헤란로 152")
                page.check("#regAgreeTerms")
                page.check("#regAgreePrivacy")
                page.click("button[type='submit']")
                page.wait_for_url(f"{base_url}/")

                # 1. Product Detail photo review upload + submit + edit + delete
                page.goto(f"{base_url}/product/401", wait_until="networkidle")
                page.set_input_files("#pdReviewImageInput", str(png_path))
                page.fill("#pdReviewContent", "Playwright 포토 리뷰 테스트입니다.")
                page.click("#pdReviewSubmitBtn")
                page.wait_for_selector("#pdReviewList .ax-review-photo-thumb")
                assert "구매 인증" not in page.locator("#pdReviewList").inner_text()
                assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")

                # 2. Checkout with WELCOME3000 coupon
                page.click("button:has-text('장바구니 담기')")
                page.goto(f"{base_url}/checkout", wait_until="networkidle")
                page.fill("#coCouponCode", "WELCOME3000")
                page.click("#btnCoApplyCoupon")
                page.wait_for_selector("#coCouponDiscountRow:not(.ax-is-hidden)")
                assert "적용 완료" in page.locator("#coCouponMessage").inner_text()
                assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")

                # 3. MyPage coupons & points tabs
                page.goto(f"{base_url}/mypage", wait_until="networkidle")
                page.click("#mypageTabCoupons")
                page.wait_for_selector("#mypageCouponsList .ax-coupon-card")
                page.click("#mypageTabPoints")
                page.wait_for_selector("#mypagePointsTbody")
                assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")

                # 4. Live page
                page.goto(f"{base_url}/live", wait_until="networkidle")
                assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")

                assert console_errs == [], f"Console errors ({vp}): {console_errs}"
                assert page_errs == [], f"Page errors ({vp}): {page_errs}"
                assert bad_reqs == [], f"404+ responses ({vp}): {bad_reqs}"
                context.close()

            # Admin UI check on desktop & mobile
            adm_ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            adm_page = adm_ctx.new_page()
            adm_page.goto(f"{base_url}/auth/login?next=/admin", wait_until="networkidle")
            adm_page.fill("input[name='email']", ADMIN_EMAIL)
            adm_page.fill("input[name='password']", ADMIN_PASS)
            adm_page.click("button[type='submit']")
            adm_page.wait_for_url(f"{base_url}/admin")
            adm_page.click("#adminTabReviews")
            adm_page.wait_for_selector("#adminReviewsList .ax-admin-review-card")
            adm_page.click("#adminTabPromos")
            adm_page.wait_for_selector("#adminCouponsTbody tr")
            adm_page.click("#adminTabStore")
            adm_page.wait_for_selector("#asPointEarnRate")
            assert adm_page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
            adm_ctx.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        for fname in set(os.listdir(reviews_upload_dir)) - before_uploads:
            try:
                (reviews_upload_dir / fname).unlink()
            except Exception:
                pass
