# -*- coding: utf-8 -*-
"""
쇼핑몰 고객센터 · 상품 Q&A(비밀글) · 1:1 문의 · 공지사항 · FAQ 서비스 (mall_cs_service.py)
- 비밀글(is_secret=1) 본문/답변은 서버 측(_format_qna_row)에서 작성자·관리자 외 무조건 마스킹
- 1:1 문의는 user_id 소유권 엄격 검증 (IDOR 방지)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import mall_database

INQUIRY_STATUSES = ("접수대기", "처리중", "답변완료")
INQUIRY_CATEGORIES = ("주문/결제", "배송", "취소/교환/반품", "상품문의", "회원/기타", "일반문의", "배송문의")


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_bool_flag(val: Any, default: bool = False) -> bool:
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


def _format_qna_row(row: Any, viewer: Optional[dict] = None, product_name: str = "") -> dict[str, Any]:
    d = dict(row)
    viewer_id = int(viewer["id"]) if viewer and viewer.get("id") else 0
    is_admin = bool(viewer and str(viewer.get("role") or "").upper() == "ADMIN")
    row_uid = int(d.get("user_id") or 0)
    is_owner = bool(viewer_id and row_uid == viewer_id)
    is_secret = bool(int(d.get("is_secret") or 0))
    is_masked = bool(is_secret and not is_owner and not is_admin)

    raw_answer = str(d.get("answer_content") or "").strip()
    has_answer = bool(raw_answer)

    content = str(d.get("content") or "")
    answer_content = str(d.get("answer_content") or "")
    title = str(d.get("title") or "").strip()

    if is_masked:
        title = "비밀글입니다."
        content = "비밀글입니다. 작성자와 관리자만 확인할 수 있습니다."
        answer_content = "비밀글 답변입니다." if has_answer else ""
        exposed_uid = None
    else:
        exposed_uid = row_uid

    return {
        "id": int(d["id"]),
        "product_num": int(d["product_num"]),
        "product_name": str(product_name or d.get("product_name") or ""),
        "user_id": exposed_uid,
        "author_name": str(d.get("author_name") or "회원"),
        "title": title,
        "content": content,
        "is_secret": is_secret,
        "is_masked": is_masked,
        "is_owner": is_owner,
        "is_answered": has_answer,
        "answer_content": answer_content,
        "answered_by": str(d.get("answered_by") or ""),
        "answered_at": str(d.get("answered_at") or ""),
        "created_at": str(d.get("created_at") or ""),
        "updated_at": str(d.get("updated_at") or ""),
    }


def list_product_qna(product_num: int, viewer: Optional[dict] = None, limit: int = 100) -> list[dict[str, Any]]:
    p_num = mall_database.parse_strict_int64(product_num, min_val=1)
    if p_num is None:
        return []
    eff_limit = mall_database._parse_clamped_query_int(limit, 100, 1, 500)
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM mall_product_qna WHERE product_num = ? ORDER BY id DESC LIMIT ?",
                (p_num, eff_limit),
            )
            return [_format_qna_row(r, viewer=viewer) for r in cur.fetchall()]
        finally:
            conn.close()


def list_admin_qna(
    product_num: Optional[int] = None,
    status_filter: str = "ALL",
    limit: int = 200,
) -> list[dict[str, Any]]:
    st = str(status_filter or "ALL").strip().upper()
    eff_limit = mall_database._parse_clamped_query_int(limit, 200, 1, 500)
    where = []
    params: list[Any] = []
    if product_num is not None and str(product_num).strip() != "":
        p_num = mall_database.parse_strict_int64(product_num, min_val=1)
        if p_num is None:
            return []
        where.append("q.product_num = ?")
        params.append(p_num)
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            if st == "WAITING":
                where.append("COALESCE(TRIM(q.answer_content), '') = ''")
            elif st == "ANSWERED":
                where.append("COALESCE(TRIM(q.answer_content), '') != ''")
            where_sql = f"WHERE {' AND '.join(where)}" if where else ""
            cur.execute(
                f"""
                SELECT q.*, COALESCE(p.name, '') AS product_name
                FROM mall_product_qna q
                LEFT JOIN mall_products p ON p.num = q.product_num
                {where_sql}
                ORDER BY q.id DESC
                LIMIT ?
                """,
                (*params, eff_limit),
            )
            admin_viewer = {"id": 0, "role": "ADMIN"}
            return [
                _format_qna_row(r, viewer=admin_viewer, product_name=str(r["product_name"] or ""))
                for r in cur.fetchall()
            ]
        finally:
            conn.close()


def create_product_qna(product_num: int, user: dict, data: dict) -> dict[str, Any]:
    if not user or not user.get("id"):
        return {"ok": False, "message": "로그인이 필요합니다."}
    p_num = mall_database.parse_strict_int64(product_num, min_val=1)
    if p_num is None:
        return {"ok": False, "not_found": True, "message": "상품 번호가 올바르지 않습니다."}

    raw_content = data.get("content")
    if raw_content is None or str(raw_content).strip() == "":
        raw_content = data.get("question")
    content = str(raw_content or "").strip()
    if len(content) < 2:
        return {"ok": False, "message": "문의 내용을 2자 이상 입력해 주세요."}
    if len(content) > 2000:
        return {"ok": False, "message": "문의 내용은 2,000자 이내로 입력해 주세요."}

    raw_title = str(data.get("title") or "").strip()
    if raw_title:
        title = raw_title[:120]
    else:
        first_line = content.splitlines()[0].strip() if content.splitlines() else ""
        title = (first_line or content)[:40]
        if len(title) < 2:
            title = content[:40]
    is_secret = 1 if _parse_bool_flag(data.get("is_secret"), default=False) else 0
    author_name = str(user.get("name") or "회원").strip()[:40] or "회원"
    uid = int(user["id"])
    now = _now_str()

    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT num, name FROM mall_products WHERE num = ? LIMIT 1", (p_num,))
            prod = cur.fetchone()
            if not prod:
                return {"ok": False, "not_found": True, "message": "상품을 찾을 수 없습니다."}
            cur.execute(
                """
                INSERT INTO mall_product_qna
                (product_num, user_id, author_name, title, content, is_secret,
                 answer_content, answered_by, answered_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, '', '', '', ?, ?)
                """,
                (p_num, uid, author_name, title, content, is_secret, now, now),
            )
            qid = int(cur.lastrowid)
            conn.commit()
            cur.execute("SELECT * FROM mall_product_qna WHERE id = ? LIMIT 1", (qid,))
            row = cur.fetchone()
            return {"ok": True, "item": _format_qna_row(row, viewer=user, product_name=str(prod["name"] or ""))}
        finally:
            conn.close()


def answer_product_qna(qna_id: Any, answer_content: str, admin_user: dict) -> dict[str, Any]:
    qid = mall_database.parse_strict_int64(qna_id, min_val=1)
    if qid is None:
        return {"ok": False, "message": "유효하지 않은 Q&A ID입니다."}
    ans = str(answer_content or "").strip()
    if not ans:
        return {"ok": False, "message": "답변 내용을 입력해 주세요."}
    if len(ans) > 2000:
        return {"ok": False, "message": "답변 내용은 2,000자 이내로 입력해 주세요."}
    answered_by = str((admin_user or {}).get("name") or "관리자").strip()[:40] or "관리자"
    now = _now_str()

    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM mall_product_qna WHERE id = ? LIMIT 1", (qid,))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "not_found": True, "message": "Q&A 글을 찾을 수 없습니다."}
            cur.execute(
                """
                UPDATE mall_product_qna
                SET answer_content = ?, answered_by = ?, answered_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (ans, answered_by, now, now, qid),
            )
            conn.commit()
            cur.execute("SELECT * FROM mall_product_qna WHERE id = ? LIMIT 1", (qid,))
            updated = cur.fetchone()
            return {"ok": True, "item": _format_qna_row(updated, viewer={"id": 0, "role": "ADMIN"})}
        finally:
            conn.close()


def delete_product_qna(qna_id: Any) -> dict[str, Any]:
    qid = mall_database.parse_strict_int64(qna_id, min_val=1)
    if qid is None:
        return {"ok": False, "message": "유효하지 않은 Q&A ID입니다."}
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM mall_product_qna WHERE id = ? LIMIT 1", (qid,))
            if not cur.fetchone():
                return {"ok": False, "not_found": True, "message": "Q&A 글을 찾을 수 없습니다."}
            cur.execute("DELETE FROM mall_product_qna WHERE id = ?", (qid,))
            conn.commit()
            return {"ok": True, "deleted_id": qid}
        finally:
            conn.close()


def _format_inquiry_row(row: Any) -> dict[str, Any]:
    d = dict(row)
    return {
        "id": int(d["id"]),
        "user_id": int(d["user_id"]),
        "author_name": str(d.get("author_name") or ""),
        "author_email": str(d.get("author_email") or ""),
        "category": str(d.get("category") or "일반문의"),
        "title": str(d.get("title") or ""),
        "content": str(d.get("content") or ""),
        "order_code": str(d.get("order_code") or ""),
        "status": str(d.get("status") or "접수대기"),
        "answer_content": str(d.get("answer_content") or ""),
        "answered_by": str(d.get("answered_by") or ""),
        "answered_at": str(d.get("answered_at") or ""),
        "created_at": str(d.get("created_at") or ""),
        "updated_at": str(d.get("updated_at") or ""),
    }


def list_user_inquiries(user_id: int, limit: int = 100) -> list[dict[str, Any]]:
    if not user_id:
        return []
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT i.*, COALESCE(u.name, '') AS author_name, COALESCE(u.email, '') AS author_email
                FROM mall_inquiries i
                LEFT JOIN mall_users u ON u.id = i.user_id
                WHERE i.user_id = ?
                ORDER BY i.id DESC
                LIMIT ?
                """,
                (int(user_id), int(limit)),
            )
            return [_format_inquiry_row(r) for r in cur.fetchall()]
        finally:
            conn.close()


def list_admin_inquiries(status_filter: str = "ALL", limit: int = 200) -> list[dict[str, Any]]:
    st = str(status_filter or "ALL").strip()
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            if st and st.upper() != "ALL" and st in INQUIRY_STATUSES:
                cur.execute(
                    """
                    SELECT i.*, COALESCE(u.name, '') AS author_name, COALESCE(u.email, '') AS author_email
                    FROM mall_inquiries i
                    LEFT JOIN mall_users u ON u.id = i.user_id
                    WHERE i.status = ?
                    ORDER BY i.id DESC
                    LIMIT ?
                    """,
                    (st, int(limit)),
                )
            else:
                cur.execute(
                    """
                    SELECT i.*, COALESCE(u.name, '') AS author_name, COALESCE(u.email, '') AS author_email
                    FROM mall_inquiries i
                    LEFT JOIN mall_users u ON u.id = i.user_id
                    ORDER BY i.id DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                )
            return [_format_inquiry_row(r) for r in cur.fetchall()]
        finally:
            conn.close()


def create_inquiry(user: dict, data: dict) -> dict[str, Any]:
    if not user or not user.get("id"):
        return {"ok": False, "message": "로그인이 필요합니다."}
    title = str(data.get("title") or "").strip()
    content = str(data.get("content") or "").strip()
    if len(title) < 2 or len(title) > 120:
        return {"ok": False, "message": "문의 제목을 2자 이상 120자 이내로 입력해 주세요."}
    if len(content) < 2 or len(content) > 2000:
        return {"ok": False, "message": "문의 내용을 2자 이상 2,000자 이내로 입력해 주세요."}

    raw_cat = str(data.get("category") or "일반문의").strip()
    category = raw_cat if raw_cat in INQUIRY_CATEGORIES else "일반문의"
    order_code = str(data.get("order_code") or "").strip()[:40]
    uid = int(user["id"])
    now = _now_str()

    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO mall_inquiries
                (user_id, category, title, content, order_code, status,
                 answer_content, answered_by, answered_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, '접수대기', '', '', '', ?, ?)
                """,
                (uid, category, title, content, order_code, now, now),
            )
            inq_id = int(cur.lastrowid)
            conn.commit()
            cur.execute(
                """
                SELECT i.*, COALESCE(u.name, '') AS author_name, COALESCE(u.email, '') AS author_email
                FROM mall_inquiries i
                LEFT JOIN mall_users u ON u.id = i.user_id
                WHERE i.id = ? LIMIT 1
                """,
                (inq_id,),
            )
            return {"ok": True, "item": _format_inquiry_row(cur.fetchone())}
        finally:
            conn.close()


def answer_inquiry(
    inquiry_id: Any,
    answer_content: str,
    admin_user: dict,
    status: str = "답변완료",
) -> dict[str, Any]:
    iid = mall_database.parse_strict_int64(inquiry_id, min_val=1)
    if iid is None:
        return {"ok": False, "message": "유효하지 않은 문의 ID입니다."}
    ans = str(answer_content or "").strip()
    if not ans:
        return {"ok": False, "message": "답변 내용을 입력해 주세요."}
    if len(ans) > 2000:
        return {"ok": False, "message": "답변 내용은 2,000자 이내로 입력해 주세요."}

    next_status = str(status or "답변완료").strip()
    if next_status not in INQUIRY_STATUSES:
        next_status = "답변완료"
    answered_by = str((admin_user or {}).get("name") or "관리자").strip()[:40] or "관리자"
    now = _now_str()

    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM mall_inquiries WHERE id = ? LIMIT 1", (iid,))
            if not cur.fetchone():
                return {"ok": False, "not_found": True, "message": "1:1 문의를 찾을 수 없습니다."}
            cur.execute(
                """
                UPDATE mall_inquiries
                SET status = ?, answer_content = ?, answered_by = ?, answered_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_status, ans, answered_by, now, now, iid),
            )
            conn.commit()
            cur.execute(
                """
                SELECT i.*, COALESCE(u.name, '') AS author_name, COALESCE(u.email, '') AS author_email
                FROM mall_inquiries i
                LEFT JOIN mall_users u ON u.id = i.user_id
                WHERE i.id = ? LIMIT 1
                """,
                (iid,),
            )
            return {"ok": True, "item": _format_inquiry_row(cur.fetchone())}
        finally:
            conn.close()


def list_notices(limit: int = 100) -> list[dict[str, Any]]:
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM mall_notices ORDER BY is_pinned DESC, id DESC LIMIT ?",
                (int(limit),),
            )
            out = []
            for r in cur.fetchall():
                d = dict(r)
                d["id"] = int(d["id"])
                d["is_pinned"] = bool(int(d.get("is_pinned") or 0))
                out.append(d)
            return out
        finally:
            conn.close()


def save_notice(data: dict, admin_user: Optional[dict] = None) -> dict[str, Any]:
    raw_nid = data.get("id")
    if isinstance(raw_nid, (bool, float)):
        return {"ok": False, "message": "유효하지 않은 공지 ID입니다."}
    if raw_nid in (None, "", 0, "0"):
        nid = 0
    else:
        nid = mall_database.parse_strict_int64(raw_nid, min_val=1)
        if nid is None:
            return {"ok": False, "message": "유효하지 않은 공지 ID입니다."}
    title = str(data.get("title") or "").strip()
    content = str(data.get("content") or "").strip()
    if len(title) < 2 or len(title) > 160:
        return {"ok": False, "message": "공지 제목을 2자 이상 160자 이내로 입력해 주세요."}
    if len(content) < 2 or len(content) > 5000:
        return {"ok": False, "message": "공지 내용을 2자 이상 5,000자 이내로 입력해 주세요."}
    is_pinned = 1 if _parse_bool_flag(data.get("is_pinned"), default=False) else 0
    author_name = str((admin_user or {}).get("name") or "관리자").strip()[:40] or "관리자"
    now = _now_str()

    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            if nid > 0:
                cur.execute("SELECT id FROM mall_notices WHERE id = ? LIMIT 1", (nid,))
                if not cur.fetchone():
                    return {"ok": False, "not_found": True, "message": "수정할 공지사항을 찾을 수 없습니다."}
                cur.execute(
                    """
                    UPDATE mall_notices
                    SET title = ?, content = ?, is_pinned = ?, author_name = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (title, content, is_pinned, author_name, now, nid),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO mall_notices (title, content, is_pinned, author_name, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (title, content, is_pinned, author_name, now, now),
                )
                nid = int(cur.lastrowid)
            conn.commit()
            cur.execute("SELECT * FROM mall_notices WHERE id = ? LIMIT 1", (nid,))
            row = dict(cur.fetchone())
            row["id"] = int(row["id"])
            row["is_pinned"] = bool(int(row.get("is_pinned") or 0))
            return {"ok": True, "item": row}
        finally:
            conn.close()


def delete_notice(notice_id: Any) -> dict[str, Any]:
    nid = mall_database.parse_strict_int64(notice_id, min_val=1)
    if nid is None:
        return {"ok": False, "message": "유효하지 않은 공지 ID입니다."}
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM mall_notices WHERE id = ? LIMIT 1", (nid,))
            if not cur.fetchone():
                return {"ok": False, "not_found": True, "message": "공지사항을 찾을 수 없습니다."}
            cur.execute("DELETE FROM mall_notices WHERE id = ?", (nid,))
            conn.commit()
            return {"ok": True, "deleted_id": nid}
        finally:
            conn.close()


def list_faqs(active_only: bool = True, category: str = "ALL", limit: int = 200) -> list[dict[str, Any]]:
    clean_cat = str(category or "ALL").strip()
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            where = []
            params: list[Any] = []
            if active_only:
                where.append("is_active = 1")
            if clean_cat and clean_cat.upper() != "ALL":
                where.append("category = ?")
                params.append(clean_cat)
            where_sql = f"WHERE {' AND '.join(where)}" if where else ""
            cur.execute(
                f"SELECT * FROM mall_faqs {where_sql} ORDER BY sort_order ASC, id ASC LIMIT ?",
                (*params, int(limit)),
            )
            out = []
            for r in cur.fetchall():
                d = dict(r)
                d["id"] = int(d["id"])
                d["sort_order"] = int(d.get("sort_order") or 10)
                d["is_active"] = bool(int(d.get("is_active") or 0))
                out.append(d)
            return out
        finally:
            conn.close()


def save_faq(data: dict) -> dict[str, Any]:
    raw_fid = data.get("id")
    if isinstance(raw_fid, (bool, float)):
        return {"ok": False, "message": "유효하지 않은 FAQ ID입니다."}
    if raw_fid in (None, "", 0, "0"):
        fid = 0
    else:
        fid = mall_database.parse_strict_int64(raw_fid, min_val=1)
        if fid is None:
            return {"ok": False, "message": "유효하지 않은 FAQ ID입니다."}
    category = str(data.get("category") or "주문/결제").strip()[:40] or "주문/결제"
    question = str(data.get("question") or "").strip()
    answer = str(data.get("answer") or "").strip()
    if len(question) < 2 or len(question) > 300:
        return {"ok": False, "message": "FAQ 질문을 2자 이상 300자 이내로 입력해 주세요."}
    if len(answer) < 2 or len(answer) > 3000:
        return {"ok": False, "message": "FAQ 답변을 2자 이상 3,000자 이내로 입력해 주세요."}
    raw_sort = data.get("sort_order")
    if raw_sort is None or str(raw_sort).strip() == "":
        sort_order = 10
    else:
        sort_order = mall_database.parse_strict_int64(raw_sort, min_val=-1_000_000, max_val=1_000_000)
        if sort_order is None:
            return {"ok": False, "message": "정렬 순서는 올바른 정수로 입력해 주세요."}
    is_active = 1 if _parse_bool_flag(data.get("is_active"), default=True) else 0
    now = _now_str()

    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            if fid > 0:
                cur.execute("SELECT id FROM mall_faqs WHERE id = ? LIMIT 1", (fid,))
                if not cur.fetchone():
                    return {"ok": False, "not_found": True, "message": "수정할 FAQ를 찾을 수 없습니다."}
                cur.execute(
                    """
                    UPDATE mall_faqs
                    SET category = ?, question = ?, answer = ?, sort_order = ?, is_active = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (category, question, answer, sort_order, is_active, now, fid),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO mall_faqs (category, question, answer, sort_order, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (category, question, answer, sort_order, is_active, now, now),
                )
                fid = int(cur.lastrowid)
            conn.commit()
            cur.execute("SELECT * FROM mall_faqs WHERE id = ? LIMIT 1", (fid,))
            row = dict(cur.fetchone())
            row["id"] = int(row["id"])
            row["sort_order"] = int(row.get("sort_order") or 10)
            row["is_active"] = bool(int(row.get("is_active") or 0))
            return {"ok": True, "item": row}
        finally:
            conn.close()


def delete_faq(faq_id: Any) -> dict[str, Any]:
    fid = mall_database.parse_strict_int64(faq_id, min_val=1)
    if fid is None:
        return {"ok": False, "message": "유효하지 않은 FAQ ID입니다."}
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM mall_faqs WHERE id = ? LIMIT 1", (fid,))
            if not cur.fetchone():
                return {"ok": False, "not_found": True, "message": "FAQ를 찾을 수 없습니다."}
            cur.execute("DELETE FROM mall_faqs WHERE id = ?", (fid,))
            conn.commit()
            return {"ok": True, "deleted_id": fid}
        finally:
            conn.close()
