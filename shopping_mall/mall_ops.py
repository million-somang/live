# -*- coding: utf-8 -*-
"""
쇼핑몰 운영 자동화 · SQLite 온라인 백업 · 관리자 감사 로그 모듈 (mall_ops.py)
- 독립 쇼핑몰 단독 배포 규칙 준수 (c:\\live\\ 루트 모듈 import 금지)
- 관리자 감사 로그(mall_audit_logs) 기록/조회 및 민감정보 자동 마스킹
- SQLite Online Backup API 기반 무중단 백업, 경로 역추적(Path Traversal) 차단 다운로드
- 미입금 주문 기한 경과 자동 취소 + 재고/OBS/쿠폰/적립금 복구 + 다채널 알림 + 백그라운드 스케줄러
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from flask import Blueprint, current_app, has_app_context, has_request_context, jsonify, request, send_file, session

import mall_database

ops_bp = Blueprint("ops_bp", __name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_SENSITIVE_AUDIT_SUBSTRINGS = (
    "password",
    "hash",
    "secret",
    "token",
    "csrf",
    "api_key",
    "api_secret",
    "webhook_secret",
    "authorization",
    "cookie",
)

_BACKUP_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.(?:db|sqlite|sqlite3)$")

_AUDIT_CATEGORY_SQL: dict[str, str] = {
    "PRODUCT": "(UPPER(action) LIKE 'PRODUCT_%' OR UPPER(action) = 'BULK_DISCOUNT_APPLY' OR UPPER(action) LIKE 'CATEGORY_%')",
    "ORDER": "UPPER(action) LIKE 'ORDER_%'",
    "USER": "UPPER(action) LIKE 'USER_%'",
    "STORE": "(UPPER(action) LIKE 'STORE_%' OR UPPER(action) LIKE 'POLICY_%' OR UPPER(action) LIKE 'BANNER_%' OR UPPER(action) LIKE 'SUPPLIER_%')",
    "COUPON": "UPPER(action) LIKE 'COUPON_%'",
    "CS": "(UPPER(action) LIKE 'QNA_%' OR UPPER(action) LIKE 'INQUIRY_%' OR UPPER(action) LIKE 'NOTICE_%' OR UPPER(action) LIKE 'FAQ_%')",
    "REVIEW": "UPPER(action) LIKE 'REVIEW_%'",
    "SYSTEM": "(UPPER(action) LIKE 'SYSTEM_%' OR UPPER(action) LIKE 'AUTO_CANCEL_%')",
}

_SCHEDULER_LOCK = threading.Lock()
_SCHEDULER_STOP_EVENT: Optional[threading.Event] = None
_SCHEDULER_THREAD: Optional[threading.Thread] = None


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_ops_schema(conn: sqlite3.Connection) -> None:
    """mall_audit_logs 테이블 및 인덱스를 멱등적으로 생성합니다."""
    cur = conn.cursor()
    cur.execute(
        """
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
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_audit_logs_created ON mall_audit_logs (id DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_audit_logs_action ON mall_audit_logs (action, id DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mall_audit_logs_target ON mall_audit_logs (target_type, target_id)")


def _redact_audit_value(val: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "[TRUNCATED]"
    if isinstance(val, dict):
        clean: dict[str, Any] = {}
        for k, v in val.items():
            key_str = str(k)
            lk = key_str.strip().lower()
            if any(sub in lk for sub in _SENSITIVE_AUDIT_SUBSTRINGS):
                clean[key_str] = "[REDACTED]"
            else:
                clean[key_str] = _redact_audit_value(v, depth + 1)
        return clean
    if isinstance(val, (list, tuple)):
        return [_redact_audit_value(item, depth + 1) for item in list(val)[:100]]
    if isinstance(val, str):
        return val[:2000]
    return val


def log_admin_action(
    admin_user: Optional[dict[str, Any]],
    action: str,
    target_type: str = "",
    target_id: Any = "",
    detail_dict: Optional[dict[str, Any]] = None,
    ip_addr: str = "",
    ip_address: str = "",
) -> int:
    """관리자 작업 감사 로그를 비파괴적(Non-crashing)으로 기록합니다."""
    try:
        uid: Optional[int] = None
        email = ""
        name = ""
        if isinstance(admin_user, dict):
            uid = mall_database.parse_strict_int64(admin_user.get("id"), min_val=1)
            email = str(admin_user.get("email") or "").strip()[:160]
            name = str(admin_user.get("name") or "").strip()[:80]

        resolved_ip = str(ip_addr or ip_address or "").strip()
        if not resolved_ip and has_request_context():
            resolved_ip = str(request.remote_addr or "").strip()
        resolved_ip = resolved_ip[:80]

        clean_action = str(action or "UNKNOWN").strip().upper()[:80] or "UNKNOWN"
        clean_target_type = str(target_type or "").strip()[:60]
        clean_target_id = str(target_id if target_id is not None else "").strip()[:120]

        if isinstance(detail_dict, dict):
            redacted = _redact_audit_value(detail_dict)
        elif detail_dict is None:
            redacted = {}
        else:
            redacted = {"value": _redact_audit_value(detail_dict)}
        detail_json = json.dumps(redacted, ensure_ascii=False, default=str)[:8000]
        now_str = _now_str()

        with mall_database.mall_db_lock:
            conn = mall_database.get_connection()
            try:
                cur = conn.cursor()
                try:
                    cur.execute(
                        """
                        INSERT INTO mall_audit_logs
                        (admin_user_id, admin_email, admin_name, action, target_type, target_id, detail_json, ip_address, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            uid,
                            email,
                            name,
                            clean_action,
                            clean_target_type,
                            clean_target_id,
                            detail_json,
                            resolved_ip,
                            now_str,
                        ),
                    )
                except sqlite3.OperationalError:
                    ensure_ops_schema(conn)
                    cur.execute(
                        """
                        INSERT INTO mall_audit_logs
                        (admin_user_id, admin_email, admin_name, action, target_type, target_id, detail_json, ip_address, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            uid,
                            email,
                            name,
                            clean_action,
                            clean_target_type,
                            clean_target_id,
                            detail_json,
                            resolved_ip,
                            now_str,
                        ),
                    )
                log_id = int(cur.lastrowid or 0)
                conn.commit()
                return log_id
            finally:
                conn.close()
    except Exception as exc:
        print(f"[ShoppingMall] 감사 로그 기록 경고: {exc}")
        return 0


def _resolve_request_admin_user() -> Optional[dict[str, Any]]:
    try:
        if has_app_context():
            fn = current_app.config.get("MALL_GET_CURRENT_USER")
            if callable(fn):
                u = fn()
                if isinstance(u, dict):
                    return u
        if has_request_context():
            uid = mall_database.parse_strict_int64(session.get("mall_user_id"), min_val=1)
            if uid is not None:
                u = mall_database.get_mall_user_by_id(uid)
                if isinstance(u, dict):
                    return u
    except Exception:
        pass
    return None


def log_current_admin_action(
    action: str,
    target_type: str = "",
    target_id: Any = "",
    detail_dict: Optional[dict[str, Any]] = None,
) -> int:
    """현재 Flask 요청의 관리자 사용자와 IP를 자동 조회하여 감사 로그를 기록합니다."""
    try:
        admin_user = _resolve_request_admin_user()
        ip_addr = str(request.remote_addr or "") if has_request_context() else ""
        return log_admin_action(
            admin_user=admin_user,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail_dict=detail_dict,
            ip_addr=ip_addr,
        )
    except Exception as exc:
        print(f"[ShoppingMall] 현재 관리자 감사 로그 기록 경고: {exc}")
        return 0


def _escape_like_pattern(val: str) -> str:
    return val.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_admin_audit_logs(
    action: str = "",
    target_type: str = "",
    query: str = "",
    page: Any = 1,
    per_page: Any = 20,
    limit: Any = None,
) -> dict[str, Any]:
    """관리자 감사 로그를 필터·검색·페이지네이션하여 반환합니다."""
    clean_action = str(action or "").strip().upper()
    clean_target = str(target_type or "").strip().lower()
    clean_q = str(query or "").strip().lower()

    eff_page = mall_database._parse_clamped_query_int(page, 1, 1, 1_000_000)
    eff_per_page = mall_database._parse_clamped_query_int(per_page, 20, 1, 500)
    if limit is not None and str(limit).strip() != "":
        eff_per_page = mall_database._parse_clamped_query_int(limit, eff_per_page, 1, 500)

    where: list[str] = []
    params: list[Any] = []

    if clean_action and clean_action != "ALL":
        if clean_action in _AUDIT_CATEGORY_SQL:
            where.append(_AUDIT_CATEGORY_SQL[clean_action])
        else:
            where.append("UPPER(action) = ?")
            params.append(clean_action)

    if clean_target and clean_target != "all":
        where.append("LOWER(target_type) = ?")
        params.append(clean_target)

    if clean_q:
        like_pat = f"%{_escape_like_pattern(clean_q)}%"
        where.append(
            "("
            "LOWER(COALESCE(admin_email, '')) LIKE ? ESCAPE '\\' OR "
            "LOWER(COALESCE(admin_name, '')) LIKE ? ESCAPE '\\' OR "
            "LOWER(COALESCE(action, '')) LIKE ? ESCAPE '\\' OR "
            "LOWER(COALESCE(target_type, '')) LIKE ? ESCAPE '\\' OR "
            "LOWER(COALESCE(target_id, '')) LIKE ? ESCAPE '\\' OR "
            "LOWER(COALESCE(detail_json, '')) LIKE ? ESCAPE '\\'"
            ")"
        )
        params.extend([like_pat] * 6)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    offset = (eff_page - 1) * eff_per_page

    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            ensure_ops_schema(conn)
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM mall_audit_logs {where_sql}", tuple(params))
            total_count = int((cur.fetchone() or [0])[0] or 0)
            total_pages = max(1, (total_count + eff_per_page - 1) // eff_per_page)

            cur.execute(
                f"""
                SELECT id, admin_user_id, admin_email, admin_name, action,
                       target_type, target_id, detail_json, ip_address, created_at
                FROM mall_audit_logs
                {where_sql}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, eff_per_page, offset),
            )
            logs: list[dict[str, Any]] = []
            for r in cur.fetchall():
                d = dict(r)
                d["id"] = int(d["id"])
                if d.get("admin_user_id") is not None:
                    d["admin_user_id"] = int(d["admin_user_id"])
                raw_dj = str(d.get("detail_json") or "{}")
                try:
                    parsed_detail = json.loads(raw_dj)
                except Exception:
                    parsed_detail = {}
                d["detail"] = parsed_detail if isinstance(parsed_detail, dict) else {"value": parsed_detail}
                logs.append(d)
            return {
                "logs": logs,
                "total_count": total_count,
                "page": eff_page,
                "per_page": eff_per_page,
                "total_pages": total_pages,
            }
        finally:
            conn.close()


def resolve_backup_dir(backup_dir: Optional[str] = None) -> str:
    if backup_dir is not None and str(backup_dir).strip():
        return os.path.abspath(str(backup_dir).strip())
    env_dir = (os.getenv("MALL_BACKUP_DIR") or "").strip()
    if env_dir:
        return os.path.abspath(env_dir)
    db_path = mall_database.get_db_path()
    if not db_path or db_path == ":memory:":
        return os.path.join(BASE_DIR, "backups")
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")


def backup_mall_database(backup_dir: Optional[str] = None, keep_last: int = 14) -> dict[str, Any]:
    """SQLite Online Backup API로 일관된 스냅샷 백업을 생성하고 오래된 백업을 순환 삭제합니다."""
    target_dir = resolve_backup_dir(backup_dir)
    os.makedirs(target_dir, exist_ok=True)

    now_dt = datetime.now()
    filename = f"mall_backup_{now_dt.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}.db"
    final_path = os.path.join(target_dir, filename)
    tmp_path = final_path + ".tmp"

    with mall_database.mall_db_lock:
        src_conn = mall_database.get_connection()
        dst_conn: Optional[sqlite3.Connection] = None
        try:
            try:
                dst_conn = sqlite3.connect(tmp_path, timeout=20.0)
                src_conn.backup(dst_conn)
            finally:
                if dst_conn is not None:
                    try:
                        dst_conn.close()
                    except Exception:
                        pass
                try:
                    src_conn.close()
                except Exception:
                    pass
            os.replace(tmp_path, final_path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    size_bytes = int(os.path.getsize(final_path)) if os.path.exists(final_path) else 0

    deleted_old_count = 0
    keep_n = int(keep_last or 0)
    if keep_n > 0:
        try:
            candidates = []
            for entry in os.scandir(target_dir):
                if entry.is_file() and entry.name.startswith("mall_backup_") and entry.name.endswith(".db"):
                    st = entry.stat()
                    candidates.append((st.st_mtime_ns, entry.name, entry.path))
            candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
            for _, _, old_path in candidates[keep_n:]:
                try:
                    os.remove(old_path)
                    deleted_old_count += 1
                except Exception:
                    pass
        except Exception:
            pass

    created_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "ok": True,
        "filename": filename,
        "path": final_path,
        "size": size_bytes,
        "size_bytes": size_bytes,
        "created_at": created_str,
        "deleted_old_count": deleted_old_count,
    }


def list_mall_backups(backup_dir: Optional[str] = None) -> list[dict[str, Any]]:
    target_dir = resolve_backup_dir(backup_dir)
    if not os.path.isdir(target_dir):
        return []
    rows: list[tuple[int, dict[str, Any]]] = []
    try:
        for entry in os.scandir(target_dir):
            if not entry.is_file():
                continue
            if not _BACKUP_FILENAME_RE.fullmatch(entry.name):
                continue
            st = entry.stat()
            created_str = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            rows.append(
                (
                    st.st_mtime_ns,
                    {
                        "filename": entry.name,
                        "size": int(st.st_size),
                        "size_bytes": int(st.st_size),
                        "created_at": created_str,
                    },
                )
            )
    except Exception:
        return []
    rows.sort(key=lambda item: (item[0], item[1]["filename"]), reverse=True)
    return [item[1] for item in rows]


def resolve_backup_filepath(
    filename: str,
    backup_dir: Optional[str] = None,
) -> tuple[bool, Optional[Path], str, int]:
    """백업 파일명을 엄격히 검증하고 경로 역추적(Path Traversal)을 차단합니다."""
    raw = str(filename or "")
    if (
        not raw
        or "/" in raw
        or "\\" in raw
        or ".." in raw
        or "%" in raw
        or "\x00" in raw
        or raw.startswith(".")
        or not _BACKUP_FILENAME_RE.fullmatch(raw)
    ):
        return False, None, "유효하지 않은 백업 파일명입니다.", 400

    base_dir = Path(resolve_backup_dir(backup_dir)).resolve()
    candidate = (base_dir / raw).resolve()
    try:
        candidate.relative_to(base_dir)
    except ValueError:
        return False, None, "허용되지 않은 파일 경로입니다.", 400

    if not candidate.is_file():
        return False, None, "요청하신 백업 파일을 찾을 수 없습니다.", 404

    return True, candidate, "", 200


def _get_obs_db_path() -> str:
    env_p = (os.getenv("OBS_DB_PATH") or "").strip()
    if env_p:
        return env_p
    return os.path.abspath(os.path.join(BASE_DIR, "..", "live_commerce.db"))


def _restore_obs_stock_for_auto_cancel(restored_items: list[dict[str, Any]]) -> int:
    if not restored_items:
        return 0
    if os.getenv("MALL_MODE", "").strip().lower() == "cloud":
        return 0
    obs_db_path = _get_obs_db_path()
    if not obs_db_path or not os.path.exists(obs_db_path):
        return 0

    updated = 0
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(obs_db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='product_options'")
        if not cur.fetchone():
            return 0
        for it in restored_items:
            p_num = int(it.get("product_num") or 0)
            color = str(it.get("color") or "-")
            size = str(it.get("size") or "-")
            qty = max(0, int(it.get("quantity") or 0))
            if p_num <= 0 or qty <= 0:
                continue
            cur.execute("SELECT id FROM products WHERE num = ? LIMIT 1", (p_num,))
            p_row = cur.fetchone()
            if not p_row:
                continue
            obs_pid = int(p_row["id"])
            cur.execute(
                "UPDATE product_options SET stock = stock + ? WHERE product_id = ? AND color = ? AND size = ?",
                (qty, obs_pid, color, size),
            )
            if cur.rowcount == 0:
                cur.execute(
                    "SELECT id FROM product_options WHERE product_id = ? ORDER BY id ASC LIMIT 1",
                    (obs_pid,),
                )
                fb = cur.fetchone()
                if fb:
                    cur.execute(
                        "UPDATE product_options SET stock = stock + ? WHERE id = ?",
                        (qty, int(fb["id"])),
                    )
            if cur.rowcount > 0:
                updated += 1
        conn.commit()
    except Exception as exc:
        print(f"[ShoppingMall] 자동취소 OBS DB 재고 복구 경고: {exc}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return updated


def cancel_expired_unpaid_orders(expire_hours: Optional[int] = None) -> dict[str, Any]:
    """입금대기 기한이 경과한 미입금 주문을 자동 취소하고 재고·쿠폰·적립금·알림을 원자적으로 처리합니다."""
    if expire_hours is None:
        store_info = mall_database.get_mall_store_info()
        raw_hours = store_info.get("unpaid_cancel_hours")
        hours = int(raw_hours) if raw_hours is not None else 72
    else:
        hours = int(expire_hours)

    if hours <= 0:
        return {
            "ok": True,
            "enabled": False,
            "expire_hours": 0,
            "checked_count": 0,
            "cancelled_count": 0,
            "cancelled_orders": [],
            "obs_stock_restored": 0,
        }

    now_dt = datetime.now()
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    cutoff_str = (now_dt - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    claim_reason = f"미입금 자동 취소 ({hours}시간 경과)"

    restock_events: list[dict[str, Any]] = []
    all_restored_items: list[dict[str, Any]] = []
    cancelled_orders_payload: list[dict[str, Any]] = []
    cancelled_codes: list[str] = []
    checked_count = 0

    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT * FROM mall_orders
                WHERE payment_status = '입금대기'
                  AND order_status IN ('결제대기', '상품준비중')
                  AND created_at <= ?
                ORDER BY id ASC
                """,
                (cutoff_str,),
            )
            expired_rows = cur.fetchall()
            checked_count = len(expired_rows)

            if expired_rows:
                import mall_promotions

                for row in expired_rows:
                    oid = int(row["id"])
                    ocode = str(row["order_code"] or "").strip()
                    already_restored = int(row["stock_restored"] or 0) if "stock_restored" in row.keys() else 0
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
                                    ps, pt = mall_database._snapshot_product_option_stocks(cur, mall_pid)
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
                            all_restored_items.append(
                                {
                                    "product_num": p_num,
                                    "color": color,
                                    "size": size,
                                    "quantity": qty,
                                }
                            )
                        for mall_pid, (p_num, p_name, ps, pt) in affected_products.items():
                            restock_events.extend(
                                mall_database._collect_restock_notifications_tx(
                                    cur, mall_pid, p_num, p_name, ps, pt, now_str
                                )
                            )

                    cur.execute(
                        """
                        UPDATE mall_orders
                        SET order_status = '주문취소',
                            claim_reason = ?,
                            claim_updated_at = ?,
                            stock_restored = 1
                        WHERE id = ?
                          AND payment_status = '입금대기'
                          AND order_status IN ('결제대기', '상품준비중')
                        """,
                        (claim_reason, now_str, oid),
                    )
                    if cur.rowcount == 1:
                        mall_promotions.restore_order_promotions_on_cancel(conn, oid, now_str)
                        cur.execute("SELECT * FROM mall_orders WHERE id = ? LIMIT 1", (oid,))
                        updated_row = cur.fetchone()
                        cur.execute("SELECT * FROM mall_order_items WHERE order_id = ? ORDER BY id ASC", (oid,))
                        items = [dict(ir) for ir in cur.fetchall()]
                        if updated_row:
                            cancelled_orders_payload.append(mall_database._format_order_row(updated_row, items))
                        cancelled_codes.append(ocode)

                if cancelled_codes:
                    conn.commit()
                    mall_database.bump_state_version(bump_catalog=True)
        finally:
            conn.close()

    if restock_events:
        mall_database._dispatch_restock_notifications(restock_events)

    obs_restored = _restore_obs_stock_for_auto_cancel(all_restored_items) if cancelled_codes else 0

    if cancelled_orders_payload:
        try:
            import mall_notifications

            for ord_obj in cancelled_orders_payload:
                try:
                    mall_notifications.notify_order_auto_cancelled(ord_obj, expire_hours=hours)
                except Exception as notify_err:
                    print(f"[ShoppingMall] ORDER_AUTO_CANCELLED 알림 경고: {notify_err}")
        except Exception:
            pass

    if cancelled_codes:
        log_admin_action(
            admin_user={"id": None, "email": "system@scheduler", "name": "자동 스케줄러"},
            action="AUTO_CANCEL_UNPAID_ORDERS",
            target_type="order_batch",
            target_id=",".join(cancelled_codes)[:120],
            detail_dict={
                "expire_hours": hours,
                "cancelled_count": len(cancelled_codes),
                "cancelled_orders": cancelled_codes,
                "obs_stock_restored": obs_restored,
            },
            ip_addr="SYSTEM",
        )

    return {
        "ok": True,
        "enabled": True,
        "expire_hours": hours,
        "checked_count": checked_count,
        "cancelled_count": len(cancelled_codes),
        "cancelled_orders": cancelled_codes,
        "obs_stock_restored": obs_restored,
    }


def _maintenance_loop(stop_event: threading.Event, interval_sec: float, backup_interval_sec: float) -> None:
    last_backup_ts = time.monotonic()
    while not stop_event.wait(interval_sec):
        try:
            cancel_expired_unpaid_orders()
        except Exception as exc:
            print(f"[ShoppingMall] 자동 취소 스케줄러 오류: {exc}")
        now_mono = time.monotonic()
        if (now_mono - last_backup_ts) >= backup_interval_sec:
            try:
                backup_mall_database()
                last_backup_ts = now_mono
            except Exception as exc:
                print(f"[ShoppingMall] 자동 백업 스케줄러 오류: {exc}")


def _resolve_scheduler_intervals() -> tuple[float, float]:
    try:
        interval_sec = float(os.getenv("MALL_MAINTENANCE_INTERVAL_SEC", "300") or 300)
    except Exception:
        interval_sec = 300.0
    interval_sec = max(5.0, interval_sec)

    try:
        backup_hours = float(os.getenv("MALL_BACKUP_INTERVAL_HOURS", "24") or 24)
    except Exception:
        backup_hours = 24.0
    backup_interval_sec = max(60.0, backup_hours * 3600.0)
    return interval_sec, backup_interval_sec


def start_maintenance_scheduler() -> bool:
    """싱글톤 백그라운드 유지보수(미입금 자동취소·일일 백업) 스케줄러 스레드를 시작합니다."""
    global _SCHEDULER_STOP_EVENT, _SCHEDULER_THREAD
    interval_sec, backup_interval_sec = _resolve_scheduler_intervals()
    with _SCHEDULER_LOCK:
        if _SCHEDULER_THREAD is not None and _SCHEDULER_THREAD.is_alive():
            return False
        stop_ev = threading.Event()
        t = threading.Thread(
            target=_maintenance_loop,
            args=(stop_ev, interval_sec, backup_interval_sec),
            name="mall-maintenance-scheduler",
            daemon=True,
        )
        _SCHEDULER_STOP_EVENT = stop_ev
        _SCHEDULER_THREAD = t
        t.start()
        return True


def stop_maintenance_scheduler(timeout_sec: float = 2.0) -> bool:
    """실행 중인 유지보수 스케줄러 스레드를 안전하게 중지합니다."""
    global _SCHEDULER_STOP_EVENT, _SCHEDULER_THREAD
    with _SCHEDULER_LOCK:
        stop_ev = _SCHEDULER_STOP_EVENT
        t = _SCHEDULER_THREAD
        _SCHEDULER_STOP_EVENT = None
        _SCHEDULER_THREAD = None
    if stop_ev is not None:
        stop_ev.set()
    if t is not None and t.is_alive():
        t.join(timeout=max(0.1, float(timeout_sec or 2.0)))
        return not t.is_alive()
    return True


def get_scheduler_status() -> dict[str, Any]:
    interval_sec, backup_interval_sec = _resolve_scheduler_intervals()
    with _SCHEDULER_LOCK:
        running = bool(_SCHEDULER_THREAD is not None and _SCHEDULER_THREAD.is_alive())
    return {
        "running": running,
        "interval_sec": interval_sec,
        "backup_interval_hours": round(backup_interval_sec / 3600.0, 2),
    }


@ops_bp.route("/api/admin/audit-logs", methods=["GET"])
def api_admin_audit_logs():
    action = str(request.args.get("action") or "")
    target_type = str(request.args.get("target_type") or "")
    query = str(request.args.get("q") or request.args.get("actor") or "")
    page = request.args.get("page", 1)
    per_page = request.args.get("per_page", 20)
    limit = request.args.get("limit")

    res = list_admin_audit_logs(
        action=action,
        target_type=target_type,
        query=query,
        page=page,
        per_page=per_page,
        limit=limit,
    )
    return jsonify({"status": "ok", "ok": True, **res}), 200


@ops_bp.route("/api/admin/system/backup", methods=["POST"])
def api_admin_system_backup():
    res = backup_mall_database()
    backups = list_mall_backups()
    backup_meta = {
        "filename": res["filename"],
        "size": res["size"],
        "size_bytes": res["size_bytes"],
        "created_at": res["created_at"],
    }
    log_current_admin_action(
        action="SYSTEM_DB_BACKUP",
        target_type="backup",
        target_id=res["filename"],
        detail_dict={
            "filename": res["filename"],
            "size_bytes": res["size_bytes"],
            "deleted_old_count": res.get("deleted_old_count", 0),
        },
    )
    return jsonify(
        {
            "status": "ok",
            "ok": True,
            "filename": res["filename"],
            "backup": backup_meta,
            "backups": backups,
        }
    ), 200


@ops_bp.route("/api/admin/system/backups", methods=["GET"])
def api_admin_system_backups():
    backups = list_mall_backups()
    sched = get_scheduler_status()
    sched["enabled"] = bool(current_app.config.get("MALL_SCHEDULER_ENABLED"))
    return jsonify(
        {
            "status": "ok",
            "ok": True,
            "backups": backups,
            "count": len(backups),
            "scheduler": sched,
        }
    ), 200


@ops_bp.route("/api/admin/system/backup/<path:filename>", methods=["GET"])
def api_admin_system_backup_download(filename: str):
    ok, target_path, err_msg, status_code = resolve_backup_filepath(filename)
    if not ok or target_path is None:
        return jsonify({"status": "error", "ok": False, "message": err_msg}), status_code
    return send_file(
        str(target_path),
        as_attachment=True,
        download_name=target_path.name,
        mimetype="application/octet-stream",
    )


def _parse_valid_backdate_hours(raw_val: Any) -> Optional[float]:
    if isinstance(raw_val, bool):
        return None
    if isinstance(raw_val, str):
        s = raw_val.strip()
        if not s or len(s) > 100:
            return None
        raw_val = s
    try:
        hrs = float(raw_val)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(hrs) or hrs <= 0 or hrs > 87600:
        return None
    return hrs


@ops_bp.route("/api/admin/system/run-maintenance", methods=["POST"])
def api_admin_system_run_maintenance():
    if request.is_json:
        raw_payload = request.get_json(silent=True)
        if raw_payload is None:
            raw_bytes = request.get_data() or b""
            if raw_bytes.strip():
                return jsonify({"status": "error", "ok": False, "message": "유효한 JSON 객체 형식이 아닙니다."}), 400
            data = {}
        elif not isinstance(raw_payload, dict):
            return jsonify({"status": "error", "ok": False, "message": "요청 본문은 JSON 객체(dict)여야 합니다."}), 400
        else:
            data = raw_payload
    else:
        raw_payload = request.get_json(silent=True)
        if raw_payload is not None and not isinstance(raw_payload, dict):
            return jsonify({"status": "error", "ok": False, "message": "요청 본문은 JSON 객체(dict)여야 합니다."}), 400
        data = raw_payload if isinstance(raw_payload, dict) else {}

    raw_backdate_orders = data.get("backdate_orders")
    raw_backdate_code = str(data.get("backdate_order_code") or "").strip()
    raw_backdate_hours = data.get("backdate_hours")
    has_backdate_params = bool(
        (raw_backdate_orders not in (None, "", []))
        or raw_backdate_code
        or (raw_backdate_hours not in (None, ""))
    )

    backdated_count = 0
    if has_backdate_params:
        if os.getenv("MALL_ALLOW_TEST_BACKDATE", "").strip() != "1":
            return jsonify(
                {
                    "status": "error",
                    "ok": False,
                    "code": "BACKDATE_DISABLED",
                    "message": "테스트용 주문 일시 변경(backdate)이 허용되지 않았습니다.",
                }
            ), 403

        if raw_backdate_orders not in (None, "") and not isinstance(raw_backdate_orders, list):
            return jsonify(
                {
                    "status": "error",
                    "ok": False,
                    "message": "backdate_orders는 배열(list) 형식이어야 합니다.",
                }
            ), 400

        targets: list[tuple[str, float]] = []
        if isinstance(raw_backdate_orders, list):
            for entry in raw_backdate_orders:
                if not isinstance(entry, dict):
                    return jsonify(
                        {
                            "status": "error",
                            "ok": False,
                            "message": "backdate_orders 항목 형식이 올바르지 않습니다.",
                        }
                    ), 400
                oc = str(entry.get("order_code") or "").strip()
                raw_entry_hours = entry.get("hours")
                if raw_entry_hours is not None and raw_entry_hours != "":
                    hrs = _parse_valid_backdate_hours(raw_entry_hours)
                    if hrs is None:
                        return jsonify(
                            {
                                "status": "error",
                                "ok": False,
                                "message": "유효하지 않은 backdate hours 값입니다. (0 초과 87600 이하)",
                            }
                        ), 400
                    if oc:
                        targets.append((oc, hrs))
        if raw_backdate_hours not in (None, ""):
            top_hrs = _parse_valid_backdate_hours(raw_backdate_hours)
            if top_hrs is None:
                return jsonify(
                    {
                        "status": "error",
                        "ok": False,
                        "message": "유효하지 않은 backdate_hours 값입니다. (0 초과 87600 이하)",
                    }
                ), 400
            if raw_backdate_code:
                targets.append((raw_backdate_code, top_hrs))

        if targets:
            with mall_database.mall_db_lock:
                conn = mall_database.get_connection()
                try:
                    cur = conn.cursor()
                    now_dt = datetime.now()
                    for oc, hrs in targets:
                        ts = (now_dt - timedelta(hours=hrs)).strftime("%Y-%m-%d %H:%M:%S")
                        cur.execute(
                            "UPDATE mall_orders SET created_at = ? WHERE order_code = ?",
                            (ts, oc),
                        )
                        backdated_count += int(cur.rowcount or 0)
                    conn.commit()
                finally:
                    conn.close()

    cancel_res = cancel_expired_unpaid_orders()

    backup_meta = None
    if bool(data.get("run_backup")):
        b_res = backup_mall_database()
        backup_meta = {
            "filename": b_res["filename"],
            "size": b_res["size"],
            "size_bytes": b_res["size_bytes"],
            "created_at": b_res["created_at"],
        }

    log_current_admin_action(
        action="SYSTEM_MAINTENANCE_RUN",
        target_type="system",
        target_id="maintenance",
        detail_dict={
            "cancelled_count": cancel_res.get("cancelled_count", 0),
            "cancelled_orders": cancel_res.get("cancelled_orders", []),
            "obs_stock_restored": cancel_res.get("obs_stock_restored", 0),
            "backdated_count": backdated_count,
            "run_backup": bool(data.get("run_backup")),
        },
    )

    return jsonify(
        {
            "status": "ok",
            "ok": True,
            **cancel_res,
            "backdated_count": backdated_count,
            "backup": backup_meta,
        }
    ), 200
