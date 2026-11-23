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
import hashlib
import shutil
import sqlite3
import threading
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from typing import Optional
from flask import Flask, Response, request, jsonify, render_template, send_from_directory, redirect, url_for, session
from flask.sessions import session_json_serializer
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from flask_cors import CORS

import mall_database
import mall_security

# 윈도우 콘솔 UTF-8 출력 보장
if sys.platform.startswith("win"):
    try:
        reconf_out = getattr(sys.stdout, "reconfigure", None)
        reconf_err = getattr(sys.stderr, "reconfigure", None)
        if callable(reconf_out):
            reconf_out(encoding="utf-8", errors="replace")
        if callable(reconf_err):
            reconf_err(encoding="utf-8", errors="replace")
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


_SSO_JTI_LOCK = threading.Lock()
_USED_SSO_JTIS: dict[str, float] = {}


def _consume_sso_jti(jti: str, ttl_sec: int = 120) -> bool:
    """1회용 SSO 토큰 jti를 기록하여 재사용(Replay) 공격을 차단합니다."""
    clean_jti = str(jti or "").strip()
    if not clean_jti:
        return False
    now = time.time()
    with _SSO_JTI_LOCK:
        expired = [k for k, exp_ts in _USED_SSO_JTIS.items() if exp_ts <= now]
        for k in expired:
            _USED_SSO_JTIS.pop(k, None)
        if clean_jti in _USED_SSO_JTIS:
            return False
        _USED_SSO_JTIS[clean_jti] = now + float(ttl_sec)
        return True


def _get_studio_secret_key() -> str:
    env_val = (os.getenv("STUDIO_SECRET_KEY") or "").strip()
    if len(env_val) >= 24:
        return env_val
    key_file = os.path.abspath(os.path.join(BASE_DIR, "..", ".studio_secret_key"))
    try:
        if os.path.exists(key_file):
            with open(key_file, "r", encoding="utf-8") as f:
                saved = f.read().strip()
                if len(saved) >= 24:
                    return saved
    except Exception:
        pass
    return ""


def _get_obs_cloud_sync_secret() -> str:
    if os.getenv("MALL_MODE", "").strip().lower() == "cloud":
        return ""
    obs_db_path = _get_obs_db_path()
    if not obs_db_path or not os.path.exists(obs_db_path):
        return ""
    conn = None
    try:
        conn = sqlite3.connect(obs_db_path, timeout=3.0)
        cur = conn.cursor()
        cur.execute("SELECT value FROM overlay_state WHERE key = 'cloud_mall_config'")
        row = cur.fetchone()
        if row and row[0]:
            cfg = json.loads(row[0])
            if isinstance(cfg, dict):
                return str(cfg.get("sync_secret") or "").strip()
    except Exception:
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return ""


def _get_sso_signing_secret() -> str:
    sync_sec = _get_sync_secret()
    if sync_sec:
        return sync_sec
    if os.getenv("MALL_MODE", "").strip().lower() == "cloud":
        return ""
    obs_sec = _get_obs_cloud_sync_secret()
    if obs_sec:
        return obs_sec
    return _get_studio_secret_key()


def _query_approved_studio_admin(studio_uid: int) -> Optional[dict]:
    obs_db_path = _get_obs_db_path()
    if not obs_db_path or not os.path.exists(obs_db_path):
        return None
    conn = None
    try:
        conn = sqlite3.connect(obs_db_path, timeout=3.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT id, username, display_name, role, status FROM studio_users WHERE id = ?",
            (int(studio_uid),),
        )
        row = cur.fetchone()
        if not row:
            return None
        if row["role"] != "ADMIN" or row["status"] != "APPROVED":
            return None
        username = str(row["username"] or "")
        display_name = str(row["display_name"] or username or "최고 관리자")
        return {
            "id": int(row["id"]),
            "username": username,
            "display_name": display_name,
        }
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _resolve_studio_admin_from_cookie() -> Optional[dict]:
    if os.getenv("MALL_MODE", "").strip().lower() == "cloud":
        return None
    raw_cookie = (request.cookies.get("studio_sid") or "").strip()
    if not raw_cookie:
        return None
    studio_secret = _get_studio_secret_key()
    if not studio_secret:
        return None
    try:
        serializer = URLSafeTimedSerializer(
            secret_key=studio_secret,
            salt="cookie-session",
            serializer=session_json_serializer,
            signer_kwargs={"key_derivation": "hmac", "digest_method": hashlib.sha1},
        )
        data = serializer.loads(raw_cookie, max_age=43200)
    except (BadSignature, SignatureExpired, Exception):
        return None
    if not isinstance(data, dict):
        return None
    raw_uid = data.get("studio_user_id")
    if not raw_uid:
        return None
    try:
        studio_uid = int(raw_uid)
    except (TypeError, ValueError):
        return None
    return _query_approved_studio_admin(studio_uid)


def _establish_mall_sso_session(studio_admin: dict, auth_source: str, preserve_csrf: bool = False) -> Optional[dict]:
    admin_user = mall_database.get_primary_mall_admin_user()
    if not admin_user:
        return None
    display_name = str(studio_admin.get("display_name") or studio_admin.get("username") or "최고 관리자")
    mall_security.rotate_session(session, preserve_keys=["_csrf_token"] if preserve_csrf else None)
    session.permanent = True
    session["mall_user_id"] = int(admin_user["id"])
    session["mall_role"] = "ADMIN"
    session["mall_auth_source"] = auth_source
    session["studio_sso_user_id"] = int(studio_admin["id"])
    session["studio_sso_display_name"] = display_name
    user_view = dict(admin_user)
    user_view["name"] = display_name
    return user_view


def get_current_mall_user():
    auth_source = session.get("mall_auth_source")
    if auth_source == "studio_cookie":
        studio_admin = _resolve_studio_admin_from_cookie()
        if not studio_admin or int(studio_admin["id"]) != int(session.get("studio_sso_user_id") or 0):
            session.clear()
            return None
    elif auth_source == "studio_sso" and os.getenv("MALL_MODE", "").strip().lower() != "cloud":
        if request.cookies.get("studio_sid"):
            studio_admin = _resolve_studio_admin_from_cookie()
            if not studio_admin or int(studio_admin["id"]) != int(session.get("studio_sso_user_id") or 0):
                session.clear()
                return None
        elif os.path.exists(_get_obs_db_path()) and session.get("studio_sso_user_id"):
            if not _query_approved_studio_admin(int(session["studio_sso_user_id"])):
                session.clear()
                return None

    uid = session.get("mall_user_id")
    if uid:
        u = mall_database.get_mall_user_by_id(int(uid))
        if u and auth_source in ("studio_sso", "studio_cookie") and session.get("studio_sso_display_name"):
            u = dict(u)
            u["name"] = str(session["studio_sso_display_name"])
        return u

    if not session.get("sso_opt_out"):
        studio_admin = _resolve_studio_admin_from_cookie()
        if studio_admin:
            return _establish_mall_sso_session(
                studio_admin,
                auth_source="studio_cookie",
                preserve_csrf=(request.method in ("POST", "PUT", "DELETE", "PATCH")),
            )
    return None


def create_live_order_token() -> str:
    return mall_security.create_signed_token(str(app.secret_key or ""), "live_order", {"page": "LIVE"}, ttl_sec=86400)


@app.context_processor
def _inject_mall_globals():
    u = get_current_mall_user()
    return {
        "current_mall_user": u,
        "csrf_token": mall_security.get_csrf_token(session),
        "studio_public_url": resolve_studio_public_url(),
        "mall_public_url": resolve_mall_public_url(),
        "live_order_token": create_live_order_token(),
        "mall_categories": mall_database.get_mall_categories(active_only=True),
        "category_labels": mall_database.get_mall_category_label_map()
    }


@app.before_request
def _mall_security_gateway():
    if request.method == "OPTIONS":
        return None
    path = request.path or "/"

    # 어드민 페이지 및 어드민 API 보호 (/admin, /api/admin/*)
    if path == "/admin" or path.startswith("/api/admin/"):
        u = get_current_mall_user()
        if u and u.get("role") != "ADMIN" and not session.get("sso_opt_out"):
            studio_admin = _resolve_studio_admin_from_cookie()
            if studio_admin:
                u = _establish_mall_sso_session(
                    studio_admin,
                    auth_source="studio_cookie",
                    preserve_csrf=(request.method in ("POST", "PUT", "DELETE", "PATCH")),
                )
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
_last_obs_catalog_poll_ts = 0.0


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
    raw_imgs = mall_database.parse_detail_images(prod_dict.get("detail_images"))
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


def pull_sync_from_obs_server(force=False, force_catalog=False):
    """
    OBS 서버(OBS_SERVER_URL)의 최신 상품 목록 및 라이브 상태(현재 선택된 상품 번호, 방송중 여부, 타이머, 안내 자막)를
    조회하여 쇼핑몰 DB(shopping_mall.db)에 실시간 반영합니다.
    (클라우드 단독 모드에서는 로컬 에이전트의 Outbound Push만 수신하므로 즉시 반환합니다.)
    """
    if not _is_obs_pull_enabled():
        return False

    global _last_obs_poll_ts, _last_obs_catalog_poll_ts
    now = time.time()
    need_overlay_poll = force or (now - _last_obs_poll_ts) >= 0.8
    need_catalog_poll = (
        force
        or len(mall_database.get_all_mall_products()) == 0
        or (force_catalog and (now - _last_obs_catalog_poll_ts) >= 5.0)
    )
    if not need_overlay_poll and not need_catalog_poll:
        return False

    synced_any = False

    # 1. OBS 서버 HTTP API 조회 시도 (라이브 상태 / 핀 고정 상품)
    if need_overlay_poll:
        _last_obs_poll_ts = now
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

    # 2. 상품 전체 목록 동기화 (강제 요청, DB 공백, 또는 홈/상세 페이지 진입 시 5초 throttle 갱신)
    if need_catalog_poll:
        _last_obs_catalog_poll_ts = now
        try:
            req_pr = urllib.request.Request(
                f"{OBS_SERVER_URL}/api/products",
                headers={"User-Agent": "ShoppingMallProductSync/1.0"}
            )
            with urllib.request.urlopen(req_pr, timeout=1.8) as resp:
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
    pull_sync_from_obs_server(force=False, force_catalog=True)
    category = request.args.get("category", "ALL").upper()
    search = request.args.get("q", "").strip()
    sort = request.args.get("sort", "num_asc")
    all_prods = mall_database.get_all_mall_products()
    has_etc = any((p.get("category") or "").upper() == "ETC" for p in all_prods)
    products = mall_database.get_all_mall_products(category=category, search=search, sort=sort)
    live_state = mall_database.get_full_live_state()
    return render_template(
        "index.html",
        products=products,
        live_state=live_state,
        has_etc_products=has_etc,
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
    user = get_current_mall_user()
    is_admin = bool(user and user.get("role") == "ADMIN")
    if not live_state.get("is_live") and not is_admin:
        return redirect(url_for("mall_home"))
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
    pull_sync_from_obs_server(force=False, force_catalog=True)
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


@app.route("/auth/sso", methods=["GET"])
def mall_auth_sso():
    """Studio 최고 관리자(/admin/mall-sso)에서 전달된 1회용 서명 SSO 토큰 검증 및 자동 로그인"""
    token = (request.args.get("token") or "").strip()
    next_url = (request.args.get("next") or "/admin").strip()
    if not mall_security.is_safe_redirect_url(next_url):
        next_url = "/admin"

    sso_secret = _get_sso_signing_secret()
    payload = mall_security.verify_signed_token(sso_secret, "mall_admin_sso", token) if (sso_secret and token) else None
    if (
        isinstance(payload, dict)
        and payload.get("studio_user_id")
        and _consume_sso_jti(str(payload.get("jti") or ""))
    ):
        studio_admin = {
            "id": int(payload["studio_user_id"]),
            "username": str(payload.get("username") or ""),
            "display_name": str(payload.get("display_name") or payload.get("username") or "최고 관리자"),
        }
        if _establish_mall_sso_session(studio_admin, auth_source="studio_sso", preserve_csrf=False):
            return redirect(next_url)

    return redirect(url_for("mall_auth_login", next=next_url))


@app.route("/auth/logout", methods=["GET", "POST"])
def mall_auth_logout():
    session.clear()
    session["sso_opt_out"] = True
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
    """쇼핑몰 관리자 페이지 (상품 관리 · 카테고리 관리 · 주문 관리)"""
    pull_sync_from_obs_server(force=False)
    products = mall_database.get_all_mall_products()
    orders = mall_database.get_mall_orders(limit=100)
    live_state = mall_database.get_full_live_state()
    all_categories = mall_database.get_mall_categories(active_only=False)
    policy_defaults = mall_database.get_mall_policy_defaults()
    return render_template(
        "admin.html",
        products=products,
        orders=orders,
        live_state=live_state,
        all_categories=all_categories,
        policy_defaults=policy_defaults,
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
            return jsonify({
                "status": "ok",
                "action": "upsert_product",
                "detail_fields_supported": True,
                "product": saved
            }), 200

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
        return jsonify({
            "status": "ok",
            "action": "bulk_sync",
            "detail_fields_supported": True,
            "synced_count": cnt
        }), 200

    elif action == "upload_product_image":
        filename = os.path.basename(str(data.get("filename") or "").split("?")[0]).strip()
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        image_b64 = data.get("image_b64")
        if not filename or ext not in ("jpg", "jpeg", "jfif", "png", "webp", "gif") or not isinstance(image_b64, str) or not image_b64.strip():
            return jsonify({"status": "error", "message": "Invalid image upload payload"}), 400
        saved_url = _copy_or_download_product_image(
            f"/static/uploads/products/{filename}",
            image_b64=image_b64.strip()
        )
        return jsonify({
            "status": "ok",
            "action": "upload_product_image",
            "url": saved_url,
            "detail_fields_supported": True
        }), 200

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
    pull_sync_from_obs_server(force=False, force_catalog=True)
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
    if live_tok and mall_security.verify_signed_token(str(app.secret_key or ""), "live_order", live_tok) is None:
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


def _restore_obs_stock_for_items(restored_items: list) -> int:
    """주문취소·환불완료로 복구된 품목 수량을 동일 호스트 OBS 재고 DB(live_commerce.db)에도 반영합니다."""
    if not restored_items:
        return 0
    obs_db_path = _get_obs_db_path()
    if not obs_db_path or not os.path.exists(obs_db_path):
        return 0
    updated = 0
    conn = None
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
    except Exception as e:
        print(f"[ShoppingMall] OBS DB 재고 복구 경고: {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return updated


@app.route("/api/admin/order-status", methods=["POST"])
def api_mall_admin_order_status():
    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id")
    if not order_id:
        return jsonify({"status": "error", "message": "order_id가 필요합니다."}), 400
    res = mall_database.update_mall_order_workflow(
        order_id=order_id,
        payment_status=data.get("payment_status"),
        order_status=data.get("order_status"),
        claim_reason=data.get("claim_reason"),
    )
    if not res.get("ok"):
        return jsonify({
            "status": "error",
            "message": res.get("message") or "주문 상태 변경에 실패했습니다."
        }), 400
    obs_restored = (
        _restore_obs_stock_for_items(res.get("restored_items") or [])
        if res.get("stock_restored_now")
        else 0
    )
    return jsonify({
        "status": "ok",
        "order": res.get("order"),
        "stock_restored_now": bool(res.get("stock_restored_now")),
        "obs_stock_restored": obs_restored,
    }), 200


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


def _get_obs_db_path() -> str:
    env_p = (os.getenv("OBS_DB_PATH") or "").strip()
    if env_p:
        return env_p
    return os.path.abspath(os.path.join(BASE_DIR, "..", "live_commerce.db"))


def _copy_mall_image_to_obs(img_url: str):
    if not img_url or not str(img_url).startswith("/static/uploads/products/"):
        return
    fname = os.path.basename(str(img_url).split("?")[0])
    if not fname:
        return
    src_p = os.path.join(UPLOAD_PRODUCTS_DIR, fname)
    parent_static = os.path.abspath(os.path.join(BASE_DIR, "..", "static"))
    if not (os.path.isdir(parent_static) and os.path.exists(src_p)):
        return
    dst_dir = os.path.join(parent_static, "uploads", "products")
    os.makedirs(dst_dir, exist_ok=True)
    dst_p = os.path.join(dst_dir, fname)
    try:
        if not os.path.exists(dst_p) or os.path.getsize(dst_p) == 0:
            shutil.copy2(src_p, dst_p)
    except Exception:
        pass


def _sync_mall_product_to_obs_db(
    action: str,
    product=None,
    deleted_num=None,
    deleted_obs_id=None,
    deleted_category=None
) -> bool:
    """동일 호스트에 live_commerce.db가 존재할 경우 쇼핑몰 관리자 변경을 OBS 재고 DB에 양방향 동기화합니다."""
    obs_db_path = _get_obs_db_path()
    if not os.path.exists(obs_db_path):
        return False

    import sqlite3
    try:
        conn = sqlite3.connect(obs_db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='products'")
        if not cur.fetchone():
            conn.close()
            return False

        for col_name, col_def in (
            ("category", "TEXT DEFAULT ''"),
            ("live_price", "INTEGER DEFAULT 0"),
            ("consumer_price", "INTEGER DEFAULT 0"),
            ("cost_price", "INTEGER DEFAULT 0"),
            ("detail_content", "TEXT DEFAULT ''"),
            ("detail_images", "TEXT DEFAULT '[]'"),
            ("shipping_info", "TEXT DEFAULT ''"),
            ("exchange_return_info", "TEXT DEFAULT ''"),
            ("non_returnable_info", "TEXT DEFAULT ''"),
            ("as_info", "TEXT DEFAULT ''"),
        ):
            try:
                cur.execute(f"ALTER TABLE products ADD COLUMN {col_name} {col_def}")
            except Exception:
                pass

        if action == "upsert" and isinstance(product, dict):
            _copy_mall_image_to_obs(str(product.get("image_url") or ""))
            for d_u in (product.get("detail_images") or []):
                _copy_mall_image_to_obs(str(d_u or ""))

            num = int(product["num"])
            obs_id = int(product.get("obs_product_id") or 0)
            row = None
            if obs_id > 0:
                cur.execute("SELECT id, cost_price FROM products WHERE id = ?", (obs_id,))
                row = cur.fetchone()
            if not row:
                cur.execute("SELECT id, cost_price FROM products WHERE num = ?", (num,))
                row = cur.fetchone()

            detail_imgs_json = json.dumps(mall_database.parse_detail_images(product.get("detail_images")), ensure_ascii=False)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if row:
                target_obs_id = int(row["id"])
                cur.execute("""
                    UPDATE products
                    SET num = ?, name = ?, category = ?, price = ?, live_price = ?, consumer_price = ?,
                        image_url = ?, specs = ?, detail_content = ?, detail_images = ?,
                        shipping_info = ?, exchange_return_info = ?, non_returnable_info = ?, as_info = ?
                    WHERE id = ?
                """, (
                    num,
                    str(product.get("name") or ""),
                    str(product.get("category") or "").strip().upper(),
                    int(product.get("price") or 0),
                    int(product.get("live_price") or product.get("price") or 0),
                    int(product.get("consumer_price") or product.get("price") or 0),
                    str(product.get("image_url") or ""),
                    str(product.get("specs") or ""),
                    str(product.get("detail_content") or ""),
                    detail_imgs_json,
                    str(product.get("shipping_info") or ""),
                    str(product.get("exchange_return_info") or ""),
                    str(product.get("non_returnable_info") or ""),
                    str(product.get("as_info") or ""),
                    target_obs_id
                ))
            else:
                cur.execute("""
                    INSERT INTO products
                    (num, name, category, price, live_price, consumer_price, cost_price,
                     image_url, specs, detail_content, detail_images,
                     shipping_info, exchange_return_info, non_returnable_info, as_info, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    num,
                    str(product.get("name") or ""),
                    str(product.get("category") or "").strip().upper(),
                    int(product.get("price") or 0),
                    int(product.get("live_price") or product.get("price") or 0),
                    int(product.get("consumer_price") or product.get("price") or 0),
                    str(product.get("image_url") or ""),
                    str(product.get("specs") or ""),
                    str(product.get("detail_content") or ""),
                    detail_imgs_json,
                    str(product.get("shipping_info") or ""),
                    str(product.get("exchange_return_info") or ""),
                    str(product.get("non_returnable_info") or ""),
                    str(product.get("as_info") or ""),
                    now_str
                ))
                target_obs_id = int(cur.lastrowid or 0)

            cur.execute("DELETE FROM product_options WHERE product_id = ?", (target_obs_id,))
            for opt in (product.get("options") or [{"color": "기본", "size": "FREE", "stock": 10}]):
                cur.execute("""
                    INSERT OR REPLACE INTO product_options (product_id, color, size, stock)
                    VALUES (?, ?, ?, ?)
                """, (
                    target_obs_id,
                    str(opt.get("color") or "-").strip() or "-",
                    str(opt.get("size") or "-").strip() or "-",
                    max(0, int(opt.get("stock") or 0))
                ))
            conn.commit()
            conn.close()
            if product.get("id") and int(product.get("obs_product_id") or 0) != target_obs_id:
                mall_database.set_mall_product_obs_id(int(product["id"]), target_obs_id)
            return True

        elif action == "delete":
            if deleted_obs_id and int(deleted_obs_id) > 0:
                cur.execute("DELETE FROM products WHERE id = ?", (int(deleted_obs_id),))
            elif deleted_num is not None:
                cur.execute("DELETE FROM products WHERE num = ?", (int(deleted_num),))
            conn.commit()
            conn.close()
            return True

        elif action == "category_delete" and deleted_category:
            cur.execute(
                "UPDATE products SET category = 'ETC' WHERE UPPER(COALESCE(category, '')) = ?",
                (str(deleted_category).strip().upper(),)
            )
            conn.commit()
            conn.close()
            return True

        conn.close()
    except Exception as sync_err:
        print(f"[ShoppingMall] OBS DB 양방향 동기화 경고: {sync_err}")
    return False


@app.route("/api/admin/categories", methods=["GET"])
def api_mall_admin_categories_list():
    cats = mall_database.get_mall_categories(active_only=False)
    return jsonify({"status": "ok", "categories": cats}), 200


@app.route("/api/admin/categories/save", methods=["POST"])
def api_mall_admin_categories_save():
    data = request.get_json(silent=True) or {}
    cat_id = data.get("id")
    if cat_id not in (None, ""):
        res = mall_database.update_mall_category(
            cat_id=int(cat_id),
            name=data.get("name"),
            sort_order=data.get("sort_order"),
            is_active=data.get("is_active")
        )
    else:
        res = mall_database.create_mall_category(
            name=str(data.get("name") or ""),
            code=str(data.get("code") or ""),
            sort_order=data.get("sort_order"),
            is_active=bool(data.get("is_active", True))
        )
    if not res.get("ok"):
        return jsonify({"status": "error", "message": res.get("message", "카테고리 저장 실패")}), 400
    cats = mall_database.get_mall_categories(active_only=False)
    return jsonify({"status": "ok", "categories": cats, "result": res}), 200


@app.route("/api/admin/categories/delete", methods=["POST"])
def api_mall_admin_categories_delete():
    data = request.get_json(silent=True) or {}
    cat_id = data.get("id")
    if not cat_id:
        return jsonify({"status": "error", "message": "삭제할 카테고리 ID가 필요합니다."}), 400
    res = mall_database.delete_mall_category(int(cat_id))
    if not res.get("ok"):
        return jsonify({"status": "error", "message": res.get("message", "카테고리 삭제 실패")}), 400
    _sync_mall_product_to_obs_db("category_delete", deleted_category=res.get("deleted_code"))
    cats = mall_database.get_mall_categories(active_only=False)
    return jsonify({
        "status": "ok",
        "deleted_code": res.get("deleted_code"),
        "moved_products": res.get("moved_products", 0),
        "categories": cats
    }), 200


@app.route("/api/admin/upload/image", methods=["POST"])
def api_mall_admin_upload_image():
    if "image" not in request.files:
        return jsonify({"status": "error", "message": "이미지 파일이 전달되지 않았습니다."}), 400
    file = request.files["image"]
    filename = str(file.filename or "").strip()
    if not filename:
        return jsonify({"status": "error", "message": "선택된 파일이 없습니다."}), 400
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ("jpg", "jpeg", "jfif", "png", "webp", "gif"):
        return jsonify({"status": "error", "message": "지원하지 않는 이미지 형식입니다. (jpg, jfif, png, webp 가능)"}), 400
    import uuid
    unique_name = f"prod_{int(time.time())}_{uuid.uuid4().hex[:8]}.{ext}"
    save_path = os.path.join(UPLOAD_PRODUCTS_DIR, unique_name)
    file.save(save_path)
    img_url = f"/static/uploads/products/{unique_name}"
    _copy_mall_image_to_obs(img_url)
    return jsonify({"status": "ok", "url": img_url}), 200


@app.route("/api/admin/products/add", methods=["POST"])
def api_mall_admin_products_add():
    data = request.get_json(silent=True) or {}
    res = mall_database.create_mall_product(data)
    if not res.get("ok"):
        return jsonify({"status": "error", "message": res.get("message", "상품 등록 실패")}), 400
    prod = res.get("product")
    _sync_mall_product_to_obs_db("upsert", product=prod)
    refreshed = mall_database.get_mall_product_by_num(prod["num"]) if prod else None
    cats = mall_database.get_mall_categories(active_only=False)
    return jsonify({"status": "ok", "product": refreshed or prod, "categories": cats}), 200


@app.route("/api/admin/products/update", methods=["POST"])
def api_mall_admin_products_update():
    data = request.get_json(silent=True) or {}
    pid = data.get("id")
    if not pid:
        return jsonify({"status": "error", "message": "수정할 상품 ID가 필요합니다."}), 400
    res = mall_database.update_mall_product(int(pid), data)
    if not res.get("ok"):
        return jsonify({"status": "error", "message": res.get("message", "상품 수정 실패")}), 400
    prod = res.get("product")
    _sync_mall_product_to_obs_db("upsert", product=prod)
    refreshed = mall_database.get_mall_product_by_num(prod["num"]) if prod else None
    cats = mall_database.get_mall_categories(active_only=False)
    return jsonify({"status": "ok", "product": refreshed or prod, "categories": cats}), 200


@app.route("/api/admin/products/delete", methods=["POST"])
def api_mall_admin_products_delete():
    data = request.get_json(silent=True) or {}
    pid = data.get("id")
    if not pid:
        return jsonify({"status": "error", "message": "삭제할 상품 ID가 필요합니다."}), 400
    res = mall_database.delete_mall_product(int(pid))
    if not res.get("ok"):
        return jsonify({"status": "error", "message": res.get("message", "상품 삭제 실패")}), 400
    _sync_mall_product_to_obs_db(
        "delete",
        deleted_num=res.get("deleted_num"),
        deleted_obs_id=res.get("deleted_obs_id")
    )
    cats = mall_database.get_mall_categories(active_only=False)
    return jsonify({"status": "ok", "categories": cats}), 200


@app.route("/api/admin/policy-defaults", methods=["GET", "POST"])
def api_mall_admin_policy_defaults():
    if request.method == "GET":
        return jsonify({"status": "ok", "defaults": mall_database.get_mall_policy_defaults()}), 200
    data = request.get_json(silent=True) or {}
    saved = mall_database.set_mall_policy_defaults(data)
    return jsonify({"status": "ok", "defaults": saved}), 200


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
