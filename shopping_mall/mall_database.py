# -*- coding: utf-8 -*-
"""
독립 쇼핑몰 전용 SQLite 데이터베이스 관리 모듈 (mall_database.py)
- OBS 프로그램의 DB(live_commerce.db)와 완전히 분리된 별도의 DB(shopping_mall.db)로 운영됩니다.
- OBS에서 등록/수정된 상품(products, options, 재고, 이미지) 및 라이브 선택(Pin) 상품 상태를
  실시간 동기화 받아 저장하며, 쇼핑몰 자체 주문/결제/장바구니/리뷰 데이터를 관리합니다.
"""

import os
import re
import math
import time
import json
import uuid
import sqlite3
import secrets as _secrets
from datetime import datetime
from threading import RLock, Condition
from typing import Any, Optional

import mall_security
import mall_shipping

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MALL_DB_PATH = os.getenv("MALL_DB_PATH", os.path.join(BASE_DIR, "shopping_mall.db"))
HEARTBEAT_TIMEOUT_SEC = float(os.getenv("MALL_HEARTBEAT_TIMEOUT_SEC", "35"))
SQLITE_INT64_MIN = -9223372036854775808
SQLITE_INT64_MAX = 9223372036854775807
mall_db_lock = RLock()
mall_state_cond = Condition()


def parse_strict_int64(
    val: Any,
    min_val: Optional[int] = None,
    max_val: Optional[int] = None,
) -> Optional[int]:
    """bool/float/문자열 쓰레기값 및 SQLite int64 범위 초과 정수를 거부합니다."""
    if val is None or isinstance(val, bool) or isinstance(val, float):
        return None
    if isinstance(val, int):
        num = val
    else:
        s = str(val).strip()
        if not s or not re.fullmatch(r"-?[0-9]+", s):
            return None
        is_neg = s.startswith("-")
        digits = (s[1:] if is_neg else s).lstrip("0") or "0"
        if len(digits) > 19:
            return None
        num = -int(digits) if is_neg else int(digits)
    if num < SQLITE_INT64_MIN or num > SQLITE_INT64_MAX:
        return None
    if min_val is not None and num < min_val:
        return None
    if max_val is not None and num > max_val:
        return None
    return num

def _safe_live_float(val: Any, default: float = 0.0, min_val: float = 0.0) -> float:
    """mall_live_state 실수 필드 파싱 시 예외나 비유한값(NaN/Inf)으로 인한 500을 방지합니다."""
    if val is None or isinstance(val, bool):
        return default
    if isinstance(val, (int, float)):
        num = float(val)
    elif isinstance(val, str):
        s = val.strip()
        if not s:
            return default
        try:
            num = float(s)
        except (ValueError, TypeError, OverflowError):
            return default
    else:
        return default
    if not math.isfinite(num) or num < min_val:
        return default
    return num


def _safe_live_int(val: Any, default: int = 1, min_val: int = 1) -> int:
    """mall_live_state 정수 필드 파싱 시 예외나 범위 초과로 인한 500을 방지합니다."""
    parsed = parse_strict_int64(val, min_val=min_val)
    if parsed is None:
        return default
    return parsed


def _parse_strict_non_neg_finite_float(val: Any) -> Optional[float]:
    """bool/NaN/Inf/음수/비수치 문자열을 거부하고 0 이상의 유한 실수를 반환합니다."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        num = float(val)
    elif isinstance(val, str):
        s = val.strip()
        if not s or not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", s):
            return None
        try:
            num = float(s)
        except (ValueError, TypeError, OverflowError):
            return None
    else:
        return None
    if not math.isfinite(num) or num < 0.0:
        return None
    return num


def validate_live_state_bundle(state_dict: Any) -> Optional[dict]:
    """OBS 동기화 live_state / live_bundle 페이로드의 핵심 타입과 범위를 DB 반영 전 엄격히 검증합니다."""
    if not isinstance(state_dict, dict):
        return None
    out = dict(state_dict)
    if "last_heartbeat_ts" in out:
        hb = _parse_strict_non_neg_finite_float(out["last_heartbeat_ts"])
        if hb is None:
            return None
        out["last_heartbeat_ts"] = hb
    for ver_key in ("state_version", "catalog_version"):
        if ver_key in out:
            ver_val = parse_strict_int64(out[ver_key], min_val=1)
            if ver_val is None:
                return None
            out[ver_key] = ver_val
    for num_key in ("pinned_product_num", "active_product_num"):
        if num_key in out and out[num_key] is not None:
            parsed_num = parse_strict_int64(out[num_key], min_val=0)
            if parsed_num is None:
                return None
            out[num_key] = parsed_num
    for dict_key in ("timer", "live_urls"):
        if dict_key in out and out[dict_key] is not None and not isinstance(out[dict_key], dict):
            return None
    return out


DEFAULT_MALL_CATEGORIES = [
    ("TOP", "상의/탑", 10),
    ("OUTER", "아우터", 20),
    ("BOTTOM", "팬츠/스커트", 30),
    ("DRESS", "원피스/세트", 40),
    ("ACC", "잡화/악세서리", 50),
]

DEFAULT_MALL_POLICIES = {
    "shipping_info": mall_shipping.format_shipping_policy_line({
        "shipping_fee": mall_shipping.DEFAULT_SHIPPING_FEE,
        "free_shipping_threshold": mall_shipping.DEFAULT_FREE_SHIPPING_THRESHOLD,
    }),
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
    "shipping_fee": mall_shipping.DEFAULT_SHIPPING_FEE,
    "free_shipping_threshold": mall_shipping.DEFAULT_FREE_SHIPPING_THRESHOLD,
    "point_earn_rate": 1,
    "unpaid_cancel_hours": 72,
}

_STORE_INFO_INT_FIELDS = {
    "shipping_fee": (mall_shipping.DEFAULT_SHIPPING_FEE, 1_000_000),
    "free_shipping_threshold": (mall_shipping.DEFAULT_FREE_SHIPPING_THRESHOLD, 100_000_000),
    "unpaid_cancel_hours": (72, 8760),
}
_STORE_INFO_FLOAT_FIELDS = {
    "point_earn_rate": (1, 0.0, 100.0),
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
            cur_v = 0
            if row and row["value"] is not None:
                try:
                    cur_v = _safe_live_int(json.loads(row["value"]), default=0, min_val=0)
                except Exception:
                    cur_v = 0
            next_v = cur_v + 1
            cur.execute("""
                INSERT INTO mall_live_state (key, value, updated_at)
                VALUES ('state_version', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """, (json.dumps(next_v), now_str))

            if bump_catalog:
                cur.execute("SELECT value FROM mall_live_state WHERE key = 'catalog_version'")
                crow = cur.fetchone()
                cur_cv = 0
                if crow and crow["value"] is not None:
                    try:
                        cur_cv = _safe_live_int(json.loads(crow["value"]), default=0, min_val=0)
                    except Exception:
                        cur_cv = 0
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


def get_db_path() -> str:
    """현재 활성 쇼핑몰 SQLite DB 파일 경로를 반환합니다."""
    return MALL_DB_PATH


def set_db_path(new_path: str, init: bool = True) -> str:
    """테스트 격리 및 환경 전환을 위해 MALL_DB_PATH를 변경하고 선택적으로 스키마를 초기화합니다."""
    global MALL_DB_PATH
    raw = str(new_path or "").strip()
    if not raw:
        raise ValueError("new_path must not be empty")
    resolved = raw if raw == ":memory:" else os.path.abspath(raw)
    if resolved != ":memory:":
        parent = os.path.dirname(resolved)
        if parent:
            os.makedirs(parent, exist_ok=True)
    with mall_db_lock:
        MALL_DB_PATH = resolved
        if init:
            init_mall_db()
    return MALL_DB_PATH


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
                    depositor_name TEXT DEFAULT '',
                    cash_receipt_type TEXT NOT NULL DEFAULT 'NONE',
                    cash_receipt_no TEXT DEFAULT '',
                    pg_provider TEXT DEFAULT '',
                    pg_tid TEXT DEFAULT '',
                    pg_status TEXT DEFAULT '',
                    paid_at TEXT DEFAULT '',
                    payment_status TEXT NOT NULL DEFAULT '입금대기',
                    order_status TEXT NOT NULL DEFAULT '결제대기',
                    carrier_code TEXT DEFAULT '',
                    carrier_name TEXT DEFAULT '',
                    tracking_number TEXT DEFAULT '',
                    shipped_at TEXT DEFAULT '',
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
                    images TEXT NOT NULL DEFAULT '[]',
                    image_url TEXT NOT NULL DEFAULT '',
                    is_verified_buyer INTEGER NOT NULL DEFAULT 0,
                    is_hidden INTEGER NOT NULL DEFAULT 0,
                    admin_reply TEXT NOT NULL DEFAULT '',
                    admin_replied_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
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
                    withdrawn_at TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)

            # 기존 mall_products, mall_reviews, mall_orders 및 mall_order_items 호환성 마이그레이션
            for tbl, col_name, col_def in [
                ("mall_products", "live_price", "INTEGER NOT NULL DEFAULT 0"),
                ("mall_products", "cost_price", "INTEGER NOT NULL DEFAULT 0"),
                ("mall_products", "supplier_id", "INTEGER DEFAULT NULL"),
                ("mall_products", "detail_content", "TEXT DEFAULT ''"),
                ("mall_products", "detail_images", "TEXT DEFAULT '[]'"),
                ("mall_products", "shipping_info", "TEXT DEFAULT ''"),
                ("mall_products", "exchange_return_info", "TEXT DEFAULT ''"),
                ("mall_products", "non_returnable_info", "TEXT DEFAULT ''"),
                ("mall_products", "as_info", "TEXT DEFAULT ''"),
                ("mall_reviews", "user_id", "INTEGER DEFAULT NULL"),
                ("mall_reviews", "images", "TEXT NOT NULL DEFAULT '[]'"),
                ("mall_reviews", "image_url", "TEXT NOT NULL DEFAULT ''"),
                ("mall_reviews", "is_verified_buyer", "INTEGER NOT NULL DEFAULT 0"),
                ("mall_reviews", "is_hidden", "INTEGER NOT NULL DEFAULT 0"),
                ("mall_reviews", "admin_reply", "TEXT NOT NULL DEFAULT ''"),
                ("mall_reviews", "admin_replied_at", "TEXT NOT NULL DEFAULT ''"),
                ("mall_reviews", "updated_at", "TEXT NOT NULL DEFAULT ''"),
                ("mall_orders", "user_id", "INTEGER DEFAULT NULL"),
                ("mall_orders", "zipcode", "TEXT DEFAULT ''"),
                ("mall_orders", "address", "TEXT DEFAULT ''"),
                ("mall_orders", "address_detail", "TEXT DEFAULT ''"),
                ("mall_orders", "is_guest", "INTEGER NOT NULL DEFAULT 0"),
                ("mall_orders", "guest_password_hash", "TEXT DEFAULT ''"),
                ("mall_orders", "carrier_code", "TEXT DEFAULT ''"),
                ("mall_orders", "carrier_name", "TEXT DEFAULT ''"),
                ("mall_orders", "tracking_number", "TEXT DEFAULT ''"),
                ("mall_orders", "shipped_at", "TEXT DEFAULT ''"),
                ("mall_orders", "claim_reason", "TEXT DEFAULT ''"),
                ("mall_orders", "claim_updated_at", "TEXT DEFAULT ''"),
                ("mall_orders", "stock_restored", "INTEGER NOT NULL DEFAULT 0"),
                ("mall_orders", "depositor_name", "TEXT DEFAULT ''"),
                ("mall_orders", "cash_receipt_type", "TEXT NOT NULL DEFAULT 'NONE'"),
                ("mall_orders", "cash_receipt_no", "TEXT DEFAULT ''"),
                ("mall_orders", "pg_provider", "TEXT DEFAULT ''"),
                ("mall_orders", "pg_tid", "TEXT DEFAULT ''"),
                ("mall_orders", "pg_status", "TEXT DEFAULT ''"),
                ("mall_orders", "paid_at", "TEXT DEFAULT ''"),
                ("mall_users", "zipcode", "TEXT DEFAULT ''"),
                ("mall_users", "address", "TEXT DEFAULT ''"),
                ("mall_users", "address_detail", "TEXT DEFAULT ''"),
                ("mall_users", "withdrawn_at", "TEXT DEFAULT ''"),
                ("mall_order_items", "unit_cost", "INTEGER NOT NULL DEFAULT 0"),
                ("mall_order_items", "cost_total", "INTEGER NOT NULL DEFAULT 0"),
                ("mall_order_items", "supplier_id", "INTEGER DEFAULT NULL"),
                ("mall_order_items", "supplier_name", "TEXT DEFAULT ''"),
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

            # 12. 메인 페이지 이벤트 히어로 배너 테이블
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mall_hero_banners (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    eyebrow TEXT DEFAULT '',
                    title TEXT DEFAULT '',
                    subtitle TEXT DEFAULT '',
                    cta_label TEXT DEFAULT '',
                    link_url TEXT DEFAULT '',
                    image_url TEXT DEFAULT '',
                    theme TEXT DEFAULT 'indigo',
                    sort_order INTEGER DEFAULT 10,
                    is_active INTEGER DEFAULT 1,
                    start_at TEXT DEFAULT '',
                    end_at TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_mall_hero_banners_active_sort ON mall_hero_banners (is_active, sort_order, id DESC)"
            )

            # 13. 매입업체 마스터 테이블 (mall_suppliers)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mall_suppliers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    contact_name TEXT DEFAULT '',
                    phone TEXT DEFAULT '',
                    bank_name TEXT DEFAULT '',
                    bank_account TEXT DEFAULT '',
                    bank_holder TEXT DEFAULT '',
                    memo TEXT DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            # 14. 매입업체별 실제 지급(결제) 내역 테이블 (mall_supplier_settlements)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mall_supplier_settlements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    supplier_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL DEFAULT 0,
                    paid_at TEXT NOT NULL,
                    payment_method TEXT NOT NULL DEFAULT '계좌이체',
                    memo TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (supplier_id) REFERENCES mall_suppliers (id) ON DELETE CASCADE
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_mall_supplier_settlements_sup ON mall_supplier_settlements (supplier_id, paid_at DESC, id DESC)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_mall_orders_created_pay ON mall_orders (created_at, payment_status, order_status)"
            )

            # 15. 회원 다중 배송지 주소록 (mall_user_addresses)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mall_user_addresses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    label TEXT NOT NULL DEFAULT '기본 배송지',
                    recipient_name TEXT NOT NULL,
                    recipient_phone TEXT NOT NULL,
                    zipcode TEXT DEFAULT '',
                    address TEXT NOT NULL,
                    address_detail TEXT DEFAULT '',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES mall_users (id) ON DELETE CASCADE
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_mall_user_addresses_user ON mall_user_addresses (user_id, is_default DESC, id DESC)"
            )

            # 16. 회원 비밀번호 재설정 토큰·임시코드 (mall_password_resets)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mall_password_resets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    email TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    code_hash TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    used_at TEXT DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES mall_users (id) ON DELETE CASCADE
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_mall_password_resets_token ON mall_password_resets (token_hash)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_mall_password_resets_user ON mall_password_resets (user_id, id DESC)"
            )

            # 17. 다채널 알림 발송 및 폴백 로그 (mall_notification_logs - M2/M3 공용 스키마)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mall_notification_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    subject TEXT DEFAULT '',
                    body TEXT DEFAULT '',
                    status TEXT NOT NULL,
                    error_msg TEXT DEFAULT '',
                    ref_code TEXT DEFAULT '',
                    meta_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_mall_notification_logs_created ON mall_notification_logs (id DESC)"
            )

            # 18. 상품 Q&A 테이블 (mall_product_qna - 비밀글 지원)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mall_product_qna (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_num INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    author_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    is_secret INTEGER NOT NULL DEFAULT 0,
                    answer_content TEXT DEFAULT '',
                    answered_by TEXT DEFAULT '',
                    answered_at TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_product_qna_num ON mall_product_qna (product_num, id DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_product_qna_user ON mall_product_qna (user_id, id DESC)")

            # 19. 1:1 고객 문의 테이블 (mall_inquiries)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mall_inquiries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    category TEXT NOT NULL DEFAULT '일반문의',
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    order_code TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT '접수대기',
                    answer_content TEXT DEFAULT '',
                    answered_by TEXT DEFAULT '',
                    answered_at TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_inquiries_user ON mall_inquiries (user_id, id DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_inquiries_status ON mall_inquiries (status, id DESC)")

            # 20. 스토어 공지사항 테이블 (mall_notices)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mall_notices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    is_pinned INTEGER NOT NULL DEFAULT 0,
                    author_name TEXT NOT NULL DEFAULT '관리자',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_notices_pin ON mall_notices (is_pinned DESC, id DESC)")

            # 21. 자주 묻는 질문 테이블 (mall_faqs)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mall_faqs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL DEFAULT '주문/결제',
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 10,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_faqs_active ON mall_faqs (is_active, sort_order ASC, id ASC)")

            # 22. 재입고 알림 신청 테이블 (mall_restock_alerts - F16)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mall_restock_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_num INTEGER NOT NULL,
                    color TEXT NOT NULL DEFAULT '-',
                    size TEXT NOT NULL DEFAULT '-',
                    user_id INTEGER DEFAULT NULL,
                    contact_type TEXT NOT NULL DEFAULT 'EMAIL',
                    contact_value TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    notified_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_mall_restock_alerts_lookup ON mall_restock_alerts (product_num, status, id DESC)"
            )

            # 23. 관리자 작업 감사 로그 테이블 (mall_audit_logs - F18)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mall_audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_user_id INTEGER DEFAULT NULL,
                    admin_email TEXT DEFAULT '',
                    admin_name TEXT DEFAULT '',
                    action TEXT NOT NULL,
                    target_type TEXT DEFAULT '',
                    target_id TEXT DEFAULT '',
                    detail_json TEXT DEFAULT '{}',
                    ip_address TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_audit_logs_created ON mall_audit_logs (id DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_audit_logs_action ON mall_audit_logs (action, id DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_audit_logs_target ON mall_audit_logs (target_type, target_id)")

            import mall_promotions
            mall_promotions.ensure_promotions_schema(conn)

            conn.commit()
        finally:
            conn.close()
    try:
        ensure_default_mall_admin()
    except Exception:
        pass


def _snapshot_product_option_stocks(cur, product_id: int) -> tuple[dict[tuple[str, str], int], int]:
    cur.execute(
        "SELECT color, size, stock FROM mall_product_options WHERE product_id = ?",
        (int(product_id),),
    )
    stocks: dict[tuple[str, str], int] = {}
    total = 0
    for r in cur.fetchall():
        c = str(r["color"] or "-").strip() or "-"
        s = str(r["size"] or "-").strip() or "-"
        stk = max(0, int(r["stock"] or 0))
        stocks[(c, s)] = stk
        total += stk
    return stocks, total


def _collect_restock_notifications_tx(
    cur,
    product_id: int,
    product_num: int,
    product_name: str,
    prev_stocks: dict[tuple[str, str], int],
    prev_total: int,
    now_str: str,
) -> list[dict[str, Any]]:
    new_stocks, new_total = _snapshot_product_option_stocks(cur, product_id)
    restocked_skus = {
        (c, s): stk
        for (c, s), stk in new_stocks.items()
        if stk > 0 and prev_stocks.get((c, s), 0) <= 0
    }
    product_restocked = (prev_total <= 0 and new_total > 0)
    if not restocked_skus and not product_restocked:
        return []

    cur.execute(
        """
        SELECT * FROM mall_restock_alerts
        WHERE product_num = ? AND status = 'PENDING'
        ORDER BY id ASC
        """,
        (int(product_num),),
    )
    pending_rows = cur.fetchall()
    to_dispatch: list[dict[str, Any]] = []
    for ar in pending_rows:
        ac = str(ar["color"] or "-").strip() or "-"
        az = str(ar["size"] or "-").strip() or "-"
        matched_stock: Optional[int] = None
        if (ac, az) in restocked_skus:
            matched_stock = restocked_skus[(ac, az)]
        elif ac in ("-", "") and az in ("-", "") and product_restocked:
            matched_stock = new_total
        if matched_stock is None:
            continue
        cur.execute(
            """
            UPDATE mall_restock_alerts
            SET status = 'NOTIFIED', notified_at = ?
            WHERE id = ? AND status = 'PENDING'
            """,
            (now_str, int(ar["id"])),
        )
        if cur.rowcount == 1:
            alert_dict = dict(ar)
            alert_dict["status"] = "NOTIFIED"
            alert_dict["notified_at"] = now_str
            to_dispatch.append({
                "alert": alert_dict,
                "product_name": str(product_name or f"상품 #{product_num}"),
                "new_stock": int(matched_stock),
            })
    return to_dispatch


def _dispatch_restock_notifications(events: list[dict[str, Any]]) -> None:
    if not events:
        return
    try:
        import mall_notifications
    except Exception:
        return
    for ev in events:
        try:
            mall_notifications.notify_restock_alert(
                ev["alert"],
                product_name=ev.get("product_name", ""),
                new_stock=int(ev.get("new_stock") or 0),
            )
        except Exception as exc:
            print(f"[ShoppingMall] RESTOCK_ALERT 알림 경고: {exc}")


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


def _get_supplier_name_map(conn) -> dict[int, str]:
    try:
        rows = conn.execute("SELECT id, name FROM mall_suppliers").fetchall()
        return {int(r["id"]): str(r["name"] or "") for r in rows if r["id"] is not None}
    except Exception:
        return {}


def _format_product_row(prow, opt_rows, supplier_map: dict[int, str] | None = None):
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
    cost_p = max(0, int(prow["cost_price"] or 0)) if "cost_price" in keys and prow["cost_price"] is not None else 0
    raw_sup_id = prow["supplier_id"] if "supplier_id" in keys else None
    try:
        sup_id = int(raw_sup_id) if raw_sup_id is not None and int(raw_sup_id) > 0 else None
    except Exception:
        sup_id = None
    sup_name = (supplier_map or {}).get(sup_id, "") if sup_id else ""
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
        "cost_price": cost_p,
        "supplier_id": sup_id,
        "supplier_name": sup_name,
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


def parse_obs_product_payload(prod_data):
    """OBS 동기화 상품 payload의 숫자형 필드(num, obs_id, 가격, 옵션 재고)를 엄격히 검증·정규화합니다."""
    if not isinstance(prod_data, dict):
        return None
    num = parse_strict_int64(prod_data.get("num"), min_val=1)
    if num is None:
        return None

    raw_db_id = prod_data.get("db_id")
    raw_id = prod_data.get("id")
    if isinstance(raw_db_id, (bool, float)) or isinstance(raw_id, (bool, float)):
        return None
    raw_obs = raw_db_id if raw_db_id not in (None, "", 0, "0") else raw_id
    if raw_obs in (None, "", 0, "0"):
        obs_id = 0
    else:
        if isinstance(raw_obs, str):
            obs_str = raw_obs.strip()
            if obs_str.startswith("item-"):
                obs_str = obs_str[5:].strip()
            obs_id = parse_strict_int64(obs_str, min_val=0)
        else:
            obs_id = parse_strict_int64(raw_obs, min_val=0)
        if obs_id is None:
            return None

    raw_price = prod_data.get("price")
    if isinstance(raw_price, (bool, float)):
        return None
    if raw_price is None or (isinstance(raw_price, str) and raw_price.strip() == ""):
        price = 0
    else:
        price = parse_strict_int64(raw_price, min_val=0)
        if price is None:
            return None

    raw_lp = prod_data.get("live_price")
    if isinstance(raw_lp, (bool, float)):
        return None
    if raw_lp in (None, 0, "0") or (isinstance(raw_lp, str) and raw_lp.strip() in ("", "0")):
        live_price = price
    else:
        live_price = parse_strict_int64(raw_lp, min_val=0)
        if live_price is None:
            return None
        if live_price <= 0:
            live_price = price

    raw_cp = prod_data.get("consumer_price")
    if isinstance(raw_cp, (bool, float)):
        return None
    if raw_cp in (None, 0, "0") or (isinstance(raw_cp, str) and raw_cp.strip() in ("", "0")):
        consumer_price = price
    else:
        parsed_cp = parse_strict_int64(raw_cp, min_val=0)
        if parsed_cp is None:
            return None
        consumer_price = max(price, parsed_cp)

    has_options = "options" in prod_data and prod_data.get("options") is not None
    parsed_options = None
    if has_options:
        raw_opts = prod_data.get("options")
        if not isinstance(raw_opts, list):
            return None
        if not raw_opts:
            parsed_options = [{"color": "기본", "size": "FREE", "stock": 10}]
        else:
            parsed_options = []
            for opt in raw_opts:
                if not isinstance(opt, dict):
                    return None
                raw_stk = opt.get("stock")
                if isinstance(raw_stk, (bool, float)):
                    return None
                if raw_stk is None or (isinstance(raw_stk, str) and raw_stk.strip() == ""):
                    stk = 0
                else:
                    stk = parse_strict_int64(raw_stk, min_val=0)
                    if stk is None:
                        return None
                c = str(opt.get("color") or "-").strip() or "-"
                s = str(opt.get("size") or "-").strip() or "-"
                parsed_options.append({"color": c, "size": s, "stock": stk})

    return {
        "num": num,
        "obs_id": obs_id,
        "price": price,
        "live_price": live_price,
        "consumer_price": consumer_price,
        "has_options": has_options,
        "options": parsed_options,
    }


def upsert_product_from_obs(prod_data):
    """OBS에서 전달받은 단일 상품 데이터를 쇼핑몰 DB(mall_products, mall_product_options)에 저장/갱신합니다."""
    parsed = parse_obs_product_payload(prod_data)
    if parsed is None:
        return None
    num = parsed["num"]
    obs_id = parsed["obs_id"]
    name = str(prod_data.get("name") or f"상품 #{num}").strip()
    price = parsed["price"]
    live_price = parsed["live_price"]
    consumer_price = parsed["consumer_price"]
    image_url = str(prod_data.get("image_url") or "").strip()
    specs = str(prod_data.get("specs") or "").strip()
    incoming_cat = str(prod_data.get("category") or "").strip().upper()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    created_at = str(prod_data.get("created_at") or now_str)

    for attempt in range(2):
        need_retry = False
        restock_events: list[dict[str, Any]] = []
        saved_product = None
        with mall_db_lock:
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT id, category, detail_content, detail_images, shipping_info, exchange_return_info, non_returnable_info, as_info
                    FROM mall_products WHERE num = ?
                """, (num,))
                existing = cur.fetchone()
                prev_stocks: dict[tuple[str, str], int] = {}
                prev_total = 0
                if existing:
                    mall_pid = existing["id"]
                    if "options" in prod_data and isinstance(prod_data["options"], list):
                        prev_stocks, prev_total = _snapshot_product_option_stocks(cur, int(mall_pid))
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

                if parsed["has_options"]:
                    cur.execute("DELETE FROM mall_product_options WHERE product_id = ?", (mall_pid,))
                    for opt in (parsed["options"] or []):
                        cur.execute("""
                            INSERT OR REPLACE INTO mall_product_options (product_id, color, size, stock)
                            VALUES (?, ?, ?, ?)
                        """, (mall_pid, opt["color"], opt["size"], opt["stock"]))
                    if existing:
                        restock_events = _collect_restock_notifications_tx(
                            cur, int(mall_pid), num, name, prev_stocks, prev_total, now_str
                        )

                conn.commit()
                bump_state_version(bump_catalog=True)
                saved_product = get_mall_product_by_num(num)
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
        _dispatch_restock_notifications(restock_events)
        return saved_product
    return None


def _obs_bulk_sort_key(item):
    if not isinstance(item, dict):
        return 999999
    v = parse_strict_int64(item.get("num"), min_val=0)
    if v is None:
        v = parse_strict_int64(item.get("db_id"), min_val=0)
    return v if v is not None else 999999


def bulk_sync_products_from_obs(products_list, delete_missing=True):
    """OBS의 전체 상품 목록을 쇼핑몰 DB에 일괄 동기화합니다."""
    if not isinstance(products_list, list):
        return 0
    sorted_list = sorted(products_list, key=_obs_bulk_sort_key)
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
                p_num = parse_strict_int64(num, min_val=1)
                if p_num is None:
                    return False
                cur.execute("DELETE FROM mall_products WHERE num = ?", (p_num,))
            elif obs_product_id is not None:
                real_id = parse_strict_int64(str(obs_product_id).replace("item-", ""), min_val=1)
                if real_id is None:
                    return False
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
                parsed_order = parse_strict_int64(sort_order, min_val=0)
                if parsed_order is None:
                    return {"ok": False, "message": "정렬 순서는 0 이상의 올바른 정수로 입력해 주세요."}
                order_val = parsed_order

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
    parsed_cid = parse_strict_int64(cat_id, min_val=1)
    if parsed_cid is None:
        return {"ok": False, "message": "유효하지 않은 카테고리 ID입니다."}
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM mall_categories WHERE id = ?", (parsed_cid,))
            existing = cur.fetchone()
            if not existing:
                return {"ok": False, "message": "수정할 카테고리를 찾을 수 없습니다."}

            new_name = str(name).strip() if name is not None else str(existing["name"])
            if not new_name or len(new_name) > 40:
                return {"ok": False, "message": "카테고리명은 1~40자 이내로 입력해 주세요."}

            cur.execute("SELECT id FROM mall_categories WHERE name = ? AND id != ?", (new_name, parsed_cid))
            if cur.fetchone():
                return {"ok": False, "message": "이미 동일한 이름의 카테고리가 존재합니다."}

            if sort_order is None or str(sort_order).strip() == "":
                new_order = int(existing["sort_order"] or 0)
            else:
                parsed_order = parse_strict_int64(sort_order, min_val=0)
                if parsed_order is None:
                    return {"ok": False, "message": "정렬 순서는 0 이상의 올바른 정수로 입력해 주세요."}
                new_order = parsed_order
            new_active = int(existing["is_active"]) if is_active is None else (1 if is_active else 0)

            cur.execute("""
                UPDATE mall_categories
                SET name = ?, sort_order = ?, is_active = ?
                WHERE id = ?
            """, (new_name, new_order, new_active, parsed_cid))
            conn.commit()
            bump_state_version(bump_catalog=True)
            return {"ok": True}
        finally:
            conn.close()


def delete_mall_category(cat_id: int) -> dict:
    """카테고리를 삭제하고 해당 카테고리에 속해 있던 상품은 기타(ETC)로 이동합니다."""
    parsed_cid = parse_strict_int64(cat_id, min_val=1)
    if parsed_cid is None:
        return {"ok": False, "message": "유효하지 않은 카테고리 ID입니다."}
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, code, name FROM mall_categories WHERE id = ?", (parsed_cid,))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "message": "삭제할 카테고리를 찾을 수 없습니다."}

            old_code = str(row["code"] or "").strip().upper()
            cur.execute("UPDATE mall_products SET category = 'ETC' WHERE UPPER(COALESCE(category, '')) = ?", (old_code,))
            moved_cnt = int(cur.rowcount or 0)
            cur.execute("DELETE FROM mall_categories WHERE id = ?", (parsed_cid,))
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
    defaults = dict(DEFAULT_MALL_POLICIES)
    defaults["shipping_info"] = mall_shipping.format_shipping_policy_line(get_mall_store_info())
    return defaults


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


def _coerce_bounded_float(raw_val: Any, default_val: float, min_val: float = 0.0, max_val: float = 100.0):
    if isinstance(raw_val, bool) or raw_val is None:
        return default_val
    try:
        v = float(str(raw_val).strip().replace(",", ""))
    except (TypeError, ValueError):
        return default_val
    if math.isnan(v) or math.isinf(v) or v < min_val or v > max_val:
        return default_val
    return int(v) if v.is_integer() else round(v, 2)


def _normalize_store_info(raw: Any) -> dict:
    src = raw if isinstance(raw, dict) else {}
    info = {}
    for key, default_val in DEFAULT_STORE_INFO.items():
        if key in _STORE_INFO_INT_FIELDS or key in _STORE_INFO_FLOAT_FIELDS:
            continue
        max_len = _STORE_INFO_MAX_LEN.get(key, 200)
        val = str(src.get(key) or "").strip()
        if not val:
            val = str(default_val)
        info[key] = val[:max_len]
    for key, (default_int, max_int) in _STORE_INFO_INT_FIELDS.items():
        raw_val = src.get(key)
        if key in src and raw_val is not None and str(raw_val).strip() != "":
            info[key] = mall_shipping._coerce_non_negative_int(raw_val, default_int, max_val=max_int)
        else:
            info[key] = default_int
    for key, (default_flt, min_flt, max_flt) in _STORE_INFO_FLOAT_FIELDS.items():
        raw_val = src.get(key)
        if key in src and raw_val is not None and str(raw_val).strip() != "":
            info[key] = _coerce_bounded_float(raw_val, default_flt, min_val=min_flt, max_val=max_flt)
        else:
            info[key] = default_flt
    digits = re.sub(r"\D", "", info["biz_reg_no"])
    info["biz_reg_digits"] = digits
    if len(digits) == 10 and digits != "0000000000":
        info["ftc_biz_url"] = f"https://www.ftc.go.kr/bizCommPop.do?wrkr_no={digits}"
    else:
        info["ftc_biz_url"] = ""
    return info


def get_mall_store_info() -> dict:
    """쇼핑몰 하단 푸터와 법정 약관에 표시되는 사업자·고객센터·입금계좌·배송비 설정 정보를 반환합니다."""
    saved = get_live_state_key("store_info", None)
    return _normalize_store_info(saved)


def calculate_shipping_fee(subtotal: int, store_info: Optional[dict] = None) -> int:
    """스토어 설정을 기준으로 주문 배송비를 계산합니다 (Single Source of Truth 재수출)."""
    info = store_info if isinstance(store_info, dict) else get_mall_store_info()
    return mall_shipping.calculate_shipping_fee(subtotal, info)


def set_mall_store_info(info_data: Any) -> dict:
    """쇼핑몰 사업자·고객센터·입금계좌·배송비 설정 정보를 DB에 영구 저장합니다."""
    raw = info_data if isinstance(info_data, dict) else {}
    current = get_mall_store_info()
    merged = dict(current)
    for key in DEFAULT_STORE_INFO:
        if key in raw:
            if key in _STORE_INFO_INT_FIELDS or key in _STORE_INFO_FLOAT_FIELDS:
                if raw.get(key) is not None and str(raw.get(key)).strip() != "":
                    merged[key] = raw.get(key)
            else:
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

    raw_sell = prod_data.get("price")
    if raw_sell in (None, ""):
        sell_price = 0
    else:
        sell_price = parse_strict_int64(raw_sell, min_val=0)
        if sell_price is None:
            return {"ok": False, "message": "가격 및 원가는 올바른 정수로 입력해 주세요."}

    raw_live = prod_data.get("live_price")
    if raw_live in (None, "", 0, "0"):
        live_price = sell_price
    else:
        live_price = parse_strict_int64(raw_live, min_val=0)
        if live_price is None:
            return {"ok": False, "message": "가격 및 원가는 올바른 정수로 입력해 주세요."}
        if live_price <= 0:
            live_price = sell_price

    raw_cons = prod_data.get("consumer_price")
    if raw_cons in (None, "", 0, "0"):
        consumer_price = sell_price
    else:
        parsed_cons = parse_strict_int64(raw_cons, min_val=0)
        if parsed_cons is None:
            return {"ok": False, "message": "가격 및 원가는 올바른 정수로 입력해 주세요."}
        consumer_price = max(sell_price, parsed_cons)

    raw_cost = prod_data.get("cost_price")
    if raw_cost in (None, ""):
        cost_price = 0
    else:
        cost_price = parse_strict_int64(raw_cost, min_val=0)
        if cost_price is None:
            return {"ok": False, "message": "가격 및 원가는 올바른 정수로 입력해 주세요."}

    raw_sup_id = prod_data.get("supplier_id")
    if isinstance(raw_sup_id, (bool, float)):
        return {"ok": False, "message": "선택한 매입업체 ID가 올바르지 않습니다."}
    if raw_sup_id in (None, "", 0, "0"):
        supplier_id = None
    else:
        supplier_id = parse_strict_int64(raw_sup_id, min_val=1)
        if supplier_id is None:
            return {"ok": False, "message": "선택한 매입업체 ID가 올바르지 않습니다."}

    raw_opts = prod_data.get("options")
    clean_opts = []
    if isinstance(raw_opts, list):
        for o in raw_opts:
            if not isinstance(o, dict):
                continue
            c = str(o.get("color") or "-").strip() or "-"
            s = str(o.get("size") or "-").strip() or "-"
            raw_stk = o.get("stock")
            if raw_stk in (None, ""):
                stk = 0
            else:
                stk = parse_strict_int64(raw_stk, min_val=0)
                if stk is None:
                    return {"ok": False, "message": "옵션 재고는 0 이상의 올바른 정수로 입력해 주세요."}
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
            if supplier_id is not None:
                cur.execute("SELECT id FROM mall_suppliers WHERE id = ?", (supplier_id,))
                if not cur.fetchone():
                    return {"ok": False, "message": "선택한 매입업체를 찾을 수 없습니다."}

            raw_num = prod_data.get("num")
            if raw_num in (None, ""):
                cur.execute("SELECT COALESCE(MAX(num), 0) + 1 FROM mall_products")
                num = int(cur.fetchone()[0] or 1)
            else:
                num = parse_strict_int64(raw_num, min_val=1)
                if num is None:
                    return {"ok": False, "message": "상품 번호는 1 이상의 올바른 정수여야 합니다."}
                cur.execute("SELECT id FROM mall_products WHERE num = ?", (num,))
                if cur.fetchone():
                    return {"ok": False, "message": f"이미 #{num}번 상품이 등록되어 있습니다."}

            cur.execute("""
                INSERT INTO mall_products
                (obs_product_id, num, name, category, price, live_price, consumer_price, cost_price, supplier_id,
                 image_url, specs, detail_content, detail_images,
                 shipping_info, exchange_return_info, non_returnable_info, as_info,
                 is_featured, updated_at, created_at)
                VALUES (0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """, (
                num, name, category, sell_price, live_price, consumer_price, cost_price, supplier_id,
                image_url, specs,
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
    parsed_pid = parse_strict_int64(product_id, min_val=1)
    if parsed_pid is None:
        return {"ok": False, "message": "유효하지 않은 상품 ID입니다."}

    restock_events: list[dict[str, Any]] = []
    res_payload: dict[str, Any] = {}
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM mall_products WHERE id = ?", (parsed_pid,))
            existing = cur.fetchone()
            if not existing:
                return {"ok": False, "message": "수정할 상품을 찾을 수 없습니다."}

            raw_num = prod_data.get("num")
            if raw_num in (None, ""):
                target_num = int(existing["num"])
            else:
                target_num = parse_strict_int64(raw_num, min_val=1)
                if target_num is None:
                    return {"ok": False, "message": "상품 번호는 1 이상의 올바른 정수여야 합니다."}
            if target_num != int(existing["num"]):
                cur.execute("SELECT id FROM mall_products WHERE num = ? AND id != ?", (target_num, parsed_pid))
                if cur.fetchone():
                    return {"ok": False, "message": f"이미 #{target_num}번 상품이 등록되어 있습니다."}

            name = str(prod_data.get("name") if "name" in prod_data else existing["name"]).strip()
            if not name:
                return {"ok": False, "message": "상품명을 입력해 주세요."}

            if "price" in prod_data:
                sell_price = parse_strict_int64(prod_data.get("price"), min_val=0)
                if sell_price is None:
                    return {"ok": False, "message": "판매가는 0 이상의 올바른 정수로 입력해 주세요."}
            else:
                sell_price = int(existing["price"] or 0)

            if "live_price" in prod_data:
                raw_lp = prod_data.get("live_price")
                if raw_lp in (None, "", 0, "0"):
                    live_price = sell_price
                else:
                    live_price = parse_strict_int64(raw_lp, min_val=0)
                    if live_price is None:
                        return {"ok": False, "message": "라이브 가격은 0 이상의 올바른 정수로 입력해 주세요."}
                    if live_price <= 0:
                        live_price = sell_price
            else:
                live_price = int(existing["live_price"] or sell_price)
                if live_price <= 0:
                    live_price = sell_price

            if "consumer_price" in prod_data:
                raw_cp = prod_data.get("consumer_price")
                if raw_cp in (None, "", 0, "0"):
                    consumer_price = sell_price
                else:
                    parsed_cp = parse_strict_int64(raw_cp, min_val=0)
                    if parsed_cp is None:
                        return {"ok": False, "message": "소비자가는 0 이상의 올바른 정수로 입력해 주세요."}
                    consumer_price = max(sell_price, parsed_cp)
            else:
                consumer_price = max(sell_price, int(existing["consumer_price"] or sell_price))

            ext_keys = set(existing.keys())
            existing_cost = int(existing["cost_price"] or 0) if "cost_price" in ext_keys and existing["cost_price"] is not None else 0
            if "cost_price" in prod_data:
                raw_cost = prod_data.get("cost_price")
                if raw_cost in (None, ""):
                    cost_price = 0
                else:
                    cost_price = parse_strict_int64(raw_cost, min_val=0)
                    if cost_price is None:
                        return {"ok": False, "message": "매입원가는 0 이상의 올바른 정수로 입력해 주세요."}
            else:
                cost_price = existing_cost

            if "supplier_id" in prod_data:
                raw_sup = prod_data.get("supplier_id")
                if isinstance(raw_sup, (bool, float)):
                    return {"ok": False, "message": "선택한 매입업체 ID가 올바르지 않습니다."}
                if raw_sup in (None, "", 0, "0"):
                    supplier_id = None
                else:
                    supplier_id = parse_strict_int64(raw_sup, min_val=1)
                    if supplier_id is None:
                        return {"ok": False, "message": "선택한 매입업체 ID가 올바르지 않습니다."}
                if supplier_id is not None:
                    cur.execute("SELECT id FROM mall_suppliers WHERE id = ?", (supplier_id,))
                    if not cur.fetchone():
                        return {"ok": False, "message": "선택한 매입업체를 찾을 수 없습니다."}
            else:
                raw_existing_sup = existing["supplier_id"] if "supplier_id" in ext_keys else None
                supplier_id = int(raw_existing_sup) if raw_existing_sup is not None and int(raw_existing_sup) > 0 else None

            category = str(prod_data.get("category") if "category" in prod_data else existing["category"]).strip().upper() or "ETC"
            image_url = str(prod_data.get("image_url") if "image_url" in prod_data else existing["image_url"] or "").strip()
            specs = str(prod_data.get("specs") if "specs" in prod_data else existing["specs"] or "").strip()
            detail_imgs = _serialize_detail_images(prod_data["detail_images"]) if "detail_images" in prod_data else (existing["detail_images"] or "[]")
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
                    cost_price = ?, supplier_id = ?,
                    image_url = ?, specs = ?,
                    detail_content = ?, detail_images = ?,
                    shipping_info = ?, exchange_return_info = ?, non_returnable_info = ?, as_info = ?,
                    updated_at = ?
                WHERE id = ?
            """, (
                target_num, name, category, sell_price, live_price, consumer_price,
                cost_price, supplier_id,
                image_url, specs,
                resolved_texts["detail_content"], detail_imgs,
                resolved_texts["shipping_info"], resolved_texts["exchange_return_info"],
                resolved_texts["non_returnable_info"], resolved_texts["as_info"],
                now_str, parsed_pid
            ))

            if "options" in prod_data and isinstance(prod_data["options"], list):
                clean_opts = []
                for o in prod_data["options"]:
                    if not isinstance(o, dict):
                        continue
                    c = str(o.get("color") or "-").strip() or "-"
                    s = str(o.get("size") or "-").strip() or "-"
                    raw_stk = o.get("stock")
                    if raw_stk in (None, ""):
                        stk = 0
                    else:
                        stk = parse_strict_int64(raw_stk, min_val=0)
                        if stk is None:
                            conn.rollback()
                            return {"ok": False, "message": "옵션 재고는 0 이상의 올바른 정수로 입력해 주세요."}
                    clean_opts.append({"color": c, "size": s, "stock": stk})
                if not clean_opts:
                    conn.rollback()
                    return {"ok": False, "message": "최소 1개 이상의 옵션을 입력해 주세요."}
                prev_stocks, prev_total = _snapshot_product_option_stocks(cur, parsed_pid)
                cur.execute("DELETE FROM mall_product_options WHERE product_id = ?", (parsed_pid,))
                for opt in clean_opts:
                    cur.execute("""
                        INSERT OR REPLACE INTO mall_product_options (product_id, color, size, stock)
                        VALUES (?, ?, ?, ?)
                    """, (parsed_pid, opt["color"], opt["size"], opt["stock"]))
                restock_events = _collect_restock_notifications_tx(
                    cur, parsed_pid, target_num, name, prev_stocks, prev_total, now_str
                )

            conn.commit()
            bump_state_version(bump_catalog=True)
            res_payload = {"ok": True, "product": get_mall_product_by_num(target_num)}
        finally:
            conn.close()
    _dispatch_restock_notifications(restock_events)
    return res_payload


def delete_mall_product(product_id: int) -> dict:
    """쇼핑몰 관리자에서 상품을 삭제합니다."""
    parsed_pid = parse_strict_int64(product_id, min_val=1)
    if parsed_pid is None:
        return {"ok": False, "message": "유효하지 않은 상품 ID입니다."}
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, num, obs_product_id, name FROM mall_products WHERE id = ?", (parsed_pid,))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "message": "삭제할 상품을 찾을 수 없습니다."}
            cur.execute("DELETE FROM mall_products WHERE id = ?", (parsed_pid,))
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
            supplier_map = _get_supplier_name_map(conn)
            result = []
            for r in rows:
                cur.execute(
                    "SELECT color, size, stock FROM mall_product_options WHERE product_id = ? ORDER BY id ASC",
                    (r["id"],)
                )
                opt_rows = cur.fetchall()
                result.append(_format_product_row(r, opt_rows, supplier_map))
            return result
        finally:
            conn.close()


def _parse_optional_price_bound(raw_val: Any) -> Optional[int]:
    if raw_val is None or isinstance(raw_val, bool):
        return None
    s = str(raw_val).strip().replace(",", "")
    if not s:
        return None
    if re.fullmatch(r"-?[0-9]+", s):
        is_neg = s.startswith("-")
        digits = (s[1:] if is_neg else s).lstrip("0") or "0"
        if len(digits) > 19:
            return SQLITE_INT64_MIN if is_neg else SQLITE_INT64_MAX
        val = -int(digits) if is_neg else int(digits)
        if val < SQLITE_INT64_MIN:
            return SQLITE_INT64_MIN
        if val > SQLITE_INT64_MAX:
            return SQLITE_INT64_MAX
        return val
    return None


def _parse_int64_safe(raw_val: Any, allow_commas: bool = False) -> Optional[int]:
    """선행 0이 4300자 이상인 문자열을 안전하게 파싱하면서 유효 자릿수 19자리 초과 및 int64 범위 초과는 차단합니다."""
    if raw_val is None or isinstance(raw_val, bool):
        return None
    if isinstance(raw_val, int):
        return raw_val if SQLITE_INT64_MIN <= raw_val <= SQLITE_INT64_MAX else None
    if isinstance(raw_val, float):
        if not math.isfinite(raw_val) or not raw_val.is_integer():
            return None
        iv = int(raw_val)
        return iv if SQLITE_INT64_MIN <= iv <= SQLITE_INT64_MAX else None
    s = str(raw_val).strip()
    if allow_commas:
        s = s.replace(",", "")
    if not s or not re.fullmatch(r"-?[0-9]+", s):
        return None
    is_neg = s.startswith("-")
    digits = (s[1:] if is_neg else s).lstrip("0") or "0"
    if len(digits) > 19:
        return None
    val = -int(digits) if is_neg else int(digits)
    if val < SQLITE_INT64_MIN or val > SQLITE_INT64_MAX:
        return None
    return val


def _parse_clamped_query_int(raw_val: Any, default_val: int, min_val: int, max_val: int) -> int:
    if raw_val is None or isinstance(raw_val, bool):
        return default_val
    s = str(raw_val).strip()
    if not s or not re.fullmatch(r"-?[0-9]+", s):
        return default_val
    is_neg = s.startswith("-")
    digits = (s[1:] if is_neg else s).lstrip("0") or "0"
    if len(digits) > 19:
        return min_val if is_neg else max_val
    val = -int(digits) if is_neg else int(digits)
    if val <= 0:
        return default_val
    return max(min_val, min(val, max_val))


def query_mall_products_paginated(
    category=None,
    search=None,
    sort="num_asc",
    exclude_sold_out: Any = False,
    min_price: Any = None,
    max_price: Any = None,
    page: Any = 1,
    per_page: Any = 12,
) -> dict[str, Any]:
    """쇼핑몰 상품 목록을 필터(카테고리·검색·품절제외·가격대)·정렬·페이지네이션으로 조회합니다."""
    clean_cat = str(category or "").strip().upper()
    clean_search = str(search or "").strip()
    clean_sort = str(sort or "num_asc").strip()
    excl = (
        exclude_sold_out
        if isinstance(exclude_sold_out, bool)
        else str(exclude_sold_out or "").strip().lower() in ("1", "true", "yes", "on")
    )
    min_p = _parse_optional_price_bound(min_price)
    max_p = _parse_optional_price_bound(max_price)

    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            where_clauses: list[str] = []
            params: list[Any] = []

            if clean_cat and clean_cat != "ALL":
                where_clauses.append("UPPER(COALESCE(p.category, '')) = ?")
                params.append(clean_cat)

            if clean_search:
                q = f"%{clean_search}%"
                where_clauses.append("(p.name LIKE ? OR p.specs LIKE ? OR CAST(p.num AS TEXT) LIKE ?)")
                params.extend([q, q, q])

            if min_p is not None:
                where_clauses.append("p.price >= ?")
                params.append(min_p)

            if max_p is not None:
                where_clauses.append("p.price <= ?")
                params.append(max_p)

            where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
            having_sql = "HAVING COALESCE(SUM(o.stock), 0) > 0" if excl else ""

            if clean_sort == "price_asc":
                order_sql = "ORDER BY p.price ASC, p.num ASC, p.id DESC"
            elif clean_sort == "price_desc":
                order_sql = "ORDER BY p.price DESC, p.num ASC, p.id DESC"
            elif clean_sort == "newest":
                order_sql = "ORDER BY p.id DESC"
            else:
                order_sql = "ORDER BY p.num ASC, p.id DESC"

            cur.execute(
                f"""
                SELECT COUNT(*) FROM (
                    SELECT p.id
                    FROM mall_products p
                    LEFT JOIN mall_product_options o ON o.product_id = p.id
                    {where_sql}
                    GROUP BY p.id
                    {having_sql}
                ) sub
                """,
                params,
            )
            total_count = int(cur.fetchone()[0] or 0)

            limit_sql = ""
            exec_params = list(params)
            if per_page is None:
                eff_page = 1
                eff_per_page = max(1, total_count) if total_count > 0 else 12
                total_pages = 1
                has_prev = False
                has_next = False
                skip_rows = False
            else:
                eff_per_page = _parse_clamped_query_int(per_page, 12, 1, 100)
                eff_page = _parse_clamped_query_int(page, 1, 1, SQLITE_INT64_MAX)
                total_pages = max(1, math.ceil(total_count / eff_per_page)) if total_count > 0 else 1
                offset = (eff_page - 1) * eff_per_page
                skip_rows = (total_count == 0) or (eff_page > total_pages) or (offset > SQLITE_INT64_MAX)
                limit_sql = "LIMIT ? OFFSET ?"
                exec_params.extend([eff_per_page, min(offset, SQLITE_INT64_MAX)])
                has_prev = eff_page > 1 and total_count > 0
                has_next = eff_page < total_pages

            if skip_rows:
                rows = []
            else:
                cur.execute(
                    f"""
                    SELECT p.*
                    FROM mall_products p
                    LEFT JOIN mall_product_options o ON o.product_id = p.id
                    {where_sql}
                    GROUP BY p.id
                    {having_sql}
                    {order_sql}
                    {limit_sql}
                    """,
                    exec_params,
                )
                rows = cur.fetchall()
            supplier_map = _get_supplier_name_map(conn)
            result: list[dict] = []
            for r in rows:
                cur.execute(
                    "SELECT color, size, stock FROM mall_product_options WHERE product_id = ? ORDER BY id ASC",
                    (r["id"],),
                )
                opt_rows = cur.fetchall()
                result.append(_format_product_row(r, opt_rows, supplier_map))
            return {
                "products": result,
                "total_count": total_count,
                "page": eff_page,
                "per_page": eff_per_page,
                "total_pages": total_pages,
                "has_prev": has_prev,
                "has_next": has_next,
            }
        finally:
            conn.close()


def get_mall_product_by_num(num):
    """상품 번호(num)로 쇼핑몰 상품 상세 정보를 조회합니다."""
    p_num = parse_strict_int64(num)
    if p_num is None:
        return None
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM mall_products WHERE num = ?", (p_num,))
            r = cur.fetchone()
            if not r:
                return None
            supplier_map = _get_supplier_name_map(conn)
            cur.execute(
                "SELECT color, size, stock FROM mall_product_options WHERE product_id = ? ORDER BY id ASC",
                (r["id"],)
            )
            opt_rows = cur.fetchall()
            return _format_product_row(r, opt_rows, supplier_map)
        finally:
            conn.close()


def get_related_mall_products(product: Optional[dict], limit: int = 4) -> list[dict]:
    """
    현재 상품과 연관된 상품을 최대 limit개 반환합니다.
    우선순위:
    1) 동일 카테고리 + 재고 있음(판매중)
    2) 동일 카테고리 + 품절
    3) 타 카테고리 + 재고 있음(판매중)
    4) 타 카테고리 + 품절
    (현재 상품 num 제외, num ASC 정렬)
    """
    if not isinstance(product, dict):
        return []
    try:
        cur_num = int(product.get("num"))
    except Exception:
        return []
    cur_cat = str(product.get("category") or "").strip().upper()
    max_items = max(0, int(limit or 0))
    if max_items <= 0:
        return []

    candidates = [p for p in get_all_mall_products(sort="num_asc") if int(p.get("num") or 0) != cur_num]
    candidates.sort(
        key=lambda p: (
            0 if str(p.get("category") or "").strip().upper() == cur_cat else 1,
            0 if not p.get("is_sold_out") else 1,
            int(p.get("num") or 0),
        )
    )
    return candidates[:max_items]


def get_mall_product_by_id(product_id):
    """상품 번호(num) 최우선, 이후 OBS ID 또는 쇼핑몰 DB ID로 상품 상세 정보를 조회합니다."""
    real_id = parse_strict_int64(str(product_id or "").replace("item-", ""), min_val=1)
    if real_id is None:
        return None
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM mall_products WHERE num = ?", (real_id,))
            r = cur.fetchone()
            if not r:
                cur.execute("SELECT * FROM mall_products WHERE obs_product_id = ? OR id = ?", (real_id, real_id))
                r = cur.fetchone()
            if not r:
                return None
            supplier_map = _get_supplier_name_map(conn)
            cur.execute(
                "SELECT color, size, stock FROM mall_product_options WHERE product_id = ? ORDER BY id ASC",
                (r["id"],)
            )
            opt_rows = cur.fetchall()
            return _format_product_row(r, opt_rows, supplier_map)
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
            hb_ts = _safe_live_float(raw.get("last_heartbeat_ts"), default=0.0, min_val=0.0)
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


PUBLIC_INTERNAL_PRODUCT_KEYS = ("cost_price", "supplier_id", "supplier_name", "obs_product_id")


def sanitize_public_product_detail(product: Optional[dict]) -> Optional[dict]:
    """공개/라이브 뷰용 상품 dict에서 매입원가·매입업체·OBS 내부 ID를 제거합니다 (live_price/live_discount_rate 유지)."""
    if not isinstance(product, dict):
        return product
    sanitized = {k: v for k, v in product.items() if k not in PUBLIC_INTERNAL_PRODUCT_KEYS}
    if isinstance(sanitized.get("options"), list):
        sanitized["options"] = [
            dict(opt) if isinstance(opt, dict) else opt
            for opt in sanitized["options"]
        ]
    return sanitized


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

    public_all_products = [sanitize_public_product_detail(p) for p in all_products]
    public_selected_product = sanitize_public_product_detail(selected_product)

    return {
        "state_version": _safe_live_int(raw.get("state_version"), default=1, min_val=1),
        "catalog_version": _safe_live_int(raw.get("catalog_version"), default=1, min_val=1),
        "is_live": bool(raw.get("is_live", False)),
        "broadcast_title": str(raw.get("broadcast_title") or "실시간 라이브 커머스 특가 방송"),
        "ticker_message": str(raw.get("ticker_message") or "라이브 방송 중 선택된 상품을 페이지 이동 없이 바로 확인하고 특가로 구매하세요!"),
        "pinned_product_num": pinned_num,
        "is_pinned_by_obs": is_pinned_by_obs,
        "selected_product": public_selected_product,
        "selected_product_reviews": sel_reviews,
        "selected_product_review_summary": sel_review_summary,
        "all_products": public_all_products,
        "timer": raw.get("timer") if isinstance(raw.get("timer"), dict) else {"active": False},
        "youtube_video_id": str(raw.get("youtube_video_id") or ""),
        "youtube_channel_input": str(raw.get("youtube_channel_input") or ""),
        "youtube_embed_url": _derive_youtube_embed_url(raw),
        "live_urls": raw.get("live_urls") if isinstance(raw.get("live_urls"), dict) else {},
        "last_heartbeat_ts": _safe_live_float(raw.get("last_heartbeat_ts"), default=0.0, min_val=0.0),
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
        "state_version": _safe_live_int(raw.get("state_version"), default=1, min_val=1),
        "catalog_version": _safe_live_int(raw.get("catalog_version"), default=1, min_val=1),
        "is_live": bool(raw.get("is_live", False)),
        "broadcast_title": str(raw.get("broadcast_title") or "실시간 라이브 커머스 특가 방송"),
        "ticker_message": str(raw.get("ticker_message") or "라이브 방송 중 선택된 상품을 페이지 이동 없이 바로 확인하고 특가로 구매하세요!"),
        "pinned_product_num": pinned_num,
        "is_pinned_by_obs": is_pinned_by_obs,
        "selected_product": sanitize_public_product_detail(selected_product),
        "product_stocks": product_stocks,
        "timer": raw.get("timer") if isinstance(raw.get("timer"), dict) else {"active": False},
        "youtube_video_id": str(raw.get("youtube_video_id") or ""),
        "youtube_embed_url": _derive_youtube_embed_url(raw),
        "live_urls": raw.get("live_urls") if isinstance(raw.get("live_urls"), dict) else {},
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


def _normalize_zipcode(val: Any) -> str:
    return re.sub(r"\s+", "", str(val or "").strip())[:16]


def _normalize_payment_method_value(raw_method: Any) -> Optional[str]:
    val = str(raw_method or "BANK").strip().upper()
    if not val:
        return "BANK"
    alias = {
        "BANK": "BANK",
        "BANK_TRANSFER": "BANK",
        "PORTONE": "PORTONE_V2",
        "PORTONE_V2": "PORTONE_V2",
        "TOSS": "TOSS_PAYMENTS",
        "TOSS_PAYMENTS": "TOSS_PAYMENTS",
        "CARD": "CARD",
    }
    return alias.get(val)


def _format_order_row(row, items: list[dict]) -> dict:
    d = dict(row)
    d.pop("guest_password_hash", None)
    d["zipcode"] = _normalize_zipcode(d.get("zipcode"))
    d["address"] = str(d.get("address") or "")
    d["address_detail"] = str(d.get("address_detail") or "")
    d["delivery_memo"] = str(d.get("delivery_memo") or "")
    pm = _normalize_payment_method_value(d.get("payment_method")) or str(d.get("payment_method") or "BANK").strip().upper() or "BANK"
    d["payment_method"] = pm
    d["depositor_name"] = str(d.get("depositor_name") or "").strip()
    crt = str(d.get("cash_receipt_type") or "NONE").strip().upper()
    d["cash_receipt_type"] = crt if crt in ("NONE", "INCOME", "EXPENSE") else "NONE"
    d["cash_receipt_no"] = str(d.get("cash_receipt_no") or "").strip() if d["cash_receipt_type"] != "NONE" else ""
    d["pg_provider"] = str(d.get("pg_provider") or "").strip().upper()
    d["pg_tid"] = str(d.get("pg_tid") or "").strip()
    d["pg_status"] = str(d.get("pg_status") or "").strip().upper()
    d["paid_at"] = str(d.get("paid_at") or "").strip()
    raw_cc = str(d.get("carrier_code") or "").strip()
    raw_cn = str(d.get("carrier_name") or "").strip()
    if raw_cc or raw_cn:
        res_cc, res_cn = mall_shipping.resolve_carrier(raw_cc, raw_cn)
        d["carrier_code"] = res_cc or raw_cc
        d["carrier_name"] = res_cn or raw_cn
    else:
        d["carrier_code"] = ""
        d["carrier_name"] = ""
    d["tracking_number"] = str(d.get("tracking_number") or "").strip()
    d["shipped_at"] = str(d.get("shipped_at") or "").strip()
    d["tracking_url"] = (
        mall_shipping.get_carrier_tracking_url(d["carrier_code"] or d["carrier_name"], d["tracking_number"])
        if d["tracking_number"]
        else ""
    )
    if d.get("shipping_fee") is None:
        try:
            tot = int(d.get("total_amount") or 0)
            sub = int(d.get("subtotal_amount") or 0)
            d["shipping_fee"] = max(0, tot - sub) if tot >= sub else 0
        except Exception:
            d["shipping_fee"] = 0
    else:
        try:
            d["shipping_fee"] = max(0, int(d.get("shipping_fee") or 0))
        except Exception:
            d["shipping_fee"] = 0
    d["coupon_code"] = str(d.get("coupon_code") or "").strip().upper()
    d["coupon_discount"] = max(0, int(d.get("coupon_discount") or 0))
    d["points_used"] = max(0, int(d.get("points_used") or 0))
    d["points_earned"] = max(0, int(d.get("points_earned") or 0))
    d["points_awarded"] = int(d.get("points_awarded") or 0)
    d["points_refunded"] = int(d.get("points_refunded") or 0)
    d["coupon_restored"] = int(d.get("coupon_restored") or 0)
    d["points_revoked"] = int(d.get("points_revoked") or 0)
    d["items"] = items
    d["order_items"] = items
    return d


def create_mall_order(order_data):
    """
    쇼핑몰 주문 생성 및 옵션 재고 차감 트랜잭션 처리
    - items: [{"num": 1, "color": "블랙", "size": "L", "quantity": 1}, ...]
    """
    buyer_name = str(order_data.get("buyer_name") or "").strip()
    buyer_phone = str(order_data.get("buyer_phone") or "").strip()
    zipcode = _normalize_zipcode(order_data.get("zipcode"))
    address = str(order_data.get("address") or "").strip()
    address_detail = str(order_data.get("address_detail") or "").strip()
    delivery_memo = str(order_data.get("delivery_memo") or "").strip()
    payment_method = _normalize_payment_method_value(order_data.get("payment_method"))
    if not payment_method:
        return {"ok": False, "message": "지원하지 않는 결제 수단입니다."}
    depositor_name = str(order_data.get("depositor_name") or "").strip()[:60]
    raw_cr_type = str(order_data.get("cash_receipt_type") or "NONE").strip().upper() or "NONE"
    if raw_cr_type not in ("NONE", "INCOME", "EXPENSE"):
        return {"ok": False, "message": "올바른 현금영수증 유형을 선택해 주세요."}
    raw_cr_no = str(order_data.get("cash_receipt_no") or "").strip()
    if raw_cr_type in ("INCOME", "EXPENSE"):
        digits_only = "".join(ch for ch in raw_cr_no if ch.isdigit())
        if not raw_cr_no or not re.fullmatch(r"[0-9\-]{6,32}", raw_cr_no) or len(digits_only) < 6:
            return {"ok": False, "message": "현금영수증 발급번호를 올바르게 입력해 주세요. (숫자 6자리 이상)"}
        cash_receipt_no = raw_cr_no[:32]
    else:
        cash_receipt_no = ""
    cash_receipt_type = raw_cr_type
    pg_provider = "" if payment_method == "BANK" else ("TOSS_PAYMENTS" if payment_method == "TOSS_PAYMENTS" else "PORTONE_V2")
    pg_tid = ""
    pg_status = "" if payment_method == "BANK" else "READY"
    paid_at = ""
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
            supplier_map = _get_supplier_name_map(conn)
            resolved_items = []
            subtotal = 0

            # 라이브 커머스 전용관(/live) 주문일 때만 라이브 특가 적용 (일반 쇼핑몰은 항상 일반 판매가 적용)
            use_live_pricing = (source_page == "LIVE")

            for idx, it in enumerate(items_req):
                if not isinstance(it, dict):
                    continue
                raw_num = it.get("num") if it.get("num") is not None else it.get("product_num")
                raw_num_s = "" if (raw_num is None or isinstance(raw_num, bool)) else str(raw_num).strip()
                if not raw_num_s or not re.fullmatch(r"-?[0-9]+", raw_num_s):
                    conn.rollback()
                    return {"ok": False, "http_status": 400, "message": "장바구니에 유효하지 않은 상품 번호가 포함되어 있습니다."}
                is_neg_num = raw_num_s.startswith("-")
                num_digits = (raw_num_s[1:] if is_neg_num else raw_num_s).lstrip("0") or "0"
                if len(num_digits) > 19:
                    conn.rollback()
                    return {"ok": False, "http_status": 400, "message": "장바구니 상품 번호가 허용 범위를 초과했습니다."}
                p_num = -int(num_digits) if is_neg_num else int(num_digits)
                if p_num < SQLITE_INT64_MIN or p_num > SQLITE_INT64_MAX:
                    conn.rollback()
                    return {"ok": False, "http_status": 400, "message": "장바구니 상품 번호가 허용 범위를 초과했습니다."}
                color = str(it.get("color") or "-").strip() or "-"
                size = str(it.get("size") or "-").strip() or "-"
                if "quantity" not in it or it.get("quantity") is None:
                    qty = 1
                else:
                    qty = parse_strict_int64(it.get("quantity"), min_val=1)
                    if qty is None:
                        conn.rollback()
                        return {"ok": False, "http_status": 400, "message": "주문 수량은 1개 이상의 올바른 정수로 입력해 주세요."}

                cur.execute("SELECT * FROM mall_products WHERE num = ?", (p_num,))
                p_row = cur.fetchone()
                if not p_row:
                    conn.rollback()
                    return {
                        "ok": False,
                        "code": "OUT_OF_STOCK",
                        "http_status": 400,
                        "message": f"상품 #{p_num} 정보를 찾을 수 없습니다.",
                    }

                mall_pid = p_row["id"]
                cur.execute("""
                    SELECT id, stock FROM mall_product_options
                    WHERE product_id = ? AND color = ? AND size = ?
                """, (mall_pid, color, size))
                opt_row = cur.fetchone()
                if opt_row:
                    cur_stk = max(0, int(opt_row["stock"] or 0))
                    if cur_stk < qty:
                        conn.rollback()
                        return {
                            "ok": False,
                            "code": "OUT_OF_STOCK" if cur_stk <= 0 else "INSUFFICIENT_STOCK",
                            "http_status": 400,
                            "message": f"[{p_row['name']} ({color}/{size})] 재고가 부족합니다. (잔여: {cur_stk}개)"
                        }
                    new_stk = cur_stk - qty
                    cur.execute("UPDATE mall_product_options SET stock = ? WHERE id = ?", (new_stk, opt_row["id"]))
                else:
                    cur.execute(
                        "SELECT id, color, size, stock FROM mall_product_options WHERE product_id = ? ORDER BY id ASC",
                        (mall_pid,),
                    )
                    all_opts = cur.fetchall()
                    if len(all_opts) == 1 and color in ("-", "") and size in ("-", ""):
                        any_opt = all_opts[0]
                        color = str(any_opt["color"] or "-").strip() or "-"
                        size = str(any_opt["size"] or "-").strip() or "-"
                        cur_stk = max(0, int(any_opt["stock"] or 0))
                        if cur_stk < qty:
                            conn.rollback()
                            return {
                                "ok": False,
                                "code": "OUT_OF_STOCK" if cur_stk <= 0 else "INSUFFICIENT_STOCK",
                                "http_status": 400,
                                "message": f"[{p_row['name']}] 재고가 부족합니다. (잔여: {cur_stk}개)"
                            }
                        cur.execute(
                            "UPDATE mall_product_options SET stock = ? WHERE id = ?",
                            (cur_stk - qty, any_opt["id"])
                        )
                    else:
                        conn.rollback()
                        return {
                            "ok": False,
                            "code": "OUT_OF_STOCK",
                            "http_status": 400,
                            "message": f"[{p_row['name']} ({color}/{size})] 유효한 상품 옵션을 찾을 수 없습니다."
                        }

                p_keys = set(p_row.keys())
                sell_price = int(p_row["price"] or 0)
                live_price = int(p_row["live_price"] or 0) if "live_price" in p_keys and p_row["live_price"] is not None and int(p_row["live_price"]) > 0 else sell_price
                unit_price = live_price if use_live_pricing else sell_price

                raw_item_exp_price = it.get("expected_price")
                if raw_item_exp_price is not None and str(raw_item_exp_price).strip() != "":
                    exp_p_val = _parse_int64_safe(raw_item_exp_price, allow_commas=True)
                    if exp_p_val is None:
                        conn.rollback()
                        return {"ok": False, "code": "PRICE_CHANGED", "http_status": 409, "message": "상품 가격 정보가 변경되었습니다. 다시 확인해 주세요."}
                    if exp_p_val != unit_price:
                        conn.rollback()
                        return {
                            "ok": False,
                            "code": "PRICE_CHANGED",
                            "http_status": 409,
                            "message": f"[{p_row['name']}] 상품 가격이 변경되었습니다. (기존 {exp_p_val:,}원 → 현재 {unit_price:,}원)"
                        }

                line_total = unit_price * qty
                unit_cost = max(0, int(p_row["cost_price"] or 0)) if "cost_price" in p_keys and p_row["cost_price"] is not None else 0
                cost_total = unit_cost * qty
                raw_sup = p_row["supplier_id"] if "supplier_id" in p_keys else None
                try:
                    sup_id = int(raw_sup) if raw_sup is not None and int(raw_sup) > 0 else None
                except Exception:
                    sup_id = None
                sup_name = supplier_map.get(sup_id, "") if sup_id else ""
                subtotal += line_total
                resolved_items.append({
                    "product_num": p_num,
                    "product_name": p_row["name"],
                    "image_url": p_row["image_url"] or "",
                    "color": color,
                    "size": size,
                    "quantity": qty,
                    "unit_price": unit_price,
                    "line_total": line_total,
                    "unit_cost": unit_cost,
                    "cost_total": cost_total,
                    "supplier_id": sup_id,
                    "supplier_name": sup_name,
                })

            if not resolved_items:
                conn.rollback()
                return {"ok": False, "http_status": 400, "message": "주문할 상품이 없습니다."}

            if "expected_items" in order_data and order_data.get("expected_items") is not None:
                exp_items = order_data.get("expected_items")
                if not isinstance(exp_items, list) or len(exp_items) != len(resolved_items):
                    conn.rollback()
                    return {
                        "ok": False,
                        "code": "PRICE_CHANGED",
                        "http_status": 409,
                        "message": "주문 상품 구성이 변경되었습니다. 장바구니를 다시 확인해 주세요.",
                    }
                for idx_exp, exp_it in enumerate(exp_items):
                    if not isinstance(exp_it, dict):
                        conn.rollback()
                        return {
                            "ok": False,
                            "code": "PRICE_CHANGED",
                            "http_status": 409,
                            "message": "상품 가격 또는 구성이 변경되었습니다. 장바구니를 다시 확인해 주세요.",
                        }
                    res_it = resolved_items[idx_exp]
                    exp_raw_num = exp_it.get("num") if exp_it.get("num") is not None else exp_it.get("product_num")
                    exp_num = _parse_int64_safe(exp_raw_num)
                    raw_exp_qty = exp_it.get("quantity")
                    if raw_exp_qty is None or str(raw_exp_qty).strip() == "":
                        exp_qty = 1
                    else:
                        parsed_exp_qty = _parse_int64_safe(raw_exp_qty)
                        exp_qty = max(1, parsed_exp_qty) if parsed_exp_qty is not None else None
                    if exp_num is None or exp_qty is None:
                        conn.rollback()
                        return {
                            "ok": False,
                            "code": "PRICE_CHANGED",
                            "http_status": 409,
                            "message": "상품 가격 또는 구성이 변경되었습니다. 장바구니를 다시 확인해 주세요.",
                        }
                    exp_c = str(exp_it.get("color") or "-").strip() or "-"
                    exp_s = str(exp_it.get("size") or "-").strip() or "-"
                    if exp_num != res_it["product_num"] or exp_qty != res_it["quantity"] or (
                        exp_c not in ("-", "") and exp_c != res_it["color"]
                    ) or (exp_s not in ("-", "") and exp_s != res_it["size"]):
                        conn.rollback()
                        return {
                            "ok": False,
                            "code": "PRICE_CHANGED",
                            "http_status": 409,
                            "message": "상품 옵션 또는 수량이 변경되었습니다. 장바구니를 다시 확인해 주세요.",
                        }
                    raw_ep = None
                    for pk in ("unit_price", "expected_price", "client_price", "price"):
                        if exp_it.get(pk) is not None and str(exp_it.get(pk)).strip() != "":
                            raw_ep = exp_it.get(pk)
                            break
                    if raw_ep is not None:
                        ep_val = _parse_int64_safe(raw_ep, allow_commas=True)
                        if ep_val is None:
                            conn.rollback()
                            return {
                                "ok": False,
                                "code": "PRICE_CHANGED",
                                "http_status": 409,
                                "message": "상품 가격 정보가 올바르지 않습니다.",
                            }
                        if ep_val != res_it["unit_price"]:
                            conn.rollback()
                            return {
                                "ok": False,
                                "code": "PRICE_CHANGED",
                                "http_status": 409,
                                "message": f"[{res_it['product_name']}] 상품 가격이 변경되었습니다. ({ep_val:,}원 → {res_it['unit_price']:,}원)",
                            }

            store_info = get_mall_store_info()
            shipping_fee = calculate_shipping_fee(subtotal, store_info=store_info)
            pay_status = "입금대기"
            ord_status = "결제대기"

            user_id = parse_strict_int64(order_data.get("user_id"), min_val=1)
            is_guest = 1 if (not user_id or order_data.get("is_guest")) else 0
            guest_pw_raw = str(order_data.get("guest_password") or "").strip()
            guest_pw_hash = ""
            if is_guest and guest_pw_raw:
                guest_pw_hash = mall_security.hash_password(guest_pw_raw)

            import mall_promotions
            promo_ok, promo_res = mall_promotions.prepare_order_promotions_tx(
                conn,
                user_id=user_id,
                subtotal=subtotal,
                source_page=source_page,
                coupon_code=order_data.get("coupon_code"),
                points_used_raw=order_data.get("points_used"),
                point_earn_rate=store_info.get("point_earn_rate", 1),
            )
            if not promo_ok:
                conn.rollback()
                return {"ok": False, "message": str(promo_res)}
            assert isinstance(promo_res, dict)
            coupon_code_used = str(promo_res["coupon_code"])
            coupon_discount = int(promo_res["coupon_discount"])
            points_used = int(promo_res["points_used"])
            points_earned = int(promo_res["points_earned"])
            total_amount = max(0, subtotal - coupon_discount - points_used) + shipping_fee

            if "expected_total" in order_data and order_data.get("expected_total") not in (None, ""):
                ok_et, parsed_et, et_err = mall_promotions.parse_strict_non_neg_int(
                    order_data.get("expected_total"),
                    "결제 예정 금액",
                )
                if not ok_et or parsed_et is None:
                    conn.rollback()
                    return {"ok": False, "http_status": 400, "message": et_err or "유효하지 않은 결제 예정 금액입니다."}
                if parsed_et != total_amount:
                    conn.rollback()
                    return {
                        "ok": False,
                        "code": "PRICE_CHANGED",
                        "http_status": 409,
                        "message": f"결제 예정 금액이 변경되었습니다. ({parsed_et:,}원 → {total_amount:,}원)",
                    }

            cur.execute("""
                INSERT INTO mall_orders
                (order_code, buyer_name, buyer_phone, zipcode, address, address_detail, delivery_memo,
                 payment_method, depositor_name, cash_receipt_type, cash_receipt_no,
                 pg_provider, pg_tid, pg_status, paid_at,
                 payment_status, order_status, subtotal_amount, shipping_fee, total_amount,
                 coupon_code, coupon_discount, points_used, points_earned,
                 points_awarded, points_refunded, coupon_restored, points_revoked,
                 source_page, user_id, is_guest, guest_password_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?, ?, ?, ?)
            """, (
                order_code, buyer_name, buyer_phone, zipcode, address, address_detail, delivery_memo,
                payment_method, depositor_name, cash_receipt_type, cash_receipt_no,
                pg_provider, pg_tid, pg_status, paid_at,
                pay_status, ord_status, subtotal, shipping_fee, total_amount,
                coupon_code_used, coupon_discount, points_used, points_earned,
                source_page, user_id, is_guest, guest_pw_hash, now_str
            ))
            order_id = cur.lastrowid

            for item in resolved_items:
                cur.execute("""
                    INSERT INTO mall_order_items
                    (order_id, product_num, product_name, image_url, color, size, quantity,
                     unit_price, line_total, unit_cost, cost_total, supplier_id, supplier_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    order_id,
                    item["product_num"],
                    item["product_name"],
                    item["image_url"],
                    item["color"],
                    item["size"],
                    item["quantity"],
                    item["unit_price"],
                    item["line_total"],
                    item["unit_cost"],
                    item["cost_total"],
                    item["supplier_id"],
                    item["supplier_name"],
                ))

            commit_ok, commit_err = mall_promotions.commit_order_promotions_tx(
                conn,
                user_id=user_id,
                order_id=int(order_id),
                order_code=order_code,
                promo=promo_res,
                now_str=now_str,
            )
            if not commit_ok:
                conn.rollback()
                return {"ok": False, "message": commit_err}

            conn.commit()
            bump_state_version(bump_catalog=True)
            return {
                "ok": True,
                "order_id": order_id,
                "order_code": order_code,
                "buyer_name": buyer_name,
                "buyer_phone": buyer_phone,
                "zipcode": zipcode,
                "address": address,
                "address_detail": address_detail,
                "delivery_memo": delivery_memo,
                "total_amount": total_amount,
                "subtotal_amount": subtotal,
                "shipping_fee": shipping_fee,
                "coupon_code": coupon_code_used,
                "coupon_discount": coupon_discount,
                "points_used": points_used,
                "points_earned": points_earned,
                "points_awarded": 0,
                "points_refunded": 0,
                "coupon_restored": 0,
                "points_revoked": 0,
                "payment_method": payment_method,
                "depositor_name": depositor_name,
                "cash_receipt_type": cash_receipt_type,
                "cash_receipt_no": cash_receipt_no,
                "pg_provider": pg_provider,
                "pg_tid": pg_tid,
                "pg_status": pg_status,
                "paid_at": paid_at,
                "payment_status": pay_status,
                "order_status": ord_status,
                "is_guest": bool(is_guest),
                "user_id": user_id,
                "carrier_code": "",
                "carrier_name": "",
                "tracking_number": "",
                "shipped_at": "",
                "tracking_url": "",
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
                orders.append(_format_order_row(r, items))
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
                orders.append(_format_order_row(r, items))
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
                orders.append(_format_order_row(r, items))
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
            return [_format_order_row(row, items)]
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
            return [_format_order_row(row, items)]
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
    conn=None,
):
    """
    쇼핑몰 주문 상태/입금 상태/환불·교환 사유를 갱신하고,
    '주문취소' 또는 '환불완료' 전환 시 아직 복구되지 않은 품목 재고를 1회 자동 복구합니다.
    """
    oid = parse_strict_int64(order_id, min_val=1)
    if oid is None:
        return {"ok": False, "http_status": 400, "message": "유효하지 않은 주문 ID입니다."}

    clean_pay = str(payment_status).strip() if payment_status is not None else ""
    clean_ord = str(order_status).strip() if order_status is not None else ""
    if clean_pay and clean_pay not in VALID_PAYMENT_STATUSES:
        return {"ok": False, "http_status": 400, "message": f"허용되지 않는 입금 상태입니다: {clean_pay}"}
    if clean_ord and clean_ord not in VALID_ORDER_STATUSES:
        return {"ok": False, "http_status": 400, "message": f"허용되지 않는 주문 상태입니다: {clean_ord}"}

    owns_conn = conn is None
    restock_events: list[dict[str, Any]] = []
    res_payload: dict[str, Any] = {}
    with mall_db_lock:
        active_conn = conn if conn is not None else get_connection()
        try:
            cur = active_conn.cursor()
            cur.execute("SELECT * FROM mall_orders WHERE id = ? LIMIT 1", (oid,))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "http_status": 404, "message": "주문을 찾을 수 없습니다."}

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

            if cur_ord in ("주문취소", "환불완료"):
                same_state = (next_ord == cur_ord and next_pay == cur_pay)
                paid_refund_transition = (
                    cur_ord == "주문취소"
                    and cur_pay == "입금완료"
                    and next_ord == "환불완료"
                    and next_pay == "환불완료"
                )
                if not same_state and not paid_refund_transition:
                    return {
                        "ok": False,
                        "http_status": 400,
                        "message": f"이미 [{cur_ord}] 처리된 주문은 [{next_ord}] 상태로 변경할 수 없습니다.",
                    }

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
            affected_products: dict[int, tuple[int, str, dict[tuple[str, str], int], int]] = {}

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
                    cur.execute("SELECT id, name FROM mall_products WHERE num = ? LIMIT 1", (p_num,))
                    p_row = cur.fetchone()
                    if p_row:
                        mall_pid = int(p_row["id"])
                        if mall_pid not in affected_products:
                            ps, pt = _snapshot_product_option_stocks(cur, mall_pid)
                            affected_products[mall_pid] = (p_num, str(p_row["name"] or ""), ps, pt)
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
                for mall_pid, (p_num, p_name, ps, pt) in affected_products.items():
                    restock_events.extend(
                        _collect_restock_notifications_tx(cur, mall_pid, p_num, p_name, ps, pt, now_str)
                    )

            cur_shipped_at = str(row["shipped_at"] or "").strip() if "shipped_at" in row.keys() else ""
            next_shipped_at = cur_shipped_at or now_str if next_ord == "배송중" else cur_shipped_at
            cur_paid_at = str(row["paid_at"] or "").strip() if "paid_at" in row.keys() else ""
            next_paid_at = cur_paid_at or now_str if next_pay == "입금완료" else cur_paid_at
            became_paid = (cur_pay != "입금완료" and next_pay == "입금완료")
            became_shipped = (cur_ord != "배송중" and next_ord == "배송중")

            cur.execute(
                """
                UPDATE mall_orders
                SET payment_status = ?,
                    order_status = ?,
                    claim_reason = ?,
                    claim_updated_at = ?,
                    stock_restored = ?,
                    shipped_at = ?,
                    paid_at = ?
                WHERE id = ?
                """,
                (next_pay, next_ord, next_reason, next_claim_ts, next_restored, next_shipped_at, next_paid_at, oid),
            )
            import mall_promotions
            if next_ord == "배송완료":
                mall_promotions.award_order_points_on_delivery(active_conn, oid, now_str)
            if next_ord in STOCK_RESTORE_ORDER_STATUSES:
                mall_promotions.restore_order_promotions_on_cancel(active_conn, oid, now_str)
            active_conn.commit()
            bump_state_version(bump_catalog=True)

            cur.execute("SELECT * FROM mall_orders WHERE id = ? LIMIT 1", (oid,))
            updated_row = cur.fetchone()
            cur.execute("SELECT * FROM mall_order_items WHERE order_id = ? ORDER BY id ASC", (oid,))
            items = [dict(ir) for ir in cur.fetchall()]
            order_dict = _format_order_row(updated_row, items) if updated_row else {}

            res_payload = {
                "ok": True,
                "order": order_dict,
                "restored_items": restored_items,
                "stock_restored_now": bool(already_restored == 0 and next_restored == 1),
                "became_paid": became_paid,
                "became_shipped": became_shipped,
            }
        finally:
            if owns_conn:
                active_conn.close()
    if owns_conn:
        _dispatch_restock_notifications(restock_events)
    return res_payload


def update_mall_order_status(order_id, payment_status=None, order_status=None):
    res = update_mall_order_workflow(
        order_id=order_id,
        payment_status=payment_status,
        order_status=order_status,
    )
    return bool(res.get("ok"))


def confirm_mall_order_pg_payment(
    order_code: str,
    pg_provider: str,
    pg_tid: str,
    pg_status: str = "PAID",
    paid_at: str = "",
) -> dict:
    """PG 결제 승인/웹훅 검증 완료 시 주문 입금상태·PG 거래 정보를 원자적으로 갱신합니다."""
    code = str(order_code or "").strip()
    if not code:
        return {"ok": False, "not_found": True, "message": "주문번호가 필요합니다."}
    clean_provider = str(pg_provider or "").strip().upper()[:40]
    clean_tid = str(pg_tid or "").strip()[:120]
    clean_pg_status = str(pg_status or "PAID").strip().upper()[:40] or "PAID"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM mall_orders WHERE order_code = ? LIMIT 1", (code,))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "not_found": True, "message": "주문을 찾을 수 없습니다."}
            oid = int(row["id"])
            cur_pay = str(row["payment_status"] or "입금대기")
            cur_ord = str(row["order_status"] or "결제대기")
            cur_method = str(row["payment_method"] or "BANK").strip().upper() or "BANK"
            if cur_method == "BANK":
                return {"ok": False, "message": "무통장 입금 주문은 PG 결제 확정을 사용할 수 없습니다."}
            if cur_pay in ("환불대기", "환불완료"):
                return {"ok": False, "message": f"결제 상태 [{cur_pay}]인 주문은 결제 확정할 수 없습니다."}
            if cur_ord in ("주문취소", "환불요청", "환불완료", "교환요청", "교환완료"):
                return {"ok": False, "message": f"[{cur_ord}] 상태의 주문은 결제 확정할 수 없습니다."}
            if cur_pay not in ("입금대기", "입금완료"):
                return {"ok": False, "message": f"결제 상태 [{cur_pay}]인 주문은 결제 확정할 수 없습니다."}
            if cur_pay == "입금대기" and cur_ord not in ("결제대기", "상품준비중"):
                return {"ok": False, "message": f"[{cur_ord}] 상태의 미결제 주문은 결제 확정할 수 없습니다."}

            existing_tid = str(row["pg_tid"] or "").strip() if "pg_tid" in row.keys() else ""
            if cur_pay == "입금완료" and existing_tid and clean_tid and existing_tid != clean_tid:
                return {"ok": False, "message": "이미 다른 거래번호로 결제 완료된 주문입니다."}

            next_pay = "입금완료"
            next_ord = "상품준비중" if cur_ord == "결제대기" else cur_ord
            cur_paid_at = str(row["paid_at"] or "").strip() if "paid_at" in row.keys() else ""
            next_paid_at = str(paid_at or "").strip() or cur_paid_at or now_str
            next_tid = clean_tid or existing_tid
            became_paid = (cur_pay != "입금완료")

            cur.execute(
                """
                UPDATE mall_orders
                SET payment_status = ?,
                    order_status = ?,
                    pg_provider = ?,
                    pg_tid = ?,
                    pg_status = ?,
                    paid_at = ?
                WHERE id = ?
                """,
                (next_pay, next_ord, clean_provider, next_tid, clean_pg_status, next_paid_at, oid),
            )
            conn.commit()
            bump_state_version(bump_catalog=False)

            cur.execute("SELECT * FROM mall_orders WHERE id = ? LIMIT 1", (oid,))
            updated_row = cur.fetchone()
            cur.execute("SELECT * FROM mall_order_items WHERE order_id = ? ORDER BY id ASC", (oid,))
            items = [dict(ir) for ir in cur.fetchall()]
            return {
                "ok": True,
                "order": _format_order_row(updated_row, items) if updated_row else {},
                "became_paid": became_paid,
            }
        finally:
            conn.close()


def _fetch_order_by_id_or_code(cur, order_id=None, order_code=None):
    oid = parse_strict_int64(order_id, min_val=1)
    if oid is not None:
        cur.execute("SELECT * FROM mall_orders WHERE id = ? LIMIT 1", (oid,))
        row = cur.fetchone()
        if row:
            return row
    code = str(order_code or "").strip()
    if code:
        cur.execute("SELECT * FROM mall_orders WHERE order_code = ? LIMIT 1", (code,))
        return cur.fetchone()
    return None


def get_mall_order_raw_by_code_or_id(order_id=None, order_code=None) -> Optional[dict]:
    """주문 ID 또는 주문번호로 단건 주문(+소유권 메타데이터)을 조회합니다."""
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            row = _fetch_order_by_id_or_code(cur, order_id=order_id, order_code=order_code)
            if not row:
                return None
            cur.execute("SELECT * FROM mall_order_items WHERE order_id = ? ORDER BY id ASC", (row["id"],))
            items = [dict(ir) for ir in cur.fetchall()]
            return _format_order_row(row, items)
        finally:
            conn.close()


def update_mall_order_tracking(
    order_id=None,
    order_code=None,
    carrier_code: str = "",
    carrier_name: str = "",
    tracking_number: str = "",
    mark_shipped: bool = False,
) -> dict:
    """주문의 택배사·운송장 번호를 저장하고 선택적으로 배송중으로 전환합니다."""
    raw_tracking = str(tracking_number or "").strip()
    clean_tracking = mall_shipping.sanitize_tracking_number(raw_tracking) if raw_tracking else ""
    if raw_tracking and not clean_tracking:
        return {"ok": False, "message": "운송장 번호는 영문, 숫자, 하이픈(-)만 입력할 수 있습니다."}

    resolved_code, resolved_name = mall_shipping.resolve_carrier(carrier_code, carrier_name)
    if (str(carrier_code or "").strip() or str(carrier_name or "").strip()) and not resolved_code:
        return {"ok": False, "message": f"지원하지 않는 택배사입니다: {carrier_code or carrier_name}"}
    if clean_tracking and not resolved_code:
        return {"ok": False, "message": "택배사를 선택해 주세요."}
    if mark_shipped and not clean_tracking and resolved_code != "DIRECT":
        return {"ok": False, "message": "배송중 전환을 위해 운송장 번호를 입력해 주세요."}

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            row = _fetch_order_by_id_or_code(cur, order_id=order_id, order_code=order_code)
            if not row:
                return {"ok": False, "not_found": True, "message": "주문을 찾을 수 없습니다."}

            oid = int(row["id"])
            cur_ord = str(row["order_status"] or "결제대기")
            if cur_ord in ("주문취소", "환불완료"):
                return {"ok": False, "message": f"[{cur_ord}] 상태의 주문에는 운송장을 등록할 수 없습니다."}

            next_ord = cur_ord
            cur_shipped_at = str(row["shipped_at"] or "").strip() if "shipped_at" in row.keys() else ""
            next_shipped_at = cur_shipped_at
            if mark_shipped and cur_ord in ("결제대기", "상품준비중"):
                next_ord = "배송중"
                next_shipped_at = cur_shipped_at or now_str
            elif cur_ord == "배송중" and clean_tracking and not cur_shipped_at:
                next_shipped_at = now_str

            cur.execute(
                """
                UPDATE mall_orders
                SET carrier_code = ?,
                    carrier_name = ?,
                    tracking_number = ?,
                    shipped_at = ?,
                    order_status = ?
                WHERE id = ?
                """,
                (resolved_code, resolved_name, clean_tracking, next_shipped_at, next_ord, oid),
            )
            conn.commit()
            bump_state_version(bump_catalog=False)

            cur.execute("SELECT * FROM mall_orders WHERE id = ? LIMIT 1", (oid,))
            updated_row = cur.fetchone()
            cur.execute("SELECT * FROM mall_order_items WHERE order_id = ? ORDER BY id ASC", (oid,))
            items = [dict(ir) for ir in cur.fetchall()]
            return {
                "ok": True,
                "order": _format_order_row(updated_row, items),
                "became_shipped": (cur_ord != "배송중" and next_ord == "배송중"),
            }
        finally:
            conn.close()


def bulk_update_mall_order_tracking(rows: list[dict], default_mark_shipped: bool = True) -> dict:
    """CSV/배치 송장 데이터를 행별로 검증·반영하며 오류 행이 있어도 정상 행 처리를 중단하지 않습니다."""
    updated_orders: list[dict] = []
    errors: list[dict] = []
    total_rows = len(rows or [])
    for idx, entry in enumerate(rows or [], start=1):
        if not isinstance(entry, dict):
            errors.append({"row": idx, "order_code": "", "error": "유효하지 않은 행 데이터입니다."})
            continue
        row_no = entry.get("row_num") or idx
        ord_code = str(entry.get("order_code") or "").strip()
        ord_id = entry.get("order_id")
        if not ord_code and not ord_id:
            errors.append({"row": row_no, "order_code": "", "error": "주문번호(order_code)가 비어 있습니다."})
            continue
        tracking_no = str(entry.get("tracking_number") or "").strip()
        if not tracking_no:
            errors.append({"row": row_no, "order_code": ord_code, "error": "운송장 번호(tracking_number)가 비어 있습니다."})
            continue
        raw_mark = entry.get("mark_shipped")
        mark_shipped = default_mark_shipped if raw_mark is None else bool(raw_mark)
        res = update_mall_order_tracking(
            order_id=ord_id,
            order_code=ord_code,
            carrier_code=str(entry.get("carrier_code") or ""),
            carrier_name=str(entry.get("carrier_name") or ""),
            tracking_number=tracking_no,
            mark_shipped=mark_shipped,
        )
        if res.get("ok") and res.get("order"):
            updated_orders.append(res["order"])
        else:
            errors.append({
                "row": row_no,
                "order_code": ord_code,
                "error": str(res.get("message") or "운송장 반영 실패"),
            })
    return {
        "ok": True,
        "total_rows": total_rows,
        "updated_count": len(updated_orders),
        "error_count": len(errors),
        "updated_orders": updated_orders,
        "errors": errors,
    }


def get_mall_orders_for_shipping_export(order_status: str = "", limit: int = 500) -> list[dict]:
    """택배사 발송용 주문서 CSV 내보내기를 위한 주문 목록을 조회합니다."""
    clean_status = str(order_status or "").strip()
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            if clean_status and clean_status.upper() != "ALL":
                cur.execute(
                    "SELECT * FROM mall_orders WHERE order_status = ? ORDER BY id DESC LIMIT ?",
                    (clean_status, int(limit)),
                )
            else:
                cur.execute(
                    "SELECT * FROM mall_orders WHERE COALESCE(order_status, '') NOT IN ('주문취소', '환불완료') ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                )
            ord_rows = cur.fetchall()
            orders = []
            for r in ord_rows:
                cur.execute("SELECT * FROM mall_order_items WHERE order_id = ? ORDER BY id ASC", (r["id"],))
                items = [dict(ir) for ir in cur.fetchall()]
                orders.append(_format_order_row(r, items))
            return orders
        finally:
            conn.close()


def cancel_mall_order_by_customer(order_id=None, order_code=None, claim_reason: str = "") -> dict:
    """
    고객 셀프 주문 즉시 취소:
    - 반드시 mall_db_lock 내부에서 payment_status == '입금대기' 및 order_status in ('결제대기', '상품준비중') 검증
    - '주문취소' 전환 및 미복구 재고 1회 원자적 복구
    """
    clean_reason = str(claim_reason or "").strip()[:500] or "고객 직접 주문 취소"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    restock_events: list[dict[str, Any]] = []
    res_payload: dict[str, Any] = {}
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            row = _fetch_order_by_id_or_code(cur, order_id=order_id, order_code=order_code)
            if not row:
                return {"ok": False, "not_found": True, "message": "주문을 찾을 수 없습니다."}

            oid = int(row["id"])
            cur_pay = str(row["payment_status"] or "입금대기")
            cur_ord = str(row["order_status"] or "결제대기")
            if cur_pay != "입금대기" or cur_ord not in ("결제대기", "상품준비중"):
                return {
                    "ok": False,
                    "message": "입금대기 상태의 결제대기·상품준비중 주문만 즉시 취소할 수 있습니다.",
                }

            already_restored = int(row["stock_restored"] or 0) if "stock_restored" in row.keys() else 0
            next_restored = already_restored
            restored_items: list[dict] = []
            affected_products: dict[int, tuple[int, str, dict[tuple[str, str], int], int]] = {}
            if already_restored == 0:
                cur.execute(
                    "SELECT product_num, color, size, quantity FROM mall_order_items WHERE order_id = ? ORDER BY id ASC",
                    (oid,),
                )
                for it in cur.fetchall():
                    p_num = int(it["product_num"])
                    color = str(it["color"] or "-")
                    size = str(it["size"] or "-")
                    qty = max(0, int(it["quantity"] or 0))
                    if qty <= 0:
                        continue
                    cur.execute("SELECT id, name FROM mall_products WHERE num = ? LIMIT 1", (p_num,))
                    p_row = cur.fetchone()
                    if p_row:
                        mall_pid = int(p_row["id"])
                        if mall_pid not in affected_products:
                            ps, pt = _snapshot_product_option_stocks(cur, mall_pid)
                            affected_products[mall_pid] = (p_num, str(p_row["name"] or ""), ps, pt)
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
                for mall_pid, (p_num, p_name, ps, pt) in affected_products.items():
                    restock_events.extend(
                        _collect_restock_notifications_tx(cur, mall_pid, p_num, p_name, ps, pt, now_str)
                    )

            cur.execute(
                """
                UPDATE mall_orders
                SET order_status = '주문취소',
                    claim_reason = ?,
                    claim_updated_at = ?,
                    stock_restored = ?
                WHERE id = ?
                """,
                (clean_reason, now_str, next_restored, oid),
            )
            import mall_promotions
            mall_promotions.restore_order_promotions_on_cancel(conn, oid, now_str)
            conn.commit()
            bump_state_version(bump_catalog=True)

            cur.execute("SELECT * FROM mall_orders WHERE id = ? LIMIT 1", (oid,))
            updated_row = cur.fetchone()
            cur.execute("SELECT * FROM mall_order_items WHERE order_id = ? ORDER BY id ASC", (oid,))
            items = [dict(ir) for ir in cur.fetchall()]
            res_payload = {
                "ok": True,
                "order": _format_order_row(updated_row, items),
                "restored_items": restored_items,
                "stock_restored_now": bool(already_restored == 0 and next_restored == 1),
            }
        finally:
            conn.close()
    _dispatch_restock_notifications(restock_events)
    return res_payload


def submit_mall_order_claim_by_customer(
    order_id=None,
    order_code=None,
    claim_type: str = "",
    claim_reason: str = "",
) -> dict:
    """고객 교환/반품(환불요청·교환요청) 접수 및 관리자 클레임 워크플로우 연동."""
    c_type = str(claim_type or "").strip()
    c_reason = str(claim_reason or "").strip()
    if c_type not in ("환불요청", "교환요청"):
        return {"ok": False, "message": "신청 유형은 '환불요청' 또는 '교환요청'만 선택할 수 있습니다."}
    if not c_reason:
        return {"ok": False, "message": "교환 또는 반품 신청 사유를 입력해 주세요."}
    if len(c_reason) > 500:
        return {"ok": False, "message": "신청 사유는 500자 이내로 입력해 주세요."}

    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            row = _fetch_order_by_id_or_code(cur, order_id=order_id, order_code=order_code)
            if not row:
                return {"ok": False, "not_found": True, "message": "주문을 찾을 수 없습니다."}
            oid = int(row["id"])
            cur_pay = str(row["payment_status"] or "입금대기")
            cur_ord = str(row["order_status"] or "결제대기")

            if cur_ord in ("주문취소", "환불요청", "환불완료", "교환요청", "교환완료"):
                return {"ok": False, "message": f"현재 [{cur_ord}] 상태에서는 교환/반품을 신청할 수 없습니다."}
            if cur_pay == "입금대기" and cur_ord in ("결제대기", "상품준비중"):
                return {"ok": False, "message": "입금대기 상태의 주문은 [주문 취소]를 이용해 주세요."}

            return update_mall_order_workflow(
                order_id=oid,
                order_status=c_type,
                claim_reason=c_reason,
                conn=conn,
            )
        finally:
            conn.close()


_REVIEW_IMG_URL_RE = re.compile(
    r"^/static/uploads/reviews/[A-Za-z0-9][A-Za-z0-9._-]*\.(?:png|jpg|jpeg|webp|gif)$",
    re.IGNORECASE,
)


def parse_review_images(raw_images: Any = None, raw_image_url: Any = None) -> tuple[bool, list[str], str]:
    candidates: list[str] = []
    if raw_image_url is not None and raw_image_url != "":
        if not isinstance(raw_image_url, str):
            return False, [], "유효하지 않은 리뷰 이미지 경로입니다."
        candidates.append(raw_image_url)

    if raw_images is not None:
        if not isinstance(raw_images, list):
            return False, [], "리뷰 이미지 목록은 배열 형식이어야 합니다."
        if len(raw_images) > 5:
            return False, [], "리뷰 이미지는 최대 5장까지 첨부할 수 있습니다."
        for item in raw_images:
            if not isinstance(item, str):
                return False, [], "유효하지 않은 리뷰 이미지 경로입니다."
            candidates.append(item)

    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        u = raw.strip()
        if not u:
            continue
        if ".." in u or "\\" in u or "\x00" in u or not _REVIEW_IMG_URL_RE.match(u):
            return False, [], "유효하지 않은 리뷰 이미지 경로입니다."
        if u not in seen:
            seen.add(u)
            cleaned.append(u)
        if len(cleaned) > 5:
            return False, [], "리뷰 이미지는 최대 5장까지 첨부할 수 있습니다."
    return True, cleaned, ""


def is_verified_buyer_for_product(user_id: Any, product_num: Any, conn=None) -> bool:
    uid = parse_strict_int64(user_id, min_val=1) or 0
    pnum = parse_strict_int64(product_num, min_val=1) or 0
    if uid <= 0 or pnum <= 0:
        return False

    sql = """
        SELECT 1
        FROM mall_orders o
        INNER JOIN mall_order_items oi ON oi.order_id = o.id
        WHERE o.user_id = ?
          AND oi.product_num = ?
          AND COALESCE(o.order_status, '') NOT IN ('주문취소', '환불완료')
          AND COALESCE(o.payment_status, '') != '환불완료'
        LIMIT 1
    """
    if conn is not None:
        row = conn.execute(sql, (uid, pnum)).fetchone()
        return row is not None

    with mall_db_lock:
        c = get_connection()
        try:
            row = c.execute(sql, (uid, pnum)).fetchone()
            return row is not None
        finally:
            c.close()


def _format_review_row(row, viewer: Optional[dict] = None, verified_override: Optional[bool] = None) -> dict:
    d = dict(row)
    raw_imgs = d.get("images")
    parsed_imgs: list[str] = []
    if isinstance(raw_imgs, list):
        parsed_imgs = [str(x).strip() for x in raw_imgs if str(x).strip()]
    elif isinstance(raw_imgs, str) and raw_imgs.strip():
        try:
            loaded = json.loads(raw_imgs)
            if isinstance(loaded, list):
                parsed_imgs = [str(x).strip() for x in loaded if str(x).strip()]
        except Exception:
            parsed_imgs = []

    single_url = str(d.get("image_url") or "").strip()
    if single_url and single_url not in parsed_imgs:
        parsed_imgs.insert(0, single_url)
    parsed_imgs = [u for u in parsed_imgs if _REVIEW_IMG_URL_RE.match(u)][:5]
    main_url = parsed_imgs[0] if parsed_imgs else ""

    if verified_override is not None:
        is_verified = bool(verified_override)
    else:
        is_verified = bool(int(d.get("is_verified_buyer") or 0))

    viewer_id = int(viewer["id"]) if viewer and viewer.get("id") else 0
    is_admin = bool(viewer and str(viewer.get("role") or "").upper() == "ADMIN")
    row_uid = int(d["user_id"]) if d.get("user_id") is not None else None
    is_owner = bool(viewer_id and row_uid is not None and row_uid == viewer_id)

    out = {
        "id": int(d["id"]),
        "product_num": int(d["product_num"]),
        "user_id": row_uid,
        "author": str(d.get("author") or "구매회원"),
        "rating": max(1, min(5, int(d.get("rating") or 5))),
        "content": str(d.get("content") or ""),
        "images": parsed_imgs,
        "image_url": main_url,
        "is_verified_buyer": is_verified,
        "is_hidden": bool(int(d.get("is_hidden") or 0)),
        "admin_reply": str(d.get("admin_reply") or ""),
        "admin_replied_at": str(d.get("admin_replied_at") or ""),
        "created_at": str(d.get("created_at") or ""),
        "updated_at": str(d.get("updated_at") or ""),
        "is_owner": is_owner,
        "can_edit": bool(is_owner or is_admin),
        "can_delete": bool(is_owner or is_admin),
    }
    if "product_name" in d:
        out["product_name"] = str(d.get("product_name") or "")
    return out


def get_mall_reviews(product_num, limit=50, viewer=None, include_hidden=False):
    pnum = parse_strict_int64(product_num, min_val=1)
    if pnum is None:
        return []
    eff_limit = _parse_clamped_query_int(limit, 50, 1, 500)
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            hidden_clause = "" if include_hidden else "AND COALESCE(r.is_hidden, 0) = 0"
            cur.execute(
                f"""
                SELECT
                    r.*,
                    EXISTS (
                        SELECT 1
                        FROM mall_orders o
                        INNER JOIN mall_order_items oi ON oi.order_id = o.id
                        WHERE o.user_id = r.user_id
                          AND oi.product_num = r.product_num
                          AND COALESCE(o.order_status, '') NOT IN ('주문취소', '환불완료')
                          AND COALESCE(o.payment_status, '') != '환불완료'
                    ) AS live_verified_buyer
                FROM mall_reviews r
                WHERE r.product_num = ? {hidden_clause}
                ORDER BY r.id DESC
                LIMIT ?
                """,
                (pnum, eff_limit),
            )
            return [
                _format_review_row(r, viewer=viewer, verified_override=bool(r["live_verified_buyer"]))
                for r in cur.fetchall()
            ]
        finally:
            conn.close()


def get_mall_review_summary(product_num):
    pnum = parse_strict_int64(product_num, min_val=1)
    if pnum is None:
        return {"count": 0, "avg_rating": 0.0}
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COUNT(*) AS cnt, COALESCE(AVG(rating), 0) AS avg_rating
                FROM mall_reviews
                WHERE product_num = ? AND COALESCE(is_hidden, 0) = 0
                """,
                (pnum,),
            )
            row = cur.fetchone()
            cnt = int(row["cnt"] or 0) if row else 0
            avg = round(float(row["avg_rating"] or 0.0), 1) if cnt > 0 else 0.0
            return {"count": cnt, "avg_rating": avg}
        finally:
            conn.close()


def get_mall_review_by_id(review_id, viewer=None) -> Optional[dict]:
    rid = parse_strict_int64(review_id, min_val=1)
    if rid is None:
        return None
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT
                    r.*,
                    EXISTS (
                        SELECT 1
                        FROM mall_orders o
                        INNER JOIN mall_order_items oi ON oi.order_id = o.id
                        WHERE o.user_id = r.user_id
                          AND oi.product_num = r.product_num
                          AND COALESCE(o.order_status, '') NOT IN ('주문취소', '환불완료')
                          AND COALESCE(o.payment_status, '') != '환불완료'
                    ) AS live_verified_buyer
                FROM mall_reviews r
                WHERE r.id = ?
                LIMIT 1
                """,
                (rid,),
            )
            row = cur.fetchone()
            return _format_review_row(row, viewer=viewer, verified_override=bool(row["live_verified_buyer"])) if row else None
        finally:
            conn.close()


def add_mall_review(product_num, author, rating, content, user_id=None, images=None, image_url=""):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ok_imgs, imgs_list, _ = parse_review_images(images, image_url)
    if not ok_imgs:
        imgs_list = []
    main_img = imgs_list[0] if imgs_list else ""
    uid = parse_strict_int64(user_id, min_val=1)
    pnum = parse_strict_int64(product_num, min_val=1)
    if pnum is None:
        return {"ok": False, "message": "유효하지 않은 상품 번호입니다."}
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            is_verified = 1 if is_verified_buyer_for_product(uid, pnum, conn=conn) else 0
            cur.execute(
                """
                INSERT INTO mall_reviews
                (product_num, user_id, author, rating, content, images, image_url,
                 is_verified_buyer, is_hidden, admin_reply, admin_replied_at, updated_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, '', '', ?, ?)
                """,
                (
                    pnum,
                    uid,
                    str(author or "구매회원").strip(),
                    max(1, min(5, int(rating or 5))),
                    str(content or "").strip(),
                    json.dumps(imgs_list, ensure_ascii=False),
                    main_img,
                    is_verified,
                    now_str,
                    now_str,
                ),
            )
            rid = int(cur.lastrowid)
            conn.commit()
            cur.execute("SELECT * FROM mall_reviews WHERE id = ? LIMIT 1", (rid,))
            row = cur.fetchone()
            viewer = {"id": uid, "role": "MEMBER"} if uid else None
            formatted = _format_review_row(row, viewer=viewer, verified_override=bool(is_verified)) if row else {"id": rid}
        finally:
            conn.close()
    bump_state_version(bump_catalog=False)
    return formatted


def update_mall_review(review_id, rating=None, content=None, images=None, image_url=None) -> dict:
    rid = parse_strict_int64(review_id, min_val=1)
    if rid is None:
        return {"ok": False, "message": "유효하지 않은 리뷰 ID입니다."}
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM mall_reviews WHERE id = ? LIMIT 1", (rid,))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "not_found": True, "message": "리뷰를 찾을 수 없습니다."}

            next_rating = max(1, min(5, int(rating))) if rating is not None else int(row["rating"] or 5)
            next_content = str(content).strip() if content is not None else str(row["content"] or "")
            if images is not None or image_url is not None:
                ok_imgs, imgs_list, img_err = parse_review_images(images, image_url)
                if not ok_imgs:
                    return {"ok": False, "message": img_err}
            else:
                existing = _format_review_row(row)
                imgs_list = existing["images"]
            main_img = imgs_list[0] if imgs_list else ""

            cur.execute(
                """
                UPDATE mall_reviews
                SET rating = ?, content = ?, images = ?, image_url = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_rating, next_content, json.dumps(imgs_list, ensure_ascii=False), main_img, now_str, rid),
            )
            conn.commit()
            cur.execute(
                """
                SELECT r.*, EXISTS (
                    SELECT 1 FROM mall_orders o
                    INNER JOIN mall_order_items oi ON oi.order_id = o.id
                    WHERE o.user_id = r.user_id AND oi.product_num = r.product_num
                      AND COALESCE(o.order_status, '') NOT IN ('주문취소', '환불완료')
                      AND COALESCE(o.payment_status, '') != '환불완료'
                ) AS live_verified_buyer
                FROM mall_reviews r WHERE r.id = ? LIMIT 1
                """,
                (rid,),
            )
            upd = cur.fetchone()
            viewer = {"id": int(upd["user_id"]), "role": "MEMBER"} if upd and upd["user_id"] else None
            review_obj = _format_review_row(upd, viewer=viewer, verified_override=bool(upd["live_verified_buyer"])) if upd else None
        finally:
            conn.close()
    bump_state_version(bump_catalog=False)
    return {"ok": True, "review": review_obj}


def delete_mall_review(review_id) -> dict:
    rid = parse_strict_int64(review_id, min_val=1)
    if rid is None:
        return {"ok": False, "message": "유효하지 않은 리뷰 ID입니다."}
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, product_num FROM mall_reviews WHERE id = ? LIMIT 1", (rid,))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "not_found": True, "message": "리뷰를 찾을 수 없습니다."}
            pnum = int(row["product_num"])
            cur.execute("DELETE FROM mall_reviews WHERE id = ?", (rid,))
            conn.commit()
        finally:
            conn.close()
    bump_state_version(bump_catalog=False)
    return {"ok": True, "deleted_id": rid, "product_num": pnum}


def list_admin_reviews(product_num=None, visibility: str = "ALL", query: str = "", limit: int = 200) -> list[dict]:
    vis = str(visibility or "ALL").strip().upper()
    q = str(query or "").strip().lower()
    where_parts: list[str] = []
    params: list[Any] = []
    if product_num is not None and str(product_num).strip() != "":
        pnum_filter = parse_strict_int64(product_num, min_val=1)
        if pnum_filter is None:
            return []
        where_parts.append("r.product_num = ?")
        params.append(pnum_filter)
    if vis == "HIDDEN":
        where_parts.append("COALESCE(r.is_hidden, 0) = 1")
    elif vis == "VISIBLE":
        where_parts.append("COALESCE(r.is_hidden, 0) = 0")
    if q:
        like_q = f"%{q}%"
        where_parts.append(
            "(LOWER(r.author) LIKE ? OR LOWER(r.content) LIKE ? OR LOWER(COALESCE(p.name, '')) LIKE ? OR CAST(r.product_num AS TEXT) LIKE ?)"
        )
        params.extend([like_q, like_q, like_q, like_q])
    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    admin_viewer = {"id": 0, "role": "ADMIN"}

    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT
                    r.*,
                    COALESCE(p.name, '') AS product_name,
                    EXISTS (
                        SELECT 1
                        FROM mall_orders o
                        INNER JOIN mall_order_items oi ON oi.order_id = o.id
                        WHERE o.user_id = r.user_id
                          AND oi.product_num = r.product_num
                          AND COALESCE(o.order_status, '') NOT IN ('주문취소', '환불완료')
                          AND COALESCE(o.payment_status, '') != '환불완료'
                    ) AS live_verified_buyer
                FROM mall_reviews r
                LEFT JOIN mall_products p ON p.num = r.product_num
                {where_sql}
                ORDER BY r.id DESC
                LIMIT ?
                """,
                (*params, int(limit)),
            )
            return [
                _format_review_row(r, viewer=admin_viewer, verified_override=bool(r["live_verified_buyer"]))
                for r in cur.fetchall()
            ]
        finally:
            conn.close()


def moderate_mall_review(
    review_id,
    action: str = "",
    is_hidden=None,
    admin_reply=None,
    is_deleted=None,
) -> dict:
    rid = parse_strict_int64(review_id, min_val=1)
    if rid is None:
        return {"ok": False, "message": "유효하지 않은 리뷰 ID입니다."}

    act = str(action or "").strip().lower()
    if act == "delete" or bool(is_deleted):
        res = delete_mall_review(rid)
        if res.get("ok"):
            res["deleted"] = True
        return res

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    admin_viewer = {"id": 0, "role": "ADMIN"}
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM mall_reviews WHERE id = ? LIMIT 1", (rid,))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "not_found": True, "message": "리뷰를 찾을 수 없습니다."}

            next_hidden = int(row["is_hidden"] or 0)
            if act == "hide":
                next_hidden = 1
            elif act == "unhide":
                next_hidden = 0
            elif is_hidden is not None:
                if isinstance(is_hidden, bool):
                    next_hidden = 1 if is_hidden else 0
                elif isinstance(is_hidden, (int, float)):
                    next_hidden = 1 if int(is_hidden) != 0 else 0
                else:
                    next_hidden = 1 if str(is_hidden).strip().lower() in ("1", "true", "yes", "y", "on") else 0

            next_reply = str(row["admin_reply"] or "")
            next_replied_at = str(row["admin_replied_at"] or "")
            if admin_reply is not None or act == "reply":
                reply_txt = str(admin_reply or "").strip()[:1000]
                next_reply = reply_txt
                next_replied_at = now_str if reply_txt else ""

            cur.execute(
                """
                UPDATE mall_reviews
                SET is_hidden = ?, admin_reply = ?, admin_replied_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_hidden, next_reply, next_replied_at, now_str, rid),
            )
            conn.commit()
            cur.execute(
                """
                SELECT r.*, COALESCE(p.name, '') AS product_name,
                    EXISTS (
                        SELECT 1 FROM mall_orders o
                        INNER JOIN mall_order_items oi ON oi.order_id = o.id
                        WHERE o.user_id = r.user_id AND oi.product_num = r.product_num
                          AND COALESCE(o.order_status, '') NOT IN ('주문취소', '환불완료')
                          AND COALESCE(o.payment_status, '') != '환불완료'
                    ) AS live_verified_buyer
                FROM mall_reviews r
                LEFT JOIN mall_products p ON p.num = r.product_num
                WHERE r.id = ? LIMIT 1
                """,
                (rid,),
            )
            upd = cur.fetchone()
            review_obj = _format_review_row(upd, viewer=admin_viewer, verified_override=bool(upd["live_verified_buyer"])) if upd else None
        finally:
            conn.close()
    bump_state_version(bump_catalog=False)
    return {"ok": True, "review": review_obj}


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
            norm_zip = _normalize_zipcode(zipcode)
            norm_addr = str(address or "").strip()
            norm_detail = str(address_detail or "").strip()
            cur.execute("""
                INSERT INTO mall_users (email, password_hash, name, phone, zipcode, address, address_detail, role, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'MEMBER', 1, ?)
            """, (
                mail, pw_hash, nm, ph,
                norm_zip,
                norm_addr,
                norm_detail,
                now_str
            ))
            uid = cur.lastrowid
            if norm_addr:
                cur.execute("""
                    INSERT INTO mall_user_addresses
                    (user_id, label, recipient_name, recipient_phone, zipcode, address, address_detail, is_default, created_at, updated_at)
                    VALUES (?, '기본 배송지', ?, ?, ?, ?, ?, 1, ?, ?)
                """, (uid, nm, ph, norm_zip, norm_addr, norm_detail, now_str, now_str))
            import mall_promotions
            mall_promotions.issue_welcome_coupons_to_user(conn, int(uid), now_str)
            conn.commit()
            return {
                "ok": True,
                "user": {
                    "id": uid,
                    "email": mail,
                    "name": nm,
                    "phone": ph,
                    "zipcode": norm_zip,
                    "address": norm_addr,
                    "address_detail": norm_detail,
                    "role": "MEMBER",
                    "points_balance": 0,
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
                SELECT id, email, password_hash, name, phone, zipcode, address, address_detail, role, is_active, withdrawn_at, failed_login_count, locked_until
                FROM mall_users
                WHERE email = ?
            """, (mail,))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "code": "INVALID", "message": "이메일 또는 비밀번호가 올바르지 않습니다."}
            if str(row["withdrawn_at"] or "").strip():
                return {"ok": False, "code": "WITHDRAWN", "message": "탈퇴 처리된 계정입니다. 로그인할 수 없습니다."}
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


def _format_user_row(row) -> Optional[dict]:
    if not row:
        return None
    d = dict(row)
    d.pop("password_hash", None)
    d["zipcode"] = _normalize_zipcode(d.get("zipcode"))
    d["address"] = str(d.get("address") or "")
    d["address_detail"] = str(d.get("address_detail") or "")
    d["withdrawn_at"] = str(d.get("withdrawn_at") or "")
    d["points_balance"] = max(0, int(d.get("points_balance") or 0))
    return d


def get_mall_user_by_id(user_id: int):
    if not user_id:
        return None
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, email, name, phone, zipcode, address, address_detail, role, is_active, withdrawn_at, points_balance, last_login_at, created_at
                FROM mall_users
                WHERE id = ? AND is_active = 1 AND COALESCE(withdrawn_at, '') = ''
            """, (int(user_id),))
            row = cur.fetchone()
            return _format_user_row(row)
        finally:
            conn.close()


def get_primary_mall_admin_user():
    """쇼핑몰 활성 관리자(ADMIN) 대표 계정을 조회합니다 (없으면 초기 관리자 자동 생성 후 반환)."""
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, email, name, phone, zipcode, address, address_detail, role, is_active, withdrawn_at, points_balance, last_login_at, created_at
                FROM mall_users
                WHERE role = 'ADMIN' AND is_active = 1 AND COALESCE(withdrawn_at, '') = ''
                ORDER BY id ASC
                LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                return _format_user_row(row)
        finally:
            conn.close()

    ensure_default_mall_admin()
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, email, name, phone, zipcode, address, address_detail, role, is_active, withdrawn_at, points_balance, last_login_at, created_at
                FROM mall_users
                WHERE role = 'ADMIN' AND is_active = 1 AND COALESCE(withdrawn_at, '') = ''
                ORDER BY id ASC
                LIMIT 1
            """)
            row = cur.fetchone()
            return _format_user_row(row)
        finally:
            conn.close()


def _normalize_wish_product_nums(raw_nums) -> list[int]:
    if not isinstance(raw_nums, (list, tuple, set)):
        return []
    result: list[int] = []
    seen: set[int] = set()
    for val in raw_nums:
        num = parse_strict_int64(val, min_val=1)
        if num is None or num in seen:
            continue
        seen.add(num)
        result.append(num)
        if len(result) >= 100:
            break
    return result


def get_user_wishlist_nums(user_id: int) -> list[int]:
    uid = parse_strict_int64(user_id, min_val=1)
    if uid is None:
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
                (uid,),
            )
            return [int(r["product_num"]) for r in cur.fetchall()]
        finally:
            conn.close()


def get_product_wish_count(product_num: int) -> int:
    p_num = parse_strict_int64(product_num, min_val=1)
    if p_num is None:
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
    uid = parse_strict_int64(user_id, min_val=1)
    p_num = parse_strict_int64(product_num, min_val=1)
    if uid is None or p_num is None:
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
    uid = parse_strict_int64(user_id, min_val=1)
    if uid is None:
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
    uid = parse_strict_int64(user_id, min_val=1)
    if uid is None:
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


ALLOWED_BANNER_THEMES = ("indigo", "charcoal", "slate", "emerald")


def _sanitize_banner_link(url: Any) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    if raw.startswith("/") and not raw.startswith("//") and not raw.startswith("/\\"):
        return raw[:300]
    lower = raw.lower()
    if lower.startswith("https://") or lower.startswith("http://"):
        return raw[:300]
    return ""


def _sanitize_banner_image_url(url: Any) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    if raw.startswith("/") and not raw.startswith("//") and not raw.startswith("/\\"):
        return raw[:300]
    lower = raw.lower()
    if lower.startswith("https://") or lower.startswith("http://"):
        return raw[:300]
    return ""


def _normalize_banner_date(val: Any) -> str:
    raw = str(val or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    return ""


def _format_banner_row(row_dict: dict, today_str: str) -> dict:
    item = dict(row_dict)
    item["id"] = int(item.get("id") or 0)
    item["sort_order"] = int(item.get("sort_order") or 0)
    item["is_active"] = 1 if int(item.get("is_active") or 0) else 0
    theme = str(item.get("theme") or "indigo").strip().lower()
    item["theme"] = theme if theme in ALLOWED_BANNER_THEMES else "indigo"
    start_at = _normalize_banner_date(item.get("start_at"))
    end_at = _normalize_banner_date(item.get("end_at"))
    item["start_at"] = start_at
    item["end_at"] = end_at

    if not item["is_active"]:
        item["schedule_status"] = "INACTIVE"
    elif start_at and today_str < start_at:
        item["schedule_status"] = "SCHEDULED"
    elif end_at and today_str > end_at:
        item["schedule_status"] = "EXPIRED"
    else:
        item["schedule_status"] = "ACTIVE"
    return item


def list_mall_hero_banners(active_only: bool = False) -> list:
    """메인 히어로 배너 목록을 노출 순서대로 반환합니다."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM mall_hero_banners ORDER BY sort_order ASC, id DESC")
            rows = [_format_banner_row(dict(r), today_str) for r in cur.fetchall()]
        finally:
            conn.close()
    if active_only:
        return [r for r in rows if r["schedule_status"] == "ACTIVE"]
    return rows


def save_mall_hero_banner(banner_data: Any) -> dict:
    """메인 히어로 배너를 신규 등록하거나 수정합니다."""
    if not isinstance(banner_data, dict):
        return {"ok": False, "message": "잘못된 요청 형식입니다."}
    raw = banner_data
    raw_bid = raw.get("id")
    if isinstance(raw_bid, (bool, float)):
        return {"ok": False, "message": "올바른 배너 ID가 필요합니다."}
    if raw_bid in (None, "", 0, "0"):
        banner_id = 0
    else:
        banner_id = parse_strict_int64(raw_bid, min_val=1)
        if banner_id is None:
            return {"ok": False, "message": "올바른 배너 ID가 필요합니다."}

    eyebrow = str(raw.get("eyebrow") or "").strip()[:60]
    title = str(raw.get("title") or "").strip()[:140]
    subtitle = str(raw.get("subtitle") or "").strip()[:300]
    cta_label = str(raw.get("cta_label") or "").strip()[:50]
    link_url = _sanitize_banner_link(raw.get("link_url"))
    image_url = _sanitize_banner_image_url(raw.get("image_url"))
    theme = str(raw.get("theme") or "indigo").strip().lower()
    if theme not in ALLOWED_BANNER_THEMES:
        theme = "indigo"

    raw_sort = raw.get("sort_order")
    if raw_sort is None or str(raw_sort).strip() == "":
        sort_order = 10
    else:
        sort_order = parse_strict_int64(raw_sort, min_val=-1_000_000, max_val=1_000_000)
        if sort_order is None:
            return {"ok": False, "message": "정렬 순서는 올바른 정수로 입력해 주세요."}

    raw_active = raw.get("is_active")
    is_active = 0 if raw_active in (0, False, "0", "false", "False") else 1
    start_at = _normalize_banner_date(raw.get("start_at"))
    end_at = _normalize_banner_date(raw.get("end_at"))

    if not title and not image_url:
        return {"ok": False, "message": "배너 제목 또는 배너 이미지 중 하나 이상을 입력해 주세요."}
    if start_at and end_at and start_at > end_at:
        return {"ok": False, "message": "종료일은 시작일 이후 날짜로 입력해 주세요."}

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            if banner_id > 0:
                cur.execute("SELECT id FROM mall_hero_banners WHERE id = ?", (banner_id,))
                if not cur.fetchone():
                    return {"ok": False, "message": "수정할 배너를 찾을 수 없습니다."}
                cur.execute(
                    """
                    UPDATE mall_hero_banners
                    SET eyebrow = ?, title = ?, subtitle = ?, cta_label = ?, link_url = ?,
                        image_url = ?, theme = ?, sort_order = ?, is_active = ?,
                        start_at = ?, end_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        eyebrow, title, subtitle, cta_label, link_url, image_url,
                        theme, sort_order, is_active, start_at, end_at, now_str, banner_id,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO mall_hero_banners
                    (eyebrow, title, subtitle, cta_label, link_url, image_url, theme,
                     sort_order, is_active, start_at, end_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        eyebrow, title, subtitle, cta_label, link_url, image_url,
                        theme, sort_order, is_active, start_at, end_at, now_str, now_str,
                    ),
                )
                banner_id = int(cur.lastrowid or 0)
            conn.commit()
            cur.execute("SELECT * FROM mall_hero_banners WHERE id = ?", (banner_id,))
            saved_row = cur.fetchone()
        finally:
            conn.close()

    today_str = datetime.now().strftime("%Y-%m-%d")
    saved = _format_banner_row(dict(saved_row), today_str) if saved_row else None
    return {"ok": True, "banner": saved, "banners": list_mall_hero_banners(active_only=False)}


def toggle_mall_hero_banner(banner_id: int, is_active: Any) -> dict:
    """히어로 배너 활성/비활성 상태를 전환합니다."""
    bid = parse_strict_int64(banner_id, min_val=1)
    if bid is None:
        return {"ok": False, "message": "올바른 배너 ID가 필요합니다."}
    active_int = 0 if is_active in (0, False, "0", "false", "False") else 1
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE mall_hero_banners SET is_active = ?, updated_at = ? WHERE id = ?",
                (active_int, now_str, bid),
            )
            if int(cur.rowcount or 0) == 0:
                return {"ok": False, "message": "배너를 찾을 수 없습니다."}
            conn.commit()
        finally:
            conn.close()
    return {"ok": True, "banners": list_mall_hero_banners(active_only=False)}


def delete_mall_hero_banner(banner_id: int) -> dict:
    """히어로 배너를 삭제합니다."""
    bid = parse_strict_int64(banner_id, min_val=1)
    if bid is None:
        return {"ok": False, "message": "올바른 배너 ID가 필요합니다."}
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM mall_hero_banners WHERE id = ?", (bid,))
            if int(cur.rowcount or 0) == 0:
                return {"ok": False, "message": "삭제할 배너를 찾을 수 없습니다."}
            conn.commit()
        finally:
            conn.close()
    return {"ok": True, "banners": list_mall_hero_banners(active_only=False)}


def _normalize_iso_date(raw_val: Any) -> str:
    txt = str(raw_val or "").strip()
    if len(txt) >= 10 and txt[4:5] == "-" and txt[7:8] == "-":
        candidate = txt[:10]
        try:
            datetime.strptime(candidate, "%Y-%m-%d")
            return candidate
        except Exception:
            return ""
    return ""


def _format_supplier_row(row) -> dict:
    return {
        "id": int(row["id"]),
        "name": str(row["name"] or ""),
        "contact_name": str(row["contact_name"] or ""),
        "phone": str(row["phone"] or ""),
        "bank_name": str(row["bank_name"] or ""),
        "bank_account": str(row["bank_account"] or ""),
        "bank_holder": str(row["bank_holder"] or ""),
        "memo": str(row["memo"] or ""),
        "is_active": bool(int(row["is_active"] if row["is_active"] is not None else 1)),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }


def list_mall_suppliers(active_only: bool = False) -> list[dict]:
    """등록된 매입업체(공급처) 목록을 조회합니다."""
    with mall_db_lock:
        conn = get_connection()
        try:
            sql = "SELECT * FROM mall_suppliers"
            if active_only:
                sql += " WHERE is_active = 1"
            sql += " ORDER BY name COLLATE NOCASE ASC, id DESC"
            rows = conn.execute(sql).fetchall()
            return [_format_supplier_row(r) for r in rows]
        finally:
            conn.close()


def save_mall_supplier(data: Any) -> dict:
    """매입업체를 신규 등록하거나 수정합니다."""
    if not isinstance(data, dict):
        return {"ok": False, "message": "잘못된 요청 형식입니다."}
    raw = data
    raw_sid = raw.get("id")
    if isinstance(raw_sid, (bool, float)):
        return {"ok": False, "message": "올바른 매입업체 ID가 필요합니다."}
    if raw_sid in (None, "", 0, "0"):
        sup_id = 0
    else:
        sup_id = parse_strict_int64(raw_sid, min_val=1)
        if sup_id is None:
            return {"ok": False, "message": "올바른 매입업체 ID가 필요합니다."}

    name = str(raw.get("name") or "").strip()[:80]
    if not name:
        return {"ok": False, "message": "매입업체명을 입력해 주세요."}

    contact_name = str(raw.get("contact_name") or "").strip()[:60]
    phone = str(raw.get("phone") or "").strip()[:40]
    bank_name = str(raw.get("bank_name") or "").strip()[:60]
    bank_account = str(raw.get("bank_account") or "").strip()[:80]
    bank_holder = str(raw.get("bank_holder") or "").strip()[:60]
    memo = str(raw.get("memo") or "").strip()[:300]
    raw_active = raw.get("is_active")
    is_active = 0 if raw_active in (0, False, "0", "false", "False") else 1
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            if sup_id > 0:
                cur.execute("SELECT id FROM mall_suppliers WHERE id = ?", (sup_id,))
                if not cur.fetchone():
                    return {"ok": False, "message": "수정할 매입업체를 찾을 수 없습니다."}
                cur.execute("SELECT id FROM mall_suppliers WHERE LOWER(name) = LOWER(?) AND id != ?", (name, sup_id))
                if cur.fetchone():
                    return {"ok": False, "message": f"이미 등록된 업체명입니다: {name}"}
                cur.execute("""
                    UPDATE mall_suppliers
                    SET name = ?, contact_name = ?, phone = ?, bank_name = ?, bank_account = ?, bank_holder = ?,
                        memo = ?, is_active = ?, updated_at = ?
                    WHERE id = ?
                """, (name, contact_name, phone, bank_name, bank_account, bank_holder, memo, is_active, now_str, sup_id))
                saved_id = sup_id
            else:
                cur.execute("SELECT id FROM mall_suppliers WHERE LOWER(name) = LOWER(?)", (name,))
                if cur.fetchone():
                    return {"ok": False, "message": f"이미 등록된 업체명입니다: {name}"}
                cur.execute("""
                    INSERT INTO mall_suppliers
                    (name, contact_name, phone, bank_name, bank_account, bank_holder, memo, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (name, contact_name, phone, bank_name, bank_account, bank_holder, memo, is_active, now_str, now_str))
                saved_id = int(cur.lastrowid or 0)
            conn.commit()
            row = conn.execute("SELECT * FROM mall_suppliers WHERE id = ?", (saved_id,)).fetchone()
            saved_sup = _format_supplier_row(row) if row else None
        finally:
            conn.close()
    return {"ok": True, "supplier": saved_sup, "suppliers": list_mall_suppliers(active_only=False)}


def delete_mall_supplier(supplier_id: int) -> dict:
    """매입업체를 삭제합니다 (연결된 상품의 업체 참조는 해제하고 기존 주문 스냅샷은 보존)."""
    sid = parse_strict_int64(supplier_id, min_val=1)
    if sid is None:
        return {"ok": False, "message": "올바른 매입업체 ID가 필요합니다."}
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, name FROM mall_suppliers WHERE id = ?", (sid,))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "message": "삭제할 매입업체를 찾을 수 없습니다."}
            cur.execute("UPDATE mall_products SET supplier_id = NULL WHERE supplier_id = ?", (sid,))
            cur.execute("DELETE FROM mall_suppliers WHERE id = ?", (sid,))
            conn.commit()
        finally:
            conn.close()
    return {"ok": True, "suppliers": list_mall_suppliers(active_only=False)}


def add_mall_supplier_settlement(data: Any) -> dict:
    """매입업체에 실제 지급(결제)한 금액을 기록합니다."""
    if not isinstance(data, dict):
        return {"ok": False, "message": "잘못된 요청 형식입니다."}
    raw = data
    sup_id = parse_strict_int64(raw.get("supplier_id"), min_val=1)
    if sup_id is None:
        return {"ok": False, "message": "지급 대상 매입업체를 선택해 주세요."}

    amount = parse_strict_int64(raw.get("amount"), min_val=1)
    if amount is None:
        return {"ok": False, "message": "지급 금액은 1원 이상의 올바른 정수로 입력해 주세요."}

    paid_at = _normalize_iso_date(raw.get("paid_at")) or datetime.now().strftime("%Y-%m-%d")
    payment_method = str(raw.get("payment_method") or "계좌이체").strip()[:40] or "계좌이체"
    memo = str(raw.get("memo") or "").strip()[:200]
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, name FROM mall_suppliers WHERE id = ?", (sup_id,))
            sup_row = cur.fetchone()
            if not sup_row:
                return {"ok": False, "message": "선택한 매입업체를 찾을 수 없습니다."}
            cur.execute("""
                INSERT INTO mall_supplier_settlements (supplier_id, amount, paid_at, payment_method, memo, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (sup_id, amount, paid_at, payment_method, memo, now_str))
            new_id = int(cur.lastrowid or 0)
            conn.commit()
            return {
                "ok": True,
                "settlement": {
                    "id": new_id,
                    "supplier_id": sup_id,
                    "supplier_name": str(sup_row["name"] or ""),
                    "amount": amount,
                    "paid_at": paid_at,
                    "payment_method": payment_method,
                    "memo": memo,
                    "created_at": now_str,
                },
            }
        finally:
            conn.close()


def delete_mall_supplier_settlement(settlement_id: int) -> dict:
    """매입업체 지급(결제) 이력을 삭제합니다."""
    settle_id = parse_strict_int64(settlement_id, min_val=1)
    if settle_id is None:
        return {"ok": False, "message": "올바른 지급 이력 ID가 필요합니다."}
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM mall_supplier_settlements WHERE id = ?", (settle_id,))
            if int(cur.rowcount or 0) == 0:
                return {"ok": False, "message": "삭제할 지급 이력을 찾을 수 없습니다."}
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()


def get_mall_sales_summary(
    start_date: str = "",
    end_date: str = "",
    include_pending: bool = False,
    supplier_id: Optional[int] = None,
) -> dict:
    """
    쇼핑몰 매출·매입대금·매출총이익(마진)·택배비(별도) 및 매입업체별 정산 현황을 집계합니다.
    - 기본 집계 기준: 입금완료 주문 (주문취소·환불완료 제외), include_pending=True 시 입금대기 포함
    - 이익 공식: 상품 판매대금 - 매입대금 = 매출총이익 (택배비는 별도 KPI)
    """
    s_date = _normalize_iso_date(start_date)
    e_date = _normalize_iso_date(end_date)
    filter_sup_id = parse_strict_int64(supplier_id, min_val=1) if supplier_id is not None and str(supplier_id).strip() != "" else None

    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            suppliers = [_format_supplier_row(r) for r in cur.execute(
                "SELECT * FROM mall_suppliers ORDER BY name COLLATE NOCASE ASC, id DESC"
            ).fetchall()]
            supplier_by_id: dict[int, dict] = {int(s["id"]): s for s in suppliers}

            prod_fallback: dict[int, tuple[int, Optional[int], str]] = {}
            for pr in cur.execute("SELECT num, cost_price, supplier_id FROM mall_products").fetchall():
                pnum = int(pr["num"])
                pcost = max(0, int(pr["cost_price"] or 0))
                raw_psup = pr["supplier_id"]
                psup = int(raw_psup) if raw_psup is not None and int(raw_psup) > 0 else None
                psup_name = str(supplier_by_id[psup]["name"]) if psup and psup in supplier_by_id else ""
                prod_fallback[pnum] = (pcost, psup, psup_name)

            order_where = [
                "COALESCE(o.order_status, '') NOT IN ('주문취소', '환불완료')",
                "COALESCE(o.payment_status, '') != '환불완료'",
            ]
            order_params: list[Any] = []
            if include_pending:
                order_where.append("COALESCE(o.payment_status, '입금대기') IN ('입금완료', '입금대기')")
            else:
                order_where.append("COALESCE(o.payment_status, '') = '입금완료'")
            if s_date:
                order_where.append("substr(o.created_at, 1, 10) >= ?")
                order_params.append(s_date)
            if e_date:
                order_where.append("substr(o.created_at, 1, 10) <= ?")
                order_params.append(e_date)

            orders_rows = cur.execute(
                f"""
                SELECT o.id, o.shipping_fee, o.created_at
                FROM mall_orders o
                WHERE {' AND '.join(order_where)}
                ORDER BY o.id DESC
                """,
                order_params,
            ).fetchall()

            order_ids = [int(r["id"]) for r in orders_rows]
            order_meta: dict[int, dict] = {
                int(r["id"]): {
                    "date": str(r["created_at"] or "")[:10],
                    "shipping_fee": max(0, int(r["shipping_fee"] or 0)),
                }
                for r in orders_rows
            }

            item_rows = []
            if order_ids:
                placeholders = ",".join(["?"] * len(order_ids))
                item_rows = cur.execute(
                    f"""
                    SELECT order_id, product_num, product_name, quantity, line_total,
                           unit_cost, cost_total, supplier_id, supplier_name
                    FROM mall_order_items
                    WHERE order_id IN ({placeholders})
                    ORDER BY id ASC
                    """,
                    order_ids,
                ).fetchall()

            daily_map: dict[str, dict] = {}
            product_map: dict[int, dict] = {}
            sup_agg_map: dict[Any, dict] = {}

            for s in suppliers:
                sid = int(s["id"])
                sup_agg_map[sid] = {
                    "supplier_id": sid,
                    "supplier_name": str(s["name"]),
                    "contact_name": str(s["contact_name"]),
                    "phone": str(s["phone"]),
                    "bank_name": str(s["bank_name"]),
                    "bank_account": str(s["bank_account"]),
                    "bank_holder": str(s["bank_holder"]),
                    "sold_qty": 0,
                    "sales_amount": 0,
                    "purchase_cost": 0,
                    "settled_amount": 0,
                    "unpaid_amount": 0,
                }

            total_sales = 0
            total_cost = 0
            total_qty = 0
            matched_order_ids: set[int] = set()

            for it in item_rows:
                oid = int(it["order_id"])
                ometa = order_meta.get(oid)
                if not ometa:
                    continue
                pnum = int(it["product_num"] or 0)
                pname = str(it["product_name"] or f"상품 #{pnum}")
                qty = max(0, int(it["quantity"] or 0))
                line_total = max(0, int(it["line_total"] or 0))

                snap_cost = max(0, int(it["unit_cost"] or 0))
                snap_cost_total = max(0, int(it["cost_total"] or 0))
                raw_it_sup = it["supplier_id"]
                snap_sup_id = int(raw_it_sup) if raw_it_sup is not None and int(raw_it_sup) > 0 else None
                snap_sup_name = str(it["supplier_name"] or "").strip()

                if snap_cost == 0 and snap_sup_id is None and not snap_sup_name and pnum in prod_fallback:
                    fb_cost, fb_sup_id, fb_sup_name = prod_fallback[pnum]
                    eff_unit_cost = fb_cost
                    eff_cost_total = fb_cost * qty
                    eff_sup_id = fb_sup_id
                    eff_sup_name = fb_sup_name
                else:
                    eff_unit_cost = snap_cost
                    eff_cost_total = snap_cost_total if snap_cost_total > 0 or eff_unit_cost == 0 else eff_unit_cost * qty
                    eff_sup_id = snap_sup_id
                    eff_sup_name = snap_sup_name or (str(supplier_by_id[eff_sup_id]["name"]) if eff_sup_id and eff_sup_id in supplier_by_id else "")

                if filter_sup_id is not None and eff_sup_id != filter_sup_id:
                    continue

                matched_order_ids.add(oid)
                total_qty += qty
                total_sales += line_total
                total_cost += eff_cost_total

                dkey = ometa["date"] or "미상"
                if dkey not in daily_map:
                    daily_map[dkey] = {
                        "date": dkey,
                        "order_ids": set(),
                        "sold_qty": 0,
                        "sales_amount": 0,
                        "shipping_amount": 0,
                        "purchase_cost": 0,
                    }
                d_ent = daily_map[dkey]
                d_ent["order_ids"].add(oid)
                d_ent["sold_qty"] += qty
                d_ent["sales_amount"] += line_total
                d_ent["purchase_cost"] += eff_cost_total

                if pnum not in product_map:
                    product_map[pnum] = {
                        "product_num": pnum,
                        "product_name": pname,
                        "supplier_id": eff_sup_id,
                        "supplier_name": eff_sup_name or "매입업체 미지정",
                        "sold_qty": 0,
                        "sales_amount": 0,
                        "purchase_cost": 0,
                    }
                p_ent = product_map[pnum]
                p_ent["sold_qty"] += qty
                p_ent["sales_amount"] += line_total
                p_ent["purchase_cost"] += eff_cost_total

                sup_key: Any = eff_sup_id if (eff_sup_id and eff_sup_id in sup_agg_map) else ("UNASSIGNED" if not eff_sup_name else f"LEGACY:{eff_sup_name}")
                if sup_key not in sup_agg_map:
                    sup_agg_map[sup_key] = {
                        "supplier_id": eff_sup_id,
                        "supplier_name": eff_sup_name or "매입업체 미지정",
                        "contact_name": "",
                        "phone": "",
                        "bank_name": "",
                        "bank_account": "",
                        "bank_holder": "",
                        "sold_qty": 0,
                        "sales_amount": 0,
                        "purchase_cost": 0,
                        "settled_amount": 0,
                        "unpaid_amount": 0,
                    }
                s_ent = sup_agg_map[sup_key]
                s_ent["sold_qty"] += qty
                s_ent["sales_amount"] += line_total
                s_ent["purchase_cost"] += eff_cost_total

            total_shipping = 0
            for oid in matched_order_ids:
                ometa = order_meta.get(oid)
                if not ometa:
                    continue
                ship = int(ometa["shipping_fee"])
                total_shipping += ship
                dkey = ometa["date"] or "미상"
                if dkey in daily_map:
                    daily_map[dkey]["shipping_amount"] += ship

            settle_where = []
            settle_params: list[Any] = []
            if filter_sup_id is not None:
                settle_where.append("st.supplier_id = ?")
                settle_params.append(filter_sup_id)
            if s_date:
                settle_where.append("st.paid_at >= ?")
                settle_params.append(s_date)
            if e_date:
                settle_where.append("st.paid_at <= ?")
                settle_params.append(e_date)
            settle_where_sql = f"WHERE {' AND '.join(settle_where)}" if settle_where else ""

            settle_rows = cur.execute(
                f"""
                SELECT st.id, st.supplier_id, COALESCE(sup.name, '') AS supplier_name,
                       st.amount, st.paid_at, st.payment_method, st.memo, st.created_at
                FROM mall_supplier_settlements st
                LEFT JOIN mall_suppliers sup ON sup.id = st.supplier_id
                {settle_where_sql}
                ORDER BY st.paid_at DESC, st.id DESC
                LIMIT 200
                """,
                settle_params,
            ).fetchall()

            sum_where_sql = f"WHERE {' AND '.join(settle_where)}" if settle_where else ""
            settle_sum_rows = cur.execute(
                f"""
                SELECT st.supplier_id, COALESCE(SUM(st.amount), 0) AS total_paid
                FROM mall_supplier_settlements st
                {sum_where_sql}
                GROUP BY st.supplier_id
                """,
                settle_params,
            ).fetchall()

            total_settled = 0
            for sr in settle_sum_rows:
                sid = int(sr["supplier_id"])
                paid = max(0, int(sr["total_paid"] or 0))
                total_settled += paid
                if sid in sup_agg_map:
                    sup_agg_map[sid]["settled_amount"] = paid

            supplier_rows: list[dict] = []
            for skey, s_ent in sup_agg_map.items():
                if filter_sup_id is not None and s_ent.get("supplier_id") != filter_sup_id:
                    continue
                s_ent["unpaid_amount"] = int(s_ent["purchase_cost"]) - int(s_ent["settled_amount"])
                supplier_rows.append(s_ent)

            supplier_rows.sort(
                key=lambda x: (
                    0 if x["supplier_id"] is not None else 1,
                    -int(x["purchase_cost"]),
                    -int(x["sales_amount"]),
                    str(x["supplier_name"]),
                )
            )

            total_unpaid = sum(int(r["unpaid_amount"]) for r in supplier_rows if r.get("supplier_id") is not None)

            daily_rows: list[dict] = []
            for dkey, d_ent in sorted(daily_map.items(), key=lambda kv: kv[0], reverse=True):
                dsales = int(d_ent["sales_amount"])
                dcost = int(d_ent["purchase_cost"])
                dprofit = dsales - dcost
                dmrate = round((dprofit / dsales) * 100.0, 1) if dsales > 0 else 0.0
                daily_rows.append({
                    "date": dkey,
                    "order_count": len(d_ent["order_ids"]),
                    "sold_qty": int(d_ent["sold_qty"]),
                    "sales_amount": dsales,
                    "shipping_amount": int(d_ent["shipping_amount"]),
                    "purchase_cost": dcost,
                    "gross_profit": dprofit,
                    "margin_rate": dmrate,
                })

            product_rows: list[dict] = []
            for p_ent in sorted(product_map.values(), key=lambda x: (-int(x["sales_amount"]), int(x["product_num"]))):
                psales = int(p_ent["sales_amount"])
                pcost = int(p_ent["purchase_cost"])
                pprofit = psales - pcost
                pmrate = round((pprofit / psales) * 100.0, 1) if psales > 0 else 0.0
                product_rows.append({
                    "product_num": int(p_ent["product_num"]),
                    "product_name": str(p_ent["product_name"]),
                    "supplier_id": p_ent["supplier_id"],
                    "supplier_name": str(p_ent["supplier_name"]),
                    "sold_qty": int(p_ent["sold_qty"]),
                    "sales_amount": psales,
                    "purchase_cost": pcost,
                    "gross_profit": pprofit,
                    "margin_rate": pmrate,
                })

            gross_profit = total_sales - total_cost
            margin_rate = round((gross_profit / total_sales) * 100.0, 1) if total_sales > 0 else 0.0

            settlements = [
                {
                    "id": int(r["id"]),
                    "supplier_id": int(r["supplier_id"]),
                    "supplier_name": str(r["supplier_name"] or ""),
                    "amount": int(r["amount"] or 0),
                    "paid_at": str(r["paid_at"] or ""),
                    "payment_method": str(r["payment_method"] or ""),
                    "memo": str(r["memo"] or ""),
                    "created_at": str(r["created_at"] or ""),
                }
                for r in settle_rows
            ]

            return {
                "filters": {
                    "start_date": s_date,
                    "end_date": e_date,
                    "include_pending": bool(include_pending),
                    "supplier_id": filter_sup_id,
                },
                "kpi": {
                    "order_count": len(matched_order_ids),
                    "sold_qty": total_qty,
                    "sales_amount": total_sales,
                    "shipping_amount": total_shipping,
                    "total_collected": total_sales + total_shipping,
                    "purchase_cost": total_cost,
                    "gross_profit": gross_profit,
                    "margin_rate": margin_rate,
                    "total_settled": total_settled,
                    "total_unpaid": total_unpaid,
                },
                "daily_rows": daily_rows,
                "product_rows": product_rows,
                "supplier_rows": supplier_rows,
                "settlements": settlements,
                "suppliers": suppliers,
            }
        finally:
            conn.close()


try:
    init_mall_db()
except Exception:
    pass
