# -*- coding: utf-8 -*-
"""
쇼핑몰 다채널 자동 알림 서비스 (mall_notifications.py)
- 지원 채널: EMAIL (SMTP), TELEGRAM, KAKAO_ALIMTALK, SMS
- 환경변수 미설정 시 예외를 발생시키지 않고 mall_notification_logs 테이블에 LOGGED_ONLY 상태로 기록
- 비밀번호/해시/시크릿 등 민감정보 로그 저장 원천 차단
"""

from __future__ import annotations

import json
import os
import smtplib
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Optional

import mall_database

_SENSITIVE_META_KEYS = {
    "password",
    "guest_password",
    "guest_password_hash",
    "password_hash",
    "secret",
    "token",
    "api_secret",
    "webhook_secret",
}


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sanitize_meta(meta: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(meta, dict):
        return {}
    clean: dict[str, Any] = {}
    for k, v in meta.items():
        lk = str(k or "").strip().lower()
        if any(sk in lk for sk in _SENSITIVE_META_KEYS):
            continue
        clean[str(k)] = v
    return clean


def _normalize_channel(channel: Any) -> str:
    ch = str(channel or "EMAIL").strip().upper()
    if ch in ("KAKAO", "ALIMTALK", "KAKAO_ALIMTALK"):
        return "KAKAO_ALIMTALK"
    if ch in ("EMAIL", "SMTP"):
        return "EMAIL"
    if ch in ("TELEGRAM", "TG"):
        return "TELEGRAM"
    if ch == "SMS":
        return "SMS"
    return ch or "EMAIL"


def record_notification_log(
    event_type: str,
    channel: str,
    recipient: str,
    subject: str,
    body: str,
    status: str,
    error_msg: str = "",
    ref_code: str = "",
    meta: Optional[dict[str, Any]] = None,
) -> int:
    now_str = _now_str()
    clean_meta = _sanitize_meta(meta)
    meta_str = json.dumps(clean_meta, ensure_ascii=False)
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
                    str(event_type or "")[:60],
                    _normalize_channel(channel)[:40],
                    str(recipient or "")[:160],
                    str(subject or "")[:300],
                    str(body or "")[:2000],
                    str(status or "LOGGED_ONLY")[:30],
                    str(error_msg or "")[:300],
                    str(ref_code or "")[:80],
                    meta_str,
                    now_str,
                ),
            )
            log_id = int(cur.lastrowid)
            conn.commit()
            return log_id
        finally:
            conn.close()


def _send_email_channel(recipient: str, subject: str, body: str) -> tuple[str, str]:
    smtp_host = (os.getenv("MALL_SMTP_HOST") or os.getenv("SMTP_HOST") or "").strip()
    if not smtp_host:
        return "LOGGED_ONLY", "SMTP 환경변수 미설정으로 발송 로그만 기록됨"
    if not recipient or "@" not in recipient:
        return "LOGGED_ONLY", "수신자 이메일 주소가 없어 발송 로그만 기록됨"

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

    if use_ssl:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=2.0) as server:
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.send_message(msg)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=2.0) as server:
            try:
                server.starttls()
            except Exception:
                pass
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.send_message(msg)
    return "SENT", ""


def _send_telegram_channel(subject: str, body: str) -> tuple[str, str]:
    bot_token = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("MALL_TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("MALL_TELEGRAM_CHAT_ID") or "").strip()
    if not bot_token or not chat_id:
        return "LOGGED_ONLY", "TELEGRAM 환경변수 미설정으로 발송 로그만 기록됨"

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": f"{subject}\n{body}".strip(),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2.0) as resp:
        resp.read()
    return "SENT", ""


def _send_kakao_channel(recipient: str, subject: str, body: str, ref_code: str) -> tuple[str, str]:
    api_key = (os.getenv("KAKAO_ALIMTALK_API_KEY") or os.getenv("KAKAO_ALIMTALK_KEY") or os.getenv("MALL_KAKAO_ALIMTALK_KEY") or "").strip()
    endpoint = (os.getenv("KAKAO_ALIMTALK_URL") or "").strip()
    if not api_key:
        return "LOGGED_ONLY", "카카오 알림톡 API 키 미설정으로 발송 로그만 기록됨"
    if not endpoint:
        return "LOGGED_ONLY", "카카오 알림톡 발송 엔드포인트 미설정으로 발송 로그만 기록됨"
    payload = json.dumps(
        {
            "recipient": recipient,
            "subject": subject,
            "message": body,
            "ref_code": ref_code,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2.0) as resp:
        resp.read()
    return "SENT", ""


def _send_sms_channel(recipient: str, subject: str, body: str, ref_code: str) -> tuple[str, str]:
    api_key = (os.getenv("SMS_API_KEY") or os.getenv("MALL_SMS_API_KEY") or "").strip()
    endpoint = (os.getenv("SMS_API_URL") or "").strip()
    if not api_key:
        return "LOGGED_ONLY", "SMS API 키 미설정으로 발송 로그만 기록됨"
    if not endpoint:
        return "LOGGED_ONLY", "SMS 발송 엔드포인트 미설정으로 발송 로그만 기록됨"
    payload = json.dumps(
        {
            "to": recipient,
            "title": subject,
            "text": body,
            "ref_code": ref_code,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2.0) as resp:
        resp.read()
    return "SENT", ""


def dispatch_notification(
    event_type: str,
    channel: str,
    recipient: str,
    subject: str,
    body: str,
    ref_code: str = "",
    meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    norm_channel = _normalize_channel(channel)
    clean_recipient = str(recipient or "").strip() or "-"
    status = "LOGGED_ONLY"
    error_msg = ""

    try:
        if norm_channel == "EMAIL":
            status, error_msg = _send_email_channel(clean_recipient, subject, body)
        elif norm_channel == "TELEGRAM":
            status, error_msg = _send_telegram_channel(subject, body)
        elif norm_channel == "KAKAO_ALIMTALK":
            status, error_msg = _send_kakao_channel(clean_recipient, subject, body, ref_code)
        elif norm_channel == "SMS":
            status, error_msg = _send_sms_channel(clean_recipient, subject, body, ref_code)
        else:
            status, error_msg = "LOGGED_ONLY", f"지원 채널 대기({norm_channel})"
    except Exception as exc:
        status = "FAILED"
        error_msg = str(exc)[:300]

    try:
        log_id = record_notification_log(
            event_type=event_type,
            channel=norm_channel,
            recipient=clean_recipient,
            subject=subject,
            body=body,
            status=status,
            error_msg=error_msg,
            ref_code=ref_code,
            meta=meta,
        )
    except Exception:
        log_id = 0

    return {
        "ok": status != "FAILED",
        "id": log_id,
        "event_type": event_type,
        "channel": norm_channel,
        "recipient": clean_recipient,
        "status": status,
        "error_msg": error_msg,
        "ref_code": ref_code,
    }


def _resolve_order_customer_email(order: dict, explicit_email: str = "") -> str:
    mail = str(explicit_email or "").strip()
    if mail and "@" in mail:
        return mail
    uid = mall_database.parse_strict_int64(order.get("user_id"), min_val=1)
    if uid is not None:
        try:
            u = mall_database.get_mall_user_by_id(uid)
            if u and u.get("email"):
                return str(u["email"]).strip()
        except Exception:
            pass
    return ""


def _resolve_admin_email() -> str:
    env_mail = (os.getenv("ADMIN_NOTIFY_EMAIL") or os.getenv("MALL_ADMIN_NOTIFY_EMAIL") or "").strip()
    if env_mail:
        return env_mail
    try:
        info = mall_database.get_mall_store_info()
        c_mail = str(info.get("contact_email") or "").strip()
        if c_mail:
            return c_mail
    except Exception:
        pass
    return "admin@golabang.local"


def notify_order_created(order: dict, customer_email: str = "") -> list[dict[str, Any]]:
    if not isinstance(order, dict):
        return []
    ocode = str(order.get("order_code") or "").strip()
    buyer_name = str(order.get("buyer_name") or "").strip() or "고객"
    buyer_phone = str(order.get("buyer_phone") or "").strip()
    total_amt = int(order.get("total_amount") or 0)
    pm = str(order.get("payment_method") or "BANK")

    cust_mail = _resolve_order_customer_email(order, customer_email)
    cust_channel = "EMAIL" if cust_mail else "KAKAO_ALIMTALK"
    cust_recipient = cust_mail or buyer_phone or buyer_name
    cust_subject = f"[골라방 LIVE] 주문이 접수되었습니다 ({ocode})"
    cust_body = (
        f"{buyer_name}님, 주문번호 {ocode} (총 {total_amt:,}원, 결제수단: {pm}) 주문이 정상 접수되었습니다."
    )
    results = [
        dispatch_notification(
            event_type="ORDER_CREATED",
            channel=cust_channel,
            recipient=cust_recipient,
            subject=cust_subject,
            body=cust_body,
            ref_code=ocode,
            meta={"target": "CUSTOMER", "order_code": ocode, "total_amount": total_amt},
        )
    ]

    tg_token = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("MALL_TELEGRAM_BOT_TOKEN") or "").strip()
    tg_chat = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("MALL_TELEGRAM_CHAT_ID") or "").strip()
    admin_channel = "TELEGRAM" if (tg_token and tg_chat) else ("EMAIL" if (os.getenv("MALL_SMTP_HOST") or os.getenv("SMTP_HOST")) else "TELEGRAM")
    admin_recipient = tg_chat if admin_channel == "TELEGRAM" else _resolve_admin_email()
    admin_subject = f"[관리자 알림] 신규 주문 접수 ({ocode})"
    admin_body = f"신규 주문 {ocode} / 수령인: {buyer_name} / 금액: {total_amt:,}원"
    results.append(
        dispatch_notification(
            event_type="ORDER_CREATED",
            channel=admin_channel,
            recipient=admin_recipient or "admin",
            subject=admin_subject,
            body=admin_body,
            ref_code=ocode,
            meta={"target": "ADMIN", "order_code": ocode, "total_amount": total_amt},
        )
    )
    return results


def notify_payment_confirmed(order: dict, customer_email: str = "") -> list[dict[str, Any]]:
    if not isinstance(order, dict):
        return []
    ocode = str(order.get("order_code") or "").strip()
    buyer_name = str(order.get("buyer_name") or "").strip() or "고객"
    buyer_phone = str(order.get("buyer_phone") or "").strip()
    total_amt = int(order.get("total_amount") or 0)
    cust_mail = _resolve_order_customer_email(order, customer_email)
    cust_channel = "EMAIL" if cust_mail else "KAKAO_ALIMTALK"
    cust_recipient = cust_mail or buyer_phone or buyer_name
    subject = f"[골라방 LIVE] 입금완료 확인 ({ocode})"
    body = f"{buyer_name}님, 주문번호 {ocode} ({total_amt:,}원) 입금완료 확인되어 상품 준비를 시작합니다."
    return [
        dispatch_notification(
            event_type="PAYMENT_CONFIRMED",
            channel=cust_channel,
            recipient=cust_recipient,
            subject=subject,
            body=body,
            ref_code=ocode,
            meta={"target": "CUSTOMER", "order_code": ocode, "total_amount": total_amt},
        )
    ]


def notify_order_shipped(order: dict, customer_email: str = "") -> list[dict[str, Any]]:
    if not isinstance(order, dict):
        return []
    ocode = str(order.get("order_code") or "").strip()
    buyer_name = str(order.get("buyer_name") or "").strip() or "고객"
    buyer_phone = str(order.get("buyer_phone") or "").strip()
    carrier_name = str(order.get("carrier_name") or "택배").strip()
    tracking_no = str(order.get("tracking_number") or "").strip()
    cust_mail = _resolve_order_customer_email(order, customer_email)
    cust_channel = "EMAIL" if cust_mail else "SMS"
    cust_recipient = cust_mail or buyer_phone or buyer_name
    subject = f"[골라방 LIVE] 상품이 출고되었습니다 ({ocode})"
    body = f"{buyer_name}님, 주문번호 {ocode} 상품이 출고되었습니다. ({carrier_name} {tracking_no})".strip()
    return [
        dispatch_notification(
            event_type="ORDER_SHIPPED",
            channel=cust_channel,
            recipient=cust_recipient,
            subject=subject,
            body=body,
            ref_code=ocode,
            meta={"target": "CUSTOMER", "order_code": ocode, "carrier_name": carrier_name, "tracking_number": tracking_no},
        )
    ]


def notify_claim_requested(order: dict, claim_type: str = "", claim_reason: str = "") -> list[dict[str, Any]]:
    if not isinstance(order, dict):
        return []
    ocode = str(order.get("order_code") or "").strip()
    buyer_name = str(order.get("buyer_name") or "").strip() or "고객"
    ctype = str(claim_type or order.get("order_status") or "교환/반품 요청").strip()
    reason = str(claim_reason or order.get("claim_reason") or "").strip()
    tg_token = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("MALL_TELEGRAM_BOT_TOKEN") or "").strip()
    tg_chat = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("MALL_TELEGRAM_CHAT_ID") or "").strip()
    admin_channel = "TELEGRAM" if (tg_token and tg_chat) else ("EMAIL" if (os.getenv("MALL_SMTP_HOST") or os.getenv("SMTP_HOST")) else "TELEGRAM")
    admin_recipient = tg_chat if admin_channel == "TELEGRAM" else _resolve_admin_email()
    subject = f"[관리자 알림] {ctype} 접수 ({ocode})"
    body = f"주문번호 {ocode} ({buyer_name}) - {ctype}: {reason}"
    return [
        dispatch_notification(
            event_type="CLAIM_REQUESTED",
            channel=admin_channel,
            recipient=admin_recipient or "admin",
            subject=subject,
            body=body,
            ref_code=ocode,
            meta={"target": "ADMIN", "order_code": ocode, "claim_type": ctype},
        )
    ]


def notify_order_auto_cancelled(order: dict, expire_hours: int = 72) -> list[dict[str, Any]]:
    if not isinstance(order, dict):
        return []
    ocode = str(order.get("order_code") or "").strip()
    buyer_name = str(order.get("buyer_name") or "").strip() or "고객"
    buyer_phone = str(order.get("buyer_phone") or "").strip()
    total_amt = int(order.get("total_amount") or 0)
    hours = max(1, int(expire_hours or 72))

    cust_mail = _resolve_order_customer_email(order)
    cust_channel = "EMAIL" if cust_mail else "KAKAO_ALIMTALK"
    cust_recipient = cust_mail or buyer_phone or buyer_name
    cust_subject = f"[골라방 LIVE] 입금 기한 경과로 주문이 자동 취소되었습니다 ({ocode})"
    cust_body = (
        f"{buyer_name}님, 주문번호 {ocode} ({total_amt:,}원)은 입금 기한({hours}시간) 경과로 "
        f"자동 취소되었으며, 사용하신 쿠폰·적립금 및 재고가 복구되었습니다."
    )
    results = [
        dispatch_notification(
            event_type="ORDER_AUTO_CANCELLED",
            channel=cust_channel,
            recipient=cust_recipient,
            subject=cust_subject,
            body=cust_body,
            ref_code=ocode,
            meta={
                "target": "CUSTOMER",
                "order_code": ocode,
                "total_amount": total_amt,
                "expire_hours": hours,
            },
        )
    ]

    tg_token = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("MALL_TELEGRAM_BOT_TOKEN") or "").strip()
    tg_chat = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("MALL_TELEGRAM_CHAT_ID") or "").strip()
    admin_channel = "TELEGRAM" if (tg_token and tg_chat) else (
        "EMAIL" if (os.getenv("MALL_SMTP_HOST") or os.getenv("SMTP_HOST")) else "TELEGRAM"
    )
    admin_recipient = tg_chat if admin_channel == "TELEGRAM" else _resolve_admin_email()
    admin_subject = f"[관리자 알림] 미입금 주문 자동 취소 ({ocode})"
    admin_body = f"주문번호 {ocode} ({buyer_name}, {total_amt:,}원) - 미입금 {hours}시간 경과 자동 취소 처리"
    results.append(
        dispatch_notification(
            event_type="ORDER_AUTO_CANCELLED",
            channel=admin_channel,
            recipient=admin_recipient or "admin",
            subject=admin_subject,
            body=admin_body,
            ref_code=ocode,
            meta={
                "target": "ADMIN",
                "order_code": ocode,
                "total_amount": total_amt,
                "expire_hours": hours,
            },
        )
    )
    return results


def notify_restock_alert(
    alert: dict,
    product_name: str = "",
    new_stock: int = 0,
) -> list[dict[str, Any]]:
    if not isinstance(alert, dict):
        return []
    p_num = int(alert.get("product_num") or 0)
    color = str(alert.get("color") or "-").strip() or "-"
    size = str(alert.get("size") or "-").strip() or "-"
    ctype = str(alert.get("contact_type") or "EMAIL").strip().upper()
    cval = str(alert.get("contact_value") or "").strip()
    if not cval:
        return []

    if ctype == "EMAIL":
        channel = "EMAIL"
    elif ctype in ("KAKAO", "ALIMTALK", "KAKAO_ALIMTALK"):
        channel = "KAKAO_ALIMTALK"
    else:
        channel = "SMS"

    pname = str(product_name or f"상품 #{p_num}").strip()
    opt_label = f"{color}/{size}" if not (color == "-" and size == "-") else "전체 옵션"
    ref_code = f"RESTOCK-{p_num}"
    subject = f"[골라방 LIVE] 요청하신 상품이 재입고되었습니다 (#{p_num} {pname})"
    body = f"{pname} ({opt_label}) 옵션이 재입고되었습니다. (현재 잔여 재고: {max(0, int(new_stock or 0))}개)"
    return [
        dispatch_notification(
            event_type="RESTOCK_ALERT",
            channel=channel,
            recipient=cval,
            subject=subject,
            body=body,
            ref_code=ref_code,
            meta={
                "target": "CUSTOMER",
                "product_num": p_num,
                "color": color,
                "size": size,
                "new_stock": max(0, int(new_stock or 0)),
                "alert_id": int(alert.get("id") or 0),
            },
        )
    ]


def list_notification_logs(
    event_type: str = "",
    channel: str = "",
    status: str = "",
    query: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    ev = str(event_type or "").strip().upper()
    ch = str(channel or "").strip().upper()
    st = str(status or "").strip().upper()
    q = str(query or "").strip().lower()

    with mall_database.mall_db_lock:
        conn = mall_database.get_connection()
        try:
            cur = conn.cursor()
            where = []
            params: list[Any] = []
            if ev and ev != "ALL":
                where.append("UPPER(event_type) = ?")
                params.append(ev)
            if ch and ch != "ALL":
                where.append("UPPER(channel) = ?")
                params.append(_normalize_channel(ch))
            if st and st != "ALL":
                where.append("UPPER(status) = ?")
                params.append(st)
            where_sql = f"WHERE {' AND '.join(where)}" if where else ""
            cur.execute(
                f"SELECT * FROM mall_notification_logs {where_sql} ORDER BY id DESC LIMIT ?",
                (*params, max(1, min(500, int(limit or 200)))),
            )
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                d["id"] = int(d["id"])
                if q:
                    hay = f"{d.get('event_type','')} {d.get('channel','')} {d.get('recipient','')} {d.get('subject','')} {d.get('body','')} {d.get('ref_code','')}".lower()
                    if q not in hay:
                        continue
                rows.append(d)
            return rows
        finally:
            conn.close()
