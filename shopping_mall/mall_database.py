# -*- coding: utf-8 -*-
"""
독립 쇼핑몰 전용 SQLite 데이터베이스 관리 모듈 (mall_database.py)
- OBS 프로그램의 DB(live_commerce.db)와 완전히 분리된 별도의 DB(shopping_mall.db)로 운영됩니다.
- OBS에서 등록/수정된 상품(products, options, 재고, 이미지) 및 라이브 선택(Pin) 상품 상태를
  실시간 동기화 받아 저장하며, 쇼핑몰 자체 주문/결제/장바구니/리뷰 데이터를 관리합니다.
"""

import os
import re
import time
import json
import uuid
import sqlite3
import secrets as _secrets
from datetime import datetime
from threading import RLock, Condition
from typing import Any

import mall_security

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MALL_DB_PATH = os.getenv("MALL_DB_PATH", os.path.join(BASE_DIR, "shopping_mall.db"))
HEARTBEAT_TIMEOUT_SEC = float(os.getenv("MALL_HEARTBEAT_TIMEOUT_SEC", "35"))
mall_db_lock = RLock()
mall_state_cond = Condition()

DEFAULT_MALL_CATEGORIES = [
    ("TOP", "상의/탑", 10),
    ("OUTER", "아우터", 20),
    ("BOTTOM", "팬츠/스커트", 30),
    ("DRESS", "원피스/세트", 40),
    ("ACC", "잡화/악세서리", 50),
]

DEFAULT_MALL_POLICIES = {
    "shipping_info": "• 배송방법: 택배 발송 (오후 2시 이전 주문 당일 출고)\n• 배송비: 기본 4,000원 (도서/산간 지역 추가 비용 발생 가능)",
    "exchange_return_info": "• 상품 수령일로부터 7일 이내 교환/반품 신청이 가능합니다.\n• 단순 변심 시 왕복 배송비는 고객 부담이며, 오배송/상품 불량 시 판매자가 부담합니다.",
    "non_returnable_info": "• 착용, 세탁, 수선 또는 상품 택(Tag)/라벨이 훼손된 경우\n• 고객 부주의로 인한 오염(화장품/향수 등) 및 구성품 누락 시 교환/반품이 불가합니다.",
    "as_info": "• 수령 후 7일 이내 초기 불량 확인 시 신속히 교환/환불 처리해 드립니다.\n• A/S 및 기타 문의는 고객센터 또는 주문 내역을 통해 접수해 주세요.",
}

DEFAULT_STORE_INFO = {
    "brand_name": "골라방 LIVE",
    "company_name": "골라방 LIVE",
    "ceo_name": "박창현",
    "biz_reg_no": "000-00-00000",
    "telecom_reg_no": "제2026-서울강남-00000호",
    "biz_address": "서울특별시 (관리자 '스토어 설정'에서 사업장 주소를 입력해 주세요)",
    "privacy_officer": "박창현",
    "contact_email": "support@golabang.kr",
    "cs_phone": "010-0000-0000",
    "cs_hours": "평일 10:00 ~ 18:00 (점심 12:00 ~ 13:00 / 주말·공휴일 휴무)",
    "bank_name": "카카오뱅크",
    "bank_account": "3333 233 094904",
    "bank_holder": "박창현",
    "hosting_provider": "자체 운영 서버 (골라방 LIVE)",
    "escrow_notice": "고객님은 안전거래를 위해 현금 등으로 결제 시 쇼핑몰에서 안내하는 구매안전서비스(주문조회·교환/환불 보장)를 이용하실 수 있습니다.",
    "effective_date": "2026-01-01",
}

_STORE_INFO_MAX_LEN = {
    "brand_name": 80,
    "company_name": 100,
    "ceo_name": 60,
    "biz_reg_no": 40,
    "telecom_reg_no": 80,
    "biz_address": 200,
    "privacy_officer": 60,
    "contact_email": 120,
    "cs_phone": 60,
    "cs_hours": 140,
    "bank_name": 60,
    "bank_account": 80,
    "bank_holder": 60,
    "hosting_provider": 100,
    "escrow_notice": 400,
    "effective_date": 40,
}


def bump_state_version(bump_catalog=False):
    """실시간 상태 버전(state_version) 및 선택적 카탈로그 버전(catalog_version)을 단조 증가시키고 SSE 대기 스레드를 깨웁니다."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT value FROM mall_live_state WHERE key = 'state_version'")
            row = cur.fetchone()
            cur_v = int(json.loads(row["value"])) if row and str(row["value"]).strip().isdigit() else 0
            next_v = cur_v + 1
            cur.execute("""
                INSERT INTO mall_live_state (key, value, updated_at)
                VALUES ('state_version', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """, (json.dumps(next_v), now_str))

            if bump_catalog:
                cur.execute("SELECT value FROM mall_live_state WHERE key = 'catalog_version'")
                crow = cur.fetchone()
                cur_cv = int(json.loads(crow["value"])) if crow and str(crow["value"]).strip().isdigit() else 0
                next_cv = cur_cv + 1
                cur.execute("""
                    INSERT INTO mall_live_state (key, value, updated_at)
                    VALUES ('catalog_version', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """, (json.dumps(next_cv), now_str))
            conn.commit()
        finally:
            conn.close()
    with mall_state_cond:
        mall_state_cond.notify_all()
    return next_v


def get_connection():
    """쇼핑몰 전용 SQLite 데이터베이스 연결을 생성합니다."""
    conn = sqlite3.connect(MALL_DB_PATH, check_same_thread=False, timeout=20.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_mall_db():
    """쇼핑몰 전용 테이블 및 인덱스를 초기화합니다."""
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()

            # 1. 쇼핑몰 상품 테이블 (OBS 상품과 동기화 + 쇼핑몰 고유 메타데이터 지원)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mall_products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    obs_product_id INTEGER DEFAULT 0,
                    num INTEGER UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'ALL',
                    price INTEGER NOT NULL DEFAULT 0,
                    live_price INTEGER NOT NULL DEFAULT 0,
                    consumer_price INTEGER NOT NULL DEFAULT 0,
                    image_url TEXT DEFAULT '',
                    specs TEXT DEFAULT '',
                    detail_content TEXT DEFAULT '',
                    detail_images TEXT DEFAULT '[]',
                    shipping_info TEXT DEFAULT '',
                    exchange_return_info TEXT DEFAULT '',
                    non_returnable_info TEXT DEFAULT '',
                    as_info TEXT DEFAULT '',
                    is_featured INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)

            # 2. 쇼핑몰 상품 옵션 및 재고 테이블
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mall_product_options (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER NOT NULL,
                    color TEXT NOT NULL DEFAULT '-',
                    size TEXT NOT NULL DEFAULT '-',
                    stock INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (product_id) REFERENCES mall_products (id) ON DELETE CASCADE,
                    UNIQUE(product_id, color, size)
                )
            """)

            # 3. OBS 라이브 커머스 연동 상태 테이블 (선택된 상품 번호, 방송중 여부, 타이머, 안내 자막 등)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mall_live_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            # 4. 쇼핑몰 주문 원장 테이블 (OBS 채팅 주문과 분리된 쇼핑몰 웹 결제 주문)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mall_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_code TEXT UNIQUE NOT NULL,
                    buyer_name TEXT NOT NULL,
                    buyer_phone TEXT NOT NULL,
                    zipcode TEXT DEFAULT '',
                    address TEXT NOT NULL,
                    address_detail TEXT DEFAULT '',
                    delivery_memo TEXT DEFAULT '',
                    payment_method TEXT NOT NULL DEFAULT 'BANK',
                    payment_status TEXT NOT NULL DEFAULT '입금대기',
                    order_status TEXT NOT NULL DEFAULT '결제대기',
                    claim_reason TEXT DEFAULT '',
                    claim_updated_at TEXT DEFAULT '',
                    stock_restored INTEGER NOT NULL DEFAULT 0,
                    subtotal_amount INTEGER NOT NULL DEFAULT 0,
                    shipping_fee INTEGER NOT NULL DEFAULT 4000,
                    total_amount INTEGER NOT NULL DEFAULT 0,
                    source_page TEXT NOT NULL DEFAULT 'MALL',
                    created_at TEXT NOT NULL
                )
            """)

            # 5. 쇼핑몰 주문 상세 품목 테이블
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mall_order_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    product_num INTEGER NOT NULL,
                    product_name TEXT NOT NULL,
                    image_url TEXT DEFAULT '',
                    color TEXT NOT NULL DEFAULT '-',
                    size TEXT NOT NULL DEFAULT '-',
                    quantity INTEGER NOT NULL DEFAULT 1,
                    unit_price INTEGER NOT NULL DEFAULT 0,
                    line_total INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (order_id) REFERENCES mall_orders (id) ON DELETE CASCADE
                )
            """)

            # 6. 상품 구매후기/리뷰 테이블
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mall_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_num INTEGER NOT NULL,
                    user_id INTEGER DEFAULT NULL,
                    author TEXT NOT NULL,
                    rating INTEGER NOT NULL DEFAULT 5,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)

            # 7. 쇼핑몰 전용 회원 테이블 (도메인 1 OBS 방송자 DB와 완전 분리)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mall_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    zipcode TEXT DEFAULT '',
                    address TEXT DEFAULT '',
                    address_detail TEXT DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'MEMBER',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    failed_login_count INTEGER NOT NULL DEFAULT 0,
                    locked_until REAL NOT NULL DEFAULT 0,
                    last_login_at TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)

            # 기존 mall_products, mall_reviews 및 mall_orders 호환성 마이그레이션
            for tbl, col_name, col_def in [
                ("mall_products", "live_price", "INTEGER NOT NULL DEFAULT 0"),
                ("mall_products", "detail_content", "TEXT DEFAULT ''"),
                ("mall_products", "detail_images", "TEXT DEFAULT '[]'"),
                ("mall_products", "shipping_info", "TEXT DEFAULT ''"),
                ("mall_products", "exchange_return_info", "TEXT DEFAULT ''"),
                ("mall_products", "non_returnable_info", "TEXT DEFAULT ''"),
                ("mall_products", "as_info", "TEXT DEFAULT ''"),
                ("mall_reviews", "user_id", "INTEGER DEFAULT NULL"),
                ("mall_orders", "user_id", "INTEGER DEFAULT NULL"),
                ("mall_orders", "is_guest", "INTEGER NOT NULL DEFAULT 0"),
                ("mall_orders", "guest_password_hash", "TEXT DEFAULT ''"),
                ("mall_orders", "claim_reason", "TEXT DEFAULT ''"),
                ("mall_orders", "claim_updated_at", "TEXT DEFAULT ''"),
                ("mall_orders", "stock_restored", "INTEGER NOT NULL DEFAULT 0"),
            ]:
                try:
                    cur.execute(f"ALTER TABLE {tbl} ADD COLUMN {col_name} {col_def}")
                except Exception:
                    pass

            cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_products_num ON mall_products (num)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_orders_code ON mall_orders (order_code)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_orders_lookup ON mall_orders (buyer_name, buyer_phone)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_orders_user ON mall_orders (user_id, id DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_reviews_num ON mall_reviews (product_num, id DESC)")

            # 8. 쇼핑몰 동적 카테고리 테이블 (mall_categories)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mall_categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )
            """)
            cur.execute("SELECT COUNT(*) FROM mall_categories")
            if int(cur.fetchone()[0] or 0) == 0:
                seed_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for c_code, c_name, c_order in DEFAULT_MALL_CATEGORIES:
                    cur.execute("""
                        INSERT INTO mall_categories (code, name, sort_order, is_active, created_at)
                        VALUES (?, ?, ?, 1, ?)
                    """, (c_code, c_name, c_order, seed_ts))

            # 9. 상품 찜 목록 테이블 (mall_wishlist)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mall_wishlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    product_num INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, product_num)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_wishlist_user ON mall_wishlist (user_id, id DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_wishlist_prod ON mall_wishlist (product_num)")

            conn.commit()
        finally:
            conn.close()
    try:
        ensure_default_mall_admin()
    except Exception:
        pass


def _infer_category(name, specs=""):
    text = f"{name} {specs}".lower()
    if any(k in text for k in ["자켓", "코트", "점퍼", "패딩", "가디건", "아우터", "블레이저", "트렌치", "베스트", "조끼"]):
        return "OUTER"
    if any(k in text for k in ["팬츠", "바지", "데님", "청바지", "슬랙스", "스커트", "치마", "조거", "레깅스"]):
        return "BOTTOM"
    if any(k in text for k in ["원피스", "드레스", "투피스", "세트", "점프수트"]):
        return "DRESS"
    if any(k in text for k in ["가방", "백", "모자", "신발", "슈즈", "스니커즈", "벨트", "양말", "스카프", "목걸이", "귀걸이"]):
        return "ACC"
    return "TOP"


MALL_DETAIL_TEXT_FIELDS = (
    "detail_content",
    "shipping_info",
    "exchange_return_info",
    "non_returnable_info",
    "as_info",
)


def _parse_detail_images(raw):
    items = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        s = raw.strip()
        if s:
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    items = parsed
            except Exception:
                items = [line.strip() for line in s.splitlines() if line.strip()]
    cleaned = []
    for u in items:
        su = str(u or "").strip()
        if su and len(cleaned) < 30:
            cleaned.append(su)
    return cleaned


def _serialize_detail_images(raw):
    return json.dumps(_parse_detail_images(raw), ensure_ascii=False)


parse_detail_images = _parse_detail_images


def _format_product_row(prow, opt_rows):
    keys = set(prow.keys())
    options = []
    total_stock = 0
    colors = []
    sizes = []
    for opt in opt_rows:
        stk = max(0, int(opt["stock"] or 0))
        c = str(opt["color"] or "-").strip()
        s = str(opt["size"] or "-").strip()
        total_stock += stk
        options.append({
            "color": c,
            "size": s,
            "stock": stk
        })
        if c and c not in colors:
            colors.append(c)
        if s and s not in sizes:
            sizes.append(s)

    sell_p = int(prow["price"] or 0)
    live_p = int(prow["live_price"] or 0) if "live_price" in keys and prow["live_price"] is not None else 0
    if live_p <= 0:
        live_p = sell_p
    cons_p = int(prow["consumer_price"] or sell_p)
    if cons_p < sell_p:
        cons_p = sell_p
    discount_rate = round((cons_p - sell_p) / cons_p * 100) if cons_p > sell_p and cons_p > 0 else 0
    live_discount_rate = round((cons_p - live_p) / cons_p * 100) if cons_p > live_p and cons_p > 0 else discount_rate

    img_url = str(prow["image_url"] or "").strip()
    detail_images = _parse_detail_images(prow["detail_images"] if "detail_images" in keys else [])

    return {
        "id": prow["id"],
        "obs_product_id": prow["obs_product_id"],
        "num": prow["num"],
        "name": prow["name"],
        "category": prow["category"] or "TOP",
        "price": sell_p,
        "live_price": live_p,
        "consumer_price": cons_p,
        "discount_rate": discount_rate,
        "live_discount_rate": live_discount_rate,
        "image_url": img_url,
        "specs": prow["specs"] or "",
        "detail_content": str(prow["detail_content"] or "") if "detail_content" in keys and prow["detail_content"] is not None else "",
        "detail_images": detail_images,
        "shipping_info": str(prow["shipping_info"] or "") if "shipping_info" in keys and prow["shipping_info"] is not None else "",
        "exchange_return_info": str(prow["exchange_return_info"] or "") if "exchange_return_info" in keys and prow["exchange_return_info"] is not None else "",
        "non_returnable_info": str(prow["non_returnable_info"] or "") if "non_returnable_info" in keys and prow["non_returnable_info"] is not None else "",
        "as_info": str(prow["as_info"] or "") if "as_info" in keys and prow["as_info"] is not None else "",
        "is_featured": bool(prow["is_featured"]),
        "total_stock": total_stock,
        "is_sold_out": total_stock <= 0,
        "options": options,
        "colors": colors,
        "sizes": sizes,
        "updated_at": prow["updated_at"],
        "created_at": prow["created_at"]
    }


def upsert_product_from_obs(prod_data):
    """OBS에서 전달받은 단일 상품 데이터를 쇼핑몰 DB(mall_products, mall_product_options)에 저장/갱신합니다."""
    if not isinstance(prod_data, dict):
        return None
    num = prod_data.get("num")
    if num is None:
        return None
    num = int(num)
    obs_id = int(str(prod_data.get("db_id") or prod_data.get("id") or 0).replace("item-", "") or 0)
    name = str(prod_data.get("name") or f"상품 #{num}").strip()
    price = max(0, int(prod_data.get("price") or 0))
    live_price = max(0, int(prod_data.get("live_price") or price))
    if live_price <= 0:
        live_price = price
    consumer_price = max(price, int(prod_data.get("consumer_price") or price))
    image_url = str(prod_data.get("image_url") or "").strip()
    specs = str(prod_data.get("specs") or "").strip()
    incoming_cat = str(prod_data.get("category") or "").strip().upper()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    created_at = str(prod_data.get("created_at") or now_str)

    for attempt in range(2):
        need_retry = False
        with mall_db_lock:
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT id, category, detail_content, detail_images, shipping_info, exchange_return_info, non_returnable_info, as_info
                    FROM mall_products WHERE num = ?
                """, (num,))
                existing = cur.fetchone()
                if existing:
                    mall_pid = existing["id"]
                    ext_keys = set(existing.keys())
                    existing_cat = str(existing["category"] or "").strip().upper() if "category" in ext_keys else ""
                    category = incoming_cat or existing_cat or _infer_category(name, specs)
                    resolved_text = {
                        col: (
                            str(prod_data[col] or "").strip()
                            if col in prod_data
                            else str(existing[col] or "" if col in ext_keys else "")
                        )
                        for col in MALL_DETAIL_TEXT_FIELDS
                    }
                    resolved_detail_imgs = (
                        _serialize_detail_images(prod_data["detail_images"])
                        if "detail_images" in prod_data
                        else (existing["detail_images"] if "detail_images" in ext_keys and existing["detail_images"] else "[]")
                    )
                    cur.execute("""
                        UPDATE mall_products
                        SET obs_product_id = ?, name = ?, category = ?, price = ?, live_price = ?, consumer_price = ?,
                            image_url = ?, specs = ?,
                            detail_content = ?, detail_images = ?,
                            shipping_info = ?, exchange_return_info = ?, non_returnable_info = ?, as_info = ?,
                            updated_at = ?
                        WHERE id = ?
                    """, (
                        obs_id, name, category, price, live_price, consumer_price,
                        image_url, specs,
                        resolved_text["detail_content"], resolved_detail_imgs,
                        resolved_text["shipping_info"], resolved_text["exchange_return_info"],
                        resolved_text["non_returnable_info"], resolved_text["as_info"],
                        now_str, mall_pid
                    ))
                else:
                    category = incoming_cat or _infer_category(name, specs)
                    cur.execute("""
                        INSERT INTO mall_products
                        (obs_product_id, num, name, category, price, live_price, consumer_price, image_url, specs,
                         detail_content, detail_images, shipping_info, exchange_return_info, non_returnable_info, as_info,
                         is_featured, updated_at, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """, (
                        obs_id, num, name, category, price, live_price, consumer_price, image_url, specs,
                        str(prod_data.get("detail_content") or "").strip(),
                        _serialize_detail_images(prod_data.get("detail_images")),
                        str(prod_data.get("shipping_info") or "").strip(),
                        str(prod_data.get("exchange_return_info") or "").strip(),
                        str(prod_data.get("non_returnable_info") or "").strip(),
                        str(prod_data.get("as_info") or "").strip(),
                        now_str, created_at
                    ))
                    mall_pid = cur.lastrowid

                if "options" in prod_data and isinstance(prod_data["options"], list):
                    cur.execute("DELETE FROM mall_product_options WHERE product_id = ?", (mall_pid,))
                    options = prod_data["options"]
                    if not options:
                        options = [{"color": "기본", "size": "FREE", "stock": 10}]
                    for opt in options:
                        c = str(opt.get("color") or "-").strip() or "-"
                        s = str(opt.get("size") or "-").strip() or "-"
                        stk = max(0, int(opt.get("stock") or 0))
                        cur.execute("""
                            INSERT OR REPLACE INTO mall_product_options (product_id, color, size, stock)
                            VALUES (?, ?, ?, ?)
                        """, (mall_pid, c, s, stk))

                conn.commit()
                bump_state_version(bump_catalog=True)
                return get_mall_product_by_num(num)
            except sqlite3.OperationalError as err:
                if attempt == 0 and "no such column" in str(err).lower():
                    need_retry = True
                else:
                    raise
            finally:
                conn.close()
        if need_retry:
            init_mall_db()
            continue
        break
    return None


def bulk_sync_products_from_obs(products_list, delete_missing=True):
    """OBS의 전체 상품 목록을 쇼핑몰 DB에 일괄 동기화합니다."""
    if not isinstance(products_list, list):
        return 0
    sorted_list = sorted(
        products_list,
        key=lambda x: int(x.get("num") or x.get("db_id") or 999999) if isinstance(x, dict) else 999999
    )
    synced_nums = []
    for p in sorted_list:
        res = upsert_product_from_obs(p)
        if res and res.get("num") is not None:
            synced_nums.append(int(res["num"]))

    if delete_missing:
        with mall_db_lock:
            conn = get_connection()
            try:
                cur = conn.cursor()
                if synced_nums:
                    placeholders = ",".join("?" for _ in synced_nums)
                    cur.execute(f"DELETE FROM mall_products WHERE num NOT IN ({placeholders})", synced_nums)
                else:
                    cur.execute("DELETE FROM mall_products")
                conn.commit()
            finally:
                conn.close()
    return len(synced_nums)


def delete_product_by_num_or_obs_id(num=None, obs_product_id=None):
    """OBS에서 삭제된 상품을 쇼핑몰 DB에서도 삭제합니다."""
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            if num is not None:
                cur.execute("DELETE FROM mall_products WHERE num = ?", (int(num),))
            elif obs_product_id is not None:
                real_id = int(str(obs_product_id).replace("item-", ""))
                cur.execute("DELETE FROM mall_products WHERE obs_product_id = ?", (real_id,))
            conn.commit()
            deleted = cur.rowcount > 0
            if deleted:
                bump_state_version(bump_catalog=True)
            return deleted
        finally:
            conn.close()


# ==============================================================================
# 🗂️ 동적 카테고리 관리 & 쇼핑몰 관리자 직접 상품/안내 CRUD
# ==============================================================================

def get_mall_categories(active_only=False):
    """등록된 쇼핑몰 카테고리 목록과 각 카테고리별 소속 상품 수를 반환합니다."""
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            where_sql = "WHERE c.is_active = 1" if active_only else ""
            cur.execute(f"""
                SELECT c.id, c.code, c.name, c.sort_order, c.is_active, c.created_at,
                       COUNT(p.id) AS product_count
                FROM mall_categories c
                LEFT JOIN mall_products p ON UPPER(COALESCE(p.category, '')) = UPPER(c.code)
                {where_sql}
                GROUP BY c.id
                ORDER BY c.sort_order ASC, c.id ASC
            """)
            return [
                {
                    "id": int(r["id"]),
                    "code": str(r["code"]),
                    "name": str(r["name"]),
                    "sort_order": int(r["sort_order"] or 0),
                    "is_active": bool(r["is_active"]),
                    "product_count": int(r["product_count"] or 0),
                    "created_at": str(r["created_at"] or ""),
                }
                for r in cur.fetchall()
            ]
        finally:
            conn.close()


def get_mall_category_label_map():
    """카테고리 코드 -> 한글 표시명 매핑 딕셔너리를 반환합니다."""
    cats = get_mall_categories(active_only=False)
    labels = {str(c["code"]).upper(): str(c["name"]) for c in cats}
    labels.setdefault("ETC", "기타")
    return labels


def create_mall_category(name: str, code: str = "", sort_order=None, is_active: bool = True) -> dict:
    """새 쇼핑몰 카테고리를 등록합니다."""
    nm = str(name or "").strip()
    if not nm or len(nm) > 40:
        return {"ok": False, "message": "카테고리명은 1~40자 이내로 입력해 주세요."}

    raw_code = str(code or "").strip().upper()
    if raw_code:
        sanitized = re.sub(r"[^A-Z0-9_]", "_", raw_code).strip("_")[:24]
        if not sanitized:
            return {"ok": False, "message": "카테고리 코드는 영문 대문자, 숫자, 밑줄(_)만 사용할 수 있습니다."}
    else:
        ascii_slug = re.sub(r"[^A-Z0-9]+", "_", nm.upper()).strip("_")[:16]
        sanitized = ascii_slug or f"CAT_{_secrets.token_hex(3).upper()}"

    if sanitized in ("ALL", "ETC"):
        return {"ok": False, "message": "ALL 및 ETC는 시스템 예약 코드입니다. 다른 코드를 사용해 주세요."}

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM mall_categories WHERE UPPER(code) = ? OR name = ?", (sanitized, nm))
            if cur.fetchone():
                return {"ok": False, "message": "이미 동일한 이름 또는 코드의 카테고리가 존재합니다."}

            if sort_order is None or str(sort_order).strip() == "":
                cur.execute("SELECT COALESCE(MAX(sort_order), 0) + 10 FROM mall_categories")
                order_val = int(cur.fetchone()[0] or 10)
            else:
                order_val = max(0, int(sort_order))

            cur.execute("""
                INSERT INTO mall_categories (code, name, sort_order, is_active, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (sanitized, nm, order_val, 1 if is_active else 0, now_str))
            new_id = int(cur.lastrowid or 0)
            conn.commit()
            bump_state_version(bump_catalog=True)
            return {"ok": True, "id": new_id, "code": sanitized}
        finally:
            conn.close()


def update_mall_category(cat_id: int, name=None, sort_order=None, is_active=None) -> dict:
    """기존 쇼핑몰 카테고리의 이름, 정렬 순서, 스토어 노출 여부를 수정합니다."""
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM mall_categories WHERE id = ?", (int(cat_id),))
            existing = cur.fetchone()
            if not existing:
                return {"ok": False, "message": "수정할 카테고리를 찾을 수 없습니다."}

            new_name = str(name).strip() if name is not None else str(existing["name"])
            if not new_name or len(new_name) > 40:
                return {"ok": False, "message": "카테고리명은 1~40자 이내로 입력해 주세요."}

            cur.execute("SELECT id FROM mall_categories WHERE name = ? AND id != ?", (new_name, int(cat_id)))
            if cur.fetchone():
                return {"ok": False, "message": "이미 동일한 이름의 카테고리가 존재합니다."}

            new_order = int(existing["sort_order"] or 0) if sort_order is None or str(sort_order).strip() == "" else max(0, int(sort_order))
            new_active = int(existing["is_active"]) if is_active is None else (1 if is_active else 0)

            cur.execute("""
                UPDATE mall_categories
                SET name = ?, sort_order = ?, is_active = ?
                WHERE id = ?
            """, (new_name, new_order, new_active, int(cat_id)))
            conn.commit()
            bump_state_version(bump_catalog=True)
            return {"ok": True}
        finally:
            conn.close()


def delete_mall_category(cat_id: int) -> dict:
    """카테고리를 삭제하고 해당 카테고리에 속해 있던 상품은 기타(ETC)로 이동합니다."""
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, code, name FROM mall_categories WHERE id = ?", (int(cat_id),))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "message": "삭제할 카테고리를 찾을 수 없습니다."}

            old_code = str(row["code"] or "").strip().upper()
            cur.execute("UPDATE mall_products SET category = 'ETC' WHERE UPPER(COALESCE(category, '')) = ?", (old_code,))
            moved_cnt = int(cur.rowcount or 0)
            cur.execute("DELETE FROM mall_categories WHERE id = ?", (int(cat_id),))
            conn.commit()
            bump_state_version(bump_catalog=True)
            return {
                "ok": True,
                "deleted_code": old_code,
                "deleted_name": str(row["name"]),
                "moved_products": moved_cnt,
            }
        finally:
            conn.close()


def get_mall_policy_defaults() -> dict:
    """쇼핑몰 상품 등록 시 공통으로 불러올 4대 기본 안내를 반환합니다."""
    saved = get_live_state_key("product_policy_defaults", None)
    if isinstance(saved, dict):
        return {
            "shipping_info": str(saved.get("shipping_info") or "").strip(),
            "exchange_return_info": str(saved.get("exchange_return_info") or "").strip(),
            "non_returnable_info": str(saved.get("non_returnable_info") or "").strip(),
            "as_info": str(saved.get("as_info") or "").strip(),
        }
    return dict(DEFAULT_MALL_POLICIES)


def set_mall_policy_defaults(policy_data: Any) -> dict:
    """쇼핑몰 공통 4대 기본 안내를 DB에 영구 저장합니다."""
    raw = policy_data if isinstance(policy_data, dict) else {}
    normalized = {
        "shipping_info": str(raw.get("shipping_info") or "").strip(),
        "exchange_return_info": str(raw.get("exchange_return_info") or "").strip(),
        "non_returnable_info": str(raw.get("non_returnable_info") or "").strip(),
        "as_info": str(raw.get("as_info") or "").strip(),
    }
    set_live_state_key("product_policy_defaults", normalized)
    return normalized


def _normalize_store_info(raw: Any) -> dict:
    src = raw if isinstance(raw, dict) else {}
    info = {}
    for key, default_val in DEFAULT_STORE_INFO.items():
        max_len = _STORE_INFO_MAX_LEN.get(key, 200)
        val = str(src.get(key) or "").strip()
        if not val:
            val = default_val
        info[key] = val[:max_len]
    digits = re.sub(r"\D", "", info["biz_reg_no"])
    info["biz_reg_digits"] = digits
    if len(digits) == 10 and digits != "0000000000":
        info["ftc_biz_url"] = f"https://www.ftc.go.kr/bizCommPop.do?wrkr_no={digits}"
    else:
        info["ftc_biz_url"] = ""
    return info


def get_mall_store_info() -> dict:
    """쇼핑몰 하단 푸터와 법정 약관에 표시되는 사업자·고객센터·입금계좌 정보를 반환합니다."""
    saved = get_live_state_key("store_info", None)
    return _normalize_store_info(saved)


def set_mall_store_info(info_data: Any) -> dict:
    """쇼핑몰 사업자·고객센터·입금계좌 정보를 DB에 영구 저장합니다."""
    raw = info_data if isinstance(info_data, dict) else {}
    current = get_mall_store_info()
    merged = dict(current)
    for key in DEFAULT_STORE_INFO:
        if key in raw:
            merged[key] = raw.get(key)
    normalized = _normalize_store_info(merged)
    persist_payload = {k: normalized[k] for k in DEFAULT_STORE_INFO}
    set_live_state_key("store_info", persist_payload)
    return normalized


def create_mall_product(prod_data: Any) -> dict:
    """쇼핑몰 관리자에서 신규 상품을 직접 등록합니다."""
    if not isinstance(prod_data, dict):
        return {"ok": False, "message": "잘못된 상품 데이터입니다."}

    name = str(prod_data.get("name") or "").strip()
    if not name:
        return {"ok": False, "message": "상품명을 입력해 주세요."}

    try:
        sell_price = max(0, int(prod_data.get("price") or 0))
        live_price = max(0, int(prod_data.get("live_price") or sell_price))
        if live_price <= 0:
            live_price = sell_price
        consumer_price = max(sell_price, int(prod_data.get("consumer_price") or sell_price))
    except Exception:
        return {"ok": False, "message": "가격은 올바른 숫자로 입력해 주세요."}

    raw_opts = prod_data.get("options")
    clean_opts = []
    if isinstance(raw_opts, list):
        for o in raw_opts:
            if not isinstance(o, dict):
                continue
            c = str(o.get("color") or "-").strip() or "-"
            s = str(o.get("size") or "-").strip() or "-"
            stk = max(0, int(o.get("stock") or 0))
            clean_opts.append({"color": c, "size": s, "stock": stk})
    if not clean_opts:
        clean_opts = [{"color": "기본", "size": "FREE", "stock": 10}]

    image_url = str(prod_data.get("image_url") or "").strip()
    specs = str(prod_data.get("specs") or "").strip()
    category = str(prod_data.get("category") or "").strip().upper() or _infer_category(name, specs)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            raw_num = prod_data.get("num")
            if raw_num in (None, ""):
                cur.execute("SELECT COALESCE(MAX(num), 0) + 1 FROM mall_products")
                num = int(cur.fetchone()[0] or 1)
            else:
                num = int(raw_num)
                if num <= 0:
                    return {"ok": False, "message": "상품 번호는 1 이상의 숫자여야 합니다."}
                cur.execute("SELECT id FROM mall_products WHERE num = ?", (num,))
                if cur.fetchone():
                    return {"ok": False, "message": f"이미 #{num}번 상품이 등록되어 있습니다."}

            cur.execute("""
                INSERT INTO mall_products
                (obs_product_id, num, name, category, price, live_price, consumer_price, image_url, specs,
                 detail_content, detail_images, shipping_info, exchange_return_info, non_returnable_info, as_info,
                 is_featured, updated_at, created_at)
                VALUES (0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """, (
                num, name, category, sell_price, live_price, consumer_price, image_url, specs,
                str(prod_data.get("detail_content") or "").strip(),
                _serialize_detail_images(prod_data.get("detail_images")),
                str(prod_data.get("shipping_info") or "").strip(),
                str(prod_data.get("exchange_return_info") or "").strip(),
                str(prod_data.get("non_returnable_info") or "").strip(),
                str(prod_data.get("as_info") or "").strip(),
                now_str, now_str
            ))
            mall_pid = int(cur.lastrowid or 0)
            for opt in clean_opts:
                cur.execute("""
                    INSERT OR REPLACE INTO mall_product_options (product_id, color, size, stock)
                    VALUES (?, ?, ?, ?)
                """, (mall_pid, opt["color"], opt["size"], opt["stock"]))

            conn.commit()
            bump_state_version(bump_catalog=True)
            return {"ok": True, "product": get_mall_product_by_num(num)}
        finally:
            conn.close()


def update_mall_product(product_id: int, prod_data: Any) -> dict:
    """쇼핑몰 관리자에서 기존 상품 정보, 카테고리, 상세페이지, 4대 안내 및 옵션을 수정합니다."""
    if not isinstance(prod_data, dict):
        return {"ok": False, "message": "잘못된 상품 데이터입니다."}

    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM mall_products WHERE id = ?", (int(product_id),))
            existing = cur.fetchone()
            if not existing:
                return {"ok": False, "message": "수정할 상품을 찾을 수 없습니다."}

            raw_num = prod_data.get("num")
            target_num = int(raw_num) if raw_num not in (None, "") else int(existing["num"])
            if target_num <= 0:
                return {"ok": False, "message": "상품 번호는 1 이상의 숫자여야 합니다."}
            if target_num != int(existing["num"]):
                cur.execute("SELECT id FROM mall_products WHERE num = ? AND id != ?", (target_num, int(product_id)))
                if cur.fetchone():
                    return {"ok": False, "message": f"이미 #{target_num}번 상품이 등록되어 있습니다."}

            name = str(prod_data.get("name") if "name" in prod_data else existing["name"]).strip()
            if not name:
                return {"ok": False, "message": "상품명을 입력해 주세요."}

            sell_price = max(0, int(prod_data["price"])) if "price" in prod_data else int(existing["price"] or 0)
            live_price = max(0, int(prod_data.get("live_price") or sell_price)) if "live_price" in prod_data else int(existing["live_price"] or sell_price)
            if live_price <= 0:
                live_price = sell_price
            consumer_price = max(sell_price, int(prod_data.get("consumer_price") or sell_price)) if "consumer_price" in prod_data else max(sell_price, int(existing["consumer_price"] or sell_price))
            category = str(prod_data.get("category") if "category" in prod_data else existing["category"]).strip().upper() or "ETC"
            image_url = str(prod_data.get("image_url") if "image_url" in prod_data else existing["image_url"] or "").strip()
            specs = str(prod_data.get("specs") if "specs" in prod_data else existing["specs"] or "").strip()
            detail_imgs = _serialize_detail_images(prod_data["detail_images"]) if "detail_images" in prod_data else (existing["detail_images"] or "[]")
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            ext_keys = set(existing.keys())
            resolved_texts = {
                col: (
                    str(prod_data[col] or "").strip()
                    if col in prod_data
                    else str(existing[col] or "" if col in ext_keys else "")
                )
                for col in MALL_DETAIL_TEXT_FIELDS
            }

            cur.execute("""
                UPDATE mall_products
                SET num = ?, name = ?, category = ?, price = ?, live_price = ?, consumer_price = ?,
                    image_url = ?, specs = ?,
                    detail_content = ?, detail_images = ?,
                    shipping_info = ?, exchange_return_info = ?, non_returnable_info = ?, as_info = ?,
                    updated_at = ?
                WHERE id = ?
            """, (
                target_num, name, category, sell_price, live_price, consumer_price,
                image_url, specs,
                resolved_texts["detail_content"], detail_imgs,
                resolved_texts["shipping_info"], resolved_texts["exchange_return_info"],
                resolved_texts["non_returnable_info"], resolved_texts["as_info"],
                now_str, int(product_id)
            ))

            if "options" in prod_data and isinstance(prod_data["options"], list):
                clean_opts = []
                for o in prod_data["options"]:
                    if not isinstance(o, dict):
                        continue
                    c = str(o.get("color") or "-").strip() or "-"
                    s = str(o.get("size") or "-").strip() or "-"
                    stk = max(0, int(o.get("stock") or 0))
                    clean_opts.append({"color": c, "size": s, "stock": stk})
                if not clean_opts:
                    return {"ok": False, "message": "최소 1개 이상의 옵션을 입력해 주세요."}
                cur.execute("DELETE FROM mall_product_options WHERE product_id = ?", (int(product_id),))
                for opt in clean_opts:
                    cur.execute("""
                        INSERT OR REPLACE INTO mall_product_options (product_id, color, size, stock)
                        VALUES (?, ?, ?, ?)
                    """, (int(product_id), opt["color"], opt["size"], opt["stock"]))

            conn.commit()
            bump_state_version(bump_catalog=True)
            return {"ok": True, "product": get_mall_product_by_num(target_num)}
        finally:
            conn.close()


def delete_mall_product(product_id: int) -> dict:
    """쇼핑몰 관리자에서 상품을 삭제합니다."""
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, num, obs_product_id, name FROM mall_products WHERE id = ?", (int(product_id),))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "message": "삭제할 상품을 찾을 수 없습니다."}
            cur.execute("DELETE FROM mall_products WHERE id = ?", (int(product_id),))
            conn.commit()
            bump_state_version(bump_catalog=True)
            return {
                "ok": True,
                "deleted_num": int(row["num"]),
                "deleted_obs_id": int(row["obs_product_id"] or 0),
                "deleted_name": str(row["name"]),
            }
        finally:
            conn.close()


def set_mall_product_obs_id(mall_pid: int, obs_id: int):
    """쇼핑몰 상품의 obs_product_id를 연결해 OBS 양방향 동기화 일관성을 유지합니다."""
    with mall_db_lock:
        conn = get_connection()
        try:
            conn.execute("UPDATE mall_products SET obs_product_id = ? WHERE id = ?", (int(obs_id), int(mall_pid)))
            conn.commit()
        finally:
            conn.close()


def get_all_mall_products(category=None, search=None, sort="num_asc"):
    """쇼핑몰 상품 목록을 조건별로 조회합니다."""
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            where_clauses = []
            params = []

            if category and str(category).upper() not in ("ALL", ""):
                where_clauses.append("category = ?")
                params.append(str(category).upper())

            if search and str(search).strip():
                q = f"%{str(search).strip()}%"
                where_clauses.append("(name LIKE ? OR specs LIKE ? OR CAST(num AS TEXT) LIKE ?)")
                params.extend([q, q, q])

            where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

            if sort == "price_asc":
                order_sql = "ORDER BY price ASC, num ASC"
            elif sort == "price_desc":
                order_sql = "ORDER BY price DESC, num ASC"
            elif sort == "newest":
                order_sql = "ORDER BY id DESC"
            else:
                order_sql = "ORDER BY num ASC, id DESC"

            cur.execute(f"SELECT * FROM mall_products {where_sql} {order_sql}", params)
            rows = cur.fetchall()
            result = []
            for r in rows:
                cur.execute(
                    "SELECT color, size, stock FROM mall_product_options WHERE product_id = ? ORDER BY id ASC",
                    (r["id"],)
                )
                opt_rows = cur.fetchall()
                result.append(_format_product_row(r, opt_rows))
            return result
        finally:
            conn.close()


def get_mall_product_by_num(num):
    """상품 번호(num)로 쇼핑몰 상품 상세 정보를 조회합니다."""
    if num is None:
        return None
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM mall_products WHERE num = ?", (int(num),))
            r = cur.fetchone()
            if not r:
                return None
            cur.execute(
                "SELECT color, size, stock FROM mall_product_options WHERE product_id = ? ORDER BY id ASC",
                (r["id"],)
            )
            opt_rows = cur.fetchall()
            return _format_product_row(r, opt_rows)
        finally:
            conn.close()


def get_mall_product_by_id(product_id):
    """상품 번호(num) 최우선, 이후 OBS ID 또는 쇼핑몰 DB ID로 상품 상세 정보를 조회합니다."""
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            real_id = int(str(product_id).replace("item-", ""))
            cur.execute("SELECT * FROM mall_products WHERE num = ?", (real_id,))
            r = cur.fetchone()
            if not r:
                cur.execute("SELECT * FROM mall_products WHERE obs_product_id = ? OR id = ?", (real_id, real_id))
                r = cur.fetchone()
            if not r:
                return None
            cur.execute(
                "SELECT color, size, stock FROM mall_product_options WHERE product_id = ? ORDER BY id ASC",
                (r["id"],)
            )
            opt_rows = cur.fetchall()
            return _format_product_row(r, opt_rows)
        finally:
            conn.close()


def set_live_state_key(key, val):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    val_str = json.dumps(val, ensure_ascii=False)
    with mall_db_lock:
        conn = get_connection()
        try:
            conn.execute("""
                INSERT INTO mall_live_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """, (str(key), val_str, now_str))
            conn.commit()
        finally:
            conn.close()


def get_live_state_key(key, default=None):
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT value FROM mall_live_state WHERE key = ?", (str(key),))
            row = cur.fetchone()
            if not row:
                return default
            try:
                return json.loads(row["value"])
            except Exception:
                return row["value"]
        finally:
            conn.close()


def update_live_state_bundle(state_dict):
    """OBS에서 전달된 실시간 라이브 상태(선택된 상품 번호, 방송중 여부, 자막, 타이머, 하트비트 등)를 일괄 갱신합니다."""
    if not isinstance(state_dict, dict):
        return
    incoming = dict(state_dict)
    is_hb = bool(incoming.pop("heartbeat", False))

    if "last_heartbeat_ts" not in incoming:
        if "is_live" in incoming and not incoming["is_live"]:
            incoming["last_heartbeat_ts"] = 0.0
        elif is_hb or incoming.get("is_live") is True:
            incoming["last_heartbeat_ts"] = time.time()

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    meaningful_changed = False
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM mall_live_state")
            existing = {r["key"]: r["value"] for r in cur.fetchall()}

            for k, v in incoming.items():
                val_str = json.dumps(v, ensure_ascii=False)
                if k != "last_heartbeat_ts" and existing.get(str(k)) != val_str:
                    meaningful_changed = True
                cur.execute("""
                    INSERT INTO mall_live_state (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """, (str(k), val_str, now_str))
            conn.commit()
        finally:
            conn.close()

    if meaningful_changed:
        bump_state_version(bump_catalog=False)


def _derive_youtube_embed_url(raw):
    """저장된 youtube_embed_url, live_urls.YOUTUBE, 또는 youtube_video_id에서 YouTube iframe 임베드 URL을 도출합니다."""
    explicit = str(raw.get("youtube_embed_url") or "").strip()
    if explicit:
        return explicit
    live_urls = raw.get("live_urls") or {}
    yt_info = live_urls.get("YOUTUBE") if isinstance(live_urls, dict) else {}
    if isinstance(yt_info, dict):
        emb = str(yt_info.get("embed_url") or "").strip()
        if emb:
            return emb
        lurl = str(yt_info.get("live_url") or "").strip()
        if "watch?v=" in lurl:
            vid = lurl.split("watch?v=", 1)[1].split("&")[0].strip()
            if vid:
                return f"https://www.youtube.com/embed/{vid}?autoplay=1&mute=1&playsinline=1&rel=0"
    vid = str(raw.get("youtube_video_id") or "").strip()
    if vid:
        return f"https://www.youtube.com/embed/{vid}?autoplay=1&mute=1&playsinline=1&rel=0"
    return ""


def _read_raw_live_state_and_check_heartbeat():
    """DB에서 mall_live_state 전체 키를 읽고 하트비트 만료(방송 PC 비정상 종료 등) 시 자동으로 is_live=False 처리합니다."""
    expired = False
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT key, value, updated_at FROM mall_live_state")
            rows = cur.fetchall()
            raw = {}
            latest_upd = ""
            for r in rows:
                try:
                    raw[r["key"]] = json.loads(r["value"])
                except Exception:
                    raw[r["key"]] = r["value"]
                if r["updated_at"] > latest_upd:
                    latest_upd = r["updated_at"]

            is_live = bool(raw.get("is_live", False))
            hb_ts = float(raw.get("last_heartbeat_ts") or 0.0)
            if is_live and hb_ts > 0.0 and (time.time() - hb_ts) > HEARTBEAT_TIMEOUT_SEC:
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for k, v in (("is_live", False), ("last_heartbeat_ts", 0.0)):
                    cur.execute("""
                        INSERT INTO mall_live_state (key, value, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """, (k, json.dumps(v), now_str))
                conn.commit()
                raw["is_live"] = False
                raw["last_heartbeat_ts"] = 0.0
                latest_upd = now_str
                expired = True
        finally:
            conn.close()

    if expired:
        new_v = bump_state_version(bump_catalog=False)
        raw["state_version"] = new_v

    return raw, latest_upd


def get_full_live_state():
    """쇼핑몰 초기 렌더링 및 전체 상태 조회용 데이터(전체 상품 포함)를 반환합니다."""
    raw, latest_upd = _read_raw_live_state_and_check_heartbeat()

    all_products = get_all_mall_products()
    pinned_num = raw.get("pinned_product_num")
    selected_product = None

    if pinned_num is not None and str(pinned_num).strip() != "":
        p_str = str(pinned_num).replace("item-", "").strip()
        for p in all_products:
            if str(p.get("num")).strip() == p_str:
                selected_product = p
                break
        if selected_product is None:
            for p in all_products:
                if p.get("obs_product_id") is not None and str(p.get("obs_product_id")).strip() == p_str:
                    selected_product = p
                    break
        if selected_product is not None:
            pinned_num = selected_product["num"]

    is_pinned_by_obs = selected_product is not None
    if selected_product is None and all_products:
        selected_product = all_products[0]

    sel_reviews = get_mall_reviews(selected_product["num"]) if selected_product else []
    sel_review_summary = (
        get_mall_review_summary(selected_product["num"])
        if selected_product
        else {"count": 0, "avg_rating": 0.0}
    )

    return {
        "state_version": int(raw.get("state_version") or 1),
        "catalog_version": int(raw.get("catalog_version") or 1),
        "is_live": bool(raw.get("is_live", False)),
        "broadcast_title": str(raw.get("broadcast_title") or "실시간 라이브 커머스 특가 방송"),
        "ticker_message": str(raw.get("ticker_message") or "라이브 방송 중 선택된 상품을 페이지 이동 없이 바로 확인하고 특가로 구매하세요!"),
        "pinned_product_num": pinned_num,
        "is_pinned_by_obs": is_pinned_by_obs,
        "selected_product": selected_product,
        "selected_product_reviews": sel_reviews,
        "selected_product_review_summary": sel_review_summary,
        "all_products": all_products,
        "timer": raw.get("timer") or {"active": False},
        "youtube_video_id": str(raw.get("youtube_video_id") or ""),
        "youtube_channel_input": str(raw.get("youtube_channel_input") or ""),
        "youtube_embed_url": _derive_youtube_embed_url(raw),
        "live_urls": raw.get("live_urls") or {},
        "last_heartbeat_ts": float(raw.get("last_heartbeat_ts") or 0.0),
        "obs_server_url": str(raw.get("obs_server_url") or "http://localhost:5000"),
        "updated_at": latest_upd or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }


def get_lite_live_state():
    """
    CSR 실시간 구독(SSE / ETag 폴링)용 초경량 상태 페이로드를 반환합니다.
    - 전체 21개 상품 배열(all_products)을 제외하고 선택 상품 및 상품별 총재고 맵만 포함하여 트래픽을 90% 이상 절감합니다.
    """
    raw, latest_upd = _read_raw_live_state_and_check_heartbeat()

    pinned_num = raw.get("pinned_product_num")
    selected_product = None
    if pinned_num is not None and str(pinned_num).strip() != "":
        try:
            selected_product = get_mall_product_by_id(pinned_num)
        except Exception:
            selected_product = None
        if selected_product is not None:
            pinned_num = selected_product["num"]

    is_pinned_by_obs = selected_product is not None

    product_stocks = {}
    first_num = None
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT p.num, p.price, p.live_price, COALESCE(SUM(o.stock), 0) AS total_stock
                FROM mall_products p
                LEFT JOIN mall_product_options o ON o.product_id = p.id
                GROUP BY p.id
                ORDER BY p.num ASC, p.id DESC
            """)
            for r in cur.fetchall():
                if first_num is None:
                    first_num = r["num"]
                sell_p = int(r["price"] or 0)
                live_p = int(r["live_price"] or 0) if "live_price" in r.keys() and r["live_price"] is not None and int(r["live_price"]) > 0 else sell_p
                product_stocks[str(r["num"])] = {
                    "total_stock": int(r["total_stock"] or 0),
                    "price": sell_p,
                    "live_price": live_p
                }
        finally:
            conn.close()

    if selected_product is None and first_num is not None:
        selected_product = get_mall_product_by_num(first_num)

    return {
        "state_version": int(raw.get("state_version") or 1),
        "catalog_version": int(raw.get("catalog_version") or 1),
        "is_live": bool(raw.get("is_live", False)),
        "broadcast_title": str(raw.get("broadcast_title") or "실시간 라이브 커머스 특가 방송"),
        "ticker_message": str(raw.get("ticker_message") or "라이브 방송 중 선택된 상품을 페이지 이동 없이 바로 확인하고 특가로 구매하세요!"),
        "pinned_product_num": pinned_num,
        "is_pinned_by_obs": is_pinned_by_obs,
        "selected_product": selected_product,
        "product_stocks": product_stocks,
        "timer": raw.get("timer") or {"active": False},
        "youtube_video_id": str(raw.get("youtube_video_id") or ""),
        "youtube_embed_url": _derive_youtube_embed_url(raw),
        "live_urls": raw.get("live_urls") or {},
        "updated_at": latest_upd or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }


def apply_mall_discount_rate(discount_pct=30):
    """모든 쇼핑몰 상품의 판매가를 소비자가(consumer_price) 기준 지정 할인율(%)로 일괄 재계산합니다."""
    pct = max(0, min(95, int(discount_pct)))
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updated = 0
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, num, name, price, consumer_price FROM mall_products")
            rows = cur.fetchall()
            for r in rows:
                cons_p = int(r["consumer_price"] or 0)
                if cons_p <= 0:
                    cons_p = int(r["price"] or 0)
                if cons_p <= 0:
                    continue
                new_price = int(round(cons_p * (100 - pct) / 100.0))
                cur.execute("""
                    UPDATE mall_products
                    SET consumer_price = ?, price = ?, updated_at = ?
                    WHERE id = ?
                """, (cons_p, new_price, now_str, r["id"]))
                updated += 1
            conn.commit()
        finally:
            conn.close()
    if updated > 0:
        bump_state_version(bump_catalog=True)
    return {"updated": updated, "discount_pct": pct}


def create_mall_order(order_data):
    """
    쇼핑몰 주문 생성 및 옵션 재고 차감 트랜잭션 처리
    - items: [{"num": 1, "color": "블랙", "size": "L", "quantity": 1}, ...]
    """
    buyer_name = str(order_data.get("buyer_name") or "").strip()
    buyer_phone = str(order_data.get("buyer_phone") or "").strip()
    zipcode = str(order_data.get("zipcode") or "").strip()
    address = str(order_data.get("address") or "").strip()
    address_detail = str(order_data.get("address_detail") or "").strip()
    delivery_memo = str(order_data.get("delivery_memo") or "").strip()
    payment_method = "BANK"
    source_page = str(order_data.get("source_page") or "MALL").strip().upper()
    items_req = order_data.get("items") or []

    if not buyer_name or not buyer_phone or not address:
        return {"ok": False, "message": "수령인 성함, 연락처, 배송지 주소를 모두 입력해 주세요."}
    if not items_req:
        return {"ok": False, "message": "주문할 상품이 없습니다."}

    now_dt = datetime.now()
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    order_code = f"ORD-{now_dt.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            resolved_items = []
            subtotal = 0

            # 라이브 커머스 전용관(/live) 주문일 때만 라이브 특가 적용 (일반 쇼핑몰은 항상 일반 판매가 적용)
            use_live_pricing = (source_page == "LIVE")

            for it in items_req:
                if not isinstance(it, dict):
                    continue
                raw_num = it.get("num") if it.get("num") is not None else it.get("product_num")
                if raw_num is None or not str(raw_num).strip().lstrip("-").isdigit():
                    return {"ok": False, "message": "장바구니에 유효하지 않은 상품 번호가 포함되어 있습니다."}
                p_num = int(raw_num)
                color = str(it.get("color") or "-").strip() or "-"
                size = str(it.get("size") or "-").strip() or "-"
                try:
                    qty = max(1, int(it.get("quantity") or 1))
                except Exception:
                    qty = 1

                cur.execute("SELECT * FROM mall_products WHERE num = ?", (p_num,))
                p_row = cur.fetchone()
                if not p_row:
                    return {"ok": False, "message": f"상품 #{p_num} 정보를 찾을 수 없습니다."}

                mall_pid = p_row["id"]
                cur.execute("""
                    SELECT id, stock FROM mall_product_options
                    WHERE product_id = ? AND color = ? AND size = ?
                """, (mall_pid, color, size))
                opt_row = cur.fetchone()
                if opt_row:
                    if int(opt_row["stock"]) < qty:
                        return {
                            "ok": False,
                            "message": f"[{p_row['name']} ({color}/{size})] 재고가 부족합니다. (잔여: {opt_row['stock']}개)"
                        }
                    new_stk = int(opt_row["stock"]) - qty
                    cur.execute("UPDATE mall_product_options SET stock = ? WHERE id = ?", (new_stk, opt_row["id"]))
                else:
                    # 단일 옵션 상품 폴백 차감
                    cur.execute("SELECT id, color, size, stock FROM mall_product_options WHERE product_id = ? LIMIT 1", (mall_pid,))
                    any_opt = cur.fetchone()
                    if any_opt:
                        if int(any_opt["stock"]) < qty:
                            return {
                                "ok": False,
                                "message": f"[{p_row['name']}] 재고가 부족합니다. (잔여: {any_opt['stock']}개)"
                            }
                        cur.execute(
                            "UPDATE mall_product_options SET stock = ? WHERE id = ?",
                            (int(any_opt["stock"]) - qty, any_opt["id"])
                        )

                sell_price = int(p_row["price"] or 0)
                live_price = int(p_row["live_price"] or 0) if "live_price" in p_row.keys() and p_row["live_price"] is not None and int(p_row["live_price"]) > 0 else sell_price
                unit_price = live_price if use_live_pricing else sell_price
                line_total = unit_price * qty
                subtotal += line_total
                resolved_items.append({
                    "product_num": p_num,
                    "product_name": p_row["name"],
                    "image_url": p_row["image_url"] or "",
                    "color": color,
                    "size": size,
                    "quantity": qty,
                    "unit_price": unit_price,
                    "line_total": line_total
                })

            shipping_fee = 4000
            total_amount = subtotal + shipping_fee
            pay_status = "입금대기"
            ord_status = "결제대기"

            user_id = order_data.get("user_id")
            user_id = int(user_id) if user_id is not None and str(user_id).strip().isdigit() else None
            is_guest = 1 if (not user_id or order_data.get("is_guest")) else 0
            guest_pw_raw = str(order_data.get("guest_password") or "").strip()
            guest_pw_hash = ""
            if is_guest and guest_pw_raw:
                guest_pw_hash = mall_security.hash_password(guest_pw_raw)

            cur.execute("""
                INSERT INTO mall_orders
                (order_code, buyer_name, buyer_phone, zipcode, address, address_detail, delivery_memo,
                 payment_method, payment_status, order_status, subtotal_amount, shipping_fee, total_amount,
                 source_page, user_id, is_guest, guest_password_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                order_code, buyer_name, buyer_phone, zipcode, address, address_detail, delivery_memo,
                payment_method, pay_status, ord_status, subtotal, shipping_fee, total_amount,
                source_page, user_id, is_guest, guest_pw_hash, now_str
            ))
            order_id = cur.lastrowid

            for item in resolved_items:
                cur.execute("""
                    INSERT INTO mall_order_items
                    (order_id, product_num, product_name, image_url, color, size, quantity, unit_price, line_total)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    order_id,
                    item["product_num"],
                    item["product_name"],
                    item["image_url"],
                    item["color"],
                    item["size"],
                    item["quantity"],
                    item["unit_price"],
                    item["line_total"]
                ))

            conn.commit()
            bump_state_version(bump_catalog=True)
            return {
                "ok": True,
                "order_id": order_id,
                "order_code": order_code,
                "buyer_name": buyer_name,
                "buyer_phone": buyer_phone,
                "total_amount": total_amount,
                "subtotal_amount": subtotal,
                "shipping_fee": shipping_fee,
                "payment_method": payment_method,
                "payment_status": pay_status,
                "order_status": ord_status,
                "items": resolved_items,
                "order_items": resolved_items,
                "created_at": now_str
            }
        except Exception as e:
            conn.rollback()
            return {"ok": False, "message": f"주문 처리 중 오류가 발생했습니다: {e}"}
        finally:
            conn.close()


def get_mall_orders_after(after_id=0, limit=100):
    """클라우드 단독 운영 시 로컬 라이브 에이전트가 증분 Pull하여 OBS 재고를 차감할 수 있도록 신규 주문 목록을 오름차순으로 반환합니다."""
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM mall_orders WHERE id > ? ORDER BY id ASC LIMIT ?",
                (int(after_id or 0), int(limit))
            )
            ord_rows = cur.fetchall()
            orders = []
            for r in ord_rows:
                cur.execute("SELECT * FROM mall_order_items WHERE order_id = ? ORDER BY id ASC", (r["id"],))
                items = [dict(ir) for ir in cur.fetchall()]
                d = dict(r)
                d.pop("guest_password_hash", None)
                d["items"] = items
                d["order_items"] = items
                orders.append(d)
            return orders
        finally:
            conn.close()


def get_mall_orders(buyer_name=None, buyer_phone=None, order_code=None, limit=100):
    """쇼핑몰 주문 내역을 조회합니다 (관리자 전용)."""
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            where = []
            params = []
            if order_code and str(order_code).strip():
                where.append("order_code = ?")
                params.append(str(order_code).strip())
            if buyer_name and str(buyer_name).strip():
                where.append("buyer_name LIKE ?")
                params.append(f"%{str(buyer_name).strip()}%")
            if buyer_phone and str(buyer_phone).strip():
                clean_p = "".join(ch for ch in str(buyer_phone) if ch.isdigit())
                where.append("REPLACE(REPLACE(buyer_phone, '-', ''), ' ', '') LIKE ?")
                params.append(f"%{clean_p}%")

            where_sql = f"WHERE {' AND '.join(where)}" if where else ""
            cur.execute(f"SELECT * FROM mall_orders {where_sql} ORDER BY id DESC LIMIT ?", (*params, int(limit)))
            ord_rows = cur.fetchall()
            orders = []
            for r in ord_rows:
                cur.execute("SELECT * FROM mall_order_items WHERE order_id = ? ORDER BY id ASC", (r["id"],))
                items = [dict(ir) for ir in cur.fetchall()]
                d = dict(r)
                d.pop("guest_password_hash", None)
                d["items"] = items
                d["order_items"] = items
                orders.append(d)
            return orders
        finally:
            conn.close()


def get_mall_orders_by_user(user_id: int, limit: int = 50):
    """로그인한 쇼핑몰 회원의 본인 주문 내역만 안전하게 조회합니다."""
    if not user_id:
        return []
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM mall_orders WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (int(user_id), int(limit))
            )
            ord_rows = cur.fetchall()
            orders = []
            for r in ord_rows:
                cur.execute("SELECT * FROM mall_order_items WHERE order_id = ? ORDER BY id ASC", (r["id"],))
                items = [dict(ir) for ir in cur.fetchall()]
                d = dict(r)
                d.pop("guest_password_hash", None)
                d["items"] = items
                d["order_items"] = items
                orders.append(d)
            return orders
        finally:
            conn.close()


def get_mall_order_by_code(order_code: str):
    """동일 브라우저 세션에서 방금 완료한 주문을 주문번호로 단건 조회합니다."""
    code = str(order_code or "").strip()
    if not code:
        return []
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM mall_orders WHERE order_code = ? LIMIT 1", (code,))
            row = cur.fetchone()
            if not row:
                return []
            cur.execute("SELECT * FROM mall_order_items WHERE order_id = ? ORDER BY id ASC", (row["id"],))
            items = [dict(ir) for ir in cur.fetchall()]
            d = dict(row)
            d.pop("guest_password_hash", None)
            d["items"] = items
            d["order_items"] = items
            return [d]
        finally:
            conn.close()


def lookup_guest_mall_order(order_code: str, buyer_phone: str, guest_password: str):
    """
    비회원 라이브 주문 단건 조회 (3중 검증: 주문번호 + 연락처 숫자일치 + 비회원 주문 비밀번호 해시 일치).
    개인정보 노출 방지를 위해 3가지 중 하나라도 틀리면 빈 리스트를 반환합니다.
    """
    code = str(order_code or "").strip()
    clean_phone = "".join(ch for ch in str(buyer_phone or "") if ch.isdigit())
    pw = str(guest_password or "").strip()
    if not code or not clean_phone or not pw:
        return []

    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM mall_orders WHERE order_code = ? LIMIT 1", (code,))
            row = cur.fetchone()
            if not row:
                return []
            row_phone = "".join(ch for ch in str(row["buyer_phone"] or "") if ch.isdigit())
            if row_phone != clean_phone:
                return []
            stored_hash = str(row["guest_password_hash"] or "")
            if not stored_hash or not mall_security.verify_password(stored_hash, pw):
                return []

            cur.execute("SELECT * FROM mall_order_items WHERE order_id = ? ORDER BY id ASC", (row["id"],))
            items = [dict(ir) for ir in cur.fetchall()]
            d = dict(row)
            d.pop("guest_password_hash", None)
            d["items"] = items
            d["order_items"] = items
            return [d]
        finally:
            conn.close()


VALID_ORDER_STATUSES = (
    "결제대기",
    "상품준비중",
    "배송중",
    "배송완료",
    "주문취소",
    "환불요청",
    "환불완료",
    "교환요청",
    "교환완료",
)
VALID_PAYMENT_STATUSES = ("입금대기", "입금완료", "환불대기", "환불완료")
CLAIM_ORDER_STATUSES = ("환불요청", "환불완료", "교환요청", "교환완료")
STOCK_RESTORE_ORDER_STATUSES = ("주문취소", "환불완료")


def update_mall_order_workflow(
    order_id,
    payment_status=None,
    order_status=None,
    claim_reason=None,
):
    """
    쇼핑몰 주문 상태/입금 상태/환불·교환 사유를 갱신하고,
    '주문취소' 또는 '환불완료' 전환 시 아직 복구되지 않은 품목 재고를 1회 자동 복구합니다.
    """
    try:
        oid = int(order_id)
    except Exception:
        return {"ok": False, "message": "유효하지 않은 주문 ID입니다."}

    clean_pay = str(payment_status).strip() if payment_status is not None else ""
    clean_ord = str(order_status).strip() if order_status is not None else ""
    if clean_pay and clean_pay not in VALID_PAYMENT_STATUSES:
        return {"ok": False, "message": f"허용되지 않는 입금 상태입니다: {clean_pay}"}
    if clean_ord and clean_ord not in VALID_ORDER_STATUSES:
        return {"ok": False, "message": f"허용되지 않는 주문 상태입니다: {clean_ord}"}

    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM mall_orders WHERE id = ? LIMIT 1", (oid,))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "message": "주문을 찾을 수 없습니다."}

            cur_pay = str(row["payment_status"] or "입금대기")
            cur_ord = str(row["order_status"] or "결제대기")
            next_pay = clean_pay or cur_pay
            next_ord = clean_ord or cur_ord

            if clean_pay == "환불완료" and not clean_ord:
                next_ord = "환불완료"
            elif clean_ord == "환불완료" and not clean_pay:
                next_pay = "환불완료"
            elif clean_ord == "환불요청" and not clean_pay and next_pay == "입금완료":
                next_pay = "환불대기"

            cur_reason = str(row["claim_reason"] or "") if "claim_reason" in row.keys() else ""
            next_reason = str(claim_reason).strip() if claim_reason is not None else cur_reason
            cur_claim_ts = str(row["claim_updated_at"] or "") if "claim_updated_at" in row.keys() else ""
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if next_ord in ("주문취소", *CLAIM_ORDER_STATUSES) or claim_reason is not None or next_ord != cur_ord:
                next_claim_ts = now_str if (next_ord in ("주문취소", *CLAIM_ORDER_STATUSES) or claim_reason is not None) else cur_claim_ts
            else:
                next_claim_ts = cur_claim_ts

            already_restored = int(row["stock_restored"] or 0) if "stock_restored" in row.keys() else 0
            next_restored = already_restored
            restored_items = []

            if next_ord in STOCK_RESTORE_ORDER_STATUSES and already_restored == 0:
                cur.execute(
                    "SELECT product_num, color, size, quantity FROM mall_order_items WHERE order_id = ? ORDER BY id ASC",
                    (oid,),
                )
                ord_items = cur.fetchall()
                for it in ord_items:
                    p_num = int(it["product_num"])
                    color = str(it["color"] or "-")
                    size = str(it["size"] or "-")
                    qty = max(0, int(it["quantity"] or 0))
                    if qty <= 0:
                        continue
                    cur.execute("SELECT id FROM mall_products WHERE num = ? LIMIT 1", (p_num,))
                    p_row = cur.fetchone()
                    if p_row:
                        mall_pid = int(p_row["id"])
                        cur.execute(
                            "UPDATE mall_product_options SET stock = stock + ? WHERE product_id = ? AND color = ? AND size = ?",
                            (qty, mall_pid, color, size),
                        )
                        if cur.rowcount == 0:
                            cur.execute(
                                "SELECT id FROM mall_product_options WHERE product_id = ? ORDER BY id ASC LIMIT 1",
                                (mall_pid,),
                            )
                            fallback_opt = cur.fetchone()
                            if fallback_opt:
                                cur.execute(
                                    "UPDATE mall_product_options SET stock = stock + ? WHERE id = ?",
                                    (qty, int(fallback_opt["id"])),
                                )
                    restored_items.append({
                        "product_num": p_num,
                        "color": color,
                        "size": size,
                        "quantity": qty,
                    })
                next_restored = 1

            cur.execute(
                """
                UPDATE mall_orders
                SET payment_status = ?,
                    order_status = ?,
                    claim_reason = ?,
                    claim_updated_at = ?,
                    stock_restored = ?
                WHERE id = ?
                """,
                (next_pay, next_ord, next_reason, next_claim_ts, next_restored, oid),
            )
            conn.commit()
            bump_state_version(bump_catalog=True)

            cur.execute("SELECT * FROM mall_orders WHERE id = ? LIMIT 1", (oid,))
            updated_row = cur.fetchone()
            cur.execute("SELECT * FROM mall_order_items WHERE order_id = ? ORDER BY id ASC", (oid,))
            items = [dict(ir) for ir in cur.fetchall()]
            order_dict = dict(updated_row) if updated_row else {}
            order_dict.pop("guest_password_hash", None)
            order_dict["items"] = items
            order_dict["order_items"] = items

            return {
                "ok": True,
                "order": order_dict,
                "restored_items": restored_items,
                "stock_restored_now": bool(already_restored == 0 and next_restored == 1),
            }
        finally:
            conn.close()


def update_mall_order_status(order_id, payment_status=None, order_status=None):
    res = update_mall_order_workflow(
        order_id=order_id,
        payment_status=payment_status,
        order_status=order_status,
    )
    return bool(res.get("ok"))


def get_mall_reviews(product_num, limit=50):
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM mall_reviews WHERE product_num = ? ORDER BY id DESC LIMIT ?", (int(product_num), int(limit)))
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def get_mall_review_summary(product_num):
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) AS cnt, COALESCE(AVG(rating), 0) AS avg_rating FROM mall_reviews WHERE product_num = ?",
                (int(product_num),)
            )
            row = cur.fetchone()
            cnt = int(row["cnt"] or 0) if row else 0
            avg = round(float(row["avg_rating"] or 0.0), 1) if cnt > 0 else 0.0
            return {"count": cnt, "avg_rating": avg}
        finally:
            conn.close()


def add_mall_review(product_num, author, rating, content, user_id=None):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO mall_reviews (product_num, user_id, author, rating, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                int(product_num),
                int(user_id) if user_id is not None else None,
                str(author or "구매회원").strip(),
                max(1, min(5, int(rating or 5))),
                str(content or "").strip(),
                now_str
            ))
            conn.commit()
            return True
        finally:
            conn.close()


# ==============================================================================
# 🔐 쇼핑몰 전용 회원(mall_users) 및 관리자 인증 API
# ==============================================================================


def ensure_default_mall_admin():
    """최초 기동 시 쇼핑몰 관리자 계정을 보장합니다."""
    admin_email = (os.getenv("MALL_ADMIN_EMAIL") or "admin@mall.local").strip().lower()
    admin_pw = (os.getenv("MALL_ADMIN_PASSWORD") or "").strip()
    cred_file = os.path.join(BASE_DIR, ".mall_admin_credentials.txt")

    if not admin_pw:
        if os.path.exists(cred_file):
            try:
                with open(cred_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("PASSWORD="):
                            admin_pw = line.split("=", 1)[1].strip()
                            break
            except Exception:
                pass
        if not admin_pw:
            admin_pw = "MallAdmin!" + _secrets.token_urlsafe(9)
            try:
                with open(cred_file, "w", encoding="utf-8") as f:
                    f.write(f"EMAIL={admin_email}\nPASSWORD={admin_pw}\n")
                print(f"🔐 [쇼핑몰 초기 관리자 생성] 이메일: {admin_email} | 초기 비밀번호는 shopping_mall/.mall_admin_credentials.txt 에 저장되었습니다.")
            except Exception:
                pass

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM mall_users WHERE email = ?", (admin_email,))
            row = cur.fetchone()
            pw_hash = mall_security.hash_password(admin_pw)
            if not row:
                cur.execute("""
                    INSERT INTO mall_users (email, password_hash, name, phone, role, is_active, created_at)
                    VALUES (?, ?, '쇼핑몰 관리자', '010-0000-0000', 'ADMIN', 1, ?)
                """, (admin_email, pw_hash, now_str))
            elif os.getenv("MALL_ADMIN_PASSWORD"):
                cur.execute("""
                    UPDATE mall_users SET password_hash = ?, role = 'ADMIN', is_active = 1
                    WHERE id = ?
                """, (pw_hash, row["id"]))
            conn.commit()
        finally:
            conn.close()


def create_mall_user(email: str, password: str, name: str, phone: str, zipcode: str = "", address: str = "", address_detail: str = "") -> dict:
    mail = str(email or "").strip().lower()
    nm = str(name or "").strip()
    ph = str(phone or "").strip()
    if "@" not in mail or "." not in mail:
        return {"ok": False, "message": "유효한 이메일 주소를 입력해 주세요."}
    if not nm or not ph:
        return {"ok": False, "message": "성함과 연락처를 모두 입력해 주세요."}

    valid_pw, pw_msg = mall_security.validate_password_policy(password, min_len=8)
    if not valid_pw:
        return {"ok": False, "message": pw_msg}

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM mall_users WHERE email = ?", (mail,))
            if cur.fetchone():
                return {"ok": False, "message": "이미 가입된 이메일 주소입니다."}
            pw_hash = mall_security.hash_password(password)
            cur.execute("""
                INSERT INTO mall_users (email, password_hash, name, phone, zipcode, address, address_detail, role, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'MEMBER', 1, ?)
            """, (
                mail, pw_hash, nm, ph,
                str(zipcode or "").strip(),
                str(address or "").strip(),
                str(address_detail or "").strip(),
                now_str
            ))
            uid = cur.lastrowid
            conn.commit()
            return {
                "ok": True,
                "user": {
                    "id": uid,
                    "email": mail,
                    "name": nm,
                    "phone": ph,
                    "zipcode": str(zipcode or "").strip(),
                    "address": str(address or "").strip(),
                    "address_detail": str(address_detail or "").strip(),
                    "role": "MEMBER"
                }
            }
        finally:
            conn.close()


def authenticate_mall_user(email: str, password: str) -> dict:
    mail = str(email or "").strip().lower()
    if not mail or not password:
        return {"ok": False, "code": "INVALID", "message": "이메일과 비밀번호를 입력해 주세요."}

    now_ts = time.time()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, email, password_hash, name, phone, zipcode, address, address_detail, role, is_active, failed_login_count, locked_until
                FROM mall_users
                WHERE email = ?
            """, (mail,))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "code": "INVALID", "message": "이메일 또는 비밀번호가 올바르지 않습니다."}
            if not row["is_active"]:
                return {"ok": False, "code": "SUSPENDED", "message": "이용이 정지된 계정입니다."}

            locked_until = float(row["locked_until"] or 0.0)
            if locked_until > now_ts:
                remain = max(1, int(round(locked_until - now_ts)))
                return {"ok": False, "code": "LOCKED", "message": f"비밀번호 5회 연속 오류로 계정이 잠겼습니다. {remain}초 후 다시 시도해 주세요."}

            if not mall_security.verify_password(row["password_hash"], password):
                fails = int(row["failed_login_count"] or 0) + 1
                new_lock = (now_ts + 600.0) if fails >= 5 else 0.0
                cur.execute("""
                    UPDATE mall_users SET failed_login_count = ?, locked_until = ? WHERE id = ?
                """, (fails, new_lock, row["id"]))
                conn.commit()
                if new_lock > 0:
                    return {"ok": False, "code": "LOCKED", "message": "비밀번호 5회 오류로 10분간 로그인이 제한됩니다."}
                return {"ok": False, "code": "INVALID", "message": "이메일 또는 비밀번호가 올바르지 않습니다."}

            cur.execute("""
                UPDATE mall_users SET failed_login_count = 0, locked_until = 0, last_login_at = ? WHERE id = ?
            """, (now_str, row["id"]))
            conn.commit()
            return {
                "ok": True,
                "user": {
                    "id": row["id"],
                    "email": row["email"],
                    "name": row["name"],
                    "phone": row["phone"],
                    "zipcode": row["zipcode"] or "",
                    "address": row["address"] or "",
                    "address_detail": row["address_detail"] or "",
                    "role": row["role"]
                }
            }
        finally:
            conn.close()


def get_mall_user_by_id(user_id: int):
    if not user_id:
        return None
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, email, name, phone, zipcode, address, address_detail, role, is_active, created_at
                FROM mall_users
                WHERE id = ? AND is_active = 1
            """, (int(user_id),))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def get_primary_mall_admin_user():
    """쇼핑몰 활성 관리자(ADMIN) 대표 계정을 조회합니다 (없으면 초기 관리자 자동 생성 후 반환)."""
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, email, name, phone, zipcode, address, address_detail, role, is_active, created_at
                FROM mall_users
                WHERE role = 'ADMIN' AND is_active = 1
                ORDER BY id ASC
                LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                return dict(row)
        finally:
            conn.close()

    ensure_default_mall_admin()
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, email, name, phone, zipcode, address, address_detail, role, is_active, created_at
                FROM mall_users
                WHERE role = 'ADMIN' AND is_active = 1
                ORDER BY id ASC
                LIMIT 1
            """)
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def _normalize_wish_product_nums(raw_nums) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for val in (raw_nums or []):
        try:
            num = int(str(val).strip())
        except Exception:
            continue
        if num <= 0 or num in seen:
            continue
        seen.add(num)
        result.append(num)
        if len(result) >= 100:
            break
    return result


def get_user_wishlist_nums(user_id: int) -> list[int]:
    if not user_id:
        return []
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT w.product_num
                FROM mall_wishlist w
                INNER JOIN mall_products p ON p.num = w.product_num
                WHERE w.user_id = ?
                ORDER BY w.id DESC
                """,
                (int(user_id),),
            )
            return [int(r["product_num"]) for r in cur.fetchall()]
        finally:
            conn.close()


def get_product_wish_count(product_num: int) -> int:
    try:
        p_num = int(product_num)
    except Exception:
        return 0
    if p_num <= 0:
        return 0
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM mall_wishlist WHERE product_num = ?", (p_num,))
            row = cur.fetchone()
            return int(row[0] or 0) if row else 0
        finally:
            conn.close()


def get_products_by_nums(product_nums) -> list[dict]:
    clean_nums = _normalize_wish_product_nums(product_nums)
    if not clean_nums:
        return []
    products: list[dict] = []
    with mall_db_lock:
        for num in clean_nums:
            prod = get_mall_product_by_num(num)
            if prod:
                products.append(prod)
    return products


def get_user_wishlist_products(user_id: int) -> list[dict]:
    nums = get_user_wishlist_nums(user_id)
    return get_products_by_nums(nums)


def toggle_user_wishlist(user_id: int, product_num: int) -> dict:
    try:
        uid = int(user_id)
        p_num = int(product_num)
    except Exception:
        return {"ok": False, "message": "유효하지 않은 요청입니다."}
    if uid <= 0 or p_num <= 0:
        return {"ok": False, "message": "유효하지 않은 상품 또는 회원 정보입니다."}

    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM mall_products WHERE num = ? LIMIT 1", (p_num,))
            if not cur.fetchone():
                return {"ok": False, "message": "존재하지 않는 상품입니다."}

            cur.execute(
                "SELECT id FROM mall_wishlist WHERE user_id = ? AND product_num = ? LIMIT 1",
                (uid, p_num),
            )
            existing = cur.fetchone()
            if existing:
                cur.execute("DELETE FROM mall_wishlist WHERE id = ?", (int(existing["id"]),))
                wished = False
            else:
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    "INSERT OR IGNORE INTO mall_wishlist (user_id, product_num, created_at) VALUES (?, ?, ?)",
                    (uid, p_num, now_str),
                )
                wished = True
            conn.commit()
        finally:
            conn.close()

    nums = get_user_wishlist_nums(uid)
    return {
        "ok": True,
        "wished": wished,
        "product_num": p_num,
        "wishlist_nums": nums,
        "wishlist_count": len(nums),
        "product_wish_count": get_product_wish_count(p_num),
    }


def merge_user_wishlist(user_id: int, product_nums) -> list[int]:
    try:
        uid = int(user_id)
    except Exception:
        return []
    if uid <= 0:
        return []
    clean_nums = _normalize_wish_product_nums(product_nums)
    if clean_nums:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with mall_db_lock:
            conn = get_connection()
            try:
                cur = conn.cursor()
                for p_num in reversed(clean_nums):
                    cur.execute("SELECT 1 FROM mall_products WHERE num = ? LIMIT 1", (p_num,))
                    if cur.fetchone():
                        cur.execute(
                            "INSERT OR IGNORE INTO mall_wishlist (user_id, product_num, created_at) VALUES (?, ?, ?)",
                            (uid, p_num, now_str),
                        )
                conn.commit()
            finally:
                conn.close()
    return get_user_wishlist_nums(uid)


def clear_user_wishlist(user_id: int) -> int:
    try:
        uid = int(user_id)
    except Exception:
        return 0
    if uid <= 0:
        return 0
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM mall_wishlist WHERE user_id = ?", (uid,))
            deleted = int(cur.rowcount or 0)
            conn.commit()
            return deleted
        finally:
            conn.close()


try:
    init_mall_db()
except Exception:
    pass
