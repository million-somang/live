# -*- coding: utf-8 -*-
"""
쇼핑몰 결제 어댑터 (mall_payment.py)
- 무통장 입금(BankTransferGateway) 즉시 동작 보장
- 포트원 V2(PortOneV2Gateway) / 토스페이먼츠(TossPaymentsGateway) PG 어댑터 구조
- Fail-closed 웹훅 HMAC 서명 검증 및 결제 금액 무결성 검증
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Optional

import mall_database


def _parse_strict_int_amount(val: Any) -> int:
    if val is None or isinstance(val, bool):
        raise ValueError("유효하지 않은 결제 금액입니다.")
    if isinstance(val, int):
        if val < mall_database.SQLITE_INT64_MIN or val > mall_database.SQLITE_INT64_MAX:
            raise ValueError("결제 금액이 허용 범위를 초과했습니다.")
        return val
    if isinstance(val, float):
        if not math.isfinite(val) or not val.is_integer():
            raise ValueError("결제 금액은 정수여야 합니다.")
        iv = int(val)
        if iv < mall_database.SQLITE_INT64_MIN or iv > mall_database.SQLITE_INT64_MAX:
            raise ValueError("결제 금액이 허용 범위를 초과했습니다.")
        return iv
    if isinstance(val, str):
        s = val.strip()
        if not s or "." in s or not re.fullmatch(r"[+-]?[0-9]+", s):
            raise ValueError("결제 금액은 정수여야 합니다.")
        is_neg = s.startswith("-")
        digits = (s[1:] if s[0] in "+-" else s).lstrip("0") or "0"
        if len(digits) > 19:
            raise ValueError("결제 금액이 허용 범위를 초과했습니다.")
        iv = -int(digits) if is_neg else int(digits)
        if iv < mall_database.SQLITE_INT64_MIN or iv > mall_database.SQLITE_INT64_MAX:
            raise ValueError("결제 금액이 허용 범위를 초과했습니다.")
        return iv
    raise ValueError("유효하지 않은 결제 금액입니다.")


def normalize_payment_method(method: Any) -> str:
    val = str(method or "BANK").strip().upper()
    if not val:
        return "BANK"
    mapping = {
        "BANK": "BANK",
        "BANK_TRANSFER": "BANK",
        "PORTONE": "PORTONE_V2",
        "PORTONE_V2": "PORTONE_V2",
        "CARD": "PORTONE_V2",
        "TOSS": "TOSS_PAYMENTS",
        "TOSS_PAYMENTS": "TOSS_PAYMENTS",
    }
    if val not in mapping:
        raise ValueError(f"지원하지 않는 결제 수단입니다: {method}")
    return mapping[val]


def _extract_header_ci(headers: dict[str, Any], candidates: tuple[str, ...]) -> str:
    lowered = {str(k).lower(): str(v or "").strip() for k, v in (headers or {}).items()}
    for name in candidates:
        val = lowered.get(name.lower(), "")
        if val:
            return val
    return ""


def _verify_hmac_sha256_signature(raw_body: bytes, secret: str, signature_header: str) -> bool:
    if not secret or not signature_header:
        return False
    body_bytes = raw_body if isinstance(raw_body, (bytes, bytearray)) else str(raw_body or "").encode("utf-8")
    digest_bytes = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).digest()
    expected_hex = digest_bytes.hex()
    expected_b64 = base64.b64encode(digest_bytes).decode("ascii")

    tokens = [part.strip() for part in str(signature_header).replace(",", " ").split() if part.strip()]
    for tok in tokens:
        candidate = tok
        if "=" in tok and tok.split("=", 1)[0].lower() in ("sha256", "v1", "sig"):
            candidate = tok.split("=", 1)[1].strip()
        if hmac.compare_digest(candidate.lower(), expected_hex.lower()):
            return True
        if hmac.compare_digest(candidate, expected_b64):
            return True
    return False


class BasePaymentGateway(ABC):
    provider_code: str = ""

    @abstractmethod
    def prepare_payment(self, order: dict, return_url: str = "", cancel_url: str = "") -> dict:
        raise NotImplementedError

    @abstractmethod
    def verify_payment(self, order: dict, payload: dict) -> dict:
        raise NotImplementedError

    @abstractmethod
    def cancel_payment(self, order: dict, reason: str = "", amount: Optional[int] = None) -> dict:
        raise NotImplementedError

    @abstractmethod
    def verify_webhook(self, raw_body: bytes, headers: dict, payload: dict) -> dict:
        raise NotImplementedError


class BankTransferGateway(BasePaymentGateway):
    provider_code = "BANK"

    def prepare_payment(self, order: dict, return_url: str = "", cancel_url: str = "") -> dict:
        store_info = mall_database.get_mall_store_info()
        return {
            "ok": True,
            "payment_method": "BANK",
            "provider": self.provider_code,
            "order_code": str(order.get("order_code") or ""),
            "amount": int(order.get("total_amount") or 0),
            "status": "WAITING_DEPOSIT",
            "depositor_name": str(order.get("depositor_name") or ""),
            "cash_receipt_type": str(order.get("cash_receipt_type") or "NONE"),
            "cash_receipt_no": str(order.get("cash_receipt_no") or ""),
            "bank_info": {
                "bank_name": store_info.get("bank_name", ""),
                "bank_account": store_info.get("bank_account", ""),
                "bank_holder": store_info.get("bank_holder", ""),
            },
            "return_url": return_url,
            "cancel_url": cancel_url,
        }

    def verify_payment(self, order: dict, payload: dict) -> dict:
        expected_amount = int(order.get("total_amount") or 0)
        if "amount" in payload and payload.get("amount") is not None and str(payload.get("amount")).strip() != "":
            try:
                req_amount = _parse_strict_int_amount(payload.get("amount"))
            except ValueError:
                return {"ok": False, "code": "INVALID_AMOUNT", "message": "유효하지 않은 결제 금액입니다."}
            if req_amount != expected_amount:
                return {"ok": False, "code": "AMOUNT_MISMATCH", "message": "결제 금액이 주문 금액과 일치하지 않습니다."}
        return {
            "ok": True,
            "provider": self.provider_code,
            "pg_tid": str(payload.get("pg_tid") or f"bank_{order.get('order_code') or ''}"),
            "pg_status": "WAITING_DEPOSIT",
            "status": "WAITING_DEPOSIT",
            "amount": expected_amount,
            "mode": "BANK",
        }

    def cancel_payment(self, order: dict, reason: str = "", amount: Optional[int] = None) -> dict:
        return {
            "ok": True,
            "provider": self.provider_code,
            "order_code": str(order.get("order_code") or ""),
            "status": "CANCELLED",
            "reason": str(reason or ""),
            "amount": int(amount if amount is not None else (order.get("total_amount") or 0)),
        }

    def verify_webhook(self, raw_body: bytes, headers: dict, payload: dict) -> dict:
        return {
            "ok": False,
            "status_code": 400,
            "message": "무통장 입금은 PG 웹훅을 지원하지 않습니다.",
        }


class PortOneV2Gateway(BasePaymentGateway):
    provider_code = "PORTONE_V2"

    def _config(self) -> dict[str, str]:
        return {
            "api_secret": (os.getenv("PORTONE_API_SECRET") or "").strip(),
            "store_id": (os.getenv("PORTONE_STORE_ID") or "").strip(),
            "channel_key": (os.getenv("PORTONE_CHANNEL_KEY") or "").strip(),
            "webhook_secret": (os.getenv("PORTONE_WEBHOOK_SECRET") or "").strip(),
        }

    def prepare_payment(self, order: dict, return_url: str = "", cancel_url: str = "") -> dict:
        cfg = self._config()
        ocode = str(order.get("order_code") or "")
        pid = f"po2_{ocode}"
        return {
            "ok": True,
            "payment_method": "PORTONE_V2",
            "provider": self.provider_code,
            "order_code": ocode,
            "amount": int(order.get("total_amount") or 0),
            "payment_id": pid,
            "payment_key": pid,
            "merchant_uid": ocode,
            "store_id": cfg["store_id"],
            "channel_key": cfg["channel_key"],
            "return_url": return_url,
            "cancel_url": cancel_url,
            "mode": "LIVE" if cfg["api_secret"] else "SANDBOX",
        }

    def verify_payment(self, order: dict, payload: dict) -> dict:
        cfg = self._config()
        expected_amount = int(order.get("total_amount") or 0)
        payment_key = str(
            payload.get("payment_key")
            or payload.get("payment_id")
            or payload.get("pg_tid")
            or payload.get("transaction_id")
            or payload.get("merchant_uid")
            or ""
        ).strip()
        if not payment_key:
            return {"ok": False, "code": "MISSING_PAYMENT_KEY", "message": "결제 검증용 payment_key 또는 payment_id가 필요합니다."}

        if "amount" in payload and payload.get("amount") is not None and str(payload.get("amount")).strip() != "":
            try:
                req_amount = _parse_strict_int_amount(payload.get("amount"))
            except ValueError:
                return {"ok": False, "code": "INVALID_AMOUNT", "message": "유효하지 않은 결제 금액입니다."}
            if req_amount != expected_amount:
                return {"ok": False, "code": "AMOUNT_MISMATCH", "message": "결제 금액이 주문 금액과 일치하지 않습니다."}

        if cfg["api_secret"]:
            url = f"https://api.portone.io/payments/{urllib.parse.quote(payment_key, safe='')}"
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"PortOne {cfg['api_secret']}",
                    "Accept": "application/json",
                },
                method="GET",
            )
            try:
                with urllib.request.urlopen(req, timeout=4.0) as resp:
                    body = json.loads(resp.read().decode("utf-8") or "{}")
            except urllib.error.HTTPError as exc:
                return {"ok": False, "code": "PG_HTTP_ERROR", "message": f"포트원 결제 조회 실패 ({exc.code})"}
            except Exception as exc:
                return {"ok": False, "code": "PG_NETWORK_ERROR", "message": f"포트원 결제 조회 중 오류: {exc}"}

            pg_status = str(body.get("status") or "").strip().upper()
            amt_obj = body.get("amount") if isinstance(body.get("amount"), dict) else {}
            raw_total = amt_obj.get("total")
            if raw_total is None:
                total_amt = -1
            else:
                try:
                    total_amt = _parse_strict_int_amount(raw_total)
                except ValueError:
                    total_amt = -1
            if pg_status != "PAID":
                return {"ok": False, "code": "PG_NOT_PAID", "message": f"포트원 결제 상태가 완료가 아닙니다: {pg_status}"}
            if total_amt != expected_amount:
                return {"ok": False, "code": "AMOUNT_MISMATCH", "message": "포트원 승인 금액과 주문 금액이 일치하지 않습니다."}
            return {
                "ok": True,
                "provider": self.provider_code,
                "pg_tid": str(body.get("id") or payment_key),
                "pg_status": "PAID",
                "status": "PAID",
                "amount": expected_amount,
                "mode": "LIVE",
            }

        return {
            "ok": True,
            "provider": self.provider_code,
            "pg_tid": payment_key,
            "pg_status": "PAID",
            "status": "PAID",
            "amount": expected_amount,
            "mode": "SANDBOX",
        }

    def cancel_payment(self, order: dict, reason: str = "", amount: Optional[int] = None) -> dict:
        cfg = self._config()
        tid = str(order.get("pg_tid") or f"po2_{order.get('order_code') or ''}").strip()
        cancel_amt = int(amount if amount is not None else (order.get("total_amount") or 0))
        if cfg["api_secret"] and tid:
            url = f"https://api.portone.io/payments/{urllib.parse.quote(tid, safe='')}/cancel"
            data = json.dumps({"reason": reason or "고객 요청 취소", "amount": cancel_amt}, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Authorization": f"PortOne {cfg['api_secret']}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=4.0) as resp:
                    body = json.loads(resp.read().decode("utf-8") or "{}")
                return {
                    "ok": True,
                    "provider": self.provider_code,
                    "order_code": str(order.get("order_code") or ""),
                    "pg_tid": tid,
                    "status": "CANCELLED",
                    "raw": body,
                    "mode": "LIVE",
                }
            except Exception as exc:
                return {"ok": False, "provider": self.provider_code, "message": f"포트원 취소 실패: {exc}"}
        return {
            "ok": True,
            "provider": self.provider_code,
            "order_code": str(order.get("order_code") or ""),
            "pg_tid": tid,
            "status": "CANCELLED",
            "reason": str(reason or ""),
            "amount": cancel_amt,
            "mode": "SANDBOX",
        }

    def verify_webhook(self, raw_body: bytes, headers: dict, payload: dict) -> dict:
        cfg = self._config()
        secret = cfg["webhook_secret"] or cfg["api_secret"]
        sig = _extract_header_ci(headers, ("X-PortOne-Signature", "webhook-signature", "X-Webhook-Signature"))
        if not secret or not sig:
            return {"ok": False, "status_code": 401, "message": "웹훅 서명 검증에 실패했습니다."}
        if not _verify_hmac_sha256_signature(raw_body, secret, sig):
            return {"ok": False, "status_code": 401, "message": "유효하지 않은 포트원 웹훅 서명입니다."}

        data_obj = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        order_code = str(
            data_obj.get("order_code")
            or data_obj.get("merchant_uid")
            or payload.get("order_code")
            or payload.get("merchant_uid")
            or ""
        ).strip()
        pg_tid = str(
            data_obj.get("paymentId")
            or data_obj.get("payment_id")
            or data_obj.get("pg_tid")
            or data_obj.get("transactionId")
            or f"po2_{order_code}"
        ).strip()
        raw_status = str(data_obj.get("status") or payload.get("type") or "").strip().upper()
        pg_status = "PAID" if raw_status in ("PAID", "DONE", "TRANSACTION.PAID") else raw_status
        raw_amount = data_obj.get("amount") if data_obj.get("amount") is not None else payload.get("amount")
        if isinstance(raw_amount, dict):
            raw_amount = raw_amount.get("total")
        return {
            "ok": True,
            "provider": self.provider_code,
            "order_code": order_code,
            "pg_tid": pg_tid,
            "pg_status": pg_status,
            "amount": raw_amount,
        }


class TossPaymentsGateway(BasePaymentGateway):
    provider_code = "TOSS_PAYMENTS"

    def _config(self) -> dict[str, str]:
        return {
            "secret_key": (os.getenv("TOSS_SECRET_KEY") or "").strip(),
            "client_key": (os.getenv("TOSS_CLIENT_KEY") or "").strip(),
            "webhook_secret": (os.getenv("TOSS_WEBHOOK_SECRET") or "").strip(),
        }

    def prepare_payment(self, order: dict, return_url: str = "", cancel_url: str = "") -> dict:
        cfg = self._config()
        ocode = str(order.get("order_code") or "")
        pkey = f"toss_{ocode}"
        return {
            "ok": True,
            "payment_method": "TOSS_PAYMENTS",
            "provider": self.provider_code,
            "order_code": ocode,
            "amount": int(order.get("total_amount") or 0),
            "payment_key": pkey,
            "merchant_uid": ocode,
            "client_key": cfg["client_key"],
            "return_url": return_url,
            "cancel_url": cancel_url,
            "mode": "LIVE" if cfg["secret_key"] else "SANDBOX",
        }

    def verify_payment(self, order: dict, payload: dict) -> dict:
        cfg = self._config()
        expected_amount = int(order.get("total_amount") or 0)
        payment_key = str(
            payload.get("payment_key")
            or payload.get("paymentKey")
            or payload.get("pg_tid")
            or payload.get("payment_id")
            or ""
        ).strip()
        if not payment_key:
            return {"ok": False, "code": "MISSING_PAYMENT_KEY", "message": "결제 승인용 payment_key가 필요합니다."}

        if "amount" in payload and payload.get("amount") is not None and str(payload.get("amount")).strip() != "":
            try:
                req_amount = _parse_strict_int_amount(payload.get("amount"))
            except ValueError:
                return {"ok": False, "code": "INVALID_AMOUNT", "message": "유효하지 않은 결제 금액입니다."}
            if req_amount != expected_amount:
                return {"ok": False, "code": "AMOUNT_MISMATCH", "message": "결제 금액이 주문 금액과 일치하지 않습니다."}

        if cfg["secret_key"]:
            auth_raw = base64.b64encode(f"{cfg['secret_key']}:".encode("utf-8")).decode("ascii")
            body_bytes = json.dumps(
                {
                    "paymentKey": payment_key,
                    "orderId": str(order.get("order_code") or ""),
                    "amount": expected_amount,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            req = urllib.request.Request(
                "https://api.tosspayments.com/v1/payments/confirm",
                data=body_bytes,
                headers={
                    "Authorization": f"Basic {auth_raw}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=4.0) as resp:
                    body = json.loads(resp.read().decode("utf-8") or "{}")
            except urllib.error.HTTPError as exc:
                return {"ok": False, "code": "PG_HTTP_ERROR", "message": f"토스페이먼츠 승인 실패 ({exc.code})"}
            except Exception as exc:
                return {"ok": False, "code": "PG_NETWORK_ERROR", "message": f"토스페이먼츠 승인 오류: {exc}"}

            pg_status = str(body.get("status") or "").strip().upper()
            raw_total = body.get("totalAmount")
            if raw_total is None:
                total_amt = -1
            else:
                try:
                    total_amt = _parse_strict_int_amount(raw_total)
                except ValueError:
                    total_amt = -1
            if pg_status != "DONE":
                return {"ok": False, "code": "PG_NOT_PAID", "message": f"토스페이먼츠 결제 상태가 완료가 아닙니다: {pg_status}"}
            if total_amt != expected_amount:
                return {"ok": False, "code": "AMOUNT_MISMATCH", "message": "토스페이먼츠 승인 금액과 주문 금액이 일치하지 않습니다."}
            return {
                "ok": True,
                "provider": self.provider_code,
                "pg_tid": str(body.get("paymentKey") or payment_key),
                "pg_status": "PAID",
                "status": "DONE",
                "amount": expected_amount,
                "mode": "LIVE",
            }

        return {
            "ok": True,
            "provider": self.provider_code,
            "pg_tid": payment_key,
            "pg_status": "PAID",
            "status": "DONE",
            "amount": expected_amount,
            "mode": "SANDBOX",
        }

    def cancel_payment(self, order: dict, reason: str = "", amount: Optional[int] = None) -> dict:
        cfg = self._config()
        tid = str(order.get("pg_tid") or f"toss_{order.get('order_code') or ''}").strip()
        cancel_amt = int(amount if amount is not None else (order.get("total_amount") or 0))
        if cfg["secret_key"] and tid:
            auth_raw = base64.b64encode(f"{cfg['secret_key']}:".encode("utf-8")).decode("ascii")
            url = f"https://api.tosspayments.com/v1/payments/{urllib.parse.quote(tid, safe='')}/cancel"
            body_bytes = json.dumps({"cancelReason": reason or "고객 요청 취소", "cancelAmount": cancel_amt}, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=body_bytes,
                headers={
                    "Authorization": f"Basic {auth_raw}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=4.0) as resp:
                    body = json.loads(resp.read().decode("utf-8") or "{}")
                return {
                    "ok": True,
                    "provider": self.provider_code,
                    "order_code": str(order.get("order_code") or ""),
                    "pg_tid": tid,
                    "status": "CANCELLED",
                    "raw": body,
                    "mode": "LIVE",
                }
            except Exception as exc:
                return {"ok": False, "provider": self.provider_code, "message": f"토스페이먼츠 취소 실패: {exc}"}
        return {
            "ok": True,
            "provider": self.provider_code,
            "order_code": str(order.get("order_code") or ""),
            "pg_tid": tid,
            "status": "CANCELLED",
            "reason": str(reason or ""),
            "amount": cancel_amt,
            "mode": "SANDBOX",
        }

    def verify_webhook(self, raw_body: bytes, headers: dict, payload: dict) -> dict:
        cfg = self._config()
        secret = cfg["webhook_secret"] or cfg["secret_key"]
        sig = _extract_header_ci(headers, ("TossPayments-Signature", "X-Toss-Signature", "X-Webhook-Signature"))
        if not secret or not sig:
            return {"ok": False, "status_code": 401, "message": "웹훅 서명 검증에 실패했습니다."}
        if not _verify_hmac_sha256_signature(raw_body, secret, sig):
            return {"ok": False, "status_code": 401, "message": "유효하지 않은 토스페이먼츠 웹훅 서명입니다."}

        data_obj = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        order_code = str(
            data_obj.get("orderId")
            or data_obj.get("order_code")
            or payload.get("orderId")
            or payload.get("order_code")
            or ""
        ).strip()
        pg_tid = str(
            data_obj.get("paymentKey")
            or data_obj.get("payment_key")
            or data_obj.get("pg_tid")
            or f"toss_{order_code}"
        ).strip()
        raw_status = str(data_obj.get("status") or payload.get("status") or "").strip().upper()
        pg_status = "PAID" if raw_status in ("DONE", "PAID", "TRANSACTION.PAID") else raw_status
        raw_amount = data_obj.get("totalAmount") if data_obj.get("totalAmount") is not None else data_obj.get("amount")
        if raw_amount is None:
            raw_amount = payload.get("totalAmount") if payload.get("totalAmount") is not None else payload.get("amount")
        return {
            "ok": True,
            "provider": self.provider_code,
            "order_code": order_code,
            "pg_tid": pg_tid,
            "pg_status": pg_status,
            "amount": raw_amount,
        }


def get_payment_gateway(method: Any) -> BasePaymentGateway:
    canonical = normalize_payment_method(method)
    if canonical == "BANK":
        return BankTransferGateway()
    if canonical == "PORTONE_V2":
        return PortOneV2Gateway()
    if canonical == "TOSS_PAYMENTS":
        return TossPaymentsGateway()
    raise ValueError(f"지원하지 않는 결제 수단입니다: {method}")
