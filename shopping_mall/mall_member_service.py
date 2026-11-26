# -*- coding: utf-8 -*-
"""
쇼핑몰 회원 마이페이지 · 다중 배송지 · 비밀번호 재설정 · 탈퇴 · 관리자 회원 관리 서비스 (mall_member_service.py)
- 단독 배포(shopping_mall/) 경계를 유지하며 c:\\live\\ 상위 모듈을 임포트하지 않습니다.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import time
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Optional

import mall_database
import mall_security


RESET_TTL_SECONDS = 1800
RESET_MAX_ATTEMPTS = 5


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _normalize_zipcode(val: Any) -> str:
    return re.sub(r"\s+", "", str(val or "").strip())[:16]


def _hash_reset_secret(raw_value: str, purpose: str) -> str:
    msg = f"{purpose}:{str(raw_value or '').strip()}".encode("utf-8")
    return hashlib.sha256(msg).hexdigest()


def _format_address_row(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "user_id": int(row["user_id"]),
        "label": str(row["label"] or "배송지"),
        "recipient_name": str(row["recipient_name"] or ""),
        "recipient_phone": str(row["recipient_phone"] or ""),
        "zipcode": _normalize_zipcode(row["zipcode"]),
        "address": str(row["address"] or ""),
        "address_detail": str(row["address_detail"] or ""),
        "is_default": bool(int(row["is_default"] or 0)),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }


def _sync_user_default_address_fields(
    cur,
    user_id: int,
    zipcode: str,
    address: str,
    address_detail: str,
):
    cur.execute(
        """
        UPDATE mall_users
        SET zipcode = ?, address = ?, address_detail = ?
        WHERE id = ?
        """,
        (_normalize_zipcode(zipcode), str(address or "").strip(), str(address_detail or "").strip(), int(user_id)),
    )


def get_member_profile(user_id: int) -> Optional[dict[str, Any]]:
    return mall_database.get_mall_user_by_id(int(user_id))


def update_member_profile(user_id: int, data: dict[str, Any]) -> dict[str, Any]:
    uid = int(user_id)
    name = str((data or {}).get("name") or "").strip()[:60]
    phone = str((data or {}).get("phone") or "").strip()[:30]
    zipcode = _normalize_zipcode((data or {}).get("zipcode"))
    address = str((data or {}).get("address") or "").strip()[:200]
    address_detail = str((data or {}).get("address_detail") or "").strip()[:200]

    if not name or not phone:
        return {"ok": False, "message": "성함과 연락처를 모두 입력해 주세요."}

    now_str = _now_str()
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM mall_users WHERE id = ? AND is_active = 1 AND COALESCE(withdrawn_at, '') = ''",
                (uid,),
            )
            if not cur.fetchone():
                return {"ok": False, "not_found": True, "message": "회원 정보를 찾을 수 없습니다."}

            cur.execute(
                """
                UPDATE mall_users
                SET name = ?, phone = ?, zipcode = ?, address = ?, address_detail = ?
                WHERE id = ?
                """,
                (name, phone, zipcode, address, address_detail, uid),
            )

            if address:
                cur.execute(
                    "SELECT id FROM mall_user_addresses WHERE user_id = ? AND is_default = 1 ORDER BY id DESC LIMIT 1",
                    (uid,),
                )
                def_row = cur.fetchone()
                if def_row:
                    cur.execute(
                        """
                        UPDATE mall_user_addresses
                        SET recipient_name = ?, recipient_phone = ?, zipcode = ?, address = ?, address_detail = ?, updated_at = ?
                        WHERE id = ? AND user_id = ?
                        """,
                        (name, phone, zipcode, address, address_detail, now_str, int(def_row["id"]), uid),
                    )
                else:
                    cur.execute("SELECT COUNT(*) FROM mall_user_addresses WHERE user_id = ?", (uid,))
                    addr_cnt = int(cur.fetchone()[0] or 0)
                    if addr_cnt == 0:
                        cur.execute(
                            """
                            INSERT INTO mall_user_addresses
                            (user_id, label, recipient_name, recipient_phone, zipcode, address, address_detail, is_default, created_at, updated_at)
                            VALUES (?, '기본 배송지', ?, ?, ?, ?, ?, 1, ?, ?)
                            """,
                            (uid, name, phone, zipcode, address, address_detail, now_str, now_str),
                        )

            conn.commit()
        finally:
            conn.close()

    return {
        "ok": True,
        "user": mall_database.get_mall_user_by_id(uid),
        "addresses": list_user_addresses(uid),
    }


def list_user_addresses(user_id: int) -> list[dict[str, Any]]:
    uid = int(user_id)
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, user_id, label, recipient_name, recipient_phone,
                       zipcode, address, address_detail, is_default, created_at, updated_at
                FROM mall_user_addresses
                WHERE user_id = ?
                ORDER BY is_default DESC, id DESC
                """,
                (uid,),
            )
            return [_format_address_row(r) for r in cur.fetchall()]
        finally:
            conn.close()


def save_user_address(user_id: int, data: dict[str, Any]) -> dict[str, Any]:
    uid = int(user_id)
    payload = data or {}
    raw_id = payload.get("id")
    if isinstance(raw_id, (bool, float)):
        return {"ok": False, "message": "올바른 배송지 ID가 필요합니다."}
    if raw_id in (None, "", 0, "0"):
        addr_id = 0
    else:
        parsed_id = mall_database.parse_strict_int64(raw_id, min_val=1)
        if parsed_id is None:
            return {"ok": False, "message": "올바른 배송지 ID가 필요합니다."}
        addr_id = parsed_id
    label = str(payload.get("label") or "").strip()[:40] or "배송지"
    recipient_name = (
        str(payload.get("recipient_name") or "").strip()
        or str(payload.get("recipient") or "").strip()
        or str(payload.get("name") or "").strip()
    )[:60]
    recipient_phone = (
        str(payload.get("recipient_phone") or "").strip()
        or str(payload.get("phone") or "").strip()
    )[:30]
    zipcode = _normalize_zipcode(payload.get("zipcode"))
    address = str(payload.get("address") or "").strip()[:200]
    address_detail = str(payload.get("address_detail") or "").strip()[:200]
    req_default = bool(payload.get("is_default"))

    if not recipient_name or not recipient_phone or not address:
        return {"ok": False, "message": "수령인 성함, 연락처, 배송지 주소를 모두 입력해 주세요."}

    now_str = _now_str()
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            if addr_id > 0:
                cur.execute(
                    "SELECT id, is_default FROM mall_user_addresses WHERE id = ? AND user_id = ? LIMIT 1",
                    (addr_id, uid),
                )
                existing = cur.fetchone()
                if not existing:
                    return {"ok": False, "not_found": True, "message": "수정할 배송지를 찾을 수 없습니다."}
                make_default = req_default or bool(int(existing["is_default"] or 0))
                if make_default:
                    cur.execute("UPDATE mall_user_addresses SET is_default = 0 WHERE user_id = ?", (uid,))
                cur.execute(
                    """
                    UPDATE mall_user_addresses
                    SET label = ?, recipient_name = ?, recipient_phone = ?,
                        zipcode = ?, address = ?, address_detail = ?, is_default = ?, updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        label, recipient_name, recipient_phone,
                        zipcode, address, address_detail, 1 if make_default else 0, now_str,
                        addr_id, uid,
                    ),
                )
                saved_id = addr_id
            else:
                cur.execute("SELECT COUNT(*) FROM mall_user_addresses WHERE user_id = ?", (uid,))
                cnt = int(cur.fetchone()[0] or 0)
                make_default = req_default or (cnt == 0)
                if make_default:
                    cur.execute("UPDATE mall_user_addresses SET is_default = 0 WHERE user_id = ?", (uid,))
                cur.execute(
                    """
                    INSERT INTO mall_user_addresses
                    (user_id, label, recipient_name, recipient_phone, zipcode, address, address_detail, is_default, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uid, label, recipient_name, recipient_phone,
                        zipcode, address, address_detail, 1 if make_default else 0, now_str, now_str,
                    ),
                )
                saved_id = int(cur.lastrowid)

            if make_default:
                _sync_user_default_address_fields(cur, uid, zipcode, address, address_detail)

            conn.commit()
            cur.execute(
                "SELECT * FROM mall_user_addresses WHERE id = ? AND user_id = ? LIMIT 1",
                (saved_id, uid),
            )
            saved_row = cur.fetchone()
        finally:
            conn.close()

    return {
        "ok": True,
        "address": _format_address_row(saved_row) if saved_row else None,
        "addresses": list_user_addresses(uid),
    }


def set_default_user_address(user_id: int, address_id: Any) -> dict[str, Any]:
    uid = mall_database.parse_strict_int64(user_id, min_val=1)
    aid = mall_database.parse_strict_int64(address_id, min_val=1)
    if uid is None or aid is None:
        return {"ok": False, "message": "올바른 배송지 ID가 필요합니다."}

    now_str = _now_str()
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM mall_user_addresses WHERE id = ? AND user_id = ? LIMIT 1",
                (aid, uid),
            )
            target = cur.fetchone()
            if not target:
                return {"ok": False, "not_found": True, "message": "배송지를 찾을 수 없습니다."}

            cur.execute("UPDATE mall_user_addresses SET is_default = 0 WHERE user_id = ?", (uid,))
            cur.execute(
                "UPDATE mall_user_addresses SET is_default = 1, updated_at = ? WHERE id = ? AND user_id = ?",
                (now_str, aid, uid),
            )
            _sync_user_default_address_fields(
                cur,
                uid,
                str(target["zipcode"] or ""),
                str(target["address"] or ""),
                str(target["address_detail"] or ""),
            )
            conn.commit()
        finally:
            conn.close()

    return {"ok": True, "addresses": list_user_addresses(uid)}


def delete_user_address(user_id: int, address_id: Any) -> dict[str, Any]:
    uid = mall_database.parse_strict_int64(user_id, min_val=1)
    aid = mall_database.parse_strict_int64(address_id, min_val=1)
    if uid is None or aid is None:
        return {"ok": False, "message": "올바른 배송지 ID가 필요합니다."}

    now_str = _now_str()
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, is_default FROM mall_user_addresses WHERE id = ? AND user_id = ? LIMIT 1",
                (aid, uid),
            )
            target = cur.fetchone()
            if not target:
                return {"ok": False, "not_found": True, "message": "삭제할 배송지를 찾을 수 없습니다."}

            was_default = bool(int(target["is_default"] or 0))
            cur.execute("DELETE FROM mall_user_addresses WHERE id = ? AND user_id = ?", (aid, uid))

            if was_default:
                cur.execute(
                    "SELECT * FROM mall_user_addresses WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                    (uid,),
                )
                nxt = cur.fetchone()
                if nxt:
                    cur.execute(
                        "UPDATE mall_user_addresses SET is_default = 1, updated_at = ? WHERE id = ? AND user_id = ?",
                        (now_str, int(nxt["id"]), uid),
                    )
                    _sync_user_default_address_fields(
                        cur,
                        uid,
                        str(nxt["zipcode"] or ""),
                        str(nxt["address"] or ""),
                        str(nxt["address_detail"] or ""),
                    )
                else:
                    _sync_user_default_address_fields(cur, uid, "", "", "")

            conn.commit()
        finally:
            conn.close()

    return {"ok": True, "addresses": list_user_addresses(uid)}


def change_member_password(
    user_id: int,
    current_password: str,
    new_password: str,
    confirm_password: Optional[str] = None,
) -> dict[str, Any]:
    uid = int(user_id)
    cur_pw = str(current_password or "")
    nxt_pw = str(new_password or "")
    if not cur_pw or not nxt_pw:
        return {"ok": False, "message": "현재 비밀번호와 새 비밀번호를 모두 입력해 주세요."}
    if confirm_password is not None and nxt_pw != str(confirm_password):
        return {"ok": False, "message": "새 비밀번호와 비밀번호 확인이 일치하지 않습니다."}

    valid_pw, pw_msg = mall_security.validate_password_policy(nxt_pw, min_len=8)
    if not valid_pw:
        return {"ok": False, "message": pw_msg}

    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, password_hash FROM mall_users
                WHERE id = ? AND is_active = 1 AND COALESCE(withdrawn_at, '') = ''
                LIMIT 1
                """,
                (uid,),
            )
            row = cur.fetchone()
            if not row:
                return {"ok": False, "not_found": True, "message": "회원 계정을 찾을 수 없습니다."}
            if not mall_security.verify_password(row["password_hash"], cur_pw):
                return {"ok": False, "message": "현재 비밀번호가 올바르지 않습니다."}

            new_hash = mall_security.hash_password(nxt_pw)
            now_str = _now_str()
            cur.execute(
                """
                UPDATE mall_users
                SET password_hash = ?, failed_login_count = 0, locked_until = 0
                WHERE id = ?
                """,
                (new_hash, uid),
            )
            cur.execute(
                "UPDATE mall_password_resets SET used_at = ? WHERE user_id = ? AND COALESCE(used_at, '') = ''",
                (now_str, uid),
            )
            conn.commit()
            return {"ok": True, "message": "비밀번호가 안전하게 변경되었습니다."}
        finally:
            conn.close()


def _record_notification_log(
    event_type: str,
    channel: str,
    recipient: str,
    subject: str,
    body: str,
    status: str,
    error_msg: str = "",
    ref_code: str = "",
    meta: Optional[dict[str, Any]] = None,
):
    now_str = _now_str()
    meta_str = json.dumps(meta or {}, ensure_ascii=False)
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO mall_notification_logs
                (event_type, channel, recipient, subject, body, status, error_msg, ref_code, meta_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(event_type or ""),
                    str(channel or "EMAIL"),
                    str(recipient or ""),
                    str(subject or ""),
                    str(body or ""),
                    str(status or "LOGGED_ONLY"),
                    str(error_msg or ""),
                    str(ref_code or ""),
                    meta_str,
                    now_str,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def send_or_log_email(
    event_type: str,
    recipient: str,
    subject: str,
    body: str,
    ref_code: str = "",
    meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    SMTP 환경변수(MALL_SMTP_HOST / SMTP_HOST)가 설정되어 있으면 이메일을 발송하고,
    미설정 시 오류 없이 mall_notification_logs 테이블에 LOGGED_ONLY 상태로 기록합니다.
    """
    smtp_host = (os.getenv("MALL_SMTP_HOST") or os.getenv("SMTP_HOST") or "").strip()
    if not smtp_host:
        _record_notification_log(
            event_type=event_type,
            channel="EMAIL",
            recipient=recipient,
            subject=subject,
            body=body,
            status="LOGGED_ONLY",
            error_msg="SMTP 미설정으로 발송 로그만 기록됨",
            ref_code=ref_code,
            meta=meta,
        )
        return {"ok": True, "status": "LOGGED_ONLY"}

    raw_port = (os.getenv("MALL_SMTP_PORT") or os.getenv("SMTP_PORT") or "587").strip()
    try:
        smtp_port = int(raw_port)
    except Exception:
        smtp_port = 587
    smtp_user = (os.getenv("MALL_SMTP_USER") or os.getenv("SMTP_USER") or "").strip()
    smtp_pass = (os.getenv("MALL_SMTP_PASSWORD") or os.getenv("SMTP_PASSWORD") or "").strip()
    smtp_from = (os.getenv("MALL_SMTP_FROM") or os.getenv("SMTP_FROM") or smtp_user or "noreply@golabang.local").strip()
    use_ssl = (os.getenv("MALL_SMTP_SSL") or os.getenv("SMTP_SSL") or "").strip().lower() in ("1", "true", "yes") or smtp_port == 465

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = recipient
    msg.set_content(body)

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=5) as server:
                if smtp_user and smtp_pass:
                    server.login(smtp_user, smtp_pass)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=5) as server:
                try:
                    server.starttls()
                except Exception:
                    pass
                if smtp_user and smtp_pass:
                    server.login(smtp_user, smtp_pass)
                server.send_message(msg)
        _record_notification_log(
            event_type=event_type,
            channel="EMAIL",
            recipient=recipient,
            subject=subject,
            body=body,
            status="SENT",
            error_msg="",
            ref_code=ref_code,
            meta=meta,
        )
        return {"ok": True, "status": "SENT"}
    except Exception as exc:
        _record_notification_log(
            event_type=event_type,
            channel="EMAIL",
            recipient=recipient,
            subject=subject,
            body=body,
            status="FAILED",
            error_msg=str(exc)[:300],
            ref_code=ref_code,
            meta=meta,
        )
        return {"ok": False, "status": "FAILED", "error": str(exc)}


def request_password_reset(email: str, public_base_url: str = "") -> dict[str, Any]:
    """
    비밀번호 재설정 링크(토큰) 및 6자리 임시 코드를 생성·저장하고 이메일 발송(또는 로그 기록)합니다.
    계정 존재 여부 추측 방지를 위해 토큰/코드는 절대 HTTP 응답으로 반환하지 않습니다.
    """
    mail = str(email or "").strip().lower()
    if not mail or "@" not in mail or "." not in mail:
        return {"ok": False, "message": "유효한 이메일 주소를 입력해 주세요."}

    generic_msg = "입력하신 이메일이 등록되어 있다면 비밀번호 재설정 링크와 6자리 인증 코드를 발송했습니다."
    now_ts = time.time()
    now_str = _now_str()

    target_uid: Optional[int] = None
    target_name = ""
    raw_token = ""
    temp_code = ""
    reset_id = 0

    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, name, email FROM mall_users
                WHERE email = ? AND is_active = 1 AND COALESCE(withdrawn_at, '') = ''
                LIMIT 1
                """,
                (mail,),
            )
            u_row = cur.fetchone()
            if u_row:
                target_uid = int(u_row["id"])
                target_name = str(u_row["name"] or "회원")
                cur.execute(
                    "UPDATE mall_password_resets SET used_at = ? WHERE user_id = ? AND COALESCE(used_at, '') = ''",
                    (now_str, target_uid),
                )
                raw_token = secrets.token_urlsafe(32)
                temp_code = f"{secrets.randbelow(1_000_000):06d}"
                token_hash = _hash_reset_secret(raw_token, "token")
                code_hash = _hash_reset_secret(temp_code, f"code:{mail}")
                expires_at = now_ts + float(RESET_TTL_SECONDS)
                cur.execute(
                    """
                    INSERT INTO mall_password_resets
                    (user_id, email, token_hash, code_hash, expires_at, used_at, attempts, created_at)
                    VALUES (?, ?, ?, ?, ?, '', 0, ?)
                    """,
                    (target_uid, mail, token_hash, code_hash, expires_at, now_str),
                )
                reset_id = int(cur.lastrowid)
                conn.commit()
        finally:
            conn.close()

    if target_uid and raw_token:
        base_url = str(public_base_url or "").strip().rstrip("/")
        reset_path = f"/auth/reset-password?token={raw_token}"
        reset_url = f"{base_url}{reset_path}" if base_url else reset_path
        subject = "[골라방 LIVE] 비밀번호 재설정 안내"
        body = (
            f"{target_name}님, 안녕하세요.\n\n"
            "요청하신 비밀번호 재설정 안내입니다 (유효시간 30분).\n"
            f"- 재설정 링크: {reset_url}\n"
            f"- 6자리 임시 인증 코드: {temp_code}\n\n"
            "본인이 요청하지 않으셨다면 이 안내를 무시하셔도 됩니다."
        )
        send_or_log_email(
            event_type="PASSWORD_RESET",
            recipient=mail,
            subject=subject,
            body=body,
            ref_code=mail,
            meta={"user_id": target_uid, "reset_id": reset_id},
        )

    return {"ok": True, "message": generic_msg}


def complete_password_reset(
    new_password: str,
    confirm_password: Optional[str] = None,
    token: str = "",
    email: str = "",
    code: str = "",
) -> dict[str, Any]:
    nxt_pw = str(new_password or "")
    if confirm_password is not None and nxt_pw != str(confirm_password):
        return {"ok": False, "message": "새 비밀번호와 비밀번호 확인이 일치하지 않습니다."}

    valid_pw, pw_msg = mall_security.validate_password_policy(nxt_pw, min_len=8)
    if not valid_pw:
        return {"ok": False, "message": pw_msg}

    clean_token = str(token or "").strip()
    clean_email = str(email or "").strip().lower()
    clean_code = str(code or "").strip()

    if not clean_token and not (clean_email and clean_code):
        return {"ok": False, "message": "재설정 토큰 또는 이메일과 6자리 인증 코드를 입력해 주세요."}

    now_ts = time.time()
    now_str = _now_str()

    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            if clean_token:
                tok_hash = _hash_reset_secret(clean_token, "token")
                cur.execute(
                    """
                    SELECT r.*, u.is_active AS u_active, u.withdrawn_at AS u_withdrawn
                    FROM mall_password_resets r
                    JOIN mall_users u ON u.id = r.user_id
                    WHERE r.token_hash = ?
                    ORDER BY r.id DESC
                    LIMIT 1
                    """,
                    (tok_hash,),
                )
                row = cur.fetchone()
                if not row:
                    return {"ok": False, "message": "유효하지 않은 비밀번호 재설정 링크입니다."}
                if str(row["used_at"] or "").strip():
                    return {"ok": False, "message": "이미 사용된 비밀번호 재설정 링크입니다."}
                if float(row["expires_at"] or 0.0) <= now_ts:
                    return {"ok": False, "message": "만료된 비밀번호 재설정 링크입니다. 다시 요청해 주세요."}
                if int(row["attempts"] or 0) >= RESET_MAX_ATTEMPTS:
                    return {"ok": False, "message": "시도 횟수가 초과된 재설정 요청입니다. 다시 요청해 주세요."}
                if not int(row["u_active"] or 0) or str(row["u_withdrawn"] or "").strip():
                    return {"ok": False, "message": "비밀번호를 재설정할 수 없는 계정 상태입니다."}

                row_email = str(row["email"] or "").strip().lower()
                if clean_email and clean_email != row_email:
                    return {"ok": False, "message": "재설정 요청 이메일과 일치하지 않습니다."}
                if clean_code:
                    cur_attempts = int(row["attempts"] or 0)
                    expected_code_hash = _hash_reset_secret(clean_code, f"code:{row_email}")
                    if not hmac.compare_digest(str(row["code_hash"] or ""), expected_code_hash):
                        next_attempts = cur_attempts + 1
                        if next_attempts >= RESET_MAX_ATTEMPTS:
                            cur.execute(
                                "UPDATE mall_password_resets SET attempts = ?, used_at = ? WHERE id = ?",
                                (next_attempts, now_str, int(row["id"])),
                            )
                            conn.commit()
                            return {"ok": False, "message": "인증 코드 오류 5회 초과로 만료되었습니다. 다시 요청해 주세요."}
                        cur.execute(
                            "UPDATE mall_password_resets SET attempts = ? WHERE id = ?",
                            (next_attempts, int(row["id"])),
                        )
                        conn.commit()
                        return {"ok": False, "message": f"인증 코드가 올바르지 않습니다. ({next_attempts}/{RESET_MAX_ATTEMPTS})"}
            else:
                cur.execute(
                    """
                    SELECT r.*, u.is_active AS u_active, u.withdrawn_at AS u_withdrawn
                    FROM mall_password_resets r
                    JOIN mall_users u ON u.id = r.user_id
                    WHERE r.email = ? AND COALESCE(r.used_at, '') = ''
                    ORDER BY r.id DESC
                    LIMIT 1
                    """,
                    (clean_email,),
                )
                row = cur.fetchone()
                if not row:
                    return {"ok": False, "message": "유효한 비밀번호 재설정 요청이 없습니다. 인증 코드를 다시 요청해 주세요."}
                if float(row["expires_at"] or 0.0) <= now_ts:
                    return {"ok": False, "message": "인증 코드가 만료되었습니다. 다시 요청해 주세요."}
                cur_attempts = int(row["attempts"] or 0)
                if cur_attempts >= RESET_MAX_ATTEMPTS:
                    return {"ok": False, "message": "인증 시도 횟수를 초과했습니다. 다시 요청해 주세요."}
                if not int(row["u_active"] or 0) or str(row["u_withdrawn"] or "").strip():
                    return {"ok": False, "message": "비밀번호를 재설정할 수 없는 계정 상태입니다."}

                expected_code_hash = _hash_reset_secret(clean_code, f"code:{clean_email}")
                if not hmac.compare_digest(str(row["code_hash"] or ""), expected_code_hash):
                    next_attempts = cur_attempts + 1
                    if next_attempts >= RESET_MAX_ATTEMPTS:
                        cur.execute(
                            "UPDATE mall_password_resets SET attempts = ?, used_at = ? WHERE id = ?",
                            (next_attempts, now_str, int(row["id"])),
                        )
                        conn.commit()
                        return {"ok": False, "message": "인증 코드 오류 5회 초과로 만료되었습니다. 다시 요청해 주세요."}
                    cur.execute(
                        "UPDATE mall_password_resets SET attempts = ? WHERE id = ?",
                        (next_attempts, int(row["id"])),
                    )
                    conn.commit()
                    return {"ok": False, "message": f"인증 코드가 올바르지 않습니다. ({next_attempts}/{RESET_MAX_ATTEMPTS})"}

            uid = int(row["user_id"])
            new_hash = mall_security.hash_password(nxt_pw)
            cur.execute(
                """
                UPDATE mall_users
                SET password_hash = ?, failed_login_count = 0, locked_until = 0
                WHERE id = ?
                """,
                (new_hash, uid),
            )
            cur.execute(
                "UPDATE mall_password_resets SET used_at = ? WHERE user_id = ? AND COALESCE(used_at, '') = ''",
                (now_str, uid),
            )
            conn.commit()
            return {"ok": True, "message": "비밀번호가 재설정되었습니다. 새 비밀번호로 로그인해 주세요."}
        finally:
            conn.close()


def withdraw_member_account(user_id: int, password: str) -> dict[str, Any]:
    """
    policy.html 이용약관·개인정보처리방침 기준 회원 탈퇴 처리:
    - ADMIN 계정 직접 탈퇴 차단
    - 현재 비밀번호 검증
    - 전자상거래법 5년 보존 대상인 mall_orders / mall_order_items 는 보존
    - 회원 개인정보(name, phone, zipcode, address, address_detail, password_hash) 파기 및
      mall_user_addresses / mall_wishlist / mall_password_resets 즉시 삭제
    """
    uid = int(user_id)
    pw = str(password or "")
    if not pw:
        return {"ok": False, "message": "본인 확인을 위해 현재 비밀번호를 입력해 주세요."}

    now_str = _now_str()
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, role, password_hash, is_active, withdrawn_at FROM mall_users WHERE id = ? LIMIT 1",
                (uid,),
            )
            row = cur.fetchone()
            if not row or not int(row["is_active"] or 0) or str(row["withdrawn_at"] or "").strip():
                return {"ok": False, "not_found": True, "message": "탈퇴 처리할 활성 회원을 찾을 수 없습니다."}
            if str(row["role"] or "").upper() == "ADMIN":
                return {"ok": False, "forbidden": True, "message": "관리자 계정은 직접 탈퇴할 수 없습니다."}
            if not mall_security.verify_password(row["password_hash"], pw):
                return {"ok": False, "message": "현재 비밀번호가 올바르지 않습니다."}

            cur.execute("DELETE FROM mall_user_addresses WHERE user_id = ?", (uid,))
            cur.execute("DELETE FROM mall_wishlist WHERE user_id = ?", (uid,))
            cur.execute("DELETE FROM mall_password_resets WHERE user_id = ?", (uid,))
            cur.execute("DELETE FROM mall_user_coupons WHERE user_id = ?", (uid,))
            cur.execute("DELETE FROM mall_point_logs WHERE user_id = ?", (uid,))

            scrubbed_hash = "!WITHDRAWN!" + secrets.token_hex(16)
            cur.execute(
                """
                UPDATE mall_users
                SET is_active = 0,
                    withdrawn_at = ?,
                    name = '탈퇴회원',
                    phone = '',
                    zipcode = '',
                    address = '',
                    address_detail = '',
                    points_balance = 0,
                    password_hash = ?,
                    failed_login_count = 0,
                    locked_until = 0
                WHERE id = ?
                """,
                (now_str, scrubbed_hash, uid),
            )
            conn.commit()
            return {"ok": True, "withdrawn_at": now_str, "message": "회원 탈퇴가 완료되었습니다."}
        finally:
            conn.close()


def _format_admin_user_row(row) -> dict[str, Any]:
    d = dict(row)
    d.pop("password_hash", None)
    withdrawn_at = str(d.get("withdrawn_at") or "").strip()
    is_active = bool(int(d.get("is_active") or 0))
    if withdrawn_at:
        account_status = "WITHDRAWN"
    elif is_active:
        account_status = "ACTIVE"
    else:
        account_status = "SUSPENDED"
    return {
        "id": int(d["id"]),
        "email": str(d.get("email") or ""),
        "name": str(d.get("name") or ""),
        "phone": str(d.get("phone") or ""),
        "zipcode": _normalize_zipcode(d.get("zipcode")),
        "address": str(d.get("address") or ""),
        "address_detail": str(d.get("address_detail") or ""),
        "role": str(d.get("role") or "MEMBER"),
        "is_active": is_active,
        "account_status": account_status,
        "withdrawn_at": withdrawn_at,
        "last_login_at": str(d.get("last_login_at") or ""),
        "created_at": str(d.get("created_at") or ""),
        "points_balance": max(0, int(d.get("points_balance") or 0)),
        "order_count": int(d.get("order_count") or 0),
        "total_spent": int(d.get("total_spent") or 0),
    }


def count_admin_users() -> int:
    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            row = conn.execute("SELECT COUNT(*) FROM mall_users").fetchone()
            return int(row[0] or 0) if row else 0
        finally:
            conn.close()


def list_admin_users(
    query: str = "",
    status_filter: str = "ALL",
    role_filter: str = "ALL",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """관리자 회원 관리 목록: 주문 수 및 누적 구매액(취소/환불 제외) 집계."""
    q = str(query or "").strip().lower()
    st_f = str(status_filter or "ALL").strip().upper()
    role_f = str(role_filter or "ALL").strip().upper()

    where_parts: list[str] = []
    params: list[Any] = []
    if q:
        like_q = f"%{q}%"
        where_parts.append("(LOWER(u.email) LIKE ? OR LOWER(u.name) LIKE ? OR REPLACE(REPLACE(u.phone, '-', ''), ' ', '') LIKE ?)")
        clean_q_phone = re.sub(r"[-\s]+", "", q)
        params.extend([like_q, like_q, f"%{clean_q_phone}%"])

    if st_f == "ACTIVE":
        where_parts.append("u.is_active = 1 AND COALESCE(u.withdrawn_at, '') = ''")
    elif st_f == "SUSPENDED":
        where_parts.append("u.is_active = 0 AND COALESCE(u.withdrawn_at, '') = ''")
    elif st_f == "WITHDRAWN":
        where_parts.append("COALESCE(u.withdrawn_at, '') != ''")

    if role_f in ("MEMBER", "ADMIN"):
        where_parts.append("UPPER(u.role) = ?")
        params.append(role_f)

    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    eff_limit = mall_database._parse_clamped_query_int(limit, 200, 1, 500)

    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT
                    u.id, u.email, u.name, u.phone, u.zipcode, u.address, u.address_detail,
                    u.role, u.is_active, u.withdrawn_at, u.points_balance, u.last_login_at, u.created_at,
                    COUNT(
                        CASE
                            WHEN o.id IS NOT NULL
                             AND COALESCE(o.order_status, '') NOT IN ('주문취소', '환불완료')
                             AND COALESCE(o.payment_status, '') != '환불완료'
                            THEN 1
                        END
                    ) AS order_count,
                    COALESCE(
                        SUM(
                            CASE
                                WHEN o.id IS NOT NULL
                                 AND COALESCE(o.order_status, '') NOT IN ('주문취소', '환불완료')
                                 AND COALESCE(o.payment_status, '') != '환불완료'
                                THEN o.total_amount
                                ELSE 0
                            END
                        ),
                        0
                    ) AS total_spent
                FROM mall_users u
                LEFT JOIN mall_orders o ON o.user_id = u.id
                {where_sql}
                GROUP BY u.id
                ORDER BY u.id DESC
                LIMIT ?
                """,
                (*params, eff_limit),
            )
            return [_format_admin_user_row(r) for r in cur.fetchall()]
        finally:
            conn.close()


def _parse_bool_flag(raw_val: Any) -> Optional[int]:
    if isinstance(raw_val, bool):
        return 1 if raw_val else 0
    if isinstance(raw_val, int) and raw_val in (0, 1):
        return int(raw_val)
    s = str(raw_val if raw_val is not None else "").strip().lower()
    if s in ("1", "true", "active"):
        return 1
    if s in ("0", "false", "suspended"):
        return 0
    return None


def set_admin_user_status(target_user_id: Any, is_active: Any, acting_admin_id: int) -> dict[str, Any]:
    tid = mall_database.parse_strict_int64(target_user_id, min_val=1)
    if tid is None:
        return {"ok": False, "message": "올바른 회원 ID가 필요합니다."}

    next_active = _parse_bool_flag(is_active)
    if next_active is None:
        return {"ok": False, "message": "유효한 상태 값(is_active: 0 또는 1)이 필요합니다."}

    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, role, is_active, withdrawn_at FROM mall_users WHERE id = ? LIMIT 1",
                (tid,),
            )
            row = cur.fetchone()
            if not row:
                return {"ok": False, "not_found": True, "message": "회원을 찾을 수 없습니다."}
            if str(row["withdrawn_at"] or "").strip():
                return {"ok": False, "message": "탈퇴 처리된 회원은 다시 활성화할 수 없습니다."}
            if tid == int(acting_admin_id) and next_active == 0:
                return {"ok": False, "message": "현재 로그인한 관리자 본인은 정지할 수 없습니다."}
            if str(row["role"] or "").upper() == "ADMIN" and next_active == 0:
                cur.execute(
                    "SELECT COUNT(*) FROM mall_users WHERE role = 'ADMIN' AND is_active = 1 AND COALESCE(withdrawn_at, '') = ''"
                )
                active_admins = int(cur.fetchone()[0] or 0)
                if active_admins <= 1:
                    return {"ok": False, "message": "최소 1명의 활성 관리자 계정이 유지되어야 합니다."}

            cur.execute("UPDATE mall_users SET is_active = ? WHERE id = ?", (next_active, tid))
            conn.commit()

            cur.execute(
                """
                SELECT
                    u.id, u.email, u.name, u.phone, u.zipcode, u.address, u.address_detail,
                    u.role, u.is_active, u.withdrawn_at, u.points_balance, u.last_login_at, u.created_at,
                    COUNT(
                        CASE
                            WHEN o.id IS NOT NULL
                             AND COALESCE(o.order_status, '') NOT IN ('주문취소', '환불완료')
                             AND COALESCE(o.payment_status, '') != '환불완료'
                            THEN 1
                        END
                    ) AS order_count,
                    COALESCE(
                        SUM(
                            CASE
                                WHEN o.id IS NOT NULL
                                 AND COALESCE(o.order_status, '') NOT IN ('주문취소', '환불완료')
                                 AND COALESCE(o.payment_status, '') != '환불완료'
                                THEN o.total_amount
                                ELSE 0
                            END
                        ),
                        0
                    ) AS total_spent
                FROM mall_users u
                LEFT JOIN mall_orders o ON o.user_id = u.id
                WHERE u.id = ?
                GROUP BY u.id
                """,
                (tid,),
            )
            updated = cur.fetchone()
            return {
                "ok": True,
                "user": _format_admin_user_row(updated) if updated else None,
            }
        finally:
            conn.close()
