# -*- coding: utf-8 -*-
"""
M8: 3단계 계층형 카테고리 & 메인 상품 개수 배지 제거 단위/통합 테스트
"""

import os
import sqlite3
import sys
from pathlib import Path

import pytest

MALL_DIR = Path(__file__).resolve().parents[2]
if str(MALL_DIR) not in sys.path:
    sys.path.insert(0, str(MALL_DIR))

ADMIN_EMAIL = "m8admin@golabang.test"
ADMIN_PASS = "M8AdminPass!2026"


@pytest.fixture(scope="session", autouse=True)
def _bootstrap_isolated_env(tmp_path_factory):
    boot_dir = tmp_path_factory.mktemp("m8_boot")
    os.environ["MALL_DB_PATH"] = str(boot_dir / "boot_m8.db")
    os.environ["OBS_PULL_ENABLED"] = "0"
    os.environ["MALL_MODE"] = "cloud"
    os.environ["MALL_SCHEDULER_ENABLED"] = "0"
    os.environ["MALL_ADMIN_EMAIL"] = ADMIN_EMAIL
    os.environ["MALL_ADMIN_PASSWORD"] = ADMIN_PASS

    import mall_database  # noqa: F401
    import mall_security  # noqa: F401
    import app as mall_app  # noqa: F401
    return boot_dir


@pytest.fixture()
def isolated_mall_db(tmp_path, monkeypatch):
    import mall_database
    import mall_security

    db_file = tmp_path / "m8_unit.db"
    monkeypatch.setenv("MALL_ADMIN_EMAIL", ADMIN_EMAIL)
    monkeypatch.setenv("MALL_ADMIN_PASSWORD", ADMIN_PASS)
    monkeypatch.setenv("OBS_PULL_ENABLED", "0")
    monkeypatch.setenv("MALL_MODE", "cloud")
    monkeypatch.setenv("MALL_SCHEDULER_ENABLED", "0")
    mall_security.rate_limiter._hits.clear()
    mall_database.set_db_path(str(db_file), init=True)
    mall_database.ensure_default_mall_admin()
    return db_file


def _login_admin(client) -> str:
    res = client.post("/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS})
    assert res.status_code == 200, res.get_json()
    return res.get_json()["csrf_token"]


def _seed_product(num: int, name: str, category: str, price: int = 30000, stock: int = 5):
    import mall_database

    res = mall_database.create_mall_product(
        {
            "num": num,
            "name": name,
            "category": category,
            "price": price,
            "live_price": price,
            "consumer_price": price + 5000,
            "options": [{"color": "블랙", "size": "FREE", "stock": stock}],
        }
    )
    assert res["ok"], res
    return res["product"]


def test_legacy_db_migrates_parent_id_and_preserves_default_categories(tmp_path, monkeypatch):
    import mall_database

    legacy_db = tmp_path / "legacy_mall.db"
    conn = sqlite3.connect(str(legacy_db))
    conn.execute(
        """
        CREATE TABLE mall_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO mall_categories (code, name, sort_order, is_active, created_at)
        VALUES ('TOP', '상의/탑', 10, 1, '2026-01-01 00:00:00'),
               ('OUTER', '아우터', 20, 1, '2026-01-01 00:00:00')
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("MALL_ADMIN_EMAIL", ADMIN_EMAIL)
    monkeypatch.setenv("MALL_ADMIN_PASSWORD", ADMIN_PASS)
    mall_database.set_db_path(str(legacy_db), init=True)

    cols = set()
    c2 = sqlite3.connect(str(legacy_db))
    for r in c2.execute("PRAGMA table_info(mall_categories)").fetchall():
        cols.add(r[1])
    c2.close()
    assert "parent_id" in cols

    cats = mall_database.get_mall_categories(active_only=False)
    assert [c["code"] for c in cats] == ["TOP", "OUTER"]
    assert all(c["depth"] == 1 and c["parent_id"] is None for c in cats)


def test_create_3_levels_blocks_4th_and_allows_same_name_across_parents(isolated_mall_db):
    import mall_database

    r1 = mall_database.create_mall_category("의류", code="APPAREL", sort_order=60)
    assert r1["ok"], r1
    r2 = mall_database.create_mall_category("상의", code="APPAREL_TOP", parent_id=r1["id"], sort_order=10)
    assert r2["ok"], r2
    r3 = mall_database.create_mall_category("셔츠", code="APPAREL_SHIRT", parent_id=r2["id"], sort_order=10)
    assert r3["ok"], r3

    # 4단계 생성 차단
    r4 = mall_database.create_mall_category("반팔셔츠", code="APPAREL_SHORT", parent_id=r3["id"])
    assert not r4["ok"]
    assert "3단계" in r4["message"]

    # 같은 부모 내 중복 이름 차단
    dup_sibling = mall_database.create_mall_category("상의", parent_id=r1["id"])
    assert not dup_sibling["ok"]

    # 서로 다른 부모 아래에서는 동일 이름 허용 + 자동 코드 충돌 방지
    men = mall_database.create_mall_category("남성", code="MEN", sort_order=70)
    women = mall_database.create_mall_category("여성", code="WOMEN", sort_order=80)
    assert men["ok"] and women["ok"]
    m_top = mall_database.create_mall_category("니트", parent_id=men["id"])
    w_top = mall_database.create_mall_category("니트", parent_id=women["id"])
    assert m_top["ok"] and w_top["ok"]
    assert m_top["code"] != w_top["code"]

    labels = mall_database.get_mall_category_label_map()
    assert labels["APPAREL"] == "의류"
    assert labels["APPAREL_TOP"] == "의류 > 상의"
    assert labels["APPAREL_SHIRT"] == "의류 > 상의 > 셔츠"

    crumbs = mall_database.get_mall_category_breadcrumb("APPAREL_SHIRT")
    assert [(c["code"], c["name"], c["depth"]) for c in crumbs] == [
        ("APPAREL", "의류", 1),
        ("APPAREL_TOP", "상의", 2),
        ("APPAREL_SHIRT", "셔츠", 3),
    ]


def test_reparenting_guards_and_delete_blocking_and_active_cascade(isolated_mall_db):
    import app as mall_app
    import mall_database

    c1 = mall_database.create_mall_category("의류", code="WEAR", sort_order=60)
    c2 = mall_database.create_mall_category("상의", code="WEAR_TOP", parent_id=c1["id"], sort_order=10)
    c3 = mall_database.create_mall_category("셔츠", code="WEAR_SHIRT", parent_id=c2["id"], sort_order=10)
    other = mall_database.create_mall_category("잡화2", code="ACC2", sort_order=70)
    other_sub = mall_database.create_mall_category("모자", code="ACC2_CAP", parent_id=other["id"], sort_order=10)
    assert all(r["ok"] for r in (c1, c2, c3, other, other_sub))

    # 자기 자신 / 자손 순환 이동 차단
    self_move = mall_database.update_mall_category(c1["id"], parent_id=c1["id"])
    assert not self_move["ok"]
    cycle_move = mall_database.update_mall_category(c1["id"], parent_id=c3["id"])
    assert not cycle_move["ok"]

    # 하위 포함 3단계 초과 이동 차단 (c2->c3 높이 2를 depth 2인 other_sub 밑으로 이동 시 depth 4 됨)
    too_deep = mall_database.update_mall_category(c2["id"], parent_id=other_sub["id"])
    assert not too_deep["ok"]
    assert "3단계" in too_deep["message"]

    # 정상 이동: c2(높이2)를 1차 other(depth 1) 하위로 이동 -> depth 2, 3 유지됨
    ok_move = mall_database.update_mall_category(c2["id"], parent_id=other["id"])
    assert ok_move["ok"], ok_move
    assert mall_database.get_mall_category_label_map()["WEAR_SHIRT"] == "잡화2 > 상의 > 셔츠"

    # 다시 원래대로 복귀 후 비활성 상위 숨김 확인
    assert mall_database.update_mall_category(c2["id"], parent_id=c1["id"])["ok"]
    assert mall_database.update_mall_category(c1["id"], is_active=False)["ok"]
    active_codes = {c["code"] for c in mall_database.get_mall_categories(active_only=True)}
    assert "WEAR" not in active_codes
    assert "WEAR_TOP" not in active_codes
    assert "WEAR_SHIRT" not in active_codes
    assert mall_database.update_mall_category(c1["id"], is_active=True)["ok"]

    # 상품 연결 후 관리자 API로 삭제 차단 및 말단 삭제 테스트
    _seed_product(901, "옥스포드 셔츠", "WEAR_SHIRT")
    client = mall_app.app.test_client()
    csrf = _login_admin(client)

    # 잘못된 parent_id 는 400
    bad_parent_resp = client.post(
        "/api/admin/categories/save",
        json={"name": "오류", "parent_id": "invalid_id"},
        headers={"X-CSRF-Token": csrf},
    )
    assert bad_parent_resp.status_code == 400

    # 하위 있는 상위 삭제 차단 (400)
    del_parent = client.post(
        "/api/admin/categories/delete",
        json={"id": c2["id"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert del_parent.status_code == 400
    assert "하위 카테고리" in del_parent.get_json()["message"]

    # 말단 c3 삭제 -> 상품 ETC 이동
    del_leaf = client.post(
        "/api/admin/categories/delete",
        json={"id": c3["id"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert del_leaf.status_code == 200
    assert del_leaf.get_json()["moved_products"] == 1
    assert mall_database.get_mall_product_by_num(901)["category"] == "ETC"


def test_descendant_filtering_related_priority_and_home_badge_removed(isolated_mall_db):
    import app as mall_app
    import mall_database

    c1 = mall_database.create_mall_category("의류", code="CLOTH", sort_order=60)
    c2 = mall_database.create_mall_category("상의", code="CLOTH_TOP", parent_id=c1["id"], sort_order=10)
    c3 = mall_database.create_mall_category("셔츠", code="CLOTH_SHIRT", parent_id=c2["id"], sort_order=10)
    c3_b = mall_database.create_mall_category("티셔츠", code="CLOTH_TEE", parent_id=c2["id"], sort_order=20)
    assert all(r["ok"] for r in (c1, c2, c3, c3_b))

    p1 = _seed_product(101, "의류 직속 상품", "CLOTH")
    p2 = _seed_product(102, "상의 직속 상품", "CLOTH_TOP")
    p3 = _seed_product(103, "린넨 셔츠", "CLOTH_SHIRT")
    p4 = _seed_product(104, "코튼 티셔츠", "CLOTH_TEE")
    p5 = _seed_product(105, "가죽 벨트", "ACC")
    assert p1 and p2 and p3 and p4 and p5

    # 1차 조회: 101, 102, 103, 104 포함
    l1_nums = [p["num"] for p in mall_database.get_all_mall_products(category="CLOTH")]
    assert l1_nums == [101, 102, 103, 104]

    # 2차 조회: 102, 103, 104 포함
    l2_nums = [p["num"] for p in mall_database.get_all_mall_products(category="CLOTH_TOP")]
    assert l2_nums == [102, 103, 104]

    # 3차 조회: 103만 포함
    l3_nums = [p["num"] for p in mall_database.get_all_mall_products(category="CLOTH_SHIRT")]
    assert l3_nums == [103]

    # 연관 상품: 동일 대분류 계열(101, 102, 104)이 타 카테고리(105)보다 먼저 나옴
    rel_nums = [p["num"] for p in mall_database.get_related_mall_products(p3, limit=4)]
    assert rel_nums == [101, 102, 104, 105]

    client = mall_app.app.test_client()

    # /api/products & /api/catalog 하위 포함 확인
    api_prods = client.get("/api/products?category=CLOTH").get_json()
    assert [p["num"] for p in api_prods["products"]] == [101, 102, 103, 104]
    api_cat = client.get("/api/catalog?category=CLOTH_TOP").get_json()
    assert [p["num"] for p in api_cat["products"]] == [102, 103, 104]

    # 메인('/') HTML 검증: 등록 상품 배지 없음, 2·3단 드릴다운 바 및 전체 경로 표시
    home_all = client.get("/").get_data(as_text=True)
    assert "homeProductCountBadge" not in home_all
    assert "등록 상품 " not in home_all
    assert "의류 &gt; 상의 &gt; 셔츠" in home_all or "의류 > 상의 > 셔츠" in home_all

    home_l2 = client.get("/?category=CLOTH_TOP").get_data(as_text=True)
    assert 'id="homeCategoryBarL2"' in home_l2
    assert 'id="homeCategoryBarL3"' in home_l2
    assert "상의 전체" in home_l2
    assert "셔츠" in home_l2
    assert "린넨 셔츠" in home_l2
    assert "가죽 벨트" not in home_l2

    # 상세 페이지 브레드크럼 확인
    detail_html = client.get("/product/103").get_data(as_text=True)
    assert 'id="pdCategoryBreadcrumb"' in detail_html
    assert "의류 &gt; 상의 &gt; 셔츠" in detail_html or "의류 > 상의 > 셔츠" in detail_html
    assert '/?category=CLOTH_TOP' in detail_html
