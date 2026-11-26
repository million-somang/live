"""Smoke tests verifying E2E harness isolation, API/CSRF client, and Playwright helpers."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from e2e.helpers import (
    assert_no_horizontal_overflow,
    assert_no_main_internal_vertical_scroll,
    attach_page_diagnostics,
    create_order_via_api,
    create_tiny_png_bytes,
    mock_daum_postcode,
    seed_product_via_sync,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
MALL_ROOT = Path(__file__).resolve().parents[2]

PROTECTED_WORKSPACE_FILES = [
    REPO_ROOT / "live_commerce.db",
    REPO_ROOT / ".studio_secret_key",
    MALL_ROOT / "shopping_mall.db",
    MALL_ROOT / ".mall_secret_key",
    MALL_ROOT / ".mall_admin_credentials.txt",
]


def _snapshot_files(paths: list[Path]) -> dict[Path, tuple[bool, int, int]]:
    snap: dict[Path, tuple[bool, int, int]] = {}
    for p in paths:
        if p.exists():
            st = p.stat()
            snap[p] = (True, st.st_size, st.st_mtime_ns)
        else:
            snap[p] = (False, -1, -1)
    return snap


@pytest.mark.smoke
def test_smoke_isolated_db_and_server_restart_persistence(
    live_server,
    server_factory,
    api_client_factory,
    tmp_path: Path,
):
    before = _snapshot_files(PROTECTED_WORKSPACE_FILES)

    assert live_server.db_path.exists()
    assert live_server.db_path.resolve() != (MALL_ROOT / "shopping_mall.db").resolve()
    assert str(MALL_ROOT) not in str(live_server.db_path.parent)

    client = api_client_factory(live_server)
    catalog_resp = client.get("/api/catalog")
    assert catalog_resp.status_code == 200
    catalog_data = client.json_or_raise(catalog_resp, "catalog")
    assert catalog_data["status"] == "ok"
    assert len(catalog_data["products"]) >= 6

    # Verify server_factory creates a separate DB and persists data across restart on same work_dir.
    custom_work_dir = tmp_path / "restart_srv"
    srv1 = server_factory(seed_products=False, work_dir=custom_work_dir)
    assert srv1.db_path.parent == custom_work_dir
    seed_product_via_sync(
        srv1.base_url,
        srv1.sync_secret,
        num=777,
        name="재시작 영속성 테스트 상품",
        price=33000,
        category="ACC",
        options=[{"color": "네이비", "size": "ONE", "stock": 5}],
    )
    srv1.stop()

    srv2 = server_factory(seed_products=False, work_dir=custom_work_dir)
    c2 = api_client_factory(srv2)
    prod_resp = c2.get("/api/products/777")
    assert prod_resp.status_code == 200
    prod_data = c2.json_or_raise(prod_resp, "restart_product")
    assert prod_data["product"]["name"] == "재시작 영속성 테스트 상품"
    assert prod_data["product"]["total_stock"] == 5

    after = _snapshot_files(PROTECTED_WORKSPACE_FILES)
    assert after == before


@pytest.mark.smoke
def test_smoke_seed_product_via_sync_and_storefront_routes(
    live_server,
    guest_api,
):
    seeded = seed_product_via_sync(
        live_server.base_url,
        live_server.sync_secret,
        num=888,
        name="스모크 시딩 원피스",
        price=55000,
        consumer_price=69000,
        live_price=49000,
        category="DRESS",
        options=[{"color": "라벤더", "size": "S", "stock": 9}],
    )
    assert seeded["num"] == 888
    assert seeded["total_stock"] == 9

    detail_api = guest_api.get("/api/products/888")
    assert detail_api.status_code == 200
    detail_json = guest_api.json_or_raise(detail_api, "product_888")
    assert detail_json["status"] == "ok"
    assert detail_json["product"]["price"] == 55000
    assert detail_json["product"]["total_stock"] == 9

    missing_api = guest_api.get("/api/products/999999")
    assert missing_api.status_code == 404

    home_resp = guest_api.get("/")
    assert home_resp.status_code == 200
    assert "골라방 LIVE" in home_resp.text

    detail_page = guest_api.get("/product/888")
    assert detail_page.status_code == 200
    assert "스모크 시딩 원피스" in detail_page.text


@pytest.mark.smoke
def test_smoke_csrf_enforcement_and_session_rotation(
    live_server,
    guest_api,
    member_api,
    admin_api,
    server_factory,
    api_client_factory,
):
    before = _snapshot_files(PROTECTED_WORKSPACE_FILES)

    new_product_payload = {
        "name": "CSRF 검증 테스트 상품",
        "price": 31000,
        "category": "TOP",
        "options": [{"color": "블랙", "size": "M", "stock": 10}],
    }

    r_guest = guest_api.post("/api/admin/products/add", json=new_product_payload)
    assert r_guest.status_code == 401

    r_member = member_api.post("/api/admin/products/add", json=new_product_payload)
    assert r_member.status_code == 403

    r_admin_no_csrf = admin_api.post(
        "/api/admin/products/add",
        json=new_product_payload,
        include_csrf=False,
    )
    assert r_admin_no_csrf.status_code == 403

    r_admin_ok = admin_api.post("/api/admin/products/add", json=new_product_payload)
    assert r_admin_ok.status_code == 200
    ok_data = admin_api.json_or_raise(r_admin_ok, "admin_add_product")
    assert ok_data["status"] == "ok"
    assert ok_data["product"]["name"] == "CSRF 검증 테스트 상품"

    # Verify CSRF rotation on /auth/register and /auth/login using an isolated server.
    auth_srv = server_factory(seed_products=True)
    c = api_client_factory(auth_srv)
    pre_csrf = c.refresh_csrf("/auth/register")
    assert pre_csrf

    reg_data = c.register("rotate_smoke@e2e.local", "RotatePass123!")
    assert reg_data["status"] == "ok"
    assert c.csrf_token
    assert c.csrf_token != pre_csrf

    c.logout()
    assert c.csrf_token == ""

    login_data = c.login("rotate_smoke@e2e.local", "RotatePass123!")
    assert login_data["status"] == "ok"
    assert c.csrf_token

    after = _snapshot_files(PROTECTED_WORKSPACE_FILES)
    assert after == before


@pytest.mark.smoke
def test_smoke_create_order_and_guest_three_factor_isolation(
    live_server,
    api_client_factory,
):
    order_client = api_client_factory(live_server)
    order = create_order_via_api(
        order_client,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_name="스모크비회원",
        buyer_phone="010-7777-8888",
        zipcode="06236",
        address="서울특별시 강남구 테헤란로 152",
        address_detail="202호",
        guest_password="guestpass99",
    )
    assert order["order_code"].startswith("ORD-")

    fresh_guest = api_client_factory(live_server)
    anon_resp = fresh_guest.get("/api/orders/lookup")
    assert anon_resp.status_code == 200
    assert fresh_guest.json_or_raise(anon_resp)["count"] == 0

    wrong_pw_resp = fresh_guest.post(
        "/api/orders/lookup",
        json={
            "order_code": order["order_code"],
            "buyer_phone": "010-7777-8888",
            "guest_password": "wrongpassword",
        },
        include_csrf=False,
    )
    assert wrong_pw_resp.status_code == 200
    assert fresh_guest.json_or_raise(wrong_pw_resp)["count"] == 0

    valid_resp = fresh_guest.post(
        "/api/orders/lookup",
        json={
            "order_code": order["order_code"],
            "buyer_phone": "010-7777-8888",
            "guest_password": "guestpass99",
        },
        include_csrf=False,
    )
    assert valid_resp.status_code == 200
    valid_json = fresh_guest.json_or_raise(valid_resp)
    assert valid_json["count"] == 1
    found_order = valid_json["orders"][0]
    assert found_order["order_code"] == order["order_code"]
    assert "guest_password_hash" not in found_order


@pytest.mark.smoke
def test_smoke_create_tiny_png_bytes_valid_ihdr():
    png = create_tiny_png_bytes()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert png[12:16] == b"IHDR"
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (1, 1)


@pytest.mark.smoke
@pytest.mark.layout
def test_smoke_playwright_desktop_mobile_brand_layout_auth_and_daum_mock(
    live_server,
    desktop_page,
    mobile_page,
    desktop_context_factory,
    admin_storage_state,
    member_storage_state,
):
    for label, page in [("desktop", desktop_page), ("mobile", mobile_page)]:
        diag = attach_page_diagnostics(page)
        mock_daum_postcode(page, zipcode="06236", address="서울특별시 강남구 테헤란로 152")

        for route in ["/", "/product/1"]:
            diag.clear()
            resp = page.goto(f"{live_server.base_url}{route}", wait_until="domcontentloaded")
            assert resp is not None and resp.status == 200

            body_text = page.locator("body").inner_text()
            assert "골라방 LIVE" in body_text
            assert "THE VINE LIVE MALL" not in body_text

            assert_no_horizontal_overflow(page)
            assert_no_main_internal_vertical_scroll(page)
            diag.assert_clean(f"{label}:{route}")

        daum_result = page.evaluate(
            """() => new Promise((resolve) => {
                new window.daum.Postcode({
                    oncomplete: (data) => resolve(data)
                }).open();
            })"""
        )
        assert daum_result["zonecode"] == "06236"
        assert daum_result["address"] == "서울특별시 강남구 테헤란로 152"

    # Verify admin_storage_state & member_storage_state work cleanly in browser contexts.
    admin_ctx = desktop_context_factory(storage_state=admin_storage_state)
    admin_page = admin_ctx.new_page()
    admin_diag = attach_page_diagnostics(admin_page)
    admin_resp = admin_page.goto(f"{live_server.base_url}/admin", wait_until="domcontentloaded")
    assert admin_resp is not None and admin_resp.status == 200
    assert "/auth/login" not in admin_page.url
    assert_no_horizontal_overflow(admin_page)
    assert_no_main_internal_vertical_scroll(admin_page)
    admin_diag.assert_clean("desktop:/admin")

    member_ctx = desktop_context_factory(storage_state=member_storage_state)
    member_page = member_ctx.new_page()
    member_diag = attach_page_diagnostics(member_page)
    member_resp = member_page.goto(f"{live_server.base_url}/orders", wait_until="domcontentloaded")
    assert member_resp is not None and member_resp.status == 200
    assert "로그아웃" in member_page.locator("body").inner_text()
    assert_no_horizontal_overflow(member_page)
    assert_no_main_internal_vertical_scroll(member_page)
    member_diag.assert_clean("desktop:/orders")
