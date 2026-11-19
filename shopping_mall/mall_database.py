# -*- coding: utf-8 -*-
"""
독립 쇼핑몰 전용 SQLite 데이터베이스 관리 모듈 (mall_database.py)
- OBS 프로그램의 DB(live_commerce.db)와 완전히 분리된 별도의 DB(shopping_mall.db)로 운영됩니다.
- OBS에서 등록/수정된 상품(products, options, 재고, 이미지) 및 라이브 선택(Pin) 상품 상태를
  실시간 동기화 받아 저장하며, 쇼핑몰 자체 주문/결제/장바구니/리뷰 데이터를 관리합니다.
"""

import os
import time
import json
import uuid
import sqlite3
from datetime import datetime
from threading import RLock, Condition

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MALL_DB_PATH = os.getenv("MALL_DB_PATH", os.path.join(BASE_DIR, "shopping_mall.db"))
HEARTBEAT_TIMEOUT_SEC = float(os.getenv("MALL_HEARTBEAT_TIMEOUT_SEC", "35"))
mall_db_lock = RLock()
mall_state_cond = Condition()


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

            # 기존 mall_products 및 mall_orders 호환성 마이그레이션
            try:
                cur.execute("ALTER TABLE mall_products ADD COLUMN live_price INTEGER NOT NULL DEFAULT 0")
            except Exception:
                pass
            for col_name, col_def in [
                ("user_id", "INTEGER DEFAULT NULL"),
                ("is_guest", "INTEGER NOT NULL DEFAULT 0"),
                ("guest_password_hash", "TEXT DEFAULT ''")
            ]:
                try:
                    cur.execute(f"ALTER TABLE mall_orders ADD COLUMN {col_name} {col_def}")
                except Exception:
                    pass

            cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_products_num ON mall_products (num)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_orders_code ON mall_orders (order_code)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_orders_lookup ON mall_orders (buyer_name, buyer_phone)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_orders_user ON mall_orders (user_id, id DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_reviews_num ON mall_reviews (product_num, id DESC)")

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


def _format_product_row(prow, opt_rows):
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
    live_p = int(prow["live_price"] or 0) if "live_price" in prow.keys() and prow["live_price"] is not None else 0
    if live_p <= 0:
        live_p = sell_p
    cons_p = int(prow["consumer_price"] or sell_p)
    if cons_p < sell_p:
        cons_p = sell_p
    discount_rate = round((cons_p - sell_p) / cons_p * 100) if cons_p > sell_p and cons_p > 0 else 0
    live_discount_rate = round((cons_p - live_p) / cons_p * 100) if cons_p > live_p and cons_p > 0 else discount_rate

    img_url = str(prow["image_url"] or "").strip()

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
    category = str(prod_data.get("category") or _infer_category(name, specs)).strip().upper()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    created_at = str(prod_data.get("created_at") or now_str)

    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM mall_products WHERE num = ?", (num,))
            existing = cur.fetchone()
            if existing:
                mall_pid = existing["id"]
                cur.execute("""
                    UPDATE mall_products
                    SET obs_product_id = ?, name = ?, category = ?, price = ?, live_price = ?, consumer_price = ?,
                        image_url = ?, specs = ?, updated_at = ?
                    WHERE id = ?
                """, (obs_id, name, category, price, live_price, consumer_price, image_url, specs, now_str, mall_pid))
            else:
                cur.execute("""
                    INSERT INTO mall_products
                    (obs_product_id, num, name, category, price, live_price, consumer_price, image_url, specs, is_featured, updated_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """, (obs_id, num, name, category, price, live_price, consumer_price, image_url, specs, now_str, created_at))
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
        finally:
            conn.close()


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

    return {
        "state_version": int(raw.get("state_version") or 1),
        "catalog_version": int(raw.get("catalog_version") or 1),
        "is_live": bool(raw.get("is_live", False)),
        "broadcast_title": str(raw.get("broadcast_title") or "실시간 라이브 커머스 특가 방송"),
        "ticker_message": str(raw.get("ticker_message") or "라이브 방송 중 선택된 상품을 페이지 이동 없이 바로 확인하고 특가로 구매하세요!"),
        "pinned_product_num": pinned_num,
        "is_pinned_by_obs": is_pinned_by_obs,
        "selected_product": selected_product,
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

            # 현재 라이브 방송 중인지 확인 (라이브커머스 페이지 주문이거나 방송 중일 때는 라이브 가격 적용)
            cur.execute("SELECT value FROM mall_live_state WHERE key = 'is_live'")
            live_row = cur.fetchone()
            is_currently_live = False
            if live_row:
                try:
                    is_currently_live = bool(json.loads(live_row["value"]))
                except Exception:
                    is_currently_live = str(live_row["value"]).lower() in ("true", "1")

            use_live_pricing = (source_page == "LIVE") or is_currently_live

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
                import mall_security
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

    import mall_security
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


def update_mall_order_status(order_id, payment_status=None, order_status=None):
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            if payment_status:
                cur.execute("UPDATE mall_orders SET payment_status = ? WHERE id = ?", (payment_status, int(order_id)))
            if order_status:
                cur.execute("UPDATE mall_orders SET order_status = ? WHERE id = ?", (order_status, int(order_id)))
            conn.commit()
            deleted = cur.rowcount > 0
            if deleted:
                bump_state_version(bump_catalog=True)
            return deleted
        finally:
            conn.close()


def get_mall_reviews(product_num):
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM mall_reviews WHERE product_num = ? ORDER BY id DESC LIMIT 30", (int(product_num),))
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def add_mall_review(product_num, author, rating, content):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with mall_db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO mall_reviews (product_num, author, rating, content, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                int(product_num),
                str(author or "구매고객").strip(),
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

import secrets as _secrets
import mall_security


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


try:
    init_mall_db()
except Exception:
    pass
