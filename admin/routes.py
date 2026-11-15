# -*- coding: utf-8 -*-
"""
독립 관리자 페이지 라우트 블루프린트 (admin/routes.py)
- URL Prefix: /admin
- 일반 OBS 스튜디오(/overlay/studio)와 완전히 분리된 관리자 전용 로그인(/admin/login) 및
  방송자 회원가입 승인/반려/정지, 초대 코드 관리, 비밀번호 변경, 보안 감사 로그를 제공합니다.
"""

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
import database
import security_core

admin_bp = Blueprint("admin_bp", __name__, url_prefix="/admin")


def _get_current_admin_user():
    uid = session.get("studio_user_id")
    if not uid:
        return None
    u = database.get_studio_user_by_id(uid)
    if not u or u.get("role") != "ADMIN" or u.get("status") != "APPROVED":
        return None
    return u


@admin_bp.route("", methods=["GET"])
@admin_bp.route("/", methods=["GET"])
def admin_dashboard():
    """통합 관리자 대시보드 메인 (/admin)"""
    admin_user = _get_current_admin_user()
    if not admin_user:
        return redirect(url_for("admin_bp.admin_login"))

    users = database.list_studio_users()
    pending_users = [u for u in users if u["status"] == "PENDING"]
    approved_users = [u for u in users if u["status"] == "APPROVED"]
    invite_codes = database.list_studio_invite_codes()
    audit_logs = database.list_security_audit_logs(limit=80)
    cloud_cfg = database.get_cloud_mall_config()
    products = database.get_all_products()

    return render_template(
        "admin/dashboard.html",
        admin_user=admin_user,
        users=users,
        pending_count=len(pending_users),
        approved_count=len(approved_users),
        invite_codes=invite_codes,
        active_invites_count=sum(1 for c in invite_codes if c.get("is_active")),
        audit_logs=audit_logs,
        cloud_cfg=cloud_cfg,
        products_count=len(products)
    )


@admin_bp.route("/login", methods=["GET", "POST"])
def admin_login():
    """관리자 전용 로그인 페이지 (/admin/login) — 성공 시 OBS 스튜디오가 아닌 /admin으로 직행"""
    if request.method == "GET":
        if _get_current_admin_user():
            return redirect(url_for("admin_bp.admin_dashboard"))
        return render_template("admin/login.html", error=None)

    ip_addr = str(request.remote_addr or "unknown")
    allowed, retry_sec = security_core.rate_limiter.check("admin_login", ip_addr, max_requests=10, window_sec=60.0)
    if not allowed:
        msg = f"로그인 시도가 너무 많습니다. {retry_sec}초 후 다시 시도해 주세요."
        if request.is_json:
            return jsonify({"status": "error", "message": msg}), 429
        return render_template("admin/login.html", error=msg), 429

    if request.is_json:
        data = request.get_json(silent=True) or {}
        login_id = data.get("login_id") or data.get("username") or data.get("email") or ""
        password = data.get("password") or ""
    else:
        if not security_core.verify_csrf_token(session, request):
            return render_template("admin/login.html", error="CSRF 보안 토큰이 만료되었습니다. 다시 시도해 주세요."), 403
        login_id = request.form.get("login_id") or ""
        password = request.form.get("password") or ""

    res = database.authenticate_studio_user(login_id, password, ip_address=ip_addr)
    if not res.get("ok"):
        status_code = 423 if res.get("code") == "LOCKED" else 401
        if request.is_json:
            return jsonify({"status": "error", "code": res.get("code"), "message": res.get("message")}), status_code
        return render_template("admin/login.html", error=res.get("message")), status_code

    user = res["user"]
    if user.get("role") != "ADMIN" or user.get("status") != "APPROVED":
        database.log_security_audit("ADMIN_LOGIN_DENIED", actor_id=user.get("username", ""), ip_address=ip_addr, detail="non-admin attempted /admin/login")
        err_msg = "관리자(ADMIN) 권한이 없는 계정입니다. 일반 방송자는 /auth/login을 이용해 주세요."
        if request.is_json:
            return jsonify({"status": "error", "code": "ADMIN_REQUIRED", "message": err_msg}), 403
        return render_template("admin/login.html", error=err_msg), 403

    security_core.rotate_session(session)
    session.permanent = True
    session["studio_user_id"] = user["id"]
    session["studio_role"] = "ADMIN"

    if request.is_json:
        return jsonify({
            "status": "ok",
            "user": user,
            "csrf_token": security_core.get_csrf_token(session),
            "redirect": "/admin"
        }), 200

    return redirect(url_for("admin_bp.admin_dashboard"))


@admin_bp.route("/logout", methods=["GET", "POST"])
def admin_logout():
    """관리자 로그아웃 (/admin/logout)"""
    session.clear()
    if request.is_json:
        return jsonify({"status": "ok"}), 200
    return redirect(url_for("admin_bp.admin_login"))


@admin_bp.route("/api/users/status", methods=["POST"])
def admin_api_user_status():
    """회원 가입 승인(APPROVED) / 반려(REJECTED) / 정지(SUSPENDED) / 대기(PENDING) 및 권한 변경"""
    admin_user = _get_current_admin_user()
    if not admin_user:
        return jsonify({"status": "error", "message": "관리자 권한이 필요합니다."}), 403

    data = request.get_json(silent=True) or {}
    uid = data.get("user_id")
    new_status = data.get("status")
    new_role = data.get("role")
    ok = database.update_studio_user_status(
        user_id=uid,
        new_status=new_status,
        new_role=new_role,
        actor_id=admin_user.get("username", "admin"),
        ip_address=str(request.remote_addr or "")
    )
    return jsonify({
        "status": "ok" if ok else "error",
        "message": f"회원 상태가 [{new_status}]로 변경되었습니다." if ok else "상태 변경에 실패했습니다."
    }), (200 if ok else 400)


@admin_bp.route("/api/users/create", methods=["POST"])
def admin_api_user_create():
    """관리자가 직접 방송자 또는 관리자 계정을 신규 생성"""
    admin_user = _get_current_admin_user()
    if not admin_user:
        return jsonify({"status": "error", "message": "관리자 권한이 필요합니다."}), 403

    data = request.get_json(silent=True) or {}
    res = database.create_studio_user_by_admin(
        username=data.get("username", ""),
        email=data.get("email", ""),
        password=data.get("password", ""),
        display_name=data.get("display_name", ""),
        phone=data.get("phone", ""),
        role=data.get("role", "BROADCASTER"),
        status=data.get("status", "APPROVED"),
        actor_id=admin_user.get("username", "admin"),
        ip_address=str(request.remote_addr or "")
    )
    return jsonify({"status": "ok" if res.get("ok") else "error", **res}), (200 if res.get("ok") else 400)


@admin_bp.route("/api/users/delete", methods=["POST"])
def admin_api_user_delete():
    """회원 삭제 (마지막 관리자 삭제 방지 포함)"""
    admin_user = _get_current_admin_user()
    if not admin_user:
        return jsonify({"status": "error", "message": "관리자 권한이 필요합니다."}), 403

    data = request.get_json(silent=True) or {}
    res = database.delete_studio_user(
        user_id=data.get("user_id"),
        actor_id=admin_user.get("username", "admin"),
        ip_address=str(request.remote_addr or "")
    )
    return jsonify({"status": "ok" if res.get("ok") else "error", **res}), (200 if res.get("ok") else 400)


@admin_bp.route("/api/users/reset-password", methods=["POST"])
def admin_api_user_reset_password():
    """특정 회원의 비밀번호를 관리자가 재설정"""
    admin_user = _get_current_admin_user()
    if not admin_user:
        return jsonify({"status": "error", "message": "관리자 권한이 필요합니다."}), 403

    data = request.get_json(silent=True) or {}
    res = database.change_studio_user_password(
        user_id=data.get("user_id"),
        new_password=data.get("new_password", ""),
        actor_id=admin_user.get("username", "admin"),
        ip_address=str(request.remote_addr or "")
    )
    return jsonify({"status": "ok" if res.get("ok") else "error", **res}), (200 if res.get("ok") else 400)


@admin_bp.route("/api/invite-codes", methods=["POST"])
def admin_api_invite_create():
    """즉시 승인용 초대 코드 생성"""
    admin_user = _get_current_admin_user()
    if not admin_user:
        return jsonify({"status": "error", "message": "관리자 권한이 필요합니다."}), 403

    data = request.get_json(silent=True) or {}
    res = database.create_studio_invite_code(
        code=data.get("code", ""),
        memo=data.get("memo", ""),
        max_uses=data.get("max_uses", 10),
        created_by=admin_user.get("username", "ADMIN")
    )
    return jsonify({"status": "ok" if res.get("ok") else "error", **res}), (200 if res.get("ok") else 400)


@admin_bp.route("/api/invite-codes/toggle", methods=["POST"])
def admin_api_invite_toggle():
    """초대 코드 활성/비활성 전환"""
    admin_user = _get_current_admin_user()
    if not admin_user:
        return jsonify({"status": "error", "message": "관리자 권한이 필요합니다."}), 403

    data = request.get_json(silent=True) or {}
    ok = database.toggle_studio_invite_code(data.get("code_id"), bool(data.get("is_active")))
    return jsonify({"status": "ok" if ok else "error"}), (200 if ok else 400)


@admin_bp.route("/api/change-password", methods=["POST"])
def admin_api_change_own_password():
    """관리자 본인 비밀번호 변경"""
    admin_user = _get_current_admin_user()
    if not admin_user:
        return jsonify({"status": "error", "message": "관리자 권한이 필요합니다."}), 403

    data = request.get_json(silent=True) or {}
    new_pw = data.get("new_password", "")
    res = database.change_studio_user_password(
        user_id=admin_user["id"],
        new_password=new_pw,
        actor_id=admin_user.get("username", "admin"),
        ip_address=str(request.remote_addr or "")
    )
    return jsonify({"status": "ok" if res.get("ok") else "error", **res}), (200 if res.get("ok") else 400)
