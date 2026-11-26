# -*- coding: utf-8 -*-
"""
쇼핑몰 회원 마이페이지 · 배송지 주소록 · 비밀번호 재설정 · 관리자 회원 관리 Blueprint (routes_member.py)
"""

from typing import Optional
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for

import mall_database
import mall_member_service
import mall_security

member_bp = Blueprint("member_bp", __name__)


def _resolve_current_user() -> Optional[dict]:
    fn = current_app.config.get("MALL_GET_CURRENT_USER")
    if callable(fn):
        return fn()
    uid = session.get("mall_user_id")
    if uid:
        return mall_database.get_mall_user_by_id(int(uid))
    return None


def _resolve_public_url() -> str:
    fn = current_app.config.get("MALL_RESOLVE_PUBLIC_URL")
    if callable(fn):
        return str(fn() or "")
    return ""


def _read_request_payload() -> dict:
    if request.is_json:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}
    return request.form.to_dict()


@member_bp.route("/mypage", methods=["GET"])
def mall_mypage():
    user = _resolve_current_user()
    if not user:
        return redirect(url_for("mall_auth_login", next="/mypage"))
    addresses = mall_member_service.list_user_addresses(int(user["id"]))
    orders = mall_database.get_mall_orders_by_user(int(user["id"]), limit=50)
    live_state = mall_database.get_full_live_state()
    return render_template(
        "mypage.html",
        addresses=addresses,
        order_count=len(orders),
        live_state=live_state,
        active_nav="mypage",
    )


@member_bp.route("/api/mypage/profile", methods=["POST"])
def api_mypage_profile():
    user = _resolve_current_user()
    if not user:
        return jsonify({"status": "error", "code": "AUTH_REQUIRED", "message": "로그인이 필요합니다."}), 401
    if not mall_security.verify_csrf_token(session, request):
        return jsonify({"status": "error", "code": "CSRF_INVALID", "message": "CSRF 보안 토큰 검증에 실패했습니다."}), 403

    data = _read_request_payload()
    res = mall_member_service.update_member_profile(int(user["id"]), data)
    if not res.get("ok"):
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "message": res.get("message") or "프로필 수정 실패"}), status
    return jsonify({
        "status": "ok",
        "user": res.get("user"),
        "addresses": res.get("addresses", []),
    }), 200


@member_bp.route("/api/mypage/addresses", methods=["GET"])
def api_mypage_addresses_list():
    user = _resolve_current_user()
    if not user:
        return jsonify({"status": "error", "code": "AUTH_REQUIRED", "message": "로그인이 필요합니다."}), 401
    addresses = mall_member_service.list_user_addresses(int(user["id"]))
    return jsonify({"status": "ok", "addresses": addresses}), 200


@member_bp.route("/api/mypage/addresses/save", methods=["POST"])
def api_mypage_addresses_save():
    user = _resolve_current_user()
    if not user:
        return jsonify({"status": "error", "code": "AUTH_REQUIRED", "message": "로그인이 필요합니다."}), 401
    if not mall_security.verify_csrf_token(session, request):
        return jsonify({"status": "error", "code": "CSRF_INVALID", "message": "CSRF 보안 토큰 검증에 실패했습니다."}), 403

    data = _read_request_payload()
    res = mall_member_service.save_user_address(int(user["id"]), data)
    if not res.get("ok"):
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "message": res.get("message") or "배송지 저장 실패"}), status
    return jsonify({
        "status": "ok",
        "address": res.get("address"),
        "addresses": res.get("addresses", []),
    }), 200


@member_bp.route("/api/mypage/addresses/delete", methods=["POST"])
def api_mypage_addresses_delete():
    user = _resolve_current_user()
    if not user:
        return jsonify({"status": "error", "code": "AUTH_REQUIRED", "message": "로그인이 필요합니다."}), 401
    if not mall_security.verify_csrf_token(session, request):
        return jsonify({"status": "error", "code": "CSRF_INVALID", "message": "CSRF 보안 토큰 검증에 실패했습니다."}), 403

    data = _read_request_payload()
    addr_id = data.get("id") if data.get("id") is not None else data.get("address_id")
    res = mall_member_service.delete_user_address(int(user["id"]), addr_id)
    if not res.get("ok"):
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "message": res.get("message") or "배송지 삭제 실패"}), status
    return jsonify({"status": "ok", "addresses": res.get("addresses", [])}), 200


@member_bp.route("/api/mypage/addresses/default", methods=["POST"])
def api_mypage_addresses_default():
    user = _resolve_current_user()
    if not user:
        return jsonify({"status": "error", "code": "AUTH_REQUIRED", "message": "로그인이 필요합니다."}), 401
    if not mall_security.verify_csrf_token(session, request):
        return jsonify({"status": "error", "code": "CSRF_INVALID", "message": "CSRF 보안 토큰 검증에 실패했습니다."}), 403

    data = _read_request_payload()
    addr_id = data.get("id") if data.get("id") is not None else data.get("address_id")
    res = mall_member_service.set_default_user_address(int(user["id"]), addr_id)
    if not res.get("ok"):
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "message": res.get("message") or "기본 배송지 설정 실패"}), status
    return jsonify({"status": "ok", "addresses": res.get("addresses", [])}), 200


@member_bp.route("/api/mypage/password", methods=["POST"])
def api_mypage_password():
    user = _resolve_current_user()
    if not user:
        return jsonify({"status": "error", "code": "AUTH_REQUIRED", "message": "로그인이 필요합니다."}), 401
    if not mall_security.verify_csrf_token(session, request):
        return jsonify({"status": "error", "code": "CSRF_INVALID", "message": "CSRF 보안 토큰 검증에 실패했습니다."}), 403

    data = _read_request_payload()
    res = mall_member_service.change_member_password(
        int(user["id"]),
        current_password=str(data.get("current_password") or ""),
        new_password=str(data.get("new_password") or ""),
        confirm_password=data.get("confirm_password"),
    )
    if not res.get("ok"):
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "message": res.get("message") or "비밀번호 변경 실패"}), status

    mall_security.rotate_session(
        session,
        preserve_keys=[
            "mall_user_id",
            "mall_role",
            "mall_auth_source",
            "studio_sso_user_id",
            "studio_sso_display_name",
        ],
    )
    return jsonify({
        "status": "ok",
        "message": res.get("message"),
        "csrf_token": mall_security.get_csrf_token(session),
    }), 200


@member_bp.route("/api/mypage/withdraw", methods=["POST"])
def api_mypage_withdraw():
    user = _resolve_current_user()
    if not user:
        return jsonify({"status": "error", "code": "AUTH_REQUIRED", "message": "로그인이 필요합니다."}), 401
    if not mall_security.verify_csrf_token(session, request):
        return jsonify({"status": "error", "code": "CSRF_INVALID", "message": "CSRF 보안 토큰 검증에 실패했습니다."}), 403

    data = _read_request_payload()
    res = mall_member_service.withdraw_member_account(
        int(user["id"]),
        password=str(data.get("password") or ""),
    )
    if not res.get("ok"):
        if res.get("forbidden"):
            return jsonify({"status": "error", "message": res.get("message")}), 403
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "message": res.get("message") or "회원 탈퇴 처리 실패"}), status

    session.clear()
    session["sso_opt_out"] = True
    return jsonify({
        "status": "ok",
        "withdrawn_at": res.get("withdrawn_at"),
        "message": res.get("message"),
        "redirect": "/",
    }), 200


@member_bp.route("/auth/forgot-password", methods=["GET", "POST"])
def mall_auth_forgot_password():
    if request.method == "GET":
        live_state = mall_database.get_full_live_state()
        return render_template("auth_forgot_password.html", live_state=live_state)

    ip_addr = str(request.remote_addr or "unknown")
    allowed, retry_sec = mall_security.rate_limiter.check("mall_forgot_pw", ip_addr, max_requests=8, window_sec=60.0)
    if not allowed:
        msg = f"요청이 너무 많습니다. {retry_sec}초 후 다시 시도해 주세요."
        if request.is_json:
            return jsonify({"status": "error", "message": msg}), 429
        live_state = mall_database.get_full_live_state()
        return render_template("auth_forgot_password.html", live_state=live_state, error=msg), 429

    if not mall_security.verify_csrf_token(session, request):
        msg = "보안 토큰(CSRF)이 만료되었습니다. 다시 시도해 주세요."
        if request.is_json:
            return jsonify({"status": "error", "code": "CSRF_INVALID", "message": msg}), 403
        live_state = mall_database.get_full_live_state()
        return render_template("auth_forgot_password.html", live_state=live_state, error=msg), 403

    data = _read_request_payload()
    email = str(data.get("email") or "").strip()
    res = mall_member_service.request_password_reset(email, public_base_url=_resolve_public_url())
    if not res.get("ok"):
        msg = str(res.get("message") or "유효한 이메일을 입력해 주세요.")
        if request.is_json:
            return jsonify({"status": "error", "message": msg}), 400
        live_state = mall_database.get_full_live_state()
        return render_template("auth_forgot_password.html", live_state=live_state, error=msg, email=email), 400

    msg = str(res.get("message") or "")
    if request.is_json:
        return jsonify({"status": "ok", "message": msg}), 200
    live_state = mall_database.get_full_live_state()
    return render_template("auth_forgot_password.html", live_state=live_state, success=msg, email=email), 200


@member_bp.route("/auth/reset-password", methods=["GET", "POST"])
def mall_auth_reset_password():
    if request.method == "GET":
        token = str(request.args.get("token") or "").strip()
        email = str(request.args.get("email") or "").strip()
        live_state = mall_database.get_full_live_state()
        return render_template("auth_reset_password.html", live_state=live_state, token=token, email=email)

    ip_addr = str(request.remote_addr or "unknown")
    allowed, retry_sec = mall_security.rate_limiter.check("mall_reset_pw", ip_addr, max_requests=10, window_sec=60.0)
    if not allowed:
        msg = f"요청이 너무 많습니다. {retry_sec}초 후 다시 시도해 주세요."
        if request.is_json:
            return jsonify({"status": "error", "message": msg}), 429
        live_state = mall_database.get_full_live_state()
        return render_template("auth_reset_password.html", live_state=live_state, error=msg), 429

    if not mall_security.verify_csrf_token(session, request):
        msg = "보안 토큰(CSRF)이 만료되었습니다. 다시 시도해 주세요."
        if request.is_json:
            return jsonify({"status": "error", "code": "CSRF_INVALID", "message": msg}), 403
        live_state = mall_database.get_full_live_state()
        return render_template("auth_reset_password.html", live_state=live_state, error=msg), 403

    data = _read_request_payload()
    token = str(data.get("token") or "").strip()
    email = str(data.get("email") or "").strip()
    code = str(data.get("code") or data.get("temp_code") or "").strip()
    new_password = str(data.get("new_password") or "")
    confirm_password = data.get("confirm_password")

    res = mall_member_service.complete_password_reset(
        new_password=new_password,
        confirm_password=confirm_password,
        token=token,
        email=email,
        code=code,
    )
    if not res.get("ok"):
        msg = str(res.get("message") or "비밀번호 재설정에 실패했습니다.")
        if request.is_json:
            return jsonify({"status": "error", "message": msg}), 400
        live_state = mall_database.get_full_live_state()
        return render_template(
            "auth_reset_password.html",
            live_state=live_state,
            error=msg,
            token=token,
            email=email,
        ), 400

    msg = str(res.get("message") or "")
    if request.is_json:
        return jsonify({"status": "ok", "message": msg, "redirect": "/auth/login"}), 200
    live_state = mall_database.get_full_live_state()
    return render_template("auth_reset_password.html", live_state=live_state, success=msg, token="", email=""), 200


@member_bp.route("/api/admin/users", methods=["GET"])
def api_admin_users_list():
    q = str(request.args.get("q") or "").strip()
    status_f = str(request.args.get("status") or "ALL").strip()
    role_f = str(request.args.get("role") or "ALL").strip()
    users = mall_member_service.list_admin_users(query=q, status_filter=status_f, role_filter=role_f)
    return jsonify({"status": "ok", "users": users, "count": len(users)}), 200


@member_bp.route("/api/admin/users/status", methods=["POST"])
def api_admin_users_status():
    acting_admin = _resolve_current_user()
    if not acting_admin or acting_admin.get("role") != "ADMIN":
        return jsonify({"status": "error", "code": "ADMIN_REQUIRED", "message": "관리자 권한이 필요합니다."}), 403

    data = _read_request_payload()
    target_user_id = data.get("user_id") or data.get("id")
    if "is_active" not in data:
        return jsonify({"status": "error", "message": "is_active 값이 필요합니다."}), 400

    res = mall_member_service.set_admin_user_status(
        target_user_id=target_user_id,
        is_active=data.get("is_active"),
        acting_admin_id=int(acting_admin["id"]),
    )
    if not res.get("ok"):
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "message": res.get("message") or "회원 상태 변경 실패"}), status
    updated_user = res.get("user") or {}
    import mall_ops
    mall_ops.log_current_admin_action(
        action="USER_STATUS_UPDATE",
        target_type="user",
        target_id=updated_user.get("id") or target_user_id,
        detail_dict={
            "user_id": updated_user.get("id") or target_user_id,
            "email": updated_user.get("email"),
            "is_active": updated_user.get("is_active"),
            "account_status": updated_user.get("account_status"),
        },
    )
    return jsonify({"status": "ok", "user": updated_user}), 200
