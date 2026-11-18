# -*- coding: utf-8 -*-
"""
라이브 커머스 통합 SQLite 데이터베이스 관리 모듈 (database.py)
상품, 옵션별 재고, 실시간 주문 원장, 3사 채팅 로그, CS 문의 내역을
단일 SQLite 파일(live_commerce.db)에서 통합 영속화 및 트랜잭션 관리합니다.
"""

import os
import sys
import io
import sqlite3
from datetime import datetime
from threading import RLock
import openpyxl

# 윈도우 콘솔 UTF-8 출력 설정 (이모지 및 한글 깨짐/인코딩 에러 방지)
if sys.platform.startswith("win"):
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DB_PATH = "live_commerce.db"
db_lock = RLock()


def get_connection():
    """SQLite 데이터베이스 연결을 생성하고 Row 팩토리를 설정합니다."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=20.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(reset=False):
    """
    데이터베이스 테이블 및 인덱스를 생성합니다.
    reset=True일 경우 기존 데이터를 초기화하고 스키마를 재생성합니다.
    """
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()

            if reset:
                cursor.execute("DROP TABLE IF EXISTS product_options")
                cursor.execute("DROP TABLE IF EXISTS orders")
                cursor.execute("DROP TABLE IF EXISTS chats")
                cursor.execute("DROP TABLE IF EXISTS cs_inquiries")
                cursor.execute("DROP TABLE IF EXISTS products")

            # 1. 상품 테이블 (products)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    num INTEGER UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    price INTEGER NOT NULL DEFAULT 0,
                    live_price INTEGER NOT NULL DEFAULT 0,
                    consumer_price INTEGER NOT NULL DEFAULT 0,
                    cost_price INTEGER NOT NULL DEFAULT 0,
                    image_url TEXT DEFAULT '',
                    specs TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)

            # 2. 상품 옵션 및 재고 테이블 (product_options)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS product_options (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER NOT NULL,
                    color TEXT NOT NULL,
                    size TEXT NOT NULL,
                    stock INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (product_id) REFERENCES products (id) ON DELETE CASCADE,
                    UNIQUE(product_id, color, size)
                )
            """)

            # 3. 실시간 주문 원장 (orders)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_time TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    buyer_name TEXT NOT NULL,
                    product_id INTEGER,
                    item_name TEXT NOT NULL,
                    color TEXT DEFAULT '-',
                    size TEXT DEFAULT '-',
                    quantity INTEGER NOT NULL DEFAULT 1,
                    unit_price INTEGER NOT NULL DEFAULT 0,
                    unit_cost INTEGER NOT NULL DEFAULT 0,
                    total_price INTEGER NOT NULL DEFAULT 0,
                    profit INTEGER NOT NULL DEFAULT 0,
                    raw_message TEXT DEFAULT '',
                    reply_text TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    confirmed_at TEXT DEFAULT '',
                    FOREIGN KEY (product_id) REFERENCES products (id) ON DELETE SET NULL
                )
            """)

            # 기존 DB 호환성 보장: products, orders, chats 컬럼 자동 마이그레이션
            migrations = [
                ("products", "live_price", "INTEGER DEFAULT 0"),
                ("products", "consumer_price", "INTEGER DEFAULT 0"),
                ("products", "cost_price", "INTEGER DEFAULT 0"),
                ("orders", "confirmed_at", "TEXT DEFAULT ''"),
                ("orders", "unit_cost", "INTEGER DEFAULT 0"),
                ("orders", "profit", "INTEGER DEFAULT 0"),
                ("orders", "payment_status", "TEXT DEFAULT 'PENDING'"),
                ("orders", "bundle_id", "TEXT DEFAULT ''"),
                ("orders", "memo", "TEXT DEFAULT ''"),
                ("chats", "reply_by", "TEXT DEFAULT 'AI'")
            ]
            for tbl, col, col_def in migrations:
                try:
                    cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {col_def}")
                except Exception:
                    pass

            # 4. 3사 라이브 채팅 원본 로그 (chats)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS chats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_time TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    message TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'CHAT',
                    ai_reply TEXT DEFAULT '',
                    reply_by TEXT DEFAULT 'AI'
                )
            """)

            # 5. CS 고객 문의 및 답변 로그 (cs_inquiries)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS cs_inquiries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    inquiry_time TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    category_type TEXT NOT NULL DEFAULT '일반 문의',
                    question TEXT NOT NULL,
                    answer TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT '답변 완료'
                )
            """)

            # 6. 고객 관리 및 VIP/블랙리스트 (customers)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS customers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    buyer_name TEXT NOT NULL,
                    total_orders INTEGER NOT NULL DEFAULT 0,
                    total_spent INTEGER NOT NULL DEFAULT 0,
                    is_vip INTEGER NOT NULL DEFAULT 0,
                    is_blacklisted INTEGER NOT NULL DEFAULT 0,
                    phone TEXT DEFAULT '',
                    memo TEXT DEFAULT '',
                    last_order_at TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(platform, buyer_name)
                )
            """)

            # 7. 쇼호스트 실시간 인이어 큐 (broadcast_cues)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS broadcast_cues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cue_text TEXT NOT NULL,
                    priority TEXT NOT NULL DEFAULT 'NORMAL',
                    created_at TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    sender TEXT NOT NULL DEFAULT 'PD'
                )
            """)

            # 8. OBS 동적 오버레이 상태 저장 (overlay_state)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS overlay_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            # 9. 실시간 시청자 댓글 럭키 드로우 추첨 이력 (lottery_events)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS lottery_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    keyword TEXT NOT NULL,
                    winner_count INTEGER NOT NULL DEFAULT 1,
                    winners_json TEXT DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'COMPLETED',
                    created_at TEXT NOT NULL
                )
            """)

            # 10. 이전 방송 세션 아카이브 기록 (broadcast_sessions)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS broadcast_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    total_chats INTEGER NOT NULL DEFAULT 0,
                    total_orders INTEGER NOT NULL DEFAULT 0,
                    total_revenue INTEGER NOT NULL DEFAULT 0,
                    total_profit INTEGER NOT NULL DEFAULT 0,
                    total_cs INTEGER NOT NULL DEFAULT 0,
                    orders_json TEXT DEFAULT '[]',
                    chats_json TEXT DEFAULT '[]',
                    cs_json TEXT DEFAULT '[]',
                    lottery_json TEXT DEFAULT '[]',
                    analytics_json TEXT DEFAULT '{}'
                )
            """)

            # 11. OBS 스튜디오 방송자/관리자 회원 테이블 (studio_users)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS studio_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    phone TEXT DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'BROADCASTER',
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    failed_login_count INTEGER NOT NULL DEFAULT 0,
                    locked_until REAL NOT NULL DEFAULT 0,
                    last_login_at TEXT DEFAULT '',
                    approved_at TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)

            # 12. OBS 스튜디오 가입 승인용 초대코드 테이블 (studio_invite_codes)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS studio_invite_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT UNIQUE NOT NULL,
                    memo TEXT DEFAULT '',
                    max_uses INTEGER NOT NULL DEFAULT 10,
                    used_count INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    expires_at TEXT DEFAULT '',
                    created_by TEXT DEFAULT 'ADMIN',
                    created_at TEXT NOT NULL
                )
            """)

            # 13. 보안 감사 로그 테이블 (security_audit_logs)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS security_audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_time TEXT NOT NULL,
                    actor_type TEXT NOT NULL DEFAULT 'USER',
                    actor_id TEXT DEFAULT '',
                    ip_address TEXT DEFAULT '',
                    action TEXT NOT NULL,
                    detail TEXT DEFAULT ''
                )
            """)

            # 고속 검색용 인덱스
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_products_num ON products (num)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_time ON orders (order_time DESC)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_bundle ON orders (bundle_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_chats_time ON chats (chat_time DESC)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_cs_time ON cs_inquiries (inquiry_time DESC)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_customers_lookup ON customers (platform, buyer_name)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_cues_active ON broadcast_cues (is_active, id DESC)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_ended ON broadcast_sessions (ended_at DESC, id DESC)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_studio_users_status ON studio_users (status, role)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_time ON security_audit_logs (event_time DESC)")

            conn.commit()
            print("✅ [SQLite DB 초기화 완료] live_commerce.db 13개 테이블 및 인덱스 준비됨")
        finally:
            conn.close()


# ==========================================
# 📦 상품 및 재고(Options) 관리 API
# ==========================================

def add_product(product_data):
    """신규 상품 및 옵션 매트릭스를 DB에 일괄 저장합니다."""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()

            # 상품 번호 배정 (지정되지 않았으면 MAX(num) + 1)
            num = product_data.get("num")
            if not num:
                cursor.execute("SELECT COALESCE(MAX(num), 0) + 1 FROM products")
                num = cursor.fetchone()[0]

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sell_price = int(product_data.get("price", 0))
            cons_price = int(product_data.get("consumer_price", 0))
            live_price = int(product_data.get("live_price", 0))
            cost_price = int(product_data.get("cost_price", 0))

            if sell_price <= 0 and cons_price > 0:
                sell_price = cons_price
            if cons_price <= 0 and sell_price > 0:
                cons_price = sell_price
            if live_price <= 0:
                live_price = sell_price

            cursor.execute("""
                INSERT INTO products (num, name, price, live_price, consumer_price, cost_price, image_url, specs, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                num,
                product_data.get("name", "신규 상품"),
                sell_price,
                live_price,
                cons_price,
                cost_price,
                product_data.get("image_url", ""),
                product_data.get("specs", ""),
                now_str
            ))
            product_id = cursor.lastrowid

            # 옵션 저장
            options = product_data.get("options", [])
            for opt in options:
                cursor.execute("""
                    INSERT OR REPLACE INTO product_options (product_id, color, size, stock)
                    VALUES (?, ?, ?, ?)
                """, (
                    product_id,
                    str(opt.get("color", "-")).strip(),
                    str(opt.get("size", "-")).strip(),
                    max(0, int(opt.get("stock", 0)))
                ))

            conn.commit()
            created_prod = get_product_by_id(product_id)
        finally:
            conn.close()
    try:
        sync_to_shopping_mall("upsert_product", {"product": created_prod})
    except Exception:
        pass
    return created_prod


def get_all_products():
    """모든 등록 상품 및 하위 옵션별 재고 현황을 정렬하여 반환합니다 (원가 및 손익 지표 포함)."""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM products ORDER BY created_at DESC, id DESC")
            prod_rows = cursor.fetchall()

            products = []
            for prow in prod_rows:
                pid = prow["id"]
                cursor.execute("SELECT color, size, stock FROM product_options WHERE product_id = ? ORDER BY id ASC", (pid,))
                opt_rows = cursor.fetchall()

                options = []
                total_stock = 0
                for opt in opt_rows:
                    stk = opt["stock"]
                    total_stock += stk
                    options.append({
                        "color": opt["color"],
                        "size": opt["size"],
                        "stock": stk
                    })

                sell_p = prow["price"]
                live_p = prow["live_price"] if "live_price" in prow.keys() and prow["live_price"] is not None and int(prow["live_price"]) > 0 else sell_p
                cons_p = prow["consumer_price"] if "consumer_price" in prow.keys() and prow["consumer_price"] is not None else sell_p
                cost_p = prow["cost_price"] if "cost_price" in prow.keys() and prow["cost_price"] is not None else 0
                if cons_p <= 0:
                    cons_p = sell_p

                margin_unit = max(0, sell_p - cost_p) if cost_p > 0 else sell_p
                margin_rate = round((margin_unit / sell_p * 100), 1) if sell_p > 0 else 0
                live_margin_unit = max(0, live_p - cost_p) if cost_p > 0 else live_p
                live_margin_rate = round((live_margin_unit / live_p * 100), 1) if live_p > 0 else 0
                potential_profit = margin_unit * total_stock

                products.append({
                    "id": f"item-{pid}",
                    "db_id": pid,
                    "num": prow["num"],
                    "name": prow["name"],
                    "price": sell_p,
                    "live_price": live_p,
                    "consumer_price": cons_p,
                    "cost_price": cost_p,
                    "margin_unit": margin_unit,
                    "margin_rate": margin_rate,
                    "live_margin_unit": live_margin_unit,
                    "live_margin_rate": live_margin_rate,
                    "potential_profit": potential_profit,
                    "image_url": prow["image_url"],
                    "specs": prow["specs"],
                    "created_at": prow["created_at"],
                    "total_stock": total_stock,
                    "options": options
                })
            return products
        finally:
            conn.close()


def get_product_by_id(product_id):
    """특정 상품 ID의 상세 정보와 옵션 목록을 반환합니다."""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            real_id = int(str(product_id).replace("item-", ""))
            cursor.execute("SELECT * FROM products WHERE id = ?", (real_id,))
            prow = cursor.fetchone()
            if not prow:
                return None

            cursor.execute("SELECT color, size, stock FROM product_options WHERE product_id = ? ORDER BY id ASC", (real_id,))
            opt_rows = cursor.fetchall()
            options = [{"color": r["color"], "size": r["size"], "stock": r["stock"]} for r in opt_rows]
            total_stock = sum(o["stock"] for o in options)

            sell_p = prow["price"]
            live_p = prow["live_price"] if "live_price" in prow.keys() and prow["live_price"] is not None and int(prow["live_price"]) > 0 else sell_p
            cons_p = prow["consumer_price"] if "consumer_price" in prow.keys() and prow["consumer_price"] is not None else sell_p
            cost_p = prow["cost_price"] if "cost_price" in prow.keys() and prow["cost_price"] is not None else 0
            if cons_p <= 0:
                cons_p = sell_p

            margin_unit = max(0, sell_p - cost_p) if cost_p > 0 else sell_p
            margin_rate = round((margin_unit / sell_p * 100), 1) if sell_p > 0 else 0
            live_margin_unit = max(0, live_p - cost_p) if cost_p > 0 else live_p
            live_margin_rate = round((live_margin_unit / live_p * 100), 1) if live_p > 0 else 0
            potential_profit = margin_unit * total_stock

            return {
                "id": f"item-{prow['id']}",
                "db_id": prow["id"],
                "num": prow["num"],
                "name": prow["name"],
                "price": sell_p,
                "live_price": live_p,
                "consumer_price": cons_p,
                "cost_price": cost_p,
                "margin_unit": margin_unit,
                "margin_rate": margin_rate,
                "live_margin_unit": live_margin_unit,
                "live_margin_rate": live_margin_rate,
                "potential_profit": potential_profit,
                "image_url": prow["image_url"],
                "specs": prow["specs"],
                "created_at": prow["created_at"],
                "total_stock": total_stock,
                "options": options
            }
        finally:
            conn.close()


def update_product(product_id, update_data):
    """상품 기본 정보(소비자가, 원가 포함) 및 옵션 수정"""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            real_id = int(str(product_id).replace("item-", ""))

            fields = []
            vals = []
            if "name" in update_data:
                fields.append("name = ?")
                vals.append(update_data["name"])
            if "price" in update_data:
                fields.append("price = ?")
                vals.append(int(update_data["price"]))
            if "live_price" in update_data:
                fields.append("live_price = ?")
                vals.append(int(update_data["live_price"] or update_data.get("price") or 0))
            if "consumer_price" in update_data:
                fields.append("consumer_price = ?")
                vals.append(int(update_data["consumer_price"]))
            if "cost_price" in update_data:
                fields.append("cost_price = ?")
                vals.append(int(update_data["cost_price"]))
            if "image_url" in update_data:
                fields.append("image_url = ?")
                vals.append(update_data["image_url"])
            if "specs" in update_data:
                fields.append("specs = ?")
                vals.append(update_data["specs"])

            if fields:
                vals.append(real_id)
                cursor.execute(f"UPDATE products SET {', '.join(fields)} WHERE id = ?", vals)

            # 옵션 목록도 함께 전달된 경우 재구성
            if "options" in update_data and isinstance(update_data["options"], list):
                cursor.execute("DELETE FROM product_options WHERE product_id = ?", (real_id,))
                for opt in update_data["options"]:
                    cursor.execute("""
                        INSERT INTO product_options (product_id, color, size, stock)
                        VALUES (?, ?, ?, ?)
                    """, (
                        real_id,
                        str(opt.get("color", "-")).strip(),
                        str(opt.get("size", "-")).strip(),
                        max(0, int(opt.get("stock", 0)))
                    ))

            conn.commit()
            updated_prod = get_product_by_id(real_id)
        except Exception as e:
            print(f"⚠️ [상품 수정 실패] {e}")
            return False
        finally:
            conn.close()
    try:
        if updated_prod:
            sync_to_shopping_mall("upsert_product", {"product": updated_prod})
    except Exception:
        pass
    return True


def delete_product(product_id):
    """상품 및 관련 옵션을 DB에서 삭제합니다."""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            real_id = int(str(product_id).replace("item-", ""))
            cursor.execute("SELECT num FROM products WHERE id = ?", (real_id,))
            row = cursor.fetchone()
            deleted_num = row["num"] if row else None
            cursor.execute("DELETE FROM products WHERE id = ?", (real_id,))
            conn.commit()
            ok = cursor.rowcount > 0
        finally:
            conn.close()
    if ok:
        try:
            sync_to_shopping_mall("delete_product", {"num": deleted_num, "obs_product_id": real_id})
        except Exception:
            pass
    return ok


def update_stock(product_id, color, size, new_stock):
    """특정 상품 옵션의 재고 수량을 수동 조정(입고/수정)합니다."""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            real_id = int(str(product_id).replace("item-", ""))
            cursor.execute("""
                UPDATE product_options
                SET stock = ?
                WHERE product_id = ? AND color = ? AND size = ?
            """, (max(0, int(new_stock)), real_id, color, size))
            conn.commit()
            ok = cursor.rowcount > 0
            updated_prod = get_product_by_id(real_id) if ok else None
        finally:
            conn.close()
    if ok and updated_prod:
        try:
            sync_to_shopping_mall("upsert_product", {"product": updated_prod})
        except Exception:
            pass
    return ok


def deduct_stock(item_name, color, size, qty=1):
    """
    주문 발생 시 해당 상품 옵션의 재고를 실시간 원자적(Atomic) 차감합니다.
    색상 영문/한글 유연 매칭 지원.
    상세 결과 딕셔너리를 반환합니다.
    """
    color_synonyms = {
        "블랙": "black", "black": "블랙",
        "화이트": "white", "white": "화이트",
        "그레이": "gray", "gray": "그레이", "grey": "그레이",
        "차콜": "charcoal", "charcoal": "차콜",
        "오트밀": "oatmeal", "oatmeal": "오트밀",
        "네이비": "navy", "navy": "네이비",
        "아이보리": "ivory", "ivory": "아이보리",
        "베이지": "beige", "beige": "베이지",
        "브라운": "brown", "brown": "브라운",
        "카키": "khaki", "khaki": "카키",
        "연청": "light blue", "진청": "dark blue"
    }

    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            products = get_all_products()

            item_str = str(item_name).lower()
            color_str = str(color).strip().lower()
            size_str = str(size).strip().upper()

            for p in products:
                num_tag = f"{p['num']}번"
                p_name_lower = p['name'].lower()
                if (num_tag in item_str) or (p['name'] in item_str) or (item_str in p_name_lower):
                    for opt in p.get("options", []):
                        opt_c = str(opt.get("color", "")).strip().lower()
                        opt_s = str(opt.get("size", "")).strip().upper()

                        # 색상 매칭
                        c_match = False
                        if color_str in ["-", "", "전체"]:
                            c_match = True
                        elif opt_c == color_str or opt_c in color_str or color_str in opt_c:
                            c_match = True
                        elif color_synonyms.get(color_str) and (color_synonyms[color_str] in opt_c or opt_c in color_synonyms[color_str]):
                            c_match = True
                        elif color_synonyms.get(opt_c) and (color_synonyms[opt_c] in color_str or color_str in color_synonyms[opt_c]):
                            c_match = True

                        # 사이즈 매칭
                        s_match = (size_str in ["-", "", "전체"]) or (opt_s == size_str)

                        if c_match and s_match:
                            current_stk = opt["stock"]
                            if current_stk <= 0:
                                return {
                                    "status": "ALREADY_SOLD_OUT",
                                    "success": False,
                                    "product_name": p["name"],
                                    "num": p["num"],
                                    "color": opt["color"],
                                    "size": opt["size"],
                                    "prev_stock": current_stk,
                                    "remaining_stock": current_stk,
                                    "deducted_qty": 0,
                                    "just_sold_out": False,
                                    "is_sold_out": True,
                                    "message": f"[{p['name']} {opt['color']}/{opt['size']}] 옵션은 이미 전량 품절되었습니다."
                                }

                            if current_stk < qty:
                                return {
                                    "status": "INSUFFICIENT",
                                    "success": False,
                                    "product_name": p["name"],
                                    "num": p["num"],
                                    "color": opt["color"],
                                    "size": opt["size"],
                                    "prev_stock": current_stk,
                                    "remaining_stock": current_stk,
                                    "deducted_qty": 0,
                                    "just_sold_out": False,
                                    "is_sold_out": False,
                                    "message": f"잔여 재고가 {current_stk}개로 부족합니다."
                                }

                            new_stk = current_stk - qty
                            cursor.execute("""
                                UPDATE product_options
                                SET stock = ?
                                WHERE product_id = ? AND color = ? AND size = ?
                            """, (new_stk, p["db_id"], opt["color"], opt["size"]))
                            conn.commit()

                            is_sold = (new_stk == 0)
                            status_str = "SOLD_OUT_NOW" if is_sold else "SUCCESS"
                            print(f"📦 [DB 재고 차감] {p['name']} ({opt['color']}/{opt['size']}) -{qty}개 (잔여: {new_stk}개) -> 상태: {status_str}")
                            try:
                                updated_p = get_product_by_id(p["db_id"])
                                if updated_p:
                                    sync_to_shopping_mall("upsert_product", {"product": updated_p})
                            except Exception:
                                pass

                            return {
                                "status": status_str,
                                "success": True,
                                "product_name": p["name"],
                                "num": p["num"],
                                "color": opt["color"],
                                "size": opt["size"],
                                "prev_stock": current_stk,
                                "remaining_stock": new_stk,
                                "deducted_qty": qty,
                                "just_sold_out": is_sold,
                                "is_sold_out": is_sold,
                                "message": f"재고 {qty}개 차감 완료 (잔여: {new_stk}개)"
                            }

            return {
                "status": "NOT_FOUND",
                "success": False,
                "product_name": item_name,
                "num": 0,
                "color": color,
                "size": size,
                "prev_stock": 0,
                "remaining_stock": 0,
                "deducted_qty": 0,
                "just_sold_out": False,
                "is_sold_out": False,
                "message": "해당 상품 또는 옵션을 찾을 수 없습니다."
            }
        finally:
            conn.close()


def get_catalog_text():
    """AI 프롬프트용 실시간 상품 카탈로그 텍스트를 생성합니다 (품절 태그 실시간 연동)."""
    products = get_all_products()
    if not products:
        return "[오늘의 라이브 판매 상품 목록]\n(현재 등록된 판매 상품이 없습니다. 시청자에게 곧 판매 상품이 안내될 예정이라고 안내하세요.)"

    lines = ["[오늘의 라이브 판매 상품 목록]"]
    for p in products:
        num = p.get("num", "-")
        name = p.get("name", "상품")
        live_p = int(p.get("live_price") or p.get("price") or 0)
        sell_p = int(p.get("price") or 0)
        price_str = f"라이브특가 {live_p:,}원 (일반판매가 {sell_p:,}원)" if live_p != sell_p and sell_p > 0 else f"{live_p:,}원"
        specs = p.get("specs", "")
        opts = p.get("options", [])
        total_stk = p.get("total_stock", 0)

        sold_out_prefix = "[전량 품절/SOLD OUT] " if total_stk <= 0 and len(opts) > 0 else ""

        opt_items = []
        for opt in opts:
            c = opt.get("color", "-")
            s = opt.get("size", "-")
            stk = opt.get("stock", 0)
            tag = " (품절/SOLD OUT)" if stk <= 0 else f" ({stk}개)"
            opt_items.append(f"{c}/{s}{tag}")

        opt_str = f"옵션: {', '.join(opt_items)}" if opt_items else "옵션 정보 없음"
        spec_str = f" / 사양: {specs}" if specs else ""
        lines.append(f"{num}번: {sold_out_prefix}{name} (가격: {price_str} / {opt_str}{spec_str})")
    return "\n".join(lines)


# ==========================================
# 🛒 실시간 주문 원장 (Orders) 관리 API
# ==========================================

def create_order(order_data):
    """
    주문 내역을 DB orders 테이블에 영속 저장하고 엑셀 파일과 동기화합니다.
    기본 status는 관리자 확인 전인 'PENDING'입니다.
    원가(unit_cost) 및 순이익(profit)도 함께 자동 산출하여 기록합니다.
    """
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            now_str = order_data.get("order_time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 단가 및 원가 조회 (상품 매칭 시)
            unit_price = int(order_data.get("unit_price", 0))
            unit_cost = int(order_data.get("unit_cost", 0))
            item_name = order_data.get("item_name", "기본상품")
            product_id = order_data.get("product_id")

            if unit_price <= 0 or unit_cost <= 0:
                products = get_all_products()
                for p in products:
                    if f"{p['num']}번" in item_name or p['name'] in item_name:
                        if unit_price <= 0:
                            unit_price = int(p.get('live_price') or p.get('price') or 0)
                        if unit_cost <= 0:
                            unit_cost = p.get('cost_price', 0)
                        product_id = p['db_id']
                        break

            qty = max(1, int(order_data.get("quantity", 1)))
            total_price = unit_price * qty
            profit = max(0, (unit_price - unit_cost)) * qty if unit_cost > 0 else total_price
            initial_status = order_data.get("status", "PENDING")

            cursor.execute("""
                INSERT INTO orders (
                    order_time, platform, buyer_name, product_id, item_name,
                    color, size, quantity, unit_price, unit_cost, total_price, profit,
                    raw_message, reply_text, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now_str,
                order_data.get("platform", "LIVE").upper(),
                order_data.get("buyer_name", "고객"),
                product_id,
                item_name,
                order_data.get("color", "-"),
                order_data.get("size", "-"),
                qty,
                unit_price,
                unit_cost,
                total_price,
                profit,
                order_data.get("raw_message", ""),
                order_data.get("reply_text", ""),
                initial_status
            ))
            conn.commit()
            order_id = cursor.lastrowid
            print(f"💾 [DB 주문 기록 완료 (상태: {initial_status})] #{order_id} {order_data.get('buyer_name')} - {item_name} ({qty}개 / 단가: {unit_price:,}원 / 원가: {unit_cost:,}원 / 이익: {profit:,}원)")

            # 고객 통계 자동 갱신 및 VIP 판정
            try:
                record_or_update_customer(order_data.get("platform", "LIVE"), order_data.get("buyer_name", "고객"), total_price, is_order=True)
            except Exception as cust_err:
                print(f"⚠️ [고객 통계 갱신 오류] {cust_err}")

            # 엑셀 파일 동기화
            export_orders_to_excel()
            return order_id
        finally:
            conn.close()


def confirm_order_and_deduct(order_id):
    """
    관리자가 [확인 및 재고 차감] 버튼을 눌렀을 때 실행.
    주문 상태를 'CONFIRMED'로 변경하고 실제 옵션 재고를 차감합니다.
    """
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
            ord_row = cursor.fetchone()
            if not ord_row:
                return {"success": False, "message": "주문 내역을 찾을 수 없습니다."}

            current_status = ord_row["status"]
            if current_status == "CONFIRMED":
                return {"success": False, "message": "이미 확인 완료되어 재고가 차감된 주문입니다."}
            if current_status == "REJECTED":
                return {"success": False, "message": "이미 취소/반려된 주문입니다."}

            item_name = ord_row["item_name"]
            color = ord_row["color"]
            size = ord_row["size"]
            qty = ord_row["quantity"]
            buyer = ord_row["buyer_name"]

            # 재고 차감 시도
            stock_res = deduct_stock(item_name, color, size, qty)

            if not stock_res["success"] and stock_res["status"] == "ALREADY_SOLD_OUT":
                return {
                    "success": False,
                    "status": "ALREADY_SOLD_OUT",
                    "order_id": order_id,
                    "buyer_name": buyer,
                    "item_name": item_name,
                    "color": color,
                    "size": size,
                    "message": "해당 상품 옵션은 이미 전량 품절되어 재고 차감이 불가합니다."
                }

            # 주문 상태 CONFIRMED로 변경
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("""
                UPDATE orders
                SET status = 'CONFIRMED', confirmed_at = ?
                WHERE id = ?
            """, (now_str, order_id))
            conn.commit()

            # 엑셀 파일 동기화
            export_orders_to_excel()

            return {
                "success": True,
                "status": "CONFIRMED",
                "order_id": order_id,
                "buyer_name": buyer,
                "item_name": stock_res.get("product_name", item_name),
                "color": stock_res.get("color", color),
                "size": stock_res.get("size", size),
                "quantity": qty,
                "remaining_stock": stock_res.get("remaining_stock", 0),
                "just_sold_out": stock_res.get("just_sold_out", False),
                "is_sold_out": stock_res.get("is_sold_out", False),
                "message": f"주문 #{order_id} 확인 완료 및 재고 {qty}개 차감 (잔여: {stock_res.get('remaining_stock', 0)}개)"
            }
        finally:
            conn.close()


def reject_order(order_id):
    """관리자가 주문을 반려/취소할 때 실행 (재고 차감 없음)"""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM orders WHERE id = ?", (order_id,))
            row = cursor.fetchone()
            if not row:
                return {"success": False, "message": "주문을 찾을 수 없습니다."}
            if row["status"] == "CONFIRMED":
                return {"success": False, "message": "이미 확인 완료된 주문은 반려할 수 없습니다. (재고 이미 차감됨)"}

            cursor.execute("UPDATE orders SET status = 'REJECTED' WHERE id = ?", (order_id,))
            conn.commit()
            export_orders_to_excel()
            return {"success": True, "message": f"주문 #{order_id}이 반려되었습니다."}
        finally:
            conn.close()


def get_pending_orders():
    """확인 대기 중(PENDING)인 주문 목록을 최신순으로 반환합니다."""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT o.id, o.order_time, o.platform, o.buyer_name, o.item_name,
                       o.color, o.size, o.quantity, o.unit_price, o.total_price, o.raw_message, o.reply_text, o.status,
                       COALESCE(o.payment_status, 'PENDING') as payment_status,
                       COALESCE(o.bundle_id, '') as bundle_id,
                       COALESCE(o.memo, '') as memo,
                       COALESCE(c.is_vip, 0) as is_vip,
                       COALESCE(c.is_blacklisted, 0) as is_blacklisted,
                       COALESCE(c.total_orders, 0) as buyer_total_orders
                FROM orders o
                LEFT JOIN customers c ON o.platform = c.platform AND o.buyer_name = c.buyer_name
                WHERE o.status = 'PENDING'
                ORDER BY o.id DESC
            """)
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def get_all_orders():
    """모든 주문 내역을 최신순으로 반환합니다 (원가 및 순이익 포함)."""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT order_time, platform, buyer_name, item_name, color, size,
                       quantity, raw_message, reply_text, total_price, id, status,
                       unit_price, unit_cost, profit
                FROM orders
                ORDER BY id DESC
            """)
            rows = cursor.fetchall()
            orders = []
            for r in rows:
                u_price = r["unit_price"] if "unit_price" in r.keys() else 0
                u_cost = r["unit_cost"] if "unit_cost" in r.keys() else 0
                prof = r["profit"] if "profit" in r.keys() else max(0, r["total_price"] - (u_cost * r["quantity"]))
                orders.append([
                    r["order_time"],
                    r["platform"],
                    r["buyer_name"],
                    r["item_name"],
                    r["color"],
                    r["size"],
                    r["quantity"],
                    r["raw_message"],
                    r["reply_text"],
                    r["total_price"],
                    r["id"],
                    r["status"],
                    u_price,
                    u_cost,
                    prof
                ])
            return orders
        finally:
            conn.close()


def get_recent_orders(limit=5):
    """최근 접수된 상위 N개 주문을 반환합니다."""
    orders = get_all_orders()
    return orders[:limit]


# ==========================================
# 📊 실시간 판매량 및 통계 분석 (SQL 집계)
# ==========================================

def get_analytics_summary():
    """
    하드코딩 없이 SQLite의 실제 집계 쿼리를 통해
    총 주문수, 총 매출, 총 원가, 총 순이익, 평균 마진율, 플랫폼별 비중, 베스트셀러 랭킹을 실시간 산출합니다.
    """
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()

            # 1. 총 주문수, 총 매출액, 총 원가, 총 순이익 (취소/반려된 건 제외)
            cursor.execute("""
                SELECT COUNT(*),
                       COALESCE(SUM(total_price), 0),
                       COALESCE(SUM(unit_cost * quantity), 0),
                       COALESCE(SUM(profit), 0)
                FROM orders
                WHERE status != 'REJECTED'
            """)
            total_orders, total_revenue, total_cost, total_profit = cursor.fetchone()
            margin_rate = round((total_profit / total_revenue * 100), 1) if total_revenue > 0 else 0

            cursor.execute("SELECT COUNT(*) FROM orders WHERE status = 'PENDING'")
            pending_orders = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM orders WHERE status = 'CONFIRMED'")
            confirmed_orders = cursor.fetchone()[0]

            # 2. 플랫폼별 비중 집계 (반려 제외)
            p_meta = {
                "YOUTUBE": {"name": "YouTube Live", "color": "#ef4444"},
                "INSTAGRAM": {"name": "Instagram Live", "color": "#ec4899"},
                "TIKTOK": {"name": "TikTok Live", "color": "#06b6d4"}
            }
            cursor.execute("""
                SELECT platform, COUNT(*) as cnt
                FROM orders
                WHERE status != 'REJECTED'
                GROUP BY platform
            """)
            p_counts = {r["platform"].upper(): r["cnt"] for r in cursor.fetchall()}

            platform_stats = []
            for p_key, meta in p_meta.items():
                cnt = p_counts.get(p_key, 0)
                pct = round((cnt / total_orders * 100), 1) if total_orders > 0 else 0
                platform_stats.append({
                    "name": meta["name"],
                    "color": meta["color"],
                    "count": cnt,
                    "pct": pct
                })

            # 3. 베스트셀러 랭킹 (반려 제외 실제 주문된 상품명 기준 GROUP BY)
            cursor.execute("""
                SELECT item_name,
                       SUM(quantity) as total_qty,
                       SUM(total_price) as total_rev,
                       COALESCE(SUM(profit), 0) as total_prof,
                       MAX(unit_price) as u_price,
                       MAX(unit_cost) as u_cost
                FROM orders
                WHERE status != 'REJECTED'
                GROUP BY item_name
                ORDER BY total_qty DESC, total_rev DESC
            """)
            rank_rows = cursor.fetchall()

            product_ranks = []
            for idx, r in enumerate(rank_rows):
                u_p = r["u_price"]
                u_c = r["u_cost"] if "u_cost" in r.keys() and r["u_cost"] is not None else 0
                t_rev = r["total_rev"]
                t_prof = r["total_prof"] if "total_prof" in r.keys() and r["total_prof"] is not None else max(0, t_rev - (u_c * r["total_qty"]))
                m_rate = round((t_prof / t_rev * 100), 1) if t_rev > 0 else 0

                product_ranks.append((
                    idx + 1,
                    {
                        "name": r["item_name"],
                        "price": u_p,
                        "cost": u_c,
                        "sold_qty": r["total_qty"],
                        "revenue": t_rev,
                        "profit": t_prof,
                        "margin_rate": m_rate
                    }
                ))

            # 4. 주문 원본 대장 (최신순)
            orders = get_all_orders()

            return {
                "orders": orders,
                "total_orders": total_orders,
                "total_revenue": total_revenue,
                "total_cost": total_cost,
                "total_profit": total_profit,
                "margin_rate": margin_rate,
                "pending_orders": pending_orders,
                "confirmed_orders": confirmed_orders,
                "platform_stats": platform_stats,
                "product_ranks": product_ranks
            }
        finally:
            conn.close()


# ==========================================
# 💬 실시간 채팅 및 CS 문의 관리 API
# ==========================================

def save_chat(platform, sender, message, category="CHAT", ai_reply=""):
    """수신된 3사 라이브 채팅 원본 및 AI 분석/답변 결과를 chats 테이블에 저장합니다."""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("""
                INSERT INTO chats (chat_time, platform, sender, message, category, ai_reply)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (now_str, platform.upper(), sender, message, category, ai_reply))
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()


def get_recent_chats(limit=500):
    """최근 수신된 상위 N개 채팅 로그를 반환합니다."""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, chat_time, platform, sender, message, category, ai_reply,
                       COALESCE(reply_by, 'AI') as reply_by
                FROM chats
                ORDER BY id DESC
                LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def get_chat_tracker_hydration_data(limit=500):
    """
    서버 시작/재시작 시 PlatformTracker가 기존 채팅 내역과 통계를 100% 이어받도록
    SQLite DB(chats, orders)에서 최근 채팅 목록 및 누적 카운트를 조회합니다.
    """
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            # 1. 카테고리별 전체 누적 통계
            cursor.execute("SELECT category, COUNT(*) as cnt FROM chats GROUP BY category")
            cat_rows = cursor.fetchall()
            stats = {"total": 0, "order": 0, "question": 0, "chat": 0}
            for r in cat_rows:
                cat = str(r["category"] or "CHAT").upper()
                cnt = int(r["cnt"] or 0)
                stats["total"] += cnt
                if cat == "ORDER":
                    stats["order"] += cnt
                elif cat == "QUESTION":
                    stats["question"] += cnt
                else:
                    stats["chat"] += cnt

            # 2. 플랫폼별 누적 통계 및 마지막 수신 메시지
            cursor.execute("""
                SELECT platform,
                       COUNT(*) as chat_count,
                       SUM(CASE WHEN category = 'ORDER' THEN 1 ELSE 0 END) as order_count,
                       MAX(id) as max_id
                FROM chats
                GROUP BY platform
            """)
            plat_rows = cursor.fetchall()
            platform_stats = {}
            for pr in plat_rows:
                p_name = str(pr["platform"] or "YOUTUBE").upper()
                max_id = pr["max_id"]
                last_user = "-"
                last_msg = "-"
                last_time = None
                if max_id:
                    cursor.execute("SELECT sender, message, chat_time FROM chats WHERE id = ?", (max_id,))
                    lrow = cursor.fetchone()
                    if lrow:
                        last_user = lrow["sender"]
                        last_msg = lrow["message"]
                        last_time = lrow["chat_time"]
                platform_stats[p_name] = {
                    "chat_count": int(pr["chat_count"] or 0),
                    "order_count": int(pr["order_count"] or 0),
                    "last_user": last_user,
                    "last_message": last_msg,
                    "last_time": last_time
                }

            # 3. 최근 채팅 목록 (최대 limit건)
            cursor.execute("""
                SELECT id, chat_time, platform, sender, message, category, ai_reply,
                       COALESCE(reply_by, 'AI') as reply_by
                FROM chats
                ORDER BY id DESC
                LIMIT ?
            """, (limit,))
            chat_rows = [dict(r) for r in cursor.fetchall()]

            return {
                "stats": stats,
                "platform_stats": platform_stats,
                "recent_chats": chat_rows
            }
        finally:
            conn.close()


def clear_all_chats():
    """사용자가 명시적으로 [초기화] 버튼을 눌렀을 때만 DB 채팅 로그를 비웁니다."""
    import json
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("DELETE FROM chats")
            cursor.execute("""
                INSERT OR REPLACE INTO overlay_state (key, value, updated_at)
                VALUES ('restored_session_id', ?, ?)
            """, (json.dumps(None), now_str))
            cursor.execute("""
                INSERT OR REPLACE INTO overlay_state (key, value, updated_at)
                VALUES ('restored_session_title', ?, ?)
            """, (json.dumps(None), now_str))
            cursor.execute("""
                INSERT OR REPLACE INTO overlay_state (key, value, updated_at)
                VALUES ('broadcast_started_at', ?, ?)
            """, (now_str, now_str))
            conn.commit()
            return True
        finally:
            conn.close()


def update_chat_reply(chat_id=None, sender=None, message=None, reply_text="", reply_by="ADMIN"):
    """
    채팅 로그(chats) 및 관련 CS 문의(cs_inquiries)에 관리자 답변을 즉시 반영합니다.
    """
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            updated = False
            # 1. chat_id로 업데이트 시도 (db_123 또는 정수 ID인 경우)
            if chat_id is not None:
                try:
                    raw_id_str = str(chat_id)
                    if raw_id_str.startswith("db_"):
                        c_id = int(raw_id_str.replace("db_", ""))
                    else:
                        c_id = int(raw_id_str.split("_")[0])
                    cursor.execute("""
                        UPDATE chats
                        SET ai_reply = ?, reply_by = ?
                        WHERE id = ?
                    """, (reply_text, reply_by, c_id))
                    if cursor.rowcount > 0:
                        updated = True
                except Exception:
                    pass

            # 2. sender 및 message로 가장 최근 채팅 매칭 업데이트
            if not updated and sender and message:
                cursor.execute("""
                    UPDATE chats
                    SET ai_reply = ?, reply_by = ?
                    WHERE id = (
                        SELECT id FROM chats
                        WHERE sender = ? AND message = ?
                        ORDER BY id DESC LIMIT 1
                    )
                """, (reply_text, reply_by, sender, message))
                if cursor.rowcount > 0:
                    updated = True

            # 3. cs_inquiries 테이블 연동 (동일 질문자/내용 시 답변 완료 처리)
            if sender:
                if message:
                    cursor.execute("""
                        UPDATE cs_inquiries
                        SET answer = ?, status = '관리자 답변 완료'
                        WHERE sender = ? AND question = ?
                    """, (reply_text, sender, message))
                else:
                    cursor.execute("""
                        UPDATE cs_inquiries
                        SET answer = ?, status = '관리자 답변 완료'
                        WHERE id = (
                            SELECT id FROM cs_inquiries
                            WHERE sender = ? ORDER BY id DESC LIMIT 1
                        )
                    """, (reply_text, sender))

            conn.commit()
            return True
        except Exception as e:
            print(f"⚠️ [채팅 답변 DB 업데이트 실패] {e}")
            return False
        finally:
            conn.close()


def save_cs_inquiry(platform, sender, category_type, question, answer=""):
    """CS 문의 및 AI 응대 내역을 cs_inquiries 테이블에 기록합니다."""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("""
                INSERT INTO cs_inquiries (inquiry_time, platform, sender, category_type, question, answer, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (now_str, platform.upper(), sender, category_type, question, answer, "답변 완료"))
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()


def get_all_cs(q_type=None):
    """모든 CS 문의 내역(또는 특정 유형 필터링)을 최신순으로 반환합니다."""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            if q_type and q_type != "ALL":
                cursor.execute("SELECT * FROM cs_inquiries WHERE category_type = ? ORDER BY id DESC", (q_type,))
            else:
                cursor.execute("SELECT * FROM cs_inquiries ORDER BY id DESC")
            rows = cursor.fetchall()
            res = []
            for r in rows:
                res.append({
                    "id": r["id"],
                    "time": r["inquiry_time"],
                    "platform": r["platform"],
                    "user": r["sender"],
                    "type": r["category_type"],
                    "message": r["question"],
                    "reply": r["answer"],
                    "status": r["status"]
                })
            return res
        finally:
            conn.close()


def get_cs_stats():
    """CS 문의 유형별 통계를 반환합니다."""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM cs_inquiries")
            total = cursor.fetchone()[0]

            cursor.execute("SELECT category_type, COUNT(*) as cnt FROM cs_inquiries GROUP BY category_type")
            by_type = {r["category_type"]: r["cnt"] for r in cursor.fetchall()}

            return {
                "total": total,
                "by_type": by_type
            }
        finally:
            conn.close()


# ==========================================
# 📑 엑셀 파일 동기화 및 내보내기 (Export)
# ==========================================

def export_orders_to_excel(excel_path="live_orders.xlsx"):
    """DB orders 테이블의 모든 데이터를 live_orders.xlsx 파일로 새로 작성합니다."""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT order_time, platform, buyer_name, item_name, color, size,
                       quantity, unit_price, unit_cost, profit, status, raw_message, reply_text
                FROM orders
                ORDER BY id ASC
            """)
            rows = cursor.fetchall()

            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "실시간주문목록"

            # 헤더
            headers = ["주문시간", "플랫폼", "고객닉네임", "상품명(번호)", "색상", "사이즈", "수량", "판매단가", "원가", "주문이익(마진)", "확인상태", "원본댓글", "AI안내메시지"]
            ws.append(headers)

            status_map = {
                "CONFIRMED": "확인완료(차감)",
                "PENDING": "확인대기(미차감)",
                "REJECTED": "취소/반려"
            }

            for r in rows:
                st_kr = status_map.get(r["status"], r["status"])
                u_p = r["unit_price"] if "unit_price" in r.keys() else 0
                u_c = r["unit_cost"] if "unit_cost" in r.keys() else 0
                prof = r["profit"] if "profit" in r.keys() else 0
                ws.append([
                    r["order_time"],
                    r["platform"],
                    r["buyer_name"],
                    r["item_name"],
                    r["color"],
                    r["size"],
                    r["quantity"],
                    u_p,
                    u_c,
                    prof,
                    st_kr,
                    r["raw_message"],
                    r["reply_text"]
                ])

            wb.save(excel_path)
            return True
        except Exception as e:
            print(f"⚠️ [엑셀 내보내기 오류] {e}")
            return False
        finally:
            conn.close()


# ==========================================
# 👥 고객 관리 및 VIP / 블랙리스트 API
# ==========================================

def record_or_update_customer(platform, buyer_name, total_price=0, is_order=True):
    """고객 정보를 기록하거나 주문 횟수 및 누적 금액을 갱신합니다."""
    if not buyer_name or buyer_name == "고객":
        return None
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            p = str(platform).strip().upper()
            b = str(buyer_name).strip()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            cursor.execute("SELECT * FROM customers WHERE platform = ? AND buyer_name = ?", (p, b))
            cust = cursor.fetchone()
            if cust:
                new_orders = cust["total_orders"] + (1 if is_order else 0)
                new_spent = cust["total_spent"] + int(total_price)
                # 누적 3회 이상 또는 10만원 이상 시 자동 VIP 승급 (기존 VIP 유지)
                is_vip = 1 if (cust["is_vip"] == 1 or new_orders >= 3 or new_spent >= 100000) else 0
                cursor.execute("""
                    UPDATE customers
                    SET total_orders = ?, total_spent = ?, is_vip = ?,
                        last_order_at = CASE WHEN ? = 1 THEN ? ELSE last_order_at END
                    WHERE id = ?
                """, (new_orders, new_spent, is_vip, 1 if is_order else 0, now_str, cust["id"]))
            else:
                new_orders = 1 if is_order else 0
                new_spent = int(total_price)
                is_vip = 1 if (new_orders >= 3 or new_spent >= 100000) else 0
                cursor.execute("""
                    INSERT INTO customers (platform, buyer_name, total_orders, total_spent, is_vip, is_blacklisted, last_order_at, created_at)
                    VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """, (p, b, new_orders, new_spent, is_vip, now_str if is_order else "", now_str))

            conn.commit()
            return get_customer(p, b)
        finally:
            conn.close()


def get_customer(platform, buyer_name):
    """특정 고객의 정보(VIP, 블랙리스트 여부, 주문 횟수)를 조회합니다."""
    if not buyer_name:
        return None
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            p = str(platform).strip().upper()
            b = str(buyer_name).strip()
            cursor.execute("SELECT * FROM customers WHERE platform = ? AND buyer_name = ?", (p, b))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
        finally:
            conn.close()


def get_all_customers(filter_type="ALL"):
    """고객 목록을 조회합니다 (ALL, VIP, BLACKLIST)."""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            if filter_type == "VIP":
                cursor.execute("SELECT * FROM customers WHERE is_vip = 1 ORDER BY total_spent DESC")
            elif filter_type == "BLACKLIST":
                cursor.execute("SELECT * FROM customers WHERE is_blacklisted = 1 ORDER BY id DESC")
            else:
                cursor.execute("SELECT * FROM customers ORDER BY total_orders DESC, total_spent DESC")
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def toggle_customer_flag(platform, buyer_name, flag_type, value=None):
    """VIP 또는 블랙리스트 상태를 토글/지정합니다."""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            p = str(platform).strip().upper()
            b = str(buyer_name).strip()
            cursor.execute("SELECT * FROM customers WHERE platform = ? AND buyer_name = ?", (p, b))
            cust = cursor.fetchone()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            col = "is_vip" if flag_type == "vip" else "is_blacklisted"

            if cust:
                new_val = (1 - cust[col]) if value is None else (1 if value else 0)
                cursor.execute(f"UPDATE customers SET {col} = ? WHERE id = ?", (new_val, cust["id"]))
            else:
                new_val = 1 if (value is None or value) else 0
                cursor.execute(f"""
                    INSERT INTO customers (platform, buyer_name, total_orders, total_spent, is_vip, is_blacklisted, created_at)
                    VALUES (?, ?, 0, 0, ?, ?, ?)
                """, (p, b, new_val if flag_type == "vip" else 0, new_val if flag_type == "blacklist" else 0, now_str))

            conn.commit()
            return {"success": True, "buyer_name": b, "platform": p, col: new_val}
        finally:
            conn.close()


# ==========================================
# 📣 쇼호스트 인이어 프롬프터 큐 API
# ==========================================

def add_broadcast_cue(cue_text, priority="NORMAL", sender="PD"):
    """쇼호스트 프롬프터에 실시간 지시 멘트를 발행합니다."""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("""
                INSERT INTO broadcast_cues (cue_text, priority, created_at, is_active, sender)
                VALUES (?, ?, ?, 1, ?)
            """, (cue_text.strip(), priority.upper(), now_str, sender))
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()


def get_active_broadcast_cues(limit=5):
    """현재 활성화된 쇼호스트 지시 멘트를 최신순으로 가져옵니다."""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, cue_text, priority, created_at, is_active, sender
                FROM broadcast_cues
                WHERE is_active = 1
                ORDER BY id DESC
                LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def clear_broadcast_cues():
    """모든 활성 지시 멘트를 비활성화합니다."""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("UPDATE broadcast_cues SET is_active = 0 WHERE is_active = 1")
            conn.commit()
            return True
        finally:
            conn.close()


# ==========================================
# 🖥️ OBS 동적 오버레이 상태 API
# ==========================================

def set_overlay_state(key, value):
    """OBS 브라우저 소스 동적 상태(핀 상품 번호, 타이머 등)를 설정합니다."""
    import json
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            val_str = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
            cursor.execute("""
                INSERT OR REPLACE INTO overlay_state (key, value, updated_at)
                VALUES (?, ?, ?)
            """, (key, val_str, now_str))
            conn.commit()
        finally:
            conn.close()
    if key in ("pinned_product_num", "flash_timer", "obs_ticker_custom", "simulcast_unified_title"):
        try:
            if key == "pinned_product_num":
                sync_to_shopping_mall("pin_product", {"pinned_product_num": value})
            elif key == "flash_timer":
                sync_to_shopping_mall("live_state", {"timer": value})
            elif key == "obs_ticker_custom":
                sync_to_shopping_mall("live_state", {"ticker_message": value})
            elif key == "simulcast_unified_title":
                sync_to_shopping_mall("live_state", {"broadcast_title": value})
        except Exception:
            pass
    return True


def get_overlay_state(key, default=None):
    """OBS 브라우저 소스 상태를 조회합니다."""
    import json
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM overlay_state WHERE key = ?", (key,))
            row = cursor.fetchone()
            if not row:
                return default
            val = row["value"]
            try:
                return json.loads(val)
            except Exception:
                return val
        finally:
            conn.close()


def get_all_overlay_states():
    """모든 오버레이 상태를 딕셔너리로 반환합니다."""
    import json
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM overlay_state")
            rows = cursor.fetchall()
            result = {}
            for r in rows:
                k = r["key"]
                v = r["value"]
                try:
                    result[k] = json.loads(v)
                except Exception:
                    result[k] = v
            return result
        finally:
            conn.close()


# ==========================================
# 📱 쇼호스트 전용 텔레프롬프터 & 디렉팅 확장 API
# ==========================================

def pin_host_question(user, question, platform="LIVE"):
    """쇼호스트 화면에 특정 시청자 질문을 픽업하여 대형 카드로 고정합니다."""
    data = {
        "user": user,
        "question": question,
        "platform": platform,
        "pinned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    return set_overlay_state("host_pinned_question", data)


def clear_host_question():
    """쇼호스트 화면의 픽업 질문을 닫습니다."""
    return set_overlay_state("host_pinned_question", None)


def get_host_pinned_question():
    """쇼호스트 화면에 고정된 시청자 질문을 가져옵니다."""
    return get_overlay_state("host_pinned_question", None)


def set_host_timer(action="start", minutes=3, label="⚡ 코너 진행"):
    """쇼호스트 화면에 표시할 세그먼트 카운트다운 타이머를 설정합니다."""
    import time
    if action == "stop":
        return set_overlay_state("host_timer", {"active": False})
    
    end_timestamp = time.time() + (float(minutes) * 60)
    data = {
        "active": True,
        "end_timestamp": end_timestamp,
        "minutes": minutes,
        "label": label,
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    return set_overlay_state("host_timer", data)


def get_host_timer():
    """쇼호스트 화면 타이머를 조회합니다."""
    import time
    timer = get_overlay_state("host_timer", None)
    if not timer or not timer.get("active"):
        return {"active": False}
    remaining = max(0, int(timer.get("end_timestamp", 0) - time.time()))
    return {
        "active": remaining > 0,
        "remaining_seconds": remaining,
        "label": timer.get("label", "코너 진행"),
        "minutes": timer.get("minutes", 3)
    }


# ==========================================
# 🎁 시청자 댓글 럭키 드로우 추첨기 API
# ==========================================

def save_lottery_event(title, keyword, winner_count, winners):
    """추첨 결과를 DB에 저장합니다."""
    import json
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            winners_json = json.dumps(winners, ensure_ascii=False)
            cursor.execute("""
                INSERT INTO lottery_events (title, keyword, winner_count, winners_json, status, created_at)
                VALUES (?, ?, ?, ?, 'COMPLETED', ?)
            """, (title, keyword, winner_count, winners_json, now_str))
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()


def get_lottery_history(limit=20):
    """최근 추첨 이력을 반환합니다."""
    import json
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM lottery_events ORDER BY id DESC LIMIT ?", (limit,))
            rows = cursor.fetchall()
            res = []
            for r in rows:
                d = dict(r)
                try:
                    d["winners"] = json.loads(d["winners_json"])
                except Exception:
                    d["winners"] = []
                res.append(d)
            return res
        finally:
            conn.close()


# ==========================================
# 💳 주문 결제 상태 & 합배송 묶음 관리 API
# ==========================================

def update_order_payment_status(order_id, new_status, memo=None):
    """주문 상태(결제, 배송 등)를 갱신합니다."""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            if memo is not None:
                cursor.execute("""
                    UPDATE orders
                    SET payment_status = ?, memo = ?
                    WHERE id = ?
                """, (new_status.upper(), memo, order_id))
            else:
                cursor.execute("""
                    UPDATE orders
                    SET payment_status = ?
                    WHERE id = ?
                """, (new_status.upper(), order_id))
            conn.commit()
            return True
        finally:
            conn.close()


def bundle_orders_by_buyer(buyer_name, platform=None, order_ids=None):
    """동일 고객의 복수 주문을 단일 합배송 번호(bundle_id)로 묶습니다."""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            b = str(buyer_name).strip()
            bundle_id = f"BUNDLE_{int(datetime.now().timestamp())}_{b[:4]}"

            if order_ids and len(order_ids) > 0:
                placeholders = ",".join("?" for _ in order_ids)
                params = [bundle_id] + list(order_ids) + [b]
                sql = f"UPDATE orders SET bundle_id = ? WHERE id IN ({placeholders}) AND buyer_name = ?"
                if platform:
                    sql += " AND platform = ?"
                    params.append(str(platform).strip().upper())
                cursor.execute(sql, params)
            else:
                params = [bundle_id, b]
                sql = "UPDATE orders SET bundle_id = ? WHERE buyer_name = ? AND status != 'REJECTED'"
                if platform:
                    sql += " AND platform = ?"
                    params.append(str(platform).strip().upper())
                cursor.execute(sql, params)

            conn.commit()
            return {"success": True, "bundle_id": bundle_id, "buyer_name": b}
        finally:
            conn.close()


def get_buyer_order_history(buyer_name, platform=None):
    """특정 시청자의 전체 주문 내역을 조회합니다."""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            if platform:
                cursor.execute("""
                    SELECT * FROM orders
                    WHERE buyer_name = ? AND platform = ?
                    ORDER BY id DESC
                """, (buyer_name, platform.upper()))
            else:
                cursor.execute("""
                    SELECT * FROM orders
                    WHERE buyer_name = ?
                    ORDER BY id DESC
                """, (buyer_name,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


# ==========================================
# 🎬 방송 세션 아카이브(별도 저장) & 새 방송 초기화 API
# ==========================================

def _compute_broadcast_signature(order_rows, chat_rows, cs_rows):
    """
    방송 데이터의 고유 지문(Signature)을 생성합니다.
    (복원 시 자동 증가 ID가 바뀌더라도 동일한 방송 데이터임을 100% 식별하여 2중 중복 저장을 원천 차단)
    """
    import hashlib
    import json

    norm_orders = [
        (
            str(o.get("order_time", "")),
            str(o.get("platform", "")),
            str(o.get("buyer_name", "")),
            str(o.get("item_name", "")),
            str(o.get("color", "")),
            str(o.get("size", "")),
            int(o.get("quantity", 0) or 0),
            int(o.get("total_price", 0) or 0),
            str(o.get("raw_message", ""))
        )
        for o in (order_rows or [])
    ]
    norm_chats = [
        (
            str(c.get("chat_time", "")),
            str(c.get("platform", "")),
            str(c.get("sender", "")),
            str(c.get("message", "")),
            str(c.get("category", ""))
        )
        for c in (chat_rows or [])
    ]
    norm_cs = [
        (
            str(cs.get("inquiry_time", "")),
            str(cs.get("platform", "")),
            str(cs.get("sender", "")),
            str(cs.get("question", ""))
        )
        for cs in (cs_rows or [])
    ]
    payload = json.dumps(
        {"orders": norm_orders, "chats": norm_chats, "cs": norm_cs},
        ensure_ascii=False,
        sort_keys=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_restored_session_info():
    """현재 화면이 '이전 방송 기록'에서 불러온(복원된) 상태인지 조회합니다."""
    restored_id = get_overlay_state("restored_session_id", None)
    restored_title = get_overlay_state("restored_session_title", None)
    if restored_id is not None:
        try:
            restored_id = int(restored_id)
        except Exception:
            restored_id = None
    return {
        "is_restored": restored_id is not None,
        "session_id": restored_id,
        "title": restored_title or ""
    }


def deduplicate_broadcast_sessions():
    """
    보관함(broadcast_sessions)에 동일한 데이터로 중복 저장된 방송이나
    데이터가 0건인 빈 방송(테스트 백업 제외)을 정리하여 1개만 남깁니다.
    """
    import json
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, title, total_chats, total_orders, total_cs,
                       orders_json, chats_json, cs_json
                FROM broadcast_sessions
                ORDER BY id ASC
            """)
            rows = cursor.fetchall()
            seen_signatures = set()
            ids_to_delete = []

            for r in rows:
                sid = r["id"]
                title = str(r["title"] or "")
                if title == "__TEST_PRE_BACKUP__":
                    continue

                t_chats = int(r["total_chats"] or 0)
                t_orders = int(r["total_orders"] or 0)
                t_cs = int(r["total_cs"] or 0)

                try:
                    o_list = json.loads(r["orders_json"] or "[]")
                except Exception:
                    o_list = []
                try:
                    c_list = json.loads(r["chats_json"] or "[]")
                except Exception:
                    c_list = []
                try:
                    cs_list = json.loads(r["cs_json"] or "[]")
                except Exception:
                    cs_list = []

                if t_chats == 0 and t_orders == 0 and t_cs == 0 and not o_list and not c_list and not cs_list:
                    ids_to_delete.append(sid)
                    continue

                sig = _compute_broadcast_signature(o_list, c_list, cs_list)
                if sig in seen_signatures:
                    ids_to_delete.append(sid)
                else:
                    seen_signatures.add(sig)

            if ids_to_delete:
                placeholders = ",".join(["?"] * len(ids_to_delete))
                cursor.execute(f"DELETE FROM broadcast_sessions WHERE id IN ({placeholders})", tuple(ids_to_delete))
                conn.commit()
            return len(ids_to_delete)
        finally:
            conn.close()


def archive_and_start_new_broadcast(title=None, save_empty=False, force_save=False):
    """
    현재 방송의 주문(orders), 채팅(chats), CS 문의(cs_inquiries), 추첨 이력(lottery_events) 및
    매출 통계를 broadcast_sessions 테이블에 별도로 영속 보관한 뒤,
    현재 방송 데이터와 OBS 오버레이 진행 상태(핀 상품, 타이머, 자막 등)를 0부터 시작하도록 초기화합니다.
    - 이미 보관함에서 불러온 방송(restored_session_id)이거나 동일한 데이터 스냅샷이 이미 저장되어 있으면
      중복(2중) 저장하지 않고 화면 초기화만 수행합니다.
    """
    import json
    with db_lock:
        # 먼저 analytics 요약을 조회
        analytics_summary = get_analytics_summary()
        restored_info = get_restored_session_info()
        conn = get_connection()
        try:
            cursor = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 1. 현재 데이터 전체 스냅샷 수집
            cursor.execute("SELECT * FROM orders ORDER BY id DESC")
            order_rows = [dict(r) for r in cursor.fetchall()]

            cursor.execute("SELECT * FROM chats ORDER BY id DESC")
            chat_rows = [dict(r) for r in cursor.fetchall()]

            cursor.execute("SELECT * FROM cs_inquiries ORDER BY id DESC")
            cs_rows = [dict(r) for r in cursor.fetchall()]

            cursor.execute("SELECT * FROM lottery_events ORDER BY id DESC")
            lottery_rows = [dict(r) for r in cursor.fetchall()]

            total_chats = len(chat_rows)
            total_orders = int(analytics_summary.get("total_orders", len(order_rows)))
            total_revenue = int(analytics_summary.get("total_revenue", 0))
            total_profit = int(analytics_summary.get("total_profit", 0))
            total_cs = len(cs_rows)

            # 방송 시작 시간 추정 (저장된 broadcast_started_at 또는 가장 오래된 채팅/주문 시각)
            started_at = now_str
            cursor.execute("SELECT value FROM overlay_state WHERE key = 'broadcast_started_at'")
            bs_row = cursor.fetchone()
            saved_started_at = None
            if bs_row and bs_row["value"]:
                try:
                    saved_started_at = json.loads(bs_row["value"])
                except Exception:
                    saved_started_at = str(bs_row["value"]).strip()

            time_candidates = []
            if saved_started_at and isinstance(saved_started_at, str) and len(saved_started_at) >= 10:
                time_candidates.append(saved_started_at)
            if chat_rows:
                time_candidates.append(chat_rows[-1].get("chat_time"))
            if order_rows:
                time_candidates.append(order_rows[-1].get("order_time"))
            valid_times = [t for t in time_candidates if t]
            if valid_times:
                started_at = min(valid_times)

            has_any_data = (total_chats > 0 or len(order_rows) > 0 or total_cs > 0 or len(lottery_rows) > 0)
            session_id = None
            skipped_duplicate = False
            saved_title = ""
            if not title:
                cursor.execute("SELECT value FROM overlay_state WHERE key = 'simulcast_unified_title'")
                st_row = cursor.fetchone()
                if st_row and st_row["value"]:
                    try:
                        saved_title = str(json.loads(st_row["value"]) or "").strip()
                    except Exception:
                        saved_title = str(st_row["value"] or "").strip()
            session_title = (str(title).strip() if title else "") or saved_title or f"{datetime.now().strftime('%Y-%m-%d %H:%M')} 라이브 방송"

            # 중복(2중) 저장 검사:
            # 1) 이미 보관함에서 불러온 방송(restored_session_id)을 열람 중인 경우 재저장 금지
            if not force_save and restored_info["is_restored"]:
                skipped_duplicate = True
                session_id = restored_info["session_id"]
                if restored_info["title"]:
                    session_title = restored_info["title"]
            # 2) 동일한 데이터 지문(Signature)을 가진 방송이 이미 broadcast_sessions에 존재하는지 검사
            elif not force_save and has_any_data:
                current_sig = _compute_broadcast_signature(order_rows, chat_rows, cs_rows)
                cursor.execute("""
                    SELECT id, title, orders_json, chats_json, cs_json
                    FROM broadcast_sessions
                    ORDER BY id DESC
                """)
                for existing_row in cursor.fetchall():
                    if str(existing_row["title"] or "") == "__TEST_PRE_BACKUP__":
                        continue
                    try:
                        ex_o = json.loads(existing_row["orders_json"] or "[]")
                        ex_c = json.loads(existing_row["chats_json"] or "[]")
                        ex_cs = json.loads(existing_row["cs_json"] or "[]")
                        if _compute_broadcast_signature(ex_o, ex_c, ex_cs) == current_sig:
                            skipped_duplicate = True
                            session_id = existing_row["id"]
                            session_title = existing_row["title"]
                            break
                    except Exception:
                        continue

            # 중복이 아니고 (데이터가 있거나 save_empty=True인 경우) 아카이브 테이블에 1회 저장
            if not skipped_duplicate and (has_any_data or save_empty):
                summary_meta = {
                    "total_orders": total_orders,
                    "total_revenue": total_revenue,
                    "total_cost": analytics_summary.get("total_cost", 0),
                    "total_profit": total_profit,
                    "margin_rate": analytics_summary.get("margin_rate", 0),
                    "pending_orders": analytics_summary.get("pending_orders", 0),
                    "confirmed_orders": analytics_summary.get("confirmed_orders", 0),
                    "platform_stats": analytics_summary.get("platform_stats", []),
                    "product_ranks": analytics_summary.get("product_ranks", [])
                }
                cursor.execute("""
                    INSERT INTO broadcast_sessions (
                        title, started_at, ended_at,
                        total_chats, total_orders, total_revenue, total_profit, total_cs,
                        orders_json, chats_json, cs_json, lottery_json, analytics_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    session_title,
                    started_at,
                    now_str,
                    total_chats,
                    total_orders,
                    total_revenue,
                    total_profit,
                    total_cs,
                    json.dumps(order_rows, ensure_ascii=False),
                    json.dumps(chat_rows, ensure_ascii=False),
                    json.dumps(cs_rows, ensure_ascii=False),
                    json.dumps(lottery_rows, ensure_ascii=False),
                    json.dumps(summary_meta, ensure_ascii=False)
                ))
                session_id = cursor.lastrowid

            # 2. 현재 방송 세션 테이블 데이터 일괄 초기화 (상품/재고 및 고객/디자인 설정은 유지)
            cursor.execute("DELETE FROM orders")
            cursor.execute("DELETE FROM chats")
            cursor.execute("DELETE FROM cs_inquiries")
            cursor.execute("DELETE FROM broadcast_cues")
            cursor.execute("DELETE FROM lottery_events")

            # 3. OBS 동적 오버레이 진행 상태 초기화 (overlay_settings는 보존, 복원 세션 잠금은 해제)
            default_welcome_ticker = "잠시 후 라이브 커머스 방송이 시작됩니다! 채팅창에 번호와 수량을 입력하여 간편하게 주문해 보세요."
            state_resets = {
                "pinned_product_num": json.dumps(None),
                "flash_timer": json.dumps({"active": False, "label": "⚡ 타임특가 마감까지", "end_time": None, "minutes": 5}, ensure_ascii=False),
                "host_pinned_question": json.dumps(None),
                "host_timer": json.dumps({"active": False}, ensure_ascii=False),
                "obs_ticker_custom": default_welcome_ticker,
                "restored_session_id": json.dumps(None),
                "restored_session_title": json.dumps(None),
                "broadcast_started_at": json.dumps(None)
            }
            for k, v in state_resets.items():
                cursor.execute("""
                    INSERT OR REPLACE INTO overlay_state (key, value, updated_at)
                    VALUES (?, ?, ?)
                """, (k, v, now_str))

            conn.commit()
        finally:
            conn.close()

        # 4. 엑셀 파일도 빈 헤더 상태로 동기화
        export_orders_to_excel()

        return {
            "archived_session_id": session_id,
            "skipped_duplicate": skipped_duplicate,
            "title": session_title,
            "started_at": started_at,
            "ended_at": now_str,
            "had_data": has_any_data,
            "archived_counts": {
                "chats": total_chats,
                "orders": total_orders,
                "revenue": total_revenue,
                "profit": total_profit,
                "cs": total_cs
            }
        }


def get_broadcast_sessions(limit=50):
    """별도 저장된 이전 방송 세션 목록(요약 지표 포함)을 최신순으로 반환합니다."""
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, title, started_at, ended_at,
                       total_chats, total_orders, total_revenue, total_profit, total_cs
                FROM broadcast_sessions
                ORDER BY id DESC
                LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def get_broadcast_session_detail(session_id):
    """특정 이전 방송 세션의 상세 데이터(주문, 채팅, CS 문의, 추첨, 매출 요약)를 반환합니다."""
    import json
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM broadcast_sessions WHERE id = ?", (session_id,))
            row = cursor.fetchone()
            if not row:
                return None
            d = dict(row)
            try:
                orders = json.loads(d.pop("orders_json", "[]") or "[]")
            except Exception:
                orders = []
            try:
                chats = json.loads(d.pop("chats_json", "[]") or "[]")
            except Exception:
                chats = []
            try:
                cs_list = json.loads(d.pop("cs_json", "[]") or "[]")
            except Exception:
                cs_list = []
            try:
                lotteries = json.loads(d.pop("lottery_json", "[]") or "[]")
            except Exception:
                lotteries = []
            try:
                analytics = json.loads(d.pop("analytics_json", "{}") or "{}")
            except Exception:
                analytics = {}

            d["orders"] = orders
            d["chats"] = chats
            d["cs_inquiries"] = cs_list
            d["lotteries"] = lotteries
            d["analytics"] = analytics
            return d
        finally:
            conn.close()


def restore_broadcast_session(session_id, auto_backup_current=False, mark_restored=True):
    """
    저장된 이전 방송 세션 데이터를 현재 방송 테이블(orders, chats, cs_inquiries, lottery_events)로
    다시 불러와 복원합니다.
    - mark_restored=True(기본값)일 경우, 현재 화면이 '이미 저장된 방송(restored_session_id)'임을 기록하여
      이후 [새로운 방송 시작] 클릭 시 중복(2중) 저장되지 않도록 잠급니다.
    """
    import json
    with db_lock:
        detail = get_broadcast_session_detail(session_id)
        if not detail:
            return {"success": False, "message": "해당 방송 기록을 찾을 수 없습니다."}

        restored_info = get_restored_session_info()
        if auto_backup_current and not restored_info["is_restored"]:
            conn_chk = get_connection()
            try:
                cur_chk = conn_chk.cursor()
                cur_chk.execute("SELECT COUNT(*) FROM chats")
                c_cnt = cur_chk.fetchone()[0]
                cur_chk.execute("SELECT COUNT(*) FROM orders")
                o_cnt = cur_chk.fetchone()[0]
            finally:
                conn_chk.close()
            if c_cnt > 0 or o_cnt > 0:
                archive_and_start_new_broadcast(
                    title=f"{datetime.now().strftime('%Y-%m-%d %H:%M')} 자동 백업 방송",
                    save_empty=False
                )

        conn = get_connection()
        try:
            cursor = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("DELETE FROM orders")
            cursor.execute("DELETE FROM chats")
            cursor.execute("DELETE FROM cs_inquiries")
            cursor.execute("DELETE FROM lottery_events")

            # 1. 주문 복원 (과거순 삽입으로 순서 유지)
            orders = list(reversed(detail.get("orders", [])))
            for o in orders:
                cursor.execute("""
                    INSERT INTO orders (
                        order_time, platform, buyer_name, product_id, item_name,
                        color, size, quantity, unit_price, unit_cost, total_price, profit,
                        raw_message, reply_text, status, confirmed_at, payment_status, bundle_id, memo
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    o.get("order_time", ""),
                    o.get("platform", "LIVE"),
                    o.get("buyer_name", "고객"),
                    o.get("product_id"),
                    o.get("item_name", "상품"),
                    o.get("color", "-"),
                    o.get("size", "-"),
                    int(o.get("quantity", 1)),
                    int(o.get("unit_price", 0)),
                    int(o.get("unit_cost", 0)),
                    int(o.get("total_price", 0)),
                    int(o.get("profit", 0)),
                    o.get("raw_message", ""),
                    o.get("reply_text", ""),
                    o.get("status", "PENDING"),
                    o.get("confirmed_at", ""),
                    o.get("payment_status", "PENDING"),
                    o.get("bundle_id", ""),
                    o.get("memo", "")
                ))

            # 2. 채팅 복원 (과거순 삽입)
            chats = list(reversed(detail.get("chats", [])))
            for c in chats:
                cursor.execute("""
                    INSERT INTO chats (
                        chat_time, platform, sender, message, category, ai_reply, reply_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    c.get("chat_time", ""),
                    c.get("platform", "YOUTUBE"),
                    c.get("sender", "시청자"),
                    c.get("message", ""),
                    c.get("category", "CHAT"),
                    c.get("ai_reply", ""),
                    c.get("reply_by", "AI")
                ))

            # 3. CS 문의 복원 (과거순 삽입)
            cs_list = list(reversed(detail.get("cs_inquiries", [])))
            for cs in cs_list:
                cursor.execute("""
                    INSERT INTO cs_inquiries (
                        inquiry_time, platform, sender, category_type, question, answer, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    cs.get("inquiry_time", ""),
                    cs.get("platform", "YOUTUBE"),
                    cs.get("sender", "시청자"),
                    cs.get("category_type", "일반 문의"),
                    cs.get("question", ""),
                    cs.get("answer", ""),
                    cs.get("status", "답변 완료")
                ))

            # 4. 추첨 이력 복원
            lotteries = list(reversed(detail.get("lotteries", [])))
            for lot in lotteries:
                cursor.execute("""
                    INSERT INTO lottery_events (
                        title, keyword, winner_count, winners_json, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    lot.get("title", "이벤트"),
                    lot.get("keyword", ""),
                    int(lot.get("winner_count", 1)),
                    lot.get("winners_json", "[]") if isinstance(lot.get("winners_json"), str) else json.dumps(lot.get("winners", []), ensure_ascii=False),
                    lot.get("status", "COMPLETED"),
                    lot.get("created_at", "")
                ))

            # 5. 복원된 방송 세션 상태 기록 (중복 저장 및 수정 방지용)
            if mark_restored:
                cursor.execute("""
                    INSERT OR REPLACE INTO overlay_state (key, value, updated_at)
                    VALUES ('restored_session_id', ?, ?)
                """, (json.dumps(int(session_id)), now_str))
                cursor.execute("""
                    INSERT OR REPLACE INTO overlay_state (key, value, updated_at)
                    VALUES ('restored_session_title', ?, ?)
                """, (json.dumps(detail.get("title", ""), ensure_ascii=False), now_str))
            else:
                cursor.execute("""
                    INSERT OR REPLACE INTO overlay_state (key, value, updated_at)
                    VALUES ('restored_session_id', ?, ?)
                """, (json.dumps(None), now_str))
                cursor.execute("""
                    INSERT OR REPLACE INTO overlay_state (key, value, updated_at)
                    VALUES ('restored_session_title', ?, ?)
                """, (json.dumps(None), now_str))

            conn.commit()
        finally:
            conn.close()

        export_orders_to_excel()
        return {
            "success": True,
            "session_id": session_id,
            "title": detail.get("title"),
            "is_restored": mark_restored,
            "restored_counts": {
                "orders": len(orders),
                "chats": len(chats),
                "cs": len(cs_list)
            }
        }


def delete_broadcast_session(session_id):
    """저장된 특정 이전 방송 기록을 삭제합니다."""
    import json
    with db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM broadcast_sessions WHERE id = ?", (session_id,))
            deleted = cursor.rowcount > 0
            # 만약 현재 열람 중인 복원 세션이 삭제되었다면 복원 잠금 해제
            restored_id = get_overlay_state("restored_session_id", None)
            if restored_id is not None and str(restored_id) == str(session_id):
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute("""
                    INSERT OR REPLACE INTO overlay_state (key, value, updated_at)
                    VALUES ('restored_session_id', ?, ?)
                """, (json.dumps(None), now_str))
                cursor.execute("""
                    INSERT OR REPLACE INTO overlay_state (key, value, updated_at)
                    VALUES ('restored_session_title', ?, ?)
                """, (json.dumps(None), now_str))
            conn.commit()
            return deleted
        finally:
            conn.close()


def export_session_orders_to_excel(session_id, excel_path):
    """특정 이전 방송 세션에 보관된 주문 내역을 엑셀 파일로 생성합니다."""
    detail = get_broadcast_session_detail(session_id)
    if not detail:
        return False

    orders = list(reversed(detail.get("orders", [])))
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "이전방송주문목록"

    headers = ["주문시간", "플랫폼", "고객닉네임", "상품명(번호)", "색상", "사이즈", "수량", "판매단가", "원가", "주문이익(마진)", "확인상태", "원본댓글", "AI안내메시지"]
    ws.append(headers)

    status_map = {
        "CONFIRMED": "확인완료(차감)",
        "PENDING": "확인대기(미차감)",
        "REJECTED": "취소/반려"
    }

    for r in orders:
        st_kr = status_map.get(r.get("status"), r.get("status", "PENDING"))
        ws.append([
            r.get("order_time", ""),
            r.get("platform", ""),
            r.get("buyer_name", ""),
            r.get("item_name", ""),
            r.get("color", "-"),
            r.get("size", "-"),
            int(r.get("quantity", 1)),
            int(r.get("unit_price", 0)),
            int(r.get("unit_cost", 0)),
            int(r.get("profit", 0)),
            st_kr,
            r.get("raw_message", ""),
            r.get("reply_text", "")
        ])

    wb.save(excel_path)
    return True


# ==============================================================================
# 🛍️ 독립 쇼핑몰(shopping_mall/shopping_mall.db & Port 6001) 실시간 동기화 브릿지
# ==============================================================================
_pushed_cloud_image_sig = {}


def _normalize_mall_port_url(raw_url: str, default_port: int = 6001) -> str:
    """이전 기본 포트(:3000, :6000)로 저장된 쇼핑몰 URL을 현재 포트(:6001)로 자동 보정합니다."""
    u = str(raw_url or "").strip().rstrip("/")
    if not u:
        return f"http://127.0.0.1:{default_port}"
    for legacy_p in (":3000", ":6000"):
        if u.endswith(legacy_p):
            u = u[:-len(legacy_p)] + f":{default_port}"
        elif f"{legacy_p}/" in u:
            u = u.replace(f"{legacy_p}/", f":{default_port}/", 1)
    return u


def get_cloud_mall_config():
    """클라우드/독립 쇼핑몰 연동 URL 및 시크릿 설정을 반환합니다 (DB 저장값 우선, 환경변수 폴백)."""
    mall_port = int(os.getenv("MALL_PORT", 6001))
    saved = get_overlay_state("cloud_mall_config", None)
    env_url = _normalize_mall_port_url(os.getenv("MALL_SERVER_URL", f"http://127.0.0.1:{mall_port}"), mall_port)
    env_sec = os.getenv("MALL_SYNC_SECRET", "").strip()
    env_agent_only = os.getenv("AGENT_ONLY_MODE", "0").strip() in ("1", "true", "True", "yes")
    if isinstance(saved, dict):
        raw_saved_url = str(saved.get("mall_url") or env_url).strip().rstrip("/")
        url = _normalize_mall_port_url(raw_saved_url, mall_port)
        sec = str(saved.get("sync_secret") if saved.get("sync_secret") is not None else env_sec).strip()
        agent_only = bool(saved.get("agent_only_mode", env_agent_only))
        if url != raw_saved_url:
            try:
                set_overlay_state("cloud_mall_config", {
                    "mall_url": url,
                    "sync_secret": sec,
                    "agent_only_mode": agent_only
                })
            except Exception:
                pass
    else:
        url, sec, agent_only = env_url, env_sec, env_agent_only
    is_remote = bool(url) and not any(h in url for h in ("127.0.0.1", "localhost"))
    return {
        "mall_url": url,
        "sync_secret": sec,
        "agent_only_mode": bool(agent_only or is_remote),
        "is_remote": is_remote
    }


def _read_local_product_image_b64_if_changed(base_dir, img_url, force=False):
    """원격 클라우드 쇼핑몰로 전송할 상품 이미지를 Base64로 읽되, 이미 전송한 동일 파일(mtime+size)은 생략합니다."""
    import base64
    if not img_url or not str(img_url).startswith("/static/uploads/products/"):
        return None, None
    fname = os.path.basename(str(img_url).split("?")[0])
    if not fname:
        return None, None
    src_p = os.path.join(base_dir, "static", "uploads", "products", fname)
    if not os.path.exists(src_p):
        return fname, None
    try:
        st = os.stat(src_p)
        sig = f"{st.st_size}:{int(st.st_mtime)}"
        if not force and _pushed_cloud_image_sig.get(fname) == sig:
            return fname, None
        with open(src_p, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        _pushed_cloud_image_sig[fname] = sig
        return fname, b64
    except Exception:
        return fname, None


def sync_to_shopping_mall(action, payload=None, blocking=False):
    """
    OBS DB의 상품/옵션/재고 변경 또는 선택된(Pin) 상품 변경 시:
    1) 로컬 shopping_mall 폴더가 존재하고 로컬 모드이면 shopping_mall.db에 직접 즉시 반영 및 이미지 파일 복사
    2) 원격 클라우드 또는 로컬 쇼핑몰 서버(MALL_SERVER_URL)로 X-Mall-Sync-Secret 인증 헤더와 함께 Outbound 웹훅 푸시
    """
    import json
    import shutil
    import threading
    import urllib.request

    cfg = get_cloud_mall_config()
    mall_url = cfg["mall_url"]
    sync_secret = cfg["sync_secret"]
    is_remote = cfg["is_remote"]

    payload = dict(payload or {})
    payload["action"] = action
    base_dir = os.path.dirname(os.path.abspath(__file__))
    mall_dir = os.path.join(base_dir, "shopping_mall")

    # 1. 로컬 동시 운영 모드일 때만 로컬 shopping_mall DB 및 이미지 파일 직접 동기화
    if os.path.isdir(mall_dir) and not is_remote:
        try:
            if mall_dir not in sys.path:
                sys.path.append(mall_dir)
            import mall_database as mdb

            def _copy_local_img(img_url):
                if not img_url or not str(img_url).startswith("/static/uploads/products/"):
                    return img_url
                fname = os.path.basename(str(img_url).split("?")[0])
                src_p = os.path.join(base_dir, "static", "uploads", "products", fname)
                dst_dir = os.path.join(mall_dir, "static", "uploads", "products")
                os.makedirs(dst_dir, exist_ok=True)
                dst_p = os.path.join(dst_dir, fname)
                if os.path.exists(src_p) and not os.path.exists(dst_p):
                    shutil.copy2(src_p, dst_p)
                return f"/static/uploads/products/{fname}"

            if action == "upsert_product" and isinstance(payload.get("product"), dict):
                p = dict(payload["product"])
                p["image_url"] = _copy_local_img(p.get("image_url"))
                mdb.upsert_product_from_obs(p)
            elif action == "bulk_sync" and isinstance(payload.get("products"), list):
                prods = []
                for p in payload["products"]:
                    cp = dict(p)
                    cp["image_url"] = _copy_local_img(cp.get("image_url"))
                    prods.append(cp)
                mdb.bulk_sync_products_from_obs(prods, delete_missing=True)
                if isinstance(payload.get("live_state"), dict):
                    mdb.update_live_state_bundle(payload["live_state"])
            elif action == "delete_product":
                mdb.delete_product_by_num_or_obs_id(
                    num=payload.get("num"),
                    obs_product_id=payload.get("obs_product_id")
                )
            elif action in ("pin_product", "live_state", "heartbeat"):
                state_bundle = {
                    k: payload[k]
                    for k in (
                        "pinned_product_num",
                        "is_live",
                        "broadcast_title",
                        "ticker_message",
                        "timer",
                        "live_urls",
                        "youtube_embed_url",
                        "youtube_video_id",
                        "heartbeat"
                    )
                    if k in payload
                }
                if action == "heartbeat":
                    state_bundle["heartbeat"] = True
                if state_bundle:
                    mdb.update_live_state_bundle(state_bundle)
        except Exception as local_sync_err:
            print(f"⚠️ [로컬 쇼핑몰 DB 동기화 경고] {local_sync_err}")

    # 2. 쇼핑몰 HTTP 서버(원격 클라우드 또는 로컬 Port 3000)로 Outbound 웹훅 전송
    if mall_url:
        # 원격 클라우드 서버인 경우 변경된 로컬 상품 이미지를 Base64로 함께 첨부
        if is_remote or not os.path.isdir(mall_dir):
            if action == "upsert_product" and isinstance(payload.get("product"), dict):
                _, b64 = _read_local_product_image_b64_if_changed(base_dir, payload["product"].get("image_url"))
                if b64:
                    payload["image_b64"] = b64
            elif action == "bulk_sync" and isinstance(payload.get("products"), list):
                images_b64 = dict(payload.get("images_b64") or {})
                for p in payload["products"]:
                    if isinstance(p, dict):
                        fname, b64 = _read_local_product_image_b64_if_changed(base_dir, p.get("image_url"))
                        if fname and b64:
                            images_b64[fname] = b64
                if images_b64:
                    payload["images_b64"] = images_b64

        def _push_webhook():
            try:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                headers = {"Content-Type": "application/json"}
                if sync_secret:
                    headers["X-Mall-Sync-Secret"] = sync_secret
                req = urllib.request.Request(
                    f"{mall_url}/api/sync/from-obs",
                    data=body,
                    headers=headers,
                    method="POST"
                )
                urllib.request.urlopen(req, timeout=3.5).read()
                return True
            except Exception:
                return False

        if blocking:
            return _push_webhook()
        threading.Thread(target=_push_webhook, daemon=True).start()
    return True


def sync_all_products_to_shopping_mall(is_live=False, live_urls=None, youtube_embed_url="", blocking=False):
    """OBS DB에 등록된 전체 상품 및 현재 핀 고정 상품 번호를 쇼핑몰 DB로 일괄 동기화합니다."""
    all_prods = get_all_products()
    pinned_num = get_overlay_state("pinned_product_num", None)
    title = get_overlay_state("simulcast_unified_title", "실시간 라이브 커머스 특가 방송")
    ticker = get_overlay_state("obs_ticker_custom", "")
    timer = get_overlay_state("flash_timer", {"active": False})
    live_state_payload = {
        "is_live": bool(is_live),
        "pinned_product_num": pinned_num,
        "broadcast_title": title,
        "ticker_message": ticker,
        "timer": timer,
        "heartbeat": bool(is_live)
    }
    if live_urls is not None:
        live_state_payload["live_urls"] = live_urls
    if youtube_embed_url:
        live_state_payload["youtube_embed_url"] = youtube_embed_url
    sync_to_shopping_mall("bulk_sync", {
        "products": all_prods,
        "delete_missing": True,
        "live_state": live_state_payload
    }, blocking=blocking)
    return len(all_prods)


# ==========================================
# 🔐 OBS 스튜디오 방송자 회원 · 초대코드 · 보안 감사로그 API
# ==========================================

import time as _time
import secrets as _secrets
import security_core


def log_security_audit(action: str, actor_id: str = "", actor_type: str = "USER", ip_address: str = "", detail: str = ""):
    """보안 감사 로그를 기록합니다."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO security_audit_logs (event_time, actor_type, actor_id, ip_address, action, detail)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (now_str, str(actor_type or "USER"), str(actor_id or ""), str(ip_address or ""), str(action), str(detail or "")))
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()


def list_security_audit_logs(limit: int = 80):
    with db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM security_audit_logs ORDER BY id DESC LIMIT ?", (int(limit),))
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def ensure_default_studio_admin():
    """
    최초 기동 시 OBS 스튜디오 관리자 계정을 보장합니다.
    - 환경변수 STUDIO_ADMIN_USERNAME / STUDIO_ADMIN_EMAIL / STUDIO_ADMIN_PASSWORD 우선 사용
    - 미설정 시 .studio_admin_credentials.txt에 난수 비밀번호를 1회 생성·보관하여 하드코딩 취약점을 방지합니다.
    """
    admin_user = (os.getenv("STUDIO_ADMIN_USERNAME") or "admin").strip().lower()
    admin_email = (os.getenv("STUDIO_ADMIN_EMAIL") or "admin@studio.local").strip().lower()
    admin_pw = (os.getenv("STUDIO_ADMIN_PASSWORD") or "").strip()
    cred_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".studio_admin_credentials.txt")

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
            admin_pw = "Admin!" + _secrets.token_urlsafe(9)
            try:
                with open(cred_file, "w", encoding="utf-8") as f:
                    f.write(f"USERNAME={admin_user}\nEMAIL={admin_email}\nPASSWORD={admin_pw}\n")
                print(f"🔐 [OBS 스튜디오 초기 관리자 생성] ID: {admin_user} ({admin_email}) | 초기 비밀번호는 .studio_admin_credentials.txt 에 저장되었습니다.")
            except Exception:
                pass

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, role FROM studio_users WHERE username = ? OR email = ?", (admin_user, admin_email))
            row = cur.fetchone()
            if not row:
                pw_hash = security_core.hash_password(admin_pw)
                cur.execute("""
                    INSERT INTO studio_users (username, email, password_hash, display_name, phone, role, status, approved_at, created_at)
                    VALUES (?, ?, ?, ?, '', 'ADMIN', 'APPROVED', ?, ?)
                """, (admin_user, admin_email, pw_hash, "스튜디오 총괄관리자", now_str, now_str))
            elif os.getenv("STUDIO_ADMIN_PASSWORD"):
                pw_hash = security_core.hash_password(admin_pw)
                cur.execute("""
                    UPDATE studio_users
                    SET password_hash = ?, role = 'ADMIN', status = 'APPROVED'
                    WHERE id = ?
                """, (pw_hash, row["id"]))

            # 기본 초대 코드(환경변수 STUDIO_DEFAULT_INVITE_CODE 설정 시) 보장
            default_inv = (os.getenv("STUDIO_DEFAULT_INVITE_CODE") or "").strip()
            if default_inv:
                cur.execute("SELECT id FROM studio_invite_codes WHERE code = ?", (default_inv,))
                if not cur.fetchone():
                    cur.execute("""
                        INSERT INTO studio_invite_codes (code, memo, max_uses, used_count, is_active, created_by, created_at)
                        VALUES (?, '환경변수 기본 초대코드', 9999, 0, 1, 'SYSTEM', ?)
                    """, (default_inv, now_str))
            conn.commit()
        finally:
            conn.close()


def create_studio_user(username: str, email: str, password: str, display_name: str, phone: str = "", invite_code: str = "", ip_address: str = "") -> dict:
    """
    OBS 스튜디오 신규 방송자 회원가입을 처리합니다.
    - 유효한 초대 코드(invite_code) 입력 시 즉시 APPROVED 활성화
    - 초대 코드 미입력 시 PENDING(관리자 승인 대기) 상태로 저장
    """
    uname = str(username or "").strip().lower()
    mail = str(email or "").strip().lower()
    dname = str(display_name or "").strip() or uname
    ph = str(phone or "").strip()
    inv = str(invite_code or "").strip()

    if len(uname) < 3 or len(uname) > 40:
        return {"ok": False, "message": "아이디는 3~40자 사이로 입력해 주세요."}
    if "@" not in mail or "." not in mail:
        return {"ok": False, "message": "유효한 이메일 주소를 입력해 주세요."}

    valid_pw, pw_msg = security_core.validate_password_policy(password, min_len=8)
    if not valid_pw:
        return {"ok": False, "message": pw_msg}

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM studio_users WHERE username = ? OR email = ?", (uname, mail))
            if cur.fetchone():
                return {"ok": False, "message": "이미 사용 중인 아이디 또는 이메일입니다."}

            initial_status = "PENDING"
            approved_at = ""
            if inv:
                cur.execute("""
                    SELECT id, max_uses, used_count, is_active, expires_at
                    FROM studio_invite_codes
                    WHERE code = ?
                """, (inv,))
                inv_row = cur.fetchone()
                if not inv_row or not inv_row["is_active"]:
                    return {"ok": False, "message": "유효하지 않거나 비활성화된 초대 코드입니다."}
                if int(inv_row["used_count"]) >= int(inv_row["max_uses"]):
                    return {"ok": False, "message": "사용 한도가 초과된 초대 코드입니다."}
                exp_str = str(inv_row["expires_at"] or "").strip()
                if exp_str and exp_str < now_str:
                    return {"ok": False, "message": "만료된 초대 코드입니다."}
                cur.execute("UPDATE studio_invite_codes SET used_count = used_count + 1 WHERE id = ?", (inv_row["id"],))
                initial_status = "APPROVED"
                approved_at = now_str

            pw_hash = security_core.hash_password(password)
            cur.execute("""
                INSERT INTO studio_users (username, email, password_hash, display_name, phone, role, status, approved_at, created_at)
                VALUES (?, ?, ?, ?, ?, 'BROADCASTER', ?, ?, ?)
            """, (uname, mail, pw_hash, dname, ph, initial_status, approved_at, now_str))
            user_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()

    log_security_audit(
        action="STUDIO_REGISTER",
        actor_id=uname,
        ip_address=ip_address,
        detail=f"status={initial_status}, invite={'YES' if inv else 'NO'}"
    )
    return {
        "ok": True,
        "user_id": user_id,
        "username": uname,
        "email": mail,
        "display_name": dname,
        "role": "BROADCASTER",
        "status": initial_status
    }


def authenticate_studio_user(login_id: str, password: str, ip_address: str = "") -> dict:
    """OBS 스튜디오 회원 로그인을 검증합니다 (5회 실패 시 10분 계정 잠금)."""
    ident = str(login_id or "").strip().lower()
    if not ident or not password:
        return {"ok": False, "code": "INVALID", "message": "아이디(또는 이메일)와 비밀번호를 입력해 주세요."}

    now_ts = _time.time()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, username, email, password_hash, display_name, phone, role, status, failed_login_count, locked_until
                FROM studio_users
                WHERE username = ? OR email = ?
            """, (ident, ident))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "code": "INVALID", "message": "아이디 또는 비밀번호가 올바르지 않습니다."}

            locked_until = float(row["locked_until"] or 0.0)
            if locked_until > now_ts:
                remain_sec = max(1, int(round(locked_until - now_ts)))
                return {
                    "ok": False,
                    "code": "LOCKED",
                    "message": f"비밀번호 5회 연속 오류로 계정이 잠겼습니다. {remain_sec}초 후 다시 시도해 주세요."
                }

            if not security_core.verify_password(row["password_hash"], password):
                fails = int(row["failed_login_count"] or 0) + 1
                new_lock = (now_ts + 600.0) if fails >= 5 else 0.0
                cur.execute("""
                    UPDATE studio_users
                    SET failed_login_count = ?, locked_until = ?
                    WHERE id = ?
                """, (fails, new_lock, row["id"]))
                conn.commit()
                log_security_audit("STUDIO_LOGIN_FAIL", actor_id=row["username"], ip_address=ip_address, detail=f"fails={fails}")
                if new_lock > 0:
                    return {"ok": False, "code": "LOCKED", "message": "비밀번호 5회 오류로 10분간 로그인이 제한됩니다."}
                return {"ok": False, "code": "INVALID", "message": "아이디 또는 비밀번호가 올바르지 않습니다."}

            cur.execute("""
                UPDATE studio_users
                SET failed_login_count = 0, locked_until = 0, last_login_at = ?
                WHERE id = ?
            """, (now_str, row["id"]))
            conn.commit()
            user_dict = {
                "id": row["id"],
                "username": row["username"],
                "email": row["email"],
                "display_name": row["display_name"],
                "phone": row["phone"],
                "role": row["role"],
                "status": row["status"]
            }
        finally:
            conn.close()

    log_security_audit("STUDIO_LOGIN_OK", actor_id=user_dict["username"], ip_address=ip_address, detail=f"status={user_dict['status']}")
    return {"ok": True, "user": user_dict}


def get_studio_user_by_id(user_id: int):
    if not user_id:
        return None
    with db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, username, email, display_name, phone, role, status, last_login_at, approved_at, created_at
                FROM studio_users
                WHERE id = ?
            """, (int(user_id),))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def list_studio_users():
    with db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, username, email, display_name, phone, role, status, failed_login_count, last_login_at, approved_at, created_at
                FROM studio_users
                ORDER BY CASE status WHEN 'PENDING' THEN 0 WHEN 'APPROVED' THEN 1 ELSE 2 END, id DESC
            """)
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def update_studio_user_status(user_id: int, new_status: str, new_role: str = None, actor_id: str = "", ip_address: str = "") -> bool:
    allowed_status = {"PENDING", "APPROVED", "REJECTED", "SUSPENDED"}
    st = str(new_status or "").upper()
    if st not in allowed_status:
        return False
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            if new_role and str(new_role).upper() in ("BROADCASTER", "ADMIN"):
                cur.execute("""
                    UPDATE studio_users
                    SET status = ?, role = ?, approved_at = CASE WHEN ? = 'APPROVED' THEN ? ELSE approved_at END,
                        failed_login_count = 0, locked_until = 0
                    WHERE id = ?
                """, (st, str(new_role).upper(), st, now_str, int(user_id)))
            else:
                cur.execute("""
                    UPDATE studio_users
                    SET status = ?, approved_at = CASE WHEN ? = 'APPROVED' THEN ? ELSE approved_at END,
                        failed_login_count = 0, locked_until = 0
                    WHERE id = ?
                """, (st, st, now_str, int(user_id)))
            updated = cur.rowcount > 0
            conn.commit()
        finally:
            conn.close()
    if updated:
        log_security_audit("STUDIO_USER_STATUS_CHANGE", actor_id=actor_id, ip_address=ip_address, detail=f"target_id={user_id}, status={st}, role={new_role or ''}")
    return updated


def create_studio_invite_code(code: str = "", memo: str = "", max_uses: int = 10, created_by: str = "ADMIN") -> dict:
    code_str = (str(code or "").strip().upper()) or ("OBS-" + _secrets.token_hex(4).upper())
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO studio_invite_codes (code, memo, max_uses, used_count, is_active, created_by, created_at)
                VALUES (?, ?, ?, 0, 1, ?, ?)
            """, (code_str, str(memo or "").strip(), max(1, int(max_uses or 10)), str(created_by or "ADMIN"), now_str))
            conn.commit()
            return {"ok": True, "code": code_str}
        except Exception as e:
            return {"ok": False, "message": f"초대 코드 생성 실패: {e}"}
        finally:
            conn.close()


def list_studio_invite_codes():
    with db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM studio_invite_codes ORDER BY id DESC")
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def toggle_studio_invite_code(code_id: int, is_active: bool) -> bool:
    with db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE studio_invite_codes SET is_active = ? WHERE id = ?", (1 if is_active else 0, int(code_id)))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def change_studio_user_password(user_id: int, new_password: str, actor_id: str = "", ip_address: str = "") -> dict:
    valid_pw, pw_msg = security_core.validate_password_policy(new_password, min_len=8)
    if not valid_pw:
        return {"ok": False, "message": pw_msg}
    pw_hash = security_core.hash_password(new_password)
    with db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE studio_users
                SET password_hash = ?, failed_login_count = 0, locked_until = 0
                WHERE id = ?
            """, (pw_hash, int(user_id)))
            ok = cur.rowcount > 0
            conn.commit()
        finally:
            conn.close()
    if ok:
        log_security_audit("STUDIO_PASSWORD_CHANGE", actor_id=actor_id, ip_address=ip_address, detail=f"target_id={user_id}")
        return {"ok": True, "message": "비밀번호가 성공적으로 변경되었습니다."}
    return {"ok": False, "message": "대상 계정을 찾을 수 없습니다."}


def delete_studio_user(user_id: int, actor_id: str = "", ip_address: str = "") -> dict:
    with db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, username, role FROM studio_users WHERE id = ?", (int(user_id),))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "message": "대상 회원을 찾을 수 없습니다."}
            if row["role"] == "ADMIN":
                cur.execute("SELECT COUNT(*) FROM studio_users WHERE role = 'ADMIN' AND status = 'APPROVED'")
                admin_cnt = cur.fetchone()[0]
                if admin_cnt <= 1:
                    return {"ok": False, "message": "최소 1명의 활성 관리자(ADMIN) 계정은 유지되어야 합니다."}
            cur.execute("DELETE FROM studio_users WHERE id = ?", (int(user_id),))
            conn.commit()
            deleted_uname = row["username"]
        finally:
            conn.close()
    log_security_audit("STUDIO_USER_DELETE", actor_id=actor_id, ip_address=ip_address, detail=f"deleted={deleted_uname} (id={user_id})")
    return {"ok": True, "message": f"계정({deleted_uname})이 삭제되었습니다."}


def create_studio_user_by_admin(username: str, email: str, password: str, display_name: str, phone: str = "", role: str = "BROADCASTER", status: str = "APPROVED", actor_id: str = "admin", ip_address: str = "") -> dict:
    uname = str(username or "").strip().lower()
    mail = str(email or "").strip().lower()
    dname = str(display_name or "").strip() or uname
    ph = str(phone or "").strip()
    r_val = "ADMIN" if str(role or "").upper() == "ADMIN" else "BROADCASTER"
    s_val = str(status or "APPROVED").upper()
    if s_val not in ("PENDING", "APPROVED", "REJECTED", "SUSPENDED"):
        s_val = "APPROVED"

    if len(uname) < 3 or "@" not in mail:
        return {"ok": False, "message": "아이디(3자 이상)와 유효한 이메일을 입력해 주세요."}
    valid_pw, pw_msg = security_core.validate_password_policy(password, min_len=8)
    if not valid_pw:
        return {"ok": False, "message": pw_msg}

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM studio_users WHERE username = ? OR email = ?", (uname, mail))
            if cur.fetchone():
                return {"ok": False, "message": "이미 등록된 아이디 또는 이메일입니다."}
            pw_hash = security_core.hash_password(password)
            cur.execute("""
                INSERT INTO studio_users (username, email, password_hash, display_name, phone, role, status, approved_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (uname, mail, pw_hash, dname, ph, r_val, s_val, now_str if s_val == "APPROVED" else "", now_str))
            uid = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
    log_security_audit("STUDIO_ADMIN_CREATE_USER", actor_id=actor_id, ip_address=ip_address, detail=f"created={uname}, role={r_val}, status={s_val}")
    return {"ok": True, "user_id": uid, "username": uname}


# 모듈 임포트 시 DB 자동 초기화 (테이블 없으면 자동 생성) 및 기존 중복 저장된 방송 정리
init_db(reset=False)
ensure_default_studio_admin()
deduplicate_broadcast_sessions()
try:
    sync_all_products_to_shopping_mall(is_live=False)
except Exception:
    pass
