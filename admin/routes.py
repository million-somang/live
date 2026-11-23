# -*- coding: utf-8 -*-
"""
독립 관리자 페이지 라우트 블루프린트 (admin/routes.py)
- URL Prefix: /admin
- 일반 OBS 스튜디오(/overlay/studio)와 완전히 분리된 관리자 전용 로그인(/admin/login) 및
  방송자 회원가입 승인/반려/정지, 초대 코드 관리, 비밀번호 변경, 보안 감사 로그를 제공합니다.
"""

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, current_app, make_response
import os
import sys
import secrets
from urllib.parse import urlencode
import database
import security_core

_MALL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "shopping_mall"))
if os.path.isdir(_MALL_DIR) and _MALL_DIR not in sys.path:
    sys.path.append(_MALL_DIR)
try:
    import mall_database
except Exception:
    mall_database = None

admin_bp = Blueprint("admin_bp", __name__, url_prefix="/admin")


def _get_current_admin_user():
    uid = session.get("studio_user_id")
    if not uid:
        return None
    u = database.get_studio_user_by_id(uid)
    if not u or u.get("role") != "ADMIN" or u.get("status") != "APPROVED":
        return None
    return u


def _load_admin_orders_bundle():
    """통합 관리자 페이지(/admin)에서 쇼핑몰 웹 구매 내역을 조회합니다 (로컬 DB + HTTP API 폴백 지원)."""
    mall_orders = []
    if mall_database is not None:
        try:
            mall_orders = mall_database.get_mall_orders(limit=100)
        except Exception:
            mall_orders = []

    if not mall_orders:
        try:
            import json
            import urllib.request
            cfg = database.get_cloud_mall_config()
            target_url = (cfg.get("mall_url") or "http://127.0.0.1:6001").rstrip("/")
            headers = {"Accept": "application/json"}
            if cfg.get("sync_secret"):
                headers["X-Mall-Sync-Secret"] = cfg["sync_secret"]
            req = urllib.request.Request(
                f"{target_url}/api/sync/pending-orders?after_id=0&limit=100",
                headers=headers,
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                if isinstance(data, dict) and data.get("status") == "ok":
                    fetched = data.get("orders") or []
                    for o in fetched:
                        if isinstance(o, dict) and "order_items" not in o:
                            o["order_items"] = o.get("items") or []
                    mall_orders = list(reversed(fetched))
        except Exception:
            pass

    mall_rev = sum(int(o.get("total_amount") or 0) for o in mall_orders if str(o.get("order_status") or "") != "주문취소")
    return {
        "mall_orders": mall_orders,
        "mall_orders_count": len(mall_orders),
        "total_orders_count": len(mall_orders),
        "total_orders_revenue": mall_rev,
    }


@admin_bp.route("", methods=["GET"])
@admin_bp.route("/", methods=["GET"])
@admin_bp.route("/traffic", methods=["GET"])
@admin_bp.route("/orders", methods=["GET"])
def admin_dashboard():
    """통합 관리자 대시보드 메인 (/admin, /admin/orders, /admin/traffic)"""
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
    if request.path.endswith("/traffic"):
        initial_tab = "traffic"
    elif request.path.endswith("/orders"):
        initial_tab = "orders"
    else:
        initial_tab = request.args.get("tab") or "orders"
    traffic_snapshot = security_core.traffic_monitor.get_snapshot()
    orders_bundle = _load_admin_orders_bundle()

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
        products=products,
        products_count=len(products),
        initial_tab=initial_tab,
        traffic_snapshot=traffic_snapshot,
        **orders_bundle,
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
        resp = make_response(jsonify({"status": "ok"}), 200)
    else:
        resp = make_response(redirect(url_for("admin_bp.admin_login")))
    resp.delete_cookie("mall_sid", path="/")
    return resp


def _get_mall_sso_secret() -> str:
    env_sec = (os.getenv("MALL_SYNC_SECRET") or "").strip()
    if env_sec:
        return env_sec
    try:
        cfg = database.get_cloud_mall_config() or {}
        cfg_sec = str(cfg.get("sync_secret") or "").strip()
        if cfg_sec:
            return cfg_sec
    except Exception:
        pass
    return str(current_app.secret_key or "").strip()


def _resolve_mall_public_url_for_sso() -> str:
    mall_port = int(os.getenv("MALL_PORT", 6001))
    raw_env_pub = (os.getenv("MALL_PUBLIC_URL") or "").strip().rstrip("/")
    if raw_env_pub and not any(h in raw_env_pub for h in ("localhost", "127.0.0.1")):
        return database._normalize_mall_port_url(raw_env_pub, mall_port)
    try:
        cfg = database.get_cloud_mall_config() or {}
        if cfg.get("is_remote") and cfg.get("mall_url"):
            return database._normalize_mall_port_url(str(cfg["mall_url"]), mall_port)
    except Exception:
        pass
    try:
        fwd_host = (request.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
        raw_host = fwd_host or (request.host or "localhost")
        if raw_host.startswith("[") and "]" in raw_host:
            hostname = raw_host[:raw_host.index("]") + 1]
        else:
            hostname = raw_host.split(":")[0].strip() or "localhost"
        scheme = (request.headers.get("X-Forwarded-Proto") or request.scheme or "http").split(",")[0].strip()
        return f"{scheme}://{hostname}:{mall_port}"
    except Exception:
        return f"http://localhost:{mall_port}"


@admin_bp.route("/mall-sso", methods=["GET"])
def admin_mall_sso():
    """최고 관리자(Studio ADMIN)를 독립 쇼핑몰 관리자(:6001/admin)로 무로그인 SSO 연결"""
    admin_user = _get_current_admin_user()
    if not admin_user:
        return redirect(url_for("admin_bp.admin_login"))

    next_path = (request.args.get("next") or "/admin").strip()
    if not security_core.is_safe_redirect_url(next_path):
        next_path = "/admin"

    sso_secret = _get_mall_sso_secret()
    token = security_core.create_signed_token(
        sso_secret,
        "mall_admin_sso",
        {
            "studio_user_id": int(admin_user["id"]),
            "username": str(admin_user.get("username") or ""),
            "display_name": str(admin_user.get("display_name") or admin_user.get("username") or "최고 관리자"),
            "jti": secrets.token_hex(12),
        },
        ttl_sec=60,
    )
    mall_base = _resolve_mall_public_url_for_sso().rstrip("/")
    return redirect(f"{mall_base}/auth/sso?{urlencode({'token': token, 'next': next_path})}")


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


@admin_bp.route("/api/traffic", methods=["GET"])
def admin_api_traffic_snapshot():
    """관리자 전용: 실시간 서버 인입/송출 트래픽 관제 스냅샷 조회"""
    admin_user = _get_current_admin_user()
    if not admin_user:
        return jsonify({"status": "error", "message": "관리자 권한이 필요합니다."}), 403
    return jsonify(security_core.traffic_monitor.get_snapshot()), 200


@admin_bp.route("/api/traffic/reset", methods=["POST"])
def admin_api_traffic_reset():
    """관리자 전용: 실시간 서버 트래픽 누적 카운터 초기화"""
    admin_user = _get_current_admin_user()
    if not admin_user:
        return jsonify({"status": "error", "message": "관리자 권한이 필요합니다."}), 403
    security_core.traffic_monitor.reset()
    database.log_security_audit(
        "ADMIN_TRAFFIC_RESET",
        actor_id=admin_user.get("username", "admin"),
        ip_address=str(request.remote_addr or ""),
        detail="관리자가 서버 트래픽 통계 카운터를 초기화함",
    )
    return jsonify(security_core.traffic_monitor.get_snapshot()), 200


@admin_bp.route("/api/mall-orders/status", methods=["POST"])
def admin_api_mall_order_status():
    """통합 관리자 페이지(/admin)에서 쇼핑몰 주문의 입금상태/배송상태를 즉시 변경"""
    admin_user = _get_current_admin_user()
    if not admin_user:
        return jsonify({"status": "error", "message": "관리자 권한이 필요합니다."}), 403
    if mall_database is None:
        return jsonify({"status": "error", "message": "쇼핑몰 DB 모듈을 불러올 수 없습니다."}), 500
    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id")
    if not order_id:
        return jsonify({"status": "error", "message": "order_id가 필요합니다."}), 400
    ok = mall_database.update_mall_order_status(
        order_id=order_id,
        payment_status=data.get("payment_status"),
        order_status=data.get("order_status"),
    )
    return jsonify({"status": "ok" if ok else "error"}), (200 if ok else 400)
