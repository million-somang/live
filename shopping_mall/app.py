# -*- coding: utf-8 -*-
"""
OBS 실시간 연동 독립형 쇼핑몰 서버 (shopping_mall/app.py)
- 기본 포트: 3000번 (http://0.0.0.0:3000)
- 별도의 SQLite DB(shopping_mall.db)를 사용하며, OBS 스튜디오(Port 5000)와
  상품/옵션/재고 및 라이브 선택(Pin) 상품을 실시간 동기화합니다.
- 이 폴더(shopping_mall)만 단독으로 Git에 푸시하여 클라우드/외부 서버에 배포할 수 있습니다.
"""

import os
import sys
import io
import json
import time
import base64
import shutil
import threading
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from flask import Flask, Response, request, jsonify, render_template, send_from_directory, redirect, url_for, session
from flask_cors import CORS

import mall_database
import mall_security

# 윈도우 콘솔 UTF-8 출력 보장
if sys.platform.startswith("win"):
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_PRODUCTS_DIR = os.path.join(BASE_DIR, "static", "uploads", "products")
os.makedirs(UPLOAD_PRODUCTS_DIR, exist_ok=True)

OBS_SERVER_URL = os.getenv("OBS_SERVER_URL", "http://127.0.0.1:5000").rstrip("/")
STUDIO_PUBLIC_URL = os.getenv("STUDIO_PUBLIC_URL", "").rstrip("/")
MALL_PUBLIC_URL = os.getenv("MALL_PUBLIC_URL", "").rstrip("/")
MALL_PORT = int(os.getenv("MALL_PORT", os.getenv("PORT", 6001)))
STUDIO_PORT = int(os.getenv("STUDIO_PORT", os.getenv("SERVER_PORT", 5000)))
MALL_SYNC_SECRET = os.getenv("MALL_SYNC_SECRET", "")


def _extract_mall_request_hostname_and_scheme():
    """현재 쇼핑몰에 접속한 브라우저의 프로토콜과 서버 호스트명(IP 또는 도메인)을 동적으로 추출합니다."""
    try:
        fwd_host = (request.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
        raw_host = fwd_host or (request.host or "localhost")
        if raw_host.startswith("[") and "]" in raw_host:
            hostname = raw_host[:raw_host.index("]") + 1]
        else:
            hostname = raw_host.split(":")[0].strip() or "localhost"
        scheme = (request.headers.get("X-Forwarded-Proto") or request.scheme or "http").split(",")[0].strip()
        return scheme, hostname
    except Exception:
        return "http", "localhost"


def resolve_mall_public_url() -> str:
    port = int(os.getenv("MALL_PORT", os.getenv("PORT", MALL_PORT)))
    raw_env = os.getenv("MALL_PUBLIC_URL", MALL_PUBLIC_URL).strip().rstrip("/")
    if raw_env and not any(h in raw_env for h in ("localhost", "127.0.0.1")):
        return raw_env
    scheme, hostname = _extract_mall_request_hostname_and_scheme()
    return f"{scheme}://{hostname}:{port}"


def resolve_studio_public_url() -> str:
    port = int(os.getenv("STUDIO_PORT", os.getenv("SERVER_PORT", STUDIO_PORT)))
    raw_env = os.getenv("STUDIO_PUBLIC_URL", STUDIO_PUBLIC_URL).strip().rstrip("/")
    if raw_env and not any(h in raw_env for h in ("localhost", "127.0.0.1")):
        return raw_env
    scheme, hostname = _extract_mall_request_hostname_and_scheme()
    return f"{scheme}://{hostname}:{port}"


def _get_sync_secret():
    return os.getenv("MALL_SYNC_SECRET", MALL_SYNC_SECRET).strip()


def _is_obs_pull_enabled():
    """클라우드 단독 운영 모드(MALL_MODE=cloud 또는 OBS_PULL_ENABLED=0)일 때는 역방향 OBS HTTP Pull을 수행하지 않습니다."""
    if os.getenv("MALL_MODE", "").strip().lower() == "cloud":
        return False
    if os.getenv("OBS_PULL_ENABLED", "1").strip() in ("0", "false", "False", "no"):
        return False
    return bool(OBS_SERVER_URL)


def _verify_sync_secret(data=None):
    sec = _get_sync_secret()
    if not sec:
        if os.getenv("MALL_MODE", "").strip().lower() == "cloud":
            return False
        return True
    req_sec = (
        request.headers.get("X-Mall-Sync-Secret", "")
        or ((data or {}).get("secret", "") if isinstance(data, dict) else "")
    )
    return mall_security.constant_time_equals(sec, req_sec)


app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static")
)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.secret_key = mall_security.load_or_create_secret_key(
    "MALL_SECRET_KEY",
    os.path.join(BASE_DIR, ".mall_secret_key")
)
app.config["SESSION_COOKIE_NAME"] = "mall_sid"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
CORS(app)

mall_database.init_mall_db()


def get_current_mall_user():
    uid = session.get("mall_user_id")
    if not uid:
        return None
    return mall_database.get_mall_user_by_id(uid)


def create_live_order_token() -> str:
    return mall_security.create_signed_token(app.secret_key, "live_order", {"page": "LIVE"}, ttl_sec=86400)


@app.context_processor
def _inject_mall_globals():
    u = get_current_mall_user()
    return {
        "current_mall_user": u,
        "csrf_token": mall_security.get_csrf_token(session),
        "studio_public_url": resolve_studio_public_url(),
        "mall_public_url": resolve_mall_public_url(),
        "live_order_token": create_live_order_token()
    }


@app.before_request
def _mall_security_gateway():
    if request.method == "OPTIONS":
        return None
    path = request.path or "/"

    # 어드민 페이지 및 어드민 API 보호 (/admin, /api/admin/*)
    if path == "/admin" or path.startswith("/api/admin/"):
        u = get_current_mall_user()
        if not u:
            if path.startswith("/api/"):
                return jsonify({"status": "error", "code": "AUTH_REQUIRED", "message": "쇼핑몰 관리자 로그인이 필요합니다."}), 401
            return redirect(url_for("mall_auth_login", next="/admin"))
        if u.get("role") != "ADMIN":
            if path.startswith("/api/"):
                return jsonify({"status": "error", "code": "ADMIN_REQUIRED", "message": "쇼핑몰 관리자(ADMIN) 권한이 필요합니다."}), 403
            return Response("쇼핑몰 관리자 전용 페이지입니다.", status=403, mimetype="text/plain; charset=utf-8")
        if request.method in ("POST", "PUT", "DELETE", "PATCH") and not mall_security.verify_csrf_token(session, request):
            return jsonify({"status": "error", "code": "CSRF_INVALID", "message": "CSRF 보안 토큰 검증에 실패했습니다."}), 403
    return None


@app.after_request
def _mall_security_after_request(response):
    mall_security.apply_security_headers(response, allow_framing=False)
    mall_security.inject_csrf_into_html_response(response, mall_security.get_csrf_token(session))
    return response

_last_obs_poll_ts = 0.0


def _copy_or_download_product_image(image_url, image_b64=None):
    """
    OBS 상품 이미지를 쇼핑몰 자체 static/uploads/products 폴더로 복제/저장합니다.
    (외부 서버 단독 배포 시에는 base64 또는 HTTP 다운로드, 로컬 동시 개발 시에는 파일 복사 지원)
    """
    if not image_url or not str(image_url).strip():
        return ""
    image_url = str(image_url).strip()
    if image_url.startswith("http://") or image_url.startswith("https://"):
        return image_url

    filename = os.path.basename(image_url.split("?")[0])
    if not filename:
        return image_url

    dest_path = os.path.join(UPLOAD_PRODUCTS_DIR, filename)
    if image_b64:
        try:
            raw = base64.b64decode(image_b64)
            with open(dest_path, "wb") as f:
                f.write(raw)
            return f"/static/uploads/products/{filename}"
        except Exception:
            pass

    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        return f"/static/uploads/products/{filename}"

    # 1) 동일 서버 내 상위 폴더(c:\live\static\uploads\products)가 존재하는 경우 우선 복사 (클라우드 모드/동일 호스트 겸용)
    parent_upload_candidate = os.path.abspath(os.path.join(BASE_DIR, "..", "static", "uploads", "products", filename))
    if os.path.exists(parent_upload_candidate):
        try:
            shutil.copy2(parent_upload_candidate, dest_path)
            return f"/static/uploads/products/{filename}"
        except Exception:
            pass

    if not _is_obs_pull_enabled():
        return f"/static/uploads/products/{filename}"

    # 2) OBS 서버에서 HTTP로 이미지 다운로드
    if OBS_SERVER_URL:
        try:
            req = urllib.request.Request(
                f"{OBS_SERVER_URL}/static/uploads/products/{filename}",
                headers={"User-Agent": "ShoppingMallSync/1.0"}
            )
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = resp.read()
                if data:
                    with open(dest_path, "wb") as f:
                        f.write(data)
                    return f"/static/uploads/products/{filename}"
        except Exception:
            pass

    return f"/static/uploads/products/{filename}"


def _sync_product_detail_images(prod_dict, images_b64=None):
    """상품의 detail_images 배열에 포함된 로컬/원격 이미지를 쇼핑몰 저장소로 동기화합니다."""
    if not isinstance(prod_dict, dict) or "detail_images" not in prod_dict:
        return prod_dict
    raw_imgs = mall_database._parse_detail_images(prod_dict.get("detail_images"))
    b64_map = images_b64 if isinstance(images_b64, dict) else {}
    resolved = []
    for img_u in raw_imgs:
        fname = os.path.basename(str(img_u).split("?")[0])
        b64_data = b64_map.get(fname) if fname else None
        copied = _copy_or_download_product_image(img_u, image_b64=b64_data)
        if copied:
            resolved.append(copied)
    prod_dict["detail_images"] = resolved
    return prod_dict


def pull_sync_from_obs_server(force=False):
    """
    OBS 서버(OBS_SERVER_URL)의 최신 상품 목록 및 라이브 상태(현재 선택된 상품 번호, 방송중 여부, 타이머, 안내 자막)를
    조회하여 쇼핑몰 DB(shopping_mall.db)에 실시간 반영합니다.
    (클라우드 단독 모드에서는 로컬 에이전트의 Outbound Push만 수신하므로 즉시 반환합니다.)
    """
    if not _is_obs_pull_enabled():
        return False

    global _last_obs_poll_ts
    now = time.time()
    if not force and (now - _last_obs_poll_ts) < 0.8:
        return False
    _last_obs_poll_ts = now

    synced_any = False

    # 1. OBS 서버 HTTP API 조회 시도
    try:
        req_ov = urllib.request.Request(
            f"{OBS_SERVER_URL}/api/overlay/data",
            headers={"User-Agent": "ShoppingMallLivePoll/1.0"}
        )
        with urllib.request.urlopen(req_ov, timeout=1.2) as resp:
            ov_data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if isinstance(ov_data, dict) and ov_data.get("status") == "ok":
                pinned_prod = ov_data.get("pinned_product")
                pinned_num = pinned_prod.get("num") if isinstance(pinned_prod, dict) else None
                yt_rtmp = ov_data.get("youtube_rtmp") or {}
                tt_rtmp = ov_data.get("tiktok_rtmp") or {}
                ig_rtmp = ov_data.get("instagram_rtmp") or {}
                is_live = bool(yt_rtmp.get("active") or tt_rtmp.get("active") or ig_rtmp.get("active"))

                if isinstance(pinned_prod, dict):
                    pinned_prod["image_url"] = _copy_or_download_product_image(pinned_prod.get("image_url"))
                    _sync_product_detail_images(pinned_prod)
                    mall_database.upsert_product_from_obs(pinned_prod)

                live_urls_data = ov_data.get("live_urls") or {}
                yt_emb = ""
                if isinstance(live_urls_data.get("YOUTUBE"), dict):
                    yt_emb = live_urls_data["YOUTUBE"].get("embed_url") or ""
                mall_database.update_live_state_bundle({
                    "is_live": is_live,
                    "pinned_product_num": pinned_num,
                    "ticker_message": ov_data.get("latest_reply") or "",
                    "timer": ov_data.get("timer") or {"active": False},
                    "live_urls": live_urls_data,
                    "youtube_embed_url": yt_emb,
                    "obs_server_url": OBS_SERVER_URL
                })
                synced_any = True
    except Exception:
        pass

    # 상품 전체 목록 동기화 (강제 요청이거나 쇼핑몰 DB가 비어있을 때)
    if force or len(mall_database.get_all_mall_products()) == 0:
        try:
            req_pr = urllib.request.Request(
                f"{OBS_SERVER_URL}/api/products",
                headers={"User-Agent": "ShoppingMallProductSync/1.0"}
            )
            with urllib.request.urlopen(req_pr, timeout=2.0) as resp:
                pr_data = json.loads(resp.read().decode("utf-8", errors="replace"))
                prods = pr_data.get("products") or pr_data.get("items") or []
                for p in prods:
                    if isinstance(p, dict):
                        p["image_url"] = _copy_or_download_product_image(p.get("image_url"))
                        _sync_product_detail_images(p)
                mall_database.bulk_sync_products_from_obs(prods, delete_missing=True)
                synced_any = True
        except Exception:
            pass

    return synced_any


# 초기 기동 시 OBS 상품 동기화 1회 수행
try:
    pull_sync_from_obs_server(force=True)
except Exception:
    pass


# ==============================================================================
# 🌐 정적 업로드 이미지 폴백 라우트
# ==============================================================================
@app.route("/static/uploads/products/<path:filename>")
def serve_product_image(filename):
    safe_name = os.path.basename(filename)
    local_path = os.path.join(UPLOAD_PRODUCTS_DIR, safe_name)
    if not os.path.exists(local_path):
        _copy_or_download_product_image(f"/static/uploads/products/{safe_name}")
    return send_from_directory(UPLOAD_PRODUCTS_DIR, safe_name)


# ==============================================================================
# 🖥️ 쇼핑몰 페이지 라우트 (Port 3000)
# ==============================================================================

@app.route("/", methods=["GET"])
def mall_home():
    """쇼핑몰 메인 홈 페이지"""
    pull_sync_from_obs_server(force=False)
    category = request.args.get("category", "ALL").upper()
    search = request.args.get("q", "").strip()
    sort = request.args.get("sort", "num_asc")
    products = mall_database.get_all_mall_products(category=category, search=search, sort=sort)
    live_state = mall_database.get_full_live_state()
    return render_template(
        "index.html",
        products=products,
        live_state=live_state,
        current_category=category,
        search_query=search,
        current_sort=sort,
        active_nav="home"
    )


@app.route("/live", methods=["GET"])
def mall_live_commerce():
    """
    🔴 라이브 커머스 전용 페이지 (/live)
    - 브라우저 주소(/live)를 바꾸지 않고, OBS에서 선택(고정)된 상품의 상세 화면이
      실시간으로 이 페이지 내부에서 즉시 전환됩니다.
    """
    pull_sync_from_obs_server(force=True)
    live_state = mall_database.get_full_live_state()
    return render_template(
        "live_commerce.html",
        live_state=live_state,
        selected_product=live_state.get("selected_product"),
        all_products=live_state.get("all_products", []),
        active_nav="live"
    )


@app.route("/product/<int:num>", methods=["GET"])
def mall_product_detail(num):
    """일반 쇼핑몰 개별 상품 상세 페이지"""
    pull_sync_from_obs_server(force=False)
    product = mall_database.get_mall_product_by_num(num)
    if not product:
        return redirect(url_for("mall_home"))
    reviews = mall_database.get_mall_reviews(num)
    review_summary = mall_database.get_mall_review_summary(num)
    related = [p for p in mall_database.get_all_mall_products() if p["num"] != num][:4]
    live_state = mall_database.get_full_live_state()
    return render_template(
        "product_detail.html",
        product=product,
        reviews=reviews,
        review_summary=review_summary,
        related_products=related,
        live_state=live_state,
        active_nav="shop"
    )


@app.route("/cart", methods=["GET"])
def mall_cart():
    """장바구니 페이지"""
    pull_sync_from_obs_server(force=False)
    live_state = mall_database.get_full_live_state()
    return render_template("cart.html", live_state=live_state, active_nav="cart")


@app.route("/auth/login", methods=["GET", "POST"])
def mall_auth_login():
    """쇼핑몰 회원 로그인"""
    next_url = request.args.get("next") or request.form.get("next") or "/"
    if not mall_security.is_safe_redirect_url(next_url):
        next_url = "/"

    if request.method == "GET":
        if get_current_mall_user():
            return redirect(next_url)
        live_state = mall_database.get_full_live_state()
        return render_template("auth_login.html", live_state=live_state, next_url=next_url)

    ip_addr = str(request.remote_addr or "unknown")
    allowed, retry_sec = mall_security.rate_limiter.check("mall_login", ip_addr, max_requests=10, window_sec=60.0)
    if not allowed:
        live_state = mall_database.get_full_live_state()
        return render_template(
            "auth_login.html",
            live_state=live_state,
            next_url=next_url,
            error=f"로그인 시도가 너무 많습니다. {retry_sec}초 후 다시 시도해 주세요."
        ), 429

    if request.is_json:
        data = request.get_json(silent=True) or {}
        email = data.get("email", "")
        password = data.get("password", "")
    else:
        if not mall_security.verify_csrf_token(session, request):
            live_state = mall_database.get_full_live_state()
            return render_template("auth_login.html", live_state=live_state, next_url=next_url, error="보안 토큰(CSRF)이 만료되었습니다. 다시 시도해 주세요."), 403
        email = request.form.get("email", "")
        password = request.form.get("password", "")

    res = mall_database.authenticate_mall_user(email, password)
    if not res.get("ok"):
        status_code = 423 if res.get("code") == "LOCKED" else 401
        if request.is_json:
            return jsonify({"status": "error", "code": res.get("code"), "message": res.get("message")}), status_code
        live_state = mall_database.get_full_live_state()
        return render_template("auth_login.html", live_state=live_state, next_url=next_url, error=res.get("message")), status_code

    u = res["user"]
    mall_security.rotate_session(session)
    session.permanent = True
    session["mall_user_id"] = u["id"]
    session["mall_role"] = u["role"]

    if request.is_json:
        return jsonify({"status": "ok", "user": u, "csrf_token": mall_security.get_csrf_token(session), "redirect": next_url}), 200
    return redirect(next_url)


@app.route("/auth/register", methods=["GET", "POST"])
def mall_auth_register():
    """쇼핑몰 회원가입 (라이브 비회원 구매 후 간편 가입 프리필 지원)"""
    next_url = request.args.get("next") or request.form.get("next") or "/"
    if not mall_security.is_safe_redirect_url(next_url):
        next_url = "/"

    if request.method == "GET":
        prefill = {
            "email": request.args.get("email", ""),
            "name": request.args.get("name", ""),
            "phone": request.args.get("phone", ""),
            "address": request.args.get("address", ""),
            "address_detail": request.args.get("address_detail", "")
        }
        live_state = mall_database.get_full_live_state()
        return render_template("auth_register.html", live_state=live_state, next_url=next_url, prefill=prefill)

    ip_addr = str(request.remote_addr or "unknown")
    allowed, retry_sec = mall_security.rate_limiter.check("mall_register", ip_addr, max_requests=8, window_sec=60.0)
    if not allowed:
        live_state = mall_database.get_full_live_state()
        return render_template(
            "auth_register.html",
            live_state=live_state,
            next_url=next_url,
            prefill={},
            error=f"가입 요청이 너무 많습니다. {retry_sec}초 후 다시 시도해 주세요."
        ), 429

    if request.is_json:
        form_data = request.get_json(silent=True) or {}
    else:
        if not mall_security.verify_csrf_token(session, request):
            live_state = mall_database.get_full_live_state()
            return render_template("auth_register.html", live_state=live_state, next_url=next_url, prefill={}, error="CSRF 보안 토큰이 만료되었습니다."), 403
        form_data = {
            "email": request.form.get("email", ""),
            "password": request.form.get("password", ""),
            "name": request.form.get("name", ""),
            "phone": request.form.get("phone", ""),
            "zipcode": request.form.get("zipcode", ""),
            "address": request.form.get("address", ""),
            "address_detail": request.form.get("address_detail", "")
        }

    res = mall_database.create_mall_user(
        email=form_data.get("email", ""),
        password=form_data.get("password", ""),
        name=form_data.get("name", ""),
        phone=form_data.get("phone", ""),
        zipcode=form_data.get("zipcode", ""),
        address=form_data.get("address", ""),
        address_detail=form_data.get("address_detail", "")
    )
    if not res.get("ok"):
        if request.is_json:
            return jsonify({"status": "error", "message": res.get("message")}), 400
        live_state = mall_database.get_full_live_state()
        return render_template("auth_register.html", live_state=live_state, next_url=next_url, prefill=form_data, error=res.get("message")), 400

    u = res["user"]
    mall_security.rotate_session(session)
    session.permanent = True
    session["mall_user_id"] = u["id"]
    session["mall_role"] = u["role"]

    if request.is_json:
        return jsonify({"status": "ok", "user": u, "csrf_token": mall_security.get_csrf_token(session), "redirect": next_url}), 200
    return redirect(next_url)


@app.route("/auth/logout", methods=["GET", "POST"])
def mall_auth_logout():
    session.clear()
    if request.is_json:
        return jsonify({"status": "ok"}), 200
    return redirect(url_for("mall_home"))


@app.route("/checkout", methods=["GET"])
def mall_checkout():
    """주문/결제 작성 페이지 (회원 및 비회원 모두 구매 가능)"""
    pull_sync_from_obs_server(force=False)
    live_state = mall_database.get_full_live_state()
    return render_template("checkout.html", live_state=live_state, active_nav="cart")


@app.route("/orders", methods=["GET", "POST"])
def mall_orders_view():
    """
    주문/배송 조회 페이지
    - 회원: 본인(user_id)의 주문 내역만 노출
    - 비회원: 기본 목록 0건 (타인 개인정보 노출 원천 차단), 주문번호+연락처+비밀번호 3중 일치 시에만 단건 표시
      (단, 방금 동일 브라우저 세션에서 주문을 완료해 리다이렉트된 직후에는 해당 주문을 1회 즉시 표시)
    """
    user = get_current_mall_user()
    order_code = (request.values.get("order_code") or "").strip()
    buyer_phone = (request.values.get("buyer_phone") or "").strip()
    guest_password = (request.values.get("guest_password") or "").strip()
    orders = []
    lookup_error = ""

    if order_code or buyer_phone or guest_password:
        ip_addr = str(request.remote_addr or "unknown")
        allowed, retry_sec = mall_security.rate_limiter.check("guest_order_lookup", ip_addr, max_requests=10, window_sec=60.0)
        if not allowed:
            lookup_error = f"조회 시도가 너무 많습니다. {retry_sec}초 후 다시 시도해 주세요."
        elif not (order_code and buyer_phone and guest_password):
            if user and request.method == "GET" and not (buyer_phone or guest_password):
                orders = mall_database.get_mall_orders_by_user(user["id"], limit=50)
            else:
                lookup_error = "비회원 주문 조회를 위해 주문번호, 휴대폰 번호, 주문조회 비밀번호 3가지를 모두 입력해 주세요."
        else:
            orders = mall_database.lookup_guest_mall_order(order_code, buyer_phone, guest_password)
            if not orders:
                lookup_error = "일치하는 주문 정보가 없거나 비밀번호가 올바르지 않습니다."
    elif user:
        orders = mall_database.get_mall_orders_by_user(user["id"], limit=50)
    else:
        recent_guest_code = str(session.get("mall_recent_guest_order_code") or "").strip()
        if recent_guest_code:
            orders = mall_database.get_mall_order_by_code(recent_guest_code)
            if orders:
                order_code = orders[0].get("order_code", "")
                buyer_phone = orders[0].get("buyer_phone", "")

    live_state = mall_database.get_full_live_state()
    return render_template(
        "orders.html",
        orders=orders,
        lookup_error=lookup_error,
        order_code=order_code,
        buyer_phone=buyer_phone,
        live_state=live_state,
        active_nav="orders"
    )


@app.route("/admin", methods=["GET"])
def mall_admin_view():
    """쇼핑몰 관리자 페이지 (주문 관리 & OBS 상품 동기화 현황)"""
    pull_sync_from_obs_server(force=False)
    products = mall_database.get_all_mall_products()
    orders = mall_database.get_mall_orders(limit=100)
    live_state = mall_database.get_full_live_state()
    return render_template(
        "admin.html",
        products=products,
        orders=orders,
        live_state=live_state,
        active_nav="admin"
    )


# ==============================================================================
# 🔄 OBS ↔ 쇼핑몰 실시간 연동 REST API
# ==============================================================================

@app.route("/api/catalog", methods=["GET"])
def api_mall_catalog():
    """CSR 초기 로드 및 카탈로그 버전 변경 시 전체 상품 목록을 반환하는 캐시 친화 API (ETag 304 지원)"""
    pull_sync_from_obs_server(force=False)
    lite = mall_database.get_lite_live_state()
    cat_ver = lite.get("catalog_version", 1)
    etag = f'"cat-{cat_ver}"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag})

    category = request.args.get("category", "ALL")
    search = request.args.get("q", "")
    sort = request.args.get("sort", "num_asc")
    prods = mall_database.get_all_mall_products(category=category, search=search, sort=sort)
    resp = jsonify({
        "status": "ok",
        "catalog_version": cat_ver,
        "state_version": lite.get("state_version", 1),
        "products": prods,
        "total_count": len(prods)
    })
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "no-cache"
    return resp, 200


@app.route("/api/live/state-lite", methods=["GET"])
def api_mall_live_state_lite():
    """
    CSR 초경량 실시간 상태 API (~300~800B)
    - 전체 상품 배열(all_products)을 제외하고 변경 시에만 바디를 내려보냅니다 (If-None-Match ETag -> 304 Not Modified).
    """
    pull_sync_from_obs_server(force=False)
    state = mall_database.get_lite_live_state()
    etag = f'"st-{state.get("state_version", 1)}"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag})

    if request.args.get("reviews") == "1" and state.get("selected_product"):
        sel_num = state["selected_product"]["num"]
        state["selected_product_reviews"] = mall_database.get_mall_reviews(sel_num)
        state["selected_product_review_summary"] = mall_database.get_mall_review_summary(sel_num)
    state["status"] = "ok"
    resp = jsonify(state)
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "no-cache"
    return resp, 200


@app.route("/api/live/stream", methods=["GET"])
def api_mall_live_sse_stream():
    """
    CSR 실시간 상태 구독용 Server-Sent Events (SSE) 스트림
    - state_version 변경 또는 하트비트 만료가 발생할 때만 즉시 푸시하여 불필요한 1초 폴링 트래픽을 제거합니다.
    """
    def _event_gen():
        last_sent_ver = -1
        while True:
            pull_sync_from_obs_server(force=False)
            state = mall_database.get_lite_live_state()
            cur_ver = state.get("state_version", 1)
            if cur_ver != last_sent_ver:
                if state.get("selected_product"):
                    sel_num = state["selected_product"]["num"]
                    state["selected_product_reviews"] = mall_database.get_mall_reviews(sel_num)
                    state["selected_product_review_summary"] = mall_database.get_mall_review_summary(sel_num)
                state["status"] = "ok"
                payload = json.dumps(state, ensure_ascii=False)
                yield f"id: {cur_ver}\nevent: live_state\ndata: {payload}\n\n"
                last_sent_ver = cur_ver
            else:
                yield ": ping\n\n"

            with mall_database.mall_state_cond:
                mall_database.mall_state_cond.wait(timeout=12.0)

    return Response(
        _event_gen(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@app.route("/api/live/state", methods=["GET"])
def api_mall_live_state():
    """
    /live 페이지 호환용 전체 실시간 상태 API
    """
    pull_sync_from_obs_server(force=False)
    state = mall_database.get_full_live_state()
    sel = state.get("selected_product")
    reviews = mall_database.get_mall_reviews(sel["num"]) if sel else []
    state["selected_product_reviews"] = reviews
    state["selected_product_review_summary"] = (
        mall_database.get_mall_review_summary(sel["num"]) if sel else {"count": 0, "avg_rating": 0.0}
    )
    state["status"] = "ok"
    return jsonify(state), 200


@app.route("/api/sync/from-obs", methods=["POST"])
def api_sync_from_obs():
    """
    방송자 PC 로컬 라이브 에이전트(live_commerce_server.py / database.py)에서
    상품 추가/수정/삭제, 핀 고정 변경, 방송 시작/종료 및 하트비트를 Outbound 푸시받는 엔드포인트
    """
    data = request.get_json(silent=True) or {}
    if not _verify_sync_secret(data):
        return jsonify({"status": "error", "message": "Invalid sync secret"}), 403

    action = str(data.get("action") or "state").strip().lower()

    if action == "upsert_product":
        prod = data.get("product") or {}
        if prod:
            images_b64 = data.get("images_b64") or {}
            img_u = prod.get("image_url") or ""
            fname = os.path.basename(str(img_u).split("?")[0]) if img_u else ""
            prod["image_url"] = _copy_or_download_product_image(
                img_u,
                image_b64=data.get("image_b64") or (images_b64.get(fname) if fname else None)
            )
            _sync_product_detail_images(prod, images_b64=images_b64)
            saved = mall_database.upsert_product_from_obs(prod)
            return jsonify({"status": "ok", "action": "upsert_product", "product": saved}), 200

    elif action == "bulk_sync":
        products = data.get("products") or []
        images_b64 = data.get("images_b64") or {}
        for p in products:
            if isinstance(p, dict):
                img_u = p.get("image_url") or ""
                b64_data = images_b64.get(os.path.basename(str(img_u).split("?")[0])) if img_u else None
                p["image_url"] = _copy_or_download_product_image(img_u, image_b64=b64_data)
                _sync_product_detail_images(p, images_b64=images_b64)
        cnt = mall_database.bulk_sync_products_from_obs(
            products,
            delete_missing=bool(data.get("delete_missing", True))
        )
        if "live_state" in data and isinstance(data["live_state"], dict):
            mall_database.update_live_state_bundle(data["live_state"])
        return jsonify({"status": "ok", "action": "bulk_sync", "synced_count": cnt}), 200

    elif action == "delete_product":
        ok = mall_database.delete_product_by_num_or_obs_id(
            num=data.get("num"),
            obs_product_id=data.get("id") or data.get("obs_product_id")
        )
        return jsonify({"status": "ok", "action": "delete_product", "deleted": ok}), 200

    elif action in ("pin_product", "live_state", "heartbeat", "state"):
        live_bundle = {}
        if action == "heartbeat":
            live_bundle["heartbeat"] = True
        for k in (
            "pinned_product_num",
            "is_live",
            "broadcast_title",
            "ticker_message",
            "timer",
            "youtube_video_id",
            "youtube_channel_input",
            "youtube_embed_url",
            "live_urls",
            "heartbeat",
            "last_heartbeat_ts",
            "obs_server_url"
        ):
            if k in data:
                live_bundle[k] = data[k]
        if "product" in data and isinstance(data["product"], dict):
            p = data["product"]
            images_b64 = data.get("images_b64") or {}
            img_u = p.get("image_url") or ""
            fname = os.path.basename(str(img_u).split("?")[0]) if img_u else ""
            p["image_url"] = _copy_or_download_product_image(
                img_u,
                image_b64=data.get("image_b64") or (images_b64.get(fname) if fname else None)
            )
            _sync_product_detail_images(p, images_b64=images_b64)
            mall_database.upsert_product_from_obs(p)
        if live_bundle:
            mall_database.update_live_state_bundle(live_bundle)
        return jsonify({
            "status": "ok",
            "action": action,
            "live_state": mall_database.get_lite_live_state()
        }), 200

    return jsonify({"status": "ok"}), 200


@app.route("/api/sync/pending-orders", methods=["GET"])
def api_sync_pending_orders():
    """
    로컬 라이브 에이전트가 공유기(NAT) 뒤에서도 클라우드 쇼핑몰 신규 주문을 증분 Pull하여
    OBS 재고를 실시간 차감할 수 있게 해주는 엔드포인트
    """
    if not _verify_sync_secret():
        return jsonify({"status": "error", "message": "Invalid sync secret"}), 403
    try:
        after_id = int(request.args.get("after_id", 0))
    except Exception:
        after_id = 0
    try:
        limit = min(200, max(1, int(request.args.get("limit", 100))))
    except Exception:
        limit = 100
    orders = mall_database.get_mall_orders_after(after_id=after_id, limit=limit)
    latest_id = orders[-1]["id"] if orders else after_id
    return jsonify({
        "status": "ok",
        "orders": orders,
        "count": len(orders),
        "latest_order_id": latest_id
    }), 200


@app.route("/api/sync/pull-now", methods=["POST"])
def api_sync_pull_now():
    """쇼핑몰 관리자 페이지에서 수동으로 OBS 서버 상품/상태를 즉시 당겨오는 API"""
    ok = pull_sync_from_obs_server(force=True)
    prods = mall_database.get_all_mall_products()
    return jsonify({
        "status": "ok",
        "pulled": ok,
        "product_count": len(prods),
        "message": f"OBS 서버와 상품 {len(prods)}건 동기화가 완료되었습니다."
    }), 200


@app.route("/api/products", methods=["GET"])
def api_mall_products():
    category = request.args.get("category", "ALL")
    search = request.args.get("q", "")
    sort = request.args.get("sort", "num_asc")
    prods = mall_database.get_all_mall_products(category=category, search=search, sort=sort)
    return jsonify({"status": "ok", "products": prods, "total_count": len(prods)}), 200


@app.route("/api/products/<int:num>", methods=["GET"])
def api_mall_product_detail(num):
    prod = mall_database.get_mall_product_by_num(num)
    if not prod:
        return jsonify({"status": "error", "message": "상품을 찾을 수 없습니다."}), 404
    reviews = mall_database.get_mall_reviews(num)
    review_summary = mall_database.get_mall_review_summary(num)
    return jsonify({"status": "ok", "product": prod, "reviews": reviews, "review_summary": review_summary}), 200


@app.route("/api/orders/create", methods=["POST"])
def api_mall_create_order():
    """
    쇼핑몰 장바구니(/checkout), 상품 상세(/product/<num>), 또는 라이브 커머스(/live) 바로구매 모달에서 주문을 생성합니다.
    - 회원 및 비회원 모두 모든 경로(MALL, PRODUCT_DETAIL, CART_CHECKOUT, LIVE)에서 주문 가능
    - 비회원 주문 시 주문 조회용 비밀번호(4자 이상) 설정 및 저장 (미입력 시 연락처 뒷 4자리 자동 폴백)
    """
    data = dict(request.get_json(silent=True) or {})
    source_page = str(data.get("source_page") or "MALL").strip().upper()
    user = get_current_mall_user()

    # 라이브커머스(/live) 주문: live_order_token이 전달된 경우 서명 무결성 검증 (위조 토큰 차단)
    live_tok = str(data.get("live_order_token") or "").strip()
    if live_tok and mall_security.verify_signed_token(app.secret_key, "live_order", live_tok) is None:
        return jsonify({"status": "error", "code": "INVALID_LIVE_TOKEN", "message": "유효하지 않은 라이브 주문 보안 토큰입니다."}), 403

    if user:
        if source_page != "LIVE" and not mall_security.verify_csrf_token(session, request):
            return jsonify({"status": "error", "code": "CSRF_INVALID", "message": "CSRF 보안 토큰 검증에 실패했습니다."}), 403
        data["user_id"] = user["id"]
        data["is_guest"] = False
    else:
        data["user_id"] = None
        data["is_guest"] = True
        guest_pw = str(data.get("guest_password") or "").strip()
        if guest_pw:
            valid_gpw, gpw_msg = mall_security.validate_guest_password_policy(guest_pw, min_len=4)
            if not valid_gpw:
                return jsonify({"status": "error", "message": gpw_msg}), 400
        else:
            # 하위호환 폴백: 휴대폰 번호 뒷 4자리를 초기 조회 비밀번호로 자동 설정
            digits = "".join(ch for ch in str(data.get("buyer_phone") or "") if ch.isdigit())
            data["guest_password"] = digits[-4:] if len(digits) >= 4 else "0000"

    if len(mall_database.get_all_mall_products()) == 0:
        pull_sync_from_obs_server(force=False)

    res = mall_database.create_mall_order(data)
    if not res.get("ok"):
        return jsonify({"status": "error", "message": res.get("message", "주문 실패")}), 400

    if not user and res.get("order_code"):
        session["mall_recent_guest_order_code"] = res["order_code"]

    # 로컬 동시 기동 모드일 때만 OBS 서버에 비동기로 주문 웹훅 전달 (클라우드 단독 모드에서는 에이전트가 pending-orders를 Pull)
    if _is_obs_pull_enabled():
        def _send_local_obs_order_webhook():
            try:
                webhook_payload = json.dumps({
                    "order_code": res["order_code"],
                    "buyer_name": res["buyer_name"],
                    "buyer_phone": res["buyer_phone"],
                    "address": str(data.get("address") or ""),
                    "address_detail": str(data.get("address_detail") or ""),
                    "delivery_memo": str(data.get("delivery_memo") or ""),
                    "total_amount": res["total_amount"],
                    "source_page": data.get("source_page", "MALL"),
                    "created_at": res.get("created_at", ""),
                    "items": res["items"]
                }).encode("utf-8")
                req = urllib.request.Request(
                    f"{OBS_SERVER_URL}/api/mall/order-webhook",
                    data=webhook_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                urllib.request.urlopen(req, timeout=1.2).read()
            except Exception:
                pass
        threading.Thread(target=_send_local_obs_order_webhook, daemon=True).start()

    return jsonify({"status": "ok", "order": res}), 200


@app.route("/api/orders/lookup", methods=["GET", "POST"])
def api_mall_orders_lookup():
    """
    보안 강화 주문 조회 API
    - 무인증/무조건 호출 시 타인 주문 목록 반환 금지 (0건)
    - 로그인 회원: 본인 주문 반환
    - 비회원: 주문번호(order_code) + 연락처(buyer_phone) + 비밀번호(guest_password) 3중 일치 시에만 단건 반환
    """
    user = get_current_mall_user()
    req_data = request.get_json(silent=True) if request.is_json else {}
    order_code = str((req_data or {}).get("order_code") or request.values.get("order_code") or "").strip()
    buyer_phone = str((req_data or {}).get("buyer_phone") or request.values.get("buyer_phone") or "").strip()
    guest_password = str((req_data or {}).get("guest_password") or request.values.get("guest_password") or "").strip()

    if order_code or buyer_phone or guest_password:
        ip_addr = str(request.remote_addr or "unknown")
        allowed, retry_sec = mall_security.rate_limiter.check("api_guest_lookup", ip_addr, max_requests=10, window_sec=60.0)
        if not allowed:
            return jsonify({"status": "error", "message": f"조회 시도가 너무 많습니다. {retry_sec}초 후 다시 시도해 주세요.", "orders": [], "count": 0}), 429
        if not (order_code and buyer_phone and guest_password):
            return jsonify({
                "status": "error",
                "message": "주문번호, 연락처, 비회원 주문 비밀번호를 모두 입력해야 조회할 수 있습니다.",
                "orders": [],
                "count": 0
            }), 400
        orders = mall_database.lookup_guest_mall_order(order_code, buyer_phone, guest_password)
        return jsonify({"status": "ok", "orders": orders, "count": len(orders)}), 200

    if user:
        if user.get("role") == "ADMIN":
            orders = mall_database.get_mall_orders(limit=50)
        else:
            orders = mall_database.get_mall_orders_by_user(user["id"], limit=50)
        return jsonify({"status": "ok", "orders": orders, "count": len(orders)}), 200

    return jsonify({"status": "ok", "orders": [], "count": 0}), 200


@app.route("/api/admin/order-status", methods=["POST"])
def api_mall_admin_order_status():
    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id")
    if not order_id:
        return jsonify({"status": "error", "message": "order_id가 필요합니다."}), 400
    ok = mall_database.update_mall_order_status(
        order_id=order_id,
        payment_status=data.get("payment_status"),
        order_status=data.get("order_status")
    )
    return jsonify({"status": "ok" if ok else "error"}), 200


@app.route("/api/admin/apply-discount", methods=["POST"])
def api_mall_admin_apply_discount():
    """모든 상품의 판매가를 소비자가 기준 지정 할인율(%)로 일괄 변경합니다 (OBS DB 포함)."""
    data = request.get_json(silent=True) or {}
    try:
        discount_pct = int(data.get("discount_pct", 30))
    except Exception:
        return jsonify({"status": "error", "message": "유효한 할인율 숫자를 입력해 주세요."}), 400
    discount_pct = max(0, min(95, discount_pct))

    res = mall_database.apply_mall_discount_rate(discount_pct)

    # OBS 메인 DB(live_commerce.db)도 함께 갱신하여 동기화 시 할인율이 덮어써지지 않도록 유지
    obs_updated = 0
    obs_db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "live_commerce.db"))
    if os.path.exists(obs_db_path):
        import sqlite3
        try:
            conn = sqlite3.connect(obs_db_path, timeout=10)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT id, consumer_price, price FROM products")
            for row in cur.fetchall():
                cons_p = int(row["consumer_price"] or 0) or int(row["price"] or 0)
                if cons_p <= 0:
                    continue
                new_p = int(round(cons_p * (100 - discount_pct) / 100.0))
                cur.execute(
                    "UPDATE products SET consumer_price = ?, price = ? WHERE id = ?",
                    (cons_p, new_p, row["id"])
                )
                obs_updated += 1
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[ShoppingMall] OBS DB 할인율 동기화 경고: {e}")

    return jsonify({
        "status": "ok",
        "discount_pct": res["discount_pct"],
        "updated": res["updated"],
        "obs_updated": obs_updated
    }), 200


@app.route("/api/reviews/add", methods=["POST"])
def api_mall_add_review():
    user = get_current_mall_user()
    if not user:
        return jsonify({
            "status": "error",
            "code": "AUTH_REQUIRED",
            "message": "리뷰 작성은 로그인한 회원만 이용하실 수 있습니다."
        }), 401
    if not mall_security.verify_csrf_token(session, request):
        return jsonify({
            "status": "error",
            "code": "CSRF_INVALID",
            "message": "보안 토큰(CSRF) 검증에 실패했습니다. 페이지를 새로고침해 주세요."
        }), 403

    data = request.get_json(silent=True) or {}
    try:
        num = int(data.get("product_num") or 0)
    except Exception:
        num = 0
    if num <= 0 or not mall_database.get_mall_product_by_num(num):
        return jsonify({"status": "error", "message": "리뷰를 등록할 상품을 찾을 수 없습니다."}), 404

    content = str(data.get("content") or "").strip()
    if len(content) < 2:
        return jsonify({"status": "error", "message": "리뷰 내용을 2자 이상 입력해 주세요."}), 400
    if len(content) > 1000:
        return jsonify({"status": "error", "message": "리뷰 내용은 1,000자 이내로 입력해 주세요."}), 400

    try:
        rating = max(1, min(5, int(data.get("rating") or 5)))
    except Exception:
        rating = 5

    author = str(user.get("name") or "구매회원").strip() or "구매회원"
    mall_database.add_mall_review(num, author, rating, content, user_id=user.get("id"))
    reviews = mall_database.get_mall_reviews(num)
    summary = mall_database.get_mall_review_summary(num)
    return jsonify({"status": "ok", "reviews": reviews, "summary": summary}), 200


if __name__ == "__main__":
    print("=" * 68)
    print(f"🛍️ [독립 쇼핑몰 서버 기동] http://localhost:{MALL_PORT}")
    print(f"🔴 [라이브 커머스 전용관] http://localhost:{MALL_PORT}/live")
    print(f"💾 [쇼핑몰 전용 DB 경로] {mall_database.MALL_DB_PATH}")
    print(f"🔗 [OBS 연동 서버 주소] {OBS_SERVER_URL}")
    print("=" * 68)
    app.run(host="0.0.0.0", port=MALL_PORT, debug=False, threaded=True)
