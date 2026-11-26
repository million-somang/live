# -*- coding: utf-8 -*-
"""
쇼핑몰 쿠폰(웰컴 쿠폰·라이브 전용 할인 코드) 및 적립금(포인트) 서비스 + Blueprint (mall_promotions.py)
- 단독 배포 경계 준수: c:\\live\\ 상위 모듈 임포트 금지
- 주문 적용/배송완료 적립/취소·환불 복구 훅은 mall_database.mall_db_lock 트랜잭션 내부에서 호출됩니다.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any, Optional

from flask import Blueprint, current_app, jsonify, request, session

import mall_database
import mall_security

promotions_bp = Blueprint("promotions_bp", __name__)

_COUPON_CODE_RE = re.compile(r"^[A-Z0-9_-]{2,40}$")


def _now_dt() -> datetime:
    return datetime.now()


def _now_str() -> str:
    return _now_dt().strftime("%Y-%m-%d %H:%M:%S")


def _resolve_current_user() -> Optional[dict]:
    fn = current_app.config.get("MALL_GET_CURRENT_USER")
    if callable(fn):
        return fn()
    uid = session.get("mall_user_id")
    if uid:
        return mall_database.get_mall_user_by_id(int(uid))
    return None


def _read_request_payload() -> dict:
    if request.is_json:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}
    return request.form.to_dict()


def parse_strict_non_neg_int(
    raw_val: Any,
    field_name: str = "값",
    max_val: Optional[int] = None,
    default: Optional[int] = None,
) -> tuple[bool, Optional[int], str]:
    effective_max = (
        mall_database.SQLITE_INT64_MAX
        if max_val is None
        else min(int(max_val), mall_database.SQLITE_INT64_MAX)
    )
    if raw_val is None or (isinstance(raw_val, str) and raw_val.strip() == ""):
        if default is not None:
            return True, int(default), ""
        return False, None, f"{field_name}을(를) 입력해 주세요."
    if isinstance(raw_val, bool):
        return False, None, f"{field_name}은(는) 0 이상의 정수로 입력해 주세요."
    if isinstance(raw_val, float):
        if math.isnan(raw_val) or math.isinf(raw_val) or not raw_val.is_integer():
            return False, None, f"{field_name}은(는) 0 이상의 정수로 입력해 주세요."
        parsed = int(raw_val)
    elif isinstance(raw_val, int):
        parsed = int(raw_val)
    else:
        s = str(raw_val).strip().replace(",", "")
        if not re.fullmatch(r"-?[0-9]+", s):
            return False, None, f"{field_name}은(는) 0 이상의 정수로 입력해 주세요."
        is_neg = s.startswith("-")
        digits = (s[1:] if is_neg else s).lstrip("0") or "0"
        if is_neg and digits != "0":
            return False, None, f"{field_name}은(는) 0 이상이어야 합니다."
        if len(digits) > 19:
            if max_val is not None:
                return False, None, f"{field_name}은(는) {effective_max:,} 이하로 입력해 주세요."
            return False, None, f"{field_name}이(가) 허용 범위를 초과했습니다."
        try:
            parsed = int(digits)
        except ValueError:
            return False, None, f"{field_name}이(가) 허용 범위를 초과했습니다."

    if parsed < 0:
        return False, None, f"{field_name}은(는) 0 이상이어야 합니다."
    if parsed > effective_max:
        if max_val is not None:
            return False, None, f"{field_name}은(는) {effective_max:,} 이하로 입력해 주세요."
        return False, None, f"{field_name}이(가) 허용 범위를 초과했습니다."
    return True, parsed, ""


def parse_strict_bool(val: Any, default: bool = False) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return int(val) != 0
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off", ""):
        return False
    return default


def _parse_datetime_bound(raw_val: Any, is_end: bool = False) -> Optional[datetime]:
    s = str(raw_val or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        d = datetime.strptime(s[:10], "%Y-%m-%d")
        if is_end:
            return d.replace(hour=23, minute=59, second=59)
        return d.replace(hour=0, minute=0, second=0)
    except ValueError:
        return None


def _normalize_date_str(raw_val: Any) -> str:
    s = str(raw_val or "").strip()
    if not s:
        return ""
    return s[:32]


def ensure_promotions_schema(conn) -> None:
    """mall_coupons, mall_user_coupons, mall_point_logs 테이블과 회원/주문 프로모션 컬럼을 보장합니다."""
    cur = conn.cursor()

    for tbl, col_name, col_def in [
        ("mall_users", "points_balance", "INTEGER NOT NULL DEFAULT 0"),
        ("mall_orders", "coupon_code", "TEXT NOT NULL DEFAULT ''"),
        ("mall_orders", "coupon_discount", "INTEGER NOT NULL DEFAULT 0"),
        ("mall_orders", "points_used", "INTEGER NOT NULL DEFAULT 0"),
        ("mall_orders", "points_earned", "INTEGER NOT NULL DEFAULT 0"),
        ("mall_orders", "points_awarded", "INTEGER NOT NULL DEFAULT 0"),
        ("mall_orders", "points_refunded", "INTEGER NOT NULL DEFAULT 0"),
        ("mall_orders", "coupon_restored", "INTEGER NOT NULL DEFAULT 0"),
        ("mall_orders", "points_revoked", "INTEGER NOT NULL DEFAULT 0"),
    ]:
        try:
            cur.execute(f"ALTER TABLE {tbl} ADD COLUMN {col_name} {col_def}")
        except Exception:
            pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS mall_coupons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            discount_type TEXT NOT NULL DEFAULT 'FIXED',
            discount_value INTEGER NOT NULL DEFAULT 0,
            max_discount INTEGER NOT NULL DEFAULT 0,
            min_order_amount INTEGER NOT NULL DEFAULT 0,
            live_only INTEGER NOT NULL DEFAULT 0,
            usage_limit INTEGER NOT NULL DEFAULT 0,
            used_count INTEGER NOT NULL DEFAULT 0,
            start_at TEXT NOT NULL DEFAULT '',
            end_at TEXT NOT NULL DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1,
            is_welcome_auto INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS mall_user_coupons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            coupon_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'AVAILABLE',
            used_order_id INTEGER DEFAULT NULL,
            used_order_code TEXT NOT NULL DEFAULT '',
            issued_at TEXT NOT NULL,
            used_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES mall_users (id) ON DELETE CASCADE,
            FOREIGN KEY (coupon_id) REFERENCES mall_coupons (id) ON DELETE CASCADE,
            UNIQUE(user_id, coupon_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS mall_point_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            delta INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            reason TEXT NOT NULL,
            order_code TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES mall_users (id) ON DELETE CASCADE
        )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_coupons_code ON mall_coupons (code)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_user_coupons_user ON mall_user_coupons (user_id, status, id DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_point_logs_user ON mall_point_logs (user_id, id DESC)")

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
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_restock_alerts_lookup ON mall_restock_alerts (product_num, status, id DESC)")

    cur.execute("SELECT COUNT(*) FROM mall_coupons")
    if int(cur.fetchone()[0] or 0) == 0:
        cur.execute("""
            INSERT INTO mall_coupons
            (code, name, discount_type, discount_value, max_discount, min_order_amount,
             live_only, usage_limit, used_count, start_at, end_at, is_active, is_welcome_auto, created_at)
            VALUES (?, ?, 'FIXED', 3000, 0, 0, 0, 0, 0, '', '', 1, 1, ?)
        """, ("WELCOME3000", "신규 회원 웰컴 3,000원 할인 쿠폰", _now_str()))


def _format_coupon_row(row) -> dict[str, Any]:
    d = dict(row)
    max_d = max(0, int(d.get("max_discount") or 0))
    return {
        "id": int(d["id"]),
        "code": str(d.get("code") or "").strip().upper(),
        "name": str(d.get("name") or "").strip(),
        "discount_type": str(d.get("discount_type") or "FIXED").strip().upper(),
        "discount_value": max(0, int(d.get("discount_value") or 0)),
        "max_discount": max_d,
        "max_discount_amount": max_d,
        "min_order_amount": max(0, int(d.get("min_order_amount") or 0)),
        "live_only": bool(int(d.get("live_only") or 0)),
        "usage_limit": max(0, int(d.get("usage_limit") or 0)),
        "used_count": max(0, int(d.get("used_count") or 0)),
        "start_at": str(d.get("start_at") or ""),
        "end_at": str(d.get("end_at") or ""),
        "is_active": bool(int(d.get("is_active") or 0)),
        "is_welcome_auto": bool(int(d.get("is_welcome_auto") or 0)),
        "created_at": str(d.get("created_at") or ""),
    }


def issue_welcome_coupons_to_user(conn, user_id: int, now_str: Optional[str] = None) -> list[dict[str, Any]]:
    """신규 가입 회원에게 활성화된 웰컴 자동 발급 쿠폰을 모두 지급합니다."""
    ts = now_str or _now_str()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, code FROM mall_coupons
        WHERE is_active = 1 AND is_welcome_auto = 1
        ORDER BY id ASC
    """)
    rows = cur.fetchall()
    issued: list[dict[str, Any]] = []
    for r in rows:
        cur.execute("""
            INSERT OR IGNORE INTO mall_user_coupons
            (user_id, coupon_id, code, status, used_order_id, used_order_code, issued_at, used_at)
            VALUES (?, ?, ?, 'AVAILABLE', NULL, '', ?, '')
        """, (int(user_id), int(r["id"]), str(r["code"]).strip().upper(), ts))
        issued.append({"coupon_id": int(r["id"]), "code": str(r["code"]).strip().upper()})
    return issued


def validate_and_calculate_coupon(
    conn,
    code: str,
    subtotal: int,
    source_page: str = "STORE",
    user_id: Optional[int] = None,
    now_dt: Optional[datetime] = None,
) -> tuple[bool, dict[str, Any] | str]:
    clean_code = str(code or "").strip().upper()
    if not clean_code:
        return False, "쿠폰 코드를 입력해 주세요."

    src = str(source_page or "STORE").strip().upper()
    ok_sub, sub_val, sub_err = parse_strict_non_neg_int(subtotal, "주문 상품 금액", default=0)
    if not ok_sub or sub_val is None:
        return False, sub_err or "유효하지 않은 주문 금액입니다."

    cur = conn.cursor()
    cur.execute("SELECT * FROM mall_coupons WHERE UPPER(code) = ? LIMIT 1", (clean_code,))
    row = cur.fetchone()
    if not row:
        return False, "존재하지 않는 쿠폰 코드입니다."

    coupon = _format_coupon_row(row)
    if not coupon["is_active"]:
        return False, "현재 사용이 중지된 쿠폰입니다."

    ref_dt = now_dt or _now_dt()
    if coupon["start_at"]:
        start_dt = _parse_datetime_bound(coupon["start_at"], is_end=False)
        if start_dt and ref_dt < start_dt:
            return False, "아직 사용 기간이 시작되지 않은 쿠폰입니다."
    if coupon["end_at"]:
        end_dt = _parse_datetime_bound(coupon["end_at"], is_end=True)
        if end_dt and ref_dt > end_dt:
            return False, "사용 기간이 만료된 쿠폰입니다."

    if coupon["usage_limit"] > 0 and coupon["used_count"] >= coupon["usage_limit"]:
        return False, "선착순 발급/사용 수량이 모두 소진된 쿠폰입니다."

    if coupon["live_only"] and src != "LIVE":
        return False, "라이브 방송 전용관(/live) 주문에서만 사용할 수 있는 할인 코드입니다."

    if sub_val < coupon["min_order_amount"]:
        return False, f"최소 주문 금액 {coupon['min_order_amount']:,}원 이상일 때 사용할 수 있습니다."

    uid = mall_database.parse_strict_int64(user_id, min_val=1)
    if coupon["is_welcome_auto"]:
        if not uid:
            return False, "웰컴 쿠폰은 회원 로그인 후 사용하실 수 있습니다."
        cur.execute(
            "SELECT status FROM mall_user_coupons WHERE user_id = ? AND coupon_id = ? LIMIT 1",
            (uid, coupon["id"]),
        )
        uc_row = cur.fetchone()
        if not uc_row:
            return False, "보유하지 않은 웰컴 쿠폰입니다."
        if str(uc_row["status"] or "").upper() != "AVAILABLE":
            return False, "이미 사용 완료되었거나 만료된 쿠폰입니다."

    if uid:
        cur.execute(
            "SELECT status FROM mall_user_coupons WHERE user_id = ? AND coupon_id = ? LIMIT 1",
            (uid, coupon["id"]),
        )
        uc_row = cur.fetchone()
        if uc_row and str(uc_row["status"] or "").upper() != "AVAILABLE":
            return False, "이미 사용 완료되었거나 만료된 쿠폰입니다."
        cur.execute(
            """
            SELECT id FROM mall_orders
            WHERE user_id = ?
              AND UPPER(COALESCE(coupon_code, '')) = ?
              AND COALESCE(order_status, '') NOT IN ('주문취소', '환불완료')
              AND COALESCE(payment_status, '') != '환불완료'
            LIMIT 1
            """,
            (uid, coupon["code"]),
        )
        if cur.fetchone():
            return False, "이미 해당 쿠폰을 사용한 주문 이력이 있습니다."

    if coupon["discount_type"] == "PERCENT":
        raw_disc = (sub_val * coupon["discount_value"]) // 100
        if coupon["max_discount"] > 0:
            raw_disc = min(raw_disc, coupon["max_discount"])
        discount_amount = min(sub_val, max(0, raw_disc))
    else:
        discount_amount = min(sub_val, max(0, coupon["discount_value"]))

    result = dict(coupon)
    result["discount_amount"] = int(discount_amount)
    return True, result


def prepare_order_promotions_tx(
    conn,
    *,
    user_id: Optional[int],
    subtotal: int,
    source_page: str,
    coupon_code: Any,
    points_used_raw: Any,
    point_earn_rate: Any,
) -> tuple[bool, dict[str, Any] | str]:
    """주문 생성 트랜잭션(mall_db_lock 내부)에서 쿠폰·적립금 유효성과 금액을 검증·계산합니다."""
    uid = mall_database.parse_strict_int64(user_id, min_val=1)
    clean_code = str(coupon_code or "").strip().upper()
    coupon_id: Optional[int] = None
    coupon_discount = 0

    if clean_code:
        ok_c, c_res = validate_and_calculate_coupon(
            conn,
            code=clean_code,
            subtotal=subtotal,
            source_page=source_page,
            user_id=uid,
        )
        if not ok_c:
            return False, str(c_res)
        assert isinstance(c_res, dict)
        coupon_id = int(c_res["id"])
        clean_code = str(c_res["code"])
        coupon_discount = int(c_res["discount_amount"])

    ok_pts, pts_used, pts_err = parse_strict_non_neg_int(points_used_raw, "사용 적립금", default=0)
    if not ok_pts or pts_used is None:
        return False, pts_err or "사용 적립금은 0 이상의 정수로 입력해 주세요."

    cur = conn.cursor()
    if pts_used > 0:
        if not uid:
            return False, "적립금(포인트) 사용은 회원 로그인 후 이용할 수 있습니다."
        cur.execute(
            "SELECT points_balance FROM mall_users WHERE id = ? AND is_active = 1 AND COALESCE(withdrawn_at, '') = '' LIMIT 1",
            (uid,),
        )
        urow = cur.fetchone()
        if not urow:
            return False, "활성 회원 계정을 찾을 수 없습니다."
        bal = max(0, int(urow["points_balance"] or 0))
        if pts_used > bal:
            return False, f"보유 적립금({bal:,}P)을 초과하여 사용할 수 없습니다."
        after_coupon = max(0, int(subtotal) - coupon_discount)
        if pts_used > after_coupon:
            return False, f"적립금 사용 금액은 쿠폰 할인 후 상품 금액({after_coupon:,}원)을 초과할 수 없습니다."

    try:
        rate_f = float(point_earn_rate if point_earn_rate is not None else 0.0)
    except (TypeError, ValueError):
        rate_f = 0.0
    if rate_f < 0:
        rate_f = 0.0
    if rate_f > 100:
        rate_f = 100.0
    points_earned = int(int(subtotal) * rate_f / 100.0 + 1e-9) if (uid and subtotal > 0 and rate_f > 0) else 0

    return True, {
        "coupon_id": coupon_id,
        "coupon_code": clean_code if coupon_id else "",
        "coupon_discount": coupon_discount,
        "points_used": pts_used,
        "points_earned": max(0, points_earned),
    }


def commit_order_promotions_tx(
    conn,
    *,
    user_id: Optional[int],
    order_id: int,
    order_code: str,
    promo: dict[str, Any],
    now_str: str,
) -> tuple[bool, str]:
    """mall_orders INSERT 직후 동일 트랜잭션 내에서 쿠폰 사용량과 적립금을 원자적으로 차감·기록합니다."""
    cur = conn.cursor()
    uid = mall_database.parse_strict_int64(user_id, min_val=1)

    coupon_id = promo.get("coupon_id")
    coupon_code = str(promo.get("coupon_code") or "").strip().upper()
    if coupon_id:
        cur.execute(
            """
            UPDATE mall_coupons
            SET used_count = used_count + 1
            WHERE id = ? AND (usage_limit = 0 OR used_count < usage_limit)
            """,
            (int(coupon_id),),
        )
        if cur.rowcount != 1:
            return False, "선착순 쿠폰 사용 한도가 초과되었습니다."

        if uid:
            cur.execute(
                "SELECT id, status FROM mall_user_coupons WHERE user_id = ? AND coupon_id = ? LIMIT 1",
                (uid, int(coupon_id)),
            )
            uc = cur.fetchone()
            if uc:
                if str(uc["status"] or "").upper() != "AVAILABLE":
                    return False, "이미 사용된 쿠폰입니다."
                cur.execute(
                    """
                    UPDATE mall_user_coupons
                    SET status = 'USED', used_order_id = ?, used_order_code = ?, used_at = ?
                    WHERE id = ? AND status = 'AVAILABLE'
                    """,
                    (int(order_id), str(order_code), now_str, int(uc["id"])),
                )
                if cur.rowcount != 1:
                    return False, "쿠폰 사용 상태 갱신에 실패했습니다."
            else:
                cur.execute(
                    """
                    INSERT INTO mall_user_coupons
                    (user_id, coupon_id, code, status, used_order_id, used_order_code, issued_at, used_at)
                    VALUES (?, ?, ?, 'USED', ?, ?, ?, ?)
                    """,
                    (uid, int(coupon_id), coupon_code, int(order_id), str(order_code), now_str, now_str),
                )

    pts_used = max(0, int(promo.get("points_used") or 0))
    if pts_used > 0:
        if not uid:
            return False, "적립금 사용은 회원만 가능합니다."
        cur.execute(
            """
            UPDATE mall_users
            SET points_balance = points_balance - ?
            WHERE id = ? AND points_balance >= ?
            """,
            (pts_used, uid, pts_used),
        )
        if cur.rowcount != 1:
            return False, "보유 적립금이 부족합니다."
        cur.execute("SELECT points_balance FROM mall_users WHERE id = ? LIMIT 1", (uid,))
        bal_row = cur.fetchone()
        bal_after = max(0, int(bal_row["points_balance"] or 0)) if bal_row else 0
        cur.execute(
            """
            INSERT INTO mall_point_logs
            (user_id, delta, balance_after, reason, order_code, description, created_at)
            VALUES (?, ?, ?, 'ORDER_SPEND', ?, ?, ?)
            """,
            (
                uid,
                -pts_used,
                bal_after,
                str(order_code),
                f"주문 {order_code} 결제 적립금 사용 (-{pts_used:,}P)",
                now_str,
            ),
        )

    return True, ""


def award_order_points_on_delivery(conn, order_id: int, now_str: Optional[str] = None) -> dict[str, Any]:
    """주문이 '배송완료' 상태에 도달했을 때 구매 적립금을 1회 멱등 지급합니다."""
    ts = now_str or _now_str()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, order_code, user_id, order_status, payment_status, stock_restored, points_earned, points_awarded
        FROM mall_orders
        WHERE id = ?
        LIMIT 1
        """,
        (int(order_id),),
    )
    row = cur.fetchone()
    if not row:
        return {"awarded": False, "points_earned": 0}
    ord_status = str(row["order_status"] or "")
    pay_status = str(row["payment_status"] or "")
    if ord_status != "배송완료" or ord_status in ("주문취소", "환불완료") or pay_status == "환불완료":
        return {"awarded": False, "points_earned": 0}
    if int(row["stock_restored"] or 0) != 0:
        return {"awarded": False, "points_earned": 0}
    uid = row["user_id"]
    earned = max(0, int(row["points_earned"] or 0))
    already = int(row["points_awarded"] or 0)
    if not uid or earned <= 0 or already != 0:
        return {"awarded": False, "points_earned": 0}

    cur.execute(
        "UPDATE mall_orders SET points_awarded = 1 WHERE id = ? AND points_awarded = 0",
        (int(order_id),),
    )
    if cur.rowcount != 1:
        return {"awarded": False, "points_earned": 0}

    cur.execute("UPDATE mall_users SET points_balance = points_balance + ? WHERE id = ?", (earned, int(uid)))
    cur.execute("SELECT points_balance FROM mall_users WHERE id = ? LIMIT 1", (int(uid),))
    bal_row = cur.fetchone()
    bal_after = max(0, int(bal_row["points_balance"] or 0)) if bal_row else earned
    ocode = str(row["order_code"] or "")
    cur.execute(
        """
        INSERT INTO mall_point_logs
        (user_id, delta, balance_after, reason, order_code, description, created_at)
        VALUES (?, ?, ?, 'ORDER_EARN', ?, ?, ?)
        """,
        (
            int(uid),
            earned,
            bal_after,
            ocode,
            f"주문 {ocode} 배송완료 구매 적립 (+{earned:,}P)",
            ts,
        ),
    )
    return {"awarded": True, "points_earned": earned, "balance_after": bal_after}


def restore_order_promotions_on_cancel(conn, order_id: int, now_str: Optional[str] = None) -> dict[str, Any]:
    """주문이 '주문취소' 또는 '환불완료' 될 때 사용 적립금 환불, 쿠폰 복구, 기지급 적립금 회수를 1회 멱등 처리합니다."""
    ts = now_str or _now_str()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, order_code, user_id, order_status, coupon_code, points_used,
               points_earned, points_awarded, points_refunded, coupon_restored, points_revoked
        FROM mall_orders
        WHERE id = ?
        LIMIT 1
        """,
        (int(order_id),),
    )
    row = cur.fetchone()
    if not row or str(row["order_status"] or "") not in ("주문취소", "환불완료"):
        return {"points_refunded": 0, "coupon_restored": False, "points_revoked": 0}

    uid = row["user_id"]
    ocode = str(row["order_code"] or "")
    refunded_amt = 0
    coupon_restored_now = False
    revoked_amt = 0

    pts_used = max(0, int(row["points_used"] or 0))
    if uid and pts_used > 0 and int(row["points_refunded"] or 0) == 0:
        cur.execute(
            "UPDATE mall_orders SET points_refunded = 1 WHERE id = ? AND points_refunded = 0",
            (int(order_id),),
        )
        if cur.rowcount == 1:
            cur.execute("UPDATE mall_users SET points_balance = points_balance + ? WHERE id = ?", (pts_used, int(uid)))
            cur.execute("SELECT points_balance FROM mall_users WHERE id = ? LIMIT 1", (int(uid),))
            bal_row = cur.fetchone()
            bal_after = max(0, int(bal_row["points_balance"] or 0)) if bal_row else pts_used
            cur.execute(
                """
                INSERT INTO mall_point_logs
                (user_id, delta, balance_after, reason, order_code, description, created_at)
                VALUES (?, ?, ?, 'ORDER_CANCEL_REFUND', ?, ?, ?)
                """,
                (
                    int(uid),
                    pts_used,
                    bal_after,
                    ocode,
                    f"주문 {ocode} 취소/환불 적립금 반환 (+{pts_used:,}P)",
                    ts,
                ),
            )
            refunded_amt = pts_used

    c_code = str(row["coupon_code"] or "").strip().upper()
    if c_code and int(row["coupon_restored"] or 0) == 0:
        cur.execute(
            "UPDATE mall_orders SET coupon_restored = 1 WHERE id = ? AND coupon_restored = 0",
            (int(order_id),),
        )
        if cur.rowcount == 1:
            resolved_coupon_id = None
            cur.execute(
                """
                SELECT coupon_id FROM mall_user_coupons
                WHERE used_order_id = ? OR (used_order_code != '' AND used_order_code = ?)
                LIMIT 1
                """,
                (int(order_id), ocode),
            )
            uc_row = cur.fetchone()
            if uc_row and uc_row["coupon_id"] is not None:
                resolved_coupon_id = int(uc_row["coupon_id"])

            if resolved_coupon_id is not None:
                cur.execute(
                    "UPDATE mall_coupons SET used_count = MAX(0, used_count - 1) WHERE id = ?",
                    (resolved_coupon_id,),
                )
            else:
                cur.execute(
                    "UPDATE mall_coupons SET used_count = MAX(0, used_count - 1) WHERE UPPER(code) = ?",
                    (c_code,),
                )
            if uid:
                cur.execute(
                    """
                    UPDATE mall_user_coupons
                    SET status = 'AVAILABLE', used_order_id = NULL, used_order_code = '', used_at = ''
                    WHERE user_id = ?
                      AND status = 'USED'
                      AND (used_order_id = ? OR used_order_code = ? OR UPPER(code) = ?)
                    """,
                    (int(uid), int(order_id), ocode, c_code),
                )
            coupon_restored_now = True

    pts_earned = max(0, int(row["points_earned"] or 0))
    if uid and int(row["points_awarded"] or 0) == 1 and pts_earned > 0 and int(row["points_revoked"] or 0) == 0:
        cur.execute(
            "UPDATE mall_orders SET points_revoked = 1 WHERE id = ? AND points_revoked = 0",
            (int(order_id),),
        )
        if cur.rowcount == 1:
            cur.execute("SELECT points_balance FROM mall_users WHERE id = ? LIMIT 1", (int(uid),))
            bal_row = cur.fetchone()
            cur_bal = max(0, int(bal_row["points_balance"] or 0)) if bal_row else 0
            deduct = min(cur_bal, pts_earned)
            if deduct > 0:
                cur.execute(
                    "UPDATE mall_users SET points_balance = MAX(0, points_balance - ?) WHERE id = ?",
                    (deduct, int(uid)),
                )
                cur.execute("SELECT points_balance FROM mall_users WHERE id = ? LIMIT 1", (int(uid),))
                bal_row2 = cur.fetchone()
                bal_after = max(0, int(bal_row2["points_balance"] or 0)) if bal_row2 else 0
                cur.execute(
                    """
                    INSERT INTO mall_point_logs
                    (user_id, delta, balance_after, reason, order_code, description, created_at)
                    VALUES (?, ?, ?, 'ORDER_EARN_REVOKE', ?, ?, ?)
                    """,
                    (
                        int(uid),
                        -deduct,
                        bal_after,
                        ocode,
                        f"주문 {ocode} 환불에 따른 적립금 회수 (-{deduct:,}P)",
                        ts,
                    ),
                )
            revoked_amt = deduct

    return {
        "points_refunded": refunded_amt,
        "coupon_restored": coupon_restored_now,
        "points_revoked": revoked_amt,
    }


def list_user_coupons(user_id: int) -> list[dict[str, Any]]:
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT
                    uc.id AS user_coupon_id,
                    uc.status,
                    uc.used_order_code,
                    uc.issued_at,
                    uc.used_at,
                    c.*
                FROM mall_user_coupons uc
                INNER JOIN mall_coupons c ON c.id = uc.coupon_id
                WHERE uc.user_id = ?
                ORDER BY CASE WHEN uc.status = 'AVAILABLE' THEN 0 ELSE 1 END, uc.id DESC
                """,
                (int(user_id),),
            )
            items: list[dict[str, Any]] = []
            for r in cur.fetchall():
                item = _format_coupon_row(r)
                item["user_coupon_id"] = int(r["user_coupon_id"])
                item["status"] = str(r["status"] or "AVAILABLE").upper()
                item["used_order_code"] = str(r["used_order_code"] or "")
                item["issued_at"] = str(r["issued_at"] or "")
                item["used_at"] = str(r["used_at"] or "")
                items.append(item)
            return items
        finally:
            conn.close()


def get_user_points_summary(user_id: int, limit: int = 100) -> dict[str, Any]:
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT points_balance FROM mall_users WHERE id = ? LIMIT 1", (int(user_id),))
            urow = cur.fetchone()
            balance = max(0, int(urow["points_balance"] or 0)) if urow else 0
            cur.execute(
                """
                SELECT id, user_id, delta, balance_after, reason, order_code, description, created_at
                FROM mall_point_logs
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(user_id), int(limit)),
            )
            logs = [
                {
                    "id": int(r["id"]),
                    "user_id": int(r["user_id"]),
                    "delta": int(r["delta"] or 0),
                    "balance_after": int(r["balance_after"] or 0),
                    "reason": str(r["reason"] or ""),
                    "order_code": str(r["order_code"] or ""),
                    "description": str(r["description"] or ""),
                    "created_at": str(r["created_at"] or ""),
                }
                for r in cur.fetchall()
            ]
            return {"balance": balance, "points_balance": balance, "ledger": logs, "logs": logs}
        finally:
            conn.close()


def list_admin_coupons() -> list[dict[str, Any]]:
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM mall_coupons ORDER BY id DESC")
            return [_format_coupon_row(r) for r in cur.fetchall()]
        finally:
            conn.close()


def save_admin_coupon(data: dict[str, Any]) -> dict[str, Any]:
    raw = data if isinstance(data, dict) else {}
    raw_id = raw.get("id") if raw.get("id") is not None else raw.get("coupon_id")
    cid = 0
    if isinstance(raw_id, (bool, float)):
        return {"ok": False, "message": "유효하지 않은 쿠폰 ID입니다."}
    if (
        raw_id is None
        or (isinstance(raw_id, str) and raw_id.strip() in ("", "0"))
        or (isinstance(raw_id, int) and raw_id == 0)
    ):
        cid = 0
    else:
        parsed_id = mall_database.parse_strict_int64(raw_id, min_val=1)
        if parsed_id is None:
            return {"ok": False, "message": "유효하지 않은 쿠폰 ID입니다."}
        cid = parsed_id

    code = str(raw.get("code") or "").strip().upper()
    if not _COUPON_CODE_RE.fullmatch(code):
        return {"ok": False, "message": "쿠폰 코드는 영문/숫자/하이픈(-)/언더스코어(_) 2~40자로 입력해 주세요."}

    name = str(raw.get("name") or "").strip()[:120]
    if not name:
        return {"ok": False, "message": "쿠폰명을 입력해 주세요."}

    discount_type = str(raw.get("discount_type") or "FIXED").strip().upper()
    if discount_type not in ("FIXED", "PERCENT"):
        return {"ok": False, "message": "할인 유형은 FIXED 또는 PERCENT만 선택할 수 있습니다."}

    ok_val, discount_value, val_err = parse_strict_non_neg_int(
        raw.get("discount_value"),
        "할인 값",
        max_val=100 if discount_type == "PERCENT" else 10_000_000,
    )
    if not ok_val or discount_value is None or discount_value <= 0:
        return {"ok": False, "message": val_err if (not ok_val and val_err) else "할인 값은 1 이상이어야 합니다."}

    raw_max_d = raw.get("max_discount_amount") if raw.get("max_discount_amount") is not None else raw.get("max_discount")
    ok_md, max_discount, md_err = parse_strict_non_neg_int(raw_max_d, "최대 할인 금액", max_val=10_000_000, default=0)
    if not ok_md or max_discount is None:
        return {"ok": False, "message": md_err}

    ok_min, min_order_amount, min_err = parse_strict_non_neg_int(
        raw.get("min_order_amount"),
        "최소 주문 금액",
        max_val=100_000_000,
        default=0,
    )
    if not ok_min or min_order_amount is None:
        return {"ok": False, "message": min_err}

    ok_lim, usage_limit, lim_err = parse_strict_non_neg_int(
        raw.get("usage_limit"),
        "최대 사용 횟수",
        max_val=1_000_000,
        default=0,
    )
    if not ok_lim or usage_limit is None:
        return {"ok": False, "message": lim_err}

    live_only = 1 if parse_strict_bool(raw.get("live_only"), default=False) else 0
    is_active = 1 if parse_strict_bool(raw.get("is_active"), default=True) else 0
    is_welcome_auto = 1 if parse_strict_bool(raw.get("is_welcome_auto"), default=False) else 0
    start_at = _normalize_date_str(raw.get("start_at"))
    end_at = _normalize_date_str(raw.get("end_at"))
    if start_at and _parse_datetime_bound(start_at, is_end=False) is None:
        return {"ok": False, "message": "시작일시 형식이 올바르지 않습니다. (예: 2026-01-01)"}
    if end_at and _parse_datetime_bound(end_at, is_end=True) is None:
        return {"ok": False, "message": "종료일시 형식이 올바르지 않습니다. (예: 2026-12-31)"}

    now_str = _now_str()
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM mall_coupons WHERE UPPER(code) = ? LIMIT 1", (code,))
            dup = cur.fetchone()
            if dup and int(dup["id"]) != cid:
                return {"ok": False, "message": f"이미 등록된 쿠폰 코드입니다: {code}"}

            if cid > 0:
                cur.execute("SELECT id, code FROM mall_coupons WHERE id = ? LIMIT 1", (cid,))
                existing_cp = cur.fetchone()
                if not existing_cp:
                    return {"ok": False, "not_found": True, "message": "수정할 쿠폰을 찾을 수 없습니다."}
                old_code = str(existing_cp["code"] or "").strip().upper()
                cur.execute(
                    """
                    UPDATE mall_coupons
                    SET code = ?, name = ?, discount_type = ?, discount_value = ?,
                        max_discount = ?, min_order_amount = ?, live_only = ?,
                        usage_limit = ?, start_at = ?, end_at = ?,
                        is_active = ?, is_welcome_auto = ?
                    WHERE id = ?
                    """,
                    (
                        code, name, discount_type, discount_value,
                        max_discount, min_order_amount, live_only,
                        usage_limit, start_at, end_at,
                        is_active, is_welcome_auto, cid,
                    ),
                )
                cur.execute(
                    "UPDATE mall_user_coupons SET code = ? WHERE coupon_id = ?",
                    (code, cid),
                )
                if old_code and old_code != code:
                    cur.execute(
                        "UPDATE mall_orders SET coupon_code = ? WHERE UPPER(COALESCE(coupon_code, '')) = ?",
                        (code, old_code),
                    )
                target_id = cid
            else:
                cur.execute(
                    """
                    INSERT INTO mall_coupons
                    (code, name, discount_type, discount_value, max_discount, min_order_amount,
                     live_only, usage_limit, used_count, start_at, end_at, is_active, is_welcome_auto, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                    """,
                    (
                        code, name, discount_type, discount_value, max_discount, min_order_amount,
                        live_only, usage_limit, start_at, end_at, is_active, is_welcome_auto, now_str,
                    ),
                )
                target_id = int(cur.lastrowid)

            conn.commit()
            cur.execute("SELECT * FROM mall_coupons WHERE id = ? LIMIT 1", (target_id,))
            saved = _format_coupon_row(cur.fetchone())
            cur.execute("SELECT * FROM mall_coupons ORDER BY id DESC")
            all_items = [_format_coupon_row(r) for r in cur.fetchall()]
            return {"ok": True, "coupon": saved, "coupons": all_items}
        finally:
            conn.close()


def delete_admin_coupon(coupon_id: Any) -> dict[str, Any]:
    cid = mall_database.parse_strict_int64(coupon_id, min_val=1)
    if cid is None:
        return {"ok": False, "message": "올바른 쿠폰 ID가 필요합니다."}

    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM mall_coupons WHERE id = ? LIMIT 1", (cid,))
            if not cur.fetchone():
                return {"ok": False, "not_found": True, "message": "삭제할 쿠폰을 찾을 수 없습니다."}
            cur.execute("DELETE FROM mall_user_coupons WHERE coupon_id = ?", (cid,))
            cur.execute("DELETE FROM mall_coupons WHERE id = ?", (cid,))
            conn.commit()
            cur.execute("SELECT * FROM mall_coupons ORDER BY id DESC")
            all_items = [_format_coupon_row(r) for r in cur.fetchall()]
            return {"ok": True, "deleted_id": cid, "coupons": all_items}
        finally:
            conn.close()


# ==============================================================================
# 🌐 Promotions API Routes
# ==============================================================================


@promotions_bp.route("/api/coupons/validate", methods=["POST"])
def api_validate_coupon():
    data = _read_request_payload()
    code = str(data.get("code") or "").strip()
    source_page = str(data.get("source_page") or "STORE").strip().upper()
    ok_sub, sub_val, sub_err = parse_strict_non_neg_int(data.get("subtotal"), "주문 상품 금액", default=0)
    if not ok_sub or sub_val is None:
        return jsonify({"status": "error", "ok": False, "valid": False, "message": sub_err}), 400

    user = _resolve_current_user()
    uid = int(user["id"]) if user and user.get("id") else None

    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            ok, result = validate_and_calculate_coupon(
                conn,
                code=code,
                subtotal=sub_val,
                source_page=source_page,
                user_id=uid,
            )
        finally:
            conn.close()

    if not ok:
        return jsonify({"status": "error", "ok": False, "valid": False, "message": str(result)}), 400

    assert isinstance(result, dict)
    return jsonify({
        "status": "ok",
        "ok": True,
        "valid": True,
        "discount_amount": int(result["discount_amount"]),
        "coupon": result,
    }), 200


@promotions_bp.route("/api/mypage/coupons", methods=["GET"])
def api_mypage_coupons():
    user = _resolve_current_user()
    if not user:
        return jsonify({"status": "error", "code": "AUTH_REQUIRED", "message": "로그인이 필요합니다."}), 401
    items = list_user_coupons(int(user["id"]))
    return jsonify({"status": "ok", "ok": True, "coupons": items, "count": len(items)}), 200


@promotions_bp.route("/api/mypage/points", methods=["GET"])
def api_mypage_points():
    user = _resolve_current_user()
    if not user:
        return jsonify({"status": "error", "code": "AUTH_REQUIRED", "message": "로그인이 필요합니다."}), 401
    summary = get_user_points_summary(int(user["id"]))
    return jsonify({"status": "ok", "ok": True, **summary}), 200


@promotions_bp.route("/api/admin/coupons", methods=["GET"])
def api_admin_coupons_list():
    items = list_admin_coupons()
    return jsonify({"status": "ok", "ok": True, "coupons": items, "count": len(items)}), 200


@promotions_bp.route("/api/admin/coupons/save", methods=["POST"])
def api_admin_coupons_save():
    data = _read_request_payload()
    res = save_admin_coupon(data)
    if not res.get("ok"):
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "ok": False, "message": res.get("message") or "쿠폰 저장 실패"}), status
    saved_coupon = res.get("coupon") or {}
    import mall_ops
    mall_ops.log_current_admin_action(
        action="COUPON_SAVE",
        target_type="coupon",
        target_id=saved_coupon.get("code") or saved_coupon.get("id") or "",
        detail_dict={
            "id": saved_coupon.get("id"),
            "code": saved_coupon.get("code"),
            "discount_type": saved_coupon.get("discount_type"),
            "discount_value": saved_coupon.get("discount_value"),
            "is_active": saved_coupon.get("is_active"),
        },
    )
    return jsonify({
        "status": "ok",
        "ok": True,
        "coupon": saved_coupon,
        "coupons": res.get("coupons", []),
    }), 200


@promotions_bp.route("/api/admin/coupons/delete", methods=["POST"])
def api_admin_coupons_delete():
    data = _read_request_payload()
    cid = data.get("id") if data.get("id") is not None else data.get("coupon_id")
    res = delete_admin_coupon(cid)
    if not res.get("ok"):
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "ok": False, "message": res.get("message") or "쿠폰 삭제 실패"}), status
    import mall_ops
    mall_ops.log_current_admin_action(
        action="COUPON_DELETE",
        target_type="coupon",
        target_id=res.get("deleted_id") or cid,
        detail_dict={"deleted_id": res.get("deleted_id")},
    )
    return jsonify({
        "status": "ok",
        "ok": True,
        "deleted_id": res.get("deleted_id"),
        "coupons": res.get("coupons", []),
    }), 200


# ==============================================================================
# 🛒 F15 장바구니 실시간 재검증 & F16 재입고 알림 서비스
# ==============================================================================

_RESTOCK_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _parse_int64_strict(raw_val: Any) -> tuple[bool, Optional[int]]:
    """정수를 엄격하게 파싱하고 SQLite signed 64-bit 범위를 검증합니다."""
    if raw_val is None or isinstance(raw_val, bool):
        return False, None
    if isinstance(raw_val, float):
        if math.isnan(raw_val) or math.isinf(raw_val) or not raw_val.is_integer():
            return False, None
        val = int(raw_val)
    elif isinstance(raw_val, int):
        val = int(raw_val)
    else:
        s = str(raw_val).strip().replace(",", "")
        if not re.fullmatch(r"-?[0-9]+", s):
            return False, None
        is_neg = s.startswith("-")
        digits = (s[1:] if is_neg else s).lstrip("0") or "0"
        if len(digits) > 19:
            return False, (mall_database.SQLITE_INT64_MIN if is_neg else mall_database.SQLITE_INT64_MAX)
        try:
            val = -int(digits) if is_neg else int(digits)
        except ValueError:
            return False, (mall_database.SQLITE_INT64_MIN if is_neg else mall_database.SQLITE_INT64_MAX)
    if val < mall_database.SQLITE_INT64_MIN or val > mall_database.SQLITE_INT64_MAX:
        return False, val
    return True, val


def _extract_client_unit_price_checked(item_dict: dict[str, Any]) -> tuple[bool, Optional[int], str]:
    for key in ("unit_price", "expected_price", "client_price", "price"):
        raw = item_dict.get(key)
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            continue
        ok_i64, parsed_i64 = _parse_int64_strict(raw)
        if not ok_i64 and parsed_i64 is not None:
            return False, None, "상품 단가가 허용 범위(64비트 정수)를 초과했습니다."
        ok, val, _ = parse_strict_non_neg_int(raw, "단가", max_val=mall_database.SQLITE_INT64_MAX)
        if ok and val is not None:
            return True, val, ""
    return True, None, ""


def _extract_client_unit_price(item_dict: dict[str, Any]) -> Optional[int]:
    ok, val, _ = _extract_client_unit_price_checked(item_dict)
    return val if ok else None


def revalidate_cart_items(
    items_req: list[Any],
    *,
    source_page: str = "STORE",
    coupon_code: Any = "",
    points_used_raw: Any = 0,
    user_id: Optional[int] = None,
) -> dict[str, Any]:
    """장바구니/주문서 품목의 최신 가격, 옵션 유효성, 재고, 배송비 및 프로모션을 서버 DB 기준으로 재검증합니다."""
    src = str(source_page or "STORE").strip().upper()
    use_live = (src == "LIVE")
    uid = mall_database.parse_strict_int64(user_id, min_val=1)

    if isinstance(points_used_raw, bool):
        return {"status": "error", "ok": False, "http_status": 400, "message": "사용 적립금은 0 이상의 정수로 입력해 주세요."}
    if points_used_raw not in (None, "", 0, "0"):
        ok_pts, pts_req, pts_err = parse_strict_non_neg_int(
            points_used_raw,
            "사용 적립금",
            max_val=mall_database.SQLITE_INT64_MAX,
            default=0,
        )
        if not ok_pts or pts_req is None:
            return {"status": "error", "ok": False, "http_status": 400, "message": pts_err or "유효하지 않은 적립금 입력입니다."}
    else:
        pts_req = 0

    result_items: list[dict[str, Any]] = []
    errors: list[str] = []
    price_changed = False
    sold_out = False
    insufficient_stock = False
    subtotal = 0

    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            requested_qty_by_sku: dict[tuple[int, str, str], int] = {}

            for raw_it in items_req:
                if not isinstance(raw_it, dict):
                    continue
                ok_cp, client_price, cp_err = _extract_client_unit_price_checked(raw_it)
                if not ok_cp:
                    return {"status": "error", "ok": False, "http_status": 400, "message": cp_err}

                raw_num = raw_it.get("num") if raw_it.get("num") is not None else raw_it.get("product_num")
                ok_num, parsed_num = _parse_int64_strict(raw_num)
                if not ok_num and parsed_num is not None:
                    return {
                        "status": "error",
                        "ok": False,
                        "http_status": 400,
                        "message": "상품 번호가 허용 범위(64비트 정수)를 초과했습니다.",
                    }
                if not ok_num or parsed_num is None:
                    sold_out = True
                    errors.append("유효하지 않은 상품 번호가 포함되어 있습니다.")
                    result_items.append({
                        "num": 0,
                        "product_num": 0,
                        "name": str(raw_it.get("name") or "알 수 없는 상품"),
                        "image_url": "",
                        "color": str(raw_it.get("color") or "-").strip() or "-",
                        "size": str(raw_it.get("size") or "-").strip() or "-",
                        "quantity": 1,
                        "stock": 0,
                        "total_stock": 0,
                        "price": 0,
                        "unit_price": 0,
                        "consumer_price": 0,
                        "line_total": 0,
                        "client_price": client_price,
                        "price_changed": False,
                        "sold_out": True,
                        "insufficient_stock": False,
                        "exists": False,
                        "option_exists": False,
                        "available_options": [],
                    })
                    continue

                p_num = int(parsed_num)
                color = str(raw_it.get("color") or "-").strip() or "-"
                size = str(raw_it.get("size") or "-").strip() or "-"
                raw_qty = raw_it.get("quantity")
                if raw_qty is not None and not (isinstance(raw_qty, str) and raw_qty.strip() == ""):
                    ok_qty, parsed_qty = _parse_int64_strict(raw_qty)
                    if not ok_qty and parsed_qty is not None:
                        return {
                            "status": "error",
                            "ok": False,
                            "http_status": 400,
                            "message": "주문 수량이 허용 범위(64비트 정수)를 초과했습니다.",
                        }
                    qty = max(1, parsed_qty) if (ok_qty and parsed_qty is not None) else 1
                else:
                    qty = 1

                cur.execute("SELECT * FROM mall_products WHERE num = ? LIMIT 1", (p_num,))
                p_row = cur.fetchone()
                if not p_row:
                    sold_out = True
                    errors.append(f"#{p_num}번 상품을 찾을 수 없습니다.")
                    result_items.append({
                        "num": p_num,
                        "product_num": p_num,
                        "name": str(raw_it.get("name") or f"상품 #{p_num}"),
                        "image_url": "",
                        "color": color,
                        "size": size,
                        "quantity": qty,
                        "stock": 0,
                        "total_stock": 0,
                        "price": 0,
                        "unit_price": 0,
                        "consumer_price": 0,
                        "line_total": 0,
                        "client_price": client_price,
                        "price_changed": False,
                        "sold_out": True,
                        "insufficient_stock": False,
                        "exists": False,
                        "option_exists": False,
                        "available_options": [],
                    })
                    continue

                mall_pid = int(p_row["id"])
                cur.execute(
                    "SELECT color, size, stock FROM mall_product_options WHERE product_id = ? ORDER BY id ASC",
                    (mall_pid,),
                )
                opt_rows = cur.fetchall()
                available_options = [
                    {
                        "color": str(r["color"] or "-").strip() or "-",
                        "size": str(r["size"] or "-").strip() or "-",
                        "stock": max(0, int(r["stock"] or 0)),
                    }
                    for r in opt_rows
                ]
                total_stock = sum(o["stock"] for o in available_options)

                matched_opt = None
                for o in available_options:
                    if o["color"] == color and o["size"] == size:
                        matched_opt = o
                        break
                if matched_opt is None and len(available_options) == 1 and color in ("-", "") and size in ("-", ""):
                    matched_opt = available_options[0]
                    color = matched_opt["color"]
                    size = matched_opt["size"]

                option_exists = matched_opt is not None
                opt_stock = int(matched_opt["stock"]) if matched_opt is not None else 0

                sku_key = (p_num, color, size)
                cumulative_qty = requested_qty_by_sku.get(sku_key, 0) + qty
                requested_qty_by_sku[sku_key] = cumulative_qty

                p_keys = set(p_row.keys())
                sell_price = max(0, int(p_row["price"] or 0))
                live_price = (
                    max(0, int(p_row["live_price"] or 0))
                    if "live_price" in p_keys and p_row["live_price"] is not None and int(p_row["live_price"] or 0) > 0
                    else sell_price
                )
                cons_price = max(sell_price, int(p_row["consumer_price"] or sell_price))
                server_unit_price = live_price if use_live else sell_price
                line_total = server_unit_price * qty

                item_price_changed = (client_price is not None and client_price != server_unit_price)
                if item_price_changed:
                    price_changed = True
                    errors.append(
                        f"[{p_row['name']}] 가격이 변동되었습니다. ({client_price:,}원 → {server_unit_price:,}원)"
                    )

                item_sold_out = (not option_exists) or (opt_stock <= 0)
                item_insufficient = (not item_sold_out) and (cumulative_qty > opt_stock)
                if item_sold_out:
                    sold_out = True
                    if not option_exists:
                        errors.append(f"[{p_row['name']} ({color}/{size})] 존재하지 않는 옵션입니다.")
                    else:
                        errors.append(f"[{p_row['name']} ({color}/{size})] 품절된 옵션입니다.")
                elif item_insufficient:
                    insufficient_stock = True
                    errors.append(
                        f"[{p_row['name']} ({color}/{size})] 재고가 부족합니다. (요청 {cumulative_qty}개 / 잔여 {opt_stock}개)"
                    )

                subtotal += line_total
                item_entry: dict[str, Any] = {
                    "num": p_num,
                    "product_num": p_num,
                    "name": str(p_row["name"] or ""),
                    "image_url": str(p_row["image_url"] or ""),
                    "color": color,
                    "size": size,
                    "quantity": qty,
                    "stock": opt_stock,
                    "total_stock": total_stock,
                    "price": server_unit_price,
                    "unit_price": server_unit_price,
                    "consumer_price": cons_price,
                    "line_total": line_total,
                    "client_price": client_price,
                    "price_changed": item_price_changed,
                    "sold_out": item_sold_out,
                    "insufficient_stock": item_insufficient,
                    "exists": True,
                    "option_exists": option_exists,
                    "available_options": available_options,
                }
                if use_live:
                    item_entry["live_price"] = live_price
                result_items.append(item_entry)

            store_info = mall_database.get_mall_store_info()
            shipping_fee = mall_database.calculate_shipping_fee(subtotal, store_info=store_info)
            applied_code = ""
            coupon_discount = 0
            applied_points = 0
            coupon_valid = True
            promo_error = ""

            raw_code = str(coupon_code or "").strip()
            if result_items and raw_code:
                ok_c, c_res = validate_and_calculate_coupon(
                    conn,
                    code=raw_code,
                    subtotal=subtotal,
                    source_page=src,
                    user_id=uid,
                )
                if ok_c and isinstance(c_res, dict):
                    applied_code = str(c_res["code"])
                    coupon_discount = max(0, int(c_res["discount_amount"]))
                    coupon_valid = True
                else:
                    coupon_valid = False
                    applied_code = ""
                    coupon_discount = 0
                    promo_error = str(c_res)
                    errors.append(promo_error)

            if result_items and pts_req > 0:
                if not uid:
                    applied_points = 0
                    pts_msg = "적립금(포인트) 사용은 회원 로그인 후 이용할 수 있습니다."
                    if not promo_error:
                        promo_error = pts_msg
                    errors.append(pts_msg)
                else:
                    cur.execute(
                        "SELECT points_balance FROM mall_users WHERE id = ? AND is_active = 1 AND COALESCE(withdrawn_at, '') = '' LIMIT 1",
                        (uid,),
                    )
                    urow = cur.fetchone()
                    if not urow:
                        applied_points = 0
                        pts_msg = "활성 회원 계정을 찾을 수 없습니다."
                        if not promo_error:
                            promo_error = pts_msg
                        errors.append(pts_msg)
                    else:
                        bal = max(0, int(urow["points_balance"] or 0))
                        after_coupon = max(0, subtotal - coupon_discount)
                        applied_points = min(pts_req, bal, after_coupon)

            total_amount = max(0, subtotal - coupon_discount - applied_points) + shipping_fee
            has_blocked = sold_out or insufficient_stock
            has_changes = price_changed or has_blocked
            valid = (not has_changes) and (len(result_items) > 0)

            return {
                "status": "ok",
                "ok": valid,
                "valid": valid,
                "price_changed": price_changed,
                "sold_out": sold_out,
                "insufficient_stock": insufficient_stock,
                "has_changes": has_changes,
                "has_blocked": has_blocked,
                "errors": errors,
                "subtotal": subtotal,
                "shipping_fee": shipping_fee,
                "free_shipping_threshold": int(store_info.get("free_shipping_threshold") or 0),
                "coupon_code": applied_code,
                "coupon_discount": coupon_discount,
                "coupon_valid": coupon_valid,
                "promo_error": promo_error,
                "points_used": applied_points,
                "total": total_amount,
                "total_amount": total_amount,
                "items": result_items,
            }
        finally:
            conn.close()


def _format_restock_alert_row(row) -> dict[str, Any]:
    d = dict(row)
    return {
        "id": int(d["id"]),
        "product_num": int(d["product_num"]),
        "product_name": str(d.get("product_name") or ""),
        "color": str(d.get("color") or "-"),
        "size": str(d.get("size") or "-"),
        "user_id": int(d["user_id"]) if d.get("user_id") is not None else None,
        "contact_type": str(d.get("contact_type") or "EMAIL").upper(),
        "contact_value": str(d.get("contact_value") or ""),
        "status": str(d.get("status") or "PENDING").upper(),
        "notified_at": str(d.get("notified_at") or ""),
        "created_at": str(d.get("created_at") or ""),
    }


def subscribe_restock_alert(
    data: dict[str, Any],
    *,
    user: Optional[dict] = None,
) -> dict[str, Any]:
    raw = data if isinstance(data, dict) else {}
    raw_num = raw.get("product_num") if raw.get("product_num") is not None else raw.get("num")
    ok_num, parsed_num = _parse_int64_strict(raw_num)
    if not ok_num or parsed_num is None:
        if parsed_num is not None:
            return {"ok": False, "http_status": 400, "message": "상품 번호(product_num)가 허용 범위를 초과했습니다."}
        return {"ok": False, "http_status": 400, "message": "유효한 상품 번호(product_num)를 입력해 주세요."}
    p_num = int(parsed_num)
    if p_num <= 0:
        return {"ok": False, "http_status": 400, "message": "상품 번호는 1 이상이어야 합니다."}

    color = str(raw.get("color") or "-").strip()[:60] or "-"
    size = str(raw.get("size") or "-").strip()[:60] or "-"

    raw_ctype = str(raw.get("contact_type") or raw.get("channel") or "").strip().upper()
    if raw_ctype in ("PHONE", "MOBILE", "TEL"):
        raw_ctype = "SMS"
    elif raw_ctype in ("KAKAO", "ALIMTALK", "KAKAO_ALIMTALK"):
        raw_ctype = "KAKAO_ALIMTALK"

    if raw_ctype in ("SMS", "KAKAO_ALIMTALK"):
        contact_val = str(
            raw.get("contact_value")
            or raw.get("contact")
            or raw.get("phone")
            or ((user or {}).get("phone") if user else "")
            or ""
        ).strip()
    elif raw_ctype == "EMAIL":
        contact_val = str(
            raw.get("contact_value")
            or raw.get("contact")
            or raw.get("email")
            or ((user or {}).get("email") if user else "")
            or ""
        ).strip()
    else:
        contact_val = str(
            raw.get("contact_value")
            or raw.get("contact")
            or raw.get("email")
            or raw.get("phone")
            or ((user or {}).get("email") if user else "")
            or ((user or {}).get("phone") if user else "")
            or ""
        ).strip()

    if not contact_val:
        return {"ok": False, "http_status": 400, "message": "재입고 알림을 받을 이메일 또는 휴대폰 번호를 입력해 주세요."}

    contact_type = raw_ctype or ("EMAIL" if "@" in contact_val else "SMS")
    if contact_type == "EMAIL":
        if not _RESTOCK_EMAIL_RE.fullmatch(contact_val) or len(contact_val) > 160:
            return {"ok": False, "http_status": 400, "message": "올바른 이메일 주소를 입력해 주세요."}
    elif contact_type in ("SMS", "KAKAO_ALIMTALK"):
        digits = re.sub(r"\D", "", contact_val)
        if len(digits) < 9 or len(digits) > 13:
            return {"ok": False, "http_status": 400, "message": "올바른 휴대폰 번호(숫자 9~13자리)를 입력해 주세요."}
    else:
        return {"ok": False, "http_status": 400, "message": "지원하지 않는 알림 수신 유형입니다. (EMAIL 또는 SMS)"}

    uid = int(user["id"]) if user and user.get("id") else None
    now_str = _now_str()

    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, name FROM mall_products WHERE num = ? LIMIT 1", (p_num,))
            p_row = cur.fetchone()
            if not p_row:
                return {"ok": False, "http_status": 404, "message": "상품을 찾을 수 없습니다."}

            mall_pid = int(p_row["id"])
            cur.execute(
                "SELECT color, size, stock FROM mall_product_options WHERE product_id = ? ORDER BY id ASC",
                (mall_pid,),
            )
            opt_rows = cur.fetchall()
            if not opt_rows:
                return {"ok": False, "http_status": 400, "message": "재입고 알림을 신청할 옵션이 없습니다."}

            if color in ("-", "") and size in ("-", ""):
                if len(opt_rows) == 1:
                    color = str(opt_rows[0]["color"] or "-").strip() or "-"
                    size = str(opt_rows[0]["size"] or "-").strip() or "-"
                    target_stock = max(0, int(opt_rows[0]["stock"] or 0))
                else:
                    target_stock = sum(max(0, int(r["stock"] or 0)) for r in opt_rows)
                if target_stock > 0:
                    return {"ok": False, "http_status": 400, "message": "현재 재고가 남아 있는 상품입니다."}
            else:
                matched = None
                for r in opt_rows:
                    rc = str(r["color"] or "-").strip() or "-"
                    rs = str(r["size"] or "-").strip() or "-"
                    if rc == color and rs == size:
                        matched = r
                        break
                if matched is None:
                    return {"ok": False, "http_status": 400, "message": "선택한 상품 옵션을 찾을 수 없습니다."}
                if int(matched["stock"] or 0) > 0:
                    return {"ok": False, "http_status": 400, "message": "현재 재고가 남아 있는 옵션은 재입고 알림을 신청할 수 없습니다."}

            cur.execute(
                """
                SELECT r.*, p.name AS product_name
                FROM mall_restock_alerts r
                LEFT JOIN mall_products p ON p.num = r.product_num
                WHERE r.product_num = ?
                  AND r.color = ?
                  AND r.size = ?
                  AND r.contact_type = ?
                  AND LOWER(r.contact_value) = LOWER(?)
                  AND r.status = 'PENDING'
                LIMIT 1
                """,
                (p_num, color, size, contact_type, contact_val),
            )
            dup = cur.fetchone()
            if dup:
                return {
                    "ok": True,
                    "already_subscribed": True,
                    "alert": _format_restock_alert_row(dup),
                    "message": "이미 해당 옵션에 대한 재입고 알림 신청이 접수되어 있습니다.",
                }

            cur.execute(
                """
                INSERT INTO mall_restock_alerts
                (product_num, color, size, user_id, contact_type, contact_value, status, notified_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'PENDING', '', ?)
                """,
                (p_num, color, size, uid, contact_type, contact_val, now_str),
            )
            new_id = int(cur.lastrowid)
            conn.commit()
            cur.execute(
                """
                SELECT r.*, p.name AS product_name
                FROM mall_restock_alerts r
                LEFT JOIN mall_products p ON p.num = r.product_num
                WHERE r.id = ?
                LIMIT 1
                """,
                (new_id,),
            )
            saved_row = cur.fetchone()
            return {
                "ok": True,
                "already_subscribed": False,
                "alert": _format_restock_alert_row(saved_row),
                "message": "재입고 알림 신청이 완료되었습니다. 입고 시 빠르게 안내해 드릴게요.",
            }
        finally:
            conn.close()


def list_admin_restock_alerts(
    *,
    status: str = "",
    product_num: Any = "",
    query: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    clean_status = str(status or "").strip().upper()
    q = str(query or "").strip().lower()
    pnum_filter: Optional[int] = None
    if product_num not in (None, ""):
        raw_pnum_s = str(product_num).strip()
        if re.fullmatch(r"-?[0-9]+", raw_pnum_s):
            ok_pnum, parsed_pnum = _parse_int64_strict(raw_pnum_s)
            if not ok_pnum or parsed_pnum is None or parsed_pnum <= 0:
                return []
            pnum_filter = parsed_pnum
    eff_limit = mall_database._parse_clamped_query_int(limit, 200, 1, 500)

    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            where: list[str] = []
            params: list[Any] = []
            if clean_status and clean_status != "ALL":
                where.append("UPPER(r.status) = ?")
                params.append(clean_status)
            if pnum_filter is not None:
                where.append("r.product_num = ?")
                params.append(pnum_filter)
            where_sql = f"WHERE {' AND '.join(where)}" if where else ""
            cur.execute(
                f"""
                SELECT r.*, COALESCE(p.name, '') AS product_name
                FROM mall_restock_alerts r
                LEFT JOIN mall_products p ON p.num = r.product_num
                {where_sql}
                ORDER BY r.id DESC
                LIMIT ?
                """,
                (*params, eff_limit),
            )
            items = []
            for row in cur.fetchall():
                item = _format_restock_alert_row(row)
                if q:
                    hay = f"{item['product_num']} {item['product_name']} {item['color']} {item['size']} {item['contact_value']} {item['status']}".lower()
                    if q not in hay:
                        continue
                items.append(item)
            return items
        finally:
            conn.close()


@promotions_bp.route("/api/cart/revalidate", methods=["POST"])
def api_cart_revalidate():
    data = _read_request_payload()
    if not isinstance(data, dict):
        return jsonify({"status": "error", "ok": False, "message": "잘못된 요청 형식입니다."}), 400

    live_tok = str(data.get("live_order_token") or "").strip()
    if live_tok and mall_security.verify_signed_token(str(current_app.secret_key or ""), "live_order", live_tok) is None:
        return jsonify({"status": "error", "ok": False, "code": "INVALID_LIVE_TOKEN", "message": "유효하지 않은 라이브 주문 보안 토큰입니다."}), 403

    items = data.get("items")
    if not isinstance(items, list):
        return jsonify({"status": "error", "ok": False, "message": "검증할 상품 목록(items)이 필요합니다."}), 400

    user = _resolve_current_user()
    uid = int(user["id"]) if user and user.get("id") else None
    res = revalidate_cart_items(
        items,
        source_page=str(data.get("source_page") or "STORE"),
        coupon_code=data.get("coupon_code") or "",
        points_used_raw=data.get("points_used"),
        user_id=uid,
    )
    status_code = int(res.pop("http_status", 200) or 200) if isinstance(res, dict) else 200
    return jsonify(res), status_code


@promotions_bp.route("/api/restock-alerts/subscribe", methods=["POST"])
def api_restock_alerts_subscribe():
    ip_addr = str(request.remote_addr or "unknown")
    allowed, retry_sec = mall_security.rate_limiter.check("restock_subscribe", ip_addr, max_requests=25, window_sec=60.0)
    if not allowed:
        return jsonify({
            "status": "error",
            "ok": False,
            "message": f"요청이 너무 많습니다. {retry_sec}초 후 다시 시도해 주세요.",
        }), 429

    user = _resolve_current_user()
    if user and not mall_security.verify_csrf_token(session, request):
        return jsonify({
            "status": "error",
            "ok": False,
            "code": "CSRF_INVALID",
            "message": "CSRF 보안 토큰 검증에 실패했습니다.",
        }), 403

    data = _read_request_payload()
    res = subscribe_restock_alert(data, user=user)
    if not res.get("ok"):
        return jsonify({
            "status": "error",
            "ok": False,
            "message": res.get("message") or "재입고 알림 신청 실패",
        }), int(res.get("http_status") or 400)

    return jsonify({
        "status": "ok",
        "ok": True,
        "already_subscribed": bool(res.get("already_subscribed")),
        "alert": res.get("alert"),
        "message": res.get("message"),
    }), 200


@promotions_bp.route("/api/admin/restock-alerts", methods=["GET"])
def api_admin_restock_alerts():
    status = str(request.args.get("status") or "")
    product_num = (request.args.get("product_num") or "").strip()
    if product_num and re.fullmatch(r"-?[0-9]+", product_num):
        ok_pnum, parsed_pnum = _parse_int64_strict(product_num)
        if not ok_pnum or parsed_pnum is None or parsed_pnum <= 0:
            return jsonify({"status": "error", "ok": False, "message": "유효한 상품 번호 범위를 벗어났습니다."}), 400
    query = str(request.args.get("q") or "")
    limit = mall_database._parse_clamped_query_int(request.args.get("limit"), 200, 1, 500)
    alerts = list_admin_restock_alerts(
        status=status,
        product_num=product_num,
        query=query,
        limit=limit,
    )
    return jsonify({"status": "ok", "ok": True, "alerts": alerts, "count": len(alerts)}), 200
