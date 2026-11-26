# -*- coding: utf-8 -*-
"""
Milestone 7 Tier 5 — Adversarial Security, Concurrency, Boundary & Hygiene Regression Suite.
Runs strictly against isolated tmp_path SQLite DBs and isolated upload/backup dirs.
"""

import base64
from datetime import datetime, timedelta
from io import BytesIO
import os
import re
import sys
import threading
from pathlib import Path

import pytest

MALL_DIR = Path(__file__).resolve().parents[2]
if str(MALL_DIR) not in sys.path:
    sys.path.insert(0, str(MALL_DIR))

ADMIN_EMAIL = "t5admin@golabang.test"
ADMIN_PASS = "T5AdminPass!2026"

_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture(scope="session", autouse=True)
def _bootstrap_t5_env(tmp_path_factory):
    boot_dir = tmp_path_factory.mktemp("t5_boot")
    os.environ["MALL_DB_PATH"] = str(boot_dir / "t5_boot.db")
    os.environ["MALL_BACKUP_DIR"] = str(boot_dir / "backups")
    os.environ["MALL_PRODUCT_UPLOAD_DIR"] = str(boot_dir / "uploads_products")
    os.environ["MALL_REVIEW_UPLOAD_DIR"] = str(boot_dir / "uploads_reviews")
    os.environ["OBS_PULL_ENABLED"] = "0"
    os.environ["MALL_MODE"] = "cloud"
    os.environ["MALL_SCHEDULER_ENABLED"] = "0"
    os.environ["MALL_ADMIN_EMAIL"] = ADMIN_EMAIL
    os.environ["MALL_ADMIN_PASSWORD"] = ADMIN_PASS
    os.environ["MALL_ALLOW_PRIVATE_PG_ENDPOINTS"] = "0"

    import mall_database  # noqa: F401
    import mall_security  # noqa: F401
    import app as mall_app  # noqa: F401
    return boot_dir


@pytest.fixture()
def isolated_t5_env(tmp_path, monkeypatch):
    import app as mall_app
    import mall_database
    import mall_ops
    import mall_security
    import routes_cs

    db_file = tmp_path / "t5_unit.db"
    prod_uploads = tmp_path / "uploads_products"
    rev_uploads = tmp_path / "uploads_reviews"
    backup_dir = tmp_path / "backups"
    prod_uploads.mkdir(parents=True, exist_ok=True)
    rev_uploads.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("MALL_ADMIN_EMAIL", ADMIN_EMAIL)
    monkeypatch.setenv("MALL_ADMIN_PASSWORD", ADMIN_PASS)
    monkeypatch.setenv("OBS_PULL_ENABLED", "0")
    monkeypatch.setenv("MALL_MODE", "cloud")
    monkeypatch.setenv("MALL_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("MALL_PRODUCT_UPLOAD_DIR", str(prod_uploads))
    monkeypatch.setenv("MALL_REVIEW_UPLOAD_DIR", str(rev_uploads))
    monkeypatch.setenv("MALL_BACKUP_DIR", str(backup_dir))
    monkeypatch.setattr(mall_app, "UPLOAD_PRODUCTS_DIR", str(prod_uploads))
    monkeypatch.setattr(routes_cs, "UPLOAD_REVIEWS_DIR", str(rev_uploads))

    mall_app.app.config["TESTING"] = True
    mall_app.app.config["PROPAGATE_EXCEPTIONS"] = False
    mall_security.rate_limiter._hits.clear()
    mall_database.set_db_path(str(db_file), init=True)
    mall_database.ensure_default_mall_admin()
    return tmp_path


def _register_and_login(client, email: str, name: str, phone: str, password: str = "Password123!"):
    import mall_database

    res = mall_database.create_mall_user(email, password, name, phone)
    assert res["ok"], res
    login = client.post("/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.get_json()
    return login.get_json()["csrf_token"], int(res["user"]["id"])


def _login_admin(client) -> str:
    login = client.post("/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS})
    assert login.status_code == 200, login.get_json()
    return login.get_json()["csrf_token"]


def _seed_product(
    name: str = "테스트상품",
    price: int = 50000,
    live_price: int = 30000,
    cost_price: int = 12345,
    stock: int = 50,
    supplier_id: int | None = None,
):
    import mall_database

    res = mall_database.create_mall_product({
        "name": name,
        "price": price,
        "live_price": live_price,
        "cost_price": cost_price,
        "supplier_id": supplier_id,
        "options": [{"color": "블랙", "size": "FREE", "stock": stock}],
    })
    assert res["ok"], res
    return res["product"]


def test_t5_01_live_order_requires_csrf_for_authenticated_user(isolated_t5_env):
    import app as mall_app
    import mall_database

    prod = _seed_product()
    c = mall_app.app.test_client()
    csrf, uid = _register_and_login(c, "t5_csrf@test.local", "CSRF테스트", "010-1000-0001")
    with mall_database.get_connection() as conn:
        conn.execute("UPDATE mall_users SET points_balance = 5000 WHERE id = ?", (uid,))
        conn.commit()

    # Missing X-CSRF-Token MUST be rejected 403 even when source_page == "LIVE"
    bad = c.post("/api/orders/create", json={
        "source_page": "LIVE",
        "items": [{"num": prod["num"], "color": "블랙", "size": "FREE", "quantity": 1}],
        "buyer_name": "피해자",
        "buyer_phone": "010-1000-0001",
        "address": "서울시 강남구",
        "payment_method": "BANK",
        "points_used": 5000,
    })
    assert bad.status_code == 403
    assert mall_database.get_mall_user_by_id(uid)["points_balance"] == 5000

    # Valid X-CSRF-Token without live_order_token remains backwards-compatible
    ok = c.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf},
        json={
            "source_page": "LIVE",
            "items": [{"num": prod["num"], "color": "블랙", "size": "FREE", "quantity": 1}],
            "buyer_name": "정상회원",
            "buyer_phone": "010-1000-0001",
            "address": "서울시 강남구",
            "payment_method": "BANK",
            "points_used": 5000,
        },
    )
    assert ok.status_code == 200


def test_t5_02_password_change_invalidates_active_reset_tokens(isolated_t5_env):
    import app as mall_app
    import mall_database
    import mall_member_service

    c = mall_app.app.test_client()
    csrf, _ = _register_and_login(c, "t5_pw@test.local", "비번유저", "010-1000-0002", password="OldPassword1!")
    req = mall_member_service.request_password_reset("t5_pw@test.local")
    assert req["ok"]

    with mall_database.get_connection() as conn:
        row = conn.execute(
            "SELECT body FROM mall_notification_logs WHERE recipient = ? ORDER BY id DESC LIMIT 1",
            ("t5_pw@test.local",),
        ).fetchone()
    tok = re.search(r"token=([A-Za-z0-9_\-]+)", row["body"]).group(1)
    code = re.search(r"6자리 임시 인증 코드:\s*(\d{6})", row["body"]).group(1)

    chg = c.post(
        "/api/mypage/password",
        headers={"X-CSRF-Token": csrf},
        json={
            "current_password": "OldPassword1!",
            "new_password": "NewPassword2@",
            "new_password_confirm": "NewPassword2@",
        },
    )
    assert chg.status_code == 200

    anon = mall_app.app.test_client()
    r_get = anon.get("/auth/reset-password")
    page_csrf = re.search(r'name="csrf-token"\s+content="([^"]+)"', r_get.get_data(as_text=True)).group(1)
    replay = anon.post(
        "/auth/reset-password",
        data={
            "csrf_token": page_csrf,
            "token": tok,
            "code": code,
            "new_password": "AttackerPassword3#",
            "new_password_confirm": "AttackerPassword3#",
        },
    )
    assert replay.status_code == 400
    login_check = mall_app.app.test_client().post(
        "/auth/login",
        json={"email": "t5_pw@test.local", "password": "AttackerPassword3#"},
    )
    assert login_check.status_code in (400, 401, 403)


def test_t5_03_public_product_endpoints_do_not_leak_internal_cost_or_supplier(isolated_t5_env):
    import app as mall_app
    import mall_database

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with mall_database.get_connection() as conn:
        conn.execute(
            "INSERT INTO mall_suppliers (name, contact_name, phone, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("SecretVendor", "담당자", "010-0000-0000", now_str, now_str),
        )
        sup_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    prod = _seed_product(cost_price=12345, supplier_id=sup_id)

    anon = mall_app.app.test_client()
    cat_prod = anon.get("/api/catalog").get_json()["products"][0]
    det_prod = anon.get(f"/api/products/{prod['num']}").get_json()["product"]
    for obj in (cat_prod, det_prod):
        for secret_field in ("cost_price", "supplier_id", "supplier_name", "obs_product_id"):
            assert secret_field not in obj
    assert "live_price" not in cat_prod
    assert det_prod["live_price"] == 30000


def test_t5_04_admin_image_upload_rejects_magic_byte_spoofing(isolated_t5_env):
    import app as mall_app

    adm = mall_app.app.test_client()
    csrf = _login_admin(adm)
    bad = adm.post(
        "/api/admin/upload/image",
        headers={"X-CSRF-Token": csrf},
        data={"image": (BytesIO(b"<script>alert(1)</script>"), "evil.png")},
        content_type="multipart/form-data",
    )
    assert bad.status_code == 400

    # Oversized (> 10MB) rejected
    oversize = adm.post(
        "/api/admin/upload/image",
        headers={"X-CSRF-Token": csrf},
        data={"image": (BytesIO(b"\x89PNG\r\n\x1a\n" + (b"\x00" * (10 * 1024 * 1024 + 10))), "huge.png")},
        content_type="multipart/form-data",
    )
    assert oversize.status_code == 400

    # Valid PNG accepted and served from isolated directory
    ok = adm.post(
        "/api/admin/upload/image",
        headers={"X-CSRF-Token": csrf},
        data={"image": (BytesIO(_TINY_PNG), "ok.png")},
        content_type="multipart/form-data",
    )
    assert ok.status_code == 200
    img_url = ok.get_json()["url"]
    fname = img_url.rsplit("/", 1)[-1]
    assert (isolated_t5_env / "uploads_products" / fname).is_file()
    fetched = adm.get(img_url)
    assert fetched.status_code == 200
    assert fetched.data == _TINY_PNG


def test_t5_05_sales_export_csv_neutralizes_formula_injection(isolated_t5_env):
    import app as mall_app
    import mall_database

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with mall_database.get_connection() as conn:
        conn.execute(
            "INSERT INTO mall_suppliers (name, contact_name, phone, bank_name, bank_account, bank_holder, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("=CMD|' /C calc'!A0", "Sup", "010-1111-2222", "=1+1", "+2+2", "@SUM(1,1)", now_str, now_str),
        )
        sup_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    prod = _seed_product(name="=HYPERLINK(\"http://evil\")", supplier_id=sup_id)
    ord_res = mall_database.create_mall_order({
        "is_guest": True,
        "source_page": "MALL",
        "items": [{"num": prod["num"], "color": "블랙", "size": "FREE", "quantity": 1}],
        "buyer_name": "\x0b=CMD|calc",
        "buyer_phone": "010-9999-8888",
        "address": "서울",
        "payment_method": "BANK",
    })
    assert ord_res["ok"]

    adm = mall_app.app.test_client()
    _login_admin(adm)
    csv_text = adm.get("/api/admin/sales/export.csv?include_pending=1&start_date==1%2B1").get_data(as_text=True)
    assert "'=CMD|" in csv_text
    assert ",=1+1," not in csv_text
    assert ",+2+2," not in csv_text
    assert ",@SUM(1,1)" not in csv_text
    assert "'=HYPERLINK" in csv_text


def test_t5_06_cancelled_order_cannot_transition_to_delivered_or_award_points(isolated_t5_env):
    import app as mall_app
    import mall_database
    import mall_promotions

    prod = _seed_product(stock=20)
    c = mall_app.app.test_client()
    csrf, uid = _register_and_login(c, "t5_term@test.local", "취소테스트", "010-1000-0003")
    o = c.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf},
        json={
            "source_page": "MALL",
            "items": [{"num": prod["num"], "color": "블랙", "size": "FREE", "quantity": 1}],
            "buyer_name": "취소테스트",
            "buyer_phone": "010-1000-0003",
            "address": "서울",
            "payment_method": "BANK",
        },
    ).get_json()["order"]

    adm = mall_app.app.test_client()
    csrf_adm = _login_admin(adm)
    assert adm.post("/api/admin/order-status", headers={"X-CSRF-Token": csrf_adm}, json={"order_id": o["order_id"], "order_status": "주문취소"}).status_code == 200
    for bad_next in ("결제대기", "상품준비중", "배송중", "배송완료"):
        revive = adm.post("/api/admin/order-status", headers={"X-CSRF-Token": csrf_adm}, json={"order_id": o["order_id"], "order_status": bad_next})
        assert revive.status_code == 400, (bad_next, revive.status_code)
    assert mall_database.get_mall_user_by_id(uid)["points_balance"] == 0

    # Direct award_order_points_on_delivery guard also refuses stock_restored != 0
    with mall_database.get_connection() as conn:
        aw = mall_promotions.award_order_points_on_delivery(conn, o["order_id"])
        assert aw["awarded"] is False


def test_t5_07_invalid_and_int64_overflow_inputs_never_return_500(isolated_t5_env):
    import app as mall_app

    prod = _seed_product()
    adm = mall_app.app.test_client()
    csrf_adm = _login_admin(adm)
    c = mall_app.app.test_client()
    csrf_u, _ = _register_and_login(c, "t5_int@test.local", "정수유저", "010-1000-0004")

    admin_cases = [
        ("/api/admin/products/update", {"id": "abc", "name": "x", "price": 1000}),
        ("/api/admin/products/update", {"id": prod["id"], "name": "x", "price": "notanint"}),
        ("/api/admin/products/update", {"id": 10**25, "name": "x", "price": 1000}),
        ("/api/admin/products/add", {"name": "overflow", "price": 10**25, "stock": 1}),
        ("/api/admin/products/delete", {"id": "abc"}),
        ("/api/admin/products/delete", {"id": 10**25}),
        ("/api/admin/categories/save", {"id": "abc", "name": "cat"}),
        ("/api/admin/categories/save", {"id": 10**25, "name": "cat"}),
        ("/api/admin/categories/delete", {"id": "abc"}),
        ("/api/admin/categories/delete", {"id": 10**25}),
        ("/api/admin/order-status", {"order_id": 10**25, "order_status": "입금완료"}),
        ("/api/admin/order-status", {"order_id": "abc", "order_status": "입금완료"}),
    ]
    for path, body in admin_cases:
        resp = adm.post(path, headers={"X-CSRF-Token": csrf_adm}, json=body)
        assert resp.status_code in (400, 404), (path, body, resp.status_code)

    for path, body in [
        ("/api/orders/self-cancel", {"order_id": 10**25}),
        ("/api/orders/self-claim", {"order_id": 10**25, "claim_type": "환불요청", "claim_reason": "사유"}),
    ]:
        resp = c.post(path, headers={"X-CSRF-Token": csrf_u}, json=body)
        assert resp.status_code in (400, 403, 404), (path, resp.status_code)


def test_t5_08_order_creation_rejects_zero_negative_and_bool_quantity(isolated_t5_env):
    import app as mall_app

    prod = _seed_product()
    c = mall_app.app.test_client()
    csrf, _ = _register_and_login(c, "t5_qty@test.local", "수량유저", "010-1000-0005")
    for bad_qty in (-5, 0, True, False, 1.5, "abc", 10**25):
        resp = c.post(
            "/api/orders/create",
            headers={"X-CSRF-Token": csrf},
            json={
                "source_page": "MALL",
                "items": [{"num": prod["num"], "color": "블랙", "size": "FREE", "quantity": bad_qty}],
                "buyer_name": "테스터",
                "buyer_phone": "010-1000-0005",
                "address": "서울",
                "payment_method": "BANK",
            },
        )
        assert resp.status_code == 400, (bad_qty, resp.status_code)

    # Omitted quantity still defaults to 1 for backwards compatibility
    ok = c.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf},
        json={
            "source_page": "MALL",
            "items": [{"num": prod["num"], "color": "블랙", "size": "FREE"}],
            "buyer_name": "테스터",
            "buyer_phone": "010-1000-0005",
            "address": "서울",
            "payment_method": "BANK",
        },
    )
    assert ok.status_code == 200


def test_t5_09_coupon_code_rename_syncs_user_coupons_table(isolated_t5_env):
    import app as mall_app
    import mall_database

    prod = _seed_product()
    adm = mall_app.app.test_client()
    csrf_adm = _login_admin(adm)
    c = mall_app.app.test_client()
    csrf_u, uid = _register_and_login(c, "t5_cp@test.local", "쿠폰유저", "010-1000-0006")

    saved = adm.post(
        "/api/admin/coupons/save",
        headers={"X-CSRF-Token": csrf_adm},
        json={"code": "OLDNAME10", "name": "10%", "discount_type": "PERCENT", "discount_value": 10, "is_active": 1},
    ).get_json()["coupon"]
    assert c.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf_u},
        json={
            "source_page": "MALL",
            "items": [{"num": prod["num"], "color": "블랙", "size": "FREE", "quantity": 1}],
            "buyer_name": "쿠폰유저",
            "buyer_phone": "010-1000-0006",
            "address": "서울",
            "payment_method": "BANK",
            "coupon_code": "OLDNAME10",
        },
    ).status_code == 200

    assert adm.post(
        "/api/admin/coupons/save",
        headers={"X-CSRF-Token": csrf_adm},
        json={"id": saved["id"], "code": "NEWNAME10", "name": "10%", "discount_type": "PERCENT", "discount_value": 10, "is_active": 1},
    ).status_code == 200

    with mall_database.get_connection() as conn:
        row = conn.execute("SELECT code FROM mall_user_coupons WHERE coupon_id = ? AND user_id = ?", (saved["id"], uid)).fetchone()
    assert row is not None and row["code"] == "NEWNAME10"


def test_t5_10_concurrency_and_session_invalidation_invariants(isolated_t5_env):
    import app as mall_app
    import mall_database
    import mall_ops

    limited = _seed_product(name="한정1개", stock=1)
    normal = _seed_product(name="일반상품", stock=20)
    ua = mall_database.create_mall_user("t5_ra@test.local", "Password123!", "raceA", "010-2000-0001")["user"]["id"]
    ub = mall_database.create_mall_user("t5_rb@test.local", "Password123!", "raceB", "010-2000-0002")["user"]["id"]
    with mall_database.get_connection() as conn:
        conn.execute("UPDATE mall_users SET points_balance = 5000 WHERE id = ?", (ua,))
        conn.commit()

    # 1. Parallel last-1 stock race
    b1 = threading.Barrier(2)
    r1 = []
    def _w_stock(uid):
        b1.wait()
        r1.append(mall_database.create_mall_order({
            "user_id": uid, "is_guest": False, "source_page": "MALL",
            "items": [{"num": limited["num"], "color": "블랙", "size": "FREE", "quantity": 1}],
            "buyer_name": "경합", "buyer_phone": "010-2000-0001", "address": "서울", "payment_method": "BANK",
        }).get("ok"))
    t1, t2 = threading.Thread(target=_w_stock, args=(ua,)), threading.Thread(target=_w_stock, args=(ub,))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(r1) == [False, True]

    # 2. Parallel double-spend points race
    b2 = threading.Barrier(2)
    r2 = []
    def _w_pts():
        b2.wait()
        r2.append(mall_database.create_mall_order({
            "user_id": ua, "is_guest": False, "source_page": "MALL",
            "items": [{"num": normal["num"], "color": "블랙", "size": "FREE", "quantity": 1}],
            "buyer_name": "raceA", "buyer_phone": "010-2000-0001", "address": "서울",
            "payment_method": "BANK", "points_used": 5000,
        }).get("ok"))
    tp1, tp2 = threading.Thread(target=_w_pts), threading.Thread(target=_w_pts)
    tp1.start(); tp2.start(); tp1.join(); tp2.join()
    assert sorted(r2) == [False, True]
    assert mall_database.get_mall_user_by_id(ua)["points_balance"] == 0

    # 3. Parallel cancel_expired_unpaid_orders restores stock exactly once
    o_exp = mall_database.create_mall_order({
        "user_id": ub, "is_guest": False, "source_page": "MALL",
        "items": [{"num": normal["num"], "color": "블랙", "size": "FREE", "quantity": 3}],
        "buyer_name": "exp", "buyer_phone": "010-2000-0002", "address": "서울", "payment_method": "BANK",
    })
    old_dt = (datetime.now() - timedelta(hours=100)).strftime("%Y-%m-%d %H:%M:%S")
    with mall_database.get_connection() as conn:
        conn.execute("UPDATE mall_orders SET created_at = ? WHERE id = ?", (old_dt, o_exp["order_id"]))
        conn.commit()
    stk_before = mall_database.get_mall_product_by_num(normal["num"])["total_stock"]
    b3 = threading.Barrier(2)
    counts = []
    def _w_exp():
        b3.wait()
        counts.append(mall_ops.cancel_expired_unpaid_orders(expire_hours=72).get("cancelled_count"))
    te1, te2 = threading.Thread(target=_w_exp), threading.Thread(target=_w_exp)
    te1.start(); te2.start(); te1.join(); te2.join()
    assert sorted(counts) == [0, 1]
    assert mall_database.get_mall_product_by_num(normal["num"])["total_stock"] - stk_before == 3

    # 4. Suspended session immediately invalidated
    c_susp = mall_app.app.test_client()
    _, susp_uid = _register_and_login(c_susp, "t5_susp@test.local", "정지회원", "010-2000-0009")
    adm = mall_app.app.test_client()
    csrf_adm = _login_admin(adm)
    assert adm.post("/api/admin/users/status", headers={"X-CSRF-Token": csrf_adm}, json={"user_id": susp_uid, "is_active": 0}).status_code == 200
    assert c_susp.get("/mypage", follow_redirects=False).status_code == 302
    assert c_susp.get("/api/mypage/addresses").status_code == 401


def test_t5_11_single_tx_customer_claim_and_review_upload_isolation(isolated_t5_env):
    import app as mall_app
    import mall_database

    prod = _seed_product(stock=10)
    c = mall_app.app.test_client()
    csrf, uid = _register_and_login(c, "t5_claim@test.local", "클레임회원", "010-3000-0001")
    ord_res = mall_database.create_mall_order({
        "user_id": uid,
        "is_guest": False,
        "source_page": "MALL",
        "items": [{"num": prod["num"], "color": "블랙", "size": "FREE", "quantity": 1}],
        "buyer_name": "클레임회원",
        "buyer_phone": "010-3000-0001",
        "address": "서울",
        "payment_method": "BANK",
    })
    assert ord_res["ok"]
    oid = ord_res["order_id"]
    assert mall_database.update_mall_order_workflow(oid, payment_status="입금완료", order_status="배송완료")["ok"]

    # Parallel duplicate claims race inside single connection/lock: exactly one succeeds
    b = threading.Barrier(2)
    results = []
    def _claim():
        b.wait()
        results.append(
            mall_database.submit_mall_order_claim_by_customer(
                order_id=oid,
                claim_type="반품요청" if False else "환불요청",
                claim_reason="사이즈 불일치",
            ).get("ok")
        )
    t1, t2 = threading.Thread(target=_claim), threading.Thread(target=_claim)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results) == [False, True]

    # Review image upload writes only to isolated rev_uploads and is served via /static/uploads/reviews/<file>
    up = c.post(
        "/api/reviews/upload-image",
        headers={"X-CSRF-Token": csrf},
        data={"image": (BytesIO(_TINY_PNG), "rev.png")},
        content_type="multipart/form-data",
    )
    assert up.status_code == 200
    rev_url = up.get_json()["url"]
    rev_fname = rev_url.rsplit("/", 1)[-1]
    assert (isolated_t5_env / "uploads_reviews" / rev_fname).is_file()
    assert not (MALL_DIR / "static" / "uploads" / "reviews" / rev_fname).exists()
    got = c.get(rev_url)
    assert got.status_code == 200
    assert got.data == _TINY_PNG


def test_t5_r2_01_live_endpoints_and_html_sanitize_internal_fields(isolated_t5_env):
    import json
    import app as mall_app
    import mall_database

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with mall_database.get_connection() as conn:
        conn.execute(
            "INSERT INTO mall_suppliers (name, contact_name, phone, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("SecretVendor99", "담당자", "010-0000-0000", now_str, now_str),
        )
        sup_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()

    prod = _seed_product(name="라이브노출차단상품", price=50000, live_price=30000, cost_price=12345, supplier_id=sup_id)
    mall_database.update_live_state_bundle({
        "is_live": True,
        "pinned_product_num": prod["num"],
        "broadcast_title": "라이브 보안 테스트 방송",
    })

    anon = mall_app.app.test_client()
    secret_fields = ("cost_price", "supplier_id", "supplier_name", "obs_product_id")

    st_full = anon.get("/api/live/state").get_json()
    for obj in [st_full["selected_product"], *st_full["all_products"]]:
        for k in secret_fields:
            assert k not in obj, (k, obj)
    assert st_full["selected_product"]["live_price"] == 30000
    assert "live_discount_rate" in st_full["selected_product"]

    st_lite = anon.get("/api/live/state-lite").get_json()
    for k in secret_fields:
        assert k not in st_lite["selected_product"], (k, st_lite["selected_product"])
    assert st_lite["selected_product"]["live_price"] == 30000

    sse_resp = anon.get("/api/live/stream", buffered=False)
    try:
        first_chunk = next(sse_resp.response).decode("utf-8")
    finally:
        sse_resp.close()
    assert "event: live_state" in first_chunk
    data_line = next(ln for ln in first_chunk.splitlines() if ln.startswith("data: "))
    sse_obj = json.loads(data_line[6:])
    for k in secret_fields:
        assert k not in sse_obj["selected_product"], (k, sse_obj["selected_product"])
    assert "SecretVendor99" not in first_chunk
    assert sse_obj["selected_product"]["live_price"] == 30000

    live_html_resp = anon.get("/live")
    assert live_html_resp.status_code == 200
    live_html = live_html_resp.get_data(as_text=True)
    assert "SecretVendor99" not in live_html
    for k in secret_fields:
        assert k not in live_html
    assert "30000" in live_html


def test_t5_r2_02_customer_endpoints_reject_unicode_digits_and_int64_overflow(isolated_t5_env):
    import app as mall_app

    prod = _seed_product()
    c = mall_app.app.test_client()
    csrf, _ = _register_and_login(c, "t5_r2_cust@test.local", "고객500방어", "010-3100-0001")

    for bad_id in ("²", 10**25, -1):
        for path, body in [
            ("/api/mypage/addresses/save", {"id": bad_id, "recipient_name": "홍길동", "recipient_phone": "010-1111-2222", "address": "서울"}),
            ("/api/mypage/addresses/delete", {"id": bad_id}),
            ("/api/mypage/addresses/default", {"id": bad_id}),
            ("/api/reviews/update", {"review_id": bad_id, "content": "수정내용"}),
            ("/api/reviews/delete", {"review_id": bad_id}),
            ("/api/wishlist/toggle", {"product_num": bad_id}),
        ]:
            resp = c.post(path, headers={"X-CSRF-Token": csrf}, json=body)
            assert resp.status_code == 400, (path, bad_id, resp.status_code, resp.get_data(as_text=True))

    rev_add_over = c.post(
        "/api/reviews/add",
        headers={"X-CSRF-Token": csrf},
        json={"product_num": 10**25, "rating": 5, "content": "리뷰 내용입니다"},
    )
    assert rev_add_over.status_code == 404

    qna_over_post = c.post(
        f"/api/products/{10**25}/qna",
        headers={"X-CSRF-Token": csrf},
        json={"content": "상품 문의입니다."},
    )
    assert qna_over_post.status_code == 404

    qna_over_get = c.get(f"/api/products/{10**25}/qna")
    assert qna_over_get.status_code == 404

    wish_sync = c.post(
        "/api/wishlist/sync",
        headers={"X-CSRF-Token": csrf},
        json={"product_nums": ["²", 10**25, prod["num"]]},
    )
    assert wish_sync.status_code == 200
    assert wish_sync.get_json()["product_nums"] == [prod["num"]]


def test_t5_r2_03_admin_endpoints_reject_unicode_digits_overflow_and_invalid_supplier_id(isolated_t5_env):
    import app as mall_app

    adm = mall_app.app.test_client()
    csrf = _login_admin(adm)

    for bad_val in ("²", 10**25):
        post_cases = [
            ("/api/admin/suppliers/save", {"id": bad_val, "name": "업체"}),
            ("/api/admin/suppliers/delete", {"id": bad_val}),
            ("/api/admin/suppliers/settlements/add", {"supplier_id": bad_val, "amount": 1000}),
            ("/api/admin/suppliers/settlements/add", {"supplier_id": 1, "amount": bad_val}),
            ("/api/admin/suppliers/settlements/delete", {"id": bad_val}),
            ("/api/admin/banners/save", {"id": bad_val, "title": "배너"}),
            ("/api/admin/banners/save", {"title": "배너", "sort_order": bad_val}),
            ("/api/admin/banners/toggle", {"id": bad_val, "is_active": 1}),
            ("/api/admin/banners/delete", {"id": bad_val}),
            ("/api/admin/qna/answer", {"qna_id": bad_val, "answer": "답변"}),
            ("/api/admin/qna/delete", {"qna_id": bad_val}),
            ("/api/admin/inquiries/answer", {"inquiry_id": bad_val, "answer": "답변"}),
            ("/api/admin/notices/save", {"id": bad_val, "title": "공지제목", "content": "공지내용"}),
            ("/api/admin/notices/delete", {"id": bad_val}),
            ("/api/admin/faqs/save", {"id": bad_val, "question": "질문내용", "answer": "답변내용"}),
            ("/api/admin/faqs/save", {"question": "질문내용", "answer": "답변내용", "sort_order": bad_val}),
            ("/api/admin/faqs/delete", {"id": bad_val}),
            ("/api/admin/reviews/moderate", {"review_id": bad_val, "action": "hide"}),
            ("/api/admin/users/status", {"user_id": bad_val, "is_active": 0}),
        ]
        for path, body in post_cases:
            resp = adm.post(path, headers={"X-CSRF-Token": csrf}, json=body)
            assert resp.status_code in (400, 404), (path, body, resp.status_code)

        for get_url in (
            f"/api/admin/qna?product_num={bad_val}",
            f"/api/admin/reviews?product_num={bad_val}",
            f"/api/admin/sales/summary?supplier_id={bad_val}",
            f"/api/admin/sales/export.csv?supplier_id={bad_val}",
        ):
            gresp = adm.get(get_url)
            assert gresp.status_code == 400, (get_url, gresp.status_code)

    assert adm.get("/api/admin/sales/summary?supplier_id=abc").status_code == 400
    assert adm.get("/api/admin/sales/export.csv?supplier_id=abc").status_code == 400


def test_t5_r2_04_non_dict_json_payloads_never_return_500(isolated_t5_env):
    import app as mall_app

    anon = mall_app.app.test_client()
    for path in ("/auth/login", "/auth/register", "/api/orders/create", "/api/orders/lookup"):
        r = anon.post(path, json=["bad"])
        assert r.status_code == 400, (path, r.status_code)

    mem = mall_app.app.test_client()
    csrf_u, _ = _register_and_login(mem, "t5_r2_nondict@test.local", "비딕트회원", "010-3200-0001")
    for path in (
        "/api/wishlist/toggle",
        "/api/wishlist/sync",
        "/api/orders/self-cancel",
        "/api/orders/self-claim",
        "/api/payments/prepare",
        "/api/payments/confirm",
    ):
        r = mem.post(path, headers={"X-CSRF-Token": csrf_u}, json=["bad"])
        assert r.status_code == 400, (path, r.status_code)

    adm = mall_app.app.test_client()
    csrf_a = _login_admin(adm)
    for path in (
        "/api/admin/order-status",
        "/api/admin/orders/tracking",
        "/api/admin/apply-discount",
        "/api/admin/categories/save",
        "/api/admin/categories/delete",
        "/api/admin/products/add",
        "/api/admin/products/update",
        "/api/admin/products/delete",
        "/api/admin/policy-defaults",
        "/api/admin/store-info",
        "/api/admin/banners/save",
        "/api/admin/banners/toggle",
        "/api/admin/banners/delete",
        "/api/admin/suppliers/save",
        "/api/admin/suppliers/delete",
        "/api/admin/suppliers/settlements/add",
        "/api/admin/suppliers/settlements/delete",
        "/api/admin/system/run-maintenance",
    ):
        r = adm.post(path, headers={"X-CSRF-Token": csrf_a}, json=["bad"])
        assert r.status_code == 400, (path, r.status_code)


def test_t5_r2_05_coupon_rename_and_pre_rename_order_cancel_decrements_used_count(isolated_t5_env):
    import app as mall_app
    import mall_database

    prod = _seed_product(stock=20)
    adm = mall_app.app.test_client()
    csrf_a = _login_admin(adm)
    c1 = mall_app.app.test_client()
    csrf_u1, _ = _register_and_login(c1, "t5_r2_cp1@test.local", "쿠폰유저1", "010-3300-0001")
    c2 = mall_app.app.test_client()
    csrf_u2, _ = _register_and_login(c2, "t5_r2_cp2@test.local", "쿠폰유저2", "010-3300-0002")

    saved = adm.post(
        "/api/admin/coupons/save",
        headers={"X-CSRF-Token": csrf_a},
        json={
            "code": "PRE_RENAME",
            "name": "선착순1명",
            "discount_type": "FIXED",
            "discount_value": 5000,
            "usage_limit": 1,
            "is_active": 1,
        },
    ).get_json()["coupon"]

    ord_resp = c1.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf_u1},
        json={
            "source_page": "MALL",
            "items": [{"num": prod["num"], "color": "블랙", "size": "FREE", "quantity": 1}],
            "buyer_name": "쿠폰유저1",
            "buyer_phone": "010-3300-0001",
            "address": "서울",
            "payment_method": "BANK",
            "coupon_code": "PRE_RENAME",
        },
    )
    assert ord_resp.status_code == 200
    order_id = ord_resp.get_json()["order"]["order_id"]

    # Rename coupon while order is active
    rename_resp = adm.post(
        "/api/admin/coupons/save",
        headers={"X-CSRF-Token": csrf_a},
        json={
            "id": saved["id"],
            "code": "POST_RENAME",
            "name": "선착순1명",
            "discount_type": "FIXED",
            "discount_value": 5000,
            "usage_limit": 1,
            "is_active": 1,
        },
    )
    assert rename_resp.status_code == 200

    # User 1 cancels pre-rename order -> used_count must decrement back to 0
    cancel_resp = c1.post(
        "/api/orders/self-cancel",
        headers={"X-CSRF-Token": csrf_u1},
        json={"order_id": order_id, "claim_reason": "단순변심"},
    )
    assert cancel_resp.status_code == 200

    with mall_database.get_connection() as conn:
        cp_row = conn.execute("SELECT code, used_count FROM mall_coupons WHERE id = ?", (saved["id"],)).fetchone()
        ord_row = conn.execute("SELECT coupon_code FROM mall_orders WHERE id = ?", (order_id,)).fetchone()
    assert cp_row["code"] == "POST_RENAME"
    assert int(cp_row["used_count"]) == 0
    assert ord_row["coupon_code"] == "POST_RENAME"

    # Another user can now redeem POST_RENAME within usage_limit=1
    ord2_resp = c2.post(
        "/api/orders/create",
        headers={"X-CSRF-Token": csrf_u2},
        json={
            "source_page": "MALL",
            "items": [{"num": prod["num"], "color": "블랙", "size": "FREE", "quantity": 1}],
            "buyer_name": "쿠폰유저2",
            "buyer_phone": "010-3300-0002",
            "address": "서울",
            "payment_method": "BANK",
            "coupon_code": "POST_RENAME",
        },
    )
    assert ord2_resp.status_code == 200


def test_t5_r2_06_catalog_etag_differentiates_query_parameters(isolated_t5_env):
    import app as mall_app
    import mall_database

    mall_database.create_mall_product({
        "name": "아우터코트",
        "category": "OUTER",
        "price": 70000,
        "options": [{"color": "블랙", "size": "M", "stock": 10}],
    })
    mall_database.create_mall_product({
        "name": "기본티셔츠",
        "category": "TOP",
        "price": 20000,
        "options": [{"color": "화이트", "size": "FREE", "stock": 10}],
    })

    c = mall_app.app.test_client()
    r_all = c.get("/api/catalog")
    assert r_all.status_code == 200
    etag_all = r_all.headers.get("ETag")
    assert etag_all
    assert len(r_all.get_json()["products"]) == 2

    # Same query with matching ETag -> 304
    assert c.get("/api/catalog", headers={"If-None-Match": etag_all}).status_code == 304

    # Filtered query with unfiltered ETag MUST return 200 with filtered results
    r_outer = c.get("/api/catalog?category=OUTER", headers={"If-None-Match": etag_all})
    assert r_outer.status_code == 200
    outer_data = r_outer.get_json()
    assert len(outer_data["products"]) == 1
    assert outer_data["products"][0]["category"] == "OUTER"
    etag_outer = r_outer.headers.get("ETag")
    assert etag_outer and etag_outer != etag_all

    # Same filtered query with its own ETag -> 304
    assert c.get("/api/catalog?category=OUTER", headers={"If-None-Match": etag_outer}).status_code == 304

    # Changing q/sort also avoids false 304
    r_q = c.get("/api/catalog?category=OUTER&q=없는상품", headers={"If-None-Match": etag_outer})
    assert r_q.status_code == 200
    assert len(r_q.get_json()["products"]) == 0


def test_t5_r2_07_csv_bom_formula_injection_neutralized(isolated_t5_env):
    import app as mall_app
    import mall_database
    import mall_shipping

    assert mall_shipping._sanitize_csv_cell("\ufeff=1+1") == "'\ufeff=1+1"
    assert mall_shipping._sanitize_csv_cell("\ufeff\t+SUM(1,1)") == "'\ufeff\t+SUM(1,1)"
    assert mall_shipping._sanitize_csv_cell("normal") == "normal"

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with mall_database.get_connection() as conn:
        conn.execute(
            "INSERT INTO mall_suppliers (name, contact_name, phone, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("\ufeff=1+1", "Sup", "010-1111-2222", now_str, now_str),
        )
        sup_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    prod = _seed_product(name="정상상품", supplier_id=sup_id)
    mall_database.create_mall_order({
        "is_guest": True,
        "source_page": "MALL",
        "items": [{"num": prod["num"], "color": "블랙", "size": "FREE", "quantity": 1}],
        "buyer_name": "정상구매자",
        "buyer_phone": "010-9999-8888",
        "address": "서울",
        "payment_method": "BANK",
    })
    adm = mall_app.app.test_client()
    _login_admin(adm)
    csv_text = adm.get("/api/admin/sales/export.csv?include_pending=1").get_data(as_text=True)
    assert "'\ufeff=1+1" in csv_text


def test_t5_r3_01_coupon_admin_and_non_neg_int_reject_int64_overflow_and_invalid_ids(isolated_t5_env):
    import app as mall_app
    import mall_database
    import mall_promotions

    ok_max, val_max, _ = mall_promotions.parse_strict_non_neg_int(mall_database.SQLITE_INT64_MAX, "값")
    assert ok_max is True and val_max == mall_database.SQLITE_INT64_MAX
    for bad_num in (
        mall_database.SQLITE_INT64_MAX + 1,
        str(mall_database.SQLITE_INT64_MAX + 1),
        10**25,
        "1" + ("0" * 25),
        -1,
        "²",
        "٣",
        True,
        False,
    ):
        ok_bad, parsed_bad, _ = mall_promotions.parse_strict_non_neg_int(bad_num, "값")
        assert ok_bad is False and parsed_bad is None, (bad_num, ok_bad, parsed_bad)

    adm = mall_app.app.test_client()
    csrf = _login_admin(adm)

    for bad_id in (
        9223372036854775808,
        "9223372036854775808",
        10**25,
        "²",
        "٣",
        -1,
        True,
        False,
        1.5,
    ):
        save_resp = adm.post(
            "/api/admin/coupons/save",
            headers={"X-CSRF-Token": csrf},
            json={
                "id": bad_id,
                "code": "NEWCPN99",
                "name": "테스트쿠폰",
                "discount_type": "FIXED",
                "discount_value": 1000,
            },
        )
        assert save_resp.status_code == 400, (bad_id, save_resp.status_code, save_resp.get_data(as_text=True))

        del_resp = adm.post(
            "/api/admin/coupons/delete",
            headers={"X-CSRF-Token": csrf},
            json={"id": bad_id},
        )
        assert del_resp.status_code == 400, (bad_id, del_resp.status_code, del_resp.get_data(as_text=True))

    assert adm.post(
        "/api/admin/coupons/delete",
        headers={"X-CSRF-Token": csrf},
        json={"id": 0},
    ).status_code == 400

    # Confirm NEWCPN99 was never created by id=False or other invalid IDs
    with mall_database.get_connection() as conn:
        assert conn.execute("SELECT id FROM mall_coupons WHERE code = 'NEWCPN99'").fetchone() is None

    # Valid save (create + update) and delete continue to succeed
    created = adm.post(
        "/api/admin/coupons/save",
        headers={"X-CSRF-Token": csrf},
        json={
            "id": "",
            "code": "VALIDCPN1",
            "name": "정상쿠폰",
            "discount_type": "FIXED",
            "discount_value": 1000,
        },
    )
    assert created.status_code == 200
    cid = created.get_json()["coupon"]["id"]
    assert adm.post(
        "/api/admin/coupons/save",
        headers={"X-CSRF-Token": csrf},
        json={
            "id": cid,
            "code": "VALIDCPN1",
            "name": "수정쿠폰",
            "discount_type": "FIXED",
            "discount_value": 2000,
        },
    ).status_code == 200
    assert adm.post(
        "/api/admin/coupons/delete",
        headers={"X-CSRF-Token": csrf},
        json={"id": cid},
    ).status_code == 200


def test_t5_r3_02_live_state_lite_etag_differentiates_reviews_flag(isolated_t5_env):
    import app as mall_app
    import mall_database

    prod = _seed_product(name="라이브ETag상품")
    mall_database.update_live_state_bundle({
        "is_live": True,
        "pinned_product_num": prod["num"],
        "broadcast_title": "ETag 테스트 방송",
    })
    rev_res = mall_database.add_mall_review(
        product_num=prod["num"],
        author="리뷰작성자",
        rating=5,
        content="아주 좋아요!",
    )
    assert rev_res and rev_res.get("id")

    c = mall_app.app.test_client()
    r_no_rev = c.get("/api/live/state-lite")
    assert r_no_rev.status_code == 200
    etag_no_rev = r_no_rev.headers.get("ETag")
    assert etag_no_rev
    assert "selected_product_reviews" not in r_no_rev.get_json()

    # Same query with matching ETag -> 304
    assert c.get("/api/live/state-lite", headers={"If-None-Match": etag_no_rev}).status_code == 304

    # Requesting ?reviews=1 with non-reviews ETag MUST return 200 with reviews payload and distinct ETag
    r_rev = c.get("/api/live/state-lite?reviews=1", headers={"If-None-Match": etag_no_rev})
    assert r_rev.status_code == 200
    etag_rev = r_rev.headers.get("ETag")
    assert etag_rev and etag_rev != etag_no_rev
    rev_data = r_rev.get_json()
    assert len(rev_data.get("selected_product_reviews") or []) == 1
    assert rev_data["selected_product_review_summary"]["count"] == 1

    # Matching ?reviews=1 ETag -> 304; querying without reviews with ?reviews=1 ETag -> 200
    assert c.get("/api/live/state-lite?reviews=1", headers={"If-None-Match": etag_rev}).status_code == 304
    assert c.get("/api/live/state-lite", headers={"If-None-Match": etag_rev}).status_code == 200


def test_t5_r3_03_sync_from_obs_and_arabic_indic_digits_reject_cleanly(isolated_t5_env, monkeypatch):
    import app as mall_app
    import mall_database
    import mall_promotions

    monkeypatch.setenv("MALL_SYNC_SECRET", "t5-obs-secret-123")
    headers = {"X-Mall-Sync-Secret": "t5-obs-secret-123"}
    c = mall_app.app.test_client()

    # Non-dict JSON and malformed JSON on /api/sync/from-obs -> 400 (never 500)
    for bad_json in (["bad"], 123, "hello", True):
        r_nd = c.post("/api/sync/from-obs", headers=headers, json=bad_json)
        assert r_nd.status_code == 400, (bad_json, r_nd.status_code)
    r_mal = c.post("/api/sync/from-obs", headers=headers, data=b"{bad json", content_type="application/json")
    assert r_mal.status_code == 400

    # Malformed / overflow / non-dict payloads on upsert_product -> 400
    bad_upsert_payloads = [
        {"action": "upsert_product", "product": ["bad"]},
        {"action": "upsert_product", "product": "bad"},
        {"action": "upsert_product", "product": {}},
        {"action": "upsert_product", "product": {"num": "²", "name": "X", "price": 1000}},
        {"action": "upsert_product", "product": {"num": "٣", "name": "X", "price": 1000}},
        {"action": "upsert_product", "product": {"num": 10**25, "name": "X", "price": 1000}},
        {"action": "upsert_product", "product": {"num": 9223372036854775808, "name": "X", "price": 1000}},
        {"action": "upsert_product", "product": {"num": -1, "name": "X", "price": 1000}},
        {"action": "upsert_product", "product": {"num": True, "name": "X", "price": 1000}},
        {"action": "upsert_product", "product": {"num": 9, "id": "²", "name": "X", "price": 1000}},
        {"action": "upsert_product", "product": {"num": 9, "db_id": 10**25, "name": "X", "price": 1000}},
        {"action": "upsert_product", "product": {"num": 9, "name": "X", "price": "²"}},
        {"action": "upsert_product", "product": {"num": 9, "name": "X", "price": "٣"}},
        {"action": "upsert_product", "product": {"num": 9, "name": "X", "price": 10**25}},
        {"action": "upsert_product", "product": {"num": 9, "name": "X", "price": 1000, "live_price": 10**25}},
        {"action": "upsert_product", "product": {"num": 9, "name": "X", "price": 1000, "consumer_price": "²"}},
        {
            "action": "upsert_product",
            "product": {"num": 9, "name": "X", "price": 1000, "options": [{"color": "블랙", "size": "M", "stock": "²"}]},
        },
        {
            "action": "upsert_product",
            "product": {"num": 9, "name": "X", "price": 1000, "options": [{"color": "블랙", "size": "M", "stock": 10**25}]},
        },
        {"action": "upsert_product", "product": {"num": 9, "name": "X", "price": 1000, "options": ["bad"]}},
        {"action": "upsert_product", "product": {"num": 9, "name": "X", "price": 1000}, "images_b64": ["bad"]},
    ]
    for payload in bad_upsert_payloads:
        r = c.post("/api/sync/from-obs", headers=headers, json=payload)
        assert r.status_code == 400, (payload, r.status_code, r.get_data(as_text=True))

    # Malformed / overflow payloads on bulk_sync and live_state product -> 400
    bad_bulk_payloads = [
        {"action": "bulk_sync", "products": "bad"},
        {"action": "bulk_sync", "products": 123},
        {"action": "bulk_sync", "products": ["bad"]},
        {"action": "bulk_sync", "products": [{"num": 10**25, "name": "X"}]},
        {"action": "bulk_sync", "products": [{"num": "²", "name": "X"}]},
        {"action": "bulk_sync", "products": [{"num": 1, "name": "X", "price": 10**25}]},
        {"action": "pin_product", "product": {"num": "²", "name": "X", "price": 1000}},
        {"action": "pin_product", "product": ["bad"]},
    ]
    for payload in bad_bulk_payloads:
        r = c.post("/api/sync/from-obs", headers=headers, json=payload)
        assert r.status_code == 400, (payload, r.status_code, r.get_data(as_text=True))

    # Valid upsert_product & bulk_sync remain 100% compatible
    r_ok_up = c.post(
        "/api/sync/from-obs",
        headers=headers,
        json={
            "action": "upsert_product",
            "product": {
                "id": "item-1",
                "num": 1,
                "name": "정상OBS상품",
                "price": 25000,
                "live_price": 19000,
                "consumer_price": 35000,
                "options": [{"color": "블랙", "size": "M", "stock": 12}],
            },
        },
    )
    assert r_ok_up.status_code == 200
    assert r_ok_up.get_json()["product"]["num"] == 1

    r_ok_bulk = c.post(
        "/api/sync/from-obs",
        headers=headers,
        json={
            "action": "bulk_sync",
            "products": [
                {
                    "id": 2,
                    "num": 2,
                    "name": "정상벌크상품",
                    "price": "30000",
                    "options": [{"color": "화이트", "size": "FREE", "stock": "5"}],
                }
            ],
        },
    )
    assert r_ok_bulk.status_code == 200
    assert r_ok_bulk.get_json()["synced_count"] == 1

    # Arabic-Indic digit "٣" must be rejected as non-ASCII integer across cleaned regexes
    assert mall_database._parse_optional_price_bound("٣") is None
    assert mall_database._parse_int64_safe("٣") is None
    ord_ar = mall_database.create_mall_order({
        "is_guest": True,
        "source_page": "MALL",
        "items": [{"num": "٣", "color": "블랙", "size": "FREE", "quantity": 1}],
        "buyer_name": "아라빅테스트",
        "buyer_phone": "010-1234-5678",
        "address": "서울",
        "payment_method": "BANK",
    })
    assert ord_ar["ok"] is False and ord_ar.get("http_status") == 400

    # Ensure list_admin_restock_alerts does not treat "٣" as 3
    with mall_database.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO mall_restock_alerts (product_num, color, size, contact_type, contact_value, status, created_at)
            VALUES (3, '블랙', 'FREE', 'EMAIL', 'ar3@test.local', 'PENDING', '2026-01-01 00:00:00'),
                   (4, '화이트', 'FREE', 'EMAIL', 'ar4@test.local', 'PENDING', '2026-01-01 00:00:00')
            """
        )
        conn.commit()
    assert len(mall_promotions.list_admin_restock_alerts(product_num="3")) == 1
    assert len(mall_promotions.list_admin_restock_alerts(product_num="٣")) == 2  # not parsed as 3
    assert mall_promotions.list_admin_restock_alerts(product_num="999") == []


def test_t5_r4_01_sync_pull_now_requires_admin_csrf_or_valid_sync_secret(isolated_t5_env, monkeypatch):
    import app as mall_app

    # 1. With MALL_SYNC_SECRET unset (and local/cloud mode), anonymous and non-admin callers must still be blocked
    monkeypatch.delenv("MALL_SYNC_SECRET", raising=False)
    monkeypatch.setattr(mall_app, "MALL_SYNC_SECRET", "")

    anon = mall_app.app.test_client()
    assert anon.post("/api/sync/pull-now").status_code == 401

    mem = mall_app.app.test_client()
    csrf_u, _ = _register_and_login(mem, "t5_r4_mem@test.local", "일반회원", "010-4000-0001")
    assert mem.post("/api/sync/pull-now", headers={"X-CSRF-Token": csrf_u}).status_code == 403

    adm = mall_app.app.test_client()
    csrf_a = _login_admin(adm)
    assert adm.post("/api/sync/pull-now").status_code == 403
    ok_adm = adm.post("/api/sync/pull-now", headers={"X-CSRF-Token": csrf_a})
    assert ok_adm.status_code == 200
    assert ok_adm.get_json()["status"] == "ok"

    # 2. With MALL_SYNC_SECRET configured: invalid secret -> 401, valid secret -> 200, admin+CSRF -> 200
    monkeypatch.setenv("MALL_SYNC_SECRET", "t5-r4-pull-secret")
    assert anon.post("/api/sync/pull-now").status_code == 401
    assert anon.post("/api/sync/pull-now", headers={"X-Mall-Sync-Secret": "wrong-secret"}).status_code == 401
    assert mem.post("/api/sync/pull-now", headers={"X-CSRF-Token": csrf_u}).status_code == 403
    assert adm.post("/api/sync/pull-now").status_code == 403
    assert adm.post("/api/sync/pull-now", headers={"X-CSRF-Token": csrf_a}).status_code == 200
    ok_sec = anon.post("/api/sync/pull-now", headers={"X-Mall-Sync-Secret": "t5-r4-pull-secret"})
    assert ok_sec.status_code == 200
    assert ok_sec.get_json()["status"] == "ok"


def test_t5_r4_02_sync_from_obs_rejects_poisoned_live_state_and_readers_stay_resilient(isolated_t5_env, monkeypatch):
    import app as mall_app
    import mall_database

    monkeypatch.setenv("MALL_SYNC_SECRET", "t5-r4-obs-secret")
    headers = {"X-Mall-Sync-Secret": "t5-r4-obs-secret"}
    c = mall_app.app.test_client()

    bad_live_payloads = [
        {"action": "live_state", "last_heartbeat_ts": "bad"},
        {"action": "state", "last_heartbeat_ts": ["bad"]},
        {"action": "heartbeat", "last_heartbeat_ts": True},
        {"action": "live_state", "last_heartbeat_ts": False},
        {"action": "live_state", "last_heartbeat_ts": -1},
        {"action": "live_state", "last_heartbeat_ts": "NaN"},
        {"action": "live_state", "last_heartbeat_ts": "inf"},
        {"action": "live_state", "last_heartbeat_ts": None},
        {"action": "live_state", "state_version": "bad"},
        {"action": "live_state", "catalog_version": 10**25},
        {"action": "live_state", "state_version": False},
        {"action": "pin_product", "pinned_product_num": "bad"},
        {"action": "live_state", "active_product_num": True},
        {"action": "live_state", "timer": "bad"},
        {"action": "live_state", "live_urls": ["bad"]},
        {
            "action": "pin_product",
            "pinned_product_num": "bad",
            "product": {"num": 91, "name": "부분반영금지", "price": 10000},
        },
        {
            "action": "bulk_sync",
            "products": [{"num": 92, "name": "벌크반영금지", "price": 10000}],
            "delete_missing": False,
            "live_state": {"catalog_version": "bad"},
        },
        {
            "action": "bulk_sync",
            "products": [{"num": 93, "name": "벌크반영금지2", "price": 10000}],
            "delete_missing": False,
            "live_state": {"last_heartbeat_ts": "bad"},
        },
        {
            "action": "bulk_sync",
            "products": [],
            "delete_missing": False,
            "live_state": {"state_version": 10**25},
        },
        {
            "action": "bulk_sync",
            "products": [],
            "delete_missing": False,
            "live_state": {"pinned_product_num": True},
        },
        {
            "action": "bulk_sync",
            "products": [],
            "delete_missing": False,
            "live_state": {"timer": "bad"},
        },
        {
            "action": "bulk_sync",
            "products": [],
            "delete_missing": False,
            "live_state": {"live_urls": "bad"},
        },
    ]
    for payload in bad_live_payloads:
        resp = c.post("/api/sync/from-obs", headers=headers, json=payload)
        assert resp.status_code == 400, (payload, resp.status_code, resp.get_data(as_text=True))

    # Validation must fail BEFORE any product DB write
    assert mall_database.get_all_mall_products() == []

    for get_path in ("/", "/api/live/state-lite", "/api/live/state", "/api/catalog"):
        assert c.get(get_path).status_code == 200, get_path

    # Even if legacy/corrupted values exist directly in mall_live_state, public readers never 500
    with mall_database.get_connection() as conn:
        for k, v in (
            ("last_heartbeat_ts", '"bad"'),
            ("state_version", '"bad"'),
            ("catalog_version", "9223372036854775808"),
            ("timer", '"bad"'),
            ("live_urls", '["bad"]'),
        ):
            conn.execute(
                "INSERT INTO mall_live_state (key, value, updated_at) VALUES (?, ?, '2026-01-01 00:00:00') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, v),
            )
        conn.commit()

    assert c.get("/").status_code == 200
    assert c.get("/api/catalog").status_code == 200
    lite_data = c.get("/api/live/state-lite").get_json()
    assert lite_data["state_version"] == 1 and lite_data["catalog_version"] == 1
    assert lite_data["timer"] == {"active": False} and lite_data["live_urls"] == {}
    full_data = c.get("/api/live/state").get_json()
    assert full_data["state_version"] == 1 and full_data["catalog_version"] == 1
    assert full_data["last_heartbeat_ts"] == 0.0

    # Valid bulk_sync with live_state succeeds normally
    ok_bulk = c.post(
        "/api/sync/from-obs",
        headers=headers,
        json={
            "action": "bulk_sync",
            "products": [{"num": 1, "name": "정상라이브상품", "price": 15000}],
            "delete_missing": True,
            "live_state": {
                "is_live": True,
                "pinned_product_num": 1,
                "last_heartbeat_ts": 1700000000.5,
                "timer": {"active": False},
                "live_urls": {},
            },
        },
    )
    assert ok_bulk.status_code == 200
    assert ok_bulk.get_json()["synced_count"] == 1


def test_t5_r4_03_boolean_id_false_and_store_arabic_indic_digits_rejected(isolated_t5_env):
    import app as mall_app
    import mall_database

    adm = mall_app.app.test_client()
    csrf_a = _login_admin(adm)
    mem = mall_app.app.test_client()
    csrf_u, uid = _register_and_login(mem, "t5_r4_id@test.local", "아이디테스트", "010-4100-0001")

    for bad_id in (False, True, 1.5):
        for path, body in (
            ("/api/admin/banners/save", {"id": bad_id, "title": "BanFalse"}),
            ("/api/admin/suppliers/save", {"id": bad_id, "name": "SupFalse"}),
            ("/api/admin/notices/save", {"id": bad_id, "title": "NoticeFalse", "content": "공지 본문입니다."}),
            ("/api/admin/faqs/save", {"id": bad_id, "question": "FaqFalse?", "answer": "답변 본문입니다."}),
        ):
            r = adm.post(path, headers={"X-CSRF-Token": csrf_a}, json=body)
            assert r.status_code == 400, (path, bad_id, r.status_code, r.get_data(as_text=True))

        r_addr = mem.post(
            "/api/mypage/addresses/save",
            headers={"X-CSRF-Token": csrf_u},
            json={
                "id": bad_id,
                "recipient_name": "AddrFalse",
                "recipient_phone": "010-1234-5678",
                "address": "서울시 강남구",
            },
        )
        assert r_addr.status_code == 400, (bad_id, r_addr.status_code, r_addr.get_data(as_text=True))

    assert adm.post(
        "/api/admin/products/add",
        headers={"X-CSRF-Token": csrf_a},
        json={"name": "ProdSupFalse", "price": 1000, "supplier_id": False},
    ).status_code == 400

    with mall_database.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mall_hero_banners").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM mall_suppliers").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM mall_notices").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM mall_faqs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM mall_user_addresses WHERE user_id = ?", (uid,)).fetchone()[0] == 0

    # Store info must reject Arabic-Indic / superscript digits cleanly with 400
    for field in ("shipping_fee", "free_shipping_threshold", "unpaid_cancel_hours"):
        for bad_digit in ("٣", "²"):
            r_bad = adm.post("/api/admin/store-info", headers={"X-CSRF-Token": csrf_a}, json={field: bad_digit})
            assert r_bad.status_code == 400, (field, bad_digit, r_bad.status_code)

    r_ok_store = adm.post(
        "/api/admin/store-info",
        headers={"X-CSRF-Token": csrf_a},
        json={"shipping_fee": "3000", "free_shipping_threshold": "50000", "unpaid_cancel_hours": "48"},
    )
    assert r_ok_store.status_code == 200
    si = r_ok_store.get_json()["store_info"]
    assert si["shipping_fee"] == 3000 and si["free_shipping_threshold"] == 50000 and si["unpaid_cancel_hours"] == 48


