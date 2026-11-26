# -*- coding: utf-8 -*-
"""
Milestone 2 (M2) — F6 (Logistics/Tracking/CSV), F7 (Self-Cancel/Claim),
F8 (MyPage/Addresses/Password Reset/Withdrawal), F9 (Admin Member Management) Tests.
"""

import csv
import io
import json
import os
import re
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

ADMIN_EMAIL = "m2admin@golabang.test"
ADMIN_PASS = "M2AdminPass!2026"


@pytest.fixture(scope="session", autouse=True)
def _bootstrap_isolated_env(tmp_path_factory):
    boot_dir = tmp_path_factory.mktemp("m2_boot")
    os.environ["MALL_DB_PATH"] = str(boot_dir / "boot_m2.db")
    os.environ["OBS_PULL_ENABLED"] = "0"
    os.environ["MALL_MODE"] = "cloud"
    os.environ["MALL_SCHEDULER_ENABLED"] = "0"
    os.environ["MALL_ADMIN_EMAIL"] = ADMIN_EMAIL
    os.environ["MALL_ADMIN_PASSWORD"] = ADMIN_PASS

    import mall_database  # noqa: F401
    import mall_security  # noqa: F401
    import mall_shipping  # noqa: F401
    import mall_member_service  # noqa: F401
    import app as mall_app  # noqa: F401
    return boot_dir


@pytest.fixture()
def isolated_mall_db(tmp_path, monkeypatch):
    import mall_database
    import mall_security

    db_file = tmp_path / "m2_unit.db"
    monkeypatch.setenv("MALL_ADMIN_EMAIL", ADMIN_EMAIL)
    monkeypatch.setenv("MALL_ADMIN_PASSWORD", ADMIN_PASS)
    monkeypatch.setenv("OBS_PULL_ENABLED", "0")
    monkeypatch.setenv("MALL_MODE", "cloud")
    monkeypatch.setenv("MALL_SCHEDULER_ENABLED", "0")
    monkeypatch.delenv("MALL_SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    mall_security.rate_limiter._hits.clear()
    mall_database.set_db_path(str(db_file), init=True)
    mall_database.ensure_default_mall_admin()
    return db_file


def _seed_product(num: int = 101, name: str = "테스트 니트", price: int = 30000, stock: int = 10):
    import mall_database

    return mall_database.upsert_product_from_obs(
        {
            "obs_product_id": num + 2000,
            "num": num,
            "name": name,
            "category": "TOP",
            "price": price,
            "live_price": price - 5000,
            "consumer_price": price + 10000,
            "image_url": "",
            "specs": f"{name} 상세",
            "options": [{"color": "블랙", "size": "FREE", "stock": stock}],
        }
    )


def _create_member(email: str, password: str = "MemberPass123!", name: str = "김회원", phone: str = "010-1111-2222", zipcode: str = "06236", address: str = "서울시 강남구 테헤란로 1", address_detail: str = "101호"):
    import mall_database

    res = mall_database.create_mall_user(
        email=email,
        password=password,
        name=name,
        phone=phone,
        zipcode=zipcode,
        address=address,
        address_detail=address_detail,
    )
    assert res["ok"], res
    return res["user"]


def _login_client(client, email: str, password: str) -> str:
    res = client.post("/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.get_json()
    return res.get_json()["csrf_token"]


def _get_csrf_from_html(html: str) -> str:
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert m, "csrf_token hidden input not found"
    return m.group(1)


# ==============================================================================
# F6: Carrier Catalog, Tracking URL, CSV Export/Import & Admin/Customer Tracking
# ==============================================================================


def test_f6_mall_shipping_carriers_urls_and_csv_safety():
    import mall_shipping

    catalog = mall_shipping.get_carrier_catalog()
    codes = {c["code"] for c in catalog}
    assert {"CJ", "HANJIN", "LOTTE", "EPOST", "LOGEN", "KDEXP", "CUPOST", "GSPOST", "DIRECT"} <= codes

    assert mall_shipping.resolve_carrier("cj", "") == ("CJ", "CJ대한통운")
    assert mall_shipping.resolve_carrier("", "한진택배") == ("HANJIN", "한진택배")
    assert mall_shipping.resolve_carrier("DIRECT", "자체퀵배송") == ("DIRECT", "자체퀵배송")
    assert mall_shipping.resolve_carrier("직접배송", "자체퀵배송") == ("DIRECT", "자체퀵배송")
    assert mall_shipping.resolve_carrier("", "직접배송") == ("DIRECT", "직접배송/기타")
    assert mall_shipping.resolve_carrier("", "직접배송/기타") == ("DIRECT", "직접배송/기타")
    assert mall_shipping.resolve_carrier("", "기타") == ("DIRECT", "직접배송/기타")
    assert mall_shipping.resolve_carrier("UNKNOWN_XYZ", "") == ("", "")
    assert mall_shipping.resolve_carrier("", "UNKNOWN_CARRIER_NAME") == ("", "")
    assert mall_shipping.resolve_carrier("NO_SUCH_CODE", "CJ대한통운") == ("", "")

    url = mall_shipping.get_carrier_tracking_url("CJ", " 1234-5678 9012 ")
    assert url.startswith("https://")
    assert "1234-56789012" in url
    assert mall_shipping.get_carrier_tracking_url("DIRECT", "12345") == ""
    assert mall_shipping.get_carrier_tracking_url("CJ", "javascript:alert(1)") == ""
    assert mall_shipping.get_carrier_tracking_url("CJ", "<script>1</script>") == ""

    csv_bytes = mall_shipping.build_shipping_export_csv([
        {
            "order_code": "ORD-TEST-001",
            "buyer_name": "=CMD|' /C calc'!A0",
            "buyer_phone": "+010-1234-5678",
            "zipcode": "06236",
            "address": "-서울시 강남구",
            "address_detail": "@101호",
            "delivery_memo": "\t=1+1",
            "carrier_code": "CJ",
            "tracking_number": "1234567890",
            "order_items": [{"product_num": 1, "product_name": "니트", "color": "블랙", "size": "FREE", "quantity": 2}],
        },
        {
            "order_code": "ORD-TEST-002",
            "buyer_name": "\n=CMD()",
            "buyer_phone": " =SUM(1,2)",
            "zipcode": " 06236",
            "address": "\r+1+1",
            "address_detail": "  \t-2+2",
            "delivery_memo": "문앞",
            "carrier_code": "CJ",
            "tracking_number": "0987654321",
            "order_items": [{"product_num": 1, "product_name": "니트", "color": "블랙", "size": "FREE", "quantity": 1}],
        },
    ])
    assert csv_bytes.startswith(b"\xef\xbb\xbf")
    text = csv_bytes.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert tuple(rows[0]) == mall_shipping.SHIPPING_EXPORT_COLUMNS
    assert rows[1][1].startswith("'=")
    assert rows[1][2].startswith("'+")
    assert rows[1][4].startswith("'-")
    assert rows[1][5].startswith("'@")
    assert rows[1][6].startswith("'\t=1+1")
    assert rows[2][1] == "'\n=CMD()"
    assert rows[2][2] == "' =SUM(1,2)"
    assert rows[2][3] == " 06236"
    assert rows[2][4] == "'\r+1+1"
    assert rows[2][5] == "'  \t-2+2"

    # CP949 Korean header CSV parse
    kor_csv = "주문번호,택배사,운송장번호\r\nORD-1,CJ대한통운,555566667777\r\n".encode("cp949")
    parsed = mall_shipping.parse_tracking_import_csv(kor_csv)
    assert parsed["ok"] is True
    assert len(parsed["rows"]) == 1
    assert parsed["rows"][0]["order_code"] == "ORD-1"
    assert parsed["rows"][0]["carrier_name"] == "CJ대한통운"
    assert parsed["rows"][0]["tracking_number"] == "555566667777"

    bad = mall_shipping.parse_tracking_import_csv("buyer_name,phone\n홍길동,010\n")
    assert bad["ok"] is False


def test_f6_admin_tracking_csv_export_import_and_customer_view(isolated_mall_db):
    import app as mall_app
    import mall_database

    _seed_product(101, "플리츠 스커트", price=29000, stock=10)
    u = _create_member("f6buyer@test.com")
    ord1 = mall_database.create_mall_order({
        "user_id": u["id"],
        "buyer_name": "=FormulaUser",
        "buyer_phone": "010-1234-0001",
        "zipcode": "06236",
        "address": "서울시 강남구 역삼로 10",
        "address_detail": "2층",
        "items": [{"num": 101, "color": "블랙", "size": "FREE", "quantity": 1}],
    })
    ord2 = mall_database.create_mall_order({
        "user_id": u["id"],
        "buyer_name": "김정상",
        "buyer_phone": "010-1234-0002",
        "zipcode": "06236",
        "address": "서울시 강남구 역삼로 20",
        "items": [{"num": 101, "color": "블랙", "size": "FREE", "quantity": 1}],
    })
    ord_cancel = mall_database.create_mall_order({
        "user_id": u["id"],
        "buyer_name": "취소주문",
        "buyer_phone": "010-1234-0003",
        "address": "서울시 서초구",
        "items": [{"num": 101, "color": "블랙", "size": "FREE", "quantity": 1}],
    })
    mall_database.cancel_mall_order_by_customer(order_id=ord_cancel["order_id"])

    with mall_app.app.test_client() as admin_cli:
        # Unauth blocked
        assert admin_cli.post("/api/admin/orders/tracking", json={}).status_code == 401
        assert admin_cli.get("/api/admin/orders/export-shipping.csv").status_code == 401

        csrf = _login_client(admin_cli, ADMIN_EMAIL, ADMIN_PASS)

        # Missing CSRF blocked
        assert admin_cli.post("/api/admin/orders/tracking", json={"order_id": ord1["order_id"]}).status_code == 403

        # Single tracking save + mark_shipped
        r_trk = admin_cli.post(
            "/api/admin/orders/tracking",
            headers={"X-CSRF-Token": csrf},
            json={
                "order_id": ord1["order_id"],
                "carrier_code": "CJ",
                "tracking_number": "111122223333",
                "mark_shipped": True,
            },
        )
        assert r_trk.status_code == 200, r_trk.get_json()
        o1_data = r_trk.get_json()["order"]
        assert o1_data["carrier_code"] == "CJ"
        assert o1_data["carrier_name"] == "CJ대한통운"
        assert o1_data["tracking_number"] == "111122223333"
        assert o1_data["order_status"] == "배송중"
        assert o1_data["shipped_at"] != ""
        assert o1_data["tracking_url"].startswith("https://")

        # Export shipping CSV (BOM, formula-safe, excludes cancelled)
        r_exp = admin_cli.get("/api/admin/orders/export-shipping.csv")
        assert r_exp.status_code == 200
        assert r_exp.data.startswith(b"\xef\xbb\xbf")
        exp_text = r_exp.data.decode("utf-8-sig")
        assert ord1["order_code"] in exp_text
        assert ord2["order_code"] in exp_text
        assert ord_cancel["order_code"] not in exp_text
        assert "'=FormulaUser" in exp_text

        # Bulk import via multipart/form-data (CP949 + partial error row)
        csv_body = (
            "주문번호,택배사코드,운송장번호\r\n"
            f"{ord2['order_code']},HANJIN,9988776655\r\n"
            "ORD-NOT-EXIST,CJ,12345\r\n"
        ).encode("cp949")
        r_imp = admin_cli.post(
            "/api/admin/orders/import-tracking",
            headers={"X-CSRF-Token": csrf},
            data={"file": (io.BytesIO(csv_body), "tracking_cp949.csv")},
            content_type="multipart/form-data",
        )
        assert r_imp.status_code == 200, r_imp.get_json()
        imp_json = r_imp.get_json()
        assert imp_json["total_rows"] == 2
        assert imp_json["updated_count"] == 1
        assert imp_json["error_count"] == 1
        assert imp_json["errors"][0]["order_code"] == "ORD-NOT-EXIST"
        assert imp_json["updated_orders"][0]["order_status"] == "배송중"

    # Member views /orders and sees 1-click tracking link
    with mall_app.app.test_client() as buyer_cli:
        _login_client(buyer_cli, "f6buyer@test.com", "MemberPass123!")
        r_orders = buyer_cli.get("/orders")
        assert r_orders.status_code == 200
        html = r_orders.get_data(as_text=True)
        assert "111122223333" in html
        assert "9988776655" in html
        assert "배송 추적하기" in html
        assert 'target="_blank" rel="noopener noreferrer"' in html


def test_f6_bulk_csv_unknown_carrier_rejected_and_direct_preserved(isolated_mall_db):
    import app as mall_app
    import mall_database

    _seed_product(102, "린넨 셔츠", price=33000, stock=10)
    u = _create_member("f6unknown@test.com")
    ord_unknown = mall_database.create_mall_order({
        "user_id": u["id"],
        "buyer_name": "미지원택배",
        "buyer_phone": "010-5555-0001",
        "address": "서울시 강남구",
        "items": [{"num": 102, "color": "블랙", "size": "FREE", "quantity": 1}],
    })
    ord_valid = mall_database.create_mall_order({
        "user_id": u["id"],
        "buyer_name": "정상택배",
        "buyer_phone": "010-5555-0002",
        "address": "서울시 서초구",
        "items": [{"num": 102, "color": "블랙", "size": "FREE", "quantity": 1}],
    })
    ord_direct = mall_database.create_mall_order({
        "user_id": u["id"],
        "buyer_name": "직접배송",
        "buyer_phone": "010-5555-0003",
        "address": "서울시 송파구",
        "items": [{"num": 102, "color": "블랙", "size": "FREE", "quantity": 1}],
    })

    with mall_app.app.test_client() as admin_cli:
        csrf = _login_client(admin_cli, ADMIN_EMAIL, ADMIN_PASS)

        # Single-order update with unknown carrier_name must be rejected (400)
        r_single = admin_cli.post(
            "/api/admin/orders/tracking",
            headers={"X-CSRF-Token": csrf},
            json={
                "order_code": ord_unknown["order_code"],
                "carrier_code": "",
                "carrier_name": "UNKNOWN_CARRIER_NAME",
                "tracking_number": "11112222",
                "mark_shipped": True,
            },
        )
        assert r_single.status_code == 400
        assert "지원하지 않는 택배사입니다" in r_single.get_json()["message"]

        # CP949 bulk CSV with only an unknown carrier in "택배사" -> error_count=1, updated_count=0, order unchanged
        cp949_unknown_only = (
            "주문번호,택배사,운송장번호\r\n"
            f"{ord_unknown['order_code']},UNKNOWN_CARRIER_NAME,11112222\r\n"
        ).encode("cp949")
        r_imp_only = admin_cli.post(
            "/api/admin/orders/import-tracking",
            headers={"X-CSRF-Token": csrf},
            data={"file": (io.BytesIO(cp949_unknown_only), "unknown_only.csv")},
            content_type="multipart/form-data",
        )
        assert r_imp_only.status_code == 200
        j_only = r_imp_only.get_json()
        assert j_only["updated_count"] == 0
        assert j_only["error_count"] == 1
        assert j_only["errors"][0]["order_code"] == ord_unknown["order_code"]
        assert "지원하지 않는 택배사입니다" in j_only["errors"][0]["error"]

        unchanged = mall_database.get_mall_order_raw_by_code_or_id(order_id=ord_unknown["order_id"])
        assert unchanged["order_status"] == "결제대기"
        assert unchanged["carrier_code"] == ""
        assert unchanged["tracking_number"] == ""

        # UTF-8 BOM mixed CSV: unknown carrier fails without aborting valid carrier & explicit 직접배송 alias
        bom_mixed = (
            "\ufeff주문번호,택배사,운송장번호\r\n"
            f"{ord_unknown['order_code']},CJ대한통은,22223333\r\n"
            f"{ord_valid['order_code']},CJ대한통운,44445555\r\n"
            f"{ord_direct['order_code']},직접배송,66667777\r\n"
        ).encode("utf-8")
        r_imp_mixed = admin_cli.post(
            "/api/admin/orders/import-tracking",
            headers={"X-CSRF-Token": csrf},
            data={"file": (io.BytesIO(bom_mixed), "mixed_bom.csv")},
            content_type="multipart/form-data",
        )
        assert r_imp_mixed.status_code == 200
        j_mixed = r_imp_mixed.get_json()
        assert j_mixed["total_rows"] == 3
        assert j_mixed["updated_count"] == 2
        assert j_mixed["error_count"] == 1
        assert j_mixed["errors"][0]["order_code"] == ord_unknown["order_code"]
        assert "지원하지 않는 택배사입니다" in j_mixed["errors"][0]["error"]

        # Order with unknown carrier remains unchanged
        still_unchanged = mall_database.get_mall_order_raw_by_code_or_id(order_id=ord_unknown["order_id"])
        assert still_unchanged["order_status"] == "결제대기"
        assert still_unchanged["carrier_code"] == ""

        # Valid CJ and 직접배송 orders committed cleanly
        cj_ord = mall_database.get_mall_order_raw_by_code_or_id(order_id=ord_valid["order_id"])
        assert cj_ord["order_status"] == "배송중"
        assert cj_ord["carrier_code"] == "CJ"
        assert cj_ord["tracking_number"] == "44445555"

        dir_ord = mall_database.get_mall_order_raw_by_code_or_id(order_id=ord_direct["order_id"])
        assert dir_ord["order_status"] == "배송중"
        assert dir_ord["carrier_code"] == "DIRECT"
        assert dir_ord["carrier_name"] == "직접배송/기타"
        assert dir_ord["tracking_number"] == "66667777"

        # Explicit DIRECT carrier_code with custom carrier_name in CSV still succeeds
        custom_direct_csv = (
            "주문번호,택배사코드,택배사,운송장번호\r\n"
            f"{ord_direct['order_code']},DIRECT,자체퀵배송,88889999\r\n"
        ).encode("cp949")
        r_imp_direct = admin_cli.post(
            "/api/admin/orders/import-tracking",
            headers={"X-CSRF-Token": csrf},
            data={"file": (io.BytesIO(custom_direct_csv), "direct_custom.csv")},
            content_type="multipart/form-data",
        )
        assert r_imp_direct.status_code == 200
        j_dir = r_imp_direct.get_json()
        assert j_dir["updated_count"] == 1
        assert j_dir["error_count"] == 0
        assert j_dir["updated_orders"][0]["carrier_code"] == "DIRECT"
        assert j_dir["updated_orders"][0]["carrier_name"] == "자체퀵배송"


# ==============================================================================
# F7: Self-Cancel (입금대기) & Exchange/Return Claim + Stock Restore + Auth
# ==============================================================================


def test_f7_self_cancel_and_claim_with_stock_restore_and_idor(isolated_mall_db, tmp_path, monkeypatch):
    import app as mall_app
    import mall_database

    # Optional OBS DB stock sync verification
    obs_db = tmp_path / "obs_sync.db"
    conn = sqlite3.connect(str(obs_db))
    conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, num INTEGER UNIQUE)")
    conn.execute("CREATE TABLE product_options (id INTEGER PRIMARY KEY, product_id INTEGER, color TEXT, size TEXT, stock INTEGER)")
    conn.execute("INSERT INTO products (id, num) VALUES (10, 202)")
    conn.execute("INSERT INTO product_options (id, product_id, color, size, stock) VALUES (1, 10, '블랙', 'FREE', 7)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("OBS_DB_PATH", str(obs_db))

    _seed_product(202, "캐시미어 머플러", price=45000, stock=10)
    owner = _create_member("owner@test.com")
    other = _create_member("other@test.com")

    ord_cancel = mall_database.create_mall_order({
        "user_id": owner["id"],
        "buyer_name": "소유자",
        "buyer_phone": "010-2222-3333",
        "address": "서울시 마포구",
        "items": [{"num": 202, "color": "블랙", "size": "FREE", "quantity": 3}],
    })
    assert mall_database.get_mall_product_by_num(202)["total_stock"] == 7

    ord_paid = mall_database.create_mall_order({
        "user_id": owner["id"],
        "buyer_name": "소유자",
        "buyer_phone": "010-2222-3333",
        "address": "서울시 마포구",
        "items": [{"num": 202, "color": "블랙", "size": "FREE", "quantity": 2}],
    })
    assert mall_database.get_mall_product_by_num(202)["total_stock"] == 5
    mall_database.update_mall_order_workflow(ord_paid["order_id"], payment_status="입금완료", order_status="배송완료")

    with mall_app.app.test_client() as anon_cli:
        assert anon_cli.post("/api/orders/self-cancel", json={"order_code": ord_cancel["order_code"]}).status_code == 403

    with mall_app.app.test_client() as other_cli:
        other_csrf = _login_client(other_cli, "other@test.com", "MemberPass123!")
        r_idor_c = other_cli.post(
            "/api/orders/self-cancel",
            headers={"X-CSRF-Token": other_csrf},
            json={"order_code": ord_cancel["order_code"]},
        )
        assert r_idor_c.status_code == 403
        r_idor_cl = other_cli.post(
            "/api/orders/self-claim",
            headers={"X-CSRF-Token": other_csrf},
            json={"order_code": ord_paid["order_code"], "claim_type": "환불요청", "claim_reason": "타인시도"},
        )
        assert r_idor_cl.status_code == 403

    with mall_app.app.test_client() as owner_cli:
        owner_csrf = _login_client(owner_cli, "owner@test.com", "MemberPass123!")
        # CSRF missing -> 403
        assert owner_cli.post("/api/orders/self-cancel", json={"order_code": ord_cancel["order_code"]}).status_code == 403

        # Valid self-cancel restores mall stock (+3 -> 8) and OBS stock (7+3=10)
        r_c = owner_cli.post(
            "/api/orders/self-cancel",
            headers={"X-CSRF-Token": owner_csrf},
            json={"order_code": ord_cancel["order_code"]},
        )
        assert r_c.status_code == 200, r_c.get_json()
        assert r_c.get_json()["order"]["order_status"] == "주문취소"
        assert r_c.get_json()["stock_restored_now"] is True
        assert r_c.get_json()["obs_stock_restored"] == 1
        assert mall_database.get_mall_product_by_num(202)["total_stock"] == 8

        conn_check = sqlite3.connect(str(obs_db))
        obs_stk = conn_check.execute("SELECT stock FROM product_options WHERE id = 1").fetchone()[0]
        conn_check.close()
        assert obs_stk == 10

        # Cannot cancel again or cancel paid order
        r_c2 = owner_cli.post(
            "/api/orders/self-cancel",
            headers={"X-CSRF-Token": owner_csrf},
            json={"order_code": ord_cancel["order_code"]},
        )
        assert r_c2.status_code == 400
        r_c_paid = owner_cli.post(
            "/api/orders/self-cancel",
            headers={"X-CSRF-Token": owner_csrf},
            json={"order_code": ord_paid["order_code"]},
        )
        assert r_c_paid.status_code == 400

        # Empty reason rejected
        r_cl_empty = owner_cli.post(
            "/api/orders/self-claim",
            headers={"X-CSRF-Token": owner_csrf},
            json={"order_code": ord_paid["order_code"], "claim_type": "환불요청", "claim_reason": "   "},
        )
        assert r_cl_empty.status_code == 400

        # Valid claim submission -> 환불요청 / 환불대기
        r_cl = owner_cli.post(
            "/api/orders/self-claim",
            headers={"X-CSRF-Token": owner_csrf},
            json={"order_code": ord_paid["order_code"], "claim_type": "환불요청", "claim_reason": "사이즈가 맞지 않습니다"},
        )
        assert r_cl.status_code == 200, r_cl.get_json()
        cl_ord = r_cl.get_json()["order"]
        assert cl_ord["order_status"] == "환불요청"
        assert cl_ord["payment_status"] == "환불대기"
        assert cl_ord["claim_reason"] == "사이즈가 맞지 않습니다"

    # Admin completes refund -> stock restored once (+2 -> 10)
    with mall_app.app.test_client() as admin_cli:
        adm_csrf = _login_client(admin_cli, ADMIN_EMAIL, ADMIN_PASS)
        r_ref = admin_cli.post(
            "/api/admin/order-status",
            headers={"X-CSRF-Token": adm_csrf},
            json={"order_id": ord_paid["order_id"], "order_status": "환불완료"},
        )
        assert r_ref.status_code == 200
        assert r_ref.get_json()["stock_restored_now"] is True
        assert mall_database.get_mall_product_by_num(202)["total_stock"] == 10

    # Guest 3-factor self-cancel & verified-session self-claim
    guest_ord = mall_database.create_mall_order({
        "is_guest": True,
        "buyer_name": "비회원",
        "buyer_phone": "010-7777-8888",
        "address": "부산시 해운대구",
        "guest_password": "gpass99",
        "items": [{"num": 202, "color": "블랙", "size": "FREE", "quantity": 1}],
    })
    assert mall_database.get_mall_product_by_num(202)["total_stock"] == 9
    with mall_app.app.test_client() as g_cli:
        # Wrong 3-factor -> 403
        assert g_cli.post(
            "/api/orders/self-cancel",
            json={"order_code": guest_ord["order_code"], "buyer_phone": "010-7777-8888", "guest_password": "wrong"},
        ).status_code == 403
        # Valid 3-factor -> 200 & stock restored
        r_gc = g_cli.post(
            "/api/orders/self-cancel",
            json={"order_code": guest_ord["order_code"], "buyer_phone": "010-7777-8888", "guest_password": "gpass99"},
        )
        assert r_gc.status_code == 200
        assert mall_database.get_mall_product_by_num(202)["total_stock"] == 10


# ==============================================================================
# F8: MyPage, Multi-Address Book, Password Change/Reset & Withdrawal
# ==============================================================================


def test_f8_mypage_addresses_password_and_withdrawal(isolated_mall_db):
    import app as mall_app
    import mall_database
    import mall_member_service

    _seed_product(303, "트렌치 코트", price=79000, stock=5)
    user_a = _create_member("alice@test.com", name="앨리스", phone="010-1000-0001")
    user_b = _create_member("bob@test.com", name="밥", phone="010-2000-0002")

    # Registration seeded default address
    a_addrs = mall_member_service.list_user_addresses(user_a["id"])
    assert len(a_addrs) == 1 and a_addrs[0]["is_default"] is True

    # Order for 5-year retention verification
    alice_ord = mall_database.create_mall_order({
        "user_id": user_a["id"],
        "buyer_name": "앨리스",
        "buyer_phone": "010-1000-0001",
        "zipcode": "06236",
        "address": "서울시 강남구 테헤란로 1",
        "items": [{"num": 303, "color": "블랙", "size": "FREE", "quantity": 1}],
    })

    with mall_app.app.test_client() as anon_cli:
        r_mp = anon_cli.get("/mypage")
        assert r_mp.status_code == 302 and "/auth/login" in r_mp.headers["Location"]
        assert anon_cli.get("/api/mypage/addresses").status_code == 401
        assert anon_cli.post("/api/mypage/profile", json={"name": "x"}).status_code == 401

    with mall_app.app.test_client() as a_cli:
        a_csrf = _login_client(a_cli, "alice@test.com", "MemberPass123!")

        # Missing CSRF -> 403
        assert a_cli.post("/api/mypage/profile", json={"name": "앨리스2", "phone": "010-1000-0001"}).status_code == 403

        # Profile update + default address sync
        r_prof = a_cli.post(
            "/api/mypage/profile",
            headers={"X-CSRF-Token": a_csrf},
            json={
                "name": "앨리스수정",
                "phone": "010-1000-9999",
                "zipcode": "04524",
                "address": "서울시 중구 세종대로 110",
                "address_detail": "303호",
            },
        )
        assert r_prof.status_code == 200
        assert r_prof.get_json()["user"]["zipcode"] == "04524"
        assert r_prof.get_json()["addresses"][0]["address"] == "서울시 중구 세종대로 110"

        # Add 2nd address as default
        r_add2 = a_cli.post(
            "/api/mypage/addresses/save",
            headers={"X-CSRF-Token": a_csrf},
            json={
                "label": "회사",
                "recipient_name": "앨리스회사",
                "recipient_phone": "010-1000-8888",
                "zipcode": "13494",
                "address": "경기도 성남시 분당구 판교역로 1",
                "address_detail": "5층",
                "is_default": True,
            },
        )
        assert r_add2.status_code == 200
        addrs_after = r_add2.get_json()["addresses"]
        assert len(addrs_after) == 2
        assert sum(1 for x in addrs_after if x["is_default"]) == 1
        default_addr_id = next(x["id"] for x in addrs_after if x["is_default"])

        # IDOR: user_b cannot modify/delete/default user_a's address
        with mall_app.app.test_client() as b_cli:
            b_csrf = _login_client(b_cli, "bob@test.com", "MemberPass123!")
            assert b_cli.post(
                "/api/mypage/addresses/delete",
                headers={"X-CSRF-Token": b_csrf},
                json={"id": default_addr_id},
            ).status_code == 404
            assert b_cli.post(
                "/api/mypage/addresses/default",
                headers={"X-CSRF-Token": b_csrf},
                json={"id": default_addr_id},
            ).status_code == 404

        # Delete default -> remaining auto-promoted
        r_del = a_cli.post(
            "/api/mypage/addresses/delete",
            headers={"X-CSRF-Token": a_csrf},
            json={"id": default_addr_id},
        )
        assert r_del.status_code == 200
        rem = r_del.get_json()["addresses"]
        assert len(rem) == 1 and rem[0]["is_default"] is True

        # Password change
        assert a_cli.post(
            "/api/mypage/password",
            headers={"X-CSRF-Token": a_csrf},
            json={"current_password": "WrongPass1!", "new_password": "NewPass123!"},
        ).status_code == 400

        r_pw = a_cli.post(
            "/api/mypage/password",
            headers={"X-CSRF-Token": a_csrf},
            json={"current_password": "MemberPass123!", "new_password": "NewPass123!", "confirm_password": "NewPass123!"},
        )
        assert r_pw.status_code == 200
        a_csrf = r_pw.get_json()["csrf_token"]

        # Withdraw wrong password -> 400
        assert a_cli.post(
            "/api/mypage/withdraw",
            headers={"X-CSRF-Token": a_csrf},
            json={"password": "WrongPass1!"},
        ).status_code == 400

        # Withdraw valid -> 200, session invalidated, orders retained, PII wiped, login blocked
        r_wd = a_cli.post(
            "/api/mypage/withdraw",
            headers={"X-CSRF-Token": a_csrf},
            json={"password": "NewPass123!"},
        )
        assert r_wd.status_code == 200
        assert a_cli.get("/api/mypage/addresses").status_code == 401
        assert a_cli.post("/auth/login", json={"email": "alice@test.com", "password": "NewPass123!"}).status_code == 403

        # 5-year statutory retention: order still exists in mall_orders
        retained_orders = mall_database.get_mall_order_by_code(alice_ord["order_code"])
        assert len(retained_orders) == 1

    # Admin self-withdraw blocked (403)
    with mall_app.app.test_client() as adm_cli:
        adm_csrf = _login_client(adm_cli, ADMIN_EMAIL, ADMIN_PASS)
        assert adm_cli.post(
            "/api/mypage/withdraw",
            headers={"X-CSRF-Token": adm_csrf},
            json={"password": ADMIN_PASS},
        ).status_code == 403


def test_f8_password_reset_token_code_logging_and_security(isolated_mall_db):
    import app as mall_app
    import mall_database

    _create_member("resetuser@test.com", name="리셋유저")

    with mall_app.app.test_client() as cli:
        r_get = cli.get("/auth/forgot-password")
        assert r_get.status_code == 200
        csrf = _get_csrf_from_html(r_get.get_data(as_text=True))

        # Unknown email still returns generic 200 without leaking token/code
        r_unknown = cli.post(
            "/auth/forgot-password",
            headers={"X-CSRF-Token": csrf},
            json={"email": "nobody@test.com"},
        )
        assert r_unknown.status_code == 200
        assert "token" not in r_unknown.get_data(as_text=True).lower()

        # Registered email logs LOGGED_ONLY to mall_notification_logs
        r_known = cli.post(
            "/auth/forgot-password",
            headers={"X-CSRF-Token": csrf},
            json={"email": "resetuser@test.com"},
        )
        assert r_known.status_code == 200
        body_txt = r_known.get_data(as_text=True)
        assert "token=" not in body_txt

        conn = mall_database.get_connection()
        log_row = conn.execute("SELECT * FROM mall_notification_logs ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        assert log_row is not None
        assert log_row["status"] == "LOGGED_ONLY"
        assert log_row["event_type"] == "PASSWORD_RESET"
        assert log_row["recipient"] == "resetuser@test.com"
        assert log_row["ref_code"] == "resetuser@test.com"
        assert log_row["error_msg"] != ""

        m_tok = re.search(r"token=([A-Za-z0-9_\-]+)", log_row["body"])
        m_code = re.search(r"인증 코드:\s*(\d{6})", log_row["body"])
        assert m_tok and m_code
        raw_token = m_tok.group(1)
        temp_code = m_code.group(1)

        # Wrong 6-digit code fails & increments attempts; 5 failures invalidate
        for _ in range(5):
            r_bad = cli.post(
                "/auth/reset-password",
                headers={"X-CSRF-Token": csrf},
                json={
                    "email": "resetuser@test.com",
                    "code": "000000" if temp_code != "000000" else "999999",
                    "new_password": "ResetPass123!",
                    "confirm_password": "ResetPass123!",
                },
            )
            assert r_bad.status_code == 400

        # Now even the real token/code is rejected because attempts exceeded
        r_after_lock = cli.post(
            "/auth/reset-password",
            headers={"X-CSRF-Token": csrf},
            json={
                "email": "resetuser@test.com",
                "code": temp_code,
                "new_password": "ResetPass123!",
                "confirm_password": "ResetPass123!",
            },
        )
        assert r_after_lock.status_code == 400

        # Request fresh reset & verify 6-digit code reset works + single-use
        cli.post("/auth/forgot-password", headers={"X-CSRF-Token": csrf}, json={"email": "resetuser@test.com"})
        conn = mall_database.get_connection()
        log_row2 = conn.execute("SELECT * FROM mall_notification_logs ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        code2 = re.search(r"인증 코드:\s*(\d{6})", log_row2["body"]).group(1)

        r_ok_code = cli.post(
            "/auth/reset-password",
            headers={"X-CSRF-Token": csrf},
            json={
                "email": "resetuser@test.com",
                "code": code2,
                "new_password": "NewResetPass1!",
                "confirm_password": "NewResetPass1!",
            },
        )
        assert r_ok_code.status_code == 200
        # Reuse rejected
        assert cli.post(
            "/auth/reset-password",
            headers={"X-CSRF-Token": csrf},
            json={"email": "resetuser@test.com", "code": code2, "new_password": "NewResetPass2!"},
        ).status_code == 400

        # Request another reset & verify token-link reset works + single-use
        cli.post("/auth/forgot-password", headers={"X-CSRF-Token": csrf}, json={"email": "resetuser@test.com"})
        conn = mall_database.get_connection()
        log_row3 = conn.execute("SELECT * FROM mall_notification_logs ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        tok3 = re.search(r"token=([A-Za-z0-9_\-]+)", log_row3["body"]).group(1)

        r_ok_tok = cli.post(
            "/auth/reset-password",
            headers={"X-CSRF-Token": csrf},
            json={"token": tok3, "new_password": "TokenResetPass9!", "confirm_password": "TokenResetPass9!"},
        )
        assert r_ok_tok.status_code == 200
        assert cli.post("/auth/login", json={"email": "resetuser@test.com", "password": "TokenResetPass9!"}).status_code == 200


# ==============================================================================
# F9: Admin Member Management & Immediate Session Invalidation
# ==============================================================================


def test_f9_admin_users_stats_and_session_invalidation(isolated_mall_db):
    import app as mall_app
    import mall_database

    _seed_product(404, "가디건", price=20000, stock=20)
    m1 = _create_member("m1@test.com", name="김구매", phone="010-3333-1111")
    m2 = _create_member("m2@test.com", name="이탈퇴", phone="010-4444-2222")

    # 2 valid orders (subtotal 20000 + shipping 4000 = 24000 each => 48000), 1 cancelled order (excluded!)
    mall_database.create_mall_order({
        "user_id": m1["id"],
        "buyer_name": "김구매",
        "buyer_phone": "010-3333-1111",
        "address": "서울시 강남구",
        "items": [{"num": 404, "color": "블랙", "size": "FREE", "quantity": 1}],
    })
    mall_database.create_mall_order({
        "user_id": m1["id"],
        "buyer_name": "김구매",
        "buyer_phone": "010-3333-1111",
        "address": "서울시 강남구",
        "items": [{"num": 404, "color": "블랙", "size": "FREE", "quantity": 1}],
    })
    oc = mall_database.create_mall_order({
        "user_id": m1["id"],
        "buyer_name": "김구매",
        "buyer_phone": "010-3333-1111",
        "address": "서울시 강남구",
        "items": [{"num": 404, "color": "블랙", "size": "FREE", "quantity": 1}],
    })
    mall_database.cancel_mall_order_by_customer(order_id=oc["order_id"])

    # Withdraw m2
    import mall_member_service
    mall_member_service.withdraw_member_account(m2["id"], "MemberPass123!")

    # Hold active cookie session for m1 BEFORE suspending
    m1_cli = mall_app.app.test_client()
    adm_cli = mall_app.app.test_client()
    anon_cli = mall_app.app.test_client()

    with m1_cli:
        _login_client(m1_cli, "m1@test.com", "MemberPass123!")
        assert m1_cli.get("/api/mypage/addresses").status_code == 200
        # Member blocked from admin users API
        assert m1_cli.get("/api/admin/users").status_code == 403

    # Unauthenticated blocked
    with anon_cli:
        assert anon_cli.get("/api/admin/users").status_code == 401

    with adm_cli:
        adm_csrf = _login_client(adm_cli, ADMIN_EMAIL, ADMIN_PASS)
        r_list = adm_cli.get("/api/admin/users")
        assert r_list.status_code == 200
        users = r_list.get_json()["users"]
        assert all("password_hash" not in u for u in users)

        u_m1 = next(u for u in users if u["email"] == "m1@test.com")
        u_m2 = next(u for u in users if u["email"] == "m2@test.com")
        u_adm = next(u for u in users if u["email"] == ADMIN_EMAIL)
        assert u_m1["order_count"] == 2
        assert u_m1["total_spent"] == 48000
        assert u_m2["withdrawn_at"] != ""
        assert u_m2["account_status"] == "WITHDRAWN"

        # Self-suspension blocked (400)
        assert adm_cli.post(
            "/api/admin/users/status",
            headers={"X-CSRF-Token": adm_csrf},
            json={"user_id": u_adm["id"], "is_active": 0},
        ).status_code == 400

        # Reactivating withdrawn user blocked (400)
        assert adm_cli.post(
            "/api/admin/users/status",
            headers={"X-CSRF-Token": adm_csrf},
            json={"user_id": u_m2["id"], "is_active": 1},
        ).status_code == 400

        # Suspend m1 -> m1's existing session immediately invalidated (401) and login blocked (403)
        r_susp = adm_cli.post(
            "/api/admin/users/status",
            headers={"X-CSRF-Token": adm_csrf},
            json={"user_id": u_m1["id"], "is_active": 0},
        )
        assert r_susp.status_code == 200
        assert r_susp.get_json()["user"]["is_active"] is False

    with m1_cli:
        assert m1_cli.get("/api/mypage/addresses").status_code == 401
        assert m1_cli.post("/auth/login", json={"email": "m1@test.com", "password": "MemberPass123!"}).status_code == 403

    # Reactivate m1 -> login works again
    with adm_cli:
        r_act = adm_cli.post(
            "/api/admin/users/status",
            headers={"X-CSRF-Token": adm_csrf},
            json={"user_id": u_m1["id"], "is_active": 1},
        )
        assert r_act.status_code == 200

    with m1_cli:
        assert m1_cli.post("/auth/login", json={"email": "m1@test.com", "password": "MemberPass123!"}).status_code == 200


# ==============================================================================
# Playwright Desktop (1440x900) & Mobile (390x844) E2E Verification
# ==============================================================================


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
def live_mall_server(tmp_path):
    port = _find_free_port()
    db_file = tmp_path / "m2_e2e.db"
    env = os.environ.copy()
    env["MALL_PORT"] = str(port)
    env["MALL_DB_PATH"] = str(db_file)
    env["OBS_PULL_ENABLED"] = "0"
    env["MALL_MODE"] = "cloud"
    env["MALL_SCHEDULER_ENABLED"] = "0"
    env["MALL_ADMIN_EMAIL"] = ADMIN_EMAIL
    env["MALL_ADMIN_PASSWORD"] = ADMIN_PASS

    import mall_database

    mall_database.set_db_path(str(db_file), init=True)
    mall_database.ensure_default_mall_admin()
    _seed_product(501, "이투이 울 코트", price=40000, stock=10)
    _seed_product(502, "이투이 셔츠", price=25000, stock=10)

    log_path = tmp_path / "mall_m2_server.log"
    log_f = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(MALL_DIR / "app.py")],
        cwd=str(MALL_DIR),
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 15.0
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                log_f.flush()
                raise RuntimeError(f"Server exited early: {log_path.read_text(encoding='utf-8', errors='replace')}")
            try:
                with urllib.request.urlopen(f"{base_url}/api/live/state-lite", timeout=1.0) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.15)
        if not ready:
            raise RuntimeError("Timed out waiting for live_mall_server")
        yield base_url, db_file
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        log_f.close()


def _attach_console_collector(page):
    errors = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda err: errors.append(str(err)))
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
            return { scrollWidth: doc.scrollWidth, innerWidth: window.innerWidth, overflow, badScrollEls };
        }"""
    )
    assert not metrics["overflow"], f"Horizontal overflow detected: {metrics}"
    assert metrics["badScrollEls"] == [], f"Internal vertical scroll detected: {metrics['badScrollEls']}"


def _stub_daum_postcode_script(zonecode="06236", road="서울 강남구 테헤란로 152"):
    return f"""
    window.daum = {{
        Postcode: function(opts) {{
            return {{
                open: function() {{
                    if (opts && typeof opts.oncomplete === 'function') {{
                        opts.oncomplete({{
                            zonecode: '{zonecode}',
                            userSelectedType: 'R',
                            roadAddress: '{road}',
                            jibunAddress: '{road}',
                            bname: '역삼동',
                            buildingName: '강남파이낸스센터',
                            apartment: 'N'
                        }});
                    }}
                }}
            }};
        }}
    }};
    """


def test_e2e_m2_desktop_and_mobile_flows(live_mall_server):
    base_url, db_file = live_mall_server

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            # 1. Desktop (1440x900) Member MyPage -> Address Book -> Checkout Picker -> Orders -> Admin Logistics & Members -> Reset -> Withdraw
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            ctx.route(
                "**://t1.daumcdn.net/**",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/javascript",
                    body=_stub_daum_postcode_script(),
                ),
            )
            page = ctx.new_page()
            console_errors = _attach_console_collector(page)

            # Register new member via UI
            page.goto(f"{base_url}/auth/register", wait_until="domcontentloaded")
            page.fill("#regEmail", "e2e_member@test.com")
            page.fill("#regPassword", "E2ePass123!")
            page.fill("#regName", "박이투이")
            page.fill("#regPhone", "010-9090-1212")
            page.click("#btnRegPostcode")
            page.wait_for_function("document.getElementById('regZipcode').value === '06236'")
            page.fill("#regAddressDetail", "10층")
            page.check("#regAgreeTerms")
            page.check("#regAgreePrivacy")
            page.click('button[type="submit"]')
            page.wait_for_url(f"{base_url}/")

            # Visit MyPage via header link & manage address book
            page.click("#navMyPageLink")
            page.wait_for_url(f"{base_url}/mypage")
            _assert_layout_clean(page)

            page.click("#mypageTabAddresses")
            page.fill("#addrLabel", "우리집")
            page.fill("#addrRecipientName", "박수령")
            page.fill("#addrRecipientPhone", "010-9090-3434")
            page.click("#btnAddrPostcode")
            page.wait_for_function("document.getElementById('addrZipcode').value === '06236'")
            page.fill("#addrDetail", "202호")
            with page.expect_response(lambda r: r.url.endswith("/api/mypage/addresses/save") and r.request.method == "POST") as addr_resp:
                page.click("#btnSaveAddress")
            assert addr_resp.value.status == 200
            page.wait_for_selector('#mypageAddressList .ax-address-card:has-text("우리집")')
            _assert_layout_clean(page)

            # Open product #501 -> Instant Buy address picker
            page.goto(f"{base_url}/product/501", wait_until="domcontentloaded")
            page.locator('button[onclick="pdInstantBuy()"]').first.click()
            page.wait_for_selector("#ibAddressBookSelect option[value]", state="attached")
            _assert_layout_clean(page)
            page.click("#instantBuyModal .ax-icon-btn")

            # Add #501 to cart -> Checkout -> select saved address -> place order
            page.locator('button[onclick="pdAddToCart()"]').first.click()
            page.goto(f"{base_url}/checkout", wait_until="domcontentloaded")
            page.wait_for_selector("#coAddressBookSelect option[value]", state="attached")
            _assert_layout_clean(page)
            with page.expect_response(lambda r: r.url.endswith("/api/orders/create") and r.request.method == "POST") as ord_resp:
                page.click("#coSubmitBtn")
            assert ord_resp.value.status == 200
            page.wait_for_url(f"{base_url}/order-complete/ORD-**", timeout=5000)
            _assert_layout_clean(page)
            page.goto(f"{base_url}/orders", wait_until="domcontentloaded")
            _assert_layout_clean(page)
            ord_code_1 = page.locator(".ax-order-card .ax-order-card-head .ax-code-chip").first.inner_text().strip()

            # Place 2nd order to test self-cancel
            page.goto(f"{base_url}/product/502", wait_until="domcontentloaded")
            page.locator('button[onclick="pdAddToCart()"]').first.click()
            page.goto(f"{base_url}/checkout", wait_until="domcontentloaded")
            with page.expect_response(lambda r: r.url.endswith("/api/orders/create") and r.request.method == "POST") as ord_resp2:
                page.click("#coSubmitBtn")
            assert ord_resp2.value.status == 200
            page.wait_for_url(f"{base_url}/order-complete/ORD-**", timeout=5000)
            page.goto(f"{base_url}/orders", wait_until="domcontentloaded")
            ord_code_2 = page.locator(".ax-order-card .ax-order-card-head .ax-code-chip").first.inner_text().strip()

            # Self-cancel 2nd order on /orders
            page.on("dialog", lambda d: d.accept())
            with page.expect_response(lambda r: r.url.endswith("/api/orders/self-cancel") and r.request.method == "POST") as sc_resp:
                page.click(f'.js-self-cancel-btn[data-order-code="{ord_code_2}"]')
            assert sc_resp.value.status == 200
            page.wait_for_selector(f'.ax-order-card:has-text("{ord_code_2}") .ax-badge:has-text("주문취소")')

            # Admin context: update tracking on ord_code_1 (F6 UI), verify Members tab (F9 UI)
            adm_ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            adm_page = adm_ctx.new_page()
            adm_errors = _attach_console_collector(adm_page)
            adm_page.goto(f"{base_url}/auth/login?next=/admin", wait_until="domcontentloaded")
            adm_page.fill('input[name="email"]', ADMIN_EMAIL)
            adm_page.fill('input[name="password"]', ADMIN_PASS)
            adm_page.click('button[type="submit"]')
            adm_page.wait_for_url(f"{base_url}/admin**")
            _assert_layout_clean(adm_page)

            adm_page.click("#adminTabOrders")
            adm_page.wait_for_selector('#adminOrdersList .ax-admin-order-card')
            _assert_layout_clean(adm_page)
            # Find ord_code_1 card and save tracking
            card = adm_page.locator(f'.ax-admin-order-card:has-text("{ord_code_1}")')
            card_id = card.get_attribute("data-order-card-id")
            adm_page.select_option(f"#orderCarrier_{card_id}", "CJ")
            adm_page.fill(f"#orderTracking_{card_id}", "777788889999")
            with adm_page.expect_response(lambda r: r.url.endswith("/api/admin/orders/tracking") and r.request.method == "POST") as trk_resp:
                card.locator('button:has-text("송장 저장")').click()
            assert trk_resp.value.status == 200
            adm_page.wait_for_selector(f'.ax-admin-order-card[data-order-card-id="{card_id}"] a:has-text("추적 링크")')

            # Check Members tab & search
            adm_page.click("#adminTabMembers")
            adm_page.wait_for_selector('#adminMembersTbody tr:has-text("e2e_member@test.com")')
            _assert_layout_clean(adm_page)

            # Customer reloads /orders -> sees 1-click tracking link AND submits claim via #orderClaimModal
            page.reload(wait_until="domcontentloaded")
            _assert_layout_clean(page)
            assert "777788889999" in page.content()
            assert "배송 추적하기" in page.content()
            page.click(f'.js-self-claim-btn[data-order-code="{ord_code_1}"]')
            page.wait_for_selector("#orderClaimModal.is-open")
            page.select_option("#claimTypeSelect", "교환요청")
            page.fill("#claimReasonTextarea", "사이즈 교환 요청합니다")
            with page.expect_response(lambda r: r.url.endswith("/api/orders/self-claim") and r.request.method == "POST") as cl_resp:
                page.click("#btnSubmitOrderClaim")
            assert cl_resp.value.status == 200
            page.wait_for_selector('.ax-order-claim-note:has-text("사이즈 교환 요청합니다")')

            # Forgot password page check (before withdraw) & Withdraw member
            page.goto(f"{base_url}/mypage", wait_until="domcontentloaded")
            page.click("#mypageTabWithdraw")
            page.fill("#mpWithdrawPassword", "E2ePass123!")
            page.check("#mpWithdrawAgree")
            with page.expect_response(lambda r: r.url.endswith("/api/mypage/withdraw") and r.request.method == "POST") as wd_resp:
                page.click("#btnWithdrawAccount")
            assert wd_resp.value.status == 200
            page.wait_for_url(f"{base_url}/")

            page.goto(f"{base_url}/auth/login", wait_until="domcontentloaded")
            page.click("#linkForgotPassword")
            page.wait_for_url(f"{base_url}/auth/forgot-password")
            _assert_layout_clean(page)

            # Admin refreshes Members tab and sees withdrawn status
            adm_page.click("#btnRefreshAdminMembers")
            adm_page.wait_for_selector('#adminMembersTbody tr:has-text("탈퇴")')
            _assert_layout_clean(adm_page)

            assert adm_errors == [], f"Admin console errors: {adm_errors}"
            adm_ctx.close()
            assert console_errors == [], f"Desktop console errors: {console_errors}"
            ctx.close()

            # 2. Mobile (390x844) layout & tracking/claim/mypage responsiveness
            mob_ctx = browser.new_context(viewport={"width": 390, "height": 844})
            mob_ctx.route(
                "**://t1.daumcdn.net/**",
                lambda route: route.fulfill(status=200, content_type="application/javascript", body="/* offline */"),
            )
            mob = mob_ctx.new_page()
            mob_errors = _attach_console_collector(mob)

            mob.goto(f"{base_url}/auth/login", wait_until="domcontentloaded")
            _assert_layout_clean(mob)
            mob.fill('input[name="email"]', ADMIN_EMAIL)
            mob.fill('input[name="password"]', ADMIN_PASS)
            mob.click('button[type="submit"]')
            mob.wait_for_url(f"{base_url}/")

            mob.goto(f"{base_url}/mypage", wait_until="domcontentloaded")
            _assert_layout_clean(mob)
            mob.goto(f"{base_url}/orders", wait_until="domcontentloaded")
            _assert_layout_clean(mob)
            mob.goto(f"{base_url}/admin", wait_until="domcontentloaded")
            mob.click("#adminTabMembers")
            mob.wait_for_selector("#adminMembersTbody tr")
            _assert_layout_clean(mob)

            assert mob_errors == [], f"Mobile console errors: {mob_errors}"
            mob_ctx.close()
        finally:
            browser.close()
