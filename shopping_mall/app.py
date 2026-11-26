# -*- coding: utf-8 -*-
"""
OBS 실시간 연동 독립형 쇼핑몰 서버 (shopping_mall/app.py)
- 기본 포트: 3000번 (http://0.0.0.0:3000)
- 별도의 SQLite DB(shopping_mall.db)를 사용하며, OBS 스튜디오(Port 5000)와
  상품/옵션/재고 및 라이브 선택(Pin) 상품을 실시간 동기화합니다.
- 이 폴더(shopping_mall)만 단독으로 Git에 푸시하여 클라우드/외부 서버에 배포할 수 있습니다.
"""

import os
import re
import sys
import io
import csv
import json
import time
import base64
import hashlib
import shutil
import sqlite3
import threading
import urllib.request
from datetime import datetime, timedelta
from typing import Optional
from xml.sax.saxutils import escape as _xml_escape
from flask import Flask, Response, request, jsonify, render_template, send_from_directory, redirect, url_for, session, has_request_context
from flask.sessions import session_json_serializer
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from flask_cors import CORS

import mall_cs_service
import mall_database
import mall_member_service
import mall_notifications
import mall_ops
import mall_payment
import mall_promotions
import mall_security
import mall_shipping
from mall_ops import ops_bp
from mall_promotions import promotions_bp
from routes_cs import cs_bp
from routes_member import member_bp

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
_DEFAULT_UPLOAD_PRODUCTS_DIR = os.path.join(BASE_DIR, "static", "uploads", "products")
UPLOAD_PRODUCTS_DIR = os.path.abspath(
    os.getenv("MALL_PRODUCT_UPLOAD_DIR", "").strip() or _DEFAULT_UPLOAD_PRODUCTS_DIR
)
os.makedirs(UPLOAD_PRODUCTS_DIR, exist_ok=True)
_MAX_PRODUCT_IMAGE_BYTES = 10 * 1024 * 1024


def _get_upload_products_dir() -> str:
    if UPLOAD_PRODUCTS_DIR != os.path.abspath(_DEFAULT_UPLOAD_PRODUCTS_DIR):
        return UPLOAD_PRODUCTS_DIR
    env_dir = os.getenv("MALL_PRODUCT_UPLOAD_DIR", "").strip()
    return os.path.abspath(env_dir) if env_dir else UPLOAD_PRODUCTS_DIR

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
    if has_request_context():
        try:
            fwd_host = (request.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
            raw_host = fwd_host or (request.host or "").strip()
            scheme = (request.headers.get("X-Forwarded-Proto") or request.scheme or "http").split(",")[0].strip()
            if raw_host:
                return f"{scheme}://{raw_host}"
        except Exception:
            pass
    scheme, hostname = _extract_mall_request_hostname_and_scheme()
    return f"{scheme}://{hostname}:{port}"


def build_mall_absolute_url(path_or_url: str) -> str:
    base = resolve_mall_public_url().rstrip("/")
    raw = str(path_or_url or "").strip()
    if not raw:
        return f"{base}/static/og-default.png"
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if not raw.startswith("/"):
        raw = "/" + raw
    return f"{base}{raw}"


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


def is_scheduler_enabled() -> bool:
    """백그라운드 유지보수 스케줄러 활성 여부를 MALL_SCHEDULER_ENABLED 환경변수로 판별합니다."""
    raw = os.getenv("MALL_SCHEDULER_ENABLED")
    if raw is not None and raw.strip() != "":
        val = raw.strip().lower()
        if val in ("0", "false", "no", "off"):
            return False
        if val in ("1", "true", "yes", "on"):
            return True
        return False
    if os.getenv("PYTEST_CURRENT_TEST") or app.config.get("TESTING"):
        return False
    return True


def maybe_start_mall_scheduler() -> bool:
    """M6 스케줄러 연결용 훅: 활성 상태를 기록하고 유지보수 스케줄러 스레드를 안전하게 시작합니다."""
    enabled = is_scheduler_enabled()
    app.config["MALL_SCHEDULER_ENABLED"] = enabled
    if not enabled:
        mall_ops.stop_maintenance_scheduler(timeout_sec=0.5)
        return False
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return True
    mall_ops.start_maintenance_scheduler()
    return True


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
app.json.ensure_ascii = False
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
app.config["MALL_RESOLVE_PUBLIC_URL"] = resolve_mall_public_url
app.register_blueprint(member_bp)
app.register_blueprint(cs_bp)
app.register_blueprint(promotions_bp)
app.register_blueprint(ops_bp)


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
        if not u:
            mall_security.rotate_session(
                session,
                preserve_keys=["_csrf_token"] if request.method in ("POST", "PUT", "DELETE", "PATCH") else None,
            )
            session["sso_opt_out"] = True
            return None
        if auth_source in ("studio_sso", "studio_cookie") and session.get("studio_sso_display_name"):
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


app.config["MALL_GET_CURRENT_USER"] = get_current_mall_user


@app.context_processor
def _inject_mall_globals():
    u = get_current_mall_user()
    return {
        "current_mall_user": u,
        "csrf_token": mall_security.get_csrf_token(session),
        "studio_public_url": resolve_studio_public_url(),
        "mall_public_url": resolve_mall_public_url(),
        "og_default_image_url": build_mall_absolute_url("/static/og-default.png"),
        "mall_abs_url": build_mall_absolute_url,
        "live_order_token": create_live_order_token(),
        "mall_categories": mall_database.get_mall_categories(active_only=True),
        "category_labels": mall_database.get_mall_category_label_map(),
        "user_wishlist_nums": mall_database.get_user_wishlist_nums(int(u["id"])) if u else [],
        "store_info": mall_database.get_mall_store_info(),
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


@app.errorhandler(404)
def _mall_handle_404(err):
    if (request.path or "").startswith("/api/"):
        return jsonify({"status": "error", "ok": False, "message": "요청하신 API 경로를 찾을 수 없습니다."}), 404
    return "요청하신 페이지를 찾을 수 없습니다.", 404

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

    upload_dir = _get_upload_products_dir()
    os.makedirs(upload_dir, exist_ok=True)
    dest_path = os.path.join(upload_dir, filename)
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
    upload_dir = _get_upload_products_dir()
    local_path = os.path.join(upload_dir, safe_name)
    if not os.path.exists(local_path):
        _copy_or_download_product_image(f"/static/uploads/products/{safe_name}")
    return send_from_directory(upload_dir, safe_name)


# ==============================================================================
# 🖥️ 쇼핑몰 페이지 라우트 (Port 3000)
# ==============================================================================

_PUBLIC_INTERNAL_PRODUCT_KEYS = ("cost_price", "supplier_id", "supplier_name", "obs_product_id")
_STORE_SENSITIVE_PRODUCT_KEYS = ("live_price", "live_discount_rate", *_PUBLIC_INTERNAL_PRODUCT_KEYS)


def _sanitize_store_product_for_view(product: Optional[dict]) -> Optional[dict]:
    """일반 스토어 뷰/템플릿 전달용 상품 dict에서 라이브 특가·매입원가·매입업체·OBS 내부 ID를 제거합니다."""
    if not isinstance(product, dict):
        return product
    sanitized = {k: v for k, v in product.items() if k not in _STORE_SENSITIVE_PRODUCT_KEYS}
    if isinstance(sanitized.get("options"), list):
        sanitized["options"] = [
            dict(opt) if isinstance(opt, dict) else opt
            for opt in sanitized["options"]
        ]
    return sanitized


def _sanitize_public_product_detail(product: Optional[dict]) -> Optional[dict]:
    """공개 상품 상세 API 전달용 dict에서 매입원가·매입업체·OBS 내부 ID를 제거합니다 (/live 프리뷰용 live_price 유지)."""
    if not isinstance(product, dict):
        return product
    sanitized = {k: v for k, v in product.items() if k not in _PUBLIC_INTERNAL_PRODUCT_KEYS}
    if isinstance(sanitized.get("options"), list):
        sanitized["options"] = [
            dict(opt) if isinstance(opt, dict) else opt
            for opt in sanitized["options"]
        ]
    return sanitized


def _read_json_dict(allow_form: bool = False) -> tuple[Optional[dict], Optional[tuple]]:
    """요청 JSON 본문이 dict인지 엄격히 검증하고 비-객체 JSON(['bad'] 등)은 HTTP 400으로 거부합니다."""
    if request.is_json:
        raw = request.get_json(silent=True)
        if raw is None:
            raw_bytes = request.get_data() or b""
            if raw_bytes.strip():
                return None, (jsonify({"status": "error", "ok": False, "message": "유효한 JSON 객체 형식이 아닙니다."}), 400)
            return {}, None
        if not isinstance(raw, dict):
            return None, (jsonify({"status": "error", "ok": False, "message": "요청 본문은 JSON 객체(dict)여야 합니다."}), 400)
        return raw, None
    if allow_form:
        return request.form.to_dict(), None
    raw = request.get_json(silent=True)
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return None, (jsonify({"status": "error", "ok": False, "message": "요청 본문은 JSON 객체(dict)여야 합니다."}), 400)
    return raw, None


@app.route("/", methods=["GET"])
def mall_home():
    """쇼핑몰 메인 홈 페이지"""
    pull_sync_from_obs_server(force=False, force_catalog=True)
    category = request.args.get("category", "ALL").upper()
    search = request.args.get("q", "").strip()
    sort = request.args.get("sort", "num_asc")
    exclude_sold_out = str(request.args.get("exclude_sold_out") or "").strip().lower() in ("1", "true", "yes", "on")
    min_price_raw = (request.args.get("min_price") or "").strip()
    max_price_raw = (request.args.get("max_price") or "").strip()
    page_raw = request.args.get("page", "1")
    per_page_raw = request.args.get("per_page", "12")

    all_prods = mall_database.get_all_mall_products()
    has_etc = any((p.get("category") or "").upper() == "ETC" for p in all_prods)
    page_res = mall_database.query_mall_products_paginated(
        category=category,
        search=search,
        sort=sort,
        exclude_sold_out=exclude_sold_out,
        min_price=min_price_raw or None,
        max_price=max_price_raw or None,
        page=page_raw,
        per_page=per_page_raw,
    )
    products = [_sanitize_store_product_for_view(p) for p in page_res["products"]]
    live_state = mall_database.get_full_live_state()
    show_hero = (
        category == "ALL"
        and not search
        and not exclude_sold_out
        and not min_price_raw
        and not max_price_raw
        and int(page_res.get("page") or 1) == 1
    )
    hero_banners = mall_database.list_mall_hero_banners(active_only=True) if show_hero else []
    return render_template(
        "index.html",
        products=products,
        pagination=page_res,
        exclude_sold_out=exclude_sold_out,
        min_price=min_price_raw,
        max_price=max_price_raw,
        live_state=live_state,
        has_etc_products=has_etc,
        current_category=category,
        search_query=search,
        current_sort=sort,
        hero_banners=hero_banners,
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
    sanitized_selected = _sanitize_public_product_detail(live_state.get("selected_product"))
    sanitized_all = [_sanitize_public_product_detail(p) for p in (live_state.get("all_products") or [])]
    live_state["selected_product"] = sanitized_selected
    live_state["all_products"] = sanitized_all
    return render_template(
        "live_commerce.html",
        live_state=live_state,
        selected_product=sanitized_selected,
        all_products=sanitized_all,
        active_nav="live"
    )


@app.route("/product/<int:num>", methods=["GET"])
def mall_product_detail(num):
    """일반 쇼핑몰 개별 상품 상세 페이지"""
    pull_sync_from_obs_server(force=False, force_catalog=True)
    raw_product = mall_database.get_mall_product_by_num(num)
    if not raw_product:
        return redirect(url_for("mall_home"))
    viewer = get_current_mall_user()
    reviews = mall_database.get_mall_reviews(num, viewer=viewer)
    review_summary = mall_database.get_mall_review_summary(num)
    raw_related = mall_database.get_related_mall_products(raw_product, limit=4)
    product = _sanitize_store_product_for_view(raw_product)
    related = [_sanitize_store_product_for_view(p) for p in raw_related]
    live_state = mall_database.get_full_live_state()
    product_wish_count = mall_database.get_product_wish_count(num)
    qna_items = mall_cs_service.list_product_qna(num, viewer=viewer, limit=100)
    return render_template(
        "product_detail.html",
        product=product,
        reviews=reviews,
        review_summary=review_summary,
        qna_items=qna_items,
        qna_count=len(qna_items),
        related_products=related,
        live_state=live_state,
        product_wish_count=product_wish_count,
        active_nav="shop"
    )


@app.route("/wishlist", methods=["GET"])
def mall_wishlist_page():
    """찜한 상품 목록 페이지"""
    pull_sync_from_obs_server(force=False)
    u = get_current_mall_user()
    raw_wishlist = mall_database.get_user_wishlist_products(int(u["id"])) if u else []
    wishlist_products = [_sanitize_store_product_for_view(p) for p in raw_wishlist]
    live_state = mall_database.get_full_live_state()
    return render_template(
        "wishlist.html",
        wishlist_products=wishlist_products,
        live_state=live_state,
        active_nav="wishlist"
    )


@app.route("/api/wishlist", methods=["GET"])
def api_mall_wishlist_get():
    u = get_current_mall_user()
    if u:
        nums = mall_database.get_user_wishlist_nums(int(u["id"]))
        prods = [_sanitize_store_product_for_view(p) for p in mall_database.get_products_by_nums(nums)]
        return jsonify({
            "status": "ok",
            "is_member": True,
            "product_nums": [p["num"] for p in prods],
            "products": prods,
            "count": len(prods),
        }), 200

    raw_nums = (request.args.get("nums") or "").strip()
    parsed = [part for part in raw_nums.split(",") if part.strip()] if raw_nums else []
    prods = [_sanitize_store_product_for_view(p) for p in mall_database.get_products_by_nums(parsed)]
    return jsonify({
        "status": "ok",
        "is_member": False,
        "product_nums": [p["num"] for p in prods],
        "products": prods,
        "count": len(prods),
    }), 200


@app.route("/api/wishlist/toggle", methods=["POST"])
def api_mall_wishlist_toggle():
    u = get_current_mall_user()
    if not u:
        return jsonify({"status": "error", "code": "AUTH_REQUIRED", "message": "로그인이 필요합니다."}), 401
    if not mall_security.verify_csrf_token(session, request):
        return jsonify({"status": "error", "code": "CSRF_INVALID", "message": "CSRF 보안 토큰 검증에 실패했습니다."}), 403

    data, err_resp = _read_json_dict()
    if err_resp:
        return err_resp
    p_num = data.get("product_num")
    if p_num is None:
        return jsonify({"status": "error", "message": "product_num이 필요합니다."}), 400
    res = mall_database.toggle_user_wishlist(int(u["id"]), p_num)
    if not res.get("ok"):
        return jsonify({"status": "error", "message": res.get("message") or "찜하기 처리 실패"}), 400
    return jsonify({"status": "ok", **res}), 200


@app.route("/api/wishlist/sync", methods=["POST"])
def api_mall_wishlist_sync():
    u = get_current_mall_user()
    if not u:
        return jsonify({"status": "error", "code": "AUTH_REQUIRED", "message": "로그인이 필요합니다."}), 401
    if not mall_security.verify_csrf_token(session, request):
        return jsonify({"status": "error", "code": "CSRF_INVALID", "message": "CSRF 보안 토큰 검증에 실패했습니다."}), 403

    data, err_resp = _read_json_dict()
    if err_resp:
        return err_resp
    nums = data.get("product_nums") or []
    merged = mall_database.merge_user_wishlist(int(u["id"]), nums if isinstance(nums, list) else [])
    return jsonify({"status": "ok", "product_nums": merged, "count": len(merged)}), 200


@app.route("/api/wishlist/clear", methods=["POST"])
def api_mall_wishlist_clear():
    u = get_current_mall_user()
    if not u:
        return jsonify({"status": "error", "code": "AUTH_REQUIRED", "message": "로그인이 필요합니다."}), 401
    if not mall_security.verify_csrf_token(session, request):
        return jsonify({"status": "error", "code": "CSRF_INVALID", "message": "CSRF 보안 토큰 검증에 실패했습니다."}), 403

    deleted = mall_database.clear_user_wishlist(int(u["id"]))
    return jsonify({"status": "ok", "deleted": deleted, "product_nums": [], "count": 0}), 200


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
        data, err_resp = _read_json_dict()
        if err_resp:
            return err_resp
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
        err_code = res.get("code")
        if err_code == "LOCKED":
            status_code = 423
        elif err_code in ("SUSPENDED", "WITHDRAWN"):
            status_code = 403
        else:
            status_code = 401
        if request.is_json:
            return jsonify({"status": "error", "code": err_code, "message": res.get("message")}), status_code
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
            "zipcode": request.args.get("zipcode", ""),
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
        form_data, err_resp = _read_json_dict()
        if err_resp:
            return err_resp
        if ("agree_terms" in form_data and not form_data.get("agree_terms")) or (
            "agree_privacy" in form_data and not form_data.get("agree_privacy")
        ):
            return jsonify({"status": "error", "message": "이용약관 및 개인정보 수집·이용에 모두 동의해 주세요."}), 400
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
        agree_terms = request.form.get("agree_terms") == "on"
        agree_privacy = request.form.get("agree_privacy") == "on"
        if not agree_terms or not agree_privacy:
            live_state = mall_database.get_full_live_state()
            return render_template(
                "auth_register.html",
                live_state=live_state,
                next_url=next_url,
                prefill=form_data,
                error="이용약관 및 개인정보 수집·이용에 모두 동의해 주세요.",
            ), 400

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
            else:
                verified_code = str(orders[0].get("order_code") or "").strip()
                if verified_code:
                    session["mall_recent_guest_order_code"] = verified_code
                    verified_list = [
                        str(c) for c in (session.get("mall_verified_guest_orders") or [])
                        if str(c).strip() and str(c) != verified_code
                    ]
                    verified_list.append(verified_code)
                    session["mall_verified_guest_orders"] = verified_list[-10:]
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


@app.route("/order-complete/<order_code>", methods=["GET"])
def mall_order_complete_page(order_code):
    """주문 완료 전용 확인 페이지 (회원 본인·관리자 또는 동일 세션 비회원 주문만 열람 허용)"""
    clean_code = str(order_code or "").strip()
    order = mall_database.get_mall_order_raw_by_code_or_id(order_code=clean_code)
    if not order:
        return "주문을 찾을 수 없습니다.", 404

    user = get_current_mall_user()
    is_authorized = False
    if user:
        if str(user.get("role") or "").upper() == "ADMIN":
            is_authorized = True
        elif order.get("user_id") is not None and int(order["user_id"]) == int(user["id"]):
            is_authorized = True

    if not is_authorized and order.get("is_guest"):
        recent_code = str(session.get("mall_recent_guest_order_code") or "").strip()
        verified_codes = [str(c).strip() for c in (session.get("mall_verified_guest_orders") or [])]
        if clean_code == recent_code or clean_code in verified_codes:
            is_authorized = True

    if not is_authorized:
        return "주문에 대한 접근 권한이 없습니다.", 403

    live_state = mall_database.get_full_live_state()
    return render_template(
        "order_complete.html",
        order=order,
        live_state=live_state,
        active_nav="orders",
    )


@app.route("/policy/<doc_type>", methods=["GET"])
def mall_policy_page(doc_type):
    """전자상거래 법정 필수 약관 및 이용 안내 페이지 (/policy/terms, /policy/privacy, /policy/guide)"""
    doc_key = str(doc_type or "").strip().lower()
    if doc_key not in ("terms", "privacy", "guide"):
        return "요청하신 약관 페이지를 찾을 수 없습니다.", 404
    live_state = mall_database.get_full_live_state()
    policy_defaults = mall_database.get_mall_policy_defaults()
    return render_template(
        "policy.html",
        doc_type=doc_key,
        policy_defaults=policy_defaults,
        live_state=live_state,
        active_nav="",
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
    hero_banners = mall_database.list_mall_hero_banners(active_only=False)
    suppliers = mall_database.list_mall_suppliers(active_only=False)
    carrier_catalog = mall_shipping.get_carrier_catalog()
    member_count = mall_member_service.count_admin_users()
    return render_template(
        "admin.html",
        products=products,
        orders=orders,
        live_state=live_state,
        all_categories=all_categories,
        policy_defaults=policy_defaults,
        hero_banners=hero_banners,
        suppliers=suppliers,
        carrier_catalog=carrier_catalog,
        member_count=member_count,
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
    category = request.args.get("category", "ALL")
    search = request.args.get("q", "")
    sort = request.args.get("sort", "num_asc")
    q_sig_payload = json.dumps(
        {
            "category": category,
            "q": search,
            "sort": sort,
            "args": sorted(request.args.items(multi=True)),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    q_sig = hashlib.sha1(q_sig_payload.encode("utf-8")).hexdigest()[:12]
    etag = f'"cat-{cat_ver}-{q_sig}"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag})

    prods = [
        _sanitize_store_product_for_view(p)
        for p in mall_database.get_all_mall_products(category=category, search=search, sort=sort)
    ]
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


@app.route("/api/search/suggest", methods=["GET"])
def api_mall_search_suggest():
    """헤더 검색창 실시간 추천 드롭다운용 경량 상품 검색 API"""
    q = (request.args.get("q") or "").strip()
    limit = mall_database._parse_clamped_query_int(request.args.get("limit"), 6, 1, 8)
    if not q:
        return jsonify({"status": "ok", "query": "", "items": [], "total": 0}), 200

    prods = mall_database.get_all_mall_products(category="ALL", search=q, sort="num_asc")
    cat_map = mall_database.get_mall_category_label_map()
    items = []
    for p in prods[:limit]:
        cat_code = str(p.get("category") or "").upper()
        items.append({
            "num": int(p.get("num") or 0),
            "name": str(p.get("name") or ""),
            "price": int(p.get("price") or 0),
            "consumer_price": int(p.get("consumer_price") or 0),
            "discount_rate": int(p.get("discount_rate") or 0),
            "image_url": str(p.get("image_url") or ""),
            "category_label": str(cat_map.get(cat_code, cat_code or "기타")),
            "total_stock": int(p.get("total_stock") or 0),
        })
    return jsonify({"status": "ok", "query": q, "items": items, "total": len(prods)}), 200


@app.route("/api/live/state-lite", methods=["GET"])
def api_mall_live_state_lite():
    """
    CSR 초경량 실시간 상태 API (~300~800B)
    - 전체 상품 배열(all_products)을 제외하고 변경 시에만 바디를 내려보냅니다 (If-None-Match ETag -> 304 Not Modified).
    """
    pull_sync_from_obs_server(force=False)
    state = mall_database.get_lite_live_state()
    if state.get("selected_product"):
        state["selected_product"] = _sanitize_public_product_detail(state["selected_product"])
    want_reviews = (request.args.get("reviews") == "1")
    etag = f'"st-{state.get("state_version", 1)}-r{1 if want_reviews else 0}"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag})

    if want_reviews and state.get("selected_product"):
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
            if state.get("selected_product"):
                state["selected_product"] = _sanitize_public_product_detail(state["selected_product"])
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
    if state.get("selected_product"):
        state["selected_product"] = _sanitize_public_product_detail(state["selected_product"])
    if isinstance(state.get("all_products"), list):
        state["all_products"] = [_sanitize_public_product_detail(p) for p in state["all_products"]]
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
    raw_sync = request.get_json(silent=True)
    if not _verify_sync_secret(raw_sync if isinstance(raw_sync, dict) else None):
        return jsonify({"status": "error", "message": "Invalid sync secret"}), 403
    data, err_resp = _read_json_dict()
    if err_resp:
        return err_resp

    action = str(data.get("action") or "state").strip().lower()

    if action == "upsert_product":
        prod = data.get("product")
        if not isinstance(prod, dict) or not prod:
            return jsonify({"status": "error", "message": "Invalid product payload"}), 400
        images_b64 = data.get("images_b64")
        if images_b64 is None:
            images_b64 = {}
        elif not isinstance(images_b64, dict):
            return jsonify({"status": "error", "message": "Invalid images_b64 payload"}), 400
        if mall_database.parse_obs_product_payload(prod) is None:
            return jsonify({"status": "error", "message": "Invalid product numeric fields"}), 400
        img_u = prod.get("image_url") or ""
        fname = os.path.basename(str(img_u).split("?")[0]) if img_u else ""
        prod["image_url"] = _copy_or_download_product_image(
            img_u,
            image_b64=data.get("image_b64") or (images_b64.get(fname) if fname else None)
        )
        _sync_product_detail_images(prod, images_b64=images_b64)
        saved = mall_database.upsert_product_from_obs(prod)
        if saved is None:
            return jsonify({"status": "error", "message": "Invalid product payload"}), 400
        return jsonify({
            "status": "ok",
            "action": "upsert_product",
            "detail_fields_supported": True,
            "product": saved
        }), 200

    elif action == "bulk_sync":
        raw_products = data.get("products")
        products = [] if raw_products is None else raw_products
        if not isinstance(products, list):
            return jsonify({"status": "error", "message": "Invalid products list"}), 400
        images_b64 = data.get("images_b64")
        if images_b64 is None:
            images_b64 = {}
        elif not isinstance(images_b64, dict):
            return jsonify({"status": "error", "message": "Invalid images_b64 payload"}), 400
        for p in products:
            if not isinstance(p, dict) or mall_database.parse_obs_product_payload(p) is None:
                return jsonify({"status": "error", "message": "Invalid product in bulk_sync payload"}), 400
        validated_live_state = None
        if "live_state" in data and data["live_state"] is not None:
            validated_live_state = mall_database.validate_live_state_bundle(data["live_state"])
            if validated_live_state is None:
                return jsonify({"status": "error", "message": "Invalid live_state payload"}), 400
        for p in products:
            img_u = p.get("image_url") or ""
            b64_data = images_b64.get(os.path.basename(str(img_u).split("?")[0])) if img_u else None
            p["image_url"] = _copy_or_download_product_image(img_u, image_b64=b64_data)
            _sync_product_detail_images(p, images_b64=images_b64)
        cnt = mall_database.bulk_sync_products_from_obs(
            products,
            delete_missing=bool(data.get("delete_missing", True))
        )
        if validated_live_state is not None:
            mall_database.update_live_state_bundle(validated_live_state)
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
            "active_product_num",
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
        validation_target = dict(live_bundle)
        for ver_key in ("state_version", "catalog_version"):
            if ver_key in data:
                validation_target[ver_key] = data[ver_key]
        validated_bundle = mall_database.validate_live_state_bundle(validation_target)
        if validated_bundle is None:
            return jsonify({"status": "error", "message": "Invalid live_state payload"}), 400
        for ver_key in ("state_version", "catalog_version"):
            if ver_key not in live_bundle:
                validated_bundle.pop(ver_key, None)
        if "product" in data and data["product"] is not None:
            p = data["product"]
            if not isinstance(p, dict) or mall_database.parse_obs_product_payload(p) is None:
                return jsonify({"status": "error", "message": "Invalid product payload"}), 400
            images_b64 = data.get("images_b64")
            if images_b64 is None:
                images_b64 = {}
            elif not isinstance(images_b64, dict):
                return jsonify({"status": "error", "message": "Invalid images_b64 payload"}), 400
            img_u = p.get("image_url") or ""
            fname = os.path.basename(str(img_u).split("?")[0]) if img_u else ""
            p["image_url"] = _copy_or_download_product_image(
                img_u,
                image_b64=data.get("image_b64") or (images_b64.get(fname) if fname else None)
            )
            _sync_product_detail_images(p, images_b64=images_b64)
            if mall_database.upsert_product_from_obs(p) is None:
                return jsonify({"status": "error", "message": "Invalid product payload"}), 400
        if validated_bundle:
            mall_database.update_live_state_bundle(validated_bundle)
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
    after_id = mall_database.parse_strict_int64(request.args.get("after_id", 0), min_val=0)
    if after_id is None:
        after_id = 0
    limit = mall_database._parse_clamped_query_int(request.args.get("limit"), 100, 1, 200)
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
    raw_sync = request.get_json(silent=True)
    has_valid_secret = bool(_get_sync_secret()) and _verify_sync_secret(
        raw_sync if isinstance(raw_sync, dict) else None
    )
    if not has_valid_secret:
        u = get_current_mall_user()
        if u and u.get("role") != "ADMIN" and not session.get("sso_opt_out"):
            studio_admin = _resolve_studio_admin_from_cookie()
            if studio_admin:
                u = _establish_mall_sso_session(
                    studio_admin,
                    auth_source="studio_cookie",
                    preserve_csrf=True,
                )
        if not u:
            return jsonify({"status": "error", "code": "AUTH_REQUIRED", "message": "쇼핑몰 관리자 로그인이 필요합니다."}), 401
        if u.get("role") != "ADMIN":
            return jsonify({"status": "error", "code": "ADMIN_REQUIRED", "message": "쇼핑몰 관리자(ADMIN) 권한이 필요합니다."}), 403
        if not mall_security.verify_csrf_token(session, request):
            return jsonify({"status": "error", "code": "CSRF_INVALID", "message": "CSRF 보안 토큰 검증에 실패했습니다."}), 403

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
    exclude_sold_out = str(request.args.get("exclude_sold_out") or "").strip().lower() in ("1", "true", "yes", "on")
    min_price_raw = (request.args.get("min_price") or "").strip()
    max_price_raw = (request.args.get("max_price") or "").strip()
    has_paging = ("page" in request.args) or ("per_page" in request.args)
    page_res = mall_database.query_mall_products_paginated(
        category=category,
        search=search,
        sort=sort,
        exclude_sold_out=exclude_sold_out,
        min_price=min_price_raw or None,
        max_price=max_price_raw or None,
        page=request.args.get("page", 1) if has_paging else 1,
        per_page=request.args.get("per_page", 12) if has_paging else None,
    )
    prods = [_sanitize_store_product_for_view(p) for p in page_res["products"]]
    return jsonify({
        "status": "ok",
        "products": prods,
        "total_count": page_res["total_count"],
        "page": page_res["page"],
        "per_page": page_res["per_page"],
        "total_pages": page_res["total_pages"],
        "has_prev": page_res["has_prev"],
        "has_next": page_res["has_next"],
    }), 200


@app.route("/api/products/<int:num>", methods=["GET"])
def api_mall_product_detail(num):
    pull_sync_from_obs_server(force=False, force_catalog=True)
    prod = mall_database.get_mall_product_by_num(num)
    if not prod:
        return jsonify({"status": "error", "message": "상품을 찾을 수 없습니다."}), 404
    prod = _sanitize_public_product_detail(prod)
    viewer = get_current_mall_user()
    reviews = mall_database.get_mall_reviews(num, viewer=viewer)
    review_summary = mall_database.get_mall_review_summary(num)
    return jsonify({"status": "ok", "product": prod, "reviews": reviews, "review_summary": review_summary}), 200


@app.route("/api/orders/create", methods=["POST"])
def api_mall_create_order():
    """
    쇼핑몰 장바구니(/checkout), 상품 상세(/product/<num>), 또는 라이브 커머스(/live) 바로구매 모달에서 주문을 생성합니다.
    - 회원 및 비회원 모두 모든 경로(MALL, PRODUCT_DETAIL, CART_CHECKOUT, LIVE)에서 주문 가능
    - 비회원 주문 시 주문 조회용 비밀번호(4자 이상) 설정 및 저장 (미입력 시 연락처 뒷 4자리 자동 폴백)
    """
    raw_data, err_resp = _read_json_dict()
    if err_resp:
        return err_resp
    data = dict(raw_data)
    source_page = str(data.get("source_page") or "MALL").strip().upper()
    user = get_current_mall_user()

    # 라이브커머스(/live) 주문: live_order_token이 전달된 경우 서명 무결성 검증 (위조 토큰 차단)
    live_tok = str(data.get("live_order_token") or "").strip()
    if live_tok and mall_security.verify_signed_token(str(app.secret_key or ""), "live_order", live_tok) is None:
        return jsonify({"status": "error", "code": "INVALID_LIVE_TOKEN", "message": "유효하지 않은 라이브 주문 보안 토큰입니다."}), 403

    if user:
        if not mall_security.verify_csrf_token(session, request):
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
        status_code = int(res.get("http_status") or 400)
        payload = {"status": "error", "message": res.get("message", "주문 실패")}
        if res.get("code"):
            payload["code"] = res["code"]
        return jsonify(payload), status_code

    if not user and res.get("order_code"):
        ocode = str(res["order_code"]).strip()
        session["mall_recent_guest_order_code"] = ocode
        vlist = [str(c) for c in (session.get("mall_verified_guest_orders") or []) if str(c).strip() and str(c) != ocode]
        vlist.append(ocode)
        session["mall_verified_guest_orders"] = vlist[-10:]

    # 로컬 동시 기동 모드일 때만 OBS 서버에 비동기로 주문 웹훅 전달 (클라우드 단독 모드에서는 에이전트가 pending-orders를 Pull)
    if _is_obs_pull_enabled():
        def _send_local_obs_order_webhook():
            try:
                webhook_payload = json.dumps({
                    "order_code": res["order_code"],
                    "buyer_name": res["buyer_name"],
                    "buyer_phone": res["buyer_phone"],
                    "zipcode": str(res.get("zipcode") or data.get("zipcode") or ""),
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

    try:
        cust_email = str((user or {}).get("email") or data.get("buyer_email") or "").strip()
        mall_notifications.notify_order_created(res, customer_email=cust_email)
    except Exception as notify_err:
        print(f"[ShoppingMall] ORDER_CREATED 알림 기록 경고: {notify_err}")

    return jsonify({
        "status": "ok",
        "ok": True,
        "order": res,
        "redirect_url": f"/order-complete/{res['order_code']}",
    }), 200


@app.route("/api/payments/prepare", methods=["POST"])
def api_mall_payments_prepare():
    """주문 결제 준비 (BANK / PORTONE_V2 / TOSS_PAYMENTS 어댑터 — 읽기 전용 SDK 파라미터 반환)"""
    data, err_resp = _read_json_dict(allow_form=True)
    if err_resp:
        return err_resp
    order, err_resp = _authorize_customer_order_mutation(data)
    if err_resp:
        return err_resp

    raw_method = data.get("payment_method") if (data.get("payment_method") is not None and str(data.get("payment_method")).strip() != "") else (order.get("payment_method") or "BANK")
    try:
        gw = mall_payment.get_payment_gateway(raw_method)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    prep = gw.prepare_payment(
        order,
        return_url=str(data.get("return_url") or ""),
        cancel_url=str(data.get("cancel_url") or ""),
    )
    if not prep.get("ok"):
        return jsonify({"status": "error", "message": prep.get("message") or "결제 준비 실패"}), 400
    return jsonify({
        **prep,
        "payment_status": prep.get("status"),
        "status": "ok",
        "ok": True,
        "payment": prep,
    }), 200


@app.route("/api/payments/confirm", methods=["POST"])
def api_mall_payments_confirm():
    """PG 결제 승인/검증 확정 (주문 결제수단 일치 필수, BANK 차단, 금액 불일치 차단)"""
    data, err_resp = _read_json_dict(allow_form=True)
    if err_resp:
        return err_resp
    order, err_resp = _authorize_customer_order_mutation(data)
    if err_resp:
        return err_resp

    try:
        order_method = mall_payment.normalize_payment_method(order.get("payment_method") or "BANK")
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    req_raw_method = data.get("payment_method")
    if req_raw_method is not None and str(req_raw_method).strip() != "":
        try:
            req_method = mall_payment.normalize_payment_method(req_raw_method)
        except ValueError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400
        if req_method != order_method:
            return jsonify({
                "status": "error",
                "code": "PAYMENT_METHOD_MISMATCH",
                "message": "요청한 결제 수단이 주문의 결제 수단과 일치하지 않습니다.",
            }), 400

    if order_method == "BANK":
        return jsonify({
            "status": "error",
            "code": "BANK_CONFIRM_NOT_ALLOWED",
            "message": "무통장 입금 주문은 PG 결제 확정을 사용할 수 없습니다.",
        }), 400

    try:
        gw = mall_payment.get_payment_gateway(order_method)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    ver = gw.verify_payment(order, data)
    if not ver.get("ok"):
        return jsonify({"status": "error", "code": ver.get("code", "PAYMENT_VERIFY_FAILED"), "message": ver.get("message") or "결제 검증 실패"}), 400

    upd = mall_database.confirm_mall_order_pg_payment(
        order_code=order["order_code"],
        pg_provider=ver.get("provider") or gw.provider_code,
        pg_tid=ver.get("pg_tid") or "",
        pg_status=ver.get("pg_status") or "PAID",
    )
    if not upd.get("ok"):
        return jsonify({"status": "error", "message": upd.get("message") or "결제 확정 반영 실패"}), 400

    updated_order = upd.get("order") or order
    if upd.get("became_paid"):
        try:
            mall_notifications.notify_payment_confirmed(updated_order)
        except Exception as notify_err:
            print(f"[ShoppingMall] PAYMENT_CONFIRMED 알림 기록 경고: {notify_err}")

    return jsonify({"status": "ok", "ok": True, "order": updated_order, "payment": ver}), 200


@app.route("/api/payments/webhook/<provider>", methods=["POST"])
def api_mall_payments_webhook(provider):
    """PG 서버-투-서버 웹훅 수신 (Fail-closed HMAC 서명 검증 필수)"""
    try:
        gw = mall_payment.get_payment_gateway(provider)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    raw_body = request.get_data() or b""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}

    ver = gw.verify_webhook(raw_body, dict(request.headers), payload)
    if not ver.get("ok"):
        return jsonify({
            "status": "error",
            "code": "WEBHOOK_SIGNATURE_INVALID",
            "message": ver.get("message") or "웹훅 서명 검증에 실패했습니다.",
        }), int(ver.get("status_code") or 401)

    order_code = str(ver.get("order_code") or "").strip()
    if not order_code:
        return jsonify({"status": "error", "message": "주문번호(order_code)가 누락되었습니다."}), 400

    order = mall_database.get_mall_order_raw_by_code_or_id(order_code=order_code)
    if not order:
        return jsonify({"status": "error", "message": "주문을 찾을 수 없습니다."}), 404

    try:
        order_method = mall_payment.normalize_payment_method(order.get("payment_method") or "BANK")
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    if order_method == "BANK":
        return jsonify({
            "status": "error",
            "code": "BANK_WEBHOOK_NOT_ALLOWED",
            "message": "무통장 입금 주문은 PG 웹훅으로 결제 처리할 수 없습니다.",
        }), 400
    if gw.provider_code != order_method:
        return jsonify({
            "status": "error",
            "code": "PAYMENT_METHOD_MISMATCH",
            "message": "웹훅 결제 수단이 주문의 결제 수단과 일치하지 않습니다.",
        }), 400

    pg_status = str(ver.get("pg_status") or "").strip().upper()
    raw_amount = ver.get("amount")
    has_amount = raw_amount is not None and str(raw_amount).strip() != ""
    if has_amount:
        try:
            wh_amount = mall_payment._parse_strict_int_amount(raw_amount)
        except ValueError:
            return jsonify({"status": "error", "code": "INVALID_AMOUNT", "message": "웹훅 결제 금액 형식이 올바르지 않습니다."}), 400
        if wh_amount != int(order.get("total_amount") or 0):
            return jsonify({"status": "error", "code": "AMOUNT_MISMATCH", "message": "웹훅 결제 금액이 주문 금액과 일치하지 않습니다."}), 400
    elif pg_status in ("PAID", "DONE"):
        return jsonify({"status": "error", "code": "MISSING_AMOUNT", "message": "웹훅 결제 금액이 누락되었습니다."}), 400

    updated_order = order
    if pg_status in ("PAID", "DONE"):
        upd = mall_database.confirm_mall_order_pg_payment(
            order_code=order_code,
            pg_provider=ver.get("provider") or gw.provider_code,
            pg_tid=ver.get("pg_tid") or "",
            pg_status="PAID",
        )
        if not upd.get("ok"):
            return jsonify({"status": "error", "message": upd.get("message") or "결제 상태 반영 실패"}), 400
        updated_order = upd.get("order") or order
        if upd.get("became_paid"):
            try:
                mall_notifications.notify_payment_confirmed(updated_order)
            except Exception as notify_err:
                print(f"[ShoppingMall] 웹훅 PAYMENT_CONFIRMED 알림 경고: {notify_err}")

    return jsonify({"status": "ok", "ok": True, "order": updated_order}), 200


@app.route("/api/orders/lookup", methods=["GET", "POST"])
def api_mall_orders_lookup():
    """
    보안 강화 주문 조회 API
    - 무인증/무조건 호출 시 타인 주문 목록 반환 금지 (0건)
    - 로그인 회원: 본인 주문 반환
    - 비회원: 주문번호(order_code) + 연락처(buyer_phone) + 비밀번호(guest_password) 3중 일치 시에만 단건 반환
    """
    user = get_current_mall_user()
    if request.is_json:
        req_data, err_resp = _read_json_dict()
        if err_resp:
            return err_resp
    else:
        req_data = {}
    order_code = str(req_data.get("order_code") or request.values.get("order_code") or "").strip()
    buyer_phone = str(req_data.get("buyer_phone") or request.values.get("buyer_phone") or "").strip()
    guest_password = str(req_data.get("guest_password") or request.values.get("guest_password") or "").strip()

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


@app.route("/api/admin/orders", methods=["GET"])
def api_mall_admin_orders_list():
    limit = mall_database._parse_clamped_query_int(request.args.get("limit"), 100, 1, 500)
    return jsonify({"status": "ok", "orders": mall_database.get_mall_orders(limit=limit)}), 200


@app.route("/api/admin/order-status", methods=["POST"])
def api_mall_admin_order_status():
    data, err_resp = _read_json_dict()
    if err_resp:
        return err_resp
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
        }), int(res.get("http_status") or 400)
    obs_restored = (
        _restore_obs_stock_for_items(res.get("restored_items") or [])
        if res.get("stock_restored_now")
        else 0
    )
    updated_order = res.get("order")
    if updated_order:
        if res.get("became_paid"):
            try:
                mall_notifications.notify_payment_confirmed(updated_order)
            except Exception as notify_err:
                print(f"[ShoppingMall] PAYMENT_CONFIRMED 알림 경고: {notify_err}")
        if res.get("became_shipped"):
            try:
                mall_notifications.notify_order_shipped(updated_order)
            except Exception as notify_err:
                print(f"[ShoppingMall] ORDER_SHIPPED 알림 경고: {notify_err}")
    mall_ops.log_current_admin_action(
        action="ORDER_STATUS_UPDATE",
        target_type="order",
        target_id=(updated_order or {}).get("order_code") or order_id,
        detail_dict={
            "order_id": order_id,
            "payment_status": (updated_order or {}).get("payment_status") or data.get("payment_status"),
            "order_status": (updated_order or {}).get("order_status") or data.get("order_status"),
            "stock_restored_now": bool(res.get("stock_restored_now")),
            "obs_stock_restored": obs_restored,
        },
    )
    return jsonify({
        "status": "ok",
        "order": updated_order,
        "stock_restored_now": bool(res.get("stock_restored_now")),
        "obs_stock_restored": obs_restored,
    }), 200


@app.route("/api/admin/orders/tracking", methods=["POST"])
def api_mall_admin_order_tracking():
    data, err_resp = _read_json_dict()
    if err_resp:
        return err_resp
    order_id = data.get("order_id")
    order_code = data.get("order_code")
    if not order_id and not str(order_code or "").strip():
        return jsonify({"status": "error", "message": "order_id 또는 order_code가 필요합니다."}), 400
    res = mall_database.update_mall_order_tracking(
        order_id=order_id,
        order_code=order_code,
        carrier_code=str(data.get("carrier_code") or ""),
        carrier_name=str(data.get("carrier_name") or ""),
        tracking_number=str(data.get("tracking_number") or ""),
        mark_shipped=bool(data.get("mark_shipped")),
    )
    if not res.get("ok"):
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "message": res.get("message") or "운송장 저장 실패"}), status
    updated_order = res.get("order")
    if updated_order and (
        res.get("became_shipped")
        or (bool(data.get("mark_shipped")) and updated_order.get("order_status") == "배송중" and bool(updated_order.get("tracking_number")))
    ):
        try:
            mall_notifications.notify_order_shipped(updated_order)
        except Exception as notify_err:
            print(f"[ShoppingMall] ORDER_SHIPPED 알림 경고: {notify_err}")
    mall_ops.log_current_admin_action(
        action="ORDER_TRACKING_UPDATE",
        target_type="order",
        target_id=(updated_order or {}).get("order_code") or order_id or order_code,
        detail_dict={
            "order_id": (updated_order or {}).get("id") or order_id,
            "carrier_code": (updated_order or {}).get("carrier_code") or data.get("carrier_code"),
            "tracking_number": (updated_order or {}).get("tracking_number") or data.get("tracking_number"),
            "mark_shipped": bool(data.get("mark_shipped")),
            "order_status": (updated_order or {}).get("order_status"),
        },
    )
    return jsonify({"status": "ok", "order": updated_order}), 200


@app.route("/api/admin/orders/export-shipping.csv", methods=["GET"])
def api_mall_admin_orders_export_shipping_csv():
    order_status = str(request.args.get("order_status") or "").strip()
    orders = mall_database.get_mall_orders_for_shipping_export(order_status=order_status, limit=500)
    csv_bytes = mall_shipping.build_shipping_export_csv(orders)
    return Response(
        csv_bytes,
        status=200,
        headers={
            "Content-Type": "text/csv; charset=utf-8-sig",
            "Content-Disposition": 'attachment; filename="mall_shipping_orders.csv"',
            "Cache-Control": "no-store",
        },
    )


@app.route("/api/admin/orders/import-tracking", methods=["POST"])
def api_mall_admin_orders_import_tracking():
    rows: list[dict] = []
    default_mark_shipped = True

    if "file" in request.files or "csv" in request.files:
        file_obj = request.files.get("file") or request.files.get("csv")
        raw_bytes = file_obj.read() if file_obj else b""
        raw_mark = (request.form.get("mark_shipped") or "").strip().lower()
        if raw_mark in ("0", "false", "no"):
            default_mark_shipped = False
        parsed = mall_shipping.parse_tracking_import_csv(raw_bytes)
        if not parsed.get("ok"):
            return jsonify({"status": "error", "message": parsed.get("message") or "CSV 파싱 실패"}), 400
        rows = parsed.get("rows") or []
    else:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"status": "error", "message": "CSV 파일 또는 JSON 데이터를 전달해 주세요."}), 400
        if "mark_shipped" in data:
            default_mark_shipped = bool(data.get("mark_shipped"))
        if isinstance(data.get("rows"), list):
            rows = data["rows"]
        elif isinstance(data.get("csv_text"), str):
            parsed = mall_shipping.parse_tracking_import_csv(data["csv_text"])
            if not parsed.get("ok"):
                return jsonify({"status": "error", "message": parsed.get("message") or "CSV 파싱 실패"}), 400
            rows = parsed.get("rows") or []
        else:
            return jsonify({"status": "error", "message": "업로드할 송장 파일 또는 rows가 필요합니다."}), 400

    res = mall_database.bulk_update_mall_order_tracking(rows, default_mark_shipped=default_mark_shipped)
    for ord_obj in (res.get("updated_orders") or []):
        if ord_obj.get("order_status") == "배송중":
            try:
                mall_notifications.notify_order_shipped(ord_obj)
            except Exception as notify_err:
                print(f"[ShoppingMall] 일괄출고 ORDER_SHIPPED 알림 경고: {notify_err}")
    mall_ops.log_current_admin_action(
        action="ORDER_TRACKING_BULK_IMPORT",
        target_type="order_batch",
        target_id=f"rows:{res.get('total_rows', 0)}",
        detail_dict={
            "total_rows": res.get("total_rows", 0),
            "updated_count": res.get("updated_count", 0),
            "error_count": res.get("error_count", 0),
            "mark_shipped": default_mark_shipped,
        },
    )
    return jsonify({
        "status": "ok",
        "total_rows": res.get("total_rows", 0),
        "updated_count": res.get("updated_count", 0),
        "error_count": res.get("error_count", 0),
        "errors": res.get("errors", []),
        "updated_orders": res.get("updated_orders", []),
    }), 200


def _authorize_customer_order_mutation(data: dict):
    """
    고객 셀프 주문 취소/교환·반품 요청 권한 검증:
    - 회원: 본인(order.user_id == user.id) + CSRF
    - 비회원: 명시적 3중 검증(order_code + buyer_phone + guest_password)
      또는 동일 브라우저 세션의 verified/recent guest order + CSRF
    - 타인/미인증 요청은 403으로 엄격 차단
    """
    user = get_current_mall_user()
    order_code = str(data.get("order_code") or "").strip()
    order_id = data.get("order_id")
    buyer_phone = str(data.get("buyer_phone") or "").strip()
    guest_password = str(data.get("guest_password") or "").strip()
    has_full_3factor = bool(order_code and buyer_phone and guest_password)

    if user or not has_full_3factor:
        if not mall_security.verify_csrf_token(session, request):
            return None, (jsonify({"status": "error", "code": "CSRF_INVALID", "message": "CSRF 보안 토큰 검증에 실패했습니다."}), 403)

    if not user or has_full_3factor:
        ip_addr = str(request.remote_addr or "unknown")
        allowed, retry_sec = mall_security.rate_limiter.check("guest_order_mutation", ip_addr, max_requests=20, window_sec=60.0)
        if not allowed:
            return None, (jsonify({"status": "error", "message": f"요청이 너무 많습니다. {retry_sec}초 후 다시 시도해 주세요."}), 429)

    order = mall_database.get_mall_order_raw_by_code_or_id(order_id=order_id, order_code=order_code)
    if not order:
        if not user:
            return None, (jsonify({"status": "error", "code": "FORBIDDEN", "message": "주문에 대한 접근 권한이 없습니다."}), 403)
        return None, (jsonify({"status": "error", "message": "주문을 찾을 수 없습니다."}), 404)

    if has_full_3factor:
        matched = mall_database.lookup_guest_mall_order(order["order_code"], buyer_phone, guest_password)
        if not matched:
            return None, (jsonify({"status": "error", "code": "FORBIDDEN", "message": "비회원 주문 검증 정보가 일치하지 않습니다."}), 403)
        return order, None

    if user:
        if order.get("user_id") is not None and int(order["user_id"]) == int(user["id"]):
            return order, None
        return None, (jsonify({"status": "error", "code": "FORBIDDEN", "message": "본인의 주문만 처리할 수 있습니다."}), 403)

    recent_code = str(session.get("mall_recent_guest_order_code") or "").strip()
    verified_codes = [str(c).strip() for c in (session.get("mall_verified_guest_orders") or [])]
    if order.get("is_guest") and (order["order_code"] == recent_code or order["order_code"] in verified_codes):
        return order, None

    return None, (jsonify({"status": "error", "code": "FORBIDDEN", "message": "비회원 주문 인증이 필요합니다."}), 403)


@app.route("/api/orders/self-cancel", methods=["POST"])
def api_mall_orders_self_cancel():
    data, err_resp = _read_json_dict(allow_form=True)
    if err_resp:
        return err_resp
    order, err_resp = _authorize_customer_order_mutation(data)
    if err_resp:
        return err_resp

    res = mall_database.cancel_mall_order_by_customer(
        order_id=order["id"],
        claim_reason=str(data.get("claim_reason") or ""),
    )
    if not res.get("ok"):
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "message": res.get("message") or "주문 취소에 실패했습니다."}), status

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


@app.route("/api/orders/self-claim", methods=["POST"])
def api_mall_orders_self_claim():
    data, err_resp = _read_json_dict(allow_form=True)
    if err_resp:
        return err_resp
    order, err_resp = _authorize_customer_order_mutation(data)
    if err_resp:
        return err_resp

    res = mall_database.submit_mall_order_claim_by_customer(
        order_id=order["id"],
        claim_type=str(data.get("claim_type") or ""),
        claim_reason=str(data.get("claim_reason") or ""),
    )
    if not res.get("ok"):
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "message": res.get("message") or "교환/반품 접수에 실패했습니다."}), status

    try:
        mall_notifications.notify_claim_requested(
            res.get("order") or order,
            claim_type=str(data.get("claim_type") or ""),
            claim_reason=str(data.get("claim_reason") or ""),
        )
    except Exception as notify_err:
        print(f"[ShoppingMall] CLAIM_REQUESTED 알림 경고: {notify_err}")

    return jsonify({"status": "ok", "order": res.get("order")}), 200


@app.route("/api/admin/notifications/logs", methods=["GET"])
def api_mall_admin_notifications_logs():
    event_type = str(request.args.get("event_type") or "")
    channel = str(request.args.get("channel") or "")
    status = str(request.args.get("status") or "")
    query = str(request.args.get("q") or "")
    limit = mall_database._parse_clamped_query_int(request.args.get("limit"), 200, 1, 500)
    logs = mall_notifications.list_notification_logs(
        event_type=event_type,
        channel=channel,
        status=status,
        query=query,
        limit=limit,
    )
    return jsonify({"status": "ok", "ok": True, "logs": logs, "count": len(logs)}), 200


@app.route("/api/admin/apply-discount", methods=["POST"])
def api_mall_admin_apply_discount():
    """모든 상품의 판매가를 소비자가 기준 지정 할인율(%)로 일괄 변경합니다 (OBS DB 포함)."""
    data, err_resp = _read_json_dict()
    if err_resp:
        return err_resp
    discount_pct = mall_database.parse_strict_int64(data.get("discount_pct", 30))
    if discount_pct is None:
        return jsonify({"status": "error", "message": "유효한 할인율 숫자를 입력해 주세요."}), 400
    discount_pct = max(0, min(95, discount_pct))

    res = mall_database.apply_mall_discount_rate(discount_pct)

    # OBS 메인 DB(live_commerce.db)도 함께 갱신하여 동기화 시 할인율이 덮어써지지 않도록 유지
    obs_updated = 0
    obs_db_path = _get_obs_db_path()
    if obs_db_path and os.path.exists(obs_db_path):
        conn = None
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
        except Exception as e:
            print(f"[ShoppingMall] OBS DB 할인율 동기화 경고: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    mall_ops.log_current_admin_action(
        action="BULK_DISCOUNT_APPLY",
        target_type="catalog",
        target_id="ALL",
        detail_dict={
            "discount_pct": res["discount_pct"],
            "updated": res["updated"],
            "obs_updated": obs_updated,
        },
    )
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
    if not _is_obs_pull_enabled() or os.getenv("MALL_PRODUCT_UPLOAD_DIR", "").strip():
        return
    if not img_url or not str(img_url).startswith("/static/uploads/products/"):
        return
    fname = os.path.basename(str(img_url).split("?")[0])
    if not fname:
        return
    src_p = os.path.join(_get_upload_products_dir(), fname)
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
    data, err_resp = _read_json_dict()
    if err_resp:
        return err_resp
    raw_cat_id = data.get("id")
    if raw_cat_id not in (None, ""):
        parsed_cat_id = mall_database.parse_strict_int64(raw_cat_id, min_val=1)
        if parsed_cat_id is None:
            return jsonify({"status": "error", "message": "유효하지 않은 카테고리 ID입니다."}), 400
        cat_id = parsed_cat_id
        res = mall_database.update_mall_category(
            cat_id=parsed_cat_id,
            name=data.get("name"),
            sort_order=data.get("sort_order"),
            is_active=data.get("is_active")
        )
    else:
        cat_id = None
        res = mall_database.create_mall_category(
            name=str(data.get("name") or ""),
            code=str(data.get("code") or ""),
            sort_order=data.get("sort_order"),
            is_active=bool(data.get("is_active", True))
        )
    if not res.get("ok"):
        return jsonify({"status": "error", "message": res.get("message", "카테고리 저장 실패")}), 400
    cats = mall_database.get_mall_categories(active_only=False)
    saved_cat = res.get("category") or {}
    mall_ops.log_current_admin_action(
        action="CATEGORY_SAVE",
        target_type="category",
        target_id=saved_cat.get("code") or res.get("code") or saved_cat.get("id") or cat_id or data.get("code") or "",
        detail_dict={
            "cat_id": saved_cat.get("id") or cat_id,
            "name": saved_cat.get("name") or data.get("name"),
            "code": saved_cat.get("code") or data.get("code"),
            "sort_order": saved_cat.get("sort_order") if saved_cat else data.get("sort_order"),
            "is_active": saved_cat.get("is_active") if saved_cat else data.get("is_active"),
        },
    )
    return jsonify({"status": "ok", "categories": cats, "result": res}), 200


@app.route("/api/admin/categories/delete", methods=["POST"])
def api_mall_admin_categories_delete():
    data, err_resp = _read_json_dict()
    if err_resp:
        return err_resp
    raw_cat_id = data.get("id")
    if raw_cat_id in (None, ""):
        return jsonify({"status": "error", "message": "삭제할 카테고리 ID가 필요합니다."}), 400
    cat_id = mall_database.parse_strict_int64(raw_cat_id, min_val=1)
    if cat_id is None:
        return jsonify({"status": "error", "message": "유효하지 않은 카테고리 ID입니다."}), 400
    res = mall_database.delete_mall_category(cat_id)
    if not res.get("ok"):
        return jsonify({"status": "error", "message": res.get("message", "카테고리 삭제 실패")}), 400
    _sync_mall_product_to_obs_db("category_delete", deleted_category=res.get("deleted_code"))
    cats = mall_database.get_mall_categories(active_only=False)
    mall_ops.log_current_admin_action(
        action="CATEGORY_DELETE",
        target_type="category",
        target_id=res.get("deleted_code") or cat_id,
        detail_dict={
            "cat_id": cat_id,
            "deleted_code": res.get("deleted_code"),
            "moved_products": res.get("moved_products", 0),
        },
    )
    return jsonify({
        "status": "ok",
        "deleted_code": res.get("deleted_code"),
        "moved_products": res.get("moved_products", 0),
        "categories": cats
    }), 200


def _validate_product_image_bytes(raw_bytes: bytes, ext: str) -> tuple[bool, str]:
    if not raw_bytes:
        return False, "빈 이미지 파일은 업로드할 수 없습니다."
    if len(raw_bytes) > _MAX_PRODUCT_IMAGE_BYTES:
        return False, "상품 이미지는 10MB 이하만 업로드할 수 있습니다."

    e = ext.lower()
    magic_ok = False
    if e == "png":
        magic_ok = raw_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    elif e in ("jpg", "jpeg", "jfif"):
        magic_ok = raw_bytes.startswith(b"\xff\xd8\xff")
    elif e == "gif":
        magic_ok = raw_bytes.startswith(b"GIF87a") or raw_bytes.startswith(b"GIF89a")
    elif e == "webp":
        magic_ok = len(raw_bytes) >= 12 and raw_bytes[:4] == b"RIFF" and raw_bytes[8:12] == b"WEBP"

    if not magic_ok:
        return False, "이미지 파일의 실제 헤더가 확장자와 일치하지 않습니다."

    try:
        from PIL import Image
        expected_fmt = "JPEG" if e in ("jpg", "jpeg", "jfif") else e.upper()
        with Image.open(io.BytesIO(raw_bytes)) as im:
            detected_fmt = str(im.format or "").upper()
            im.verify()
        if detected_fmt != expected_fmt:
            return False, "이미지 포맷 검증에 실패했습니다."
    except ImportError:
        pass
    except Exception:
        return False, "손상되었거나 유효하지 않은 이미지 파일입니다."

    return True, ""


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
        return jsonify({"status": "error", "message": "지원하지 않는 이미지 형식입니다. (jpg, jfif, png, webp, gif 가능)"}), 400
    raw_bytes = file.read(_MAX_PRODUCT_IMAGE_BYTES + 1)
    ok_img, img_err = _validate_product_image_bytes(raw_bytes, ext)
    if not ok_img:
        return jsonify({"status": "error", "message": img_err}), 400
    import uuid
    unique_name = f"prod_{int(time.time())}_{uuid.uuid4().hex[:8]}.{ext}"
    upload_dir = _get_upload_products_dir()
    os.makedirs(upload_dir, exist_ok=True)
    save_path = os.path.join(upload_dir, unique_name)
    with open(save_path, "wb") as f:
        f.write(raw_bytes)
    img_url = f"/static/uploads/products/{unique_name}"
    _copy_mall_image_to_obs(img_url)
    mall_ops.log_current_admin_action(
        action="PRODUCT_IMAGE_UPLOAD",
        target_type="media",
        target_id=unique_name,
        detail_dict={"url": img_url, "filename": unique_name},
    )
    return jsonify({"status": "ok", "url": img_url}), 200


@app.route("/api/admin/products/add", methods=["POST"])
def api_mall_admin_products_add():
    data, err_resp = _read_json_dict()
    if err_resp:
        return err_resp
    res = mall_database.create_mall_product(data)
    if not res.get("ok"):
        return jsonify({"status": "error", "message": res.get("message", "상품 등록 실패")}), 400
    prod = res.get("product")
    _sync_mall_product_to_obs_db("upsert", product=prod)
    refreshed = mall_database.get_mall_product_by_num(prod["num"]) if prod else None
    cats = mall_database.get_mall_categories(active_only=False)
    final_prod = refreshed or prod or {}
    mall_ops.log_current_admin_action(
        action="PRODUCT_CREATE",
        target_type="product",
        target_id=final_prod.get("num") or final_prod.get("id") or "",
        detail_dict={
            "id": final_prod.get("id"),
            "num": final_prod.get("num"),
            "name": final_prod.get("name"),
            "price": final_prod.get("price"),
            "category": final_prod.get("category"),
        },
    )
    return jsonify({"status": "ok", "product": refreshed or prod, "categories": cats}), 200


@app.route("/api/admin/products/update", methods=["POST"])
def api_mall_admin_products_update():
    data, err_resp = _read_json_dict()
    if err_resp:
        return err_resp
    raw_pid = data.get("id")
    if raw_pid in (None, ""):
        return jsonify({"status": "error", "message": "수정할 상품 ID가 필요합니다."}), 400
    pid = mall_database.parse_strict_int64(raw_pid, min_val=1)
    if pid is None:
        return jsonify({"status": "error", "message": "유효하지 않은 상품 ID입니다."}), 400
    res = mall_database.update_mall_product(pid, data)
    if not res.get("ok"):
        return jsonify({"status": "error", "message": res.get("message", "상품 수정 실패")}), 400
    prod = res.get("product")
    _sync_mall_product_to_obs_db("upsert", product=prod)
    refreshed = mall_database.get_mall_product_by_num(prod["num"]) if prod else None
    cats = mall_database.get_mall_categories(active_only=False)
    final_prod = refreshed or prod or {}
    mall_ops.log_current_admin_action(
        action="PRODUCT_UPDATE",
        target_type="product",
        target_id=final_prod.get("num") or pid,
        detail_dict={
            "id": final_prod.get("id") or pid,
            "num": final_prod.get("num"),
            "name": final_prod.get("name"),
            "price": final_prod.get("price"),
            "updated_fields": sorted(list(data.keys())) if isinstance(data, dict) else [],
        },
    )
    return jsonify({"status": "ok", "product": refreshed or prod, "categories": cats}), 200


@app.route("/api/admin/products/delete", methods=["POST"])
def api_mall_admin_products_delete():
    data, err_resp = _read_json_dict()
    if err_resp:
        return err_resp
    raw_pid = data.get("id")
    if raw_pid in (None, ""):
        return jsonify({"status": "error", "message": "삭제할 상품 ID가 필요합니다."}), 400
    pid = mall_database.parse_strict_int64(raw_pid, min_val=1)
    if pid is None:
        return jsonify({"status": "error", "message": "유효하지 않은 상품 ID입니다."}), 400
    res = mall_database.delete_mall_product(pid)
    if not res.get("ok"):
        return jsonify({"status": "error", "message": res.get("message", "상품 삭제 실패")}), 400
    _sync_mall_product_to_obs_db(
        "delete",
        deleted_num=res.get("deleted_num"),
        deleted_obs_id=res.get("deleted_obs_id")
    )
    cats = mall_database.get_mall_categories(active_only=False)
    mall_ops.log_current_admin_action(
        action="PRODUCT_DELETE",
        target_type="product",
        target_id=res.get("deleted_num") or pid,
        detail_dict={
            "id": pid,
            "deleted_num": res.get("deleted_num"),
            "deleted_name": res.get("deleted_name"),
        },
    )
    return jsonify({"status": "ok", "categories": cats}), 200


@app.route("/api/admin/policy-defaults", methods=["GET", "POST"])
def api_mall_admin_policy_defaults():
    if request.method == "GET":
        return jsonify({"status": "ok", "defaults": mall_database.get_mall_policy_defaults()}), 200
    data, err_resp = _read_json_dict()
    if err_resp:
        return err_resp
    saved = mall_database.set_mall_policy_defaults(data)
    mall_ops.log_current_admin_action(
        action="POLICY_DEFAULTS_UPDATE",
        target_type="store_policy",
        target_id="defaults",
        detail_dict={"updated_keys": sorted(list(data.keys())) if isinstance(data, dict) else []},
    )
    return jsonify({"status": "ok", "defaults": saved}), 200


def _parse_store_non_negative_int(raw_val, max_val: int) -> Optional[int]:
    if isinstance(raw_val, bool):
        return None
    if isinstance(raw_val, int):
        return raw_val if 0 <= raw_val <= max_val else None
    if isinstance(raw_val, float):
        if raw_val != raw_val or raw_val < 0 or raw_val > max_val or not raw_val.is_integer():
            return None
        return int(raw_val)
    if not isinstance(raw_val, str):
        return None
    cleaned = raw_val.replace(",", "").strip()
    if not cleaned or not re.fullmatch(r"[0-9]+", cleaned):
        return None
    if len(cleaned.lstrip("0") or "0") > 19:
        return None
    parsed = int(cleaned)
    return parsed if 0 <= parsed <= max_val else None


@app.route("/api/admin/store-info", methods=["GET", "POST"])
def api_mall_admin_store_info():
    if request.method == "GET":
        return jsonify({"status": "ok", "store_info": mall_database.get_mall_store_info()}), 200
    data, err_resp = _read_json_dict()
    if err_resp:
        return err_resp
    for field, max_val in (("shipping_fee", 1_000_000), ("free_shipping_threshold", 100_000_000)):
        if field in data and data.get(field) is not None and str(data.get(field)).strip() != "":
            parsed = _parse_store_non_negative_int(data.get(field), max_val)
            if parsed is None:
                return jsonify({
                    "status": "error",
                    "message": "배송비 및 무료배송 기준 금액은 0 이상의 정수로 입력해 주세요."
                }), 400
            data[field] = parsed
    if "unpaid_cancel_hours" in data and data.get("unpaid_cancel_hours") is not None and str(data.get("unpaid_cancel_hours")).strip() != "":
        parsed_hrs = _parse_store_non_negative_int(data.get("unpaid_cancel_hours"), 8760)
        if parsed_hrs is None:
            return jsonify({
                "status": "error",
                "message": "미입금 자동취소 시간은 0~8760 사이의 정수로 입력해 주세요."
            }), 400
        data["unpaid_cancel_hours"] = parsed_hrs
    if "point_earn_rate" in data and data.get("point_earn_rate") is not None and str(data.get("point_earn_rate")).strip() != "":
        raw_rate = data.get("point_earn_rate")
        if isinstance(raw_rate, bool):
            return jsonify({"status": "error", "message": "적립률(%)은 0~100 사이의 숫자로 입력해 주세요."}), 400
        try:
            rate_val = float(str(raw_rate).strip().replace(",", ""))
        except (TypeError, ValueError):
            rate_val = -1.0
        if not (0.0 <= rate_val <= 100.0) or rate_val != rate_val:
            return jsonify({"status": "error", "message": "적립률(%)은 0~100 사이의 숫자로 입력해 주세요."}), 400
        data["point_earn_rate"] = int(rate_val) if rate_val.is_integer() else round(rate_val, 2)
    saved = mall_database.set_mall_store_info(data)
    mall_ops.log_current_admin_action(
        action="STORE_INFO_UPDATE",
        target_type="store_info",
        target_id="store_info",
        detail_dict={
            "updated_keys": sorted(list(data.keys())),
            "shipping_fee": saved.get("shipping_fee"),
            "free_shipping_threshold": saved.get("free_shipping_threshold"),
            "unpaid_cancel_hours": saved.get("unpaid_cancel_hours"),
            "point_earn_rate": saved.get("point_earn_rate"),
        },
    )
    return jsonify({"status": "ok", "store_info": saved}), 200


@app.route("/api/admin/banners", methods=["GET"])
def api_mall_admin_banners_list():
    return jsonify({"status": "ok", "banners": mall_database.list_mall_hero_banners(active_only=False)}), 200


@app.route("/api/admin/banners/save", methods=["POST"])
def api_mall_admin_banners_save():
    data, err_resp = _read_json_dict()
    if err_resp:
        return err_resp
    res = mall_database.save_mall_hero_banner(data)
    if not res.get("ok"):
        return jsonify({"status": "error", "message": res.get("message", "배너 저장에 실패했습니다.")}), 400
    banner = res.get("banner") or {}
    mall_ops.log_current_admin_action(
        action="BANNER_SAVE",
        target_type="banner",
        target_id=banner.get("id") or "",
        detail_dict={
            "id": banner.get("id"),
            "title": banner.get("title"),
            "theme": banner.get("theme"),
            "is_active": banner.get("is_active"),
        },
    )
    return jsonify({"status": "ok", "banner": res.get("banner"), "banners": res.get("banners", [])}), 200


@app.route("/api/admin/banners/toggle", methods=["POST"])
def api_mall_admin_banners_toggle():
    data, err_resp = _read_json_dict()
    if err_resp:
        return err_resp
    bid = mall_database.parse_strict_int64(data.get("id"), min_val=1)
    if bid is None:
        return jsonify({"status": "error", "message": "올바른 배너 ID가 필요합니다."}), 400
    res = mall_database.toggle_mall_hero_banner(bid, data.get("is_active"))
    if not res.get("ok"):
        return jsonify({"status": "error", "message": res.get("message", "배너 상태 변경에 실패했습니다.")}), 404
    mall_ops.log_current_admin_action(
        action="BANNER_TOGGLE",
        target_type="banner",
        target_id=bid,
        detail_dict={"id": bid, "is_active": res.get("is_active", data.get("is_active"))},
    )
    return jsonify({"status": "ok", "banners": res.get("banners", [])}), 200


@app.route("/api/admin/banners/delete", methods=["POST"])
def api_mall_admin_banners_delete():
    data, err_resp = _read_json_dict()
    if err_resp:
        return err_resp
    bid = mall_database.parse_strict_int64(data.get("id"), min_val=1)
    if bid is None:
        return jsonify({"status": "error", "message": "올바른 배너 ID가 필요합니다."}), 400
    res = mall_database.delete_mall_hero_banner(bid)
    if not res.get("ok"):
        return jsonify({"status": "error", "message": res.get("message", "배너 삭제에 실패했습니다.")}), 404
    mall_ops.log_current_admin_action(
        action="BANNER_DELETE",
        target_type="banner",
        target_id=bid,
        detail_dict={"id": bid},
    )
    return jsonify({"status": "ok", "banners": res.get("banners", [])}), 200


@app.route("/api/admin/sales/summary", methods=["GET"])
def api_mall_admin_sales_summary():
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    include_pending = str(request.args.get("include_pending", "")).strip().lower() in ("1", "true", "yes")
    raw_sup = request.args.get("supplier_id", "").strip()
    if raw_sup:
        sup_id = mall_database.parse_strict_int64(raw_sup, min_val=1)
        if sup_id is None:
            return jsonify({"status": "error", "message": "유효하지 않은 매입업체 ID입니다."}), 400
    else:
        sup_id = None
    summary = mall_database.get_mall_sales_summary(
        start_date=start_date,
        end_date=end_date,
        include_pending=include_pending,
        supplier_id=sup_id,
    )
    return jsonify({"status": "ok", **summary}), 200


@app.route("/api/admin/sales/export.csv", methods=["GET"])
def api_mall_admin_sales_export_csv():
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    include_pending = str(request.args.get("include_pending", "")).strip().lower() in ("1", "true", "yes")
    raw_sup = request.args.get("supplier_id", "").strip()
    if raw_sup:
        sup_id = mall_database.parse_strict_int64(raw_sup, min_val=1)
        if sup_id is None:
            return jsonify({"status": "error", "message": "유효하지 않은 매입업체 ID입니다."}), 400
    else:
        sup_id = None
    summary = mall_database.get_mall_sales_summary(
        start_date=start_date,
        end_date=end_date,
        include_pending=include_pending,
        supplier_id=sup_id,
    )
    _sc = mall_shipping._sanitize_csv_cell
    kpi = summary["kpi"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["[매출관리 요약]"])
    writer.writerow([
        "집계 기준",
        "입금대기 포함" if include_pending else "입금완료 기준",
        "시작일",
        _sc(start_date) or "전체",
        "종료일",
        _sc(end_date) or "전체",
    ])
    writer.writerow(["상품 판매대금(원)", "총 매입대금(원)", "매출총이익(원)", "마진율(%)", "택배비 매출 별도(원)", "업체 지급완료(원)", "업체 미지급 잔액(원)", "유효 주문수", "총 판매수량"])
    writer.writerow([
        kpi["sales_amount"], kpi["purchase_cost"], kpi["gross_profit"], kpi["margin_rate"],
        kpi["shipping_amount"], kpi["total_settled"], kpi["total_unpaid"], kpi["order_count"], kpi["sold_qty"],
    ])
    writer.writerow([])
    writer.writerow(["[매입업체별 정산 현황]"])
    writer.writerow(["매입업체명", "판매수량", "판매대금(원)", "매입대금 정산예정(원)", "지급완료액(원)", "미지급 잔액(원)", "은행", "계좌번호", "예금주"])
    for sr in summary["supplier_rows"]:
        writer.writerow([
            _sc(sr["supplier_name"]), sr["sold_qty"], sr["sales_amount"], sr["purchase_cost"],
            sr["settled_amount"], sr["unpaid_amount"],
            _sc(sr["bank_name"]), _sc(sr["bank_account"]), _sc(sr["bank_holder"]),
        ])
    writer.writerow([])
    writer.writerow(["[상품별 매출 및 마진]"])
    writer.writerow(["상품번호", "상품명", "매입업체", "판매수량", "판매대금(원)", "매입대금(원)", "매출총이익(원)", "마진율(%)"])
    for pr in summary["product_rows"]:
        writer.writerow([
            _sc(f"#{pr['product_num']}"), _sc(pr["product_name"]), _sc(pr["supplier_name"]),
            pr["sold_qty"], pr["sales_amount"], pr["purchase_cost"], pr["gross_profit"], pr["margin_rate"],
        ])
    writer.writerow([])
    writer.writerow(["[일별 매출 추이]"])
    writer.writerow(["일자", "주문수", "판매수량", "상품 판매대금(원)", "택배비(원)", "매입대금(원)", "매출총이익(원)", "마진율(%)"])
    for dr in summary["daily_rows"]:
        writer.writerow([
            _sc(dr["date"]), dr["order_count"], dr["sold_qty"], dr["sales_amount"],
            dr["shipping_amount"], dr["purchase_cost"], dr["gross_profit"], dr["margin_rate"],
        ])

    csv_bytes = ("\ufeff" + buf.getvalue()).encode("utf-8")
    return Response(
        csv_bytes,
        status=200,
        headers={
            "Content-Type": "text/csv; charset=utf-8-sig",
            "Content-Disposition": 'attachment; filename="mall_sales_report.csv"',
            "Cache-Control": "no-store",
        },
    )


@app.route("/api/admin/suppliers", methods=["GET"])
def api_mall_admin_suppliers_list():
    return jsonify({"status": "ok", "suppliers": mall_database.list_mall_suppliers(active_only=False)}), 200


@app.route("/api/admin/suppliers/save", methods=["POST"])
def api_mall_admin_suppliers_save():
    data, err_resp = _read_json_dict()
    if err_resp:
        return err_resp
    res = mall_database.save_mall_supplier(data)
    if not res.get("ok"):
        return jsonify({"status": "error", "message": res.get("message", "매입업체 저장에 실패했습니다.")}), 400
    sup = res.get("supplier") or {}
    mall_ops.log_current_admin_action(
        action="SUPPLIER_SAVE",
        target_type="supplier",
        target_id=sup.get("id") or "",
        detail_dict={
            "id": sup.get("id"),
            "name": sup.get("name"),
            "is_active": sup.get("is_active"),
        },
    )
    return jsonify({"status": "ok", "supplier": res.get("supplier"), "suppliers": res.get("suppliers", [])}), 200


@app.route("/api/admin/suppliers/delete", methods=["POST"])
def api_mall_admin_suppliers_delete():
    data, err_resp = _read_json_dict()
    if err_resp:
        return err_resp
    sid = mall_database.parse_strict_int64(data.get("id"), min_val=1)
    if sid is None:
        return jsonify({"status": "error", "message": "올바른 매입업체 ID가 필요합니다."}), 400
    res = mall_database.delete_mall_supplier(sid)
    if not res.get("ok"):
        return jsonify({"status": "error", "message": res.get("message", "매입업체 삭제에 실패했습니다.")}), 404
    mall_ops.log_current_admin_action(
        action="SUPPLIER_DELETE",
        target_type="supplier",
        target_id=sid,
        detail_dict={"id": sid},
    )
    return jsonify({"status": "ok", "suppliers": res.get("suppliers", [])}), 200


@app.route("/api/admin/suppliers/settlements/add", methods=["POST"])
def api_mall_admin_supplier_settlement_add():
    data, err_resp = _read_json_dict()
    if err_resp:
        return err_resp
    res = mall_database.add_mall_supplier_settlement(data)
    if not res.get("ok"):
        return jsonify({"status": "error", "message": res.get("message", "지급 내역 등록에 실패했습니다.")}), 400
    settle = res.get("settlement") or {}
    mall_ops.log_current_admin_action(
        action="SUPPLIER_SETTLEMENT_ADD",
        target_type="settlement",
        target_id=settle.get("id") or "",
        detail_dict={
            "supplier_id": settle.get("supplier_id"),
            "amount": settle.get("amount"),
            "paid_at": settle.get("paid_at"),
            "payment_method": settle.get("payment_method"),
        },
    )
    return jsonify({"status": "ok", "settlement": res.get("settlement")}), 200


@app.route("/api/admin/suppliers/settlements/delete", methods=["POST"])
def api_mall_admin_supplier_settlement_delete():
    data, err_resp = _read_json_dict()
    if err_resp:
        return err_resp
    settle_id = mall_database.parse_strict_int64(data.get("id"), min_val=1)
    if settle_id is None:
        return jsonify({"status": "error", "message": "올바른 지급 이력 ID가 필요합니다."}), 400
    res = mall_database.delete_mall_supplier_settlement(settle_id)
    if not res.get("ok"):
        return jsonify({"status": "error", "message": res.get("message", "지급 이력 삭제에 실패했습니다.")}), 404
    mall_ops.log_current_admin_action(
        action="SUPPLIER_SETTLEMENT_DELETE",
        target_type="settlement",
        target_id=settle_id,
        detail_dict={"id": settle_id},
    )
    return jsonify({"status": "ok"}), 200


@app.route("/sitemap.xml", methods=["GET"])
def mall_sitemap_xml():
    """검색엔진 색인용 동적 XML Sitemap (/sitemap.xml)"""
    base = resolve_mall_public_url().rstrip("/")
    urls: list[tuple[str, str, str, str]] = [
        (f"{base}/", "", "daily", "1.0"),
        (f"{base}/cs", "", "weekly", "0.7"),
        (f"{base}/policy/terms", "", "monthly", "0.4"),
        (f"{base}/policy/privacy", "", "monthly", "0.4"),
        (f"{base}/policy/guide", "", "monthly", "0.4"),
    ]
    for prod in mall_database.get_all_mall_products():
        p_num = prod.get("num")
        if p_num is None:
            continue
        upd = str(prod.get("updated_at") or "").strip()[:10]
        urls.append((f"{base}/product/{int(p_num)}", upd, "daily", "0.8"))

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for loc, lastmod, changefreq, priority in urls:
        lines.append("  <url>")
        lines.append(f"    <loc>{_xml_escape(loc)}</loc>")
        if lastmod:
            lines.append(f"    <lastmod>{_xml_escape(lastmod)}</lastmod>")
        lines.append(f"    <changefreq>{changefreq}</changefreq>")
        lines.append(f"    <priority>{priority}</priority>")
        lines.append("  </url>")
    lines.append("</urlset>")
    return Response("\n".join(lines), status=200, mimetype="application/xml; charset=utf-8")


@app.route("/robots.txt", methods=["GET"])
def mall_robots_txt():
    """검색엔진 크롤링 정책 (/robots.txt)"""
    base = resolve_mall_public_url().rstrip("/")
    body = "\n".join([
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin",
        "Disallow: /mypage",
        "Disallow: /orders",
        "Disallow: /cart",
        "Disallow: /checkout",
        "Disallow: /auth/",
        "Disallow: /api/",
        f"Sitemap: {base}/sitemap.xml",
        "",
    ])
    return Response(body, status=200, mimetype="text/plain; charset=utf-8")


maybe_start_mall_scheduler()


if __name__ == "__main__":
    maybe_start_mall_scheduler()
    print("=" * 68)
    print(f"🛍️ [독립 쇼핑몰 서버 기동] http://localhost:{MALL_PORT}")
    print(f"🔴 [라이브 커머스 전용관] http://localhost:{MALL_PORT}/live")
    print(f"💾 [쇼핑몰 전용 DB 경로] {mall_database.MALL_DB_PATH}")
    print(f"🔗 [OBS 연동 서버 주소] {OBS_SERVER_URL}")
    print("=" * 68)
    app.run(host="0.0.0.0", port=MALL_PORT, debug=False, threaded=True)
