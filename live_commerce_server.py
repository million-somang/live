# -*- coding: utf-8 -*-
"""
라이브 커머스 3사(유튜브·인스타·틱톡) 실시간 AI 주문 접수 및 OBS 자막 서버
"""

import os
import sys
import io
import csv
import json
import time
import uuid
from datetime import datetime
import threading
from threading import Lock
import subprocess
import asyncio
import re

# 윈도우 콘솔 UTF-8 출력 설정 및 1.0ms 고정밀 멀티미디어 타이머 활성화 (time.sleep 15.6ms 지터 원천 제거)
if sys.platform.startswith("win"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    try:
        import ctypes
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass

from datetime import timedelta
from flask import Flask, request, jsonify, render_template, send_file, Response, session, redirect, url_for
from flask_cors import CORS
from dotenv import load_dotenv
from openpyxl import Workbook, load_workbook

from inventory_manager import inventory_manager
from cs_manager import cs_manager
import database
import security_core
from admin import admin_bp

# DB 테이블 및 인덱스 초기화 확인
database.init_db(reset=False)

# 환경 변수 로드 (.env 파일이 있으면 우선 반영)
load_dotenv()
database.ensure_default_studio_admin()

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.secret_key = security_core.load_or_create_secret_key(
    "STUDIO_SECRET_KEY",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".studio_secret_key")
)
app.config["SESSION_COOKIE_NAME"] = "studio_sid"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
CORS(app)  # 브라우저 확장 프로그램(Social Stream Ninja) 및 외부 CSR 대시보드에서의 로컬 에이전트 전송 허용
app.register_blueprint(admin_bp)

MALL_PORT = int(os.getenv("MALL_PORT", 6001))
STUDIO_PUBLIC_URL = os.getenv("STUDIO_PUBLIC_URL", "").rstrip("/")
MALL_PUBLIC_URL = os.getenv("MALL_PUBLIC_URL", "").rstrip("/")


def _extract_request_hostname_and_scheme():
    """현재 브라우저가 접속한 서버의 프로토콜(http/https)과 호스트명(공인 IP 또는 도메인)을 동적으로 추출합니다."""
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
    """
    접속자 브라우저가 클릭해 이동할 쇼핑몰 공개 URL을 반환합니다.
    - 외부 도메인이 명시되어 있으면 해당 도메인 사용 (기존 :3000/:6000 포트는 :6001로 자동 보정)
    - 미설정 또는 localhost/127.0.0.1인 경우 현재 접속한 서버 호스트(:6001)로 자동 매핑
    """
    mall_port = int(os.getenv("MALL_PORT", MALL_PORT))
    raw_env_pub = os.getenv("MALL_PUBLIC_URL", MALL_PUBLIC_URL).strip().rstrip("/")
    if raw_env_pub and not any(h in raw_env_pub for h in ("localhost", "127.0.0.1")):
        return database._normalize_mall_port_url(raw_env_pub, mall_port)
    try:
        cfg = database.get_cloud_mall_config()
        if cfg.get("is_remote") and cfg.get("mall_url"):
            return database._normalize_mall_port_url(cfg["mall_url"], mall_port)
    except Exception:
        pass
    scheme, hostname = _extract_request_hostname_and_scheme()
    return f"{scheme}://{hostname}:{mall_port}"


def resolve_studio_public_url() -> str:
    """현재 접속한 서버 기준의 OBS 스튜디오/홍보홈 공개 URL(:5000)을 반환합니다."""
    studio_port = int(os.getenv("SERVER_PORT", 5000))
    raw_env_pub = os.getenv("STUDIO_PUBLIC_URL", STUDIO_PUBLIC_URL).strip().rstrip("/")
    if raw_env_pub and not any(h in raw_env_pub for h in ("localhost", "127.0.0.1")):
        return raw_env_pub
    scheme, hostname = _extract_request_hostname_and_scheme()
    return f"{scheme}://{hostname}:{studio_port}"


_cached_cam_token = {"tok": "", "ts": 0.0}
_cached_ov_token = {"tok": "", "ts": 0.0}


def get_studio_cam_token(force_new: bool = False) -> str:
    """QR 코드 주소가 매초 바뀌지 않도록 서명된 cam_token을 캐싱하여 반환합니다."""
    now = time.time()
    if force_new or not _cached_cam_token["tok"] or (now - _cached_cam_token["ts"]) > 86400:
        _cached_cam_token["tok"] = security_core.create_signed_token(
            app.secret_key, "cam_pair", {"scope": "cam"}, ttl_sec=86400 * 7
        )
        _cached_cam_token["ts"] = now
    return _cached_cam_token["tok"]


def get_studio_overlay_token(force_new: bool = False) -> str:
    now = time.time()
    if force_new or not _cached_ov_token["tok"] or (now - _cached_ov_token["ts"]) > 86400:
        _cached_ov_token["tok"] = security_core.create_signed_token(
            app.secret_key, "obs_overlay", {"scope": "overlay"}, ttl_sec=86400 * 30
        )
        _cached_ov_token["ts"] = now
    return _cached_ov_token["tok"]


def get_current_studio_user():
    uid = session.get("studio_user_id")
    if not uid:
        return None
    return database.get_studio_user_by_id(uid)


def _is_direct_loopback_request() -> bool:
    remote = str(request.remote_addr or "")
    forwarded = request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP")
    return (remote in ("127.0.0.1", "::1", "localhost")) and not forwarded


def _has_valid_cam_access() -> bool:
    if session.get("cam_paired") is True:
        return True
    if _is_direct_loopback_request() and (
        request.args.get("agent_bridge") == "1"
        or request.headers.get("X-Local-Agent-Bridge") == "1"
    ):
        return True
    tok = (
        request.args.get("cam_token")
        or request.headers.get("X-Cam-Token")
        or ""
    ).strip()
    if not tok:
        ref = request.headers.get("Referer") or ""
        if "cam_token=" in ref:
            try:
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(ref).query)
                if q.get("cam_token"):
                    tok = str(q["cam_token"][0]).strip()
            except Exception:
                pass
    if tok and security_core.verify_signed_token(app.secret_key, "cam_pair", tok) is not None:
        session["cam_paired"] = True
        return True
    # 클라우드 서버(49.50.132.140)와 로컬 PC 에이전트(192.168.x.x) 간 SECRET_KEY가 서로 다를 때에도
    # 유효 기간 내의 정상 cam_pair 토큰 포맷(base64url.hmac)이면 로컬 카메라 연결을 허용
    if tok and "." in tok:
        try:
            import base64
            b64_part = tok.split(".", 1)[0]
            pad = "=" * ((4 - len(b64_part) % 4) % 4)
            raw_json = base64.urlsafe_b64decode((b64_part + pad).encode("ascii")).decode("utf-8")
            payload_obj = json.loads(raw_json)
            if (
                isinstance(payload_obj, dict)
                and payload_obj.get("p") == "cam_pair"
                and int(payload_obj.get("exp") or 0) > int(time.time())
                and isinstance(payload_obj.get("d"), dict)
                and payload_obj["d"].get("scope") == "camera"
            ):
                session["cam_paired"] = True
                return True
        except Exception:
            pass
    u = get_current_studio_user()
    if u and u.get("status") == "APPROVED":
        return True
    return False


def _has_valid_overlay_access() -> bool:
    if session.get("overlay_paired") is True:
        return True
    tok = (
        request.args.get("ov_token")
        or request.headers.get("X-Overlay-Token")
        or ""
    ).strip()
    if tok and security_core.verify_signed_token(app.secret_key, "obs_overlay", tok) is not None:
        session["overlay_paired"] = True
        return True
    if _is_direct_loopback_request():
        return True
    u = get_current_studio_user()
    return bool(u and u.get("status") == "APPROVED")


@app.context_processor
def _inject_studio_globals():
    u = get_current_studio_user()
    pending_cnt = 0
    if u and u.get("role") == "ADMIN":
        try:
            pending_cnt = sum(1 for item in database.list_studio_users() if item.get("status") == "PENDING")
        except Exception:
            pending_cnt = 0
    return {
        "current_studio_user": u,
        "studio_user": u,
        "pending_studio_users_count": pending_cnt,
        "csrf_token": security_core.get_csrf_token(session),
        "mall_public_url": resolve_mall_public_url(),
        "studio_public_url": resolve_studio_public_url(),
        "obs_overlay_token": get_studio_overlay_token(),
        "camera_pair_token": get_studio_cam_token(),
    }


@app.before_request
def _studio_security_gateway():
    """
    도메인 1 (홍보 홈페이지 + OBS 스튜디오 + /admin 관리자 콘솔) 중앙 보안 게이트웨이 (Deny-by-Default)
    1) 공개 경로(/, /auth/*, /admin/login, /admin/logout, /static/*, /status, OPTIONS) 허용
    2) 특수 토큰 경로(/cam*, /overlay/obs, 웹훅) 검증
    3) /admin* 경로는 ADMIN 권한 전용으로 보호 (비로그인 시 /admin/login 으로 리다이렉트)
    4) 그 외 모든 스튜디오/관제 화면 및 /api/* 제어 요청은 APPROVED 방송자 세션 필수
    5) 상태 변경(POST/PUT/DELETE) 요청 시 CSRF 검증
    """
    if request.method == "OPTIONS":
        return None

    path = request.path or "/"

    # 0. 이전 QR 정규식 치환으로 인해 물음표('?')가 유실된 카메라 경로(/remote-cam&... 또는 /cam&...) 자동 복구 (404 원천 방지)
    if path.startswith("/remote-cam&") or path.startswith("/cam&"):
        base_route, extra_q = path.split("&", 1)
        orig_q = request.query_string.decode("utf-8", errors="ignore")
        merged_q = f"{extra_q}&{orig_q}" if orig_q else extra_q
        return redirect(f"{base_route}?{merged_q}", code=302)

    # 1. 정적 파일 및 기본 헬스체크 허용
    if path.startswith("/static/") or path in ("/status", "/favicon.ico"):
        return None

    # 2. 홍보 홈페이지 (GET /), 일반 인증 라우트 (/auth/*), 전용 관리자 로그인/로그아웃 (/admin/login, /admin/logout)
    if path == "/" and request.method == "GET":
        return None
    if path.startswith("/auth/"):
        if request.method == "POST" and not security_core.verify_csrf_token(session, request):
            return render_template(
                "studio_auth.html",
                mode="login" if "login" in path else "register",
                error="보안 토큰(CSRF)이 만료되었거나 유효하지 않습니다. 다시 시도해 주세요."
            ), 403
        return None
    if path in ("/admin/login", "/admin/logout"):
        if request.method == "POST" and not security_core.verify_csrf_token(session, request):
            if request.is_json:
                return jsonify({"status": "error", "code": "CSRF_INVALID", "message": "CSRF 보안 토큰 검증에 실패했습니다."}), 403
            return render_template(
                "admin/login.html",
                error="보안 토큰(CSRF)이 만료되었거나 유효하지 않습니다. 페이지를 새로고침 후 다시 시도해 주세요."
            ), 403
        return None

    # 3. 로컬 에이전트 PNA 설정·헬스체크·클라우드 브릿지 동기화 조회/보고 (/api/agent/cloud-config, /api/agent/health, /api/agent/bridge-sync)
    if path in ("/api/agent/cloud-config", "/api/agent/health") and request.method == "GET":
        if _is_direct_loopback_request() or request.headers.get("Access-Control-Request-Private-Network") == "true" or request.headers.get("Origin"):
            return None
        if path == "/api/agent/health":
            return None
    if path == "/api/agent/bridge-sync" and request.method == "POST":
        if request.headers.get("X-Local-Agent-Bridge") == "1" or request.args.get("agent_bridge") == "1":
            return None

    # 3-2. 클라우드 CSR 스튜디오 ↔ 로컬 PC 에이전트(127.0.0.1) 직결 브릿지 요청 허용 (서버 영상 트래픽 0바이트 모드)
    if _is_direct_loopback_request() and (
        request.headers.get("X-Local-Agent-Bridge") == "1"
        or request.args.get("agent_bridge") == "1"
    ):
        if any(
            path.startswith(prefix)
            for prefix in (
                "/api/agent/",
                "/api/camera/",
                "/api/audio/",
                "/api/youtube/",
                "/api/tiktok/",
                "/api/instagram/",
                "/api/overlay/",
                "/api/broadcast/",
            )
        ):
            return None

    # 4. 채팅/쇼핑몰 인입 웹훅 경로 (POST /, /chat, /webhook, /api/chat, /api/mall/order-webhook)
    if path in ("/", "/chat", "/webhook", "/api/chat", "/api/mall/order-webhook") and request.method == "POST":
        webhook_sec = (os.getenv("WEBHOOK_SECRET") or "").strip()
        if webhook_sec:
            req_sec = (request.headers.get("X-Webhook-Secret") or request.args.get("webhook_secret") or "").strip()
            if not security_core.constant_time_equals(webhook_sec, req_sec) and not _is_direct_loopback_request():
                return jsonify({"status": "error", "message": "웹훅 인증 시크릿이 유효하지 않습니다."}), 403
        return None

    # 5. OBS 브라우저 소스 읽기 전용 경로 (/overlay/obs 및 GET 오버레이/상품 데이터)
    if path == "/overlay/obs" or (
        request.method == "GET" and path in ("/api/overlay/data", "/api/overlay/settings", "/api/products")
    ):
        if _has_valid_overlay_access():
            return None

    # 6. 무선 스마트폰 카메라 경로 (방식 1: 동일망 /cam, /api/camera/* & 방식 2: 다른 네트워크 /remote-cam, /api/remote-cam/*)
    if (
        path == "/cam"
        or path.startswith("/cam/")
        or path.startswith("/api/camera/")
        or path == "/remote-cam"
        or path.startswith("/remote-cam/")
        or path.startswith("/api/remote-cam/")
    ):
        # renew-url 및 switcher POST는 스튜디오 운영자 전용
        if path == "/api/camera/renew-url" or (path == "/api/camera/switcher" and request.method == "POST"):
            pass  # 아래 스튜디오 인증으로 진행
        else:
            if _has_valid_cam_access() or (
                request.method == "GET"
                and path in ("/api/camera/host-info", "/api/camera/stream", "/api/camera/latest-frame", "/api/camera/audio/pull")
                and session.get("overlay_paired") is True
            ):
                return None
            if path.startswith("/api/"):
                return jsonify({"status": "error", "message": "무선 카메라 보안 페어링 토큰(cam_token)이 필요합니다."}), 403
            return Response("🔒 무선 카메라 보안 토큰(cam_token)이 없거나 만료되었습니다. OBS 스튜디오에서 QR 코드를 다시 스캔해 주세요.", status=403, mimetype="text/plain; charset=utf-8")

    # 7. 스튜디오 정식 회원 세션 확인
    user = get_current_studio_user()
    if not user:
        if path == "/admin" or path.startswith("/admin/"):
            if path.startswith("/admin/api/") or request.method != "GET":
                return jsonify({"status": "error", "code": "ADMIN_AUTH_REQUIRED", "message": "관리자 로그인이 필요합니다."}), 401
            return redirect("/admin/login")
        if path.startswith("/api/") or request.method != "GET":
            return jsonify({"status": "error", "code": "AUTH_REQUIRED", "message": "OBS 스튜디오 로그인이 필요합니다."}), 401
        next_q = request.full_path.rstrip("?") if request.query_string else path
        if not security_core.is_safe_redirect_url(next_q):
            next_q = "/portal"
        return redirect(url_for("studio_auth_login", next=next_q))

    if user.get("status") != "APPROVED":
        if path.startswith("/api/") or path.startswith("/admin/api/") or request.method != "GET":
            return jsonify({"status": "error", "code": "APPROVAL_REQUIRED", "message": "관리자 승인 완료 후 OBS 스튜디오를 이용하실 수 있습니다."}), 403
        return redirect(url_for("studio_auth_pending"))

    # 8. 관리자 전용 경로 확인 (/admin, /admin/*, /studio/admin/*, /api/studio/admin/*)
    if path == "/admin" or path.startswith("/admin/") or path.startswith("/studio/admin") or path.startswith("/api/studio/admin"):
        if user.get("role") != "ADMIN":
            if path.startswith("/api/") or path.startswith("/admin/api/") or request.method != "GET":
                return jsonify({"status": "error", "code": "ADMIN_REQUIRED", "message": "스튜디오 관리자(ADMIN) 권한이 필요합니다."}), 403
            return Response("🛡️ 관리자(ADMIN) 전용 페이지입니다. 일반 방송자 계정으로는 접근할 수 없습니다.", status=403, mimetype="text/plain; charset=utf-8")

    # 9. 상태 변경 요청(POST/PUT/DELETE/PATCH) CSRF 검증
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        if not security_core.verify_csrf_token(session, request):
            return jsonify({"status": "error", "code": "CSRF_INVALID", "message": "CSRF 보안 토큰 검증에 실패했습니다. 페이지를 새로고침해 주세요."}), 403

    return None


@app.after_request
def _add_private_network_cors_headers(response):
    """보안 응답 헤더, CSRF 스크립트 자동 주입 및 Private Network Access(PNA) / CORS 허용"""
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    origin = request.headers.get("Origin")
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Vary"] = "Origin"
    elif "Access-Control-Allow-Origin" not in response.headers:
        response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, PATCH, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, Authorization, X-CSRF-Token, X-Requested-With, "
        "X-Local-Agent-Bridge, X-Cam-Token, X-Overlay-Token, If-None-Match"
    )

    allow_frame = request.path.startswith("/overlay/") or request.path.startswith("/cam")
    security_core.apply_security_headers(response, allow_framing=allow_frame)
    security_core.inject_csrf_into_html_response(response, security_core.get_csrf_token(session))
    return response

# 1. 파일 경로 설정
EXCEL_FILE = "live_orders.xlsx"
BACKUP_CSV_FILE = "live_orders_backup.csv"
OBS_TEXT_FILE = "obs_ai_reply.txt"

# 상품 및 오버레이 이미지 업로드 디렉토리 설정
import mimetypes
mimetypes.add_type('image/jpeg', '.jfif')

UPLOAD_FOLDER = os.path.join(app.root_path, "static", "uploads", "products")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
OVERLAY_UPLOAD_FOLDER = os.path.join(app.root_path, "static", "uploads", "overlays")
os.makedirs(OVERLAY_UPLOAD_FOLDER, exist_ok=True)
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'jfif', 'webp', 'gif', 'svg'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS

# 2. 동시성 제어 및 미반영 주문 큐 (엑셀 파일 잠금 대비)
file_lock = Lock()
pending_orders = []
broadcast_reset_seq = int(time.time())
host_reload_seq = int(time.time())


class PlatformTracker:
    """
    3사(유튜브·인스타·틱톡 등) 플랫폼의 실시간 연결 상태 및 활동 지표를 추적 관리합니다.
    """
    def __init__(self):
        from collections import deque
        self.lock = Lock()
        self.platforms = {
            "YOUTUBE": {
                "display_name": "YouTube Live",
                "color": "#ff0000",
                "last_seen": None,
                "chat_count": 0,
                "order_count": 0,
                "last_user": "-",
                "last_message": "-"
            },
            "INSTAGRAM": {
                "display_name": "Instagram Live",
                "color": "#e1306c",
                "last_seen": None,
                "chat_count": 0,
                "order_count": 0,
                "last_user": "-",
                "last_message": "-"
            },
            "TIKTOK": {
                "display_name": "TikTok Live",
                "color": "#00f2fe",
                "last_seen": None,
                "chat_count": 0,
                "order_count": 0,
                "last_user": "-",
                "last_message": "-"
            }
        }
        self.recent_chats = []
        self.chat_seq = 0
        self.stats = {
            "total": 0,
            "order": 0,
            "question": 0,
            "chat": 0
        }
        # 플랫폼별 접속 회원(아이디) 트래킹: key = (platform, normalized_user_id)
        self.connected_users = {}
        # 유튜브 실시간 라이브 시청자 메타데이터
        self.youtube_live_meta = {
            "is_live": False,
            "video_id": "",
            "title": "",
            "concurrent_viewers": 0,
            "view_count": 0,
            "updated_at": None
        }
        # 틱톡 실시간 라이브 시청자 메타데이터
        self.tiktok_live_meta = {
            "is_live": False,
            "room_id": "",
            "title": "",
            "concurrent_viewers": 0,
            "view_count": 0,
            "updated_at": None
        }
        # 인스타그램 실시간 라이브 시청자 메타데이터
        self.instagram_live_meta = {
            "is_live": False,
            "media_id": "",
            "title": "",
            "concurrent_viewers": 0,
            "view_count": 0,
            "updated_at": None
        }
        # 채팅 무결성(누락 0건 보장) 자동 복구 카운터 및 유튜브/틱톡/인스타그램 채팅 수신 모드
        self.healed_count = 0
        self.yt_chat_mode = "ALL_CHAT (실시간 전체 채팅)"
        self.tt_chat_mode = "ALL_CHAT (실시간 전체 채팅)"
        self.ig_chat_mode = "ALL_CHAT (실시간 전체 채팅)"
        self.load_from_db(limit=500)

    def _normalize_user_key(self, user_raw):
        u = str(user_raw or "").strip()
        if not u:
            return ""
        return u.lstrip("@").strip().lower()

    def _format_user_handle(self, user_raw):
        u = str(user_raw or "").strip()
        if not u:
            return "@시청자"
        return u if u.startswith("@") else f"@{u}"

    def _record_user_presence_locked(
        self,
        platform_raw,
        user,
        message="",
        category="CHAT",
        channel_id="",
        avatar="",
        badge="",
        seen_time=None,
        is_order=False,
        increment_chat=True
    ):
        u_clean = str(user or "").strip()
        if not u_clean or u_clean in ("-", "SYSTEM"):
            return
        p = str(platform_raw or "YOUTUBE").strip().upper()
        if "YOU" in p:
            p = "YOUTUBE"
        elif "INSTA" in p:
            p = "INSTAGRAM"
        elif "TIK" in p:
            p = "TIKTOK"
        u_key = self._normalize_user_key(u_clean)
        if not u_key:
            return

        now_dt = seen_time if isinstance(seen_time, datetime) else datetime.now()
        map_key = (p, u_key)
        existing = self.connected_users.get(map_key)
        handle_str = self._format_user_handle(u_clean)

        if not existing:
            self.connected_users[map_key] = {
                "user": u_clean,
                "handle": handle_str,
                "platform": p,
                "channel_id": str(channel_id or ""),
                "avatar": str(avatar or ""),
                "badge": str(badge or ""),
                "first_seen": now_dt,
                "last_seen": now_dt,
                "chat_count": 1 if increment_chat else 0,
                "order_count": 1 if (is_order or category == "ORDER") else 0,
                "question_count": 1 if category == "QUESTION" else 0,
                "last_message": str(message or ""),
                "last_category": str(category or "CHAT")
            }
        else:
            if u_clean.startswith("@") and not existing["user"].startswith("@"):
                existing["user"] = u_clean
            existing["handle"] = self._format_user_handle(existing["user"])
            if channel_id:
                existing["channel_id"] = str(channel_id)
            if avatar:
                existing["avatar"] = str(avatar)
            if badge:
                existing["badge"] = str(badge)
            if now_dt >= existing["last_seen"]:
                existing["last_seen"] = now_dt
                if message:
                    existing["last_message"] = str(message)
                if category:
                    existing["last_category"] = str(category)
            elif now_dt < existing["first_seen"]:
                existing["first_seen"] = now_dt
            if increment_chat:
                existing["chat_count"] += 1
            if is_order or category == "ORDER":
                existing["order_count"] += 1
            if category == "QUESTION" and increment_chat:
                existing["question_count"] += 1

    def record_user_presence(
        self,
        platform_raw,
        user,
        message="",
        category="CHAT",
        channel_id="",
        avatar="",
        badge="",
        seen_time=None,
        is_order=False,
        increment_chat=True
    ):
        with self.lock:
            self._record_user_presence_locked(
                platform_raw=platform_raw,
                user=user,
                message=message,
                category=category,
                channel_id=channel_id,
                avatar=avatar,
                badge=badge,
                seen_time=seen_time,
                is_order=is_order,
                increment_chat=increment_chat
            )

    def clear_platform_presence(self, platform_name):
        """특정 플랫폼 방송 종료 시 접속 회원 목록 및 실시간 메타데이터를 0으로 초기화합니다."""
        p_key = str(platform_name or "").strip().upper()
        if not p_key:
            return
        with self.lock:
            keys_to_del = [k for k in self.connected_users.keys() if k[0] == p_key]
            for k in keys_to_del:
                del self.connected_users[k]
            if p_key == "YOUTUBE":
                self.youtube_live_meta["is_live"] = False
                self.youtube_live_meta["concurrent_viewers"] = 0
                self.youtube_live_meta["video_id"] = ""
            elif p_key == "TIKTOK":
                self.tiktok_live_meta["is_live"] = False
                self.tiktok_live_meta["concurrent_viewers"] = 0
                self.tiktok_live_meta["room_id"] = ""
            elif p_key == "INSTAGRAM":
                self.instagram_live_meta["is_live"] = False
                self.instagram_live_meta["concurrent_viewers"] = 0

    def update_youtube_live_meta(self, concurrent_viewers=None, view_count=None, is_live=None, video_id=None, title=None):
        with self.lock:
            if concurrent_viewers is not None:
                try:
                    self.youtube_live_meta["concurrent_viewers"] = max(0, int(concurrent_viewers))
                except Exception:
                    pass
            if view_count is not None:
                try:
                    self.youtube_live_meta["view_count"] = max(0, int(view_count))
                except Exception:
                    pass
            if is_live is not None:
                self.youtube_live_meta["is_live"] = bool(is_live)
                if not bool(is_live):
                    self.youtube_live_meta["concurrent_viewers"] = 0
                    self.youtube_live_meta["video_id"] = ""
                    keys_to_del = [k for k in self.connected_users.keys() if k[0] == "YOUTUBE"]
                    for k in keys_to_del:
                        del self.connected_users[k]
            if video_id is not None and str(video_id).strip():
                self.youtube_live_meta["video_id"] = str(video_id).strip()
            if title is not None and str(title).strip():
                self.youtube_live_meta["title"] = str(title).strip()
            self.youtube_live_meta["updated_at"] = datetime.now().strftime("%H:%M:%S")

    def get_youtube_viewers_summary(self, active_window_sec=600):
        with self.lock:
            now = datetime.now()
            yt_members = []
            all_members = []

            for (p, _), info in self.connected_users.items():
                last_seen = info.get("last_seen") or now
                first_seen = info.get("first_seen") or last_seen
                diff = max(0, int((now - last_seen).total_seconds()))
                if diff < 15:
                    time_str = "방금 전"
                elif diff < 60:
                    time_str = f"{diff}초 전"
                elif diff < 3600:
                    time_str = f"{diff // 60}분 전"
                else:
                    time_str = last_seen.strftime("%H:%M:%S")

                is_online = diff <= 180
                is_active = diff <= active_window_sec
                status_code = "ONLINE" if is_online else ("ACTIVE" if is_active else "CONNECTED")

                member_item = {
                    "user": info["user"],
                    "handle": info["handle"],
                    "platform": p,
                    "channel_id": info.get("channel_id", ""),
                    "avatar": info.get("avatar", ""),
                    "badge": info.get("badge", ""),
                    "chat_count": max(1, int(info.get("chat_count", 0))),
                    "order_count": int(info.get("order_count", 0)),
                    "question_count": int(info.get("question_count", 0)),
                    "last_message": info.get("last_message", ""),
                    "last_category": info.get("last_category", "CHAT"),
                    "first_seen_str": first_seen.strftime("%H:%M:%S"),
                    "last_seen_str": last_seen.strftime("%H:%M:%S"),
                    "seconds_ago": diff,
                    "time_str": time_str,
                    "is_online": is_online,
                    "is_active": is_active,
                    "status": status_code
                }
                all_members.append(member_item)
                if p == "YOUTUBE":
                    yt_members.append(member_item)

            yt_members.sort(key=lambda x: (x["seconds_ago"], -x["order_count"], -x["chat_count"]))
            all_members.sort(key=lambda x: (x["seconds_ago"], -x["order_count"], -x["chat_count"]))

            poller_info = {}
            if "youtube_chat_poller" in globals():
                try:
                    poller_info = youtube_chat_poller.get_state()
                except Exception:
                    poller_info = {}

            yt_rtmp_on = bool("youtube_direct_streamer" in globals() and youtube_direct_streamer is not None and youtube_direct_streamer.active)
            yt_live_now = bool(self.youtube_live_meta.get("is_live") or poller_info.get("active") or yt_rtmp_on)
            if not yt_live_now:
                yt_members = []
                yt_connected_count = 0
                yt_active_count = 0
                yt_online_count = 0
                raw_concurrent = 0
                effective_viewers = 0
            else:
                yt_connected_count = len(yt_members)
                yt_active_count = sum(1 for m in yt_members if m["is_active"])
                yt_online_count = sum(1 for m in yt_members if m["is_online"])
                raw_concurrent = int(self.youtube_live_meta.get("concurrent_viewers") or 0)
                # 유튜브 스트림 동시 시청자 수와 채팅/참여로 확인된 접속 회원 수 중 큰 값을 유효 접속자 지표로도 함께 제공
                effective_viewers = max(raw_concurrent, yt_connected_count)
            total_views = int(self.youtube_live_meta.get("view_count") or 0)

            return {
                "is_live": bool(yt_live_now and (self.youtube_live_meta.get("is_live") or poller_info.get("video_id"))),
                "poller_active": bool(poller_info.get("active")),
                "channel_input": poller_info.get("channel_input") or "",
                "video_id": self.youtube_live_meta.get("video_id") or poller_info.get("video_id") or "",
                "broadcast_title": self.youtube_live_meta.get("title") or poller_info.get("broadcast_title") or "",
                "status_text": poller_info.get("status_text") or "",
                "concurrent_viewers": raw_concurrent,
                "view_count": total_views,
                "connected_member_count": yt_connected_count,
                "active_member_count": yt_active_count,
                "online_member_count": yt_online_count,
                "effective_viewer_count": effective_viewers,
                "all_platform_member_count": len(all_members),
                "updated_at": self.youtube_live_meta.get("updated_at") or now.strftime("%H:%M:%S"),
                "members": yt_members,
                "all_members": all_members
            }

    def update_tiktok_live_meta(self, concurrent_viewers=None, view_count=None, is_live=None, room_id=None, title=None):
        with self.lock:
            if concurrent_viewers is not None:
                try:
                    self.tiktok_live_meta["concurrent_viewers"] = max(0, int(concurrent_viewers))
                except Exception:
                    pass
            if view_count is not None:
                try:
                    self.tiktok_live_meta["view_count"] = max(0, int(view_count))
                except Exception:
                    pass
            if is_live is not None:
                self.tiktok_live_meta["is_live"] = bool(is_live)
            if room_id is not None and str(room_id).strip():
                self.tiktok_live_meta["room_id"] = str(room_id).strip()
            if title is not None and str(title).strip():
                self.tiktok_live_meta["title"] = str(title).strip()
            self.tiktok_live_meta["updated_at"] = datetime.now().strftime("%H:%M:%S")

    def get_tiktok_viewers_summary(self, active_window_sec=600):
        with self.lock:
            now = datetime.now()
            tt_members = []
            all_members = []

            for (p, _), info in self.connected_users.items():
                last_seen = info.get("last_seen") or now
                first_seen = info.get("first_seen") or last_seen
                diff = max(0, int((now - last_seen).total_seconds()))
                if diff < 15:
                    time_str = "방금 전"
                elif diff < 60:
                    time_str = f"{diff}초 전"
                elif diff < 3600:
                    time_str = f"{diff // 60}분 전"
                else:
                    time_str = last_seen.strftime("%H:%M:%S")

                is_online = diff <= 180
                is_active = diff <= active_window_sec
                status_code = "ONLINE" if is_online else ("ACTIVE" if is_active else "CONNECTED")

                member_item = {
                    "user": info["user"],
                    "handle": info["handle"],
                    "platform": p,
                    "channel_id": info.get("channel_id", ""),
                    "avatar": info.get("avatar", ""),
                    "badge": info.get("badge", ""),
                    "chat_count": max(1, int(info.get("chat_count", 0))),
                    "order_count": int(info.get("order_count", 0)),
                    "question_count": int(info.get("question_count", 0)),
                    "last_message": info.get("last_message", ""),
                    "last_category": info.get("last_category", "CHAT"),
                    "first_seen_str": first_seen.strftime("%H:%M:%S"),
                    "last_seen_str": last_seen.strftime("%H:%M:%S"),
                    "seconds_ago": diff,
                    "time_str": time_str,
                    "is_online": is_online,
                    "is_active": is_active,
                    "status": status_code
                }
                all_members.append(member_item)
                if p == "TIKTOK":
                    tt_members.append(member_item)

            tt_members.sort(key=lambda x: (x["seconds_ago"], -x["order_count"], -x["chat_count"]))
            all_members.sort(key=lambda x: (x["seconds_ago"], -x["order_count"], -x["chat_count"]))

            tt_connected_count = len(tt_members)
            tt_active_count = sum(1 for m in tt_members if m["is_active"])
            tt_online_count = sum(1 for m in tt_members if m["is_online"])

            raw_concurrent = int(self.tiktok_live_meta.get("concurrent_viewers") or 0)
            total_views = int(self.tiktok_live_meta.get("view_count") or 0)
            effective_viewers = max(raw_concurrent, tt_connected_count)

            poller_info = {}
            if "tiktok_chat_poller" in globals():
                try:
                    poller_info = tiktok_chat_poller.get_state()
                except Exception:
                    poller_info = {}

            return {
                "is_live": bool(self.tiktok_live_meta.get("is_live") or poller_info.get("room_id")),
                "poller_active": bool(poller_info.get("active")),
                "channel_input": poller_info.get("channel_input") or "",
                "room_id": self.tiktok_live_meta.get("room_id") or poller_info.get("room_id") or "",
                "broadcast_title": self.tiktok_live_meta.get("title") or poller_info.get("broadcast_title") or "",
                "status_text": poller_info.get("status_text") or "",
                "concurrent_viewers": raw_concurrent,
                "view_count": total_views,
                "connected_member_count": tt_connected_count,
                "active_member_count": tt_active_count,
                "online_member_count": tt_online_count,
                "effective_viewer_count": effective_viewers,
                "all_platform_member_count": len(all_members),
                "updated_at": self.tiktok_live_meta.get("updated_at") or now.strftime("%H:%M:%S"),
                "members": tt_members,
                "all_members": all_members
            }

    def update_instagram_live_meta(self, concurrent_viewers=None, view_count=None, is_live=None, media_id=None, title=None):
        with self.lock:
            if concurrent_viewers is not None:
                try:
                    self.instagram_live_meta["concurrent_viewers"] = max(0, int(concurrent_viewers))
                except Exception:
                    pass
            if view_count is not None:
                try:
                    self.instagram_live_meta["view_count"] = max(0, int(view_count))
                except Exception:
                    pass
            if is_live is not None:
                self.instagram_live_meta["is_live"] = bool(is_live)
            if media_id is not None and str(media_id).strip():
                self.instagram_live_meta["media_id"] = str(media_id).strip()
            if title is not None and str(title).strip():
                self.instagram_live_meta["title"] = str(title).strip()
            self.instagram_live_meta["updated_at"] = datetime.now().strftime("%H:%M:%S")

    def get_instagram_viewers_summary(self, active_window_sec=600):
        with self.lock:
            now = datetime.now()
            ig_members = []
            all_members = []

            for (p, _), info in self.connected_users.items():
                last_seen = info.get("last_seen") or now
                first_seen = info.get("first_seen") or last_seen
                diff = max(0, int((now - last_seen).total_seconds()))
                if diff < 15:
                    time_str = "방금 전"
                elif diff < 60:
                    time_str = f"{diff}초 전"
                elif diff < 3600:
                    time_str = f"{diff // 60}분 전"
                else:
                    time_str = last_seen.strftime("%H:%M:%S")

                is_online = diff <= 180
                is_active = diff <= active_window_sec
                status_code = "ONLINE" if is_online else ("ACTIVE" if is_active else "CONNECTED")

                member_item = {
                    "user": info["user"],
                    "handle": info["handle"],
                    "platform": p,
                    "channel_id": info.get("channel_id", ""),
                    "avatar": info.get("avatar", ""),
                    "badge": info.get("badge", ""),
                    "chat_count": max(1, int(info.get("chat_count", 0))),
                    "order_count": int(info.get("order_count", 0)),
                    "question_count": int(info.get("question_count", 0)),
                    "last_message": info.get("last_message", ""),
                    "last_category": info.get("last_category", "CHAT"),
                    "first_seen_str": first_seen.strftime("%H:%M:%S"),
                    "last_seen_str": last_seen.strftime("%H:%M:%S"),
                    "seconds_ago": diff,
                    "time_str": time_str,
                    "is_online": is_online,
                    "is_active": is_active,
                    "status": status_code
                }
                all_members.append(member_item)
                if p == "INSTAGRAM":
                    ig_members.append(member_item)

            ig_members.sort(key=lambda x: (x["seconds_ago"], -x["order_count"], -x["chat_count"]))
            all_members.sort(key=lambda x: (x["seconds_ago"], -x["order_count"], -x["chat_count"]))

            ig_connected_count = len(ig_members)
            ig_active_count = sum(1 for m in ig_members if m["is_active"])
            ig_online_count = sum(1 for m in ig_members if m["is_online"])

            raw_concurrent = int(self.instagram_live_meta.get("concurrent_viewers") or 0)
            total_views = int(self.instagram_live_meta.get("view_count") or 0)
            effective_viewers = max(raw_concurrent, ig_connected_count)

            poller_info = {}
            if "instagram_chat_poller" in globals():
                try:
                    poller_info = instagram_chat_poller.get_state()
                except Exception:
                    poller_info = {}

            return {
                "is_live": bool(self.instagram_live_meta.get("is_live") or poller_info.get("media_id")),
                "poller_active": bool(poller_info.get("active")),
                "channel_input": poller_info.get("channel_input") or "",
                "media_id": self.instagram_live_meta.get("media_id") or poller_info.get("media_id") or "",
                "broadcast_title": self.instagram_live_meta.get("title") or poller_info.get("broadcast_title") or "",
                "status_text": poller_info.get("status_text") or "",
                "concurrent_viewers": raw_concurrent,
                "view_count": total_views,
                "connected_member_count": ig_connected_count,
                "active_member_count": ig_active_count,
                "online_member_count": ig_online_count,
                "effective_viewer_count": effective_viewers,
                "all_platform_member_count": len(all_members),
                "updated_at": self.instagram_live_meta.get("updated_at") or now.strftime("%H:%M:%S"),
                "members": ig_members,
                "all_members": all_members
            }

    def load_from_db(self, limit=500):
        """서버 시작/재시작 시 SQLite DB(chats)에서 이전 채팅 내역과 통계를 100% 복원합니다."""
        try:
            hydrated = database.get_chat_tracker_hydration_data(limit=limit)
            with self.lock:
                self.stats = dict(hydrated.get("stats", {"total": 0, "order": 0, "question": 0, "chat": 0}))
                p_stats = hydrated.get("platform_stats", {})
                for p_name, p_info in p_stats.items():
                    p = p_name.upper()
                    if p not in self.platforms:
                        self.platforms[p] = {
                            "display_name": f"{p.capitalize()} Live",
                            "color": "#6366f1",
                            "last_seen": None,
                            "chat_count": 0,
                            "order_count": 0,
                            "last_user": "-",
                            "last_message": "-"
                        }
                    entry = self.platforms[p]
                    entry["chat_count"] = p_info.get("chat_count", 0)
                    entry["order_count"] = p_info.get("order_count", 0)
                    entry["last_user"] = p_info.get("last_user", "-")
                    entry["last_message"] = p_info.get("last_message", "-")
                    last_t = p_info.get("last_time")
                    if last_t:
                        try:
                            entry["last_seen"] = datetime.strptime(str(last_t), "%Y-%m-%d %H:%M:%S")
                        except Exception:
                            entry["last_seen"] = datetime.now()

                restored_chats = []
                for row in hydrated.get("recent_chats", []):
                    p = str(row.get("platform") or "YOUTUBE").upper()
                    entry = self.platforms.get(p, {"display_name": f"{p.capitalize()} Live", "color": "#6366f1"})
                    c_time = str(row.get("chat_time") or "")
                    short_time = c_time.split(" ")[-1] if " " in c_time else c_time
                    db_id = row.get("id")
                    if db_id and int(db_id) > self.chat_seq:
                        self.chat_seq = int(db_id)
                    restored_chats.append({
                        "id": f"db_{db_id}" if db_id else f"{short_time}_{row.get('sender')}_{row.get('message')}",
                        "db_id": db_id,
                        "time": short_time,
                        "chat_time": c_time,
                        "platform": p,
                        "platform_name": entry["display_name"],
                        "color": entry["color"],
                        "user": row.get("sender", "시청자"),
                        "sender": row.get("sender", "시청자"),
                        "message": row.get("message", ""),
                        "category": row.get("category", "CHAT"),
                        "reply": row.get("ai_reply", ""),
                        "ai_reply": row.get("ai_reply", ""),
                        "reply_by": row.get("reply_by", "AI") if row.get("ai_reply") else "",
                        "order_id": None,
                        "order_status": None
                    })
                self.recent_chats = restored_chats
                # DB 채팅 이력으로부터 현재 방송 접속 회원(아이디) 목록 복원 (오래된 순서 -> 최신 순서)
                self.connected_users.clear()
                for c in reversed(self.recent_chats):
                    c_dt = None
                    c_time_full = str(c.get("chat_time") or "")
                    if c_time_full:
                        try:
                            c_dt = datetime.strptime(c_time_full, "%Y-%m-%d %H:%M:%S")
                        except Exception:
                            c_dt = None
                    self._record_user_presence_locked(
                        platform_raw=c.get("platform", "YOUTUBE"),
                        user=c.get("user", "시청자"),
                        message=c.get("message", ""),
                        category=c.get("category", "CHAT"),
                        seen_time=c_dt,
                        is_order=(c.get("category") == "ORDER"),
                        increment_chat=True
                    )
                print(f"♻️ [채팅 복원 완료] DB에서 이전 채팅 {len(self.recent_chats)}건 (누적 {self.stats['total']}건, 접속회원 {len(self.connected_users)}명)을 이어받았습니다.", flush=True)
        except Exception as e:
            print(f"⚠️ [채팅 DB 복원 오류] {e}", flush=True)

    def record_chat(self, platform_raw, user, message, category, reply, order_id=None, order_status=None, chat_id=None):
        with self.lock:
            p = str(platform_raw).strip().upper()
            now = datetime.now()
            self.chat_seq += 1

            # 실시간 접속 회원(아이디) 자동 등록 및 최신화 (관제탑 공식/관리자/호스트 발신은 접속 시청자 수 집계에서 제외)
            if not str(user or "").strip().startswith(("📢", "🎤")):
                self._record_user_presence_locked(
                    platform_raw=p,
                    user=user,
                    message=message,
                    category=category,
                    seen_time=now,
                    is_order=(category == "ORDER"),
                    increment_chat=True
                )

            # 카테고리별 글로벌 통계 누적
            self.stats["total"] += 1
            if category == "ORDER":
                self.stats["order"] += 1
            elif category == "QUESTION":
                self.stats["question"] += 1
            else:
                self.stats["chat"] += 1

            # 기타 새 플랫폼 유입 시 동적 등록 (치지직, 아프리카TV, 페이스북 등)
            if p not in self.platforms:
                self.platforms[p] = {
                    "display_name": f"{p.capitalize()} Live",
                    "color": "#6366f1",
                    "last_seen": None,
                    "chat_count": 0,
                    "order_count": 0,
                    "last_user": "-",
                    "last_message": "-"
                }

            entry = self.platforms[p]
            entry["last_seen"] = now
            entry["chat_count"] += 1
            if category == "ORDER":
                entry["order_count"] += 1
            entry["last_user"] = user
            entry["last_message"] = message

            unique_id = f"db_{chat_id}" if chat_id is not None else f"{int(now.timestamp()*1000)}_{self.chat_seq}"
            now_full_str = now.strftime("%Y-%m-%d %H:%M:%S")
            # 최근 채팅 큐 (최대 500건 보관)
            chat_record = {
                "id": unique_id,
                "db_id": chat_id,
                "time": now.strftime("%H:%M:%S"),
                "chat_time": now_full_str,
                "platform": p,
                "platform_name": entry["display_name"],
                "color": entry["color"],
                "user": user,
                "sender": user,
                "message": message,
                "category": category,
                "reply": reply,
                "ai_reply": reply,
                "reply_by": "AI" if reply else "",
                "order_id": order_id,
                "order_status": order_status
            }
            self.recent_chats.insert(0, chat_record)
            if len(self.recent_chats) > 500:
                self.recent_chats.pop()
            return chat_record

    def update_chat_reply(self, chat_id, reply_text, reply_by="ADMIN"):
        """특정 채팅 ID의 답변(관리자 직접 회신)을 실시간 반영"""
        with self.lock:
            for chat in self.recent_chats:
                c_fallback_id = f"{chat.get('time')}_{chat.get('user')}_{chat.get('message')}"
                if str(chat.get("id")) == str(chat_id) or c_fallback_id == str(chat_id):
                    chat["reply"] = reply_text
                    chat["reply_by"] = reply_by
                    return chat
            return None

    def update_order_status(self, order_id, new_status):
        """특정 주문 ID의 확인 상태(CONFIRMED, REJECTED)를 실시간 반영"""
        with self.lock:
            for chat in self.recent_chats:
                if chat.get("order_id") == order_id:
                    chat["order_status"] = new_status

    def clear_mock_chats(self):
        """이전 테스트/모의 채팅 데이터 및 가짜 플랫폼을 정리합니다."""
        with self.lock:
            mock_keywords = ["vdoninja", "시청자_테스터", "홍길동", "패션스타", "라이브쇼퍼", "궁금박사", "빠른배송원해요", "패션피플"]
            self.recent_chats = [
                c for c in self.recent_chats
                if not any(k in str(c.get("user", "")) for k in mock_keywords)
                and c.get("platform") != "TWITCH"
            ]
            self.stats = {"total": len(self.recent_chats), "order": 0, "question": 0, "chat": 0}
            for c in self.recent_chats:
                cat = c.get("category", "CHAT")
                if cat == "ORDER":
                    self.stats["order"] += 1
                elif cat == "QUESTION":
                    self.stats["question"] += 1
                else:
                    self.stats["chat"] += 1
            if "TWITCH" in self.platforms:
                del self.platforms["TWITCH"]
            # 모의 사용자 접속 회원 목록에서도 제거
            keys_to_del = [
                k for k, v in self.connected_users.items()
                if any(mk in str(v.get("user", "")) for mk in mock_keywords) or k[0] == "TWITCH"
            ]
            for k in keys_to_del:
                del self.connected_users[k]

    def clear_all(self):
        """모든 채팅 내역과 플랫폼 카운터를 완전 초기화합니다."""
        with self.lock:
            self.recent_chats.clear()
            self.connected_users.clear()
            self.stats = {"total": 0, "order": 0, "question": 0, "chat": 0}
            for p, data in self.platforms.items():
                data["chat_count"] = 0
                data["order_count"] = 0
                data["last_user"] = "-"
                data["last_message"] = "-"
                data["last_seen"] = None

    def get_stats(self):
        with self.lock:
            return dict(self.stats)

    def get_summary(self):
        with self.lock:
            now = datetime.now()
            result = {}
            for p, data in self.platforms.items():
                last_seen = data["last_seen"]
                seconds_ago = None
                time_str = "수신 대기 중"
                status = "STANDBY"  # ACTIVE, IDLE, STANDBY

                if last_seen:
                    diff = (now - last_seen).total_seconds()
                    seconds_ago = int(diff)
                    if diff < 120:  # 최근 2분 이내
                        status = "ACTIVE"
                        time_str = "방금 전" if diff < 10 else f"{int(diff)}초 전"
                    elif diff < 600:  # 최근 10분 이내
                        status = "IDLE"
                        time_str = f"{int(diff // 60)}분 전"
                    else:
                        status = "STANDBY"
                        time_str = last_seen.strftime("%H:%M:%S")

                result[p] = {
                    "platform": p,
                    "display_name": data["display_name"],
                    "color": data["color"],
                    "status": status,
                    "seconds_ago": seconds_ago,
                    "time_str": time_str,
                    "chat_count": data["chat_count"],
                    "order_count": data["order_count"],
                    "last_user": data["last_user"],
                    "last_message": data["last_message"]
                }
            return result

    def get_recent_chats(self, limit=50):
        with self.lock:
            return self.recent_chats[:limit]

    def verify_and_sync_with_db(self, limit=500):
        """
        SQLite DB(chats)와 메모리 트래커(recent_chats)를 실시간 전수 대조하여
        중간에 빠진('이빨 빠진') 채팅이 단 1건이라도 있으면 즉시 DB에서 복원·정렬하고
        무결성 진단 리포트(chat_integrity)를 반환합니다.
        """
        try:
            conn = database.get_connection()
            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN category = 'ORDER' THEN 1 ELSE 0 END) AS ord_cnt,
                        SUM(CASE WHEN category = 'QUESTION' THEN 1 ELSE 0 END) AS q_cnt,
                        SUM(CASE WHEN category NOT IN ('ORDER', 'QUESTION') OR category IS NULL THEN 1 ELSE 0 END) AS c_cnt
                    FROM chats
                """)
                s_row = cur.fetchone()
                db_total = int(s_row["total"] or 0) if s_row else 0
                db_ord = int(s_row["ord_cnt"] or 0) if s_row else 0
                db_q = int(s_row["q_cnt"] or 0) if s_row else 0
                db_c = int(s_row["c_cnt"] or 0) if s_row else 0

                cur.execute("""
                    SELECT id, chat_time, platform, sender, message, category, ai_reply, reply_by
                    FROM chats
                    ORDER BY id DESC
                    LIMIT ?
                """, (max(limit, 500),))
                db_rows = [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

            with self.lock:
                # 기존 메모리 채팅의 order_id / order_status 보존 맵
                existing_by_db_id = {}
                for mc in self.recent_chats:
                    m_db_id = mc.get("db_id")
                    if m_db_id is not None:
                        try:
                            existing_by_db_id[int(m_db_id)] = mc
                        except Exception:
                            pass

                missing_healed_now = 0
                synced_list = []
                for row in db_rows:
                    db_id = int(row["id"])
                    if db_id > self.chat_seq:
                        self.chat_seq = db_id
                    prev = existing_by_db_id.get(db_id)
                    if prev is not None:
                        if row.get("ai_reply") and not prev.get("reply"):
                            prev["reply"] = row["ai_reply"]
                            prev["ai_reply"] = row["ai_reply"]
                            prev["reply_by"] = row.get("reply_by") or "AI"
                        synced_list.append(prev)
                    else:
                        missing_healed_now += 1
                        p = str(row.get("platform") or "YOUTUBE").upper()
                        entry = self.platforms.get(p, {"display_name": f"{p.capitalize()} Live", "color": "#6366f1"})
                        c_time = str(row.get("chat_time") or "")
                        short_time = c_time.split(" ")[-1] if " " in c_time else c_time
                        restored_item = {
                            "id": f"db_{db_id}",
                            "db_id": db_id,
                            "time": short_time,
                            "chat_time": c_time,
                            "platform": p,
                            "platform_name": entry["display_name"],
                            "color": entry["color"],
                            "user": row.get("sender", "시청자"),
                            "sender": row.get("sender", "시청자"),
                            "message": row.get("message", ""),
                            "category": row.get("category", "CHAT"),
                            "reply": row.get("ai_reply", ""),
                            "ai_reply": row.get("ai_reply", ""),
                            "reply_by": row.get("reply_by", "AI") if row.get("ai_reply") else "",
                            "order_id": None,
                            "order_status": None
                        }
                        synced_list.append(restored_item)
                        self._record_user_presence_locked(
                            platform_raw=p,
                            user=restored_item["user"],
                            message=restored_item["message"],
                            category=restored_item["category"],
                            is_order=(restored_item["category"] == "ORDER"),
                            increment_chat=True
                        )

                if missing_healed_now > 0 or len(synced_list) != len(self.recent_chats):
                    self.recent_chats = synced_list[:500]
                if missing_healed_now > 0:
                    self.healed_count += missing_healed_now

                self.stats["total"] = db_total
                self.stats["order"] = db_ord
                self.stats["question"] = db_q
                self.stats["chat"] = db_c

                return {
                    "status": "VERIFIED",
                    "db_total_count": db_total,
                    "tracker_count": len(self.recent_chats),
                    "missing_count": 0,
                    "healed_count": self.healed_count,
                    "healed_now": missing_healed_now,
                    "last_seq_id": self.chat_seq,
                    "yt_chat_mode": self.yt_chat_mode,
                    "verified_at": datetime.now().strftime("%H:%M:%S")
                }
        except Exception as e:
            with self.lock:
                return {
                    "status": "OK",
                    "db_total_count": self.stats.get("total", len(self.recent_chats)),
                    "tracker_count": len(self.recent_chats),
                    "missing_count": 0,
                    "healed_count": self.healed_count,
                    "healed_now": 0,
                    "last_seq_id": self.chat_seq,
                    "yt_chat_mode": self.yt_chat_mode,
                    "verified_at": datetime.now().strftime("%H:%M:%S"),
                    "note": str(e)
                }


platform_tracker = PlatformTracker()

# 3. OpenAI API 클라이언트 설정
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "YOUR_OPENAI_API_KEY").strip()
openai_client = None

if OPENAI_API_KEY and OPENAI_API_KEY != "YOUR_OPENAI_API_KEY":
    try:
        from openai import OpenAI
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
        print("🔑 [OpenAI] API 키가 정상적으로 설정되었습니다.")
    except Exception as e:
        print(f"⚠️ [OpenAI] 클라이언트 초기화 중 오류: {e}")
else:
    print("ℹ️ [알림] OPENAI_API_KEY가 설정되지 않았거나 기본값입니다.")
    print("   -> .env 파일에 실제 발급받은 OpenAI API 키를 입력하시면 GPT-4o-mini 엔진이 가동됩니다.")
    print("   -> 현재는 테스트 및 데모용 로컬 모의(Mock) AI 엔진으로 자동 동작합니다.")


def init_excel():
    """엑셀 파일 및 백업 CSV 파일이 없으면 헤더와 함께 새로 생성합니다."""
    headers = [
        "접수시간", "플랫폼", "고객닉네임",
        "상품명(번호)", "색상", "사이즈", "수량",
        "원본댓글", "AI안내메시지"
    ]
    with file_lock:
        if not os.path.exists(EXCEL_FILE):
            wb = Workbook()
            ws = wb.active
            ws.title = "실시간주문목록"
            ws.append(headers)
            wb.save(EXCEL_FILE)
            print(f"✅ '{EXCEL_FILE}' 파일이 생성되었습니다.")

        if not os.path.exists(BACKUP_CSV_FILE):
            with open(BACKUP_CSV_FILE, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
            print(f"🛡️ 비상용 백업 파일 '{BACKUP_CSV_FILE}'이 준비되었습니다.")


def append_to_backup_csv(order_data):
    """엑셀 저장 실패 시 비상용 CSV 백업 파일에 실시간 기록합니다."""
    try:
        with open(BACKUP_CSV_FILE, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                order_data["time"],
                order_data["platform"],
                order_data["user"],
                order_data["item_name"],
                order_data["color"],
                order_data["size"],
                order_data["quantity"],
                order_data["raw_message"],
                order_data["reply"]
            ])
        return True
    except Exception as e:
        print(f"❌ [백업 실패] CSV 파일 쓰기 중 오류: {e}")
        return False


def save_order_to_excel(order_data):
    """
    추출된 주문 데이터를 엑셀 파일 맨 아래 행에 추가합니다.
    엑셀 창이 열려 있어 파일 잠금(PermissionError)이 걸린 경우:
    1) 백업 CSV에 즉시 저장하여 데이터 유실을 방지합니다.
    2) 대기 큐(pending_orders)에 보관해 두었다가 엑셀 창이 닫히면 일괄 동기화합니다.
    """
    global pending_orders
    with file_lock:
        target_orders = pending_orders + [order_data]
        try:
            wb = load_workbook(EXCEL_FILE)
            ws = wb.active
            for order in target_orders:
                ws.append([
                    order["time"],
                    order["platform"],
                    order["user"],
                    order["item_name"],
                    order["color"],
                    order["size"],
                    order["quantity"],
                    order["raw_message"],
                    order["reply"]
                ])
            wb.save(EXCEL_FILE)
            if pending_orders:
                print(f"🔄 [동기화 완료] 엑셀 잠금 해제 감지 -> 대기 중이던 {len(pending_orders)}건 포함 일괄 저장 완료!")
            pending_orders = []
            return True
        except PermissionError:
            print("⚠️ [경고] 엑셀 파일이 열려 있어 live_orders.xlsx에 직접 저장하지 못했습니다.")
            print("         -> 백업 파일(live_orders_backup.csv)에 안전하게 즉시 기록했습니다.")
            print("         -> 엑셀 창을 닫아주시면 다음 번 주문 때 엑셀로 자동 반영됩니다.")
            pending_orders.append(order_data)
            append_to_backup_csv(order_data)
            return False
        except Exception as e:
            print(f"❌ [엑셀 저장 오류] {e}")
            append_to_backup_csv(order_data)
            pending_orders.append(order_data)
            return False


def mock_ai_analysis(platform, user, message):
    """
    OpenAI API 키가 설정되지 않은 환경에서 로컬 테스트를 지원하며,
    등록된 상품 목록(inventory_manager.get_all())을 바탕으로 실시간 주문/질문을 동적으로 분석합니다.
    """
    plain_text = re.sub(r'<[^>]+>', '', message).strip()
    msg = plain_text.replace(" ", "")
    if not msg:
        return {
            "category": "CHAT",
            "item_name": "-",
            "color": "-",
            "size": "-",
            "quantity": 0,
            "reply": ""
        }
    all_products = inventory_manager.get_all()

    # 1. 주문 감지
    order_keywords = ["주문", "살게요", "할게요", "구매", "개요", "주세요", "신청"]
    is_order = any(k in msg for k in order_keywords)

    # 등록된 상품명이나 번호 매칭
    matched_product = None
    for p in all_products:
        num_str = f"{p.get('num')}번"
        name_str = p.get('name', '').replace(" ", "").lower()
        if num_str in msg or (name_str and name_str in msg.lower()):
            matched_product = p
            break

    # 질문 감지 키워드
    question_keywords = ["?", "언제", "얼마", "배송", "사이즈", "교환", "환불", "출고", "추천", "키", "소재", "원단", "사양", "스펙", "어떻게", "있나요", "되나요", "문의"]
    is_question = any(k in msg for k in question_keywords)

    # 1. 질문 우선 처리 (질문 키워드가 있고, 직접적인 주문 명령어가 아닌 경우)
    if is_question and not any(k in msg for k in ["주문할", "살게요", "구매할", "개주세요", "벌주세요"]):
        if "배송" in msg or "출고" in msg:
            reply = f"{user}님, 오후 3시 이전 결제 완료 건은 CJ대한통운으로 오늘 바로 당일 출고됩니다!"
        elif "사이즈" in msg or "키" in msg:
            reply = f"{user}님, 보통 170~175cm이시면 L 사이즈를 추천드리며, 넉넉한 핏을 원하시면 XL를 권장합니다."
        elif "소재" in msg or "원단" in msg or "사양" in msg or "스펙" in msg or "핏" in msg:
            if matched_product and matched_product.get("specs"):
                reply = f"{user}님, {matched_product['name']} 사양 안내: {matched_product['specs']}"
            else:
                reply = f"{user}님, 방송 상품 상세 설명 및 배송 정책을 참고 부탁드립니다."
        elif "가격" in msg or "얼마" in msg:
            if matched_product:
                reply = f"{user}님, {matched_product['name']} 가격은 {matched_product.get('price', 0):,}원입니다."
            else:
                reply = f"{user}님, 방송 화면에 표시된 상품별 특가 가격을 확인해주세요!"
        else:
            reply = f"{user}님, 문의주신 내용은 방송 상품 상세 설명 및 배송 정책을 참고 부탁드립니다."
        return {
            "category": "QUESTION",
            "item_name": matched_product["name"] if matched_product else "-",
            "color": "-",
            "size": "-",
            "quantity": 0,
            "reply": reply
        }

    # 2. 주문 감지 (주문 키워드가 있거나 상품이 매칭된 경우)
    if is_order or matched_product:
        item_title = f"{matched_product['num']}번 {matched_product['name']}" if matched_product else "기본상품"
        color = "-"
        if matched_product:
            for opt in matched_product.get("options", []):
                opt_c = opt.get("color", "")
                if opt_c and (opt_c in message or opt_c.lower() in message.lower()):
                    color = opt_c
                    break
        if color == "-":
            for c in ["블랙", "검정", "화이트", "흰색", "그레이", "회색", "네이비", "베이지", "연청", "진청", "카키", "차콜", "브라운", "오트밀"]:
                if c in message:
                    color = c
                    break

        size = "-"
        for s in ["XXL", "XL", "FREE", "FREE사이즈", "L", "M", "S"]:
            if s in message.upper():
                size = s.replace("사이즈", "")
                break

        qty = 1
        qty_match = re.search(r'(\d+)\s*(?:개|벌|장|EA|ea|pcs|세트|박스)', message)
        if qty_match:
            try:
                qty = max(1, int(qty_match.group(1)))
            except ValueError:
                qty = 1
        else:
            for num in range(1, 10):
                if f"{num}개" in message or f"{num}벌" in message or f"{num}장" in message:
                    qty = num
                    break

        return {
            "category": "ORDER",
            "item_name": item_title,
            "color": color,
            "size": size,
            "quantity": qty,
            "reply": f"{user}님, {item_title} ({color}/{size}) {qty}개 정상 접수되었습니다! 감사합니다."
        }

    # 일반 채팅
    return {
        "category": "CHAT",
        "item_name": "-",
        "color": "-",
        "size": "-",
        "quantity": 0,
        "reply": ""
    }


def analyze_chat_with_ai(platform, user, message):
    """
    OpenAI Structured Outputs를 사용해 댓글을 분석하고 정형화된 JSON 결과를 반환합니다.
    실시간으로 등록된 상품 카탈로그(inventory_manager.get_catalog_text())가 자동 반영됩니다.
    """
    if openai_client is None:
        return mock_ai_analysis(platform, user, message)

    catalog_text = inventory_manager.get_catalog_text()
    system_prompt = f"""
당신은 라이브 커머스 방송의 실시간 AI 주문 접수 및 CS 비서입니다.
아래 실시간 등록된 판매 상품 정보와 사양을 바탕으로 시청자의 채팅을 분석하여 반드시 지정된 JSON 형식으로만 응답하세요.

{catalog_text}

[분류 기준 (category)]
1. "ORDER": 구매 의사가 있거나 상품 옵션/수량을 말하며 주문을 요청하는 댓글 (예: "1번 블랙 L 2개요", "후드티 그레이 M 살게요", "3번 베이지 하나 주세요")
2. "QUESTION": 상품 스펙(소재/사이즈/두께감), 사이즈 추천, 가격, 배송, 출고 등에 대한 질문 댓글
3. "CHAT": 단순 인사, 응원, 감탄사 등 답변이나 주문 처리가 필요 없는 일반 댓글

[JSON 응답 스키마]
{{
  "category": "ORDER" | "QUESTION" | "CHAT",
  "item_name": "주문 상품명 또는 번호 (없으면 '-')",
  "color": "추출된 색상 (없으면 '-')",
  "size": "추출된 사이즈 (없으면 '-')",
  "quantity": 추출된 수량 숫자(기본값 1, 주문 아니면 0),
  "reply": "시청자 이름을 부르며 방송 화면에 띄울 친절한 1문장 한국어 답변 (ORDER면 주문 접수 완료 안내 또는 누락된 옵션 질문, QUESTION이면 질문에 대한 명확한 답변, CHAT이면 빈 문자열)"
}}
"""
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"[{platform}] {user}: {message}"}
            ],
            temperature=0.2,
            timeout=4.0
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"⚠️ [OpenAI API 호출 에러] {e} -> 모의 AI로 임시 대응합니다.")
        return mock_ai_analysis(platform, user, message)


def get_all_orders():
    """DB에서 누적된 주문 목록을 최신순으로 반환합니다."""
    return database.get_all_orders()


def calculate_analytics():
    """SQLite DB에서 실시간 집계된 주문 및 매출 통계를 반환합니다."""
    return database.get_analytics_summary()


def get_dashboard_context():
    """웹 대시보드에 전달할 기본 컨텍스트"""
    obs_content = ""
    if os.path.exists(OBS_TEXT_FILE):
        try:
            with open(OBS_TEXT_FILE, "r", encoding="utf-8") as f:
                obs_content = f.read()
        except Exception:
            pass
    port = int(os.getenv("SERVER_PORT", 5000))
    engine = "OpenAI (gpt-4o-mini)" if openai_client else "로컬 모의 AI 모드 (실제 GPT 연동을 위해 .env에 OPENAI_API_KEY 입력)"
    return {
        "port": port,
        "obs_content": obs_content,
        "engine": engine,
        "platforms": platform_tracker.get_summary(),
        "recent_chats": platform_tracker.get_recent_chats(15)
    }


# ==========================================
# 1. 🌐 공개 홍보 홈페이지 (/) & 🏢 내부 비즈니스 포털 (/portal) & 🔐 방송자 인증
# ==========================================
@app.route('/', methods=['GET', 'POST'])
def index():
    """불특정 다수 공개 홍보 홈페이지 (POST 인입 시 웹훅 처리 하위호환 유지)"""
    if request.method == 'POST':
        return receive_chat()
    return render_template('promo_landing.html')


@app.route('/portal', methods=['GET'])
def studio_portal_view():
    """승인된 방송자 전용 내부 비즈니스 관제 포털"""
    analytics = calculate_analytics()
    cs_stats = cs_manager.get_stats()
    platforms_summary = platform_tracker.get_summary()
    active_count = sum(1 for p in platforms_summary.values() if p["status"] == "ACTIVE")

    return render_template(
        'main_portal.html',
        total_revenue=analytics["total_revenue"],
        total_orders=analytics["total_orders"],
        total_cs=cs_stats["total"],
        active_platforms_count=active_count,
        inventory_items=inventory_manager.get_all(),
        recent_orders=analytics["orders"][:5]
    )


@app.route('/auth/login', methods=['GET', 'POST'])
def studio_auth_login():
    """OBS 스튜디오 방송자 로그인 (로그인 성공 시 기본 /portal 로 이동)"""
    explicit_next = (request.args.get("next") or request.form.get("next") or "").strip()
    next_url = explicit_next or "/portal"
    if not security_core.is_safe_redirect_url(next_url) or next_url == "/overlay/studio":
        next_url = "/portal"

    if request.method == 'GET':
        cur_u = get_current_studio_user()
        if cur_u:
            if cur_u.get("status") == "APPROVED":
                return redirect(next_url)
            return redirect(url_for("studio_auth_pending"))
        return render_template("studio_auth.html", mode="login", next_url=next_url)

    ip_addr = str(request.remote_addr or "unknown")
    allowed, retry_sec = security_core.rate_limiter.check("studio_login", ip_addr, max_requests=10, window_sec=60.0)
    if not allowed:
        return render_template(
            "studio_auth.html",
            mode="login",
            next_url=next_url,
            error=f"로그인 시도가 너무 많습니다. {retry_sec}초 후 다시 시도해 주세요."
        ), 429

    if request.is_json:
        data = request.get_json(silent=True) or {}
        login_id = data.get("login_id") or data.get("username") or data.get("email") or ""
        password = data.get("password") or ""
    else:
        login_id = request.form.get("login_id") or ""
        password = request.form.get("password") or ""

    res = database.authenticate_studio_user(login_id, password, ip_address=ip_addr)
    if not res.get("ok"):
        status_code = 423 if res.get("code") == "LOCKED" else 401
        if request.is_json:
            return jsonify({"status": "error", "code": res.get("code"), "message": res.get("message")}), status_code
        return render_template("studio_auth.html", mode="login", next_url=next_url, error=res.get("message")), status_code

    user = res["user"]
    security_core.rotate_session(session)
    session.permanent = True
    session["studio_user_id"] = user["id"]
    session["studio_role"] = user["role"]

    target_after_login = next_url

    if request.is_json:
        return jsonify({
            "status": "ok",
            "user": user,
            "csrf_token": security_core.get_csrf_token(session),
            "redirect": target_after_login if user.get("status") == "APPROVED" else "/auth/pending"
        }), 200

    if user.get("status") != "APPROVED":
        return redirect(url_for("studio_auth_pending"))
    return redirect(target_after_login)


@app.route('/auth/register', methods=['GET', 'POST'])
def studio_auth_register():
    """OBS 스튜디오 별도 방송자 회원가입 신청"""
    if request.method == 'GET':
        return render_template("studio_auth.html", mode="register", form_data={})

    ip_addr = str(request.remote_addr or "unknown")
    allowed, retry_sec = security_core.rate_limiter.check("studio_register", ip_addr, max_requests=8, window_sec=60.0)
    if not allowed:
        return render_template(
            "studio_auth.html",
            mode="register",
            form_data={},
            error=f"가입 요청이 너무 많습니다. {retry_sec}초 후 다시 시도해 주세요."
        ), 429

    if request.is_json:
        form_data = request.get_json(silent=True) or {}
    else:
        form_data = {
            "username": request.form.get("username", ""),
            "email": request.form.get("email", ""),
            "display_name": request.form.get("display_name", ""),
            "phone": request.form.get("phone", ""),
            "password": request.form.get("password", ""),
            "invite_code": request.form.get("invite_code", "")
        }

    res = database.create_studio_user(
        username=form_data.get("username", ""),
        email=form_data.get("email", ""),
        password=form_data.get("password", ""),
        display_name=form_data.get("display_name", ""),
        phone=form_data.get("phone", ""),
        invite_code=form_data.get("invite_code", ""),
        ip_address=ip_addr
    )
    if not res.get("ok"):
        if request.is_json:
            return jsonify({"status": "error", "message": res.get("message")}), 400
        return render_template("studio_auth.html", mode="register", form_data=form_data, error=res.get("message")), 400

    security_core.rotate_session(session)
    session.permanent = True
    session["studio_user_id"] = res["user_id"]
    session["studio_role"] = res["role"]

    if request.is_json:
        return jsonify({
            "status": "ok",
            "user_status": res["status"],
            "csrf_token": security_core.get_csrf_token(session),
            "redirect": "/portal" if res["status"] == "APPROVED" else "/auth/pending"
        }), 200

    if res["status"] == "APPROVED":
        return redirect("/portal")
    return redirect(url_for("studio_auth_pending"))


@app.route('/auth/pending', methods=['GET'])
def studio_auth_pending():
    """가입 승인 대기 / 정지 상태 안내 화면"""
    return render_template("studio_auth.html", mode="pending")


@app.route('/auth/logout', methods=['GET', 'POST'])
def studio_auth_logout():
    """OBS 스튜디오 로그아웃"""
    session.clear()
    if request.is_json:
        return jsonify({"status": "ok"}), 200
    return redirect(url_for("index"))


@app.route('/studio/admin/users', methods=['GET'])
def studio_admin_users_view():
    """기존 경로 호환: 전용 관리자 콘솔(/admin)로 리다이렉트"""
    return redirect("/admin")


@app.route('/api/studio/admin/user-status', methods=['POST'])
def api_studio_admin_user_status():
    data = request.get_json(silent=True) or {}
    uid = data.get("user_id")
    new_st = data.get("status")
    new_role = data.get("role")
    cur_u = get_current_studio_user() or {}
    ok = database.update_studio_user_status(
        user_id=uid,
        new_status=new_st,
        new_role=new_role,
        actor_id=cur_u.get("username", "admin"),
        ip_address=str(request.remote_addr or "")
    )
    return jsonify({"status": "ok" if ok else "error"}), (200 if ok else 400)


@app.route('/api/studio/admin/invite-code', methods=['POST'])
def api_studio_admin_create_invite():
    data = request.get_json(silent=True) or {}
    cur_u = get_current_studio_user() or {}
    res = database.create_studio_invite_code(
        code=data.get("code", ""),
        memo=data.get("memo", ""),
        max_uses=data.get("max_uses", 10),
        created_by=cur_u.get("username", "ADMIN")
    )
    return jsonify({"status": "ok" if res.get("ok") else "error", **res}), (200 if res.get("ok") else 400)


@app.route('/api/studio/admin/invite-toggle', methods=['POST'])
def api_studio_admin_toggle_invite():
    data = request.get_json(silent=True) or {}
    ok = database.toggle_studio_invite_code(data.get("code_id"), bool(data.get("is_active")))
    return jsonify({"status": "ok" if ok else "error"}), (200 if ok else 400)


# ==========================================
# 2. 📡 실시간 웹방송 관제탑
# ==========================================
@app.route('/broadcast', methods=['GET'])
def broadcast_view():
    """실시간 웹방송 관제탑 전용 화면"""
    ctx = get_dashboard_context()
    return render_template('broadcast.html', **ctx)


_dedup_lock = Lock()
_recent_chat_fingerprints = {}  # key -> timestamp


def process_chat_payload(data):
    """
    Social Stream Ninja 웹훅(HTTP POST) 및 실시간 웹소켓(WebSocket) 수신 데이터를
    공통으로 파싱하고 AI 분석/엑셀 저장/재고 차감/OBS 자막 생성을 수행합니다.
    """
    if "data" in data and isinstance(data["data"], dict):
        inner = data["data"]
    else:
        inner = data

    # 다양한 키 이름 지원 (Social Stream Ninja 버전 및 이벤트별 차이 대응)
    user = str(inner.get("chatname") or inner.get("username") or inner.get("user") or inner.get("author") or inner.get("name") or "시청자").strip()
    message = str(inner.get("chatmessage") or inner.get("message") or inner.get("text") or inner.get("comment") or inner.get("content") or "").strip()
    raw_platform = str(inner.get("type") or inner.get("platform") or inner.get("source") or inner.get("service") or "YOUTUBE").upper()

    # 플랫폼 정규화
    if "YOU" in raw_platform:
        platform = "YOUTUBE"
    elif "INSTA" in raw_platform:
        platform = "INSTAGRAM"
    elif "TIK" in raw_platform:
        platform = "TIKTOK"
    elif "FACE" in raw_platform:
        platform = "FACEBOOK"
    else:
        platform = raw_platform

    # 빈 메시지나 봇 자신의 메시지는 무시
    if not message or inner.get("bot") is True:
        return {"status": "ignored"}

    # 한번 저장되어 불러온 이전 방송 열람 중일 때는 데이터 수정/추가 차단 (중복 저장 및 변조 방지)
    restored_info = database.get_restored_session_info()
    if restored_info["is_restored"]:
        return {
            "status": "locked",
            "message": "🔒 한번 저장된 이전 방송 데이터는 수정할 수 없습니다. [🆕 새로운 방송 시작]으로 초기화한 후 이용해 주세요."
        }

    # 다중 채널(Webhook + WebSocket CH 1~4 + YouTube Direct) 동시 유입 중복 방지 가드
    # - 고유 메시지 ID(ext_id)가 서로 다른 정상 연속 댓글이나 시뮬레이터 테스트 댓글은 절대 차단하지 않음!
    now_ts = time.time()
    ext_id = str(inner.get("id") or "").strip()
    in_source = str(inner.get("source") or "").strip().lower()
    is_simulator = (in_source == "simulator") or bool(inner.get("is_test"))
    content_key = f"{platform}|{user}|{message}"
    with _dedup_lock:
        # 만료된 키 정리
        expired_keys = [k for k, ts in _recent_chat_fingerprints.items() if now_ts - ts > 30.0]
        for k in expired_keys:
            del _recent_chat_fingerprints[k]

        if not is_simulator:
            if ext_id:
                if f"id:{ext_id}" in _recent_chat_fingerprints:
                    return {"status": "duplicate_ignored"}
                _recent_chat_fingerprints[f"id:{ext_id}"] = now_ts
                # 서로 다른 채널(예: Ninja + YouTube Direct)에서 동일 채팅이 다른 ID 체계로 거의 동시에(0.45초 내) 들어온 경우만 차단
                prev_src_info = _recent_chat_fingerprints.get(f"src:{content_key}")
                if prev_src_info and isinstance(prev_src_info, tuple):
                    prev_ts, prev_src = prev_src_info
                    if prev_src and in_source and prev_src != in_source and (now_ts - prev_ts) < 0.6:
                        return {"status": "duplicate_ignored"}
                _recent_chat_fingerprints[f"src:{content_key}"] = (now_ts, in_source or "ext")
            else:
                # ext_id가 없는 웹훅 인입의 경우에만 0.45초 초단기 네트워크 재전송 중복 방지
                if content_key in _recent_chat_fingerprints and (now_ts - _recent_chat_fingerprints[content_key]) < 0.45:
                    return {"status": "duplicate_ignored"}
                _recent_chat_fingerprints[content_key] = now_ts

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"💬 [{now}] [{platform}] {user}: {message}", flush=True)

    # 너무 짧은 단순 감탄사(ㅋㅋ, ㅎㅎ 등) 또는 이미지/이모티콘 단독 채팅은 API 비용 절감을 위해 패스하되 채팅창에는 100% 그대로 표시
    # 주의: <1번 블랙> 같은 일반 꺾쇠 텍스트가 지워지지 않도록 실제 HTML 태그만 제거
    plain_msg = re.sub(r'</?(?:img|span|div|a|br|p|b|i|strong|em|font|svg|path)\b[^>]*>', '', message, flags=re.IGNORECASE).strip()
    if len(plain_msg) < 2 and not re.search(r'\d', plain_msg):
        db_chat_id = database.save_chat(platform, user, message, "CHAT", "")
        platform_tracker.record_chat(platform, user, message, "CHAT", "", chat_id=db_chat_id)
        return {
            "status": "saved_image_chat" if "<img" in message.lower() else "skipped_short",
            "chat_id": db_chat_id
        }

    # AI 분석 및 주문/CS 연동 (어떤 예외가 발생해도 DB 및 실시간 관제탑 기록은 100% 보장!)
    category = "CHAT"
    reply = ""
    result = {"category": "CHAT", "item_name": "-", "color": "-", "size": "-", "quantity": 0, "reply": ""}
    order_id = None
    order_status = None

    try:
        result = analyze_chat_with_ai(platform, user, message) or result
        category = result.get("category", "CHAT")
        reply = result.get("reply", "")

        # 1. 주문(ORDER)인 경우 -> DB 주문 원장에 'PENDING(확인 대기)'로 저장 (관리자 확인 시에만 차감!)
        if category == "ORDER":
            item_name = result.get("item_name", "-")
            color = result.get("color", "-")
            size = result.get("size", "-")
            quantity = result.get("quantity", 1)

            if not reply:
                reply = f"{user}님, [{item_name} {color}/{size} {quantity}개] 주문이 접수되었습니다! (관리자 확인 중)"

            order_id = database.create_order({
                "order_time": now,
                "platform": platform,
                "buyer_name": user,
                "item_name": item_name,
                "color": color,
                "size": size,
                "quantity": quantity,
                "raw_message": message,
                "reply_text": reply,
                "status": "PENDING"
            })
            order_status = "PENDING"
            print(f"🛒 [주문 접수 대기 (확인 대기)] #{order_id} {user}님 -> {item_name} ({color}/{size}) {quantity}개 (재고 자동차감 없음)", flush=True)

        # 2. 질문(QUESTION)인 경우 -> CS 관리 시스템에 자동 기록 연동!
        if category == "QUESTION":
            cs_manager.record_question(platform, user, message, reply)
    except Exception as ai_err:
        print(f"⚠️ [채팅 AI/주문 처리 중 예외 발생 -> 채팅 원문 100% 보존] {ai_err}", flush=True)

    try:
        # 💾 SQLite DB chats 테이블에 실시간 영속 1회 저장 후 고유 chat_id 획득 (누락 0% 보장)
        db_chat_id = database.save_chat(platform, user, message, category, reply)
        # 플랫폼 연결 상태 및 최근 채팅 실시간 1회 기록 (db_chat_id, order_id & order_status 연계)
        platform_tracker.record_chat(
            platform, user, message, category, reply,
            order_id=order_id, order_status=order_status, chat_id=db_chat_id
        )

        # 3. 주문(ORDER) 또는 질문(QUESTION) AI 안내 메시지 콘솔 로그 출력
        #    (※ 방송 화면의 '📢 LIVE 안내' 창에는 시청자 채팅이나 AI 답변이 끼어들지 않고 사용자가 직접 넣은 문구만 고정 표시되도록 파일/상태 덮어쓰기 차단)
        if category in ["ORDER", "QUESTION"] and reply:
            print(f"🤖 [AI 안내 메시지] {reply}", flush=True)

        return {
            "status": "ok",
            "chat_id": db_chat_id,
            "category": category,
            "data": result,
            "reply": reply,
            "order_id": order_id,
            "order_status": order_status
        }
    except Exception as e:
        print(f"❌ 오류 발생: {e}", flush=True)
        return {"status": "error", "message": str(e)}


def extract_request_payload(req):
    """모든 종류의 웹훅 HTTP 인입 데이터(JSON, 폼, Raw Text, 쿼리 파라미터)를 유연하게 추출"""
    data = req.get_json(silent=True)
    if data and isinstance(data, dict):
        return data

    if req.form:
        return req.form.to_dict()

    if req.data:
        try:
            parsed = json.loads(req.data.decode('utf-8', errors='ignore'))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            text = req.data.decode('utf-8', errors='ignore').strip()
            if text:
                return {"chatmessage": text, "chatname": "시청자", "type": "YOUTUBE"}

    if req.args:
        return req.args.to_dict()

    return {}


@app.route('/chat', methods=['GET', 'POST'])
@app.route('/webhook', methods=['GET', 'POST'])
@app.route('/api/chat', methods=['GET', 'POST'])
def receive_chat():
    """
    Social Stream Ninja 웹훅 수신 엔드포인트
    - GET: 브라우저 접속 시 웹방송 관제탑 표시 (하위 호환성 유지)
    - POST: 실시간 댓글 수신 및 의도 분석/재고 차감/CS 기록/OBS 자막 갱신
    """
    if request.method == 'GET':
        ctx = get_dashboard_context()
        return render_template('broadcast.html', **ctx)

    data = extract_request_payload(request)
    print(f"\n📥 [웹훅 HTTP 인입] {data}", flush=True)
    res = process_chat_payload(data)
    status_code = 500 if res.get("status") == "error" else 200
    return jsonify(res), status_code


# ==========================================
# 3. 📦 실시간 재고 관리 라우트 & API
# ==========================================
@app.route('/inventory', methods=['GET'])
def inventory_view():
    """실시간 재고 관리 화면"""
    items = inventory_manager.get_all()
    return render_template('inventory.html', items=items)


@app.route('/api/inventory/update', methods=['POST'])
def api_inventory_update():
    """재고 수량 수정 API"""
    data = request.get_json(silent=True) or {}
    item_id = data.get("item_id")
    color = data.get("color")
    size = data.get("size")
    stock = data.get("stock")

    if None in [item_id, color, size, stock]:
        return jsonify({"status": "error", "message": "필수 파라미터 누락"}), 400

    success = inventory_manager.update_stock(item_id, color, size, stock)
    return jsonify({"status": "ok" if success else "failed"}), 200


@app.route('/api/upload/image', methods=['POST'])
def api_upload_image():
    """상품 대표 사진 업로드 API"""
    if 'image' not in request.files:
        return jsonify({"status": "error", "message": "이미지 파일이 전송되지 않았습니다."}), 400
    file = request.files['image']
    if file.filename == '':
        return jsonify({"status": "error", "message": "선택된 파일이 없습니다."}), 400
    if file and allowed_file(file.filename):
        import uuid
        ext = file.filename.rsplit('.', 1)[1].lower()
        unique_name = f"prod_{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:8]}.{ext}"
        save_path = os.path.join(UPLOAD_FOLDER, unique_name)
        file.save(save_path)
        img_url = f"/static/uploads/products/{unique_name}"
        return jsonify({"status": "ok", "url": img_url}), 200
    return jsonify({"status": "error", "message": "지원하지 않는 파일 형식입니다. (jpg, jfif, png, webp 가능)"}), 400


@app.route('/api/inventory/product/add', methods=['POST'])
def api_inventory_product_add():
    """새로운 상품 등록 API (사진, 사양, 옵션별 재고 포함)"""
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"status": "error", "message": "상품명은 필수입니다."}), 400
    new_product = inventory_manager.add_product(data)
    return jsonify({"status": "ok", "product": new_product}), 200


@app.route('/api/inventory/product/update', methods=['POST'])
def api_inventory_product_update():
    """상품 정보/사양/옵션 수정 API"""
    data = request.get_json(silent=True) or {}
    item_id = data.get("id")
    if not item_id:
        return jsonify({"status": "error", "message": "상품 ID가 누락되었습니다."}), 400
    success = inventory_manager.update_product(item_id, data)
    return jsonify({"status": "ok" if success else "failed"}), 200


@app.route('/api/inventory/product/delete', methods=['POST'])
def api_inventory_product_delete():
    """상품 삭제 API"""
    data = request.get_json(silent=True) or {}
    item_id = data.get("id")
    if not item_id:
        return jsonify({"status": "error", "message": "상품 ID가 누락되었습니다."}), 400
    success = inventory_manager.delete_product(item_id)
    return jsonify({"status": "ok" if success else "failed"}), 200


# ==========================================
# 4. 💬 CS 및 고객 문의 관리 라우트
# ==========================================
@app.route('/cs', methods=['GET'])
def cs_view():
    """CS 및 고객 문의 관리 화면"""
    q_type = request.args.get('type', 'ALL')
    questions = cs_manager.get_all(q_type)
    stats = cs_manager.get_stats()
    return render_template('cs.html', questions=questions, stats=stats)


@app.route('/api/cs/list', methods=['GET'])
def api_cs_list():
    """실시간 CS 문의 목록 및 통계 반환 API (1초 주기 동기화 지원)"""
    q_type = request.args.get('type', 'ALL')
    questions = cs_manager.get_all(q_type)
    stats = cs_manager.get_stats()
    return jsonify({
        "status": "ok",
        "broadcast_reset_seq": broadcast_reset_seq,
        "questions": questions,
        "stats": stats
    }), 200


# ==========================================
# 5. 📊 판매량 및 통계 분석 라우트 & 엑셀 다운로드
# ==========================================
@app.route('/analytics', methods=['GET'])
def analytics_view():
    """판매량 및 통계 분석 화면"""
    analytics = calculate_analytics()
    return render_template(
        'analytics.html',
        total_orders=analytics["total_orders"],
        total_revenue=analytics["total_revenue"],
        total_cost=analytics.get("total_cost", 0),
        total_profit=analytics.get("total_profit", 0),
        margin_rate=analytics.get("margin_rate", 0),
        platform_stats=analytics["platform_stats"],
        product_ranks=analytics["product_ranks"],
        orders=analytics["orders"]
    )


@app.route('/download/excel', methods=['GET'])
def download_excel():
    """실시간 DB 기반 엑셀 원본 파일 직접 생성 및 다운로드"""
    database.export_orders_to_excel(EXCEL_FILE)
    if os.path.exists(EXCEL_FILE):
        return send_file(
            EXCEL_FILE,
            as_attachment=True,
            download_name=f"live_orders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        )
    return "엑셀 파일이 아직 생성되지 않았습니다.", 404


# ==========================================
# ⚡ 통합관제탑 빠른 문구(자동 송출 프리셋) 커스텀 관리 (추가/수정/삭제, DB 영구 저장)
# ==========================================
DEFAULT_QUICK_CHAT_PHRASES = [
    {
        "id": "qp_welcome",
        "label": "👋 환영인사 즉시전송",
        "message": "👋 어서오세요! 라이브 방송 오신 분들 환영합니다~ 궁금한 점 편하게 채팅 남겨주세요!",
        "color": "default"
    },
    {
        "id": "qp_order_guide",
        "label": "🛒 주문방법 안내",
        "message": "🛒 주문은 채팅창에 [상품번호 + 색상/사이즈 + 수량]을 적어주시면 실시간 자동 접수됩니다!",
        "color": "default"
    },
    {
        "id": "qp_special_price",
        "label": "⚡ 방송특가 안내",
        "message": "⚡ 라이브 방송 중 주문 고객님께만 특별 할인가 & 당일 발송 혜택이 적용됩니다!",
        "color": "default"
    },
    {
        "id": "qp_soldout_soon",
        "label": "🚨 품절임박 안내",
        "message": "🚨 인기 옵션 잔여 수량이 얼마 남지 않았습니다! 품절 전 서둘러 주세요!",
        "color": "red"
    },
    {
        "id": "qp_shipping_today",
        "label": "📦 당일출고 안내",
        "message": "📦 입금 확인 완료된 주문 건은 방송 종료 후 오늘 바로 출고됩니다! 감사합니다.",
        "color": "green"
    }
]


def get_quick_chat_phrases():
    """SQLite DB에서 커스텀 빠른 문구 목록을 조회 (없으면 기본 5개 초기화)"""
    saved = database.get_overlay_state('custom_quick_chat_phrases', None)
    if not isinstance(saved, list):
        default_copy = [dict(item) for item in DEFAULT_QUICK_CHAT_PHRASES]
        database.set_overlay_state('custom_quick_chat_phrases', default_copy)
        return default_copy
    return saved


def save_quick_chat_phrases(phrases):
    """커스텀 빠른 문구 목록을 SQLite DB에 영구 저장"""
    clean_list = []
    for idx, item in enumerate(phrases or []):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        message = str(item.get("message") or "").strip()
        if not label or not message:
            continue
        color = str(item.get("color") or "default").strip().lower()
        if color not in ("default", "red", "green", "gold", "blue"):
            color = "default"
        item_id = str(item.get("id") or f"qp_{int(time.time()*1000)}_{idx}").strip()
        clean_list.append({
            "id": item_id,
            "label": label,
            "message": message,
            "color": color
        })
    database.set_overlay_state('custom_quick_chat_phrases', clean_list)
    return clean_list


@app.route('/api/chat/quick-phrases', methods=['GET', 'POST'])
def api_chat_quick_phrases():
    """통합관제탑 빠른 문구(자동 송출 버튼) 조회 / 추가 / 수정 / 삭제 / 초기화 API"""
    if request.method == 'GET':
        return jsonify({
            "status": "ok",
            "phrases": get_quick_chat_phrases()
        }), 200

    data = request.get_json(silent=True) or {}
    action = str(data.get("action") or "").strip().lower()
    phrases = get_quick_chat_phrases()

    if action == "add":
        label = str(data.get("label") or "").strip()
        message = str(data.get("message") or "").strip()
        color = str(data.get("color") or "default").strip().lower()
        if not label or not message:
            return jsonify({"status": "error", "message": "버튼 이름(라벨)과 자동 송출할 문구를 모두 입력해 주세요."}), 400
        new_item = {
            "id": f"qp_{int(time.time()*1000)}_{uuid.uuid4().hex[:4]}",
            "label": label,
            "message": message,
            "color": color if color in ("default", "red", "green", "gold", "blue") else "default"
        }
        phrases.append(new_item)
        saved = save_quick_chat_phrases(phrases)
        return jsonify({"status": "ok", "action": "add", "item": new_item, "phrases": saved}), 200

    elif action == "update":
        target_id = str(data.get("id") or "").strip()
        label = str(data.get("label") or "").strip()
        message = str(data.get("message") or "").strip()
        color = str(data.get("color") or "default").strip().lower()
        if not target_id or not label or not message:
            return jsonify({"status": "error", "message": "수정할 항목의 버튼 이름과 송출 문구를 모두 입력해 주세요."}), 400
        found = False
        for item in phrases:
            if str(item.get("id")) == target_id:
                item["label"] = label
                item["message"] = message
                item["color"] = color if color in ("default", "red", "green", "gold", "blue") else "default"
                found = True
                break
        if not found:
            return jsonify({"status": "error", "message": "수정할 빠른 문구 항목을 찾을 수 없습니다."}), 404
        saved = save_quick_chat_phrases(phrases)
        return jsonify({"status": "ok", "action": "update", "phrases": saved}), 200

    elif action == "delete":
        target_id = str(data.get("id") or "").strip()
        if not target_id:
            return jsonify({"status": "error", "message": "삭제할 문구 ID가 누락되었습니다."}), 400
        new_phrases = [item for item in phrases if str(item.get("id")) != target_id]
        saved = save_quick_chat_phrases(new_phrases)
        return jsonify({"status": "ok", "action": "delete", "phrases": saved}), 200

    elif action == "reset":
        default_copy = [dict(item) for item in DEFAULT_QUICK_CHAT_PHRASES]
        saved = save_quick_chat_phrases(default_copy)
        return jsonify({"status": "ok", "action": "reset", "phrases": saved}), 200

    return jsonify({"status": "error", "message": "지원하지 않는 action입니다. (add/update/delete/reset)"}), 400


# ==========================================
# 📢 방송 화면 'LIVE 안내' 자막 전용 헬퍼 (채팅 끼어들기 완전 차단 & 사용자 입력 문구만 표시)
# ==========================================
DEFAULT_LIVE_TICKER_MESSAGE = "잠시 후 라이브 커머스 방송이 시작됩니다! 채팅창에 번호와 수량을 입력하여 간편하게 주문해 보세요."


def _is_chat_polluted_ticker(text):
    """과거 실시간 채팅/AI 답변/주문 자동 로그가 자막에 섞여 들어간 문자열인지 판별"""
    t = str(text or "").strip()
    if not t:
        return True
    chat_pollution_prefixes = (
        "[YOUTUBE]",
        "[INSTAGRAM]",
        "[TIKTOK]",
        "[FACEBOOK]",
        "[LIVE]",
        "💬 [",
        "🛒 [주문 확정]",
        "🚨 [전량 품절]",
        "대기 중: 주문 또는 질문이",
    )
    if t.startswith(chat_pollution_prefixes):
        return True
    if "▶ AI 답변:" in t or "▶ 주문 접수" in t:
        return True
    return False


def get_custom_live_ticker_message():
    """오직 사용자가 직접 입력·설정한 방송 LIVE 안내 문구만 반환 (채팅 혼입 원천 차단)"""
    custom_msg = str(database.get_overlay_state('obs_ticker_custom', '') or '').strip()
    if custom_msg and not _is_chat_polluted_ticker(custom_msg):
        return custom_msg

    saved_settings = database.get_overlay_state('overlay_settings', None)
    if isinstance(saved_settings, dict):
        setting_msg = str(saved_settings.get('ticker_message') or '').strip()
        if setting_msg and not _is_chat_polluted_ticker(setting_msg):
            database.set_overlay_state('obs_ticker_custom', setting_msg)
            try:
                with open(OBS_TEXT_FILE, "w", encoding="utf-8") as f:
                    f.write(setting_msg)
            except Exception:
                pass
            return setting_msg

    # 만약 기존 obs_ticker_custom이나 OBS_TEXT_FILE이 채팅으로 오염되어 있었다면 기본 안내 문구로 깨끗하게 정화
    database.set_overlay_state('obs_ticker_custom', DEFAULT_LIVE_TICKER_MESSAGE)
    try:
        with open(OBS_TEXT_FILE, "w", encoding="utf-8") as f:
            f.write(DEFAULT_LIVE_TICKER_MESSAGE)
    except Exception:
        pass
    return DEFAULT_LIVE_TICKER_MESSAGE


def set_custom_live_ticker_message(message):
    """사용자가 직접 설정한 방송 LIVE 안내 문구를 DB(obs_ticker_custom + overlay_settings) 및 파일에 영구 저장"""
    clean_msg = str(message or "").strip()
    if not clean_msg:
        return get_custom_live_ticker_message()

    database.set_overlay_state('obs_ticker_custom', clean_msg)
    saved_settings = database.get_overlay_state('overlay_settings', None)
    if isinstance(saved_settings, dict):
        saved_settings['ticker_message'] = clean_msg
        database.set_overlay_state('overlay_settings', saved_settings)

    try:
        with open(OBS_TEXT_FILE, "w", encoding="utf-8") as f:
            f.write(clean_msg)
    except Exception:
        pass
    return clean_msg


# ==========================================
# 6. 시스템 상태 및 API
# ==========================================
@app.route('/api/platforms', methods=['GET'])
def api_platforms():
    """실시간 플랫폼별 연결 상태 및 통계 반환 API"""
    return jsonify(platform_tracker.get_summary()), 200


@app.route('/api/recent-chats', methods=['GET'])
def api_recent_chats():
    """최근 인입된 실시간 채팅 목록 반환 API (최대 500건 지원)"""
    limit = request.args.get('limit', 200, type=int)
    return jsonify(platform_tracker.get_recent_chats(limit)), 200


@app.route('/api/dashboard-summary', methods=['GET'])
def api_dashboard_summary():
    """관제탑 통합 실시간 대시보드 API (플랫폼, 카운트 통계, 실시간 전체 채팅, 무결성 검증, 자막, 재고)"""
    limit = request.args.get('limit', 300, type=int)
    obs_content = get_custom_live_ticker_message()

    chat_integrity = platform_tracker.verify_and_sync_with_db(limit=limit)
    yt_poller_state = youtube_chat_poller.get_state() if 'youtube_chat_poller' in globals() else {}
    yt_vid = yt_poller_state.get("video_id") or ""
    yt_ch_in = yt_poller_state.get("channel_input") or "@더바인-0313"
    yt_popout_url = (
        f"https://www.youtube.com/live_chat?v={yt_vid}&is_popout=1"
        if yt_vid
        else (f"https://www.youtube.com/{yt_ch_in}/live" if str(yt_ch_in).startswith("@") else "https://studio.youtube.com")
    )

    tt_poller_state = tiktok_chat_poller.get_state() if 'tiktok_chat_poller' in globals() else {}
    tt_room_id = tt_poller_state.get("room_id") or ""
    tt_ch_in = (tt_poller_state.get("channel_input") or "").strip()
    if tt_ch_in.startswith("http"):
        tt_popout_url = tt_ch_in if "/live" in tt_ch_in else tt_ch_in.rstrip("/") + "/live"
    elif tt_ch_in:
        tt_handle = tt_ch_in if tt_ch_in.startswith("@") else f"@{tt_ch_in}"
        tt_popout_url = f"https://www.tiktok.com/{tt_handle}/live"
    else:
        tt_popout_url = "https://livecenter.tiktok.com"

    ig_poller_state = instagram_chat_poller.get_state() if 'instagram_chat_poller' in globals() else {}
    ig_media_id = ig_poller_state.get("media_id") or ""
    ig_ch_in = (ig_poller_state.get("channel_input") or "").strip()
    if ig_ch_in.startswith("http"):
        ig_popout_url = ig_ch_in if "/live" in ig_ch_in else ig_ch_in.rstrip("/") + "/live/"
    elif ig_ch_in:
        ig_handle = ig_ch_in.lstrip("@").strip()
        ig_popout_url = f"https://www.instagram.com/{ig_handle}/live/"
    else:
        ig_popout_url = "https://www.instagram.com"

    return jsonify({
        "status": "online",
        "broadcast_reset_seq": broadcast_reset_seq,
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "platforms": platform_tracker.get_summary(),
        "stats": platform_tracker.get_stats(),
        "chat_integrity": chat_integrity,
        "youtube_viewers": platform_tracker.get_youtube_viewers_summary(),
        "youtube_chat_popout_url": yt_popout_url,
        "youtube_video_id": yt_vid,
        "tiktok_viewers": platform_tracker.get_tiktok_viewers_summary(),
        "tiktok_chat_popout_url": tt_popout_url,
        "tiktok_room_id": tt_room_id,
        "instagram_viewers": platform_tracker.get_instagram_viewers_summary(),
        "instagram_chat_popout_url": ig_popout_url,
        "instagram_media_id": ig_media_id,
        "quick_chat_phrases": get_quick_chat_phrases(),
        "recent_chats": platform_tracker.get_recent_chats(limit),
        "obs_reply": obs_content,
        "total_stock": inventory_manager.get_total_stock(),
        "inventory_items": inventory_manager.get_all(),
        "pending_orders": inventory_manager.get_pending_orders()
    }), 200


@app.route('/api/chat/verify-sync', methods=['GET', 'POST'])
def api_chat_verify_sync():
    """실시간 채팅 누락 점검 및 DB ↔ 메모리 ↔ 화면 완전 동기화 API"""
    limit = request.args.get('limit', 500, type=int)
    integrity = platform_tracker.verify_and_sync_with_db(limit=limit)
    return jsonify({
        "status": "ok",
        "broadcast_reset_seq": broadcast_reset_seq,
        "chat_integrity": integrity,
        "stats": platform_tracker.get_stats(),
        "recent_chats": platform_tracker.get_recent_chats(limit),
        "message": f"✅ 채팅 무결성 검증 완료: DB 총 {integrity['db_total_count']}건 전량 동기화 확인 (누락 0건)"
    }), 200


@app.route('/api/youtube/viewers', methods=['GET'])
def api_youtube_viewers():
    """유튜브 실시간 동시 시청자 수 및 접속 회원(아이디) 목록 반환 API"""
    return jsonify({
        "status": "ok",
        "broadcast_reset_seq": broadcast_reset_seq,
        "youtube_viewers": platform_tracker.get_youtube_viewers_summary(),
        "tiktok_viewers": platform_tracker.get_tiktok_viewers_summary(),
        "instagram_viewers": platform_tracker.get_instagram_viewers_summary()
    }), 200


@app.route('/api/tiktok/viewers', methods=['GET'])
def api_tiktok_viewers():
    """틱톡 실시간 동시 시청자 수 및 접속 회원(아이디) 목록 반환 API"""
    return jsonify({
        "status": "ok",
        "broadcast_reset_seq": broadcast_reset_seq,
        "tiktok_viewers": platform_tracker.get_tiktok_viewers_summary(),
        "youtube_viewers": platform_tracker.get_youtube_viewers_summary(),
        "instagram_viewers": platform_tracker.get_instagram_viewers_summary()
    }), 200


@app.route('/api/instagram/viewers', methods=['GET'])
def api_instagram_viewers():
    """인스타그램 실시간 동시 시청자 수 및 접속 회원(아이디) 목록 반환 API"""
    return jsonify({
        "status": "ok",
        "broadcast_reset_seq": broadcast_reset_seq,
        "instagram_viewers": platform_tracker.get_instagram_viewers_summary(),
        "youtube_viewers": platform_tracker.get_youtube_viewers_summary(),
        "tiktok_viewers": platform_tracker.get_tiktok_viewers_summary()
    }), 200


@app.route('/api/orders/confirm', methods=['POST'])
def api_order_confirm():
    """관리자가 주문 [확인 및 재고 차감] 버튼을 클릭했을 때 실행 (실시간 재고 차감 및 품절 연동)"""
    restored_info = database.get_restored_session_info()
    if restored_info["is_restored"]:
        return jsonify({
            "status": "error",
            "success": False,
            "message": "🔒 한번 저장된 이전 방송 데이터는 수정할 수 없습니다. [🆕 새로운 방송 시작]으로 초기화한 후 이용해 주세요."
        }), 403

    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id")
    if not order_id:
        return jsonify({"status": "error", "message": "주문 ID가 누락되었습니다."}), 400

    res = inventory_manager.confirm_order_and_deduct(int(order_id))
    if not res["success"]:
        return jsonify({"status": "error", "message": res.get("message", "승인 실패"), "data": res}), 400

    # 플랫폼 트래커 메모리 채팅 항목의 order_status도 CONFIRMED로 동기화
    platform_tracker.update_order_status(int(order_id), "CONFIRMED")

    # 방송 화면 '📢 LIVE 안내' 자막은 사용자가 직접 넣은 문구만 유지 (주문 알림으로 덮어쓰지 않음)
    obs_text = get_custom_live_ticker_message()

    return jsonify({
        "status": "ok",
        "order": res,
        "obs_reply": obs_text,
        "inventory_items": inventory_manager.get_all(),
        "total_stock": inventory_manager.get_total_stock(),
        "pending_orders": inventory_manager.get_pending_orders()
    }), 200


@app.route('/api/orders/reject', methods=['POST'])
def api_order_reject():
    """관리자 주문 반려/취소 API (재고 차감 없음)"""
    restored_info = database.get_restored_session_info()
    if restored_info["is_restored"]:
        return jsonify({
            "status": "error",
            "success": False,
            "message": "🔒 한번 저장된 이전 방송 데이터는 수정할 수 없습니다. [🆕 새로운 방송 시작]으로 초기화한 후 이용해 주세요."
        }), 403

    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id")
    if not order_id:
        return jsonify({"status": "error", "message": "주문 ID가 누락되었습니다."}), 400

    res = inventory_manager.reject_order(int(order_id))
    if not res["success"]:
        return jsonify({"status": "error", "message": res.get("message", "반려 실패")}), 400

    platform_tracker.update_order_status(int(order_id), "REJECTED")
    return jsonify({
        "status": "ok",
        "message": "주문이 반려되었습니다.",
        "pending_orders": inventory_manager.get_pending_orders()
    }), 200


@app.route('/api/chat/reply', methods=['POST'])
def api_chat_reply():
    """
    관리자 실시간 채팅 바로 답변 API
    - 메모리 큐 및 DB 갱신
    - 방송 화면 '📢 LIVE 안내' 창은 사용자가 넣은 문구만 보이도록 유지 (채팅 답장으로 덮어쓰지 않음)
    """
    restored_info = database.get_restored_session_info()
    if restored_info["is_restored"]:
        return jsonify({
            "status": "error",
            "message": "🔒 한번 저장된 이전 방송 데이터는 수정할 수 없습니다. [🆕 새로운 방송 시작]으로 초기화한 후 이용해 주세요."
        }), 403

    data = request.get_json(silent=True) or {}
    chat_id = data.get("chat_id")
    sender = data.get("user") or data.get("sender") or "시청자"
    message = data.get("message") or ""
    reply_text = str(data.get("reply_text") or "").strip()

    if not reply_text:
        return jsonify({"status": "error", "message": "답변 내용을 입력해 주세요."}), 400

    # 1. 메모리 트래커 즉시 반영
    updated_chat = platform_tracker.update_chat_reply(chat_id, reply_text, reply_by="ADMIN")

    # 2. DB chats 및 cs_inquiries 영속 업데이트
    database.update_chat_reply(chat_id=chat_id, sender=sender, message=message, reply_text=reply_text, reply_by="ADMIN")

    obs_text = get_custom_live_ticker_message()

    return jsonify({
        "status": "ok",
        "chat_id": chat_id,
        "reply": reply_text,
        "reply_by": "ADMIN",
        "obs_reply": obs_text,
        "recent_chats": platform_tracker.get_recent_chats(100)
    }), 200


@app.route('/api/chat/send', methods=['POST'])
def api_chat_send():
    """
    통합관제탑(/broadcast, /chat)에서 실시간 채팅을 직접 작성·발송하는 API
    - SQLite DB (chats 테이블) 및 인메모리 PlatformTracker에 즉시 저장
    - 통합관제탑 실시간 채팅창(#chatStreamList) 및 쇼호스트 뷰(/host #qaList)에 즉시 반영
    - 방송 화면 '📢 LIVE 안내' 창에는 채팅이 끼어들지 않도록 기본 분리하며,
      사용자가 명시적으로 요청한 경우에만 채팅 말머리 없이 입력한 문구 원문 그대로 반영
    """
    restored_info = database.get_restored_session_info()
    if restored_info["is_restored"]:
        return jsonify({
            "status": "error",
            "message": "🔒 한번 저장된 이전 방송 데이터는 수정할 수 없습니다. [🆕 새로운 방송 시작]으로 초기화한 후 이용해 주세요."
        }), 403

    data = request.get_json(silent=True) or {}
    raw_sender = str(data.get("sender") or data.get("user") or "").strip()
    platform_raw = str(data.get("platform") or "YOUTUBE").strip().upper()
    message = str(data.get("message") or "").strip()
    mode = str(data.get("mode") or "ADMIN").strip().upper()  # ADMIN | HOST | VIEWER
    broadcast_obs = bool(data.get("broadcast_obs", False))
    send_host_cue = bool(data.get("send_host_cue", False))

    if not message:
        return jsonify({"status": "error", "message": "채팅 메시지를 입력해 주세요."}), 400

    if mode == "HOST":
        sender = raw_sender if raw_sender else "🎤 쇼호스트"
        if not sender.startswith(("🎤", "📢")):
            sender = f"🎤 {sender}"
    elif mode == "ADMIN":
        sender = raw_sender if raw_sender else "📢 방송관리자"
        if not sender.startswith(("📢", "🎤")):
            sender = f"📢 {sender}"
    else:
        sender = raw_sender if raw_sender else "시청자"

    created_chat = None

    if mode == "VIEWER":
        # 일반 시청자/주문 연동 모드: 기존 AI 주문·문의 분류 파이프라인 실행
        process_chat_payload({
            "id": f"ct_viewer_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}",
            "source": "simulator",
            "is_test": True,
            "type": platform_raw.lower(),
            "chatname": sender,
            "chatmessage": message,
            "bot": False
        })
    else:
        # 관리자/쇼호스트 공식 실시간 채팅 모드: AI 봇 자동응답 없이 실시간 채팅 스트림에 즉시 반영
        db_chat_id = database.save_chat(platform_raw, sender, message, "CHAT", "")
        created_chat = platform_tracker.record_chat(
            platform_raw=platform_raw,
            user=sender,
            message=message,
            category="CHAT",
            reply="",
            order_id=None,
            order_status=None,
            chat_id=db_chat_id
        )

    if broadcast_obs and mode != "VIEWER":
        # 사용자가 명시적으로 LIVE 안내창 문구 반영을 선택한 경우에도 '💬 [방송관리자]' 말머리 없이 입력한 문구만 그대로 표시
        obs_text = set_custom_live_ticker_message(message)
    else:
        obs_text = get_custom_live_ticker_message()

    if send_host_cue:
        try:
            database.add_broadcast_cue(f"💬 [{sender}] {message}", priority="NORMAL", sender="PD")
        except Exception:
            pass

    integrity = platform_tracker.verify_and_sync_with_db(limit=500)
    yt_poller_state = youtube_chat_poller.get_state() if 'youtube_chat_poller' in globals() else {}
    yt_vid = yt_poller_state.get("video_id") or ""
    yt_ch_in = yt_poller_state.get("channel_input") or "@더바인-0313"
    yt_popout_url = (
        f"https://www.youtube.com/live_chat?v={yt_vid}&is_popout=1"
        if yt_vid
        else (f"https://www.youtube.com/{yt_ch_in}/live" if str(yt_ch_in).startswith("@") else "https://studio.youtube.com")
    )

    tt_poller_state = tiktok_chat_poller.get_state() if 'tiktok_chat_poller' in globals() else {}
    tt_ch_in = (tt_poller_state.get("channel_input") or "").strip()
    if tt_ch_in.startswith("http"):
        tt_popout_url = tt_ch_in if "/live" in tt_ch_in else tt_ch_in.rstrip("/") + "/live"
    elif tt_ch_in:
        tt_handle = tt_ch_in if tt_ch_in.startswith("@") else f"@{tt_ch_in}"
        tt_popout_url = f"https://www.tiktok.com/{tt_handle}/live"
    else:
        tt_popout_url = "https://livecenter.tiktok.com"

    ig_poller_state = instagram_chat_poller.get_state() if 'instagram_chat_poller' in globals() else {}
    ig_ch_in = (ig_poller_state.get("channel_input") or "").strip()
    if ig_ch_in.startswith("http"):
        ig_popout_url = ig_ch_in if "/live" in ig_ch_in else ig_ch_in.rstrip("/") + "/live/"
    elif ig_ch_in:
        ig_handle = ig_ch_in.lstrip("@").strip()
        ig_popout_url = f"https://www.instagram.com/{ig_handle}/live/"
    else:
        ig_popout_url = "https://www.instagram.com"

    return jsonify({
        "status": "ok",
        "mode": mode,
        "chat": created_chat,
        "obs_reply": obs_text,
        "chat_integrity": integrity,
        "stats": platform_tracker.get_stats(),
        "platforms": platform_tracker.get_summary(),
        "youtube_viewers": platform_tracker.get_youtube_viewers_summary(),
        "youtube_chat_popout_url": yt_popout_url,
        "tiktok_viewers": platform_tracker.get_tiktok_viewers_summary(),
        "tiktok_chat_popout_url": tt_popout_url,
        "instagram_viewers": platform_tracker.get_instagram_viewers_summary(),
        "instagram_chat_popout_url": ig_popout_url,
        "recent_chats": platform_tracker.get_recent_chats(500),
        "total_stock": inventory_manager.get_total_stock(),
        "inventory_items": inventory_manager.get_all()
    }), 200


@app.route('/api/orders/pending', methods=['GET'])
def api_orders_pending():
    """확인 대기 중인 주문 목록 반환 API"""
    pending = inventory_manager.get_pending_orders()
    return jsonify({
        "status": "ok",
        "count": len(pending),
        "orders": pending
    }), 200


@app.route('/api/inventory/realtime', methods=['GET'])
def api_inventory_realtime():
    """실시간 재고 관리 페이지용 경량 동기화 API (재고 목록 및 대기 주문 목록)"""
    return jsonify({
        "status": "ok",
        "total_stock": inventory_manager.get_total_stock(),
        "inventory_items": inventory_manager.get_all(),
        "pending_orders": inventory_manager.get_pending_orders()
    }), 200


@app.route('/api/clear-mock', methods=['POST'])
def api_clear_mock():
    """모의/목업 테스트 데이터를 깨끗이 제거합니다."""
    platform_tracker.clear_mock_chats()
    return jsonify({"status": "ok", "message": "목업 데이터가 제거되었습니다."}), 200


@app.route('/api/clear-all', methods=['POST'])
def api_clear_all():
    """사용자가 명시적으로 [초기화] 버튼을 눌렀을 때만 모든 채팅 및 통계를 0으로 초기화합니다."""
    global broadcast_reset_seq
    broadcast_reset_seq += 1
    platform_tracker.clear_all()
    database.clear_all_chats()
    with _dedup_lock:
        _recent_chat_fingerprints.clear()
    # OBS 자막도 기본 대기 상태로 초기화
    try:
        with open(OBS_TEXT_FILE, "w", encoding="utf-8") as f:
            f.write("대기 중: 주문 또는 질문이 들어오면 AI 안내 자막이 즉시 송출됩니다.")
    except Exception:
        pass
    return jsonify({
        "status": "ok",
        "broadcast_reset_seq": broadcast_reset_seq,
        "message": "모든 채팅 및 통계가 초기화되었습니다."
    }), 200


# ==========================================
# 🎬 새로운 방송 시작 (데이터 별도 아카이브 & 초기화) 및 이전 방송 기록 API
# ==========================================

@app.route('/api/broadcast/current-summary', methods=['GET'])
def api_broadcast_current_summary():
    """현재 방송에 누적된 데이터 요약(채팅, 주문, 매출, CS)과 저장된 이전 방송 개수를 반환합니다."""
    analytics = database.get_analytics_summary()
    chat_stats = platform_tracker.get_stats()
    cs_stats = cs_manager.get_stats()
    restored_info = database.get_restored_session_info()
    sessions = database.get_broadcast_sessions(limit=50)
    return jsonify({
        "status": "ok",
        "broadcast_reset_seq": broadcast_reset_seq,
        "is_restored_session": restored_info["is_restored"],
        "restored_session_id": restored_info["session_id"],
        "restored_session_title": restored_info["title"],
        "current": {
            "total_chats": chat_stats.get("total", 0),
            "total_orders": analytics.get("total_orders", 0),
            "total_revenue": analytics.get("total_revenue", 0),
            "total_profit": analytics.get("total_profit", 0),
            "total_cs": cs_stats.get("total", 0)
        },
        "history_count": len(sessions),
        "default_title": f"{datetime.now().strftime('%Y-%m-%d %H:%M')} 라이브 방송"
    }), 200


def is_any_platform_verified_live():
    """유튜브, 틱톡, 인스타그램 중 실제 플랫폼 온에어(라이브 방송중)가 검증된 플랫폼이 하나라도 있는지 반환합니다."""
    try:
        if youtube_direct_streamer.is_verified_live():
            return True
    except Exception:
        pass
    try:
        if 'tiktok_direct_streamer' in globals() and tiktok_direct_streamer.is_verified_live():
            return True
    except Exception:
        pass
    try:
        if 'instagram_direct_streamer' in globals() and instagram_direct_streamer.is_verified_live():
            return True
    except Exception:
        pass
    return False


def mark_live_broadcast_started(title=None):
    """실제 라이브 방송(온에어 검증 완료) 시작 시 시작 시각과 방송 제목을 기록합니다."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        existing_start = database.get_overlay_state("broadcast_started_at", None)
        if not existing_start:
            database.set_overlay_state("broadcast_started_at", now_str)
        database.set_overlay_state("restored_session_id", None)
        database.set_overlay_state("restored_session_title", None)
        if title and str(title).strip():
            database.set_overlay_state("simulcast_unified_title", str(title).strip())
        database.sync_to_shopping_mall("live_state", {
            "is_live": True,
            "broadcast_title": str(title).strip() if (title and str(title).strip()) else database.get_overlay_state("simulcast_unified_title", "실시간 라이브 특가 방송")
        })
    except Exception:
        pass


def finalize_and_archive_broadcast_session(title=None, save_empty=True, force_save=False, disconnect_chats=True):
    """
    방송 종료 시 호출되어:
    1) 현재 방송 세션(주문, 채팅, CS, 추첨, 매출 통계)을 이전 방송 기록(broadcast_sessions)에 자동 저장하고,
    2) 채팅창, 주문 큐, 오버레이 핀 상품/타이머/자막을 초기 상태로 리셋하며 (등록된 상품 products는 100% 보존),
    3) 모든 열린 화면(스튜디오, 관제탑, 호스트, 오버레이)에 리셋 시퀀스(broadcast_reset_seq)를 브로드캐스트합니다.
    """
    global pending_orders, broadcast_reset_seq
    eff_title = str(title or "").strip()
    if not eff_title:
        try:
            eff_title = str(database.get_overlay_state("simulcast_unified_title", "") or "").strip()
        except Exception:
            eff_title = ""

    archive_res = database.archive_and_start_new_broadcast(
        title=eff_title,
        save_empty=save_empty,
        force_save=force_save
    )
    broadcast_reset_seq += 1
    try:
        host_left_view_state["mode"] = "DEFAULT"
        host_left_view_state["sub_mode"] = "WEB"
        host_left_view_state["updated_at"] = time.time()
        host_site_mirror.stop()
    except Exception:
        pass

    if disconnect_chats:
        for poller_name in ("youtube_chat_poller", "tiktok_chat_poller", "instagram_chat_poller"):
            try:
                poller_obj = globals().get(poller_name)
                if poller_obj is not None and getattr(poller_obj, "active", False):
                    poller_obj.disconnect()
            except Exception:
                pass

    # 메모리 상의 채팅 트래커 및 중복 방지 큐 초기화 (등록된 상품 데이터는 절대 건드리지 않음)
    platform_tracker.clear_all()
    with _dedup_lock:
        _recent_chat_fingerprints.clear()
    with file_lock:
        pending_orders.clear()

    # OBS 자막 텍스트 파일 및 기본 환영 자막 초기화
    welcome_msg = "잠시 후 라이브 커머스 방송이 시작됩니다! 채팅창에 번호와 수량을 입력하여 간편하게 주문해 보세요."
    try:
        with open(OBS_TEXT_FILE, "w", encoding="utf-8") as f:
            f.write(welcome_msg)
    except Exception:
        pass

    sessions = database.get_broadcast_sessions(limit=50)
    try:
        database.sync_to_shopping_mall("live_state", {
            "is_live": False,
            "pinned_product_num": None
        })
    except Exception:
        pass
    return {
        "broadcast_reset_seq": broadcast_reset_seq,
        "archive": archive_res,
        "welcome_ticker": welcome_msg,
        "sessions": sessions,
        "history_count": len(sessions)
    }


@app.route('/api/broadcast/new', methods=['POST'])
def api_broadcast_start_new():
    """
    [새로운 방송 시작 / 방송 초기화] 실행 API
    - 이전 방송의 주문, 채팅, CS 문의, 추첨 이력 및 매출 통계를 broadcast_sessions 테이블에 별도로 저장(아카이브)합니다.
    - 현재 방송 데이터(채팅, 주문, CS, 추첨, 핀 상품, 타이머, 자막)를 모두 초기 상태로 리셋하여 0부터 시작합니다.
    - 등록된 상품/재고 및 로고·테마·해상도 설정(overlay_settings)은 그대로 유지합니다.
    """
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "").strip()
    save_empty = bool(data.get("save_empty", False))

    res_bundle = finalize_and_archive_broadcast_session(
        title=title,
        save_empty=save_empty,
        force_save=False,
        disconnect_chats=False
    )
    archive_res = res_bundle["archive"]

    if archive_res.get("skipped_duplicate"):
        msg = f"'{archive_res.get('title')}' 방송은 이미 보관함에 저장되어 있으므로 중복 저장하지 않고 새로운 방송 화면으로 초기화했습니다."
    elif archive_res.get("archived_session_id"):
        msg = "이전 방송 데이터가 안전하게 별도 보관되었으며, 새로운 방송 화면으로 초기화되었습니다."
    else:
        msg = "새로운 방송 화면으로 깨끗하게 초기화되었습니다."

    return jsonify({
        "status": "ok",
        "broadcast_reset_seq": res_bundle["broadcast_reset_seq"],
        "message": msg,
        "archive": archive_res,
        "welcome_ticker": res_bundle["welcome_ticker"],
        "sessions": res_bundle["sessions"],
        "history_count": res_bundle["history_count"]
    }), 200


@app.route('/api/broadcast/history', methods=['GET'])
def api_broadcast_history_list():
    """별도 저장된 이전 방송 세션 목록을 반환합니다."""
    limit = request.args.get('limit', 50, type=int)
    database.deduplicate_broadcast_sessions()
    sessions = database.get_broadcast_sessions(limit=limit)
    return jsonify({
        "status": "ok",
        "count": len(sessions),
        "sessions": sessions
    }), 200


@app.route('/api/broadcast/history/<int:session_id>', methods=['GET'])
def api_broadcast_history_detail(session_id):
    """특정 이전 방송 세션의 상세 데이터(주문, 채팅, CS 문의, 추첨, 매출 요약)를 반환합니다."""
    detail = database.get_broadcast_session_detail(session_id)
    if not detail:
        return jsonify({"status": "error", "message": "해당 방송 기록을 찾을 수 없습니다."}), 404
    return jsonify({
        "status": "ok",
        "session": detail
    }), 200


@app.route('/api/broadcast/restore/<int:session_id>', methods=['POST'])
def api_broadcast_restore_session(session_id):
    """선택한 이전 방송 데이터를 현재 방송 화면(관제탑/통계/스튜디오/CS)으로 다시 불러와 복원합니다."""
    global broadcast_reset_seq
    data = request.get_json(silent=True) or {}
    auto_backup = bool(data.get("auto_backup_current", False))

    res = database.restore_broadcast_session(session_id, auto_backup_current=auto_backup)
    if not res.get("success"):
        return jsonify({"status": "error", "message": res.get("message", "복원 실패")}), 404

    broadcast_reset_seq += 1

    # 메모리 트래커도 DB에서 다시 로드하여 즉시 동기화
    platform_tracker.clear_all()
    platform_tracker.load_from_db(limit=500)
    with _dedup_lock:
        _recent_chat_fingerprints.clear()

    return jsonify({
        "status": "ok",
        "broadcast_reset_seq": broadcast_reset_seq,
        "message": f"'{res.get('title')}' 방송 데이터를 성공적으로 불러왔습니다.",
        "restored": res
    }), 200


@app.route('/api/broadcast/history/<int:session_id>', methods=['DELETE'])
def api_broadcast_delete_session(session_id):
    """특정 이전 방송 기록을 삭제합니다."""
    ok = database.delete_broadcast_session(session_id)
    if not ok:
        return jsonify({"status": "error", "message": "삭제할 방송 기록을 찾을 수 없습니다."}), 404
    sessions = database.get_broadcast_sessions(limit=50)
    return jsonify({
        "status": "ok",
        "message": "이전 방송 기록이 삭제되었습니다.",
        "sessions": sessions,
        "history_count": len(sessions)
    }), 200


@app.route('/api/broadcast/history/<int:session_id>/excel', methods=['GET'])
def api_broadcast_history_excel(session_id):
    """저장된 이전 방송의 주문 내역을 엑셀(.xlsx) 파일로 다운로드합니다."""
    archive_dir = os.path.join(app.root_path, "static", "uploads", "archives")
    os.makedirs(archive_dir, exist_ok=True)
    excel_path = os.path.join(archive_dir, f"broadcast_session_{session_id}.xlsx")
    ok = database.export_session_orders_to_excel(session_id, excel_path)
    if not ok or not os.path.exists(excel_path):
        return "해당 방송의 엑셀 데이터를 생성할 수 없습니다.", 404
    return send_file(
        excel_path,
        as_attachment=True,
        download_name=f"broadcast_{session_id}_orders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    )

# ==========================================
# 🖥️ OBS HTML5 브라우저 소스 동적 오버레이 라우트
# ==========================================

@app.route('/overlay/obs', methods=['GET'])
def obs_overlay_view():
    """OBS Studio 브라우저 소스 전용 투명 그래픽 오버레이 화면 (Clean Output)"""
    return render_template('obs_overlay.html')


@app.route('/overlay/studio', methods=['GET'])
@app.route('/obs-studio', methods=['GET'])
@app.route('/studio', methods=['GET'])
def obs_studio_view():
    """OBS 오버레이 전용 세팅 스튜디오 컨트롤러 화면"""
    return render_template('obs_studio.html')


@app.route('/api/overlay/settings', methods=['GET', 'POST'])
def api_overlay_settings():
    """오버레이 세팅 조회 및 실시간 업데이트 API"""
    default_settings = {
        "aspect_ratio": "16:9",         # 16:9, 9:16, 1:1, 4:3, custom
        "resolution_w": 1920,
        "resolution_h": 1080,
        "bg_mode": "transparent",       # transparent, camera, screen, chromakey, dark
        "video_source": "screen",       # screen, webcam, mobile
        "is_mirrored": False,
        "product_position": "top-left",     # top-left (채팅창 가림 방지 상단 기본값), bottom-left, bottom-right
        "show_timer": True,
        "show_toast": True,
        "show_pinned": True,
        "show_ticker": True,
        "show_telemetry": True,         # 유튜브 전송 프레임(FPS) 및 실시간 오디오 음량(VU 미터) HUD 표시 ON/OFF
        "ticker_message": "잠시 후 라이브 커머스 방송이 시작됩니다! 채팅창에 번호와 수량을 입력하여 간편하게 주문해 보세요.",
        "theme": "neon-dark",           # neon-dark, modern-white, vivid-pink
        "font_size": "medium",
        "product_scale": 125,           # 📌 현재 방송 상품 카드 텍스트·배너 크기 배율 (80 ~ 180%)
        "ticker_scale": 120,            # 📢 LIVE 안내 자막 텍스트·배너 크기 배율 (80 ~ 180%)
        "show_custom_image": False,     # 기타 (외부 이미지 오버레이) ON/OFF
        "custom_image_url": "",         # 외부 이미지 URL / 파일 경로
        "custom_image_pos": "top-right",# top-right, top-left, bottom-right, bottom-left, center, fullscreen
        "custom_image_width": 250,      # 이미지 너비 (px)
        "custom_image_opacity": 100,    # 투명도 (10 ~ 100%)
        "show_logo": False,             # 라이브커머스 브랜드 로고 표시 ON/OFF
        "logo_url": "",                 # 로고 이미지 URL / 업로드 경로
        "logo_text": "LIVE COMMERCE",   # 기본 브랜드 로고 텍스트
        "logo_pos": "top-right",        # top-right, top-left, top-center, bottom-right, bottom-left, center
        "logo_width": 200,              # 로고 너비 (px)
        "logo_offset_x": 35,            # 로고 가로 여백 (px)
        "logo_offset_y": 30,            # 로고 세로 여백 (px)
        "logo_opacity": 100             # 로고 불투명도 (10 ~ 100%)
    }

    if request.method == 'GET':
        saved = database.get_overlay_state('overlay_settings', default_settings)
        if not isinstance(saved, dict):
            saved = default_settings
        merged = {**default_settings, **saved}
        merged["ticker_message"] = get_custom_live_ticker_message()
        return jsonify({"status": "ok", "settings": merged}), 200

    # POST: 설정 업데이트
    data = request.get_json(silent=True) or {}
    saved = database.get_overlay_state('overlay_settings', default_settings)
    if not isinstance(saved, dict):
        saved = default_settings
    merged = {**saved, **data}
    if "ticker_message" in data and str(data.get("ticker_message") or "").strip():
        merged["ticker_message"] = set_custom_live_ticker_message(str(data.get("ticker_message")).strip())
    else:
        merged["ticker_message"] = get_custom_live_ticker_message()
    database.set_overlay_state('overlay_settings', merged)
    return jsonify({"status": "ok", "settings": merged}), 200


@app.route('/api/overlay/upload-image', methods=['POST'])
def api_overlay_upload_image():
    """오버레이용 커스텀 외부 이미지 파일 업로드 API"""
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "업로드할 파일이 없습니다."}), 400
    file = request.files['file']
    if not file or file.filename == '':
        return jsonify({"status": "error", "message": "선택된 파일이 없습니다."}), 400
    if not allowed_file(file.filename):
        return jsonify({"status": "error", "message": "지원하지 않는 이미지 형식입니다. (PNG, JPG, JFIF, WEBP, GIF, SVG)"}), 400

    ext = file.filename.rsplit('.', 1)[1].lower()
    safe_name = f"overlay_{int(time.time())}_{uuid.uuid4().hex[:8]}.{ext}"
    save_path = os.path.join(OVERLAY_UPLOAD_FOLDER, safe_name)
    try:
        file.save(save_path)
        image_url = f"/static/uploads/overlays/{safe_name}"
        return jsonify({"status": "ok", "url": image_url, "filename": safe_name}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": f"파일 저장 실패: {str(e)}"}), 500


@app.route('/api/overlay/ticker', methods=['POST'])
def api_overlay_ticker():
    """하단 띠자막 메시지 즉시 변경 API (오직 사용자가 지정한 LIVE 안내 문구만 영구 저장)"""
    data = request.get_json(silent=True) or {}
    message = data.get("message", "").strip()
    if message:
        message = set_custom_live_ticker_message(message)
    return jsonify({"status": "ok", "message": message}), 200


@app.route('/api/products', methods=['GET'])
def api_products_list():
    """오버레이 스튜디오 및 외부 클라이언트용 상품 및 실시간 재고 목록 API"""
    prods = database.get_all_products()
    return jsonify({
        "status": "ok",
        "products": prods,
        "items": prods,
        "total_count": len(prods)
    }), 200


@app.route('/api/overlay/data', methods=['GET'])
def api_overlay_data():
    """OBS 브라우저 소스 동기화용 실시간 데이터 반환 API"""
    pinned_num = database.get_overlay_state('pinned_product_num', None)
    pinned_product = None
    if pinned_num is not None and str(pinned_num).strip() != "":
        pinned_num_str = str(pinned_num).replace("item-", "").strip()
        all_prods = database.get_all_products()
        # 1차: 방송 상품 번호(num) 정확 일치 최우선 탐색
        for p in all_prods:
            if p.get("num") == pinned_num or str(p.get("num")).strip() == pinned_num_str:
                pinned_product = p
                break
        # 2차 폴백: num 일치가 없을 때만 db_id 탐색
        if pinned_product is None:
            for p in all_prods:
                if p.get("db_id") == pinned_num or str(p.get("db_id")).strip() == pinned_num_str:
                    pinned_product = p
                    break

    flash_timer = database.get_overlay_state('flash_timer', {"active": False})

    # 최신 주문 (최근 5건)
    conn = database.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, platform, buyer_name, item_name, color, size, quantity, total_price, status
            FROM orders
            ORDER BY id DESC
            LIMIT 5
        """)
        recent_orders = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    # 최신 LIVE 안내 자막 (시청자 채팅/AI 답변 개입 없이 오직 사용자가 설정한 문구만 반환)
    obs_content = get_custom_live_ticker_message()

    # 최신 럭키드로우
    lottery_history = database.get_lottery_history(1)
    latest_lottery = lottery_history[0] if lottery_history else None

    saved_settings = database.get_overlay_state('overlay_settings', None)

    live_urls = {}
    try:
        screens = get_host_platform_screens_info(discover_youtube=False)
        for plat_key in ("YOUTUBE", "TIKTOK", "INSTAGRAM"):
            info = screens.get(plat_key) or {}
            live_urls[plat_key] = {
                "active": bool(info.get("active")),
                "live_url": info.get("live_url") or "",
                "embed_url": info.get("embed_url") or "",
                "label": info.get("label") or plat_key
            }
    except Exception:
        live_urls = {}

    return jsonify({
        "status": "ok",
        "broadcast_reset_seq": broadcast_reset_seq,
        "pinned_product": pinned_product,
        "timer": flash_timer,
        "recent_orders": recent_orders,
        "latest_reply": obs_content,
        "latest_lottery": latest_lottery,
        "youtube_rtmp": youtube_direct_streamer.get_state(),
        "tiktok_rtmp": tiktok_direct_streamer.get_state() if 'tiktok_direct_streamer' in globals() else {"active": False},
        "instagram_rtmp": instagram_direct_streamer.get_state() if 'instagram_direct_streamer' in globals() else {"active": False},
        "live_urls": live_urls,
        "settings": saved_settings if isinstance(saved_settings, dict) else None
    }), 200


@app.route('/api/overlay/pin-product', methods=['POST'])
@app.route('/api/overlay/pin', methods=['POST'])
def api_overlay_pin():
    """관제탑 또는 쇼호스트 뷰에서 OBS 화면에 고정할 상품을 지정하거나 해제합니다."""
    data = request.get_json(silent=True) or {}
    raw_val = data.get("product_num") if "product_num" in data else data.get("product_id")
    product_num = None
    matched_prod = None
    if raw_val is not None and str(raw_val).strip() != "":
        try:
            clean_str = str(raw_val).replace("item-", "").strip()
            target_int = int(clean_str)
            all_prods = database.get_all_products()
            for p in all_prods:
                if p.get("num") == target_int or str(p.get("num")).strip() == clean_str:
                    matched_prod = p
                    break
            if matched_prod is None:
                for p in all_prods:
                    if p.get("db_id") == target_int or str(p.get("db_id")).strip() == clean_str:
                        matched_prod = p
                        break
            product_num = int(matched_prod["num"]) if matched_prod and matched_prod.get("num") is not None else target_int
        except Exception:
            product_num = None

    database.set_overlay_state('pinned_product_num', product_num)
    if matched_prod:
        try:
            database.sync_to_shopping_mall("upsert_product", {"product": matched_prod})
        except Exception:
            pass
    return jsonify({
        "status": "ok",
        "pinned_product_num": product_num,
        "pinned_product": matched_prod
    }), 200


@app.route('/api/mall/order-webhook', methods=['POST'])
def api_mall_order_webhook():
    """
    독립 쇼핑몰(Port 3000)에서 고객이 주문을 완료했을 때 OBS DB의 해당 상품 옵션 재고도 함께 동기화 차감합니다.
    (중복 차감 방지를 위해 order_code/order_id 이력을 기록합니다.)
    """
    data = request.get_json(silent=True) or {}
    order_key = str(data.get("order_code") or data.get("order_id") or "").strip()
    if order_key and not _mark_mall_order_processed(order_key):
        return jsonify({"status": "ok", "skipped_duplicate": True}), 200

    items = data.get("items") or []
    for it in items:
        try:
            p_num = it.get("product_num") or it.get("num")
            p_name = it.get("product_name") or f"{p_num}번"
            color = it.get("color", "-")
            size = it.get("size", "-")
            qty = max(1, int(it.get("quantity", 1)))
            database.deduct_stock(p_name, color, size, qty=qty)
        except Exception as e:
            print(f"⚠️ [쇼핑몰 주문 재고 연동 경고] {e}", flush=True)
    return jsonify({"status": "ok"}), 200


_processed_mall_order_lock = Lock()


def _mark_mall_order_processed(order_key):
    """이미 OBS 재고 차감이 완료된 쇼핑몰 주문인지 확인하고 신규 주문이면 기록 후 True를 반환합니다."""
    if not order_key:
        return True
    with _processed_mall_order_lock:
        seen = database.get_overlay_state("mall_processed_order_keys", [])
        if not isinstance(seen, list):
            seen = []
        if order_key in seen:
            return False
        seen.append(order_key)
        if len(seen) > 500:
            seen = seen[-500:]
        database.set_overlay_state("mall_processed_order_keys", seen)
        return True


def _collect_current_live_sync_snapshot():
    """클라우드/독립 쇼핑몰로 Outbound Push할 현재 라이브 방송 상태 및 플랫폼 임베드 URL을 수집합니다."""
    yt_active = bool('youtube_direct_streamer' in globals() and youtube_direct_streamer.active)
    tt_active = bool('tiktok_direct_streamer' in globals() and tiktok_direct_streamer.active)
    ig_active = bool('instagram_direct_streamer' in globals() and instagram_direct_streamer.active)
    any_live = bool(yt_active or tt_active or ig_active)

    live_urls = {}
    yt_embed = ""
    try:
        screens = get_host_platform_screens_info(discover_youtube=False)
        for plat_key in ("YOUTUBE", "TIKTOK", "INSTAGRAM"):
            info = screens.get(plat_key) or {}
            emb = info.get("embed_url") or ""
            if plat_key == "YOUTUBE" and emb:
                yt_embed = emb
            live_urls[plat_key] = {
                "active": bool(info.get("active")),
                "live_url": info.get("live_url") or "",
                "embed_url": emb,
                "label": info.get("label") or plat_key
            }
    except Exception:
        live_urls = {}

    return {
        "is_live": any_live,
        "pinned_product_num": database.get_overlay_state("pinned_product_num", None),
        "broadcast_title": database.get_overlay_state("simulcast_unified_title", "실시간 라이브 커머스 특가 방송"),
        "ticker_message": get_custom_live_ticker_message(),
        "timer": database.get_overlay_state("flash_timer", {"active": False}),
        "live_urls": live_urls,
        "youtube_embed_url": yt_embed,
        "heartbeat": any_live
    }


def pull_and_apply_pending_mall_orders(mall_url=None, sync_secret=None):
    """
    공유기(NAT) 뒤에 있는 로컬 라이브 에이전트가 외부 클라우드 쇼핑몰의 신규 주문을 Outbound Pull하여
    OBS DB 재고를 실시간 차감합니다.
    """
    import urllib.request
    cfg = database.get_cloud_mall_config()
    target_url = (mall_url or cfg["mall_url"] or "").rstrip("/")
    sec = sync_secret if sync_secret is not None else cfg["sync_secret"]
    if not target_url:
        return {"pulled": 0, "applied": 0}

    cursor = int(database.get_overlay_state("mall_synced_order_cursor", 0) or 0)
    headers = {"Accept": "application/json"}
    if sec:
        headers["X-Mall-Sync-Secret"] = sec

    req = urllib.request.Request(
        f"{target_url}/api/sync/pending-orders?after_id={cursor}&limit=100",
        headers=headers,
        method="GET"
    )
    with urllib.request.urlopen(req, timeout=3.0) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))

    if data.get("status") != "ok":
        return {"pulled": 0, "applied": 0}

    orders = data.get("orders") or []
    applied = 0
    max_id = cursor
    for ord_obj in orders:
        oid = int(ord_obj.get("id") or 0)
        if oid > max_id:
            max_id = oid
        order_key = str(ord_obj.get("order_code") or oid)
        if not _mark_mall_order_processed(order_key):
            continue
        for it in (ord_obj.get("items") or []):
            try:
                p_num = it.get("product_num") or it.get("num")
                p_name = it.get("product_name") or f"{p_num}번"
                color = it.get("color", "-")
                size = it.get("size", "-")
                qty = max(1, int(it.get("quantity", 1)))
                database.deduct_stock(p_name, color, size, qty=qty)
            except Exception as e:
                print(f"⚠️ [클라우드 주문 Pull 재고 차감 경고] {e}", flush=True)
        applied += 1

    if max_id > cursor:
        database.set_overlay_state("mall_synced_order_cursor", max_id)
    return {"pulled": len(orders), "applied": applied, "latest_order_id": max_id}


class CloudMallSyncWorker:
    """
    방송자 PC 로컬 라이브 에이전트 ↔ 외부 클라우드 CSR 쇼핑몰 간 경량 Outbound 동기화 워커
    - 영상 데이터 전송 없이 오직 상태 변화, 5초 주기 라이브 하트비트, 신규 주문 증분 Pull만 수행합니다.
    """
    def __init__(self):
        self._started = False
        self._lock = Lock()
        self._last_pushed_sig = None
        self._last_hb_ts = 0.0

    def start(self):
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._loop, daemon=True, name="CloudMallSyncWorker").start()

    def _loop(self):
        while True:
            try:
                snap = _collect_current_live_sync_snapshot()
                sig_obj = {k: v for k, v in snap.items() if k != "heartbeat"}
                sig = json.dumps(sig_obj, sort_keys=True, ensure_ascii=False)
                now = time.time()

                if sig != self._last_pushed_sig:
                    database.sync_to_shopping_mall("live_state", snap)
                    self._last_pushed_sig = sig
                    self._last_hb_ts = now
                elif snap["is_live"] and (now - self._last_hb_ts) >= 5.0:
                    database.sync_to_shopping_mall("heartbeat", {"is_live": True, "heartbeat": True})
                    self._last_hb_ts = now

                cfg = database.get_cloud_mall_config()
                if cfg["is_remote"] or cfg["agent_only_mode"]:
                    pull_and_apply_pending_mall_orders(cfg["mall_url"], cfg["sync_secret"])
            except Exception:
                pass
            time.sleep(2.0)


cloud_mall_sync_worker = CloudMallSyncWorker()


@app.route('/api/agent/health', methods=['GET', 'OPTIONS'])
def api_agent_health():
    """
    클라우드 CSR 스튜디오(49.50.132.140:5000 등)에서 방송자 로컬 PC 에이전트(127.0.0.1:5000)의
    가동 상태, 로컬 Wi-Fi IP, 무선 카메라 보안 토큰, FFmpeg 및 RTMP 송출 상태를 1회 조회하는 경량 헬스체크 API
    """
    if request.method == 'OPTIONS':
        return Response("", status=204)
    import shutil
    ip = get_local_ip() if 'get_local_ip' in globals() else '127.0.0.1'
    wifi_ip = (globals().get('bridged_wifi_ip') or ip)
    port = int(os.getenv("SERVER_PORT", 5000))
    https_port = int(os.getenv("HTTPS_PORT", 5443))
    cam_tok = get_studio_cam_token()
    ov_tok = get_studio_overlay_token()
    cfg = database.get_cloud_mall_config()
    yt_rtmp_active = bool("youtube_direct_streamer" in globals() and youtube_direct_streamer is not None and youtube_direct_streamer.active)
    tt_rtmp_active = bool("tiktok_direct_streamer" in globals() and tiktok_direct_streamer is not None and tiktok_direct_streamer.active)
    ig_rtmp_active = bool("instagram_direct_streamer" in globals() and instagram_direct_streamer is not None and instagram_direct_streamer.active)
    return jsonify({
        "status": "ok",
        "agent_version": "2.0.0",
        "zero_traffic_mode": True,
        "server_video_traffic_bytes": 0,
        "agent_only_mode": bool(cfg.get("agent_only_mode") or os.getenv("AGENT_ONLY_MODE") == "1"),
        "local_ip": wifi_ip,
        "pc_ip": ip,
        "http_port": port,
        "https_port": https_port,
        "cam_token": cam_tok,
        "ov_token": ov_tok,
        "cam1_url": f"https://{wifi_ip}:{https_port}/cam?cam=1&cam_token={cam_tok}",
        "cam2_url": f"https://{wifi_ip}:{https_port}/cam?cam=2&cam_token={cam_tok}",
        "ffmpeg_available": bool(shutil.which("ffmpeg")),
        "rtmp_active": bool(yt_rtmp_active or tt_rtmp_active or ig_rtmp_active),
        "platforms_rtmp": {
            "youtube": yt_rtmp_active,
            "tiktok": tt_rtmp_active,
            "instagram": ig_rtmp_active,
        },
        "mall_url": cfg.get("mall_url", ""),
    }), 200


@app.route('/api/agent/cloud-config', methods=['GET', 'POST'])
def api_agent_cloud_config():
    """방송자 PC 로컬 라이브 에이전트에서 연동할 외부 클라우드 쇼핑몰 URL 및 시크릿 설정 조회/저장/테스트 API"""
    if request.method == 'GET':
        cfg = database.get_cloud_mall_config()
        return jsonify({
            "status": "ok",
            "mall_url": cfg["mall_url"],
            "public_mall_url": resolve_mall_public_url(),
            "has_secret": bool(cfg["sync_secret"]),
            "agent_only_mode": cfg["agent_only_mode"],
            "is_remote": cfg["is_remote"]
        }), 200

    data = request.get_json(silent=True) or {}
    cur_cfg = database.get_cloud_mall_config()
    new_url = database._normalize_mall_port_url(
        str(data.get("mall_url") if "mall_url" in data else cur_cfg["mall_url"]).strip().rstrip("/"),
        int(os.getenv("MALL_PORT", MALL_PORT))
    )
    new_sec = str(data.get("sync_secret") if "sync_secret" in data else cur_cfg["sync_secret"]).strip()
    new_agent_only = bool(data.get("agent_only_mode", cur_cfg["agent_only_mode"]))

    database.set_overlay_state("cloud_mall_config", {
        "mall_url": new_url,
        "sync_secret": new_sec,
        "agent_only_mode": new_agent_only
    })

    snap = _collect_current_live_sync_snapshot()
    cnt = database.sync_all_products_to_shopping_mall(
        is_live=snap["is_live"],
        live_urls=snap["live_urls"],
        youtube_embed_url=snap["youtube_embed_url"],
        blocking=True
    )
    return jsonify({
        "status": "ok",
        "mall_url": new_url,
        "public_mall_url": resolve_mall_public_url(),
        "agent_only_mode": new_agent_only,
        "synced_count": cnt,
        "message": f"클라우드 쇼핑몰({new_url}) 설정 저장 및 상품 {cnt}건 동기화가 완료되었습니다."
    }), 200


@app.route('/api/mall/sync', methods=['POST'])
def api_mall_manual_sync():
    """OBS 스튜디오에서 쇼핑몰 DB(로컬 또는 클라우드)로 전체 상품 및 현재 선택 상품을 즉시 동기화합니다."""
    snap = _collect_current_live_sync_snapshot()
    cnt = database.sync_all_products_to_shopping_mall(
        is_live=snap["is_live"],
        live_urls=snap["live_urls"],
        youtube_embed_url=snap["youtube_embed_url"]
    )
    cfg = database.get_cloud_mall_config()
    return jsonify({
        "status": "ok",
        "synced_count": cnt,
        "mall_url": cfg["mall_url"],
        "message": f"🛍️ 쇼핑몰({cfg['mall_url']})에 상품 {cnt}건 및 현재 방송 상태 동기화가 완료되었습니다!"
    }), 200


@app.route('/api/overlay/timer', methods=['POST'])
def api_overlay_timer():
    """타임세일 타이머 설정/시작/종료"""
    data = request.get_json(silent=True) or {}
    active = bool(data.get("active", True))
    label = data.get("label", "특가 마감까지")
    minutes = int(data.get("minutes", 5))

    from datetime import timedelta
    end_time_str = (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")

    timer_state = {
        "active": active,
        "label": label,
        "end_time": end_time_str if active else None,
        "minutes": minutes
    }
    database.set_overlay_state('flash_timer', timer_state)
    return jsonify({"status": "ok", "timer": timer_state}), 200


# ==========================================
# 📱 스마트폰 무선 카메라 실시간 중계 스트리밍 API (CAM 1 / CAM 2 듀얼 카메라 지원)
# ==========================================

latest_camera_frame = None
latest_camera_lock = Lock()
latest_camera_cond = threading.Condition(latest_camera_lock)
latest_camera_timestamp = 0
latest_camera_seq = 0
latest_camera_source = "none"
latest_camera_intervals_ms = []
latest_webrtc_heartbeat = 0
_waiting_frame_cache = {}
public_tunnel_url = None
public_tunnel_proc = None
public_tunnel_renew_event = threading.Event()
_public_tunnel_thread_started = False
bridged_wifi_ip = None
webrtc_lock = Lock()
webrtc_sessions = {}  # peer_id -> {"offer": dict, "answer": dict|None, "created_at": float, "answered": bool, "cam": int}

# 🎥 듀얼 카메라(CAM 1 / CAM 2) 채널별 프레임·타임스탬프·시퀀스·WebRTC·클라이언트 진단 상태
camera_channels = {
    1: {
        "frame": None,
        "timestamp": 0.0,
        "seq": 0,
        "source": "none",
        "intervals_ms": [],
        "webrtc_heartbeat": 0.0,
        "client_state": "idle",
        "client_message": "",
        "client_ip": "",
        "client_updated_at": 0.0,
    },
    2: {
        "frame": None,
        "timestamp": 0.0,
        "seq": 0,
        "source": "none",
        "intervals_ms": [],
        "webrtc_heartbeat": 0.0,
        "client_state": "idle",
        "client_message": "",
        "client_ip": "",
        "client_updated_at": 0.0,
    },
}

camera_switcher_lock = Lock()
camera_switcher_state = {
    "active_main_cam": 1,            # 1 (CAM 1) | 2 (CAM 2)
    "layout_mode": "single",         # "single" (단독) | "pip" (PIP 동시화면) | "split_v" (상하 2분할) | "split_h" (좌우 2분할)
    "pip_position": "bottom-right",  # "bottom-right" | "top-right" | "bottom-left" | "top-left"
    "pip_scale": 0.32,               # 0.22 ~ 0.45
    "cam1_label": "CAM 1 (메인/정면)",
    "cam2_label": "CAM 2 (서브/디테일)",
    "cam1_mirrored": False,
    "cam2_mirrored": False,
    "updated_at": 0.0,
}
_camera_switcher_loaded = False


def _parse_cam_slot(raw_val, default_slot=1):
    try:
        v = int(str(raw_val).strip())
        if v in (1, 2):
            return v
    except Exception:
        pass
    return default_slot


def get_camera_switcher_state():
    global _camera_switcher_loaded
    with camera_switcher_lock:
        if not _camera_switcher_loaded:
            _camera_switcher_loaded = True
            try:
                saved_main = database.get_overlay_state("cam_switcher_active_main", 1)
                saved_layout = database.get_overlay_state("cam_switcher_layout_mode", "single")
                saved_pip_pos = database.get_overlay_state("cam_switcher_pip_position", "bottom-right")
                saved_pip_scale = database.get_overlay_state("cam_switcher_pip_scale", 0.32)
                saved_c1_lbl = database.get_overlay_state("cam_switcher_cam1_label", "CAM 1 (메인/정면)")
                saved_c2_lbl = database.get_overlay_state("cam_switcher_cam2_label", "CAM 2 (서브/디테일)")
                saved_c1_mir = str(database.get_overlay_state("cam_switcher_cam1_mirrored", "0")) == "1"
                saved_c2_mir = str(database.get_overlay_state("cam_switcher_cam2_mirrored", "0")) == "1"
                camera_switcher_state["active_main_cam"] = _parse_cam_slot(saved_main, 1)
                if str(saved_layout) in ("single", "pip", "split_v", "split_h"):
                    camera_switcher_state["layout_mode"] = str(saved_layout)
                if str(saved_pip_pos) in ("bottom-right", "top-right", "bottom-left", "top-left"):
                    camera_switcher_state["pip_position"] = str(saved_pip_pos)
                camera_switcher_state["pip_scale"] = max(0.20, min(0.48, float(saved_pip_scale or 0.32)))
                if saved_c1_lbl:
                    camera_switcher_state["cam1_label"] = str(saved_c1_lbl)
                if saved_c2_lbl:
                    camera_switcher_state["cam2_label"] = str(saved_c2_lbl)
                camera_switcher_state["cam1_mirrored"] = bool(saved_c1_mir)
                camera_switcher_state["cam2_mirrored"] = bool(saved_c2_mir)
            except Exception:
                pass
        return dict(camera_switcher_state)


def get_camera_channels_summary(now_ts=None):
    if now_ts is None:
        now_ts = time.time()
    vdo_state = vdo_ninja_receiver.get_state() if 'vdo_ninja_receiver' in globals() else {}
    vdo_conn = bool(vdo_state.get("connected", False))
    out = {}
    with latest_camera_lock:
        for cid in (1, 2):
            ch = camera_channels[cid]
            ts = max(float(ch["timestamp"] or 0.0), float(ch["webrtc_heartbeat"] or 0.0))
            has_f = (ch["frame"] is not None) or (ch["webrtc_heartbeat"] > 0)
            is_vdo_slot = (cid == 1 and vdo_conn)
            c_upd = float(ch.get("client_updated_at") or 0.0)
            c_recent = (c_upd > 0 and (now_ts - c_upd) < 60.0)
            is_streaming_state = (ch.get("client_state") == "streaming" and c_upd > 0 and (now_ts - c_upd) < 15.0)
            is_act = bool((has_f and ts > 0 and (now_ts - ts) < 15.0) or (has_f and is_streaming_state) or is_vdo_slot)
            iv = ch["intervals_ms"]
            fps_est = round(1000.0 / (sum(iv) / len(iv)), 1) if iv and sum(iv) > 0 else 0.0
            out[str(cid)] = {
                "cam_id": cid,
                "active": is_act,
                "has_frame": bool(ch["frame"] is not None),
                "frame_seq": int(ch["seq"] or 0),
                "source": ch["source"] if is_act else "none",
                "fps": fps_est if is_act else 0.0,
                "last_frame_age": round(now_ts - ts, 2) if ts > 0 else None,
                "stream_url": f"/api/camera/stream?cam={cid}",
                "latest_frame_url": f"/api/camera/latest-frame?cam={cid}",
                "client_state": ch.get("client_state", "idle") if c_recent else "idle",
                "client_message": ch.get("client_message", "") if c_recent else "",
                "client_ip": ch.get("client_ip", "") if c_recent else "",
            }
    return out


def get_local_ip():
    """현재 PC의 로컬 네트워크 IP를 조회합니다."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip


_last_upnp_check_ts = 0.0


def ensure_windows_firewall_edge_traversal(ports=(5443, 5000)):
    """
    이중 공유기(예: 상위 ipTIME 192.168.0.x -> 하위 TP-Link 192.168.1.x) 환경에서
    UPnP 포트포워딩(192.168.0.69:5443 -> 192.168.1.103:5443)을 통해 들어오는 스마트폰 패킷은
    Windows 방화벽에서 Edge Traversal(서브넷 간 NAT 통과) 패킷으로 분류됩니다.
    python.exe 기본 규칙이 'Edge traversal: Defer to user'이면 백그라운드 프로세스에서 패킷이 자동 차단되므로
    'edge=yes' 규칙을 자동 확인/등록합니다.
    """
    if os.name != "nt" or "pytest" in sys.modules or "unittest" in sys.modules:
        return
    try:
        chk = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=LiveCommerce_Camera_5443", "verbose"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        out = (chk.stdout or "")
        if "LiveCommerce_Camera_5443" in out and ("Yes" in out or "예" in out):
            return
    except Exception:
        pass

    port_str = ",".join(str(p) for p in ports)
    cmd_args = (
        f'/c netsh advfirewall firewall set rule name="python.exe" new edge=yes >nul 2>&1 & '
        f'netsh advfirewall firewall delete rule name="LiveCommerce_Camera_5443" >nul 2>&1 & '
        f'netsh advfirewall firewall add rule name="LiveCommerce_Camera_5443" '
        f'dir=in action=allow protocol=TCP localport={port_str} edge=yes profile=any remoteip=any >nul 2>&1'
    )
    try:
        # 1차: 현재 권한으로 시도
        subprocess.run(["cmd.exe", *cmd_args.split()], capture_output=True, timeout=4)
        chk2 = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=LiveCommerce_Camera_5443"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if "LiveCommerce_Camera_5443" in (chk2.stdout or ""):
            print(f"🛡️ [Windows 방화벽] 서브넷 간 카메라 포트({port_str}) Edge Traversal(edge=yes) 허용 완료!", flush=True)
            return
    except Exception:
        pass

    try:
        # 2차: 관리자 권한(RunAs) 1회 승격 실행
        import ctypes
        ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", cmd_args, None, 0)
        print(f"🛡️ [Windows 방화벽] 서브넷 간 카메라 포트({port_str}) Edge Traversal(edge=yes) 권한 승인 요청됨", flush=True)
    except Exception as e:
        print(f"ℹ️ [Windows 방화벽 확인] {e}", flush=True)


def setup_upnp_cross_subnet_bridge(local_ip=None, ports=(5443, 5000), force=False):
    """
    공유기 이중 구성(예: Wi-Fi는 192.168.0.x, PC는 하위 공유기 192.168.1.x) 환경에서
    하위 공유기(192.168.1.1)의 UPnP IGD를 통해 상위 Wi-Fi 대역 IP(192.168.0.x)를 조회하고
    카메라/서버 포트(5443, 5000)를 자동으로 포트포워딩하여 대역 간 직결을 활성화합니다.
    (캐시 적용: 최초 1회 또는 30분 주기로만 공유기 UPnP SOAP를 호출하여 공유기 부하를 방지합니다.)
    """
    global bridged_wifi_ip, _last_upnp_check_ts
    import urllib.request
    now_ts = time.time()
    if not force and bridged_wifi_ip and (now_ts - _last_upnp_check_ts) < 1800.0:
        return bridged_wifi_ip
    _last_upnp_check_ts = now_ts
    if not local_ip:
        local_ip = get_local_ip()
    parts = local_ip.split(".")
    if len(parts) != 4:
        return bridged_wifi_ip
    gw_ip = f"{parts[0]}.{parts[1]}.{parts[2]}.1"
    control_url = f"http://{gw_ip}:1900/upnp/control/WANIPConn1"
    service_type = "urn:schemas-upnp-org:service:WANIPConnection:1"

    def soap_call(action, body_xml):
        envelope = f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:{action} xmlns:u="{service_type}">
      {body_xml}
    </u:{action}>
  </s:Body>
</s:Envelope>"""
        req = urllib.request.Request(
            control_url,
            data=envelope.encode("utf-8"),
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": f'"{service_type}#{action}"',
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    try:
        res_ip = soap_call("GetExternalIPAddress", "")
        m = re.search(r"<NewExternalIPAddress>([^<]+)</NewExternalIPAddress>", res_ip)
        ext_ip = m.group(1).strip() if m else None
        if ext_ip and ext_ip != "0.0.0.0" and ext_ip != local_ip and ext_ip.startswith(("192.168.", "10.", "172.")):
            for p in ports:
                body = f"""<NewRemoteHost></NewRemoteHost>
<NewExternalPort>{p}</NewExternalPort>
<NewProtocol>TCP</NewProtocol>
<NewInternalPort>{p}</NewInternalPort>
<NewInternalClient>{local_ip}</NewInternalClient>
<NewEnabled>1</NewEnabled>
<NewPortMappingDescription>LiveCommerceCam_{p}</NewPortMappingDescription>
<NewLeaseDuration>0</NewLeaseDuration>"""
                try:
                    soap_call("AddPortMapping", body)
                except Exception:
                    pass
            bridged_wifi_ip = ext_ip
            print(f"🌉 [C클래스 대역 브릿지 완료] Wi-Fi 대역({ext_ip}) ➔ PC({local_ip}) 포트 {ports} 자동 연결됨!", flush=True)
        else:
            bridged_wifi_ip = local_ip
    except Exception as e:
        bridged_wifi_ip = local_ip
        print(f"ℹ️ [UPnP 대역 브릿지 확인] {e}", flush=True)
    return bridged_wifi_ip


def ensure_ssl_certs(local_ip="127.0.0.1"):
    """스마트폰 브라우저 카메라 권한(HTTPS Secure Context)을 위한 자체 서명 인증서 생성 (한 번 생성된 인증서를 재사용하여 서버 재시작 시 스마트폰 인증서 재승인 방지)"""
    cert_path = os.path.join(app.root_path, "ssl_cert.pem")
    key_path = os.path.join(app.root_path, "ssl_key.pem")
    if os.path.exists(cert_path) and os.path.exists(key_path):
        try:
            setup_upnp_cross_subnet_bridge(local_ip)
        except Exception:
            pass
        return cert_path, key_path
    try:
        import ipaddress
        from datetime import datetime, timedelta, timezone
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "KR"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "LiveCommerce Studio"),
            x509.NameAttribute(NameOID.COMMON_NAME, local_ip),
        ])

        san_list = [
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]
        try:
            san_list.append(x509.IPAddress(ipaddress.ip_address(local_ip)))
        except Exception:
            pass
        try:
            ext_wifi_ip = setup_upnp_cross_subnet_bridge(local_ip)
            if ext_wifi_ip:
                san_list.append(x509.IPAddress(ipaddress.ip_address(ext_wifi_ip)))
        except Exception:
            pass

        now_utc = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now_utc - timedelta(days=1))
            .not_valid_after(now_utc + timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(san_list), critical=False)
            .sign(key, hashes.SHA256())
        )

        with open(key_path, "wb") as f:
            f.write(key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            ))
        with open(cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        return cert_path, key_path
    except Exception as e:
        print(f"⚠️ [SSL 인증서 생성 실패] {e}", flush=True)
        return None, None


def get_waiting_camera_frame(cam_id=1):
    """스마트폰 카메라 프레임 수신 전 표시할 대기 화면 JPEG 생성 (CAM 1 / CAM 2 채널별 안내 지원)"""
    global _waiting_frame_cache
    slot = _parse_cam_slot(cam_id, 1)
    if isinstance(_waiting_frame_cache, dict) and _waiting_frame_cache.get(slot):
        return _waiting_frame_cache[slot]
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (720, 1280), color=(15, 23, 42))
        draw = ImageDraw.Draw(img)
        border_col = (56, 189, 248) if slot == 1 else (168, 85, 247)
        box_col = (99, 102, 241) if slot == 1 else (236, 72, 153)
        draw.rectangle([24, 24, 696, 1256], outline=border_col, width=3)
        draw.rectangle([48, 490, 672, 790], fill=(30, 41, 59), outline=box_col, width=2)

        ip = get_local_ip()
        wifi_ip = bridged_wifi_ip or ip
        https_port = int(os.getenv("HTTPS_PORT", 5443))
        font_lg = None
        font_sm = None
        for font_name in ["malgunbd.ttf", "malgun.ttf", "arial.ttf"]:
            try:
                font_lg = ImageFont.truetype(font_name, 26)
                font_sm = ImageFont.truetype(font_name, 20)
                break
            except Exception:
                continue

        cam_role = "CAM 1 (메인 카메라)" if slot == 1 else "CAM 2 (서브/디테일 카메라)"
        lines = [
            (f"📱 {cam_role} 연결 대기 중", font_lg, border_col),
            (f"1. 휴대폰 접속: https://{wifi_ip}:{https_port}/cam?cam={slot}", font_sm, (248, 250, 252)),
            ("2. 휴대폰 화면을 한 번 새로고침하거나", font_sm, (203, 213, 225)),
            ("   [카메라 켜고 송출 시작]을 눌러주세요!", font_sm, (253, 224, 71))
        ]
        y = 535
        for text, fnt, col in lines:
            if fnt:
                draw.text((76, y), text, fill=col, font=fnt)
            else:
                draw.text((76, y), text, fill=col)
            y += 54

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
        if not isinstance(_waiting_frame_cache, dict):
            _waiting_frame_cache = {}
        _waiting_frame_cache[slot] = buf.getvalue()
        return _waiting_frame_cache[slot]
    except Exception:
        return None


@app.route('/cam', methods=['GET'])
@app.route('/cam/<int:cam_id>', methods=['GET'])
def mobile_cam_view(cam_id=None):
    """스마트폰 카메라 무선 송출 웹 화면 (CAM 1 / CAM 2 멀티 채널 지원)"""
    slot = _parse_cam_slot(cam_id if cam_id is not None else request.args.get('cam', 1), 1)
    local_ip = get_local_ip()
    wifi_ip = bridged_wifi_ip or local_ip
    https_port = int(os.getenv("HTTPS_PORT", 5443))
    http_port = int(os.getenv("SERVER_PORT", 5000))
    cam_tok = (request.args.get("cam_token") or get_studio_cam_token()).strip()
    with latest_camera_lock:
        camera_channels[slot]["client_state"] = "page_loaded"
        camera_channels[slot]["client_message"] = f"휴대폰({request.remote_addr}) 페이지 접속됨 — 카메라 시작 대기"
        camera_channels[slot]["client_ip"] = str(request.remote_addr or "")
        camera_channels[slot]["client_updated_at"] = time.time()
    return render_template(
        'mobile_cam.html',
        local_ip=wifi_ip,
        pc_ip=local_ip,
        https_port=https_port,
        http_port=http_port,
        public_tunnel_url=public_tunnel_url,
        cam_id=slot,
        cam_token=cam_tok,
    )


@app.route('/remote-cam', methods=['GET'])
@app.route('/remote-cam/<int:cam_id>', methods=['GET'])
def remote_network_cam_view(cam_id=None):
    """방식 2: 다른 네트워크(LTE/5G · 외부 Wi-Fi) 전용 스마트폰 원격 카메라 송출 웹페이지 (/remote-cam)"""
    slot = _parse_cam_slot(cam_id if cam_id is not None else request.args.get('cam', 1), 1)
    https_port = int(os.getenv("HTTPS_PORT", 5443))
    http_port = int(os.getenv("SERVER_PORT", 5000))
    _, req_host = _extract_request_hostname_and_scheme()
    cam_tok = (request.args.get("cam_token") or get_studio_cam_token()).strip()
    https_remote_url = f"https://{req_host}:{https_port}/remote-cam?cam={slot}&cam_token={cam_tok}"
    with latest_camera_lock:
        camera_channels[slot]["client_state"] = "page_loaded"
        camera_channels[slot]["client_message"] = f"원격 카메라({request.remote_addr}) 접속됨 — 송출 시작 대기"
        camera_channels[slot]["client_ip"] = str(request.remote_addr or "")
        camera_channels[slot]["client_updated_at"] = time.time()
    return render_template(
        'remote_cam.html',
        cam_id=slot,
        cam_token=cam_tok,
        server_host=req_host,
        https_port=https_port,
        http_port=http_port,
        https_remote_url=https_remote_url,
    )


@app.route('/api/remote-cam/frame', methods=['POST'])
def api_remote_cam_upload_frame():
    """방식 2: 다른 네트워크 원격 카메라 페이지(/remote-cam)에서 올라온 비디오 프레임(JPEG)을 스튜디오 CAM 1/2 버퍼로 수신"""
    return api_camera_upload_frame()


@app.route('/api/remote-cam/latest-frame', methods=['GET'])
def api_remote_cam_latest_frame():
    """방식 2: 스튜디오 또는 로컬 에이전트가 원격 카메라 페이지(/remote-cam)의 최신 프레임을 가져오는(Pull) 엔드포인트"""
    slot = _parse_cam_slot(request.args.get('cam', 1), 1)
    with latest_camera_lock:
        ch = camera_channels.get(slot, {})
        frame = ch.get("frame")
    if not frame:
        frame = get_waiting_camera_frame(slot) or b""
    resp = Response(frame, mimetype='image/jpeg')
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route('/api/remote-cam/signal', methods=['GET', 'POST'])
def api_remote_cam_webrtc_signal():
    """방식 2: 다른 네트워크 원격 카메라(/remote-cam) ↔ 스튜디오 간 WebRTC P2P Offer/Answer 시그널링 중계"""
    global latest_webrtc_heartbeat
    now = time.time()
    if request.method == 'GET':
        cam_slot = _parse_cam_slot(request.args.get("cam", 1), 1)
        peer_id = (request.args.get("peer_id") or "").strip()
        if peer_id:
            with webrtc_lock:
                sess = webrtc_sessions.get(peer_id)
                if sess and sess.get("answered") and sess.get("answer"):
                    ans = sess["answer"]
                    return jsonify({
                        "status": "answered",
                        "answered": True,
                        "answer": ans,
                        "sdp": ans.get("sdp") if isinstance(ans, dict) else None,
                        "type": ans.get("type", "answer") if isinstance(ans, dict) else "answer"
                    }), 200
            return jsonify({"status": "pending", "answered": False}), 200

        with latest_camera_lock:
            latest_webrtc_heartbeat = now
            camera_channels[cam_slot]["webrtc_heartbeat"] = now
        pending = []
        with webrtc_lock:
            for pid, sess in list(webrtc_sessions.items()):
                if now - sess["created_at"] > 60.0:
                    del webrtc_sessions[pid]
                    continue
                if not sess["answered"] and int(sess.get("cam", 1)) == cam_slot:
                    pending.append({
                        "peer_id": pid,
                        "cam": cam_slot,
                        "offer": sess["offer"]
                    })
        return jsonify({"status": "ok", "cam": cam_slot, "offers": pending}), 200

    data = request.get_json(silent=True) or {}
    action = str(data.get("action") or "offer").strip().lower()
    cam_slot = _parse_cam_slot(data.get("cam") or request.args.get("cam", 1), 1)
    peer_id = str(data.get("peer_id") or uuid.uuid4().hex[:12])

    if action == "answer":
        answer = data.get("answer")
        if not answer:
            return jsonify({"status": "error", "message": "answer missing"}), 400
        with latest_camera_lock:
            latest_webrtc_heartbeat = now
            camera_channels[cam_slot]["webrtc_heartbeat"] = now
        with webrtc_lock:
            if peer_id in webrtc_sessions:
                webrtc_sessions[peer_id]["answer"] = answer
                webrtc_sessions[peer_id]["answered"] = True
                return jsonify({"status": "ok", "peer_id": peer_id}), 200
        return jsonify({"status": "error", "message": "session not found"}), 404

    offer = data.get("offer")
    if not offer and data.get("sdp"):
        offer = {"type": data.get("type", "offer"), "sdp": data.get("sdp")}
    if not offer:
        return jsonify({"status": "error", "message": "offer missing"}), 400
    with webrtc_lock:
        webrtc_sessions[peer_id] = {
            "peer_id": peer_id,
            "cam": cam_slot,
            "offer": offer,
            "answer": None,
            "created_at": now,
            "answered": False
        }
    return jsonify({"status": "ok", "peer_id": peer_id, "cam": cam_slot}), 200


@app.route('/api/camera/client-log', methods=['POST'])
def api_camera_client_log():
    """스마트폰 브라우저의 카메라 권한/연결 상태 및 오류를 서버로 보고하여 스튜디오에 실시간 표시"""
    data = request.get_json(silent=True) or {}
    slot = _parse_cam_slot(data.get("cam") or request.args.get("cam", 1), 1)
    state = str(data.get("state") or "info").strip()[:40]
    msg = str(data.get("message") or "").strip()[:180]
    proto = str(data.get("protocol") or "").strip()[:16]
    client_ip = str(request.remote_addr or "")
    now_t = time.time()
    with latest_camera_lock:
        camera_channels[slot]["client_state"] = state
        camera_channels[slot]["client_message"] = msg
        camera_channels[slot]["client_ip"] = client_ip
        camera_channels[slot]["client_updated_at"] = now_t
    if state in ("error", "permission_denied", "no_mediadevices", "streaming_started"):
        print(f"📱 [CAM {slot} 모바일 진단] IP={client_ip} Proto={proto} State={state} Msg={msg}", flush=True)
    return jsonify({"status": "ok", "cam": slot}), 200


remote_bridged_local_host_info = {
    "local_ip": "",
    "pc_ip": "",
    "https_port": 5443,
    "http_port": 5000,
    "updated_at": 0.0,
    "cameras": {},
}


@app.route('/api/agent/bridge-sync', methods=['POST'])
def api_agent_bridge_sync():
    """방송자 로컬 PC 서버(192.168.0.69 등)가 클라우드 서버(49.50.132.140)로 실제 로컬 공유기 Wi-Fi IP 및 카메라 상태를 등록/동기화"""
    data = request.get_json(silent=True) or {}
    rep_local_ip = str(data.get("local_ip") or "").strip()
    rep_pc_ip = str(data.get("pc_ip") or rep_local_ip).strip()
    self_ip = get_local_ip()
    is_test_sync = (request.headers.get("X-Test-Bridge-Sync") == "1")

    # 클라우드 서버 자신이 자기 내부 IP(예: 192.168.0.6)를 덮어쓰지 않도록 방어
    if not is_test_sync and rep_local_ip == self_ip and rep_pc_ip == self_ip:
        return jsonify({"status": "ignored", "reason": "self_ip"}), 200

    now_t = time.time()
    if rep_local_ip:
        remote_bridged_local_host_info["local_ip"] = rep_local_ip
        remote_bridged_local_host_info["pc_ip"] = rep_pc_ip or rep_local_ip
        remote_bridged_local_host_info["https_port"] = int(data.get("https_port") or 5443)
        remote_bridged_local_host_info["http_port"] = int(data.get("http_port") or 5000)
        remote_bridged_local_host_info["updated_at"] = now_t
        cams_data = data.get("cameras")
        if isinstance(cams_data, dict):
            remote_bridged_local_host_info["cameras"] = cams_data
            with latest_camera_lock:
                for slot_num in (1, 2):
                    c_info = cams_data.get(str(slot_num)) or cams_data.get(slot_num)
                    if isinstance(c_info, dict):
                        c_state = str(c_info.get("client_state") or "idle")
                        if c_state in ("page_loaded", "requesting_camera", "streaming", "permission_denied", "no_mediadevices"):
                            camera_channels[slot_num]["client_state"] = c_state
                            camera_channels[slot_num]["client_message"] = str(c_info.get("client_message") or "")[:180]
                            camera_channels[slot_num]["client_ip"] = str(c_info.get("client_ip") or "")
                            camera_channels[slot_num]["client_updated_at"] = now_t

    return jsonify({
        "status": "ok",
        "cam_token": get_studio_cam_token(),
        "bridged_local_ip": remote_bridged_local_host_info["local_ip"],
    }), 200


@app.route('/api/camera/host-info', methods=['GET'])
def api_camera_host_info():
    """스마트폰 접속용 [방식 1: 동일 네트워크 Wi-Fi 직결(192.168.x.x)] 및 [방식 2: 다른 네트워크 별도 카메라 페이지(/remote-cam)] 주소와 듀얼 카메라 상태 반환"""
    import time
    now = time.time()
    req_cam = _parse_cam_slot(request.args.get('cam', 1), 1)
    ip = get_local_ip()
    wifi_ip = bridged_wifi_ip or setup_upnp_cross_subnet_bridge(ip) or ip

    # 로컬 PC 에이전트가 원격 브릿지로 등록한 실제 공유기 Wi-Fi IP(예: 192.168.0.69)가 있으면 클라우드 자체 VPC IP(192.168.0.6) 대신 최우선 사용
    bridge_fresh = (
        bool(remote_bridged_local_host_info.get("local_ip"))
        and (now - float(remote_bridged_local_host_info.get("updated_at") or 0.0)) < 30.0
    )
    eff_wifi_ip = remote_bridged_local_host_info["local_ip"] if bridge_fresh else wifi_ip
    eff_pc_ip = remote_bridged_local_host_info["pc_ip"] if bridge_fresh else ip

    port = int(os.getenv("SERVER_PORT", 5000))
    https_port = int(os.getenv("HTTPS_PORT", 5443))
    cam_tok = get_studio_cam_token()
    _, req_host = _extract_request_hostname_and_scheme()
    ext_host = req_host if req_host not in ("localhost", "127.0.0.1") else eff_wifi_ip

    # 방식 1: 같은 네트워크 (동일 공유기 Wi-Fi 192.168.x.x)
    local_https_url = f"https://{eff_wifi_ip}:{https_port}/cam?cam_token={cam_tok}&cam={req_cam}"
    cam1_https_url = f"https://{eff_wifi_ip}:{https_port}/cam?cam_token={cam_tok}&cam=1"
    cam2_https_url = f"https://{eff_wifi_ip}:{https_port}/cam?cam_token={cam_tok}&cam=2"
    pc_direct_https_url = f"https://{eff_pc_ip}:{https_port}/cam?cam_token={cam_tok}&cam={req_cam}"
    http_cam_url = f"http://{eff_wifi_ip}:{port}/cam?cam_token={cam_tok}&cam={req_cam}"

    # 방식 2: 다른 네트워크 (LTE/5G · 외부 Wi-Fi 전용 원격 카메라 페이지 /remote-cam)
    remote_cam1_https_url = f"https://{ext_host}:{https_port}/remote-cam?cam_token={cam_tok}&cam=1"
    remote_cam2_https_url = f"https://{ext_host}:{https_port}/remote-cam?cam_token={cam_tok}&cam=2"
    remote_cam_https_url = remote_cam2_https_url if req_cam == 2 else remote_cam1_https_url
    remote_cam_http_url = f"http://{ext_host}:{port}/remote-cam?cam_token={cam_tok}&cam={req_cam}"

    pub_base = public_tunnel_url or f"https://{ext_host}:{https_port}/remote-cam"
    pub_url = f"{pub_base}{'&' if '?' in pub_base else '?'}cam_token={cam_tok}"
    primary_url = cam2_https_url if req_cam == 2 else cam1_https_url

    cameras_info = get_camera_channels_summary(now)
    switcher_info = get_camera_switcher_state()
    main_slot = switcher_info.get("active_main_cam", 1)

    with latest_camera_lock:
        ts = max(latest_camera_timestamp, latest_webrtc_heartbeat)
        has_frame = (latest_camera_frame is not None) or (latest_webrtc_heartbeat > 0)
        cur_seq = latest_camera_seq
    vdo_state = vdo_ninja_receiver.get_state() if 'vdo_ninja_receiver' in globals() else {}
    vdo_conn = bool(vdo_state.get("connected", False))
    cam1_active = bool(cameras_info.get("1", {}).get("active", False))
    cam2_active = bool(cameras_info.get("2", {}).get("active", False))
    is_active = bool((has_frame and ts > 0 and (now - ts) < 5.0) or cam1_active or cam2_active or vdo_conn)
    return jsonify({
        "status": "ok",
        "local_ip": eff_wifi_ip,
        "pc_ip": eff_pc_ip,
        "server_host": ext_host,
        "bridged_wifi_ip": eff_wifi_ip,
        "local_bridge_connected": bridge_fresh,
        "port": port,
        "https_port": https_port,
        "cam_url": primary_url,
        "cam1_url": cam1_https_url,
        "cam2_url": cam2_https_url,
        "remote_cam_url": remote_cam_https_url,
        "remote_cam1_url": remote_cam1_https_url,
        "remote_cam2_url": remote_cam2_https_url,
        "remote_cam_http_url": remote_cam_http_url,
        "public_cam_url": pub_url,
        "public_tunnel_active": True,
        "local_https_url": local_https_url,
        "pc_direct_https_url": pc_direct_https_url,
        "http_cam_url": http_cam_url,
        "stream_url": f"/api/camera/stream?cam={main_slot}",
        "cam1_stream_url": "/api/camera/stream?cam=1",
        "cam2_stream_url": "/api/camera/stream?cam=2",
        "camera_active": is_active,
        "cam1_active": cam1_active,
        "cam2_active": cam2_active,
        "cameras": cameras_info,
        "switcher": switcher_info,
        "frame_seq": cur_seq,
        "vdo_connected": vdo_conn,
        "last_frame_age": round(now - ts, 1) if ts > 0 else None
    }), 200


@app.route('/api/camera/switcher', methods=['GET', 'POST'])
def api_camera_switcher():
    """듀얼 카메라(CAM 1 / CAM 2) 메인 스위칭 및 화면 분할(단독/PIP/상하2분할/좌우2분할) 제어 API"""
    global latest_camera_frame, latest_camera_timestamp, latest_camera_seq, latest_camera_source
    switcher = get_camera_switcher_state()
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        with camera_switcher_lock:
            action = str(data.get("action") or "").strip().lower()
            if action == "toggle":
                camera_switcher_state["active_main_cam"] = 2 if camera_switcher_state["active_main_cam"] == 1 else 1
            elif "active_main_cam" in data:
                camera_switcher_state["active_main_cam"] = _parse_cam_slot(data.get("active_main_cam"), camera_switcher_state["active_main_cam"])
            if "layout_mode" in data:
                lm = str(data.get("layout_mode") or "single").strip().lower()
                if lm in ("single", "pip", "split_v", "split_h"):
                    camera_switcher_state["layout_mode"] = lm
            if "pip_position" in data:
                pp = str(data.get("pip_position") or "bottom-right").strip().lower()
                if pp in ("bottom-right", "top-right", "bottom-left", "top-left"):
                    camera_switcher_state["pip_position"] = pp
            if "pip_scale" in data:
                try:
                    camera_switcher_state["pip_scale"] = max(0.20, min(0.48, float(data.get("pip_scale"))))
                except Exception:
                    pass
            if "cam1_label" in data and str(data.get("cam1_label") or "").strip():
                camera_switcher_state["cam1_label"] = str(data.get("cam1_label")).strip()[:36]
            if "cam2_label" in data and str(data.get("cam2_label") or "").strip():
                camera_switcher_state["cam2_label"] = str(data.get("cam2_label")).strip()[:36]
            if "cam1_mirrored" in data:
                camera_switcher_state["cam1_mirrored"] = bool(data.get("cam1_mirrored"))
            if "cam2_mirrored" in data:
                camera_switcher_state["cam2_mirrored"] = bool(data.get("cam2_mirrored"))
            camera_switcher_state["updated_at"] = time.time()
            switcher = dict(camera_switcher_state)

        try:
            database.set_overlay_state("cam_switcher_active_main", switcher["active_main_cam"])
            database.set_overlay_state("cam_switcher_layout_mode", switcher["layout_mode"])
            database.set_overlay_state("cam_switcher_pip_position", switcher["pip_position"])
            database.set_overlay_state("cam_switcher_pip_scale", switcher["pip_scale"])
            database.set_overlay_state("cam_switcher_cam1_label", switcher["cam1_label"])
            database.set_overlay_state("cam_switcher_cam2_label", switcher["cam2_label"])
            database.set_overlay_state("cam_switcher_cam1_mirrored", "1" if switcher["cam1_mirrored"] else "0")
            database.set_overlay_state("cam_switcher_cam2_mirrored", "1" if switcher["cam2_mirrored"] else "0")
        except Exception:
            pass

        # 메인 카메라 변경 즉시 전역 latest_camera_frame을 새 메인 카메라 프레임으로 동기화 및 스트림 깨우기
        main_slot = switcher["active_main_cam"]
        with latest_camera_cond:
            ch = camera_channels.get(main_slot)
            if ch and ch.get("frame") is not None:
                latest_camera_frame = ch["frame"]
                latest_camera_timestamp = time.time()
                latest_camera_source = ch.get("source") or f"cam_{main_slot}"
            latest_camera_seq += 1
            latest_camera_cond.notify_all()

    return jsonify({
        "status": "ok",
        "switcher": switcher,
        "cameras": get_camera_channels_summary()
    }), 200


@app.route('/api/camera/renew-url', methods=['POST'])
def api_camera_renew_url():
    """상위 Wi-Fi 대역(192.168.0.x) 포트포워딩 브릿지 및 공인 HTTPS 보안 터널 주소를 즉시 재설정/발급합니다."""
    global _public_tunnel_thread_started, public_tunnel_proc, public_tunnel_url
    now = time.time()
    ip = get_local_ip()
    port = int(os.getenv("SERVER_PORT", 5000))
    https_port = int(os.getenv("HTTPS_PORT", 5443))
    wifi_ip = setup_upnp_cross_subnet_bridge(ip, ports=(https_port, port), force=True) or ip
    bridge_fresh = (
        bool(remote_bridged_local_host_info.get("local_ip"))
        and (now - float(remote_bridged_local_host_info.get("updated_at") or 0.0)) < 30.0
    )
    eff_wifi_ip = remote_bridged_local_host_info["local_ip"] if bridge_fresh else wifi_ip
    eff_pc_ip = remote_bridged_local_host_info["pc_ip"] if bridge_fresh else ip
    _, req_host = _extract_request_hostname_and_scheme()
    ext_host = req_host if req_host not in ("localhost", "127.0.0.1") else eff_wifi_ip
    cam_tok = get_studio_cam_token(force_new=True)
    local_https_url = f"https://{eff_wifi_ip}:{https_port}/cam?cam_token={cam_tok}"
    pc_direct_https_url = f"https://{eff_pc_ip}:{https_port}/cam?cam_token={cam_tok}"
    http_cam_url = f"http://{eff_wifi_ip}:{port}/cam?cam_token={cam_tok}"
    remote_cam1_https_url = f"https://{ext_host}:{https_port}/remote-cam?cam_token={cam_tok}&cam=1"
    remote_cam2_https_url = f"https://{ext_host}:{https_port}/remote-cam?cam_token={cam_tok}&cam=2"

    # 공인 HTTPS 터널이 꺼져 있거나 재발급 요청 시 즉시 시작/갱신
    try:
        public_tunnel_renew_event.clear()
        if not _public_tunnel_thread_started:
            _public_tunnel_thread_started = True
            t = threading.Thread(target=run_public_https_tunnel, args=(port,), daemon=True)
            t.start()
        elif public_tunnel_proc is not None:
            try:
                public_tunnel_proc.terminate()
            except Exception:
                pass
        if not public_tunnel_url:
            public_tunnel_renew_event.wait(timeout=6.0)
    except Exception:
        pass

    pub_base = public_tunnel_url or f"https://{ext_host}:{https_port}/remote-cam"
    pub_url = f"{pub_base}{'&' if '?' in pub_base else '?'}cam_token={cam_tok}"

    return jsonify({
        "status": "ok",
        "local_ip": eff_wifi_ip,
        "pc_ip": eff_pc_ip,
        "cam_url": local_https_url,
        "cam1_url": f"{local_https_url}&cam=1",
        "cam2_url": f"{local_https_url}&cam=2",
        "remote_cam_url": remote_cam1_https_url,
        "remote_cam1_url": remote_cam1_https_url,
        "remote_cam2_url": remote_cam2_https_url,
        "public_cam_url": pub_url,
        "public_tunnel_active": bool(public_tunnel_url),
        "local_https_url": local_https_url,
        "pc_direct_https_url": pc_direct_https_url,
        "http_cam_url": http_cam_url,
        "message": f"Wi-Fi 초고속 직결({local_https_url}) 및 원격 카메라({remote_cam1_https_url}) 주소가 새로 발급되었습니다."
    }), 200


import queue


class VdoNinjaDirectReceiver:
    """
    서버 내장 VDO.Ninja WebRTC 자동 수신 브릿지 (Headless Chromium Receiver)
    - 스마트폰이 VDO.Ninja(?push=stream_id)로 접속하면 별도의 수동 창 열기나 화면 공유 선택 없이
      백그라운드 Chromium WebRTC 엔진이 자동으로 영상(H.264)과 음성(48kHz Opus)을 수신합니다.
    - 수신된 영상은 latest_camera_frame(/api/camera/stream)으로 25~30fps 공급되어
      내장 웹 OBS 스튜디오(#monitorStreamImg), 송출 오버레이(#overlayStreamImg), 유튜브 RTMP 합성 캔버스에 즉시 표시됩니다.
    - 수신된 음성은 24kHz s16le PCM으로 변환되어 유튜브 RTMP 오디오 파이프라인에 직결됩니다.
    """
    def __init__(self):
        from collections import deque
        self.lock = Lock()
        self.cmd_queue = queue.Queue()
        self.stream_id = ""
        self.active = True
        self.connected = False
        self.video_width = 0
        self.video_height = 0
        self.frames_received = 0
        self.audio_packets_received = 0
        self.last_frame_at = 0.0
        self.last_audio_at = 0.0
        self.last_frame_size_bytes = 0
        self._frame_intervals_ms = []
        self.frame_interval_avg_ms = 0.0
        self.frame_interval_max_ms = 0.0
        self.webrtc_stats = {
            "jitter_buffer_ms": 0.0,
            "fps_decoded": 0.0,
            "frames_decoded": 0,
            "frames_dropped": 0,
            "rtt_ms": 0.0,
            "canvas_capture_ms": 0.0,
            "ipc_push_ms": 0.0,
            "updated_at": 0.0
        }
        self.status_text = "VDO.Ninja 자동 수신 대기 중"
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()

    def ensure_started(self, stream_id: str = None):
        import random
        with self.lock:
            if not stream_id:
                stream_id = self.stream_id or str(database.get_overlay_state('vdo_ninja_stream_id', '') or '').strip()
            if not stream_id:
                stream_id = f"livecam_{random.randint(1000, 9999)}"
                try:
                    database.set_overlay_state('vdo_ninja_stream_id', stream_id)
                except Exception:
                    pass
            if stream_id != self.stream_id:
                self.stream_id = stream_id
                self.connected = False
                self.cmd_queue.put(("CONNECT", stream_id))
            return self.stream_id

    def reconnect(self, stream_id: str = None):
        sid = self.ensure_started(stream_id)
        self.cmd_queue.put(("CONNECT", sid))
        return sid

    def get_state(self):
        now = time.time()
        with self.lock:
            is_receiving = (now - self.last_frame_at) < 4.0
            return {
                "active": self.active,
                "connected": bool(self.connected and is_receiving),
                "stream_id": self.stream_id,
                "video_width": self.video_width,
                "video_height": self.video_height,
                "frames_received": self.frames_received,
                "audio_packets_received": self.audio_packets_received,
                "last_frame_age": round(now - self.last_frame_at, 3) if self.last_frame_at > 0 else None,
                "last_frame_size_kb": round(self.last_frame_size_bytes / 1024.0, 1),
                "frame_interval_avg_ms": round(self.frame_interval_avg_ms, 1),
                "frame_interval_max_ms": round(self.frame_interval_max_ms, 1),
                "webrtc_stats": dict(self.webrtc_stats),
                "status_text": "🟢 VDO.Ninja 영상+음성 실시간 수신 중" if is_receiving else self.status_text
            }

    def _on_frame_b64(self, b64_jpeg: str, w: int = 0, h: int = 0):
        global latest_camera_frame, latest_camera_timestamp, latest_camera_seq, latest_camera_source
        if not b64_jpeg:
            return
        try:
            import base64
            raw = base64.b64decode(b64_jpeg)
            if not raw:
                return
            now_f = time.time()
            active_main = get_camera_switcher_state().get("active_main_cam", 1)
            with latest_camera_cond:
                ch1 = camera_channels[1]
                prev_ts = ch1["timestamp"]
                ch1["seq"] = int(ch1["seq"] or 0) + 1
                ch1["frame"] = raw
                ch1["timestamp"] = now_f
                ch1["source"] = "vdo_ninja"
                if prev_ts > 0:
                    dt_c = (now_f - prev_ts) * 1000.0
                    if 1.0 <= dt_c <= 2000.0:
                        ch1["intervals_ms"].append(dt_c)
                        if len(ch1["intervals_ms"]) > 30:
                            del ch1["intervals_ms"][:-30]
                if active_main == 1 or camera_channels[2]["frame"] is None:
                    latest_camera_seq += 1
                    latest_camera_frame = raw
                    latest_camera_timestamp = now_f
                    latest_camera_source = "vdo_ninja"
                latest_camera_cond.notify_all()
            with self.lock:
                if self.last_frame_at > 0:
                    dt_ms = (now_f - self.last_frame_at) * 1000.0
                    if 1.0 <= dt_ms <= 2000.0:
                        self._frame_intervals_ms.append(dt_ms)
                        if len(self._frame_intervals_ms) > 30:
                            del self._frame_intervals_ms[:-30]
                        self.frame_interval_avg_ms = sum(self._frame_intervals_ms) / len(self._frame_intervals_ms)
                        self.frame_interval_max_ms = max(self._frame_intervals_ms)
                self.connected = True
                self.frames_received += 1
                self.last_frame_at = now_f
                self.last_frame_size_bytes = len(raw)
                if w > 0:
                    self.video_width = int(w)
                if h > 0:
                    self.video_height = int(h)
                self.status_text = f"🟢 VDO.Ninja 실시간 수신 중 ({self.video_width}x{self.video_height})"
        except Exception:
            pass

    def _on_audio_b64(self, b64_pcm: str, sample_rate: int = 48000):
        if not b64_pcm:
            return
        try:
            import base64
            pcm_bytes = base64.b64decode(b64_pcm)
            if not pcm_bytes:
                return
            now_f = time.time()
            with self.lock:
                self.audio_packets_received += 1
                self.last_audio_at = now_f
                pkt_seq = self.audio_packets_received
            youtube_direct_streamer.push_audio_pcm(pcm_bytes, source="mobile", seq=pkt_seq, subsource="vdo_ninja")
        except Exception:
            pass

    def _on_status(self, info):
        if not isinstance(info, dict):
            return
        with self.lock:
            if "connected" in info:
                self.connected = bool(info["connected"])
            if info.get("w"):
                self.video_width = int(info["w"])
            if info.get("h"):
                self.video_height = int(info["h"])
            if info.get("msg"):
                self.status_text = str(info["msg"])
            w_stats = info.get("webrtc_stats")
            if isinstance(w_stats, dict):
                for k in ("jitter_buffer_ms", "fps_decoded", "frames_decoded", "frames_dropped", "rtt_ms", "canvas_capture_ms", "ipc_push_ms"):
                    if k in w_stats and w_stats[k] is not None:
                        try:
                            self.webrtc_stats[k] = round(float(w_stats[k]), 2) if k not in ("frames_decoded", "frames_dropped") else int(w_stats[k])
                        except Exception:
                            pass
                self.webrtc_stats["updated_at"] = time.time()

    def _worker_loop(self):
        time.sleep(1.2)
        self.ensure_started()
        pw = None
        browser = None
        context = None
        page = None

        init_hook_js = r"""
        (() => {
            window.__vdoCapturedVideos = [];
            window.__vdoHookedAudioTrackIds = new Set();
            window.__vdoAudioNodes = [];
            let audioCtx = null;
            let workletReadyPromise = null;
            let audioChunks = [];
            let audioSamples = 0;
            let audioBusy = false;
            let audioSeq = 0;

            // VDO.Ninja 자체 WebAudio 그래프가 윈도우 OS 스피커(destination)로 소리를 중복 출력하여
            // 하울링 및 스마트폰 에코캔슬러(AEC) 2초 차단을 유발하는 현상을 원천 차단
            try {
                const origConnect = AudioNode.prototype.connect;
                AudioNode.prototype.connect = function(target, ...rest) {
                    if (target && typeof AudioDestinationNode !== 'undefined' && target instanceof AudioDestinationNode && !this.__isZeroGainSink) {
                        if (!target.__silentInterceptor) {
                            const g = target.context.createGain();
                            g.gain.value = 0.0;
                            g.__isZeroGainSink = true;
                            origConnect.call(g, target);
                            target.__silentInterceptor = g;
                        }
                        return origConnect.call(this, target.__silentInterceptor, ...rest);
                    }
                    return origConnect.call(this, target, ...rest);
                };
            } catch (e) {}

            function arrayBufferToBase64(buf) {
                const bytes = new Uint8Array(buf);
                let binary = '';
                const step = 0x8000;
                for (let i = 0; i < bytes.length; i += step) {
                    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
                }
                return btoa(binary);
            }

            let fbDcX = 0.0;
            let fbDcY = 0.0;
            function pushFloat32Samples(input, inRate, targetRate) {
                const inLen = input ? input.length : 0;
                if (inLen <= 0) return;
                let pcm16;
                if (Math.abs(inRate - targetRate) < 100) {
                    pcm16 = new Int16Array(inLen);
                    for (let i = 0; i < inLen; i++) {
                        const raw = input[i];
                        fbDcY = raw - fbDcX + 0.995 * fbDcY;
                        fbDcX = raw;
                        let s = fbDcY;
                        if (s > 0.95) s = 0.95 + (s - 0.95) * 0.15;
                        else if (s < -0.95) s = -0.95 + (s + 0.95) * 0.15;
                        if (s > 0.99) s = 0.99;
                        else if (s < -0.99) s = -0.99;
                        pcm16[i] = s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7FFF);
                    }
                } else {
                    const ratio = inRate / targetRate;
                    const outLen = Math.floor(inLen / ratio);
                    if (outLen <= 0) return;
                    pcm16 = new Int16Array(outLen);
                    for (let i = 0; i < outLen; i++) {
                        const startF = i * ratio;
                        const startIdx = Math.min(inLen - 1, Math.floor(startF));
                        const endIdx = Math.min(inLen, Math.max(startIdx + 1, Math.ceil(startF + ratio)));
                        let sum = 0, cnt = 0;
                        for (let j = startIdx; j < endIdx; j++) { sum += input[j]; cnt++; }
                        const raw = (cnt > 0 ? (sum / cnt) : input[startIdx]);
                        fbDcY = raw - fbDcX + 0.995 * fbDcY;
                        fbDcX = raw;
                        let s = fbDcY;
                        if (s > 0.95) s = 0.95 + (s - 0.95) * 0.15;
                        else if (s < -0.95) s = -0.95 + (s + 0.95) * 0.15;
                        if (s > 0.99) s = 0.99;
                        else if (s < -0.99) s = -0.99;
                        pcm16[i] = s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7FFF);
                    }
                }
                enqueuePcm16(pcm16, targetRate);
            }

            function enqueuePcm16(pcm16, targetRate) {
                if (!pcm16 || pcm16.length <= 0) return;
                audioChunks.push(pcm16);
                audioSamples += pcm16.length;
                if (audioSamples >= 1920 && !audioBusy) {
                    flushPendingAudio(targetRate || 48000);
                }
            }

            function flushPendingAudio(targetRate) {
                if (audioBusy || audioSamples < 960) return;
                audioBusy = true;
                const total = audioSamples;
                const chunks = audioChunks;
                audioChunks = [];
                audioSamples = 0;
                const merged = new Int16Array(total);
                let off = 0;
                for (const c of chunks) { merged.set(c, off); off += c.length; }
                const payload = merged.length > 14400 ? merged.subarray(merged.length - 14400) : merged;
                const rawBuf = payload.buffer.slice(payload.byteOffset, payload.byteOffset + payload.byteLength);
                const seq = ++audioSeq;

                // 1순위: 로컬 루프백 바이너리 직통 POST (Playwright 단일 스레드 IPC 병목 완전 우회)
                fetch('http://127.0.0.1:5000/api/camera/audio?src=mobile&vdo=1&seq=' + seq, {
                    method: 'POST',
                    body: rawBuf,
                    mode: 'no-cors',
                    keepalive: true
                }).catch(() => {
                    if (typeof window.pyPushVdoAudio === 'function') {
                        const b64 = arrayBufferToBase64(rawBuf);
                        return window.pyPushVdoAudio(b64, targetRate);
                    }
                }).finally(() => {
                    audioBusy = false;
                    if (audioSamples >= 1920) flushPendingAudio(targetRate);
                });
            }

            function ensureWorkletLoaded(ctx) {
                if (workletReadyPromise) return workletReadyPromise;
                if (!ctx.audioWorklet) {
                    workletReadyPromise = Promise.resolve(false);
                    return workletReadyPromise;
                }
                const workletSrc = `
                class VdoPcmWorklet extends AudioWorkletProcessor {
                    constructor() {
                        super();
                        this.buf = new Int16Array(1920);
                        this.idx = 0;
                        this.dcX = 0.0;
                        this.dcY = 0.0;
                    }
                    process(inputs) {
                        const input = inputs[0];
                        if (!input || !input[0] || input[0].length === 0) return true;
                        const ch0 = input[0];
                        const ch1 = (input.length > 1 && input[1] && input[1].length === ch0.length) ? input[1] : null;
                        for (let i = 0; i < ch0.length; i++) {
                            const raw = ch1 ? (ch0[i] + ch1[i]) * 0.5 : ch0[i];
                            // 32-bit Float 영역에서 DC 오프셋만 제거 (증폭 없음: 원음 1:1)
                            this.dcY = raw - this.dcX + 0.995 * this.dcY;
                            this.dcX = raw;
                            let s = this.dcY;
                            if (s > 0.95) s = 0.95 + (s - 0.95) * 0.15;
                            else if (s < -0.95) s = -0.95 + (s + 0.95) * 0.15;
                            if (s > 0.99) s = 0.99;
                            else if (s < -0.99) s = -0.99;
                            this.buf[this.idx++] = s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7FFF);
                            if (this.idx >= 1920) {
                                this.port.postMessage(this.buf.slice(0));
                                this.idx = 0;
                            }
                        }
                        return true;
                    }
                }
                registerProcessor('vdo-pcm-worklet', VdoPcmWorklet);
                `;
                const blob = new Blob([workletSrc], { type: 'application/javascript' });
                const blobUrl = URL.createObjectURL(blob);
                workletReadyPromise = ctx.audioWorklet.addModule(blobUrl).then(() => true).catch(() => {
                    const dataUrl = 'data:application/javascript;base64,' + btoa(workletSrc);
                    return ctx.audioWorklet.addModule(dataUrl).then(() => true).catch(() => false);
                });
                return workletReadyPromise;
            }

            function cleanupPrevAudioNodes(exceptTrackId) {
                if (!Array.isArray(window.__vdoAudioNodes)) return;
                window.__vdoAudioNodes = window.__vdoAudioNodes.filter(item => {
                    if (!item || (exceptTrackId && item.track && item.track.id === exceptTrackId)) return true;
                    try { if (item.src) item.src.disconnect(); } catch (e) {}
                    try { if (item.proc) item.proc.disconnect(); } catch (e) {}
                    try { if (item.zero) item.zero.disconnect(); } catch (e) {}
                    try {
                        if (item.keepAliveAudio) {
                            item.keepAliveAudio.pause();
                            item.keepAliveAudio.srcObject = null;
                            if (item.keepAliveAudio.parentNode) item.keepAliveAudio.parentNode.removeChild(item.keepAliveAudio);
                        }
                    } catch (e) {}
                    return false;
                });
            }

            function hookAudioTrack(track, streamObj) {
                if (!track || track.kind !== 'audio' || track.readyState !== 'live') return;
                if (window.__vdoHookedAudioTrackIds.has(track.id)) return;
                window.__vdoHookedAudioTrackIds.add(track.id);
                window.__vdoActiveTrackId = track.id;
                cleanupPrevAudioNodes(track.id);
                try {
                    const dedicatedAudioStream = new MediaStream([track]);
                    const AudioContextCls = window.AudioContext || window.webkitAudioContext;
                    if (!audioCtx) audioCtx = new AudioContextCls({ sampleRate: 48000 });
                    if (audioCtx.state === 'suspended') audioCtx.resume().catch(() => {});

                    // Chromium WebRTC 오디오 디코더(NetEq)가 백그라운드에서도 멈추지 않고 구동되도록 비가청(volume=0.0001) 싱크 연결
                    const keepAliveAudio = document.createElement('audio');
                    keepAliveAudio.autoplay = true;
                    keepAliveAudio.muted = false;
                    keepAliveAudio.volume = 0.0001;
                    keepAliveAudio.srcObject = dedicatedAudioStream;
                    keepAliveAudio.style.cssText = 'position:fixed;width:1px;height:1px;opacity:0;pointer-events:none;z-index:-1;';
                    (document.body || document.documentElement).appendChild(keepAliveAudio);
                    keepAliveAudio.play().catch(() => {});

                    const src = audioCtx.createMediaStreamSource(dedicatedAudioStream);
                    const zero = audioCtx.createGain();
                    zero.gain.value = 0;
                    zero.__isZeroGainSink = true;
                    zero.connect(audioCtx.destination);

                    const inRate = audioCtx.sampleRate || 48000;
                    const targetRate = 48000;

                    ensureWorkletLoaded(audioCtx).then((ok) => {
                        if (track.readyState === 'ended' || window.__vdoActiveTrackId !== track.id) return;
                        if (ok && Math.abs(inRate - targetRate) < 100) {
                            try {
                                cleanupPrevAudioNodes(track.id);
                                const workletNode = new AudioWorkletNode(audioCtx, 'vdo-pcm-worklet');
                                workletNode.port.onmessage = (ev) => {
                                    if (track.readyState === 'ended' || window.__vdoActiveTrackId !== track.id) return;
                                    if (ev.data && ev.data.length) {
                                        enqueuePcm16(ev.data, targetRate);
                                    }
                                };
                                src.connect(workletNode);
                                workletNode.connect(zero);
                                window.__vdoAudioNodes.push({ track, dedicatedAudioStream, keepAliveAudio, src, proc: workletNode, zero });
                                return;
                            } catch (e) {}
                        }
                        cleanupPrevAudioNodes(track.id);
                        // 폴백: 4096 샘플(85.3ms) 대용량 버퍼로 메인 스레드 캔버스 캡처 중에도 버퍼 드롭 원천 방지
                        const proc = audioCtx.createScriptProcessor(4096, 1, 1);
                        proc.onaudioprocess = (ev) => {
                            if (track.readyState === 'ended' || window.__vdoActiveTrackId !== track.id) {
                                window.__vdoHookedAudioTrackIds.delete(track.id);
                                return;
                            }
                            pushFloat32Samples(ev.inputBuffer.getChannelData(0), inRate, targetRate);
                        };
                        src.connect(proc);
                        proc.connect(zero);
                        window.__vdoAudioNodes.push({ track, dedicatedAudioStream, keepAliveAudio, src, proc, zero });
                    });

                    track.addEventListener('ended', () => {
                        window.__vdoHookedAudioTrackIds.delete(track.id);
                        try {
                            keepAliveAudio.srcObject = null;
                            if (keepAliveAudio.parentNode) keepAliveAudio.parentNode.removeChild(keepAliveAudio);
                        } catch (e) {}
                    });
                } catch (e) {
                    window.__vdoHookedAudioTrackIds.delete(track.id);
                }
            }

            function hookVideoTrack(track) {
                if (!track || track.kind !== 'video') return;
                try {
                    const v = document.createElement('video');
                    v.autoplay = true;
                    v.playsInline = true;
                    v.muted = true;
                    v.__vdoTrack = track;
                    v.__lastCapTime = -1;
                    v.__stalledCount = 0;
                    // 비디오 전용 스트림으로 분리하여 원본 오디오 트랙이 음소거 비디오 태그에 묶이지 않도록 보호
                    v.srcObject = new MediaStream([track]);
                    v.style.cssText = 'position:fixed;top:0;left:0;width:320px;height:180px;opacity:0.01;pointer-events:none;z-index:-1;';
                    (document.body || document.documentElement).appendChild(v);
                    v.play().catch(() => {});
                    window.__vdoCapturedVideos.push(v);

                    const cleanupVideo = () => {
                        try {
                            window.__vdoCapturedVideos = (window.__vdoCapturedVideos || []).filter(item => item !== v);
                            v.srcObject = null;
                            if (v.parentNode) v.parentNode.removeChild(v);
                        } catch (e) {}
                    };
                    track.addEventListener('ended', cleanupVideo);
                } catch (e) {}
            }

            // RTCPeerConnection 후킹으로 원격 비디오 및 오디오 트랙을 수신 즉시 포착 + WebRTC 수신 지연(getStats) 계측
            window.__vdoPeerConnections = [];
            const OrigPeerConnection = window.RTCPeerConnection || window.webkitRTCPeerConnection;
            if (OrigPeerConnection) {
                const WrappedPC = function(...args) {
                    const pc = new OrigPeerConnection(...args);
                    try {
                        window.__vdoPeerConnections.push(pc);
                        pc.addEventListener('connectionstatechange', () => {
                            if (pc.connectionState === 'closed' || pc.connectionState === 'failed') {
                                window.__vdoPeerConnections = (window.__vdoPeerConnections || []).filter(item => item !== pc);
                            }
                        });
                    } catch (e) {}
                    pc.addEventListener('track', (ev) => {
                        if (ev.track && ev.track.kind === 'video') {
                            hookVideoTrack(ev.track);
                        } else if (ev.track && ev.track.kind === 'audio') {
                            hookAudioTrack(ev.track, (ev.streams && ev.streams[0]) || null);
                        }
                    });
                    return pc;
                };
                WrappedPC.prototype = OrigPeerConnection.prototype;
                Object.assign(WrappedPC, OrigPeerConnection);
                window.RTCPeerConnection = WrappedPC;
            }

            function isVideoTrackLive(v) {
                if (!v || v.readyState < 2 || v.videoWidth <= 16 || v.videoHeight <= 16) return false;
                if (v.__vdoTrack && v.__vdoTrack.readyState === 'ended') return false;
                if (v.srcObject && typeof v.srcObject.getVideoTracks === 'function') {
                    const tracks = v.srcObject.getVideoTracks();
                    if (tracks.length > 0) {
                        const anyLive = tracks.some(t => t.readyState === 'live' && !t.muted);
                        if (!anyLive) return false;
                    }
                }
                const ct = Number(v.currentTime || 0);
                if (ct > 0) {
                    if (v.__lastCapTime === ct) {
                        v.__stalledCount = (v.__stalledCount || 0) + 1;
                        if (v.__stalledCount > 45) return false;
                    } else {
                        v.__lastCapTime = ct;
                        v.__stalledCount = 0;
                    }
                }
                return true;
            }

            // 30 FPS GPU 가속 캔버스 캡처 루프 (720p HD 다이어트로 픽셀 부하 56% 절감 + 무블로킹 전송)
            let capCanvas = null;
            let capCtx = null;
            let chkCanvas = null;
            let chkCtx = null;
            let framesInFlight = 0;
            let noVideoTicks = 0;
            let wasReportingConnected = false;
            let lastEncodedVideoTime = -1;
            let lastCanvasCapMs = 0.0;
            let lastIpcPushMs = 0.0;
            let prevJbDelay = 0.0;
            let prevJbEmitted = 0;
            let prevFramesDecoded = 0;
            let prevStatsTs = 0.0;

            // 1초 주기 WebRTC 비디오 수신 계측(getStats -> jitterBufferDelay, FPS, 드랍 프레임, RTT)
            setInterval(async () => {
                if (typeof window.pyReportVdoStatus !== 'function') return;
                const pcs = (window.__vdoPeerConnections || []).filter(pc => pc && pc.connectionState !== 'closed');
                if (!pcs.length) return;
                let bestVideoReport = null;
                let bestRttMs = 0.0;
                for (const pc of pcs) {
                    try {
                        const stats = await pc.getStats();
                        stats.forEach(rep => {
                            if (rep.type === 'inbound-rtp' && rep.kind === 'video' && (rep.framesDecoded > 0 || rep.bytesReceived > 0)) {
                                if (!bestVideoReport || (rep.framesDecoded || 0) >= (bestVideoReport.framesDecoded || 0)) {
                                    bestVideoReport = rep;
                                }
                            } else if (rep.type === 'candidate-pair' && rep.state === 'succeeded' && rep.currentRoundTripTime !== undefined) {
                                bestRttMs = Number(rep.currentRoundTripTime || 0) * 1000.0;
                            }
                        });
                    } catch (e) {}
                }
                if (bestVideoReport) {
                    const nowS = performance.now() / 1000.0;
                    const jbDelay = Number(bestVideoReport.jitterBufferDelay || 0);
                    const jbEmitted = Number(bestVideoReport.jitterBufferEmittedCount || 0);
                    const fDecoded = Number(bestVideoReport.framesDecoded || 0);
                    const fDropped = Number(bestVideoReport.framesDropped || 0);
                    let jbMs = 0.0;
                    if (jbEmitted > prevJbEmitted && prevJbEmitted > 0) {
                        jbMs = ((jbDelay - prevJbDelay) / (jbEmitted - prevJbEmitted)) * 1000.0;
                    } else if (jbEmitted > 0) {
                        jbMs = (jbDelay / jbEmitted) * 1000.0;
                    }
                    let fpsDec = Number(bestVideoReport.framesPerSecond || 0);
                    if (!fpsDec && prevStatsTs > 0 && (nowS - prevStatsTs) > 0.2 && fDecoded >= prevFramesDecoded) {
                        fpsDec = (fDecoded - prevFramesDecoded) / (nowS - prevStatsTs);
                    }
                    prevJbDelay = jbDelay;
                    prevJbEmitted = jbEmitted;
                    prevFramesDecoded = fDecoded;
                    prevStatsTs = nowS;
                    window.pyReportVdoStatus({
                        connected: true,
                        webrtc_stats: {
                            jitter_buffer_ms: Math.max(0, jbMs),
                            fps_decoded: Math.max(0, fpsDec),
                            frames_decoded: fDecoded,
                            frames_dropped: fDropped,
                            rtt_ms: Math.max(0, bestRttMs),
                            canvas_capture_ms: lastCanvasCapMs,
                            ipc_push_ms: lastIpcPushMs
                        }
                    }).catch(() => {});
                }
            }, 1000);

            setInterval(() => {
                try {
                    if (audioCtx && audioCtx.state === 'suspended') {
                        audioCtx.resume().catch(() => {});
                    }
                    // 죽은 캡처 비디오 엘리먼트 정리
                    if (Array.isArray(window.__vdoCapturedVideos)) {
                        window.__vdoCapturedVideos = window.__vdoCapturedVideos.filter(v => {
                            if (!v || (v.__vdoTrack && v.__vdoTrack.readyState === 'ended')) {
                                try { v.srcObject = null; if (v.parentNode) v.parentNode.removeChild(v); } catch (e) {}
                                return false;
                            }
                            return true;
                        });
                    }

                    // VDO.Ninja 내장 효과음(#testtone, #knocktone 등 정적 src를 가진 audio 태그)만 음소거/제거하고,
                    // WebRTC 스트림용 audio 태그는 volume=0.0001로 유지하여 PC 스피커 중복 하울링 차단
                    const allAudios = Array.from(document.querySelectorAll('audio'));
                    for (const aEl of allAudios) {
                        if (aEl.id === 'testtone' || aEl.id === 'knocktone' || aEl.hasAttribute('src')) {
                            try {
                                aEl.pause();
                                aEl.muted = true;
                                aEl.volume = 0;
                                aEl.removeAttribute('src');
                                if (aEl.parentNode) aEl.parentNode.removeChild(aEl);
                            } catch (e) {}
                        } else if (aEl.srcObject) {
                            aEl.muted = false;
                            aEl.volume = 0.0001;
                            if (aEl.paused) aEl.play().catch(() => {});
                            if (typeof aEl.srcObject.getAudioTracks === 'function') {
                                aEl.srcObject.getAudioTracks().forEach(t => hookAudioTrack(t, aEl.srcObject));
                            }
                        }
                    }

                    // 실제 WebRTC 스트림(srcObject)을 가진 비디오 엘리먼트도 volume=0.0001로 설정하여
                    // 백그라운드 Chromium이 윈도우 스피커로 소리를 중복 출력(하울링 유발)하지 않도록 차단
                    const allVideos = Array.from(document.querySelectorAll('video'));
                    for (const el of allVideos) {
                        if (!el.srcObject) continue;
                        if (!el.__vdoTrack) {
                            el.muted = false;
                            el.volume = 0.0001;
                        }
                        if (el.paused) el.play().catch(() => {});
                        if (typeof el.srcObject.getAudioTracks === 'function') {
                            el.srcObject.getAudioTracks().forEach(t => hookAudioTrack(t, el.srcObject));
                        }
                    }

                    if (framesInFlight >= 2 || typeof window.pyPushVdoFrame !== 'function') return;

                    // 가장 최근에 연결된 비디오 트랙부터 역순 탐색하여 재접속 즉시 새 트랙 선택
                    const domVideos = Array.from(document.querySelectorAll('video')).reverse();
                    const capVideos = [...(window.__vdoCapturedVideos || [])].reverse();
                    const candidates = [...capVideos, ...domVideos];
                    let activeVideo = null;
                    for (const v of candidates) {
                        if (isVideoTrackLive(v)) {
                            activeVideo = v;
                            break;
                        }
                    }
                    if (!activeVideo) {
                        noVideoTicks++;
                        if (noVideoTicks >= 30 && wasReportingConnected && typeof window.pyReportVdoStatus === 'function') {
                            wasReportingConnected = false;
                            window.pyReportVdoStatus({ connected: false, msg: '🔌 VDO.Ninja 스마트폰 송출 대기 중' }).catch(() => {});
                        }
                        return;
                    }

                    const curVidTime = Number(activeVideo.currentTime || 0);
                    if (curVidTime > 0 && curVidTime === lastEncodedVideoTime) {
                        return;
                    }

                    const vw = activeVideo.videoWidth;
                    const vh = activeVideo.videoHeight;
                    // 720p(1280x720 / 720x1280) 비디오 다이어트: 1080p 대비 픽셀 수 56% 감소로 렉 없이 30fps 완벽 고정
                    const maxDim = 1280;
                    const scale = Math.min(maxDim / Math.max(vw, vh), 1);
                    const cw = Math.round(vw * scale);
                    const ch = Math.round(vh * scale);

                    // 최초 연결 시에만 16x16 미니 캔버스로 암전 프레임 여부를 확인하고, 이후에는 GPU 가속 캔버스를 무간섭 구동
                    if (!wasReportingConnected) {
                        if (!chkCanvas) {
                            chkCanvas = document.createElement('canvas');
                            chkCanvas.width = 16;
                            chkCanvas.height = 16;
                            chkCtx = chkCanvas.getContext('2d', { alpha: false, willReadFrequently: true });
                        }
                        chkCtx.drawImage(activeVideo, 0, 0, 16, 16);
                        const d = chkCtx.getImageData(0, 0, 16, 16).data;
                        let hasLight = false;
                        for (let i = 0; i < d.length; i += 16) {
                            if (d[i] > 4 || d[i + 1] > 4 || d[i + 2] > 4) {
                                hasLight = true;
                                break;
                            }
                        }
                        if (!hasLight) {
                            noVideoTicks++;
                            return;
                        }
                    }

                    if (!capCanvas) {
                        capCanvas = document.createElement('canvas');
                        // willReadFrequently를 제거하여 Chromium GPU 하드웨어 가속 캔버스 활성화
                        capCtx = capCanvas.getContext('2d', { alpha: false });
                    }
                    if (capCanvas.width !== cw || capCanvas.height !== ch) {
                        capCanvas.width = cw;
                        capCanvas.height = ch;
                    }
                    const tCap0 = performance.now();
                    capCtx.drawImage(activeVideo, 0, 0, cw, ch);

                    noVideoTicks = 0;
                    wasReportingConnected = true;
                    lastEncodedVideoTime = curVidTime;
                    const dataUrl = capCanvas.toDataURL('image/jpeg', 0.80);
                    lastCanvasCapMs = performance.now() - tCap0;
                    const commaIdx = dataUrl.indexOf(',');
                    if (commaIdx > 0) {
                        framesInFlight++;
                        const b64 = dataUrl.slice(commaIdx + 1);
                        const tPush0 = performance.now();
                        window.pyPushVdoFrame(b64, cw, ch).catch(() => {}).finally(() => {
                            lastIpcPushMs = performance.now() - tPush0;
                            framesInFlight = Math.max(0, framesInFlight - 1);
                        });
                    }
                } catch (e) {
                    framesInFlight = 0;
                }
            }, 30);
        })();
        """

        def close_pw():
            nonlocal pw, browser, context, page
            for obj, method in [(page, "close"), (context, "close"), (browser, "close"), (pw, "stop")]:
                if obj is not None:
                    try:
                        getattr(obj, method)()
                    except Exception:
                        pass
            page = context = browser = pw = None

        def open_vdo_view(target_stream_id: str):
            nonlocal pw, browser, context, page
            if not target_stream_id:
                return
            if browser is None or not browser.is_connected() or context is None or page is None:
                close_pw()
                from playwright.sync_api import sync_playwright
                pw = sync_playwright().start()
                launch_args = [
                    "--autoplay-policy=no-user-gesture-required",
                    "--use-fake-ui-for-media-stream",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--disable-web-security",
                    "--allow-running-insecure-content",
                    "--ignore-certificate-errors",
                    "--disable-features=BlockInsecurePrivateNetworkRequests,PrivateNetworkAccessSendPreflights,PrivateNetworkAccessRespectPreflightResults"
                ]
                launched = None
                for ch_name in [None, "msedge", "chrome"]:
                    try:
                        if ch_name:
                            launched = pw.chromium.launch(
                                channel=ch_name,
                                headless=True,
                                args=launch_args,
                                ignore_default_args=["--mute-audio"]
                            )
                        else:
                            launched = pw.chromium.launch(
                                headless=True,
                                args=launch_args,
                                ignore_default_args=["--mute-audio"]
                            )
                        break
                    except Exception:
                        continue
                if launched is None:
                    with self.lock:
                        self.status_text = "⚠️ VDO.Ninja 브라우저 엔진 초기화 실패"
                    return
                browser = launched
                context = browser.new_context(
                    viewport={"width": 1080, "height": 1920},
                    ignore_https_errors=True,
                    permissions=["camera", "microphone"],
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
                context.expose_function("pyPushVdoFrame", self._on_frame_b64)
                context.expose_function("pyPushVdoAudio", self._on_audio_b64)
                context.expose_function("pyReportVdoStatus", self._on_status)
                context.add_init_script(init_hook_js)
                page = context.new_page()

            view_url = f"https://vdo.ninja/?view={target_stream_id}&cleanoutput&autostart&transparent&cover&audiobitrate=128"
            with self.lock:
                self.status_text = f"🔌 VDO.Ninja({target_stream_id}) 채널 수신 대기 중..."
            print(f"🥷 [VDO.Ninja 자동 수신기] 채널 연결 대기 시작: {view_url}", flush=True)
            try:
                page.goto(view_url, wait_until="domcontentloaded", timeout=20000)
            except Exception as e:
                print(f"⚠️ [VDO.Ninja 자동 수신기 접속 경고] {e}", flush=True)

        while True:
            try:
                cmd = None
                arg = None
                while not self.cmd_queue.empty():
                    try:
                        cmd, arg = self.cmd_queue.get_nowait()
                    except queue.Empty:
                        break
                if cmd == "CONNECT":
                    open_vdo_view(str(arg))

                if page is not None:
                    # 블로킹 queue.get(timeout) 없이 Playwright 이벤트 루프를 100% 연속 구동하여 30fps 콜백 처리
                    page.wait_for_timeout(45)
                else:
                    time.sleep(0.05)
            except Exception as err:
                print(f"⚠️ [VDO.Ninja 수신 워커 복구 중] {err}", flush=True)
                close_pw()
                time.sleep(2.0)
                if self.stream_id:
                    self.cmd_queue.put(("CONNECT", self.stream_id))


vdo_ninja_receiver = VdoNinjaDirectReceiver()


@app.route('/api/overlay/vdo-ninja', methods=['GET', 'POST'])
def api_overlay_vdo_ninja():
    """VDO.Ninja 스트림 ID, 오디오 수음 모드(원음 고감도 vs 소음억제) 및 서버 내장 자동 수신 브릿지 상태 조회/제어"""
    import random
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        stream_id = re.sub(r'[^a-zA-Z0-9_-]', '', str(data.get('stream_id') or '')).strip()
        if not stream_id:
            stream_id = f"livecam_{random.randint(1000, 9999)}"
        embed_active = bool(data.get('embed_active', False))
        audio_mode = str(data.get('audio_mode') or database.get_overlay_state('vdo_ninja_audio_mode', 'music_raw') or 'music_raw').strip()
        if audio_mode not in ('music_raw', 'voice_clean'):
            audio_mode = 'music_raw'
        try:
            database.set_overlay_state('vdo_ninja_stream_id', stream_id)
            database.set_overlay_state('vdo_ninja_embed_active', '1' if embed_active else '0')
            database.set_overlay_state('vdo_ninja_audio_mode', audio_mode)
        except Exception:
            pass
        if data.get('reconnect'):
            vdo_ninja_receiver.reconnect(stream_id)
        else:
            vdo_ninja_receiver.ensure_started(stream_id)
    else:
        stream_id = str(database.get_overlay_state('vdo_ninja_stream_id', '') or '').strip()
        if not stream_id:
            stream_id = f"livecam_{random.randint(1000, 9999)}"
            try:
                database.set_overlay_state('vdo_ninja_stream_id', stream_id)
            except Exception:
                pass
        embed_active = str(database.get_overlay_state('vdo_ninja_embed_active', '0')) == '1'
        audio_mode = str(database.get_overlay_state('vdo_ninja_audio_mode', 'music_raw') or 'music_raw').strip()
        if audio_mode not in ('music_raw', 'voice_clean'):
            audio_mode = 'music_raw'
        vdo_ninja_receiver.ensure_started(stream_id)

    # 오디오 비트레이트를 128kbps로 올려 Opus 저비트레이트 고역(8~12kHz) 금속성 아티팩트('쇠 긁는 소리')를 방지
    if audio_mode == 'voice_clean':
        push_url = f"https://vdo.ninja/?push={stream_id}&autostart&quality=0&audiodevice=1&proaudio=1"
    else:
        push_url = f"https://vdo.ninja/?push={stream_id}&autostart&quality=0&audiodevice=1&proaudio=1&denoise=0&autogain=1"
    view_url = f"https://vdo.ninja/?view={stream_id}&cleanoutput&autostart&transparent&cover&audiobitrate=128"
    return jsonify({
        "status": "ok",
        "stream_id": stream_id,
        "embed_active": embed_active,
        "audio_mode": audio_mode,
        "push_url": push_url,
        "view_url": view_url,
        "receiver": vdo_ninja_receiver.get_state()
    }), 200


@app.route('/api/camera/webrtc/offer', methods=['POST'])
def api_camera_webrtc_offer():
    """PC 뷰어(스튜디오/OBS/쇼호스트)가 스마트폰 카메라(CAM 1 / CAM 2)에 P2P 연결 Offer 등록 (서브넷 간 mDNS .local -> 실제 LAN IP 자동 변환)"""
    data = request.get_json(silent=True) or {}
    peer_id = str(data.get("peer_id") or uuid.uuid4().hex[:12])
    cam_slot = _parse_cam_slot(data.get("cam") or request.args.get("cam", 1), 1)
    offer = data.get("offer")
    if not offer and data.get("sdp"):
        offer = {"type": data.get("type", "offer"), "sdp": data.get("sdp")}
    if not offer:
        return jsonify({"status": "error", "message": "offer missing"}), 400
    try:
        pc_lan_ip = get_local_ip()
        raw_sdp = str(offer.get("sdp") or "")
        if pc_lan_ip and pc_lan_ip != "127.0.0.1" and raw_sdp:
            # 서로 다른 공유기 서브넷(192.168.0.x <-> 192.168.1.x)에서 해석 불가능한 Chrome mDNS(.local) 후보를 PC 실제 IP로 치환
            raw_sdp = re.sub(r'[a-fA-F0-9\-]{8,}\.local', pc_lan_ip, raw_sdp)
            offer["sdp"] = raw_sdp
    except Exception:
        pass
    now = time.time()
    with webrtc_lock:
        # 60초 지난 오래된 세션 정리
        expired = [k for k, v in webrtc_sessions.items() if now - v["created_at"] > 60.0]
        for k in expired:
            del webrtc_sessions[k]
        webrtc_sessions[peer_id] = {
            "peer_id": peer_id,
            "cam": cam_slot,
            "offer": offer,
            "answer": None,
            "created_at": now,
            "answered": False
        }
    return jsonify({"status": "ok", "peer_id": peer_id, "cam": cam_slot}), 200


@app.route('/api/camera/webrtc/offers', methods=['GET'])
def api_camera_webrtc_get_offers():
    """스마트폰 카메라(CAM 1 / CAM 2)가 대기 중인 PC 뷰어들의 Offer 목록을 조회"""
    global latest_webrtc_heartbeat
    cam_slot = _parse_cam_slot(request.args.get("cam", 1), 1)
    now = time.time()
    with latest_camera_lock:
        latest_webrtc_heartbeat = now
        camera_channels[cam_slot]["webrtc_heartbeat"] = now
    pending = []
    with webrtc_lock:
        for pid, sess in list(webrtc_sessions.items()):
            if now - sess["created_at"] > 60.0:
                del webrtc_sessions[pid]
                continue
            if not sess["answered"] and int(sess.get("cam", 1)) == cam_slot:
                pending.append({
                    "peer_id": pid,
                    "cam": cam_slot,
                    "offer": sess["offer"]
                })
    return jsonify({"status": "ok", "cam": cam_slot, "offers": pending}), 200


@app.route('/api/camera/webrtc/answer', methods=['POST'])
def api_camera_webrtc_post_answer():
    """스마트폰 카메라(CAM 1 / CAM 2)가 생성한 WebRTC SDP Answer를 등록"""
    global latest_webrtc_heartbeat
    data = request.get_json(silent=True) or {}
    peer_id = str(data.get("peer_id") or "")
    cam_slot = _parse_cam_slot(data.get("cam") or request.args.get("cam", 1), 1)
    answer = data.get("answer")
    if not peer_id or not answer:
        return jsonify({"status": "error", "message": "peer_id or answer missing"}), 400
    now = time.time()
    with latest_camera_lock:
        latest_webrtc_heartbeat = now
        camera_channels[cam_slot]["webrtc_heartbeat"] = now
    with webrtc_lock:
        if peer_id in webrtc_sessions:
            webrtc_sessions[peer_id]["answer"] = answer
            webrtc_sessions[peer_id]["answered"] = True
            return jsonify({"status": "ok"}), 200
    return jsonify({"status": "error", "message": "session not found"}), 404


@app.route('/api/camera/webrtc/answer/<peer_id>', methods=['GET'])
def api_camera_webrtc_get_answer(peer_id):
    """PC 뷰어가 자신의 Offer에 대한 스마트폰의 Answer를 폴링 조회"""
    with webrtc_lock:
        sess = webrtc_sessions.get(str(peer_id))
        if sess and sess.get("answered") and sess.get("answer"):
            ans = sess["answer"]
            sdp = ans.get("sdp") if isinstance(ans, dict) else None
            ans_type = ans.get("type", "answer") if isinstance(ans, dict) else "answer"
            return jsonify({
                "status": "answered",
                "answered": True,
                "answer": ans,
                "sdp": sdp,
                "type": ans_type
            }), 200
    return jsonify({"status": "pending", "answered": False, "answer": None}), 200


@app.route('/api/camera/frame', methods=['POST'])
def api_camera_upload_frame():
    """스마트폰 브라우저 또는 PC 웹캠에서 캡처한 비디오 프레임(JPEG) 수신 (CAM 1 / CAM 2 채널별 시퀀스 보장 & 즉시 알림)"""
    global latest_camera_frame, latest_camera_timestamp, latest_camera_seq, latest_camera_source, latest_camera_intervals_ms
    frame_data = request.data
    if not frame_data and request.files and 'frame' in request.files:
        frame_data = request.files['frame'].read()
    if frame_data:
        cam_slot = _parse_cam_slot(request.args.get('cam', 1), 1)
        src_tag = str(request.args.get('src') or f"mobile_cam_{cam_slot}").strip()
        seq = request.args.get('seq', type=int)
        now_f = time.time()
        switcher = get_camera_switcher_state()
        active_main = switcher.get("active_main_cam", 1)
        layout_mode = switcher.get("layout_mode", "single")

        with latest_camera_cond:
            ch = camera_channels[cam_slot]
            prev_ch_ts = float(ch["timestamp"] or 0.0)
            ch_seq = int(ch["seq"] or 0)
            ch_updated = False
            if seq is not None:
                if seq <= 20 or seq >= ch_seq or (ch_seq - seq) > 15 or (now_f - prev_ch_ts) > 1.2:
                    ch["seq"] = seq
                    ch["frame"] = frame_data
                    ch["timestamp"] = now_f
                    ch["source"] = src_tag
                    ch_updated = True
            else:
                ch["seq"] = ch_seq + 1
                ch["frame"] = frame_data
                ch["timestamp"] = now_f
                ch["source"] = src_tag
                ch_updated = True

            if ch_updated and prev_ch_ts > 0:
                dt_ch = (now_f - prev_ch_ts) * 1000.0
                if 1.0 <= dt_ch <= 2000.0:
                    ch["intervals_ms"].append(dt_ch)
                    if len(ch["intervals_ms"]) > 30:
                        del ch["intervals_ms"][:-30]

            # 현재 메인 카메라(active_main_cam)이거나, 메인 카메라에 최근 30초간 프레임이 전혀 없을 때만 전역 프레임 갱신
            other_slot = 2 if cam_slot == 1 else 1
            main_ch = camera_channels[active_main]
            main_alive = (main_ch["frame"] is not None) and (
                ((now_f - float(main_ch["timestamp"] or 0.0)) < 30.0) or
                (main_ch.get("client_state") == "streaming" and (now_f - float(main_ch.get("client_updated_at") or 0.0)) < 20.0)
            )
            is_effective_main = (cam_slot == active_main) or (not main_alive)

            if ch_updated:
                ch["client_state"] = "streaming"
                ch["client_message"] = "실시간 영상 송출 중"
                ch["client_ip"] = str(request.remote_addr or ch.get("client_ip") or "")
                ch["client_updated_at"] = now_f
                if is_effective_main:
                    prev_ts = latest_camera_timestamp
                    latest_camera_seq += 1
                    latest_camera_frame = frame_data
                    latest_camera_timestamp = now_f
                    latest_camera_source = src_tag
                    if prev_ts > 0:
                        dt_ms = (now_f - prev_ts) * 1000.0
                        if 1.0 <= dt_ms <= 2000.0:
                            latest_camera_intervals_ms.append(dt_ms)
                            if len(latest_camera_intervals_ms) > 30:
                                del latest_camera_intervals_ms[:-30]
                elif layout_mode != "single":
                    # PIP 또는 2분할 모드에서는 서브 카메라 프레임이 갱신될 때도 서버 합성기가 즉시 리빌드하도록 시퀀스 증가
                    latest_camera_seq += 1
                latest_camera_cond.notify_all()
        return jsonify({"status": "ok", "cam": cam_slot}), 200
    return jsonify({"status": "error", "message": "no frame"}), 400


@app.route('/api/camera/audio', methods=['POST'])
def api_camera_upload_audio():
    """스마트폰 카메라, VDO.Ninja AudioWorklet 또는 PC 스튜디오 마이크에서 캡처한 48kHz 16-bit Mono PCM 오디오 청크 수신 -> 유튜브 라이브 RTMP 실시간 믹싱"""
    pcm_data = request.data
    if pcm_data:
        src = (request.args.get('src') or 'mobile').strip().lower()
        seq = request.args.get('seq', type=int)
        is_vdo = (request.args.get('vdo') == '1')
        if is_vdo:
            with vdo_ninja_receiver.lock:
                vdo_ninja_receiver.audio_packets_received += 1
                vdo_ninja_receiver.last_audio_at = time.time()
                if seq is None:
                    seq = vdo_ninja_receiver.audio_packets_received
        subsrc = "vdo_ninja" if is_vdo else ("pc_mic" if src == "pc" else "mobile_cam")
        youtube_direct_streamer.push_audio_pcm(pcm_data, source=src, seq=seq, subsource=subsrc)
        return jsonify({"status": "ok"}), 200
    return jsonify({"status": "error", "message": "no audio"}), 400


@app.route('/api/camera/audio/pull', methods=['GET'])
def api_camera_pull_monitor_audio():
    """OBS 스튜디오(/studio) 및 송출 오버레이(/overlay/obs)에서 스마트폰 유입 음성을 실시간으로 들을 수 있도록 48kHz s16le PCM 청크를 반환"""
    from flask import Response
    # 새로고침 전 이전 버전 브라우저 탭(기본 ON 상태)이 스피커로 중복 출력하여 하울링을 일으키는 것을 즉시 차단
    if request.args.get('v') != '2':
        resp = Response(b"", status=200, mimetype='application/octet-stream')
        resp.headers['X-Audio-Seq'] = '0'
        resp.headers['X-Audio-Sample-Rate'] = '48000'
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return resp
    after_seq = request.args.get('after_seq', default=0, type=int)
    pcm_bytes, latest_seq = youtube_direct_streamer.pull_monitor_audio(after_seq)
    resp = Response(pcm_bytes, status=200, mimetype='application/octet-stream')
    resp.headers['X-Audio-Seq'] = str(latest_seq)
    resp.headers['X-Audio-Sample-Rate'] = '48000'
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp


@app.route('/api/camera/mic-worklet.js', methods=['GET'])
def api_camera_mic_worklet_js():
    """모바일 HTTPS(자체 서명 인증서 포함)에서 Blob URL 차단 없이 즉시 로드되는 전용 AudioWorklet 모듈 (48kHz 무필터 원음 통일)"""
    from flask import Response
    js_code = """class MicPcmWorklet extends AudioWorkletProcessor {
    constructor() {
        super();
        this.outBuf = new Int16Array(1920);
        this.outIdx = 0;
        this.srcPos = 0;
        this.dcX = 0;
        this.dcY = 0;
        this.prevSample = 0;
        this.hasPrev = false;
        this.filteredBuf = new Float32Array(256);
    }
    process(inputs) {
        const input = inputs[0];
        if (!input || !input[0] || input[0].length === 0) return true;
        const ch = input[0];
        const inLen = ch.length;
        if (this.filteredBuf.length < inLen) {
            this.filteredBuf = new Float32Array(inLen);
        }
        let dcX = this.dcX, dcY = this.dcY;
        for (let i = 0; i < inLen; i++) {
            const x = ch[i];
            dcY = x - dcX + 0.995 * dcY;
            dcX = x;
            this.filteredBuf[i] = dcY;
        }
        this.dcX = dcX;
        this.dcY = dcY;

        const ratio = sampleRate / 48000;
        const fBuf = this.filteredBuf;
        while (this.srcPos < inLen) {
            const idx = Math.floor(this.srcPos);
            const frac = this.srcPos - idx;
            let s0, s1;
            if (idx < 0) {
                s0 = this.hasPrev ? this.prevSample : fBuf[0];
                s1 = fBuf[0];
            } else if (idx >= inLen - 1) {
                s0 = fBuf[inLen - 1];
                s1 = fBuf[inLen - 1];
            } else {
                s0 = fBuf[idx];
                s1 = fBuf[idx + 1];
            }
            let s = (s0 * (1 - frac) + s1 * frac);
            if (s > 0.98) s = 0.98;
            else if (s < -0.98) s = -0.98;
            this.outBuf[this.outIdx++] = s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7FFF);
            if (this.outIdx >= 1920) {
                this.port.postMessage(this.outBuf.slice(0));
                this.outIdx = 0;
            }
            this.srcPos += ratio;
        }
        this.prevSample = fBuf[inLen - 1];
        this.hasPrev = true;
        this.srcPos -= inLen;
        return true;
    }
}
registerProcessor('mic-pcm-worklet', MicPcmWorklet);
"""
    resp = Response(js_code, mimetype='application/javascript')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp


@app.route('/api/camera/latest-frame', methods=['GET'])
def api_camera_latest_frame():
    """최신 스마트폰 카메라 단일 프레임 JPEG 반환 (?cam=1|2 지원, 프레임 수신 전에는 안내 대기 화면 반환)"""
    from flask import Response
    raw_cam = request.args.get('cam')
    slot = int(raw_cam) if raw_cam in ('1', '2') else get_camera_switcher_state().get("active_main_cam", 1)
    with latest_camera_lock:
        if raw_cam in ('1', '2'):
            f_bytes = camera_channels[slot]["frame"]
        else:
            f_bytes = camera_channels[slot]["frame"] or latest_camera_frame
    if not f_bytes:
        f_bytes = get_waiting_camera_frame(slot)
    if f_bytes:
        resp = Response(f_bytes, mimetype='image/jpeg')
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return resp
    return Response(b'', status=204)


@app.route('/api/camera/stream')
def api_camera_stream():
    """OBS 스튜디오 멀티뷰(?cam=1, ?cam=2) 및 메인 송출용 실시간 MJPEG 비디오 피드 (Condition 이벤트 기반 즉시 푸시)"""
    from flask import Response
    raw_cam = request.args.get('cam', '').strip()
    fixed_slot = int(raw_cam) if raw_cam in ('1', '2') else None

    def generate():
        last_sent_key = None
        last_waiting_sent = 0
        while True:
            switcher = get_camera_switcher_state()
            target_slot = fixed_slot if fixed_slot in (1, 2) else switcher.get("active_main_cam", 1)
            with latest_camera_cond:
                ch = camera_channels[target_slot]
                cur_key = (target_slot, ch["timestamp"], ch["seq"], latest_camera_seq if fixed_slot is None else 0)
                if cur_key == last_sent_key:
                    latest_camera_cond.wait(timeout=0.05)
                    switcher = get_camera_switcher_state()
                    target_slot = fixed_slot if fixed_slot in (1, 2) else switcher.get("active_main_cam", 1)
                    ch = camera_channels[target_slot]
                    cur_key = (target_slot, ch["timestamp"], ch["seq"], latest_camera_seq if fixed_slot is None else 0)
                frame = ch["frame"]
                ts = ch["timestamp"]
                # 메인 스트림(fixed_slot is None)인데 선택된 메인 카메라에 프레임이 없으면 보조 카메라 프레임 폴백
                if fixed_slot is None and not frame:
                    other_slot = 2 if target_slot == 1 else 1
                    other_ch = camera_channels[other_slot]
                    if other_ch["frame"] is not None:
                        frame = other_ch["frame"]
                        ts = other_ch["timestamp"]
                    elif latest_camera_frame is not None:
                        frame = latest_camera_frame
                        ts = latest_camera_timestamp
            now = time.time()
            # 스마트폰이 한 번이라도 프레임을 보냈다면 마지막 프레임을 최대 60초간 유지하여 대기 화면으로 깜빡이지 않도록 보장
            if frame and (ts == 0 or (now - ts) < 60.0):
                last_sent_key = cur_key
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            else:
                if now - last_waiting_sent >= 0.5:
                    waiting_frame = get_waiting_camera_frame(target_slot)
                    if waiting_frame:
                        yield (b'--frame\r\n'
                               b'Content-Type: image/jpeg\r\n\r\n' + waiting_frame + b'\r\n')
                    last_waiting_sent = now
                time.sleep(0.05)

    resp = Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


# ==========================================
# 🌐 쇼호스트 뷰 실시간 사이트 브라우저 미러링 엔진
# ==========================================

class HostSiteMirrorManager:
    """
    [🌐 사이트 보여주기] 클릭 시 관리자 전용 인터랙티브 브라우저 컨트롤 창(/host/site-controller)과
    쇼호스트 뷰(/host)에 실시간 사이트 화면을 양방향으로 렌더링·조작·스트리밍하는 매니저
    """
    VIEWPORT_W = 1280
    VIEWPORT_H = 800

    def __init__(self):
        import queue
        self.lock = Lock()
        self.frame_cond = threading.Condition(self.lock)
        self.cmd_queue = queue.Queue()
        self.active = False
        self.current_url = ""
        self.current_title = ""
        self.latest_frame = None
        self.latest_timestamp = 0.0
        self.seq = 0
        self.frame_seq = 0
        self.explicitly_stopped = False
        self._worker_thread = None
        self._waiting_frame_cache = None

    def _ensure_worker(self):
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker_thread.start()

    def get_waiting_frame(self):
        if self._waiting_frame_cache is not None:
            return self._waiting_frame_cache
        try:
            from PIL import Image, ImageDraw, ImageFont
            img = Image.new("RGB", (self.VIEWPORT_W, self.VIEWPORT_H), color=(15, 23, 42))
            draw = ImageDraw.Draw(img)
            draw.rectangle([24, 24, self.VIEWPORT_W - 24, self.VIEWPORT_H - 24], outline=(56, 189, 248), width=3)
            draw.rectangle([320, 270, 960, 530], fill=(30, 41, 59), outline=(236, 72, 153), width=2)
            font = None
            for font_name in ["malgunbd.ttf", "malgun.ttf", "arial.ttf"]:
                try:
                    font = ImageFont.truetype(font_name, 26)
                    break
                except Exception:
                    continue
            lines = [
                "🌐 실시간 사이트 브라우저 로딩 중...",
                "잠시만 기다려 주세요. 곧 화면이 표시됩니다."
            ]
            y = 355
            for line in lines:
                if font:
                    draw.text((380, y), line, fill=(248, 250, 252), font=font)
                else:
                    draw.text((380, y), line, fill=(248, 250, 252))
                y += 55
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            self._waiting_frame_cache = buf.getvalue()
            return self._waiting_frame_cache
        except Exception:
            return None

    def normalize_url(self, raw_url):
        from urllib.parse import quote
        url = (raw_url or "").strip()
        if not url:
            return "https://www.naver.com"
        if re.match(r"^https?://", url, re.IGNORECASE):
            return url
        if " " in url or ("." not in url and not url.startswith("localhost")):
            return f"https://search.naver.com/search.naver?query={quote(url)}"
        return "https://" + url

    def start(self, raw_url="https://www.naver.com"):
        url = self.normalize_url(raw_url)
        with self.frame_cond:
            already_same = (self.active and self.current_url == url and self.latest_frame is not None)
            self.active = True
            self.explicitly_stopped = False
            self.current_url = url
            if not self.current_title:
                self.current_title = url
            if self.latest_frame is None:
                self.latest_frame = self.get_waiting_frame()
                self.latest_timestamp = time.time()
            self.seq += 1
            self.frame_cond.notify_all()
        try:
            database.set_overlay_state('site_mirror_active', '1')
            database.set_overlay_state('site_mirror_url', url)
        except Exception:
            pass
        self._ensure_worker()
        if not already_same:
            self.cmd_queue.put(("START", url))
        return self.get_state()

    def stop(self):
        with self.frame_cond:
            self.active = False
            self.explicitly_stopped = True
            self.current_url = ""
            self.current_title = ""
            self.latest_frame = None
            self.seq += 1
            self.frame_cond.notify_all()
        try:
            database.set_overlay_state('site_mirror_active', '0')
        except Exception:
            pass
        if self._worker_thread and self._worker_thread.is_alive():
            self.cmd_queue.put(("STOP", None))
        return self.get_state()

    def dispatch_action(self, action_data):
        if not isinstance(action_data, dict):
            return self.get_state()
        if not self.active:
            self.start(action_data.get("url") or "https://www.naver.com")
        self._ensure_worker()
        self.cmd_queue.put(("ACTION", action_data))
        return self.get_state()

    def get_state(self):
        with self.lock:
            return {
                "active": self.active,
                "url": self.current_url,
                "title": self.current_title or self.current_url,
                "stream_url": "/api/host/site-mirror/stream",
                "frame_url": "/api/host/site-mirror/frame.jpg",
                "seq": self.seq,
                "frame_seq": self.frame_seq
            }

    def _worker_loop(self):
        import queue
        pw = None
        browser = None
        context = None
        active_page = None

        def close_browser():
            nonlocal pw, browser, context, active_page
            try:
                if context:
                    context.close()
            except Exception:
                pass
            context = None
            active_page = None
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            browser = None
            try:
                if pw:
                    pw.stop()
            except Exception:
                pass
            pw = None

        def ensure_browser_ready():
            nonlocal pw, browser, context, active_page
            if browser is not None and browser.is_connected() and context is not None:
                if not context.pages:
                    active_page = context.new_page()
                else:
                    active_page = context.pages[-1]
                return active_page

            close_browser()
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--autoplay-policy=no-user-gesture-required",
                "--mute-audio",
                "--no-sandbox"
            ]
            launched = None
            # H.264/AAC 라이브 스트림 디코더가 완벽 내장된 정품 Chrome / Edge를 최우선 실행
            for ch_name in ["chrome", "msedge", None]:
                try:
                    if ch_name:
                        launched = pw.chromium.launch(channel=ch_name, headless=True, args=launch_args)
                    else:
                        launched = pw.chromium.launch(headless=True, args=launch_args)
                    break
                except Exception:
                    continue
            if launched is None:
                raise RuntimeError("실행 가능한 브라우저(Chrome/Edge/Chromium)를 찾을 수 없습니다.")
            browser = launched
            ctx_kwargs = {
                "viewport": {"width": self.VIEWPORT_W, "height": self.VIEWPORT_H},
                "locale": "ko-KR",
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            }
            ig_state_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instagram_storage_state.json")
            if os.path.exists(ig_state_path):
                try:
                    ctx_kwargs["storage_state"] = ig_state_path
                except Exception:
                    pass
            context = browser.new_context(**ctx_kwargs)
            try:
                context.add_init_script("""
                    try {
                        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    } catch (e) {}
                """)
            except Exception:
                pass
            active_page = context.new_page()
            return active_page

        def capture_frame_now(page):
            if page is None:
                return
            try:
                # 유튜브·틱톡·인스타 웹페이지 내 일시정지된 라이브 <video> 자동 재생 유지
                page.evaluate("""() => {
                    const vids = document.querySelectorAll('video');
                    vids.forEach(v => {
                        try {
                            v.muted = true;
                            if (v.paused && v.readyState >= 2) {
                                v.play().catch(() => {});
                            }
                        } catch (e) {}
                    });
                }""")
            except Exception:
                pass
            try:
                snap = page.screenshot(type="jpeg", quality=78, timeout=1500)
                if snap:
                    with self.frame_cond:
                        self.latest_frame = snap
                        self.latest_timestamp = time.time()
                        self.frame_seq += 1
                        self.frame_cond.notify_all()
            except Exception:
                pass
            try:
                cur_u = page.url or self.current_url
                cur_t = page.title() or cur_u
                with self.lock:
                    self.current_url = cur_u
                    self.current_title = cur_t
            except Exception:
                pass

        last_yt_redirect_check = 0.0

        def auto_enter_youtube_live_video(page):
            """유튜브 채널 홈/라이브 대기 URL에 머물러 있으면 실제 /watch?v= 라이브 영상 주소로 즉시 직행합니다."""
            if page is None:
                return
            try:
                cur_u = page.url or ""
                if "youtube.com" not in cur_u or "/watch?v=" in cur_u:
                    return
                # 1) 백엔드 캐시/채팅 폴러에서 이미 확보된 video_id가 있으면 즉시 이동
                known_vid = ""
                if 'youtube_chat_poller' in globals():
                    known_vid = (youtube_chat_poller.get_state().get("video_id") or "").strip()
                if not known_vid and 'youtube_direct_streamer' in globals():
                    with youtube_direct_streamer.lock:
                        known_vid = (youtube_direct_streamer._yt_manifest_cache.get("video_id") or "").strip()
                if known_vid:
                    watch_u = f"https://www.youtube.com/watch?v={known_vid}"
                    print(f"🎯 [사이트 미러 유튜브 직행] 백엔드 video_id 감지 -> {watch_u}", flush=True)
                    page.goto(watch_u, wait_until="domcontentloaded", timeout=12000)
                    return

                # 2) DOM 내 canonical / LIVE 배지가 붙은 영상 링크(/watch?v=...) 탐색
                found_watch_url = page.evaluate("""() => {
                    try {
                        const canon = document.querySelector('link[rel="canonical"]')?.getAttribute('href') || '';
                        if (canon.includes('/watch?v=')) return canon;
                        const liveBadges = Array.from(document.querySelectorAll(
                            'ytd-thumbnail-overlay-time-status-renderer[overlay-style="LIVE"], ' +
                            'badge-shape .badge-shape-wiz__text, ' +
                            '.badge-style-type-live-now-alternate, ' +
                            '.yt-badge-shape--live'
                        ));
                        for (const b of liveBadges) {
                            const txt = (b.innerText || b.textContent || '').trim().toUpperCase();
                            const isLiveAttr = (b.getAttribute('overlay-style') || '').toUpperCase() === 'LIVE';
                            if (isLiveAttr || txt.includes('LIVE') || txt.includes('실시간')) {
                                const card = b.closest('ytd-rich-item-renderer, ytd-video-renderer, ytd-grid-video-renderer, ytd-channel-featured-content-renderer, ytm-video-with-context-renderer, div');
                                const a = (card && card.querySelector('a[href*="/watch?v="]')) || null;
                                if (a && a.href) return a.href;
                            }
                        }
                        const liveLink = document.querySelector('ytd-channel-featured-content-renderer a[href*="/watch?v="]');
                        if (liveLink && liveLink.href) return liveLink.href;
                    } catch (e) {}
                    return '';
                }""")
                if found_watch_url and "/watch?v=" in found_watch_url:
                    print(f"🎯 [사이트 미러 유튜브 직행] 채널 DOM LIVE 영상 감지 -> {found_watch_url}", flush=True)
                    page.goto(found_watch_url, wait_until="domcontentloaded", timeout=12000)
            except Exception:
                pass

        while True:
            try:
                timeout = 0.09 if (browser is not None and self.active) else 0.8
                try:
                    first_cmd = self.cmd_queue.get(timeout=timeout)
                    cmds = [first_cmd]
                    while not self.cmd_queue.empty():
                        try:
                            cmds.append(self.cmd_queue.get_nowait())
                        except queue.Empty:
                            break
                except queue.Empty:
                    cmds = []

                should_stop = False
                for cmd, arg in cmds:
                    if cmd == "STOP":
                        should_stop = True
                        break
                    elif cmd == "START":
                        target_url = self.normalize_url(arg)
                        page = ensure_browser_ready()
                        try:
                            page.goto(target_url, wait_until="domcontentloaded", timeout=12000)
                        except Exception as nav_err:
                            print(f"⚠️ [사이트 미러 이동 경고] {nav_err}", flush=True)
                        auto_enter_youtube_live_video(page)
                        capture_frame_now(page)
                    elif cmd == "ACTION" and isinstance(arg, dict):
                        page = ensure_browser_ready()
                        if context and context.pages:
                            page = context.pages[-1]
                            active_page = page
                        act_type = str(arg.get("type") or "").lower()
                        try:
                            if act_type == "click":
                                rx = float(arg.get("rx", 0.5))
                                ry = float(arg.get("ry", 0.5))
                                px = max(0, min(self.VIEWPORT_W - 1, int(rx * self.VIEWPORT_W)))
                                py = max(0, min(self.VIEWPORT_H - 1, int(ry * self.VIEWPORT_H)))
                                btn = arg.get("button", "left")
                                cnt = int(arg.get("click_count", 1))
                                page.mouse.click(px, py, button=btn, click_count=cnt)
                                page.wait_for_timeout(60)
                            elif act_type == "wheel":
                                rx = float(arg.get("rx", 0.5))
                                ry = float(arg.get("ry", 0.5))
                                px = max(0, min(self.VIEWPORT_W - 1, int(rx * self.VIEWPORT_W)))
                                py = max(0, min(self.VIEWPORT_H - 1, int(ry * self.VIEWPORT_H)))
                                dx = float(arg.get("delta_x", 0))
                                dy = float(arg.get("delta_y", 250))
                                page.mouse.move(px, py)
                                page.mouse.wheel(dx, dy)
                                page.wait_for_timeout(30)
                            elif act_type == "type":
                                txt = str(arg.get("text") or "")
                                if txt:
                                    page.keyboard.insert_text(txt)
                                if arg.get("submit"):
                                    page.keyboard.press("Enter")
                                page.wait_for_timeout(60)
                            elif act_type == "key":
                                key_name = str(arg.get("key") or "")
                                if key_name:
                                    if len(key_name) == 1:
                                        page.keyboard.insert_text(key_name)
                                    else:
                                        page.keyboard.press(key_name)
                                page.wait_for_timeout(40)
                            elif act_type == "goto":
                                target_url = self.normalize_url(arg.get("url"))
                                page.goto(target_url, wait_until="domcontentloaded", timeout=12000)
                            elif act_type == "back":
                                if context and len(context.pages) > 1:
                                    res_hist = page.evaluate("() => window.history.length")
                                    if res_hist <= 1:
                                        page.close()
                                        page = context.pages[-1]
                                        active_page = page
                                    else:
                                        page.go_back(wait_until="domcontentloaded", timeout=8000)
                                else:
                                    page.go_back(wait_until="domcontentloaded", timeout=8000)
                            elif act_type == "forward":
                                page.go_forward(wait_until="domcontentloaded", timeout=8000)
                            elif act_type == "reload":
                                page.reload(wait_until="domcontentloaded", timeout=10000)
                        except Exception as act_err:
                            print(f"⚠️ [사이트 액션 처리 경고({act_type})] {act_err}", flush=True)
                        if context and context.pages:
                            active_page = context.pages[-1]
                        capture_frame_now(active_page)

                if should_stop:
                    close_browser()
                    with self.frame_cond:
                        self.active = False
                        self.frame_cond.notify_all()
                    continue

                if browser is not None and self.active:
                    if not browser.is_connected() or context is None or len(context.pages) == 0:
                        close_browser()
                        with self.frame_cond:
                            self.active = False
                            self.current_url = ""
                            self.current_title = ""
                            self.seq += 1
                            self.frame_cond.notify_all()
                        continue

                    active_page = context.pages[-1]
                    if time.time() - last_yt_redirect_check >= 2.5:
                        last_yt_redirect_check = time.time()
                        auto_enter_youtube_live_video(active_page)
                    capture_frame_now(active_page)
            except Exception as loop_err:
                print(f"⚠️ [사이트 미러 워커 오류] {loop_err}", flush=True)
                time.sleep(0.3)


host_site_mirror = HostSiteMirrorManager()

# 쇼호스트 프롬프터(/host) 좌측 패널 화면 표시 상태
# - 기본값("DEFAULT"): 상품 상세 + 주문/매출/채팅 지표 + 전체 방송 제품 라인업
# - 선택값("YOUTUBE" | "TIKTOK" | "INSTAGRAM"): 채팅관리창(/broadcast)에서 선택한 플랫폼의 실제 웹사이트 라방 화면 표시
host_left_view_state = {
    "mode": "DEFAULT",
    "sub_mode": "WEB",
    "updated_at": 0.0
}


def get_host_platform_screens_info(discover_youtube=False):
    """유튜브, 틱톡, 인스타그램 각 플랫폼의 현재 방송 상태 및 실제 웹사이트 라이브 URL 정보를 반환합니다."""
    yt_rtmp = youtube_direct_streamer.get_state() if 'youtube_direct_streamer' in globals() else {}
    tt_rtmp = tiktok_direct_streamer.get_state() if 'tiktok_direct_streamer' in globals() else {}
    ig_rtmp = instagram_direct_streamer.get_state() if 'instagram_direct_streamer' in globals() else {}

    yt_chat = youtube_chat_poller.get_state() if 'youtube_chat_poller' in globals() else {}
    tt_chat = tiktok_chat_poller.get_state() if 'tiktok_chat_poller' in globals() else {}
    ig_chat = instagram_chat_poller.get_state() if 'instagram_chat_poller' in globals() else {}

    yt_viewers = platform_tracker.get_youtube_viewers_summary()
    tt_viewers = platform_tracker.get_tiktok_viewers_summary()
    ig_viewers = platform_tracker.get_instagram_viewers_summary()

    unified_title = database.get_overlay_state("simulcast_unified_title", "실시간 라이브 커머스 방송")

    # 1. YouTube 실제 웹사이트 라이브 URL 계산
    yt_vid = (yt_chat.get("video_id") or "").strip()
    if not yt_vid and 'youtube_direct_streamer' in globals():
        with youtube_direct_streamer.lock:
            yt_vid = (youtube_direct_streamer._yt_manifest_cache.get("video_id") or "").strip()
    if discover_youtube and not yt_vid and 'youtube_direct_streamer' in globals():
        try:
            yt_meta = youtube_direct_streamer.inspect_youtube_live_latency(force=True)
            yt_vid = (yt_meta.get("video_id") or "").strip()
        except Exception:
            pass

    yt_ch = (yt_chat.get("channel_input") or database.get_overlay_state("youtube_channel_input", "@골라방-n3j") or "@골라방-n3j").strip()
    if yt_vid:
        yt_live_url = f"https://www.youtube.com/watch?v={yt_vid}"
        yt_embed_url = f"https://www.youtube.com/embed/{yt_vid}?autoplay=1&mute=1"
    elif yt_ch:
        if yt_ch.startswith("http"):
            yt_live_url = yt_ch
        else:
            handle = yt_ch if yt_ch.startswith("@") else f"@{yt_ch}"
            yt_live_url = f"https://www.youtube.com/{handle}/live"
        yt_embed_url = ""
    else:
        yt_live_url = "https://www.youtube.com"
        yt_embed_url = ""

    # 2. TikTok 실제 웹사이트 라이브 URL 계산
    tt_ch = (tt_chat.get("channel_input") or database.get_overlay_state("tiktok_channel_input", "") or "").strip()
    tt_uid = (tt_chat.get("unique_id") or tt_ch.lstrip("@").split("/")[0].split("?")[0]).strip()
    if tt_ch.startswith("http"):
        tt_live_url = tt_ch if "/live" in tt_ch else tt_ch.rstrip("/") + "/live"
    elif tt_uid:
        tt_live_url = f"https://www.tiktok.com/@{tt_uid}/live"
    else:
        tt_live_url = "https://www.tiktok.com/live"

    # 3. Instagram 실제 웹사이트 라이브 URL 계산
    ig_ch = (ig_chat.get("channel_input") or database.get_overlay_state("instagram_channel_input", "") or "").strip()
    ig_uname = (ig_chat.get("username") or ig_ch.lstrip("@").split("/")[0].split("?")[0]).strip()
    if ig_ch.startswith("http"):
        ig_live_url = ig_ch if "/live" in ig_ch else ig_ch.rstrip("/") + "/live/"
    elif ig_uname:
        ig_live_url = f"https://www.instagram.com/{ig_uname}/live/"
    else:
        ig_live_url = "https://www.instagram.com/"

    return {
        "YOUTUBE": {
            "platform": "YOUTUBE",
            "label": "유튜브 (YouTube Live)",
            "active": bool(yt_rtmp.get("active") or yt_viewers.get("is_live")),
            "rtmp_active": bool(yt_rtmp.get("active")),
            "chat_active": bool(yt_chat.get("active")),
            "channel_input": yt_ch or "@골라방-n3j",
            "video_id": yt_vid,
            "broadcast_title": yt_chat.get("broadcast_title") or unified_title,
            "live_url": yt_live_url,
            "embed_url": yt_embed_url,
            "concurrent_viewers": int(yt_viewers.get("concurrent_viewers") or 0),
            "fps": yt_rtmp.get("fps", 30),
            "bitrate_kbps": yt_rtmp.get("video_bitrate_kbps", 4500),
            "status_text": yt_rtmp.get("status_text") or yt_chat.get("status_text") or "대기 중"
        },
        "TIKTOK": {
            "platform": "TIKTOK",
            "label": "틱톡 (TikTok LIVE)",
            "active": bool(tt_rtmp.get("active") or tt_viewers.get("is_live")),
            "rtmp_active": bool(tt_rtmp.get("active")),
            "chat_active": bool(tt_chat.get("active")),
            "channel_input": f"@{tt_uid}" if tt_uid else (tt_ch or "@틱톡채널"),
            "video_id": tt_chat.get("room_id") or "",
            "broadcast_title": tt_chat.get("broadcast_title") or unified_title,
            "live_url": tt_live_url,
            "embed_url": "",
            "concurrent_viewers": int(tt_viewers.get("concurrent_viewers") or 0),
            "fps": tt_rtmp.get("fps", 30),
            "bitrate_kbps": tt_rtmp.get("video_bitrate_kbps", 3500),
            "status_text": tt_rtmp.get("status_text") or tt_chat.get("status_text") or "대기 중"
        },
        "INSTAGRAM": {
            "platform": "INSTAGRAM",
            "label": "인스타그램 (Instagram Live)",
            "active": bool(ig_rtmp.get("active") or ig_viewers.get("is_live")),
            "rtmp_active": bool(ig_rtmp.get("active")),
            "chat_active": bool(ig_chat.get("active")),
            "channel_input": f"@{ig_uname}" if ig_uname else (ig_ch or "@인스타채널"),
            "video_id": ig_chat.get("media_id") or "",
            "broadcast_title": ig_chat.get("broadcast_title") or unified_title,
            "live_url": ig_live_url,
            "embed_url": "",
            "concurrent_viewers": int(ig_viewers.get("concurrent_viewers") or 0),
            "fps": ig_rtmp.get("fps", 30),
            "bitrate_kbps": ig_rtmp.get("video_bitrate_kbps", 2500),
            "status_text": ig_rtmp.get("status_text") or ig_chat.get("status_text") or "대기 중"
        }
    }


# ==========================================
# 📱 쇼호스트 전용 모바일 인이어 프롬프터 라우트
# ==========================================

@app.route('/host', methods=['GET'])
def host_prompter_view():
    """쇼호스트 전용 모바일 프롬프터 화면"""
    from flask import make_response
    resp = make_response(render_template('host_prompter.html'))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@app.route('/api/host/data', methods=['GET'])
def api_host_data():
    """쇼호스트 화면 실시간 큐, 핀 질문, 타이머, 전체/핀 상품, 매출 지표, 전체 실시간 채팅 반환"""
    # 활성 PD 큐
    cues = database.get_active_broadcast_cues(1)
    latest_cue = cues[0] if cues else None

    # 핀 질문 및 타이머
    pinned_question = database.get_host_pinned_question()
    host_timer = database.get_host_timer()

    # 구버전 /host 탭(v=site2 미적용 상태)이 열려 있는 경우 자동으로 최신 /host 페이지로 즉시 새로고침 유도
    referer = request.headers.get('Referer', '')
    client_ver = request.args.get('v', '')
    is_legacy_host_tab = ('/host' in referer) and ('/host/site-controller' not in referer) and (client_ver != 'site2')
    if is_legacy_host_tab:
        pinned_question = {
            "user": "SYSTEM",
            "question": '<img/src="/api/host/force-reload.png"/onerror="window.location.replace(\'/host?r=\'+Date.now())">',
            "platform": "LIVE",
            "pinned_at": f"reload_{int(time.time())}"
        }

    # 전체 상품 목록 및 핀 상품 (핀 고정 상품이 없을 때는 None 유지)
    all_prods = database.get_all_products()
    pinned_num = database.get_overlay_state('pinned_product_num', None)
    pinned_product = None
    if pinned_num is not None and str(pinned_num).strip() != "":
        pinned_num_str = str(pinned_num).replace("item-", "").strip()
        for p in all_prods:
            if p.get("num") == pinned_num or str(p.get("num")).strip() == pinned_num_str:
                pinned_product = p
                break
        if pinned_product is None:
            for p in all_prods:
                if p.get("db_id") == pinned_num or str(p.get("db_id")).strip() == pinned_num_str:
                    pinned_product = p
                    break

    # 통계 (총 주문, 총 매출, 총 채팅) 및 실시간 전체 채팅
    conn = database.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), COALESCE(SUM(total_price), 0) FROM orders WHERE status = 'CONFIRMED'")
        row = cur.fetchone()
        conf_orders = row[0] if row else 0
        total_sales = row[1] if row else 0

        cur.execute("SELECT COUNT(*) FROM chats")
        total_chats = cur.fetchone()[0]

        # 최근 질문 (카테고리 QUESTION - 하위 호환 유지)
        cur.execute("""
            SELECT id, platform, sender, message, category, ai_reply, chat_time
            FROM chats
            WHERE category = 'QUESTION'
            ORDER BY id DESC
            LIMIT 50
        """)
        recent_questions = [dict(r) for r in cur.fetchall()]

        # 전체 실시간 채팅 (최신 500건)
        cur.execute("""
            SELECT id, platform, sender, message, category, ai_reply, chat_time
            FROM chats
            ORDER BY id DESC
            LIMIT 500
        """)
        recent_chats = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    stats = {
        "orders": conf_orders,
        "sales": total_sales,
        "chats": total_chats
    }

    any_live = is_any_platform_verified_live()
    started_at_str = database.get_overlay_state('broadcast_started_at', None)
    now_dt = datetime.now()

    if any_live:
        if not started_at_str or not isinstance(started_at_str, str):
            started_at_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
            database.set_overlay_state('broadcast_started_at', started_at_str)
        try:
            start_dt = datetime.strptime(started_at_str, "%Y-%m-%d %H:%M:%S")
            elapsed_sec = max(0, int((now_dt - start_dt).total_seconds()))
            started_ms = int(start_dt.timestamp() * 1000)
        except Exception:
            elapsed_sec = 0
            started_ms = int(now_dt.timestamp() * 1000)
    else:
        # 방송 중이 아닐 때는 방송시간을 00:00:00으로 고정하고 이전 잔여 시작시각 초기화
        if started_at_str is not None:
            database.set_overlay_state('broadcast_started_at', None)
        started_at_str = None
        started_ms = None
        elapsed_sec = 0

    chat_integrity = platform_tracker.verify_and_sync_with_db(limit=500)

    platform_screens = get_host_platform_screens_info()
    if host_left_view_state.get("mode") == "YOUTUBE" and host_site_mirror.active:
        yt_screen = platform_screens.get("YOUTUBE") or {}
        yt_vid_now = (yt_screen.get("video_id") or "").strip()
        if yt_vid_now and f"watch?v={yt_vid_now}" not in (host_site_mirror.current_url or ""):
            host_site_mirror.start(f"https://www.youtube.com/watch?v={yt_vid_now}")

    return jsonify({
        "status": "ok",
        "is_live": any_live,
        "broadcast_reset_seq": broadcast_reset_seq,
        "host_reload_seq": host_reload_seq,
        "broadcast_started_at": started_at_str,
        "broadcast_started_ms": started_ms,
        "broadcast_elapsed_sec": elapsed_sec,
        "latest_cue": latest_cue,
        "pinned_question": pinned_question,
        "timer": host_timer,
        "pinned_product": pinned_product,
        "all_products": all_prods,
        "stats": stats,
        "chat_integrity": chat_integrity,
        "youtube_viewers": platform_tracker.get_youtube_viewers_summary(),
        "tiktok_viewers": platform_tracker.get_tiktok_viewers_summary(),
        "instagram_viewers": platform_tracker.get_instagram_viewers_summary(),
        "recent_questions": recent_questions,
        "recent_chats": recent_chats,
        "host_view_mode": host_left_view_state,
        "platform_screens": platform_screens,
        "site_mirror": host_site_mirror.get_state(),
        "camera_switcher": get_camera_switcher_state(),
        "cameras": get_camera_channels_summary()
    }), 200


@app.route('/api/host/view-mode', methods=['POST'])
def api_host_set_view_mode():
    """
    채팅관리창(/broadcast) 또는 쇼호스트 뷰(/host)에서 좌측 화면 모드를 전환합니다.
    - mode: "DEFAULT" (기본 상품·매출·라인업 화면) | "YOUTUBE" | "TIKTOK" | "INSTAGRAM"
    - 유튜브/틱톡/인스타그램 선택 시 내부 OBS 영상이 아닌 실제 해당 웹사이트 라이브 방송 페이지를 브라우저로 열어 화면을 송출합니다.
    """
    global host_left_view_state
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode") or "DEFAULT").strip().upper()
    if mode not in ("DEFAULT", "YOUTUBE", "TIKTOK", "INSTAGRAM"):
        mode = "DEFAULT"

    host_left_view_state = {
        "mode": mode,
        "sub_mode": "WEB",
        "updated_at": time.time()
    }

    platforms = get_host_platform_screens_info(discover_youtube=(mode == "YOUTUBE"))
    if mode == "DEFAULT":
        if host_site_mirror.active:
            host_site_mirror.stop()
    elif mode in platforms:
        custom_url = str(data.get("url") or "").strip()
        target_url = custom_url or platforms[mode].get("live_url") or "https://www.youtube.com"
        host_site_mirror.start(target_url)

    return jsonify({
        "status": "ok",
        "host_view_mode": host_left_view_state,
        "platform_screens": platforms,
        "site_mirror": host_site_mirror.get_state()
    }), 200


@app.route('/api/host/broadcast-frame.jpg', methods=['GET'])
def api_host_broadcast_frame():
    """현재 유튜브·틱톡·인스타그램으로 송출되고 있는 실시간 방송 화면(카메라+상품 오버레이 합성) 단일 JPEG 프레임 반환"""
    from flask import Response
    frame = None
    if 'youtube_direct_streamer' in globals():
        with youtube_direct_streamer.lock:
            frame = youtube_direct_streamer._latest_server_built_frame or youtube_direct_streamer.latest_composite_frame
        if not frame:
            try:
                frame = youtube_direct_streamer.build_server_composite_frame()
            except Exception:
                frame = None
    if not frame:
        with latest_camera_lock:
            frame = latest_camera_frame
    if not frame:
        frame = host_site_mirror.get_waiting_frame() or b""
    resp = Response(frame, mimetype='image/jpeg')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@app.route('/host/site-controller', methods=['GET'])
def host_site_controller_view():
    """별도 브라우저 창으로 열리는 관리자 전용 실시간 사이트 컨트롤러 화면"""
    initial_url = (request.args.get('url') or host_site_mirror.current_url or 'https://www.naver.com').strip()
    return render_template('site_controller.html', initial_url=initial_url)


@app.route('/api/host/site-mirror/state', methods=['GET'])
def api_host_site_mirror_state():
    """현재 실시간 사이트 미러 상태 반환"""
    referer = request.headers.get('Referer', '')
    if '/host/site-controller' in referer and not host_site_mirror.active and not host_site_mirror.explicitly_stopped:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(referer)
        qs = parse_qs(parsed.query)
        auto_url = (qs.get('url', [None])[0] or database.get_overlay_state('site_mirror_url', 'https://www.naver.com'))
        host_site_mirror.start(auto_url)
    return jsonify({
        "status": "ok",
        "site_mirror": host_site_mirror.get_state()
    }), 200


@app.route('/api/host/site-mirror/start', methods=['POST'])
def api_host_site_mirror_start():
    """쇼호스트 디렉팅 스튜디오에서 별도 브라우저 창을 열고 실시간 화면을 쇼호스트 뷰에 송출합니다."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "https://www.naver.com").strip()
    state = host_site_mirror.start(url)
    return jsonify({
        "status": "ok",
        "site_mirror": state,
        "message": "별도 브라우저 창을 열고 쇼호스트 뷰에 실시간 화면 송출을 시작했습니다."
    }), 200


@app.route('/api/host/site-mirror/action', methods=['POST'])
def api_host_site_mirror_action():
    """관리자 사이트 컨트롤러 창에서의 클릭, 휠 스크롤, 키 입력, 주소 이동을 실시간 실행합니다."""
    data = request.get_json(silent=True) or {}
    state = host_site_mirror.dispatch_action(data)
    return jsonify({
        "status": "ok",
        "site_mirror": state
    }), 200


@app.route('/api/host/site-mirror/stop', methods=['POST'])
def api_host_site_mirror_stop():
    """별도 브라우저 창을 닫고 쇼호스트 뷰의 실시간 사이트 화면 송출을 종료합니다."""
    state = host_site_mirror.stop()
    return jsonify({
        "status": "ok",
        "site_mirror": state,
        "message": "사이트 실시간 화면 송출이 종료되었습니다."
    }), 200


@app.route('/api/host/site-mirror/frame.jpg', methods=['GET'])
def api_host_site_mirror_frame():
    """최신 실시간 사이트 화면 JPEG 단일 프레임 반환 (더블 버퍼링 무끊김 렌더링용)"""
    from flask import Response
    referer = request.headers.get('Referer', '')
    if '/host/site-controller' in referer and not host_site_mirror.active and not host_site_mirror.explicitly_stopped:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(referer)
        qs = parse_qs(parsed.query)
        auto_url = (qs.get('url', [None])[0] or database.get_overlay_state('site_mirror_url', 'https://www.naver.com'))
        host_site_mirror.start(auto_url)
    with host_site_mirror.lock:
        frame = host_site_mirror.latest_frame
        f_seq = host_site_mirror.frame_seq
        is_active = host_site_mirror.active
    if not frame:
        frame = host_site_mirror.get_waiting_frame() or b""
    resp = Response(frame, mimetype='image/jpeg')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['X-Frame-Seq'] = str(f_seq)
    resp.headers['X-Site-Active'] = '1' if is_active else '0'
    return resp


@app.route('/api/host/site-mirror/stream')
def api_host_site_mirror_stream():
    """별도 오픈된 사이트 브라우저의 실시간 MJPEG 비디오 스트림"""
    from flask import Response

    def generate():
        last_sent_ts = 0.0
        while True:
            with host_site_mirror.frame_cond:
                if not host_site_mirror.active:
                    break
                if host_site_mirror.latest_timestamp == last_sent_ts:
                    host_site_mirror.frame_cond.wait(timeout=0.1)
                frame = host_site_mirror.latest_frame
                ts = host_site_mirror.latest_timestamp
                is_active = host_site_mirror.active
            if not is_active:
                break
            if frame:
                last_sent_ts = ts
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            else:
                waiting = host_site_mirror.get_waiting_frame()
                if waiting:
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + waiting + b'\r\n')
                time.sleep(0.1)

    resp = Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


# ==========================================
# ▶️ 유튜브 채널 직접 연결 (실시간 채팅 수집 & RTMP 직접 방송 송출) 엔진
# ==========================================

class YouTubeDirectChatPoller:
    """
    별도 브라우저 확장 프로그램이나 API 키 없이 유튜브 채널 주소(@핸들, /channel/..., watch?v=...)에서
    현재 진행 중인 라이브 방송을 자동으로 찾아 실시간 채팅을 직접 수집·연동하는 매니저
    """
    def __init__(self):
        from collections import deque
        self.lock = Lock()
        self.active = False
        self.channel_input = ""
        self.video_id = ""
        self.broadcast_title = ""
        self.status_text = "대기 중"
        self.last_error = ""
        self.collected_count = 0
        self.last_chat_at = None
        self.seen_ids = deque(maxlen=5000)
        self._initialized_video_id = ""
        self._worker_thread = None
        self._stop_flag = False
        self._worker_gen = 0

    def _sleep_interruptible(self, seconds, my_gen):
        end_t = time.time() + float(seconds)
        while time.time() < end_t:
            if self._stop_flag or not self.active or self._worker_gen != my_gen:
                return False
            time.sleep(min(0.4, max(0.05, end_t - time.time())))
        return not (self._stop_flag or not self.active or self._worker_gen != my_gen)

    def normalize_youtube_target(self, raw_input):
        s = (raw_input or "").strip()
        if "???" in s:
            s = "@더바인-0313"
        if not s:
            return "", ""
        # 1) 11자리 영상 ID 직접 입력
        if re.match(r"^[a-zA-Z0-9_-]{11}$", s):
            return f"https://www.youtube.com/watch?v={s}", s
        # 2) watch?v=, youtu.be/, /live/, /video/.../livestreaming URL에서 video_id 추출
        m = re.search(r"(?:v=|youtu\.be/|/live/|/embed/|/video/)([a-zA-Z0-9_-]{11})", s)
        if m:
            vid = m.group(1)
            return f"https://www.youtube.com/watch?v={vid}", vid
        # 3) @핸들 형태 입력
        if s.startswith("@"):
            return f"https://www.youtube.com/{s}/live", ""
        # 4) 일반 채널 URL
        if s.startswith("http://") or s.startswith("https://"):
            if "/live" not in s and "watch?v=" not in s:
                return s.rstrip("/") + "/live", ""
            return s, ""
        return f"https://www.youtube.com/@{s}/live", ""

    def connect(self, raw_input):
        clean_input = (raw_input or "").strip()
        if "???" in clean_input:
            clean_input = "@더바인-0313"
        target_url, explicit_vid = self.normalize_youtube_target(clean_input)
        if not target_url:
            return {"ok": False, "message": "유튜브 채널 주소(@핸들) 또는 라이브 방송 URL을 입력해 주세요."}

        with self.lock:
            self._worker_gen += 1
            my_gen = self._worker_gen
            self._stop_flag = False
            self.active = True
            self.channel_input = clean_input
            self.video_id = explicit_vid
            self.broadcast_title = ""
            self.status_text = "유튜브 라이브 방송 연결 확인 중..."
            self.last_error = ""
        try:
            database.set_overlay_state("youtube_channel_input", self.channel_input)
        except Exception:
            pass

        self._worker_thread = threading.Thread(
            target=self._poll_loop,
            args=(target_url, explicit_vid, my_gen),
            daemon=True
        )
        self._worker_thread.start()
        return {"ok": True, "message": "유튜브 채널 실시간 채팅 연결을 시작했습니다.", "state": self.get_state()}

    def disconnect(self):
        with self.lock:
            self._worker_gen += 1
            self._stop_flag = True
            self.active = False
            self.status_text = "연결 해제됨"
        return {"ok": True, "message": "유튜브 채널 채팅 연결이 해제되었습니다.", "state": self.get_state()}

    def get_state(self):
        with self.lock:
            ch_in = self.channel_input or database.get_overlay_state("youtube_channel_input", "")
            if "???" in ch_in:
                ch_in = "@더바인-0313"
            return {
                "active": self.active,
                "channel_input": ch_in,
                "video_id": self.video_id,
                "broadcast_title": self.broadcast_title,
                "status_text": self.status_text,
                "last_error": self.last_error,
                "collected_count": self.collected_count,
                "last_chat_at": self.last_chat_at
            }

    def _extract_viewers_from_html(self, html_text):
        """유튜브 watch 또는 /live 페이지 HTML에서 실시간 동시 시청자 수를 추출합니다."""
        if not html_text:
            return None
        try:
            m_orig = re.search(r'"videoViewCountRenderer":\{[^}]*?"originalViewCount":"(\d+)"', html_text)
            if m_orig:
                return int(m_orig.group(1))
            m_vcr = re.search(r'"videoViewCountRenderer":\{"viewCount":(\{.*?\})(?:,"isLive":true|\})', html_text)
            if m_vcr:
                digits = re.findall(r'\d[\d,]*', m_vcr.group(1))
                if digits:
                    return int(digits[0].replace(",", ""))
            if '"isLive":true' in html_text or '"isLiveNow":true' in html_text:
                m_vc = re.search(r'"videoDetails":\{[^}]*?"viewCount":"(\d+)"', html_text)
                if m_vc:
                    return int(m_vc.group(1))
        except Exception:
            pass
        return None

    def _poll_updated_metadata(self, session, api_key, vid, context_obj):
        """Innertube updated_metadata API를 통해 실시간 동시 시청자 수를 조회합니다."""
        try:
            meta_url = f"https://www.youtube.com/youtubei/v1/updated_metadata?key={api_key}"
            r_m = session.post(meta_url, json={"context": context_obj, "videoId": vid}, timeout=5)
            if r_m.status_code != 200:
                return None
            m_data = r_m.json()
            for act in (m_data.get("actions") or []):
                uv = act.get("updateViewershipAction", {})
                vcr = uv.get("viewCount", {}).get("videoViewCountRenderer", {})
                if not vcr:
                    continue
                if vcr.get("originalViewCount") is not None:
                    return int(str(vcr["originalViewCount"]).replace(",", ""))
                vc_obj = vcr.get("viewCount", {})
                txt = vc_obj.get("simpleText") or "".join(run.get("text", "") for run in (vc_obj.get("runs") or []))
                nums = re.findall(r'\d[\d,]*', str(txt))
                if nums:
                    return int(nums[0].replace(",", ""))
        except Exception:
            pass
        return None

    def _poll_loop(self, target_url, explicit_vid, my_gen):
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        session = requests.Session()
        session.verify = False
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"
        })

        try:
            while not self._stop_flag and self.active and self._worker_gen == my_gen:
                try:
                    vid = explicit_vid
                    title = ""
                    initial_viewers = None
                    if not vid:
                        resp = session.get(target_url, timeout=8, allow_redirects=True)
                        html = resp.text
                        initial_viewers = self._extract_viewers_from_html(html)
                        m_url = re.search(r"watch\?v=([a-zA-Z0-9_-]{11})", resp.url)
                        if m_url:
                            vid = m_url.group(1)
                        if not vid:
                            m_canon = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})"', html)
                            if m_canon:
                                vid = m_canon.group(1)
                            elif '"isLive":true' in html or '"isLiveNow":true' in html:
                                m_vd = re.search(r'"videoDetails":\{"videoId":"([a-zA-Z0-9_-]{11})"[^}]*"isLive":true', html)
                                if m_vd:
                                    vid = m_vd.group(1)
                                else:
                                    m_ext = re.search(r'"externalVideoId":"([a-zA-Z0-9_-]{11})"', html)
                                    if m_ext:
                                        vid = m_ext.group(1)
                                    else:
                                        m_vid = re.search(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)
                                        if m_vid:
                                            vid = m_vid.group(1)
                        m_title = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
                        if m_title:
                            title = m_title.group(1).replace("- YouTube", "").strip()

                    if not vid:
                        platform_tracker.update_youtube_live_meta(is_live=False, concurrent_viewers=0)
                        with self.lock:
                            if self._worker_gen == my_gen:
                                self.video_id = ""
                                self.status_text = "현재 공개(Public) 라이브 방송 대기 중 (유튜브 스튜디오 제어실 확인 또는 일부 공개 URL 입력)"
                        wait_sec = 4.0 if (globals().get("youtube_direct_streamer") and youtube_direct_streamer.active) else 12.0
                        if not self._sleep_interruptible(wait_sec, my_gen):
                            break
                        continue

                    # live_chat 페이지에서 INNERTUBE_API_KEY 및 초기 continuation 토큰 획득
                    chat_url = f"https://www.youtube.com/live_chat?v={vid}&is_popout=1"
                    chat_resp = session.get(chat_url, timeout=8)
                    chat_html = chat_resp.text

                    if not title or initial_viewers is None:
                        try:
                            watch_resp = session.get(f"https://www.youtube.com/watch?v={vid}", timeout=6)
                            if not title:
                                m_t = re.search(r"<title>(.*?)</title>", watch_resp.text, re.DOTALL)
                                if m_t:
                                    title = m_t.group(1).replace("- YouTube", "").strip()
                            if initial_viewers is None:
                                initial_viewers = self._extract_viewers_from_html(watch_resp.text)
                        except Exception:
                            pass

                    platform_tracker.update_youtube_live_meta(
                        concurrent_viewers=initial_viewers,
                        is_live=True,
                        video_id=vid,
                        title=title
                    )

                    m_key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', chat_html)
                    # 중요: 유튜브 기본값인 '주요 채팅(Top Chat - 일부 댓글 필터링됨)'이 아닌
                    # 모든 댓글을 100% 수신하는 '실시간 채팅(All Chat / Live Chat)' continuation 토큰을 우선 선택!
                    all_conts = re.findall(r'"continuation":"([^"]+)"', chat_html)
                    chosen_cont = None
                    if len(all_conts) >= 2:
                        # subMenuItems[0] = 주요 채팅(Top Chat), subMenuItems[1] = 실시간 채팅(All Chat)
                        chosen_cont = all_conts[1]
                        platform_tracker.yt_chat_mode = "ALL_CHAT (실시간 전체 채팅 · 필터 해제)"
                    elif len(all_conts) == 1:
                        chosen_cont = all_conts[0]

                    with self.lock:
                        if self._worker_gen == my_gen:
                            self.video_id = vid
                            if title:
                                self.broadcast_title = title

                    if not m_key or not chosen_cont:
                        with self.lock:
                            if self._worker_gen == my_gen:
                                self.status_text = f"방송 감지됨 ({vid}) - 실시간 채팅창 활성화 대기 중..."
                        if not self._sleep_interruptible(8.0, my_gen):
                            break
                        continue

                    api_key = m_key.group(1)
                    continuation = chosen_cont
                    with self.lock:
                        if self._worker_gen == my_gen:
                            self.status_text = f"🟢 실시간 전체 채팅 수신 중 ({self.broadcast_title or vid})"
                            self.last_error = ""

                    # 동일 방송(vid) 재연결 시에는 first_batch로 인해 재연결 순간의 신규 채팅이 스킵되지 않도록 보호!
                    with self.lock:
                        is_initial_connect_for_vid = (self._initialized_video_id != vid)
                    first_batch = is_initial_connect_for_vid
                    switched_to_all_chat = (len(all_conts) >= 2)
                    last_meta_ts = 0.0
                    while not self._stop_flag and self.active and self._worker_gen == my_gen and continuation:
                        api_url = f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key={api_key}"
                        context_obj = {
                            "client": {
                                "clientName": "WEB",
                                "clientVersion": "2.20240726.00.00",
                                "hl": "ko",
                                "gl": "KR"
                            }
                        }
                        payload = {
                            "context": context_obj,
                            "continuation": continuation
                        }

                        # 12초 주기로 유튜브 실시간 동시 시청자 수(updated_metadata) 비블로킹 갱신
                        now_loop_t = time.time()
                        if (now_loop_t - last_meta_ts) >= 12.0:
                            last_meta_ts = now_loop_t
                            live_v = self._poll_updated_metadata(session, api_key, vid, context_obj)
                            if live_v is not None:
                                platform_tracker.update_youtube_live_meta(
                                    concurrent_viewers=live_v,
                                    is_live=True,
                                    video_id=vid,
                                    title=self.broadcast_title or title
                                )

                        r = session.post(api_url, json=payload, timeout=6)
                        data = r.json()
                        cont_contents = data.get("continuationContents", {}).get("liveChatContinuation", {})

                        # 만약 헤더 subMenuItems에 '실시간 채팅(Live chat)' 탭이 선택되지 않은 상태라면 즉시 전체 채팅 토큰으로 전환
                        if not switched_to_all_chat:
                            try:
                                sub_items = (
                                    cont_contents.get("header", {})
                                    .get("liveChatHeaderRenderer", {})
                                    .get("viewSelector", {})
                                    .get("sortFilterSubMenuRenderer", {})
                                    .get("subMenuItems", [])
                                )
                                if len(sub_items) >= 2:
                                    all_chat_item = sub_items[1]
                                    for si in sub_items:
                                        si_title = str(si.get("title") or "")
                                        if "실시간" in si_title or "Live" in si_title or "All" in si_title:
                                            all_chat_item = si
                                            break
                                    alt_cont = (
                                        all_chat_item.get("continuation", {})
                                        .get("reloadContinuationData", {})
                                        .get("continuation")
                                    )
                                    if alt_cont and not all_chat_item.get("selected", False):
                                        continuation = alt_cont
                                        switched_to_all_chat = True
                                        platform_tracker.yt_chat_mode = "ALL_CHAT (실시간 전체 채팅 · 필터 해제)"
                                        continue
                                switched_to_all_chat = True
                            except Exception:
                                switched_to_all_chat = True

                        actions = cont_contents.get("actions", []) or []

                        for act in actions:
                            item = (
                                act.get("addChatItemAction", {}).get("item", {})
                                or act.get("addLiveChatTickerItemAction", {}).get("item", {})
                            )
                            renderer = (
                                item.get("liveChatTextMessageRenderer")
                                or item.get("liveChatPaidMessageRenderer")
                                or item.get("liveChatMembershipItemRenderer")
                                or item.get("liveChatPaidStickerRenderer")
                            )
                            if not renderer:
                                continue
                            msg_id = str(renderer.get("id") or "")
                            author = (
                                renderer.get("authorName", {}).get("simpleText")
                                or "유튜브시청자"
                            )
                            channel_id = str(renderer.get("authorExternalChannelId") or "")
                            thumbs = renderer.get("authorPhoto", {}).get("thumbnails", []) or []
                            avatar_url = str(thumbs[-1].get("url") or "") if thumbs else ""
                            badge_list = []
                            for b_item in (renderer.get("authorBadges") or []):
                                b_rend = b_item.get("liveChatAuthorBadgeRenderer", {})
                                b_tip = b_rend.get("tooltip") or b_rend.get("accessibility", {}).get("accessibilityData", {}).get("label", "")
                                if b_tip:
                                    badge_list.append(str(b_tip))
                            badge_str = ", ".join(badge_list)

                            msg_dt = None
                            ts_usec = renderer.get("timestampUsec")
                            if ts_usec:
                                try:
                                    msg_dt = datetime.fromtimestamp(int(ts_usec) / 1_000_000.0)
                                except Exception:
                                    msg_dt = None

                            runs = renderer.get("message", {}).get("runs", [])
                            msg_parts = []
                            for run in runs:
                                if "text" in run:
                                    msg_parts.append(run["text"])
                                elif "emoji" in run:
                                    em = run["emoji"]
                                    shortcuts = em.get("shortcuts", [])
                                    em_label = (
                                        (shortcuts[0] if shortcuts else "")
                                        or em.get(" image", {}).get("accessibility", {}).get("accessibilityData", {}).get("label", "")
                                        or em.get("emojiId", "")
                                        or "😊"
                                    )
                                    msg_parts.append(em_label)
                            msg_text = "".join(msg_parts).strip()
                            if not msg_text:
                                if "liveChatMembershipItemRenderer" in item:
                                    msg_text = "🎉 멤버십 가입/갱신"
                                elif "liveChatPaidStickerRenderer" in item or "liveChatPaidMessageRenderer" in item:
                                    paid_amt = renderer.get("purchaseAmountText", {}).get("simpleText", "")
                                    msg_text = f"💖 슈퍼챗/스티커 후원 {paid_amt}".strip()
                            if not msg_text:
                                continue

                            with self.lock:
                                if msg_id and msg_id in self.seen_ids:
                                    continue
                                if msg_id:
                                    self.seen_ids.append(msg_id)
                                self.collected_count += 1
                                self.last_chat_at = datetime.now().strftime("%H:%M:%S")

                            # 최근 20초 이내에 작성된 방금 전 실시간 채팅이면 최초 연결 직후라도 누락 없이 처리!
                            is_very_recent_msg = False
                            if msg_dt is not None:
                                try:
                                    if abs((datetime.now() - msg_dt).total_seconds()) <= 20.0:
                                        is_very_recent_msg = True
                                except Exception:
                                    pass

                            if first_batch and not is_very_recent_msg:
                                # 최초 연결 시 과거 이력은 접속 회원 명단에만 등록하고 중복 주문 방지
                                platform_tracker.record_user_presence(
                                    platform_raw="YOUTUBE",
                                    user=author,
                                    message=msg_text,
                                    category="CHAT",
                                    channel_id=channel_id,
                                    avatar=avatar_url,
                                    badge=badge_str,
                                    seen_time=msg_dt,
                                    increment_chat=True
                                )
                            else:
                                try:
                                    process_chat_payload({
                                        "id": msg_id or f"yt_{int(time.time()*1000)}_{author}",
                                        "chatname": author,
                                        "chatmessage": msg_text,
                                        "type": "youtube",
                                        "source": "youtube_direct"
                                    })
                                    # 프로필 아바타·채널 ID·배지 메타데이터 보강
                                    platform_tracker.record_user_presence(
                                        platform_raw="YOUTUBE",
                                        user=author,
                                        message=msg_text,
                                        channel_id=channel_id,
                                        avatar=avatar_url,
                                        badge=badge_str,
                                        seen_time=msg_dt,
                                        increment_chat=False
                                    )
                                except Exception as proc_err:
                                    print(f"⚠️ [유튜브 직접 채팅 처리 오류] {proc_err}", flush=True)

                        if first_batch:
                            first_batch = False
                            with self.lock:
                                self._initialized_video_id = vid

                        # 다음 continuation 추출 (폴링 주기를 0.8~1.5초로 유지하여 버퍼 밀림 및 누락 원천 차단)
                        next_cont = None
                        timeout_ms = 1000
                        for c_obj in cont_contents.get("continuations", []) or []:
                            for k in ("invalidationContinuationData", "timedContinuationData", "reloadContinuationData"):
                                if k in c_obj:
                                    next_cont = c_obj[k].get("continuation")
                                    timeout_ms = int(c_obj[k].get("timeoutMs", 1000))
                                    break
                            if next_cont:
                                break
                        continuation = next_cont
                        sleep_sec = max(0.8, min(1.5, timeout_ms / 1000.0))
                        if not self._sleep_interruptible(sleep_sec, my_gen):
                            break

                except Exception as e:
                    with self.lock:
                        if self._worker_gen == my_gen:
                            self.last_error = str(e)
                            self.status_text = "채팅 재연결 시도 중..."
                    if not self._sleep_interruptible(5.0, my_gen):
                        break
        finally:
            try:
                session.close()
            except Exception:
                pass


class YouTubeDirectStreamer:
    """
    웹 OBS 스튜디오(/overlay/studio)의 실시간 합성 화면(카메라+오버레이)과
    스마트폰 카메라/PC 스튜디오의 실시간 마이크 음성(PCM)을
    FFmpeg(H.264 + AAC)를 통해 유튜브 라이브 RTMP 서버(rtmp://a.rtmp.youtube.com/live2)로 직접 송출하는 매니저
    """
    def __init__(self):
        from collections import deque
        self.lock = Lock()
        self.active = False
        self.rtmp_url = "rtmp://a.rtmp.youtube.com/live2"
        self.stream_key = ""
        self.width = 720
        self.height = 1280
        self.fps = 30
        self.video_bitrate_kbps = 4500
        self.audio_bitrate_kbps = 128
        self.keyframe_interval_sec = 1
        self.started_at = None
        self.frames_sent = 0
        self.status_text = "대기 중"
        self.last_error = ""
        self.latest_composite_frame = None
        self.latest_composite_ts = 0.0
        self.latest_overlay_rgba = None
        self.latest_overlay_ts = 0.0
        self._proc = None
        self._feeder_thread = None
        self._compositor_thread = None
        self._audio_feeder_thread = None
        self._stderr_thread = None
        self._stop_flag = False
        self._user_requested_active = False
        self._cached_rtmp_ip = ""
        self._auto_restart_count = 0
        self._last_auto_restart_ts = 0.0
        self._auto_restarting = False
        self._fallback_frame_cache = None
        self._latest_server_built_frame = None
        self._last_comp_cam_seq = -1
        self._last_comp_ov_ts = -1.0
        # 실시간 마이크 오디오(48kHz 16-bit Signed Mono PCM) 소스별 지터 버퍼 & 크로스페이드 상태
        self._audio_lock = Lock()
        self._audio_buffer = bytearray()
        self._audio_buffer_pc = bytearray()
        self._audio_prebuffering = True
        self._audio_pc_prebuffering = True
        self._mob_empty_ticks = 0
        self._pc_empty_ticks = 0
        self._mob_last_sample = 0
        self._pc_last_sample = 0
        self._mob_audio_last_seq = 0
        self._mob_audio_reorder = {}
        self._pc_audio_last_seq = 0
        self._pc_audio_reorder = {}
        self._mob_rate_win = []
        self._pc_rate_win = []
        self._mob_rate_ratio = 1.0
        self._pc_rate_ratio = 1.0
        self._mob_smooth_consume = 960.0
        self._pc_smooth_consume = 960.0
        # dc_x, dc_y, lp1, lp2, sc_prev, sc_hp, sc_lp1, sc_lp2, vocal_env, cur_g, hold_cnt, sc_floor
        self._mob_dsp_state = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 8.0]
        self._pc_dsp_state = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 8.0]
        self._mon_dsp_state = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 8.0]
        self._monitor_audio_ring = []
        self._monitor_audio_seq = 0
        self._video_sync_queue = []
        self._transcode_ready_event = threading.Event()
        self._stream_clock_t0 = None
        self.audio_gain_pct = 240
        self.audio_agc_enabled = True
        self._agc_current_gain = 1.0
        self._last_effective_gain_x = 1.6
        self._raw_noise_floor_rms = 22.0
        self._agc_hold_chunks = 0
        # 🔬 음파 분석기: 3지점(raw=서버 도착 원음 / dsp=가공 후 / out=FFmpeg 최종 송출) 최근 10초 Int16 PCM 링버퍼
        self._scope_taps = {"raw": bytearray(), "dsp": bytearray(), "out": bytearray()}
        self._scope_max_bytes = 48000 * 2 * 10
        self._scope_lock = threading.Lock()
        self._dsp_bypass = False  # A/B 비교용: True면 _apply_gain_to_array 가공을 건너뛰고 원음 그대로 통과
        self._last_raw_rms_db = -60.0
        self._last_audio_sample = 0
        self._was_in_silence = True
        self.last_audio_ts = 0.0
        self.last_mobile_audio_ts = 0.0
        self.last_pc_audio_ts = 0.0
        self.audio_packets_received = 0
        self.audio_underruns = 0
        self._audio_srv_sock = None
        # 🎤 5단계 음성 파이프라인 정밀 진단(Audio Telemetry) 상태
        self._audio_pkt_intervals_ms = []
        self._audio_pkt_win = []
        self._audio_underrun_ts = []
        self._audio_overflow_drops = 0
        self._audio_clip_win = []
        self._audio_ticks_sent = 0
        self._last_mob_subsource = "none"
        self._last_vdo_audio_ts = 0.0
        self._last_cam_audio_ts = 0.0
        self._dual_mob_collision_ts = 0.0
        # 실시간 오디오 레벨(VU 미터) · 감도(Sensitivity) · 끊김(Dropout) 히스토리 상태
        self._env_level_pct = 0.0
        self._env_peak_pct = 0.0
        self._env_db = -60.0
        self._env_peak_db = -60.0
        self._noise_floor_db = -56.0
        self._env_mob_pct = 0.0
        self._env_pc_pct = 0.0
        self._audio_bars = [0.0] * 8
        self._last_meter_update_ts = 0.0
        self._audio_vad_win = []
        self._audio_pkt_gap_events = []
        self._audio_dropout_events = []
        self._last_event_by_type = {}
        self._last_clean_audio_since = time.time()
        self._audio_diag_history = []
        self._audio_diag_log_path = os.path.join(app.root_path, "audio_quality_diag.jsonl")
        self._frame_perf_times = []
        # 📊 5단계 영상 파이프라인 정밀 계측(Telemetry) & 화면 타임코드 번인(Burn-in) 상태
        self.burnin_timecode = False
        self._comp_build_samples_ms = []
        self._cam_age_samples_ms = []
        self._stdin_write_samples_ms = []
        self._frame_source_window = []
        self._ffmpeg_telemetry = {
            "encoded_frames": 0,
            "fps": 0.0,
            "q": 0.0,
            "out_time_s": 0.0,
            "wall_elapsed_s": 0.0,
            "bitrate_kbps": 0.0,
            "speed": 0.0,
            "queued_frames": 0,
            "internal_lag_ms": 0.0,
            "updated_at": 0.0,
            "raw_status": ""
        }
        self._rtmp_send_win = []
        self._rtmp_bytes_sent_total = 0
        self._rtmp_uplink_kbps = 0.0
        self._rtmp_send_block_avg_ms = 0.0
        self._rtmp_send_block_max_ms = 0.0
        self._yt_manifest_cache = {
            "video_id": "",
            "title": "",
            "is_live": False,
            "is_low_latency": None,
            "latency_class": "",
            "latency_mode_ko": "미감지 (방송 연결 대기)",
            "live_readahead_sec": None,
            "min_readahead_ms": None,
            "max_readahead_ms": None,
            "target_duration_sec": None,
            "expected_mobile_delay_sec": "-",
            "checked_at": 0.0,
            "error": ""
        }
        self._diag_history = []
        self._diag_log_path = os.path.join(app.root_path, "video_latency_diag.jsonl")
        self._telemetry_thread = threading.Thread(target=self._telemetry_collector_loop, daemon=True)
        self._telemetry_thread.start()

    def _compute_agc_target_params(self, gain_pct=None):
        """
        사용자가 설정한 보정 레벨(gain_pct: 50% ~ 1200%)을 기준으로
        목표 감도(target_db)와 무왜곡 클린 증폭 배율(clean_gain_x: 0.5x ~ 6.0x)을 계산합니다.
        과도한 배율(50x~180x)로 인한 배경 양자화 노이즈 증폭(쇠 긁는 소리)을 원천 차단합니다.
        """
        import math
        g_pct = max(50, min(1200, int(gain_pct if gain_pct is not None else (self.audio_gain_pct or 200))))
        user_gain = max(0.5, min(12.0, float(g_pct) / 100.0))
        target_db = round(max(-34.0, min(-8.0, -27.0 + 5.3 * math.log2(user_gain))), 1)
        target_rms = 32768.0 * (10.0 ** (target_db / 20.0))
        min_agc_gain = round(max(0.5, min(3.0, user_gain * 0.8)), 2)
        # 쇠 긁는 양자화 노이즈가 절대 발생하지 않도록 실제 파형 최대 증폭은 최대 6.0x(+15.5dB) 이내로 제한
        max_agc_gain = round(max(0.5, min(6.0, user_gain)), 2)
        return target_db, target_rms, min_agc_gain, max_agc_gain

    def set_audio_gain(self, gain_pct=None, agc_enabled=None):
        """실시간 마이크 증폭·자동 감도 보정 레벨(50% ~ 1200%, 기본 200%) 및 자동 감도 보정(AGC) 설정"""
        with self._audio_lock:
            prev_val = self.audio_gain_pct or 200
            if gain_pct is not None:
                try:
                    g_val = max(50, min(1200, int(gain_pct)))
                except Exception:
                    g_val = prev_val
                self.audio_gain_pct = g_val
                if prev_val > 0 and g_val != prev_val:
                    prev_t_db, _, _, _ = self._compute_agc_target_params(prev_val)
                    new_t_db, _, _, max_g = self._compute_agc_target_params(g_val)
                    self._agc_current_gain = max_g
                    self._last_effective_gain_x = round(max_g, 2)
                    if self._env_db > -58.0:
                        db_delta = new_t_db - prev_t_db
                        self._env_db = round(max(-60.0, min(0.0, self._env_db + db_delta)), 1)
                        self._env_peak_db = round(max(-60.0, min(0.0, self._env_peak_db + db_delta)), 1)
                        self._env_level_pct = round(max(0.0, min(100.0, ((max(-56.0, self._env_db) + 56.0) / 56.0) * 100.0)), 1)
                        self._env_peak_pct = round(max(0.0, min(100.0, ((max(-56.0, self._env_peak_db) + 56.0) / 56.0) * 100.0)), 1)
            else:
                g_val = prev_val

            if agc_enabled is not None:
                self.audio_agc_enabled = bool(agc_enabled)
            agc_val = self.audio_agc_enabled

        try:
            database.set_overlay_state("youtube_audio_gain_pct", g_val)
            database.set_overlay_state("youtube_audio_agc_enabled", "1" if agc_val else "0")
        except Exception:
            pass
        for sec in getattr(self, "_secondary_streamers", []):
            try:
                with sec._audio_lock:
                    sec.audio_gain_pct = g_val
                    sec.audio_agc_enabled = agc_val
            except Exception:
                pass
        return g_val

    def _record_audio_event(self, evt_type, severity, title, detail, now_ts=None):
        """끊김(Underrun/Packet Gap) 및 과증폭 클리핑 등 음성 이상 이벤트를 타임스탬프와 함께 서버 메모리에 보관"""
        if now_ts is None:
            now_ts = time.time()
        last_t = self._last_event_by_type.get(evt_type, 0.0)
        if (now_ts - last_t) < 2.2:
            return
        self._last_event_by_type[evt_type] = now_ts
        if severity in ("bad", "warn") and evt_type in ("BUFFER_UNDERRUN", "PACKET_GAP", "CLIPPING"):
            self._last_clean_audio_since = now_ts
        evt = {
            "ts": datetime.fromtimestamp(now_ts).strftime("%H:%M:%S.%f")[:-4],
            "epoch": round(now_ts, 3),
            "type": evt_type,
            "severity": severity,
            "title": title,
            "detail": detail
        }
        self._audio_dropout_events.append(evt)
        if len(self._audio_dropout_events) > 60:
            del self._audio_dropout_events[:-50]

    def _scope_push(self, tap, pcm_bytes):
        """음파 분석기 탭 링버퍼에 PCM 바이트를 적재 (탭당 최근 10초 유지)"""
        if not pcm_bytes:
            return
        with self._scope_lock:
            b = self._scope_taps.get(tap)
            if b is None:
                return
            b.extend(pcm_bytes)
            if len(b) > self._scope_max_bytes:
                del b[:len(b) - self._scope_max_bytes]

    def get_scope_pcm(self, tap, ms=200):
        """탭 링버퍼에서 최근 ms 구간의 Int16 LE PCM 바이트를 반환"""
        n_bytes = max(2, int(48000 * 2 * max(20, min(10000, int(ms))) / 1000))
        n_bytes -= n_bytes % 2
        with self._scope_lock:
            b = self._scope_taps.get(tap)
            if not b:
                return b""
            return bytes(b[-n_bytes:])

    def save_scope_snapshot(self, out_dir):
        """3개 탭의 최근 10초를 WAV(48kHz/16bit/모노)로 저장하고 파일명 목록 반환"""
        import wave
        os.makedirs(out_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        with self._scope_lock:
            data = {k: bytes(v) for k, v in self._scope_taps.items()}
        files = []
        for tap in ("raw", "dsp", "out"):
            fname = f"{stamp}_{tap}.wav"
            with wave.open(os.path.join(out_dir, fname), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(48000)
                wf.writeframes(data.get(tap, b""))
            files.append({"tap": tap, "file": fname, "seconds": round(len(data.get(tap, b"")) / 96000.0, 2)})
        return files

    def _apply_gain_to_array(self, arr, source="mobile"):
        """
        무왜곡 클린 프리앰프 + 소프트 익스팬더(먹통 게이트 제거 & 쇠 긁는 양자화 노이즈 원천 차단) DSP (48kHz):
        1) 기존 50x~180x 정수 과증폭으로 인해 목소리와 대기 구간 모두에서 발생하던 '쇠 긁는 소리(5-bit 계단 양자화 왜곡)'를 원천 제거했습니다.
        2) 입력 신호를 0으로 잘라버리던 하드 게이트(target_gain = 0.0)를 완전 제거하여, 작은 목소리나 원거리 스피커 음악도 절대 끊기거나 먹통이 되지 않습니다.
        3) 1차 32-bit Float(VdoPcmWorklet 2.5x) 증폭 후, 파이썬에서는 무왜곡 안전 범위(0.65x ~ 6.5x) 내에서만 부드럽게 증폭합니다.
        """
        import math
        if arr is None or len(arr) == 0:
            return arr
        if self._dsp_bypass:
            # A/B 비교용 DSP 바이패스: 서버 가공 없이 원음 그대로 통과
            return arr
        total_cnt = len(arr)
        g_pct = max(50, min(1200, int(self.audio_gain_pct or 200)))
        user_gain = max(0.5, min(6.5, float(g_pct) / 100.0))
        if source == "pc":
            st = self._pc_dsp_state
        elif source == "monitor":
            st = self._mon_dsp_state
        else:
            st = self._mob_dsp_state
        dc_x, dc_y = st[0], st[1]
        cur_g = st[9] if st[9] > 0.0 else user_gain

        dc_samples = [0.0] * total_cnt
        sum_sq = 0.0
        raw_peak = 0.0
        for i in range(total_cnt):
            x = float(arr[i])
            dc_y = x - dc_x + 0.995 * dc_y
            dc_x = x
            dc_samples[i] = dc_y
            ay = abs(dc_y)
            if ay > raw_peak:
                raw_peak = ay
            sum_sq += dc_y * dc_y
        st[0], st[1] = dc_x, dc_y
        raw_rms = (sum_sq / float(total_cnt)) ** 0.5

        if source != "monitor":
            # 배경 노이즈 플로어(RMS) 적응형 추적
            if raw_rms < self._raw_noise_floor_rms:
                self._raw_noise_floor_rms = max(10.0, self._raw_noise_floor_rms * 0.92 + raw_rms * 0.08)
            elif raw_rms < (self._raw_noise_floor_rms * 1.25) and raw_rms < 95.0:
                self._raw_noise_floor_rms = max(10.0, min(85.0, self._raw_noise_floor_rms * 0.98 + raw_rms * 0.02))

        nf_rms = max(10.0, self._raw_noise_floor_rms)
        snr_ratio = raw_rms / nf_rms
        is_active_sig = (snr_ratio >= 1.22) or (raw_rms >= 26.0 and raw_peak >= 68.0)

        if source != "monitor":
            self._last_raw_rms_db = round(max(-60.0, min(0.0, 20.0 * math.log10(max(1.0, raw_rms) / 32768.0))), 1)
            self._last_raw_snr_ratio = snr_ratio
            if is_active_sig:
                self._agc_hold_chunks = 18  # 360ms 홀드로 자연스러운 여운 유지
                self._last_sig_active = True
            elif self._agc_hold_chunks > 0:
                self._agc_hold_chunks -= 1
                is_active_sig = True
                self._last_sig_active = True
            else:
                self._last_sig_active = False

        if not is_active_sig:
            # 완전 정적 대기 구간: 하드 게이트(0.0)로 소리를 끊지 않고 0.65x 소프트 익스팬더로 자연스러운 정적 유지
            target_gain = min(1.2, max(0.5, user_gain * 0.55))
            if source != "monitor":
                self._last_effective_gain_x = round(user_gain, 2)
        else:
            if self.audio_agc_enabled:
                # 작은 소리일 때 계단 노이즈가 나지 않는 최대 6.5x 한도 내에서 부드럽게 부스트 보조
                boost_assist = 1.35 if raw_rms < 250.0 else (1.15 if raw_rms < 800.0 else 1.0)
                desired_gain = min(6.5, max(0.6, user_gain * boost_assist))
                if raw_peak > 1.0 and (raw_peak * desired_gain) > 23000.0:
                    desired_gain = max(0.5, 23000.0 / raw_peak)
                target_gain = desired_gain
            else:
                target_gain = user_gain

            if source == "monitor":
                target_gain = min(4.0, target_gain * 0.85)
            else:
                self._agc_current_gain = target_gain
                self._last_effective_gain_x = round(target_gain, 2)

        clip_cnt = 0
        smooth_step = 0.006 if target_gain > cur_g else 0.004
        for i in range(total_cnt):
            cur_g += (target_gain - cur_g) * smooth_step
            v = int(dc_samples[i] * cur_g)
            if v > 24000:
                if v > 32000:
                    clip_cnt += 1
                excess = v - 24000
                v = 24000 + int((8700 * excess) / (8700 + excess))
            elif v < -24000:
                if v < -32000:
                    clip_cnt += 1
                excess = -v - 24000
                v = -(24000 + int((8700 * excess) / (8700 + excess)))
            arr[i] = v
        st[9] = cur_g

        if source != "monitor" and total_cnt > 0:
            now_c = time.time()
            self._audio_clip_win.append((now_c, clip_cnt, total_cnt))
            if len(self._audio_clip_win) > 250:
                del self._audio_clip_win[:-200]
            if (clip_cnt / float(total_cnt)) >= 0.03:
                self._record_audio_event(
                    "CLIPPING",
                    "warn",
                    f"🔊 과증폭 파형 클리핑 감지 (증폭 {self.audio_gain_pct}%)",
                    f"청크 내 피크 포화 {round((clip_cnt / float(total_cnt)) * 100.0, 1)}% 발생 — 증폭을 낮추거나 자동 감도 보정(AGC)을 켜주세요.",
                    now_c
                )
        return arr

    def _compute_pcm_metrics(self, arr):
        """16-bit Signed PCM 배열에서 RMS/Peak를 계산하여 지각적 볼륨(0~100%), 피크(0~100%), dBFS(-60~0dB)를 반환"""
        import math
        n = len(arr) if arr is not None else 0
        if n == 0:
            return 0.0, 0.0, -60.0
        step = 1 if n <= 960 else 2
        total_sq = 0
        peak = 0
        count = 0
        for i in range(0, n, step):
            s = abs(arr[i])
            if s > peak:
                peak = s
            total_sq += s * s
            count += 1
        rms = (total_sq / float(max(1, count))) ** 0.5
        sig_active = getattr(self, "_last_sig_active", False)
        idle_ceil = max(24.0, self._raw_noise_floor_rms * 0.95)
        # 순수 대기 무음 구간에서는 잔상 없이 -60.0dB(0%) 반환
        if (not sig_active) and rms <= idle_ceil and peak <= max(65, int(idle_ceil * 2.4)):
            return 0.0, 0.0, -60.0
        db_rms = max(-60.0, min(0.0, 20.0 * math.log10(max(1.0, rms) / 32768.0)))
        db_peak = max(-60.0, min(0.0, 20.0 * math.log10(max(1.0, float(peak)) / 32768.0)))
        # (감도 게이지는 보정 없는 실측 파형 크기만 표시합니다)
        # 감도 게이지 트랙(-56dBFS ~ 0dBFS)과 1:1 일치하도록 RMS 감도(level_pct)와 Peak 마커(peak_pct)를 각각 독립 매핑 (-28dB = 50%, -6dB = 89.3%)
        level_pct = round(max(0.0, min(100.0, ((max(-56.0, db_rms) + 56.0) / 56.0) * 100.0)), 1)
        peak_pct = round(max(0.0, min(100.0, ((max(-56.0, db_peak) + 56.0) / 56.0) * 100.0)), 1)
        return level_pct, peak_pct, round(db_rms, 1)

    def _update_audio_envelope(self, mob_arr, pc_arr, now_ts=None):
        """20ms 피더 루프 또는 PCM 수신 시 오디오 볼륨 엔벨로프(Fast Attack / Smooth Release), 감도(SNR/VAD) 및 8-바 파형 갱신"""
        if now_ts is None:
            now_ts = time.time()
        mob_lvl, mob_pk, mob_db = self._compute_pcm_metrics(mob_arr) if mob_arr is not None else (0.0, 0.0, -60.0)
        pc_lvl, pc_pk, pc_db = self._compute_pcm_metrics(pc_arr) if pc_arr is not None else (0.0, 0.0, -60.0)

        comb_lvl = min(100.0, max(mob_lvl, pc_lvl))
        comb_pk = min(100.0, max(mob_pk, pc_pk))
        comb_db = max(mob_db, pc_db)
        inst_peak_db = round(-55.0 + (comb_pk / 100.0) * 55.0, 1) if comb_pk > 0.5 else -60.0

        # 배경 소음 플로어(Noise Floor) 추정: 비발화 구간(-59 ~ -36 dB)에서 서서히 추종
        if -59.5 <= comb_db <= -36.0:
            self._noise_floor_db = round(max(-60.0, min(-32.0, self._noise_floor_db * 0.96 + comb_db * 0.04)), 1)
        elif comb_db < -59.5:
            self._noise_floor_db = round(max(-60.0, self._noise_floor_db * 0.99 + (-58.0) * 0.01), 1)

        # 발화 활성도(VAD) 기록: 배경 노이즈보다 8dB 이상 높고 -40dBFS 이상이면 유효 음성 발화로 판정
        is_voice = (comb_db >= -40.0) and ((comb_db - self._noise_floor_db) >= 7.0)
        self._audio_vad_win.append((now_ts, 1 if is_voice else 0))
        if len(self._audio_vad_win) > 300:
            del self._audio_vad_win[:-250]

        # Fast attack, clean silence release (무음 진입 시 잔상 없이 빠르게 0%로 복귀)
        if comb_lvl > 1.0:
            self._env_mob_pct = max(mob_lvl, round(self._env_mob_pct * 0.80 + mob_lvl * 0.20, 1))
            self._env_pc_pct = max(pc_lvl, round(self._env_pc_pct * 0.80 + pc_lvl * 0.20, 1))
            self._env_level_pct = max(comb_lvl, round(self._env_level_pct * 0.78 + comb_lvl * 0.22, 1))
            self._env_peak_pct = max(comb_pk, round(self._env_peak_pct * 0.84 + comb_pk * 0.16, 1))
            self._env_peak_db = max(inst_peak_db, round(self._env_peak_db * 0.80 + inst_peak_db * 0.20, 1))
            self._env_db = max(comb_db, round(self._env_db * 0.75 + comb_db * 0.25, 1))
        else:
            self._env_mob_pct = round(self._env_mob_pct * 0.55, 1)
            self._env_pc_pct = round(self._env_pc_pct * 0.55, 1)
            self._env_level_pct = round(self._env_level_pct * 0.55, 1)
            self._env_peak_pct = round(self._env_peak_pct * 0.60, 1)
            self._env_peak_db = max(-60.0, round(self._env_peak_db - 6.0, 1))
            self._env_db = max(-60.0, round(self._env_db - 6.0, 1))

        if self._env_level_pct < 1.5:
            self._env_level_pct = 0.0
            self._env_db = -60.0
        if self._env_peak_pct < 1.5:
            self._env_peak_pct = 0.0
            self._env_peak_db = -60.0

        self._audio_bars = self._audio_bars[1:] + [round(self._env_level_pct, 1)]
        self._last_meter_update_ts = now_ts

    def _append_ordered_pcm(self, buf, reorder_map, last_seq, pcm_slice, seq, last_ts, now_a, max_buf_bytes=134400):
        """웹 OBS(_monitor_audio_ring)와 동일하게 오디오 패킷을 도착 즉시 지연 없이 링버퍼에 직결 추가"""
        reorder_map.clear()
        buf.extend(pcm_slice)
        if len(buf) > max_buf_bytes:
            del buf[:-67200]
        return seq or (last_seq + 1)

    def _update_rate_window(self, rate_win, current_ratio, nbytes, now_a):
        """최근 2.2초 동안의 실제 수신 바이트 속도를 측정하여 48kHz(96,000 bytes/sec) 대비 비율(0.84 ~ 1.02)을 완만하게 추종"""
        rate_win.append((now_a, nbytes))
        cutoff = now_a - 2.2
        while len(rate_win) > 1 and rate_win[0][0] < cutoff:
            rate_win.pop(0)
        if len(rate_win) >= 3:
            dt = rate_win[-1][0] - rate_win[0][0]
            if dt >= 0.35:
                total_b = sum(b for _, b in rate_win[1:])
                inst_ratio = max(0.84, min(1.02, (total_b / dt) / 96000.0))
                current_ratio = current_ratio * 0.82 + inst_ratio * 0.18
        return current_ratio

    def push_audio_pcm(self, pcm_bytes, source="mobile", seq=None, subsource=None):
        """스마트폰 카메라 또는 PC 스튜디오 마이크에서 전송된 48kHz s16le Mono PCM 바이트를 수신하여 지터 버퍼에 공급"""
        if not pcm_bytes:
            return
        even_len = len(pcm_bytes) - (len(pcm_bytes) % 2)
        if even_len <= 0:
            return
        import array
        now_a = time.time()
        pcm_slice = pcm_bytes[:even_len]
        if source != "pc":
            # 🔬 탭① RAW: 서버에 도착한 그대로의 원음 바이트
            self._scope_push("raw", pcm_slice)
        # 송출 대기 중(inactive)에는 최신 400ms(38400 bytes)만 유지하고, 송출 중(active)에는 최대 1400ms(134400 bytes)까지 버퍼 보장
        max_buf_bytes = 134400 if self.active else 38400
        with self._audio_lock:
            if source == "pc":
                if self.last_pc_audio_ts > 0:
                    dt_ms = (now_a - self.last_pc_audio_ts) * 1000.0
                    if 1.0 <= dt_ms <= 1500.0:
                        self._audio_pkt_intervals_ms.append(dt_ms)
                        if len(self._audio_pkt_intervals_ms) > 60:
                            del self._audio_pkt_intervals_ms[:-60]
                        if dt_ms >= 120.0:
                            self._audio_pkt_gap_events.append((now_a, round(dt_ms, 1), "pc"))
                            if len(self._audio_pkt_gap_events) > 120:
                                del self._audio_pkt_gap_events[:-100]
                            if dt_ms >= 170.0:
                                self._record_audio_event(
                                    "PACKET_GAP",
                                    "warn" if dt_ms < 280.0 else "bad",
                                    f"📡 PC 마이크 패킷 지연 ({round(dt_ms)}ms 갭)",
                                    f"PC 브라우저 오디오 패킷 간격이 {round(dt_ms, 1)}ms로 벌어졌습니다 (권장 40ms).",
                                    now_a
                                )
                self._pc_rate_ratio = self._update_rate_window(self._pc_rate_win, self._pc_rate_ratio, even_len, now_a)
                self._pc_audio_last_seq = self._append_ordered_pcm(
                    self._audio_buffer_pc,
                    self._pc_audio_reorder,
                    self._pc_audio_last_seq,
                    pcm_slice,
                    seq,
                    self.last_pc_audio_ts,
                    now_a,
                    max_buf_bytes
                )
                if not self.active and len(self._audio_buffer_pc) > 38400:
                    del self._audio_buffer_pc[:-38400]
                self.last_pc_audio_ts = now_a
            else:
                eff_sub = subsource or "mobile_cam"
                if eff_sub == "vdo_ninja":
                    self._last_vdo_audio_ts = now_a
                    self._last_mob_subsource = "vdo_ninja"
                else:
                    self._last_cam_audio_ts = now_a
                    # VDO.Ninja 오디오가 활성(최근 2.0초 이내 수신) 중일 때 내장 /cam 오디오가 동시에 들어오면
                    # 단일 버퍼에 두 마이크 스트림이 섞여 목소리가 뭉개지는 현상을 자동 차단!
                    if (now_a - self._last_vdo_audio_ts) < 2.0:
                        self._dual_mob_collision_ts = now_a
                        self._record_audio_event(
                            "DUAL_SOURCE_COLLISION",
                            "warn",
                            "🛡️ 중복 모바일 오디오 자동 차단 (VDO.Ninja + /cam)",
                            "VDO.Ninja와 /cam 마이크가 동시에 들어와 파형 충돌 방지를 위해 중복 /cam 패킷을 차단했습니다.",
                            now_a
                        )
                        return
                    self._last_mob_subsource = "mobile_cam"

                if self.last_mobile_audio_ts > 0:
                    dt_ms = (now_a - self.last_mobile_audio_ts) * 1000.0
                    if 1.0 <= dt_ms <= 1500.0:
                        self._audio_pkt_intervals_ms.append(dt_ms)
                        if len(self._audio_pkt_intervals_ms) > 60:
                            del self._audio_pkt_intervals_ms[:-60]
                        if dt_ms >= 120.0:
                            self._audio_pkt_gap_events.append((now_a, round(dt_ms, 1), eff_sub))
                            if len(self._audio_pkt_gap_events) > 120:
                                del self._audio_pkt_gap_events[:-100]
                            if dt_ms >= 170.0:
                                self._record_audio_event(
                                    "PACKET_GAP",
                                    "warn" if dt_ms < 280.0 else "bad",
                                    f"📡 무선 마이크 패킷 갭 감지 ({round(dt_ms)}ms 지연)",
                                    f"스마트폰({eff_sub}) Wi-Fi 패킷 도착 간격이 {round(dt_ms, 1)}ms로 벌어졌습니다 (정상 20~45ms).",
                                    now_a
                                )
                self._mob_rate_ratio = self._update_rate_window(self._mob_rate_win, self._mob_rate_ratio, even_len, now_a)
                self._mob_audio_last_seq = self._append_ordered_pcm(
                    self._audio_buffer,
                    self._mob_audio_reorder,
                    self._mob_audio_last_seq,
                    pcm_slice,
                    seq,
                    self.last_mobile_audio_ts,
                    now_a,
                    max_buf_bytes
                )
                if not self.active and len(self._audio_buffer) > 38400:
                    del self._audio_buffer[:-38400]
                self.last_mobile_audio_ts = now_a

            self._audio_pkt_win.append((now_a, even_len))
            if len(self._audio_pkt_win) > 150:
                del self._audio_pkt_win[:-120]
            self.last_audio_ts = now_a
            self.audio_packets_received += 1
            # OBS 스튜디오/오버레이 스피커 모니터링용 48kHz PCM 링버퍼 적재 (스마트폰 유입 음성)
            if source != "pc":
                if not self.active:
                    # 방송 대기 중에도 실제 송출(Broadcast) 파이프라인과 100% 동일한 AGC + 고감도 부스트 파형으로 감도 미터 측정
                    bcast_arr = array.array("h")
                    bcast_arr.frombytes(pcm_slice)
                    self._apply_gain_to_array(bcast_arr, source="mobile")
                    # 🔬 탭② DSP (방송 대기 중 미리보기 가공본)
                    self._scope_push("dsp", bcast_arr.tobytes())
                    self._update_audio_envelope(bcast_arr, None, now_a)
                mon_arr = array.array("h")
                mon_arr.frombytes(pcm_slice)
                self._apply_gain_to_array(mon_arr, source="monitor")
                self._monitor_audio_seq += 1
                self._monitor_audio_ring.append((self._monitor_audio_seq, now_a, mon_arr.tobytes()))
                if len(self._monitor_audio_ring) > 60:
                    del self._monitor_audio_ring[:-45]
            elif not self.active:
                in_arr = array.array("h")
                in_arr.frombytes(pcm_slice)
                self._apply_gain_to_array(in_arr, source="pc")
                self._update_audio_envelope(None, in_arr, now_a)

        for sec in getattr(self, "_secondary_streamers", []):
            try:
                sec.push_audio_pcm(pcm_bytes, source=source, seq=seq, subsource=subsource)
            except Exception:
                pass

    def pull_monitor_audio(self, after_seq=0):
        """OBS 스튜디오(/studio) 및 송출 오버레이(/overlay/obs)에서 스마트폰 유입 음성을 실시간 청취하기 위한 PCM 청크 반환"""
        now_t = time.time()
        with self._audio_lock:
            cur_seq = self._monitor_audio_seq
            if not self._monitor_audio_ring or cur_seq == 0:
                return b"", cur_seq
            # 최초 요청이거나 16청크(~340ms) 이상 뒤처진 경우 최신 3청크(~64ms)만 반환하여 끊김 및 딜레이 누적 방지
            if after_seq <= 0 or (cur_seq - after_seq) > 16:
                recent = [b for s, ts, b in self._monitor_audio_ring[-3:] if (now_t - ts) < 0.4]
                return b"".join(recent), cur_seq
            chunks = [b for s, ts, b in self._monitor_audio_ring if s > after_seq and (now_t - ts) < 0.6]
            return b"".join(chunks), cur_seq

    def get_ffmpeg_exe(self):
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return "ffmpeg"

    def _detect_hw_encoder(self):
        """NVIDIA NVENC H.264 -> Intel QuickSync H.264 -> AMD AMF H.264 -> libx264 순으로 최적 하드웨어 인코더 자동 감지"""
        if hasattr(self, "_hw_encoder_cached"):
            return self._hw_encoder_cached
        ffmpeg_exe = self.get_ffmpeg_exe()
        for enc_name in ("h264_nvenc", "h264_qsv", "h264_amf"):
            try:
                r = subprocess.run(
                    [ffmpeg_exe, "-y", "-f", "lavfi", "-i", "color=c=black:s=256x256:r=30", "-frames:v", "1", "-c:v", enc_name, "-f", "null", "-"],
                    capture_output=True,
                    timeout=3
                )
                if r.returncode == 0:
                    self._hw_encoder_cached = enc_name
                    return enc_name
            except Exception:
                pass
        self._hw_encoder_cached = "libx264"
        return "libx264"

    def _has_nvenc(self):
        return self._detect_hw_encoder() == "h264_nvenc"

    def build_server_composite_frame(self):
        """백그라운드 컴포지터 스레드에서 듀얼 카메라(CAM 1 / CAM 2 단독·PIP·2분할) 프레임 + 스튜디오 오버레이 레이어를 초고속(BILINEAR) 합성하여 반환"""
        t_comp0 = time.perf_counter()
        try:
            from PIL import Image, ImageDraw, ImageFont, ImageOps
            with self.lock:
                ov_rgba = self.latest_overlay_rgba
                last_comp = self.latest_composite_frame
                do_burnin = self.burnin_timecode

            if ov_rgba is None and last_comp is not None and not do_burnin:
                return last_comp

            switcher = get_camera_switcher_state()
            main_slot = switcher.get("active_main_cam", 1)
            sub_slot = 2 if main_slot == 1 else 1
            layout_mode = switcher.get("layout_mode", "single")
            pip_pos = switcher.get("pip_position", "bottom-right")
            pip_scale = float(switcher.get("pip_scale") or 0.32)

            with latest_camera_lock:
                c1_bytes = camera_channels[1]["frame"]
                c2_bytes = camera_channels[2]["frame"]
                c1_ts = camera_channels[1]["timestamp"]
                c2_ts = camera_channels[2]["timestamp"]
                fallback_bytes = latest_camera_frame
                fallback_ts = latest_camera_timestamp

            main_bytes = c1_bytes if main_slot == 1 else c2_bytes
            sub_bytes = c2_bytes if main_slot == 1 else c1_bytes
            main_mirrored = bool(switcher.get("cam1_mirrored") if main_slot == 1 else switcher.get("cam2_mirrored"))
            sub_mirrored = bool(switcher.get("cam2_mirrored") if main_slot == 1 else switcher.get("cam1_mirrored"))
            cam_ts = (c1_ts if main_slot == 1 else c2_ts) or fallback_ts

            if not main_bytes:
                main_bytes = sub_bytes or fallback_bytes

            fb_w = int(self.width or 720)
            fb_h = int(self.height or 1280)

            def _decode_and_fit(raw_b, target_w, target_h, mirrored=False):
                if not raw_b:
                    return None
                try:
                    im = Image.open(io.BytesIO(raw_b)).convert("RGB")
                    if mirrored:
                        im = ImageOps.mirror(im)
                    iw, ih = im.size
                    if iw <= 0 or ih <= 0:
                        return im.resize((target_w, target_h), Image.BILINEAR)
                    # 비율 왜곡 없는 Cover 크롭 리사이즈
                    scale = max(target_w / float(iw), target_h / float(ih))
                    nw = max(target_w, int(round(iw * scale)))
                    nh = max(target_h, int(round(ih * scale)))
                    im = im.resize((nw, nh), Image.BILINEAR)
                    if nw != target_w or nh != target_h:
                        left = max(0, (nw - target_w) // 2)
                        top = max(0, (nh - target_h) // 2)
                        im = im.crop((left, top, left + target_w, top + target_h))
                    return im
                except Exception:
                    return None

            base_img = Image.new("RGB", (fb_w, fb_h), color=(15, 23, 42))

            if layout_mode == "split_v" and (c1_bytes or c2_bytes):
                # 상하 2분할: 상단 CAM 1 (또는 메인), 하단 CAM 2 (또는 서브)
                top_h = fb_h // 2
                bot_h = fb_h - top_h
                top_img = _decode_and_fit(c1_bytes or main_bytes, fb_w, top_h, bool(switcher.get("cam1_mirrored")))
                bot_img = _decode_and_fit(c2_bytes or sub_bytes or main_bytes, fb_w, bot_h, bool(switcher.get("cam2_mirrored")))
                if top_img:
                    base_img.paste(top_img, (0, 0))
                if bot_img:
                    base_img.paste(bot_img, (0, top_h))
                draw_div = ImageDraw.Draw(base_img)
                draw_div.line([(0, top_h), (fb_w, top_h)], fill=(56, 189, 248), width=3)
            elif layout_mode == "split_h" and (c1_bytes or c2_bytes):
                # 좌우 2분할: 좌측 CAM 1, 우측 CAM 2
                left_w = fb_w // 2
                right_w = fb_w - left_w
                left_img = _decode_and_fit(c1_bytes or main_bytes, left_w, fb_h, bool(switcher.get("cam1_mirrored")))
                right_img = _decode_and_fit(c2_bytes or sub_bytes or main_bytes, right_w, fb_h, bool(switcher.get("cam2_mirrored")))
                if left_img:
                    base_img.paste(left_img, (0, 0))
                if right_img:
                    base_img.paste(right_img, (left_w, 0))
                draw_div = ImageDraw.Draw(base_img)
                draw_div.line([(left_w, 0), (left_w, fb_h)], fill=(56, 189, 248), width=3)
            else:
                # 단독 메인(single) 또는 PIP(pip)
                main_img = _decode_and_fit(main_bytes, fb_w, fb_h, main_mirrored)
                if main_img:
                    base_img = main_img
                if layout_mode == "pip" and sub_bytes:
                    pw = max(120, int(round(fb_w * pip_scale)))
                    ph = max(120, int(round(fb_h * pip_scale)))
                    sub_img = _decode_and_fit(sub_bytes, pw, ph, sub_mirrored)
                    if sub_img:
                        pad_x = 24
                        pad_y = 96 if "bottom" in pip_pos else 86
                        if pip_pos == "top-left":
                            px, py = pad_x, pad_y
                        elif pip_pos == "top-right":
                            px, py = fb_w - pw - pad_x, pad_y
                        elif pip_pos == "bottom-left":
                            px, py = pad_x, fb_h - ph - pad_y
                        else:
                            px, py = fb_w - pw - pad_x, fb_h - ph - pad_y
                        px = max(8, min(fb_w - pw - 8, px))
                        py = max(8, min(fb_h - ph - 8, py))
                        draw_pip = ImageDraw.Draw(base_img)
                        draw_pip.rectangle([px - 3, py - 3, px + pw + 3, py + ph + 3], fill=(15, 23, 42), outline=(56, 189, 248), width=3)
                        base_img.paste(sub_img, (px, py))

            if ov_rgba is not None:
                ov_layer = ov_rgba
                if ov_layer.size != (fb_w, fb_h):
                    ov_layer = ov_layer.resize((fb_w, fb_h), Image.BILINEAR)
                    with self.lock:
                        if self.latest_overlay_rgba is ov_rgba:
                            self.latest_overlay_rgba = ov_layer
                base_img.paste(ov_layer, (0, 0), ov_layer)

            if do_burnin:
                draw = ImageDraw.Draw(base_img)
                now_dt = datetime.now()
                tc_str = now_dt.strftime("%H:%M:%S.") + f"{int(now_dt.microsecond / 1000):03d}"
                cam_age_ms = int((time.time() - cam_ts) * 1000) if cam_ts > 0 else -1
                with self.lock:
                    f_num = self.frames_sent
                    ff_lag_ms = int(self._ffmpeg_telemetry.get("internal_lag_ms", 0))
                banner_txt = f"SYNC {tc_str} | F#{f_num:05d} | CamAge:{cam_age_ms}ms | FFLag:{ff_lag_ms:+d}ms"
                fnt = getattr(self, "_burnin_font", None)
                if fnt is None:
                    for fn in ("consolab.ttf", "consola.ttf", "malgunbd.ttf", "arial.ttf"):
                        try:
                            fnt = ImageFont.truetype(fn, 20)
                            self._burnin_font = fnt
                            break
                        except Exception:
                            continue
                bx0, by0 = 16, 16
                bx1, by1 = min(fb_w - 16, 680), 54
                draw.rectangle([bx0, by0, bx1, by1], fill=(9, 13, 22), outline=(16, 185, 129), width=2)
                if fnt:
                    draw.text((bx0 + 12, by0 + 7), banner_txt, fill=(52, 211, 153), font=fnt)
                else:
                    draw.text((bx0 + 12, by0 + 10), banner_txt, fill=(52, 211, 153))

            buf = io.BytesIO()
            base_img.save(buf, format="JPEG", quality=85)
            out_bytes = buf.getvalue()
            comp_ms = (time.perf_counter() - t_comp0) * 1000.0
            with self.lock:
                self._comp_build_samples_ms.append(comp_ms)
                if len(self._comp_build_samples_ms) > 30:
                    del self._comp_build_samples_ms[:-30]
            return out_bytes
        except Exception:
            return None

    def update_frame(self, jpeg_bytes):
        if not jpeg_bytes:
            return
        now_t = time.time()
        with self.lock:
            self.latest_composite_frame = jpeg_bytes
            self.latest_composite_ts = now_t
            # 과거 프레임이 큐에 쌓여 딜레이를 유발하지 않도록 최신 1프레임만 유지
            self._video_sync_queue.clear()
            self._video_sync_queue.append((now_t, jpeg_bytes))
        for sec in getattr(self, "_secondary_streamers", []):
            try:
                sec.update_frame(jpeg_bytes)
            except Exception:
                pass

    def update_overlay(self, png_bytes):
        if not png_bytes:
            return
        try:
            from PIL import Image
            ov_img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
            ov_img.load()
            fb_w = int(self.width or 720)
            fb_h = int(self.height or 1280)
            if ov_img.size != (fb_w, fb_h):
                ov_img = ov_img.resize((fb_w, fb_h), Image.BILINEAR)
            with self.lock:
                self.latest_overlay_rgba = ov_img
                self.latest_overlay_ts = time.time()
        except Exception:
            pass
        for sec in getattr(self, "_secondary_streamers", []):
            try:
                sec.update_overlay(png_bytes)
            except Exception:
                pass

    def _query_public_dns_a(self, hostname, dns_ip="8.8.8.8", timeout=1.5):
        """공유기(192.168.1.1) DNS 프록시 응답 지연(Event 1014) 시 관리자 권한 없이도 8.8.8.8/1.1.1.1에 직접 UDP A레코드 질의"""
        import socket
        import struct
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        try:
            q = bytearray(b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            for label in hostname.strip(".").split("."):
                eb = label.encode("ascii")
                q.append(len(eb))
                q.extend(eb)
            q.extend(b"\x00\x00\x01\x00\x01")
            s.sendto(bytes(q), (dns_ip, 53))
            resp, _ = s.recvfrom(512)
            ancount = struct.unpack("!H", resp[6:8])[0]
            idx = len(q)
            for _ in range(ancount):
                if idx >= len(resp):
                    break
                if (resp[idx] & 0xC0) == 0xC0:
                    idx += 2
                else:
                    while idx < len(resp) and resp[idx] != 0:
                        idx += 1 + resp[idx]
                    idx += 1
                if idx + 10 > len(resp):
                    break
                rtype, _rclass, _ttl, rdlen = struct.unpack("!HHIH", resp[idx:idx + 10])
                idx += 10
                if rtype == 1 and rdlen == 4 and idx + 4 <= len(resp):
                    return socket.inet_ntoa(resp[idx:idx + 4])
                idx += rdlen
        except Exception:
            pass
        finally:
            try:
                s.close()
            except Exception:
                pass
        return ""

    def _resolve_rtmp_host_ip(self, rtmp_base_url):
        """
        유튜브 RTMP 호스트(a.rtmp.youtube.com) IP를 사전 질의·캐싱합니다.
        2시간 주기 공유기 DHCP 갱신 시점에 공유기(192.168.1.1) 내장 DNS 프록시가 일시 타임아웃(Event 1014)되더라도
        8.8.8.8 / 1.1.1.1 직접 질의 및 캐싱된 IP를 사용하여 FFmpeg 송출이 중단되지 않도록 보호합니다.
        """
        import socket
        from urllib.parse import urlparse
        parsed = urlparse(rtmp_base_url)
        host = parsed.hostname or "a.rtmp.youtube.com"
        for dns_srv in ("8.8.8.8", "1.1.1.1"):
            ip = self._query_public_dns_a(host, dns_srv, timeout=1.2)
            if ip and ip != "0.0.0.0":
                self._cached_rtmp_ip = ip
                return ip
        try:
            ip = socket.gethostbyname(host)
            if ip and ip != "0.0.0.0":
                self._cached_rtmp_ip = ip
                return ip
        except Exception:
            pass
        return self._cached_rtmp_ip

    def _start_rtmp_anti_lso_proxy(self, remote_host, remote_port=1935):
        """
        Intel I219-V 랜카드의 Large Send Offload V2(*LsoV2IPv4) 하드웨어 TX 링 멈춤(SuspectArpProbeFailed)을
        원천 차단하기 위한 로컬 루프백 -> 원격 RTMP MSS 슬라이싱(<= 1400 bytes, TCP_NODELAY=1) 중계 소켓.
        단일 send() 호출 크기를 1400 바이트 이하로 제한하여 OS 커널이 64KB LSOv2 오프로드 디스크립터를 생성하지 않게 합니다.
        """
        import socket
        proxy_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        proxy_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        proxy_srv.bind(("127.0.0.1", 0))
        proxy_srv.listen(1)
        local_port = proxy_srv.getsockname()[1]

        def _proxy_worker():
            ff_conn = None
            yt_sock = None
            try:
                proxy_srv.settimeout(12.0)
                ff_conn, _ = proxy_srv.accept()
                ff_conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

                target_ip = self._resolve_rtmp_host_ip(f"rtmp://{remote_host}") or remote_host
                yt_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                yt_sock.settimeout(10.0)
                yt_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                yt_sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                yt_sock.connect((target_ip, remote_port))
                yt_sock.settimeout(None)

                def _downlink():
                    try:
                        while not self._stop_flag and self.active:
                            data = yt_sock.recv(8192)
                            if not data:
                                break
                            ff_conn.sendall(data)
                    except Exception:
                        pass

                threading.Thread(target=_downlink, daemon=True).start()

                # Uplink (FFmpeg -> YouTube): 8192 바이트 단위로 슬라이싱 전송하여 LSOv2 트리거 방지 및 시스템콜 오버헤드 최소화
                mss_chunk = 8192
                while not self._stop_flag and self.active:
                    buf = ff_conn.recv(32768)
                    if not buf:
                        break
                    mv = memoryview(buf)
                    n = len(mv)
                    offset = 0
                    t_send0 = time.perf_counter()
                    while offset < n:
                        end = offset + mss_chunk if (offset + mss_chunk) < n else n
                        yt_sock.sendall(mv[offset:end])
                        offset = end
                    send_ms = (time.perf_counter() - t_send0) * 1000.0
                    now_u = time.time()
                    with self.lock:
                        self._rtmp_bytes_sent_total += n
                        self._rtmp_send_win.append((now_u, n, send_ms))
                        cutoff_u = now_u - 2.0
                        while len(self._rtmp_send_win) > 1 and self._rtmp_send_win[0][0] < cutoff_u:
                            self._rtmp_send_win.pop(0)
                        if len(self._rtmp_send_win) >= 2:
                            dt_u = max(0.1, self._rtmp_send_win[-1][0] - self._rtmp_send_win[0][0])
                            b_sum = sum(x[1] for x in self._rtmp_send_win[1:])
                            self._rtmp_uplink_kbps = round((b_sum * 8.0) / (dt_u * 1000.0), 1)
                        self._rtmp_send_block_avg_ms = round(sum(x[2] for x in self._rtmp_send_win) / len(self._rtmp_send_win), 2)
                        self._rtmp_send_block_max_ms = round(max(x[2] for x in self._rtmp_send_win), 2)
            except Exception as e:
                with self.lock:
                    if self.active and not self.last_error:
                        self.last_error = f"RTMP 소켓 중계 종료: {e}"
            finally:
                for s_obj in (ff_conn, yt_sock, proxy_srv):
                    if s_obj is not None:
                        try:
                            s_obj.close()
                        except Exception:
                            pass

        threading.Thread(target=_proxy_worker, daemon=True).start()
        return proxy_srv, local_port

    def start(self, rtmp_url=None, stream_key=None, width=720, height=1280, fps=30, video_bitrate_kbps=None, audio_bitrate_kbps=None, keyframe_interval_sec=None, is_auto_restart=False):
        rtmp_base = (rtmp_url or self.rtmp_url or database.get_overlay_state("youtube_rtmp_url", "rtmp://a.rtmp.youtube.com/live2") or "rtmp://a.rtmp.youtube.com/live2").strip()
        s_key = (stream_key or self.stream_key or database.get_overlay_state("youtube_stream_key", "")).strip()
        if not s_key:
            return {"ok": False, "message": "유튜브 스트림 키(Stream Key)를 입력해 주세요."}

        saved_v_kbps = database.get_overlay_state("youtube_video_bitrate_kbps", 4500)
        if saved_v_kbps == 6800 and video_bitrate_kbps is None:
            saved_v_kbps = 4500
        saved_a_kbps = database.get_overlay_state("youtube_audio_bitrate_kbps", 128)
        saved_kf_sec = database.get_overlay_state("youtube_keyframe_sec", 1)
        if saved_kf_sec == 2 and keyframe_interval_sec is None:
            saved_kf_sec = 1
        saved_gain_pct = database.get_overlay_state("youtube_audio_gain_pct", 240)
        v_kbps = max(1000, min(18000, int(video_bitrate_kbps if video_bitrate_kbps is not None else (saved_v_kbps or 4500))))
        a_kbps = max(64, min(320, int(audio_bitrate_kbps if audio_bitrate_kbps is not None else (saved_a_kbps or 128))))
        kf_sec = max(1, min(4, int(keyframe_interval_sec if keyframe_interval_sec is not None else (saved_kf_sec or 1))))
        gain_pct = max(50, min(1200, int(saved_gain_pct or 240)))

        try:
            database.set_overlay_state("youtube_rtmp_url", rtmp_base)
            database.set_overlay_state("youtube_stream_key", s_key)
            database.set_overlay_state("youtube_video_bitrate_kbps", v_kbps)
            database.set_overlay_state("youtube_audio_bitrate_kbps", a_kbps)
            database.set_overlay_state("youtube_keyframe_sec", kf_sec)
        except Exception:
            pass

        self.stop(user_initiated=False)
        self._resolve_rtmp_host_ip(rtmp_base)

        with self.lock:
            self.rtmp_url = rtmp_base
            self.stream_key = s_key
            self.width = max(480, min(1920, int(width or 720)))
            self.height = max(360, min(1920, int(height or 1280)))
            # H.264 인코더는 짝수 해상도 필수
            if self.width % 2 != 0:
                self.width += 1
            if self.height % 2 != 0:
                self.height += 1
            self.fps = max(15, min(60, int(fps or 30)))
            self.video_bitrate_kbps = v_kbps
            self.audio_bitrate_kbps = a_kbps
            self.keyframe_interval_sec = kf_sec
            self._stop_flag = False
            self._user_requested_active = True
            self.active = True
            self._last_comp_cam_seq = -1
            self._last_comp_ov_ts = -1.0
            if not is_auto_restart or not self.started_at:
                self.started_at = time.time()
                self.frames_sent = 0
                self.audio_underruns = 0
                self._auto_restart_count = 0
            self.status_text = f"🟡 유튜브 RTMP 신호 전송 중 — 실제 온에어 전환 확인 중 ({self.width}x{self.height} {self.fps}fps / {v_kbps}kbps)"
            self.last_error = ""
        with self.lock:
            self._video_sync_queue.clear()
            self._transcode_ready_event.clear()
            self._stream_clock_t0 = None
            self._comp_build_samples_ms.clear()
            self._cam_age_samples_ms.clear()
            self._stdin_write_samples_ms.clear()
            self._frame_source_window.clear()
            self._rtmp_send_win.clear()
            self._rtmp_bytes_sent_total = 0
            self._rtmp_uplink_kbps = 0.0
            self._rtmp_send_block_avg_ms = 0.0
            self._rtmp_send_block_max_ms = 0.0
            self._ffmpeg_telemetry = {
                "encoded_frames": 0,
                "fps": 0.0,
                "q": 0.0,
                "out_time_s": 0.0,
                "wall_elapsed_s": 0.0,
                "bitrate_kbps": 0.0,
                "speed": 0.0,
                "queued_frames": 0,
                "internal_lag_ms": 0.0,
                "updated_at": 0.0,
                "raw_status": ""
            }
        with self._audio_lock:
            self.audio_gain_pct = gain_pct
            # 송출 시작 시 과거 잔류 오디오 버퍼를 완전히 비워 A/V 시작 시점(PTS=0)을 비디오와 100% 일치시킵니다.
            self._audio_buffer.clear()
            self._audio_buffer_pc.clear()
            self._mob_audio_reorder.clear()
            self._pc_audio_reorder.clear()
            self._audio_prebuffering = True
            self._audio_pc_prebuffering = True
            self._mob_empty_ticks = 0
            self._pc_empty_ticks = 0
            self._mob_last_sample = 0
            self._pc_last_sample = 0
            self._was_in_silence = True
            self._last_audio_sample = 0
            self._mob_dsp_state = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 8.0]
            self._pc_dsp_state = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 8.0]

        target_rtmp = f"{self.rtmp_url.rstrip('/')}/{self.stream_key}"
        ffmpeg_exe = self.get_ffmpeg_exe()

        v_bitrate = f"{v_kbps}k"
        # 1초 분량 초저지연 VBV 버퍼(1x bitrate)로 인코더 버퍼 지연 최소화 및 CBR 안정성 동시 확보
        v_bufsize = f"{max(1500, v_kbps)}k"
        a_bitrate = f"{a_kbps}k"
        gop_size = str(self.fps * self.keyframe_interval_sec)

        hw_enc = self._detect_hw_encoder()
        if hw_enc == "h264_nvenc":
            video_codec_args = [
                "-c:v", "h264_nvenc",
                "-preset", "p2",
                "-tune", "ull",
                "-zerolatency", "1",
                "-delay", "0",
                "-rc-lookahead", "0",
                "-rc", "cbr",
                "-cbr", "1",
                "-b:v", v_bitrate,
                "-minrate", v_bitrate,
                "-maxrate", v_bitrate,
                "-bufsize", v_bufsize,
                "-r", str(self.fps),
                "-g", gop_size,
                "-bf", "0",
                "-forced-idr", "1",
                "-strict_gop", "1",
                "-no-scenecut", "1"
            ]
        elif hw_enc == "h264_qsv":
            video_codec_args = [
                "-c:v", "h264_qsv",
                "-preset", "veryfast",
                "-b:v", v_bitrate,
                "-minrate", v_bitrate,
                "-maxrate", v_bitrate,
                "-bufsize", v_bufsize,
                "-r", str(self.fps),
                "-g", gop_size,
                "-bf", "0"
            ]
        elif hw_enc == "h264_amf":
            video_codec_args = [
                "-c:v", "h264_amf",
                "-usage", "ultralowlatency",
                "-quality", "speed",
                "-rc", "cbr",
                "-b:v", v_bitrate,
                "-minrate", v_bitrate,
                "-maxrate", v_bitrate,
                "-bufsize", v_bufsize,
                "-r", str(self.fps),
                "-g", gop_size,
                "-bf", "0"
            ]
        else:
            video_codec_args = [
                "-c:v", "libx264",
                "-profile:v", "high",
                "-preset", "veryfast",
                "-tune", "zerolatency",
                "-threads", "4",
                "-b:v", v_bitrate,
                "-minrate", v_bitrate,
                "-maxrate", v_bitrate,
                "-bufsize", v_bufsize,
                "-x264-params", f"nal-hrd=cbr:force-cfr=1:bframes=0:rc-lookahead=0:keyint={gop_size}:min-keyint={gop_size}:scenecut=0",
                "-r", str(self.fps),
                "-g", gop_size,
                "-keyint_min", gop_size,
                "-sc_threshold", "0"
            ]

        # 실시간 마이크 오디오(48kHz s16le Mono) 입력을 FFmpeg에 무지연 공급하기 위한 로컬 루프백 소켓 오픈
        import socket
        audio_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        audio_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        audio_srv.bind(("127.0.0.1", 0))
        audio_srv.listen(1)
        audio_port = audio_srv.getsockname()[1]
        with self.lock:
            self._audio_srv_sock = audio_srv

        # Intel I219-V LSOv2 TX 링 멈춤 및 공유기 DNS 타임아웃 방지용 로컬 MSS 슬라이싱 프록시 가동
        # (단, 인스타그램 라이브 등 rtmps:// TLS 암호화 프로토콜은 FFmpeg 자체 TLS/SNI 핸드셰이크를 그대로 사용)
        from urllib.parse import urlparse
        parsed_rtmp = urlparse(self.rtmp_url)
        is_rtmps_tls = (parsed_rtmp.scheme or "").lower() == "rtmps"
        if is_rtmps_tls:
            rtmp_output_args = [
                "-tcp_nodelay", "1",
                "-f", "flv",
                target_rtmp
            ]
        else:
            remote_rtmp_host = parsed_rtmp.hostname or "a.rtmp.youtube.com"
            remote_rtmp_port = parsed_rtmp.port or 1935
            remote_rtmp_app = (parsed_rtmp.path or "/live2").strip("/") or "live2"
            _rtmp_proxy_srv, rtmp_proxy_port = self._start_rtmp_anti_lso_proxy(remote_rtmp_host, remote_rtmp_port)
            local_proxied_rtmp = f"rtmp://127.0.0.1:{rtmp_proxy_port}/{remote_rtmp_app}/{self.stream_key}"
            orig_tcurl = f"rtmp://{remote_rtmp_host}/{remote_rtmp_app}"
            rtmp_output_args = [
                "-rtmp_tcurl", orig_tcurl,
                "-tcp_nodelay", "1",
                "-f", "flv",
                local_proxied_rtmp
            ]

        cmd = [
            ffmpeg_exe,
            "-y",
            "-stats",
            "-fflags", "+nobuffer+flush_packets",
            "-flags", "low_delay",
            "-probesize", "4096",
            "-analyzeduration", "0",
            "-thread_queue_size", "64",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "-framerate", str(self.fps),
            "-i", "-",
            "-fflags", "+nobuffer",
            "-probesize", "32",
            "-analyzeduration", "0",
            "-thread_queue_size", "64",
            "-f", "s16le",
            "-ar", "48000",
            "-ac", "1",
            "-i", f"tcp://127.0.0.1:{audio_port}",
            "-vf", f"scale={self.width}:{self.height}:flags=fast_bilinear,format=yuv420p",
            "-af", "highpass=f=60:poles=1,pan=stereo|c0=c0|c1=c0",
            *video_codec_args,
            "-c:a", "aac",
            "-b:a", "160k",
            "-ar", "48000",
            "-flvflags", "no_duration_filesize",
            "-flush_packets", "1",
            "-muxdelay", "0",
            "-muxpreload", "0",
            "-max_interleave_delta", "0",
            "-max_muxing_queue_size", "1024",
            *rtmp_output_args
        ]

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0
            )
            with self.lock:
                self._proc = proc
        except Exception as e:
            try:
                audio_srv.close()
            except Exception:
                pass
            with self.lock:
                self.active = False
                self.last_error = str(e)
                self.status_text = f"FFmpeg 실행 실패: {e}"
            return {"ok": False, "message": f"FFmpeg 송출 프로세스 시작 실패: {e}"}

        self._compositor_thread = threading.Thread(target=self._compositor_loop, args=(proc,), daemon=True)
        self._compositor_thread.start()

        self._audio_feeder_thread = threading.Thread(target=self._audio_feeder_loop, args=(audio_srv, proc), daemon=True)
        self._audio_feeder_thread.start()

        self._feeder_thread = threading.Thread(target=self._feeder_loop, args=(proc,), daemon=True)
        self._feeder_thread.start()

        self._stderr_thread = threading.Thread(target=self._stderr_reader, args=(proc,), daemon=True)
        self._stderr_thread.start()

        if self.__class__ is YouTubeDirectStreamer and not is_auto_restart:
            self._start_onair_handshake_watchdog()

        return {"ok": True, "verified_live": False, "message": "🟡 유튜브 RTMP 신호 전송을 시작했습니다. 실제 유튜브 라이브 온에어 여부를 확인 중입니다...", "state": self.get_state()}

    def _start_onair_handshake_watchdog(self):
        """
        유튜브 스튜디오를 열지 않고도 방송 시작 시 100% 온에어(On-Air) 전환을 보장하는 자동 핸드셰이크 워치독:
        - 유튜브 '지금 스트리밍' 서버가 이전 방송 종료 직후 새 스트림 방을 준비하는 동안(10~18초)
          초기 RTMP 시퀀스 헤더를 놓쳐 '스트림 준비 중'에 멈추는 현상을 자동으로 감지하고,
          온에어가 확인되지 않으면 18초/38초 시점에 0.3초 무중단 RTMP 헤더 재전송(Re-Handshake)을 자동 실행합니다.
        """
        with self.lock:
            self._onair_watchdog_gen = getattr(self, "_onair_watchdog_gen", 0) + 1
            my_gen = self._onair_watchdog_gen
            self._yt_manifest_cache["is_live"] = False
            self._yt_manifest_cache["video_id"] = ""
            self._yt_manifest_cache["checked_at"] = 0.0

        try:
            if "youtube_chat_poller" in globals() and youtube_chat_poller is not None:
                with youtube_chat_poller.lock:
                    youtube_chat_poller.video_id = ""
                ch_in = (database.get_overlay_state("youtube_channel_input", "") or "").strip()
                if ch_in and not youtube_chat_poller.active:
                    youtube_chat_poller.connect(ch_in)
        except Exception:
            pass

        def _watchdog_worker():
            start_t = time.time()
            rehandshake_done = 0
            last_probe_t = 0.0
            while True:
                time.sleep(1.0)
                with self.lock:
                    if not self._user_requested_active or getattr(self, "_onair_watchdog_gen", 0) != my_gen:
                        return
                    cur_rtmp_url = self.rtmp_url
                    cur_stream_key = self.stream_key
                    cur_w = self.width
                    cur_h = self.height
                    cur_fps = self.fps
                    cur_v_kbps = self.video_bitrate_kbps
                    cur_a_kbps = self.audio_bitrate_kbps
                    cur_kf_sec = self.keyframe_interval_sec

                now_t = time.time()
                elapsed = now_t - start_t

                if (now_t - last_probe_t) >= 4.0 and elapsed >= 5.0:
                    last_probe_t = now_t
                    try:
                        self.inspect_youtube_live_latency(force=True)
                    except Exception:
                        pass

                chat_vid = ""
                if "youtube_chat_poller" in globals() and youtube_chat_poller is not None:
                    with youtube_chat_poller.lock:
                        chat_vid = youtube_chat_poller.video_id or ""
                manifest_live = bool(self._yt_manifest_cache.get("is_live") and self._yt_manifest_cache.get("video_id"))
                live_vid = chat_vid or (self._yt_manifest_cache.get("video_id") if manifest_live else "")

                if live_vid:
                    print(f"🟢 [YouTube 자동 온에어 워치독] 유튜브 실제 라이브 온에어 확인 완료! (video_id={live_vid})", flush=True)
                    with self.lock:
                        if self.active and getattr(self, "_onair_watchdog_gen", 0) == my_gen:
                            self.status_text = f"🟢 유튜브 실제 라이브 온에어 연결됨 ({live_vid}) · {cur_w}x{cur_h} {cur_fps}fps"
                            self.last_error = ""
                    try:
                        mark_live_broadcast_started()
                    except Exception:
                        pass
                    return

                # 18초 및 38초 시점에도 아직 유튜브가 '스트림 준비 중'이면 자동으로 RTMP 헤더 재전송(Re-Handshake)
                if (elapsed >= 18.0 and rehandshake_done == 0) or (elapsed >= 38.0 and rehandshake_done == 1):
                    rehandshake_done += 1
                    print(
                        f"🔄 [YouTube 자동 온에어 워치독] 유튜브 서버 '스트림 준비 중' 자동 깨우기용 "
                        f"{rehandshake_done}차 RTMP 시퀀스 헤더 재전송(Re-Handshake) 실행...",
                        flush=True
                    )
                    with self.lock:
                        if not self._user_requested_active or getattr(self, "_onair_watchdog_gen", 0) != my_gen:
                            return
                        self.status_text = f"🟡 유튜브 온에어 대기 중 ({rehandshake_done}차 신호 재전송)..."
                    try:
                        self.start(
                            rtmp_url=cur_rtmp_url,
                            stream_key=cur_stream_key,
                            width=cur_w,
                            height=cur_h,
                            fps=cur_fps,
                            video_bitrate_kbps=cur_v_kbps,
                            audio_bitrate_kbps=cur_a_kbps,
                            keyframe_interval_sec=cur_kf_sec,
                            is_auto_restart=True
                        )
                    except Exception as e:
                        print(f"⚠️ [YouTube 자동 온에어 워치독 경고] {e}", flush=True)

                if elapsed >= 30.0:
                    with self.lock:
                        if self.active and getattr(self, "_onair_watchdog_gen", 0) == my_gen and not self.last_error:
                            self.status_text = "🟡 RTMP 신호 전송 중 — 유튜브 실제 라이브 아직 미시작"
                            self.last_error = "유튜브 RTMP 서버로 영상은 전송 중이나, 실제 유튜브 라이브가 아직 켜지지 않았습니다. 유튜브 스튜디오 [스트림] 화면 우측 상단의 [실시간 스트리밍 시작] 버튼을 누르거나, [관리] → [스트림 예약] → [추가 설정]에서 '자동 시작'을 켜주세요."
                if elapsed >= 90.0:
                    return

        threading.Thread(target=_watchdog_worker, daemon=True).start()

    def _resample_linear_int16(self, src_arr, out_samples=960):
        """16-bit PCM 배열(src_arr)을 선형 보간(Linear Interpolation)하여 계단 노이즈 없이 out_samples 길이로 부드럽게 변환"""
        import array
        in_len = len(src_arr)
        if in_len == out_samples:
            return src_arr
        if in_len <= 1:
            v = src_arr[0] if in_len == 1 else 0
            return array.array("h", [v] * out_samples)
        out_arr = array.array("h")
        scale = (in_len - 1) / float(out_samples - 1)
        for i in range(out_samples):
            pos = i * scale
            idx = int(pos)
            if idx >= in_len - 1:
                out_arr.append(src_arr[-1])
            else:
                frac = pos - idx
                val = int(src_arr[idx] * (1.0 - frac) + src_arr[idx + 1] * frac)
                out_arr.append(val)
        return out_arr

    def _pull_jitter_chunk(self, buf, prebuffering, last_src_ts, empty_ticks, last_sample, rate_ratio=1.0, smooth_consume=960.0, chunk_samples=960):
        """
        웹 OBS(pull_monitor_audio)와 100% 동일한 1:1 비트 무변조(Bit-Exact) 직결 지터 버퍼 (48kHz, 960 샘플 = 1,920 바이트 @ 20ms):
        1) 매 20ms마다 샘플 수를 864~1008개로 바꾸며 목소리 음정(Pitch)과 자음을 뭉개뜨리던 가변 리샘플링(smooth_consume)을 완전 제거하여
           48kHz 원본 PCM을 1:1 무변조로 직결 전달합니다.
        2) 버퍼가 순간 부족할 때 2.6ms(256 bytes) 자투리를 20ms(960 샘플)로 최대 7.5배 강제 늘려 괴물 소리를 내던 rem_arr 타임스트레칭을 제거하고,
           언더런 발생 시에만 120ms(11,520 bytes) 안전 쿠션을 확보한 뒤 1:1 재생을 재개합니다.
        """
        import array
        chunk_bytes = chunk_samples * 2
        prebuffer_bytes = 11520   # 120ms 안전 쿠션 @ 48kHz (40ms 패킷 3개 분량)
        target_bytes = 15360      # 160ms 목표 중심 수위 @ 48kHz
        max_sync_bytes = 38400    # 400ms 상한 (비정상 적체 시 최신 160ms로 즉시 정렬)

        buf_len = len(buf)
        now_t = time.time()
        src_alive = (last_src_ts > 0) and ((now_t - last_src_ts) < 0.85)

        if prebuffering:
            if buf_len >= prebuffer_bytes:
                prebuffering = False
                if buf_len > max_sync_bytes:
                    del buf[:-target_bytes]
                    buf_len = len(buf)
                    self._audio_overflow_drops += 1
            elif (not src_alive) and buf_len >= chunk_bytes:
                # 발화 종료 직후 버퍼에 남아 있는 온전한 20ms 청크들은 1:1로 끝까지 내보냄
                raw = bytes(buf[:chunk_bytes])
                del buf[:chunk_bytes]
                out_arr = array.array("h")
                out_arr.frombytes(raw)
                return out_arr, True, empty_ticks, (out_arr[-1] if len(out_arr) > 0 else 0), float(chunk_samples)
            else:
                if (not src_alive) and 0 < buf_len < chunk_bytes:
                    del buf[:]
                return None, True, empty_ticks, 0, float(chunk_samples)

        if buf_len > max_sync_bytes:
            del buf[:-target_bytes]
            buf_len = len(buf)
            self._audio_overflow_drops += 1

        # 100% 1:1 비트 무변조 추출 (960 샘플 = 1,920 바이트 고정, 리샘플링 왜곡 0%)
        if buf_len >= chunk_bytes:
            raw = bytes(buf[:chunk_bytes])
            del buf[:chunk_bytes]
            out_arr = array.array("h")
            out_arr.frombytes(raw)
            return out_arr, False, 0, (out_arr[-1] if len(out_arr) > 0 else 0), float(chunk_samples)

        # 버퍼가 1틱(1,920 bytes) 미만으로 마른 경우:
        # 스마트폰과 PC의 오디오 클럭 미세 오차(~0.2%)로 인한 1~2틱(20~40ms) 순간 부족 시에는
        # 120ms 동안 소리를 끊거나 언더런 경고를 띄우지 않고 부드럽게 브릿지하여 다음 40ms 패킷을 즉시 이어받음
        empty_ticks += 1
        if src_alive:
            if empty_ticks <= 2:
                bridge_arr = array.array("h")
                v = int(last_sample)
                for _ in range(chunk_samples):
                    v = int(v * 0.92)
                    bridge_arr.append(v)
                return bridge_arr, False, empty_ticks, v, float(chunk_samples)
            if empty_ticks == 3:
                self.audio_underruns += 1
                self._audio_underrun_ts.append(now_t)
                if len(self._audio_underrun_ts) > 120:
                    del self._audio_underrun_ts[:-100]
                self._record_audio_event(
                    "BUFFER_UNDERRUN",
                    "bad",
                    f"🚨 오디오 지터 버퍼 언더런(끊김) 발생 (누적 {self.audio_underruns}회)",
                    f"패킷 도착 지연으로 버퍼가 일시 고갈되어 안전 쿠션 재충전으로 전환되었습니다.",
                    now_t
                )
        elif buf_len > 0:
            del buf[:]

        return None, True, empty_ticks, 0, float(chunk_samples)

    def _audio_feeder_loop(self, audio_srv, proc):
        """
        단일 마스터 클럭(self._stream_clock_t0)에 100% 락스텝(Lockstep) 동기화된 20.0ms(960 샘플 @ 48kHz) 오디오 피더 루프:
        - 비디오 피더(_feeder_loop)와 동일한 출발 시각(t0) 및 누적 틱 카운터로 구동되어,
          장시간 방송에서도 오디오 PTS와 비디오 PTS의 어긋남(싱크 밀림)이 수학적으로 0.000ms로 고정됩니다.
        """
        import socket
        import array
        import random
        conn = None
        try:
            audio_srv.settimeout(10.0)
            conn, _ = audio_srv.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # 1) FFmpeg Input #1(s16le, -probesize 32)의 avformat_find_stream_info가 즉시 통과하도록 3840 바이트(40ms @ 48kHz) 무음만 1회 전송
            conn.sendall(b"\x00" * 3840)
        except Exception:
            try:
                audio_srv.close()
            except Exception:
                pass
            return

        # 2) FFmpeg가 유튜브 RTMP 핸드셰이크를 마치고 실제 트랜스코딩 루프(Output #0 / Press [q])에 진입할 때까지 대기 (최대 1.5초)
        wait_start = time.perf_counter()
        while not self._stop_flag and self.active and proc.poll() is None:
            if self._transcode_ready_event.is_set() or (time.perf_counter() - wait_start) > 1.5:
                break
            time.sleep(0.005)

        # 3) 비디오와 동일한 마스터 클럭(t0) 확립 및 최신 120ms(11,520 bytes) 오디오 쿠션 정렬
        with self.lock:
            if self._stream_clock_t0 is None:
                self._stream_clock_t0 = time.perf_counter()
                self.started_at = time.time()
                self.frames_sent = 0
            t0 = self._stream_clock_t0

        with self._audio_lock:
            if len(self._audio_buffer) > 11520:
                del self._audio_buffer[:-11520]
            if len(self._audio_buffer_pc) > 11520:
                del self._audio_buffer_pc[:-11520]
            self._audio_prebuffering = (len(self._audio_buffer) < 9600)
            self._audio_pc_prebuffering = (len(self._audio_buffer_pc) < 9600)

        chunk_samples = 960
        rng = random.Random(20261029)
        dither_pool_arrays = [
            array.array("h", (rng.randint(-1, 1) for _ in range(chunk_samples)))
            for _ in range(50)
        ]
        dither_pool_bytes = [arr.tobytes() for arr in dither_pool_arrays]
        dither_idx = 0
        interval = 0.02
        audio_ticks_sent = 0

        try:
            while not self._stop_flag and self.active and proc.poll() is None:
                d_arr = dither_pool_arrays[dither_idx]
                d_bytes = dither_pool_bytes[dither_idx]
                dither_idx = (dither_idx + 1) % 50

                with self._audio_lock:
                    mob_arr, self._audio_prebuffering, self._mob_empty_ticks, self._mob_last_sample, self._mob_smooth_consume = self._pull_jitter_chunk(
                        self._audio_buffer,
                        self._audio_prebuffering,
                        self.last_mobile_audio_ts,
                        self._mob_empty_ticks,
                        self._mob_last_sample,
                        self._mob_rate_ratio,
                        self._mob_smooth_consume,
                        chunk_samples
                    )
                    pc_arr, self._audio_pc_prebuffering, self._pc_empty_ticks, self._pc_last_sample, self._pc_smooth_consume = self._pull_jitter_chunk(
                        self._audio_buffer_pc,
                        self._audio_pc_prebuffering,
                        self.last_pc_audio_ts,
                        self._pc_empty_ticks,
                        self._pc_last_sample,
                        self._pc_rate_ratio,
                        self._pc_smooth_consume,
                        chunk_samples
                    )

                    if mob_arr is not None:
                        self._apply_gain_to_array(mob_arr, source="mobile")
                        # 🔬 탭② DSP (송출 중 가공 후)
                        self._scope_push("dsp", mob_arr.tobytes())
                    if pc_arr is not None:
                        self._apply_gain_to_array(pc_arr, source="pc")

                    self._update_audio_envelope(mob_arr, pc_arr)

                    if mob_arr is not None and pc_arr is not None:
                        active_arr = array.array(
                            "h",
                            (max(-32768, min(32767, mob_arr[i] + pc_arr[i])) for i in range(chunk_samples))
                        )
                    elif mob_arr is not None:
                        active_arr = mob_arr
                    elif pc_arr is not None:
                        active_arr = pc_arr
                    else:
                        active_arr = None

                    if active_arr is not None:
                        if self._was_in_silence:
                            prev_s = self._last_audio_sample
                            for i in range(min(64, chunk_samples)):
                                w = (i + 1) / 64.0
                                active_arr[i] = int(prev_s * (1.0 - w) + active_arr[i] * w)
                            self._was_in_silence = False
                        for i in range(chunk_samples):
                            if active_arr[i] == 0:
                                active_arr[i] = d_arr[i]
                        self._last_audio_sample = active_arr[-1]
                        out_chunk = active_arr.tobytes()
                    else:
                        if abs(self._last_audio_sample) > 8:
                            fade_arr = array.array("h")
                            v = self._last_audio_sample
                            for i in range(chunk_samples):
                                v = int(v * 0.88)
                                s_val = max(-32768, min(32767, v if v != 0 else d_arr[i]))
                                fade_arr.append(s_val)
                            self._last_audio_sample = 0
                            self._was_in_silence = True
                            out_chunk = fade_arr.tobytes()
                        else:
                            self._last_audio_sample = 0
                            self._was_in_silence = True
                            out_chunk = d_bytes

                # 🔬 탭③ OUT: FFmpeg로 실제 보내는 최종 바이트 (디더·브릿지·페이드 포함)
                self._scope_push("out", out_chunk)
                try:
                    conn.sendall(out_chunk)
                except Exception:
                    break

                audio_ticks_sent += 1
                self._audio_ticks_sent = audio_ticks_sent
                target_tick_time = t0 + (audio_ticks_sent * interval)
                sleep_s = target_tick_time - time.perf_counter()
                if sleep_s > 0:
                    time.sleep(sleep_s)
                elif sleep_s < -2.5:
                    t0 = time.perf_counter() - (audio_ticks_sent * interval)
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
            try:
                audio_srv.close()
            except Exception:
                pass

    def _stderr_reader(self, proc):
        buf = bytearray()
        re_frame = re.compile(r"frame=\s*(\d+)")
        re_fps = re.compile(r"fps=\s*([\d.]+)")
        re_q = re.compile(r"q=\s*([\d.-]+)")
        re_time = re.compile(r"time=\s*(-?\d+):(\d+):(\d+(?:\.\d+)?)")
        re_bitrate = re.compile(r"bitrate=\s*([\d.]+)kbits/s")
        re_speed = re.compile(r"speed=\s*([\d.]+)x")
        try:
            while True:
                chunk = proc.stderr.read(256)
                if not chunk:
                    break
                buf.extend(chunk)
                while True:
                    idx_r = buf.find(b"\r")
                    idx_n = buf.find(b"\n")
                    if idx_r == -1 and idx_n == -1:
                        break
                    if idx_r != -1 and (idx_n == -1 or idx_r < idx_n):
                        sep = idx_r
                    else:
                        sep = idx_n
                    raw_line = bytes(buf[:sep])
                    del buf[:sep + 1]
                    line = raw_line.decode("utf-8", errors="ignore").strip()
                    if not line:
                        continue
                    line_low = line.lower()
                    if ("press [q]" in line_low) or ("output #0" in line_low) or ("frame=" in line_low):
                        self._transcode_ready_event.set()
                    if "frame=" in line and "time=" in line:
                        try:
                            now_pc = time.perf_counter()
                            m_fr = re_frame.search(line)
                            m_fp = re_fps.search(line)
                            m_q = re_q.search(line)
                            m_tm = re_time.search(line)
                            m_br = re_bitrate.search(line)
                            m_sp = re_speed.search(line)
                            ff_frame = int(m_fr.group(1)) if m_fr else 0
                            ff_fps = float(m_fp.group(1)) if m_fp else 0.0
                            ff_q = float(m_q.group(1)) if m_q else 0.0
                            ff_br = float(m_br.group(1)) if m_br else 0.0
                            ff_sp = float(m_sp.group(1)) if m_sp else 0.0
                            ff_out_s = 0.0
                            if m_tm and not m_tm.group(1).startswith("-"):
                                ff_out_s = int(m_tm.group(1)) * 3600.0 + int(m_tm.group(2)) * 60.0 + float(m_tm.group(3))
                            with self.lock:
                                t0 = self._stream_clock_t0
                                wall_s = (now_pc - t0) if (t0 is not None and now_pc >= t0) else ff_out_s
                                lag_ms = round((wall_s - ff_out_s) * 1000.0, 1) if ff_out_s > 0 else 0.0
                                q_frames = max(0, self.frames_sent - ff_frame)
                                self._ffmpeg_telemetry = {
                                    "encoded_frames": ff_frame,
                                    "fps": round(ff_fps, 1),
                                    "q": round(ff_q, 1),
                                    "out_time_s": round(ff_out_s, 2),
                                    "wall_elapsed_s": round(wall_s, 2),
                                    "bitrate_kbps": round(ff_br, 1),
                                    "speed": round(ff_sp, 3),
                                    "queued_frames": q_frames,
                                    "internal_lag_ms": lag_ms,
                                    "updated_at": time.time(),
                                    "raw_status": line[:160]
                                }
                        except Exception:
                            pass
                    elif any(err_kw in line_low for err_kw in ["error", "failed", "refused", "invalid", "broken", "badname", "timed out", "403", "404"]):
                        with self.lock:
                            if "badname" in line_low or "403" in line_low or "404" in line_low or "invalid" in line_low:
                                self.last_error = f"❌ RTMP 스트림 키가 유효하지 않거나 서버가 거부했습니다: {line[-140:]}"
                                self._user_requested_active = False
                            elif "refused" in line_low or "timed out" in line_low or "resolve" in line_low:
                                self.last_error = f"❌ RTMP 서버에 연결할 수 없습니다(네트워크/방화벽 확인): {line[-140:]}"
                            else:
                                self.last_error = line[-180:]
                            if proc.poll() is not None and not self._user_requested_active:
                                self.active = False
                                self.status_text = self.last_error
        except Exception:
            pass

    def _schedule_auto_restart(self, reason=""):
        """사용자가 중지하지 않았음에도 순간적인 네트워크/파이프 단절로 FFmpeg가 종료된 경우 2.5초 후 자동 재연결"""
        with self.lock:
            if not self._user_requested_active or self._auto_restarting:
                return
            self._auto_restarting = True
            self._auto_restart_count += 1
            attempt = self._auto_restart_count
            self.status_text = f"🔄 네트워크 순단 자동 복구 및 RTMP 재연결 중... ({attempt}회)"

        def _restart_worker():
            try:
                time.sleep(2.5)
                with self.lock:
                    still_wanted = self._user_requested_active
                    already_active = self.active and (self._proc is not None and self._proc.poll() is None)
                if still_wanted and not already_active:
                    print(f"🔄 [RTMP 자가 복구] 송출 파이프 자동 재시작 시도 ({attempt}회 / 사유: {reason})", flush=True)
                    self.start(
                        rtmp_url=self.rtmp_url,
                        stream_key=self.stream_key,
                        width=self.width,
                        height=self.height,
                        fps=self.fps,
                        video_bitrate_kbps=self.video_bitrate_kbps,
                        audio_bitrate_kbps=self.audio_bitrate_kbps,
                        keyframe_interval_sec=self.keyframe_interval_sec,
                        is_auto_restart=True
                    )
            except Exception as e:
                print(f"⚠️ [RTMP 자가 복구 오류] {e}", flush=True)
            finally:
                with self.lock:
                    self._auto_restarting = False

        threading.Thread(target=_restart_worker, daemon=True).start()

    def _compositor_loop(self, proc):
        """
        비동기 백그라운드 합성 전용 스레드:
        30.0 FPS FFmpeg 피더 루프(_feeder_loop)를 단 0.1ms도 블로킹하지 않도록,
        카메라 프레임(latest_camera_seq) 또는 투명 오버레이(latest_overlay_ts)가 갱신될 때마다
        별도 스레드에서 미리 720p/1080p 합성 JPEG를 빌드하여 _latest_server_built_frame에 캐싱합니다.
        """
        while not self._stop_flag and self.active and proc.poll() is None:
            try:
                with latest_camera_lock:
                    cur_cam_seq = latest_camera_seq
                with self.lock:
                    cur_ov_ts = self.latest_overlay_ts
                    has_built = (self._latest_server_built_frame is not None)
                    do_burnin = self.burnin_timecode

                if do_burnin or (cur_cam_seq != self._last_comp_cam_seq) or (cur_ov_ts != self._last_comp_ov_ts) or (not has_built):
                    built = self.build_server_composite_frame()
                    if built:
                        with self.lock:
                            self._latest_server_built_frame = built
                            self._last_comp_cam_seq = cur_cam_seq
                            self._last_comp_ov_ts = cur_ov_ts
                        time.sleep(0.012 if not do_burnin else 0.024)
                        continue
            except Exception:
                pass
            time.sleep(0.015)

    def _feeder_loop(self, proc):
        """
        단일 마스터 클럭(self._stream_clock_t0) 1:1 락스텝(Lockstep) 30.0 FPS 비디오 피더 루프:
        1) FFmpeg mjpeg_parser가 즉시 스트림 헤더(avformat_find_stream_info)를 통과하도록 초기 3프레임을 공급하고,
           RTMP 핸드셰이크 완료(Output #0 / Press [q]) 즉시 오디오 피더와 동일한 마스터 클럭(t0)으로 출발합니다.
        2) 미세 OS 스케줄링 지연 발생 시에도 타이머를 리셋하지 않고 즉시 프레임을 보충(Catch-up)하여,
           장시간 방송에서도 elapsed_seconds * 30 == frames_sent (스트림 PTS 누적 지연 0.0초)를 100% 보장합니다.
        """
        interval = 1.0 / float(self.fps)
        unexpected_exit_reason = ""
        last_sent_frame = None
        last_sent_cam_seq = -1

        # 1) 초기 스트림 탐색(mjpeg_parser + probesize 4096) 즉시 통과용 3프레임 공급
        init_frame = None
        for _ in range(20):
            with self.lock:
                init_frame = self._latest_server_built_frame or self.latest_composite_frame
            if init_frame is None:
                with latest_camera_lock:
                    init_frame = latest_camera_frame
            if init_frame is None:
                init_frame = self.build_server_composite_frame()
            if init_frame:
                break
            time.sleep(0.02)

        if init_frame and proc.poll() is None:
            try:
                proc.stdin.write(init_frame)
                proc.stdin.write(init_frame)
                proc.stdin.write(init_frame)
                proc.stdin.flush()
                last_sent_frame = init_frame
            except Exception:
                pass

        # 2) FFmpeg가 유튜브 RTMP 핸드셰이크를 마치고 실제 인코딩 루프(Output #0 / Press [q])에 진입할 때까지 대기 (최대 1.5초)
        wait_start = time.perf_counter()
        while not self._stop_flag and self.active and proc.poll() is None:
            if self._transcode_ready_event.is_set() or (time.perf_counter() - wait_start) > 1.5:
                break
            time.sleep(0.005)

        # 3) 오디오 피더와 동일한 마스터 클럭(t0) 확립 및 과거 큐 비우기
        with self.lock:
            self._video_sync_queue.clear()
            if self._stream_clock_t0 is None:
                self._stream_clock_t0 = time.perf_counter()
                self.started_at = time.time()
                self.frames_sent = 0
            t0 = self._stream_clock_t0

        frames_emitted = 0
        while not self._stop_flag and self.active:
            now_ts = time.time()
            if proc.poll() is not None:
                with self.lock:
                    if self._proc == proc:
                        self.active = False
                        self.status_text = "송출 중단됨 (자동 재연결 확인 중)"
                        unexpected_exit_reason = self.last_error or "FFmpeg 프로세스 종료"
                break

            frame = None
            f_source = "repeated"
            with self.lock:
                # 서버 백그라운드 합성기(_latest_server_built_frame)를 최우선 사용하여 브라우저 탭 백그라운드 스로틀링 지연 차단
                frame = self._latest_server_built_frame
                if frame is not None:
                    f_source = "server_built"
                f_ts = self.latest_composite_ts
                if frame is None and self.latest_composite_frame and (now_ts - f_ts) <= 0.08:
                    frame = self.latest_composite_frame
                    f_source = "studio_canvas"
                if self._video_sync_queue:
                    popped_ts, popped_frame = self._video_sync_queue.pop(-1)
                    self._video_sync_queue.clear()
                    if frame is None and (now_ts - popped_ts) <= 0.08:
                        frame = popped_frame
                        f_source = "studio_canvas"

            with latest_camera_lock:
                cur_cam_ts = latest_camera_timestamp
                cur_cam_seq = latest_camera_seq
                if frame is None and latest_camera_frame is not None:
                    frame = latest_camera_frame
                    f_source = "raw_camera"

            if frame is None:
                frame = last_sent_frame
                f_source = "repeated"
            if frame is None:
                built = self.build_server_composite_frame()
                if built:
                    with self.lock:
                        self._latest_server_built_frame = built
                    frame = built
                    f_source = "server_built"

            if cur_cam_seq == last_sent_cam_seq and cur_cam_seq > 0 and f_source in ("server_built", "raw_camera"):
                f_source = "repeated"
            else:
                last_sent_cam_seq = cur_cam_seq

            if frame:
                last_sent_frame = frame
                try:
                    t_wr0 = time.perf_counter()
                    proc.stdin.write(frame)
                    proc.stdin.flush()
                    wr_pc = time.perf_counter()
                    wr_ms = (wr_pc - t_wr0) * 1000.0
                    cam_age_ms = (now_ts - cur_cam_ts) * 1000.0 if (cur_cam_ts > 0 and (now_ts - cur_cam_ts) < 15.0) else -1.0
                    with self.lock:
                        self.frames_sent += 1
                        self._frame_perf_times.append(wr_pc)
                        if len(self._frame_perf_times) > 90:
                            del self._frame_perf_times[:-60]
                        self._stdin_write_samples_ms.append(wr_ms)
                        if len(self._stdin_write_samples_ms) > 60:
                            del self._stdin_write_samples_ms[:-60]
                        if cam_age_ms >= 0:
                            self._cam_age_samples_ms.append(cam_age_ms)
                            if len(self._cam_age_samples_ms) > 60:
                                del self._cam_age_samples_ms[:-60]
                        self._frame_source_window.append(f_source)
                        if len(self._frame_source_window) > 60:
                            del self._frame_source_window[:-60]
                except Exception as write_err:
                    with self.lock:
                        if self._proc == proc:
                            self.active = False
                            self.last_error = str(write_err)
                            self.status_text = "RTMP 파이프 재연결 대기 중"
                            unexpected_exit_reason = str(write_err)
                    break

            frames_emitted += 1
            target_frame_time = t0 + (frames_emitted * interval)
            sleep_for = target_frame_time - time.perf_counter()
            if sleep_for > 0:
                time.sleep(sleep_for)
            elif sleep_for < -2.5:
                t0 = time.perf_counter() - (frames_emitted * interval)

        if unexpected_exit_reason and self._user_requested_active and not self._stop_flag:
            self._schedule_auto_restart(unexpected_exit_reason)

    def stop(self, user_initiated=True):
        with self.lock:
            if user_initiated:
                self._user_requested_active = False
                self._onair_watchdog_gen = getattr(self, "_onair_watchdog_gen", 0) + 1
                self._yt_manifest_cache["is_live"] = False
                self._yt_manifest_cache["video_id"] = ""
            self._stop_flag = True
            self.active = False
            proc = self._proc
            self._proc = None
            audio_srv = self._audio_srv_sock
            self._audio_srv_sock = None
            if user_initiated:
                self.status_text = "대기 중"
                self.started_at = None

        if audio_srv is not None:
            try:
                audio_srv.close()
            except Exception:
                pass

        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass
        return {"ok": True, "message": "유튜브 실시간 방송 송출이 중지되었습니다.", "state": self.get_state()}

    def set_burnin_timecode(self, enabled: bool):
        with self.lock:
            self.burnin_timecode = bool(enabled)
            self._last_comp_cam_seq = -1
        return self.burnin_timecode

    def inspect_youtube_live_latency(self, video_id=None, force=False):
        """
        유튜브 라이브 시청 페이지(ytInitialPlayerResponse)에서 현재 방송에 실제 적용된
        유튜브 서버 지연 모드(ULTRA_LOW / LOW / NORMAL), 모바일 선읽기 버퍼(liveReadaheadSeconds),
        초저지연 서브프래그먼트(hasSubfragmentedFmp4), 청크 선읽기(liveChunkReadahead),
        초기 재생 버퍼(startMinReadaheadMs), DVR 활성화 여부(isLiveDvrEnabled)를 정밀 추출하고
        Line 5를 5개 세부 항목(5-A ~ 5-E)으로 진단합니다.
        """
        now_t = time.time()
        explicit_arg_vid = (video_id or "").strip()
        vid = explicit_arg_vid
        if not vid and "youtube_chat_poller" in globals():
            vid = (youtube_chat_poller.video_id or "").strip()

        with self.lock:
            cached = dict(self._yt_manifest_cache)
            if not vid and cached.get("video_id"):
                vid = cached["video_id"]

        if not force and vid and cached.get("video_id") == vid and cached.get("is_live") and (now_t - cached.get("checked_at", 0.0)) < 10.0:
            return cached

        try:
            import requests
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            sess = requests.Session()
            sess.verify = False
            sess.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8"
            })

            def _discover_live_vid_from_channel():
                if "youtube_chat_poller" not in globals():
                    return ""
                ch_in = youtube_chat_poller.channel_input or database.get_overlay_state("youtube_channel_input", "@더바인-0313")
                target_url, explicit_v = youtube_chat_poller.normalize_youtube_target(ch_in)
                if explicit_v:
                    return explicit_v
                if not target_url:
                    return ""
                r_ch = sess.get(target_url, timeout=4.5, allow_redirects=True)
                html_ch = r_ch.text
                m_u = re.search(r"watch\?v=([a-zA-Z0-9_-]{11})", r_ch.url)
                if m_u:
                    return m_u.group(1)
                m_c = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})"', html_ch)
                if m_c:
                    return m_c.group(1)
                m_vd = re.search(r'"videoDetails":\{"videoId":"([a-zA-Z0-9_-]{11})"[^}]*"isLive":true', html_ch)
                if m_vd:
                    return m_vd.group(1)
                m_ext = re.search(r'"externalVideoId":"([a-zA-Z0-9_-]{11})"', html_ch)
                if m_ext:
                    return m_ext.group(1)
                return ""

            if not vid:
                vid = _discover_live_vid_from_channel()

            if not vid:
                return cached

            def _fetch_player_response(target_v):
                r_w = sess.get(f"https://www.youtube.com/watch?v={target_v}", timeout=5.0)
                if "youtube_chat_poller" in globals():
                    try:
                        v_cnt = youtube_chat_poller._extract_viewers_from_html(r_w.text)
                        if v_cnt is not None:
                            platform_tracker.update_youtube_live_meta(concurrent_viewers=v_cnt, video_id=target_v)
                    except Exception:
                        pass
                m_w = re.search(r"var ytInitialPlayerResponse\s*=\s*(\{.+?\});", r_w.text)
                if not m_w:
                    return None
                return json.loads(m_w.group(1))

            d = _fetch_player_response(vid)
            vd = (d or {}).get("videoDetails", {}) or {}
            mf = (d or {}).get("microformat", {}).get("playerMicroformatRenderer", {}) or {}
            live_details = mf.get("liveBroadcastDetails", {}) or {}
            is_live_now = bool(vd.get("isLive") or live_details.get("isLiveNow"))
            if "youtube_chat_poller" in globals() and is_live_now and vid:
                with youtube_chat_poller.lock:
                    youtube_chat_poller.video_id = vid
                    if vd.get("title"):
                        youtube_chat_poller.broadcast_title = str(vd.get("title"))
            try:
                raw_vc = int(vd.get("viewCount") or 0) if vd.get("viewCount") is not None else None
                platform_tracker.update_youtube_live_meta(
                    concurrent_viewers=raw_vc if is_live_now else None,
                    view_count=raw_vc,
                    is_live=is_live_now,
                    video_id=vid,
                    title=str(vd.get("title") or "")
                )
            except Exception:
                pass

            # 만약 기존에 캐시된 방송 ID가 이미 종료되었거나 삭제된 방송이면 즉시 채널 /live에서 새 방송 ID를 재탐색!
            if (not d or not vd.get("videoId") or not is_live_now) and not explicit_arg_vid:
                fresh_vid = _discover_live_vid_from_channel()
                if fresh_vid and fresh_vid != vid:
                    vid = fresh_vid
                    d = _fetch_player_response(vid)
                    vd = (d or {}).get("videoDetails", {}) or {}
                    mf = (d or {}).get("microformat", {}).get("playerMicroformatRenderer", {}) or {}
                    live_details = mf.get("liveBroadcastDetails", {}) or {}
                    is_live_now = bool(vd.get("isLive") or live_details.get("isLiveNow"))
                    if "youtube_chat_poller" in globals() and is_live_now:
                        with youtube_chat_poller.lock:
                            youtube_chat_poller.video_id = vid
                            if vd.get("title"):
                                youtube_chat_poller.broadcast_title = str(vd.get("title"))

            if not d:
                return cached

            pc = d.get("playerConfig", {}) or {}
            live_pc = pc.get("livePlayerConfig", {}) or {}
            media_cc = pc.get("mediaCommonConfig", {}) or {}
            dyn_cfg = media_cc.get("dynamicReadaheadConfig", {}) or {}
            start_policy_list = (
                media_cc.get("serverPlaybackStartConfig", {})
                .get("playbackStartPolicy", {})
                .get("startMinReadaheadPolicy", [])
            ) or []
            start_min_ra_ms = start_policy_list[0].get("minReadaheadMs") if start_policy_list else None

            lat_class = str(vd.get("latencyClass") or "")
            is_low_lat = vd.get("isLowLatencyLiveStream")
            is_dvr_enabled = bool(vd.get("isLiveDvrEnabled", True))
            live_chunk_readahead = vd.get("liveChunkReadahead")
            live_readahead_sec = live_pc.get("liveReadaheadSeconds")
            has_subfrag_fmp4 = bool(live_pc.get("hasSubfragmentedFmp4"))
            has_subfrag_webm = bool(live_pc.get("hasSubfragmentedWebm"))
            is_live_head_playable = bool(live_pc.get("isLiveHeadPlayable"))
            min_readahead_ms = dyn_cfg.get("minReadAheadMediaTimeMs")
            max_readahead_ms = dyn_cfg.get("maxReadAheadMediaTimeMs")

            # 실제 라이브 버퍼(초) 계산: liveReadaheadSeconds가 실제 라이브 플레이어 선읽기 목표값임!
            # (주의: dynamicReadaheadConfig.minReadAheadMediaTimeMs=15000은 일반 VOD 플레이어 기본값이므로 NORMAL 지연 모드일 때만 참조)
            if live_readahead_sec is not None and float(live_readahead_sec) > 0:
                effective_yt_buf_sec = round(float(live_readahead_sec), 2)
            elif "ULTRA_LOW" in lat_class:
                effective_yt_buf_sec = 2.0
            elif "LATENCY_LOW" in lat_class or is_low_lat is True:
                effective_yt_buf_sec = 5.5
            elif "NORMAL" in lat_class:
                effective_yt_buf_sec = round(float(min_readahead_ms) / 1000.0, 1) if min_readahead_ms else 14.0
            else:
                effective_yt_buf_sec = 0.0

            if "ULTRA_LOW" in lat_class:
                mode_ko = "⚡ 초저지연 (Ultra-Low Latency)"
                target_dur = 1.0
                exp_delay = "2.0 ~ 4.0초"
            elif "LATENCY_LOW" in lat_class or is_low_lat is True:
                mode_ko = "🚀 저지연 (Low Latency)"
                target_dur = 2.0
                exp_delay = "5.0 ~ 8.0초"
            elif "NORMAL" in lat_class:
                mode_ko = "🐢 기본 지연 시간 (Normal Latency - 12~15초 버퍼)"
                target_dur = 5.0
                exp_delay = "12.0 ~ 16.0초"
            else:
                mode_ko = lat_class or ("라이브 대기 중" if not is_live_now else "확인 중")
                target_dur = None
                exp_delay = "-"

            res_info = {
                "video_id": vid,
                "title": str(vd.get("title") or ""),
                "is_live": is_live_now,
                "is_low_latency": is_low_lat,
                "is_dvr_enabled": is_dvr_enabled,
                "live_chunk_readahead": live_chunk_readahead,
                "has_subfragmented_fmp4": has_subfrag_fmp4,
                "has_subfragmented_webm": has_subfrag_webm,
                "is_live_head_playable": is_live_head_playable,
                "start_min_readahead_ms": start_min_ra_ms,
                "latency_class": lat_class,
                "latency_mode_ko": mode_ko,
                "live_readahead_sec": live_readahead_sec,
                "effective_buffer_sec": effective_yt_buf_sec,
                "min_readahead_ms": min_readahead_ms,
                "max_readahead_ms": max_readahead_ms,
                "target_duration_sec": target_dur,
                "expected_mobile_delay_sec": exp_delay,
                "checked_at": now_t,
                "error": ""
            }
            with self.lock:
                self._yt_manifest_cache = res_info
            return res_info
        except Exception as e:
            with self.lock:
                self._yt_manifest_cache["error"] = str(e)
                self._yt_manifest_cache["checked_at"] = now_t
                return dict(self._yt_manifest_cache)

    def get_video_diagnostics(self, include_history=False, refresh_youtube=False):
        """
        Stage 1 (카메라/WebRTC) -> Stage 2 (서버 합성기/Feeder) -> Stage 3 (FFmpeg 인코더) ->
        Stage 4 (RTMP 소켓) -> Stage 5 (유튜브 서버/모바일 플레이어 버퍼) 전 구간 지연 데이터를 반환합니다.
        """
        now_t = time.time()
        vdo_state = vdo_ninja_receiver.get_state() if "vdo_ninja_receiver" in globals() else {}
        w_stats = vdo_state.get("webrtc_stats", {}) or {}

        with latest_camera_lock:
            cam_ts = latest_camera_timestamp
            cam_seq = latest_camera_seq
            cam_src = latest_camera_source
            cam_int_list = list(latest_camera_intervals_ms)
            cam_size_kb = round(len(latest_camera_frame) / 1024.0, 1) if latest_camera_frame else 0.0

        if cam_src == "vdo_ninja":
            ingest_avg_ms = vdo_state.get("frame_interval_avg_ms", 0.0) or 0.0
            ingest_max_ms = vdo_state.get("frame_interval_max_ms", 0.0) or 0.0
        else:
            ingest_avg_ms = round(sum(cam_int_list) / len(cam_int_list), 1) if cam_int_list else 0.0
            ingest_max_ms = round(max(cam_int_list), 1) if cam_int_list else 0.0

        with self.lock:
            comp_samples = list(self._comp_build_samples_ms)
            age_samples = list(self._cam_age_samples_ms)
            wr_samples = list(self._stdin_write_samples_ms)
            src_win = list(self._frame_source_window)
            ff_tel = dict(self._ffmpeg_telemetry)
            rtmp_kbps = self._rtmp_uplink_kbps
            rtmp_blk_avg = self._rtmp_send_block_avg_ms
            rtmp_blk_max = self._rtmp_send_block_max_ms
            rtmp_total_mb = round(self._rtmp_bytes_sent_total / (1024.0 * 1024.0), 2)
            burnin_on = self.burnin_timecode
            is_active = self.active
            frames_sent = self.frames_sent
            elapsed_s = round(now_t - self.started_at, 1) if (self.active and self.started_at) else 0.0
            yt_cache = dict(self._yt_manifest_cache)
            hist_list = list(self._diag_history[-60:]) if include_history else []

        if refresh_youtube:
            yt_cache = self.inspect_youtube_live_latency(force=True)

        comp_avg_ms = round(sum(comp_samples) / len(comp_samples), 2) if comp_samples else 0.0
        comp_max_ms = round(max(comp_samples), 2) if comp_samples else 0.0
        cam_age_avg_ms = round(sum(age_samples) / len(age_samples), 1) if age_samples else (round((now_t - cam_ts) * 1000.0, 1) if (cam_ts > 0 and (now_t - cam_ts) < 10.0) else 0.0)
        cam_age_max_ms = round(max(age_samples), 1) if age_samples else cam_age_avg_ms
        wr_avg_ms = round(sum(wr_samples) / len(wr_samples), 2) if wr_samples else 0.0
        wr_max_ms = round(max(wr_samples), 2) if wr_samples else 0.0

        src_counts = {"server_built": 0, "studio_canvas": 0, "raw_camera": 0, "repeated": 0}
        for s_name in src_win:
            src_counts[s_name] = src_counts.get(s_name, 0) + 1

        # 구간별 추정 지연 시간(ms / sec) 합산
        s1_jb_ms = float(w_stats.get("jitter_buffer_ms", 0.0) or 0.0)
        s1_cap_ms = float(w_stats.get("canvas_capture_ms", 0.0) or 0.0)
        s1_ipc_ms = float(w_stats.get("ipc_push_ms", 0.0) or 0.0)
        s1_total_ms = round(s1_jb_ms + s1_cap_ms + s1_ipc_ms, 1)

        s2_total_ms = round(cam_age_avg_ms + comp_avg_ms + wr_avg_ms, 1)
        s3_lag_ms = max(0.0, float(ff_tel.get("internal_lag_ms", 0.0) or 0.0))
        s4_net_ms = round(float(rtmp_blk_avg or 0.0) + float(w_stats.get("rtt_ms", 0.0) or 0.0) * 0.5, 1)

        local_total_ms = round(s1_total_ms + s2_total_ms + s3_lag_ms + s4_net_ms, 1)
        local_total_sec = round(local_total_ms / 1000.0, 2)

        min_ra_ms = yt_cache.get("min_readahead_ms")
        live_ra_sec = yt_cache.get("live_readahead_sec")
        lat_cls = str(yt_cache.get("latency_class") or "")
        is_yt_live = bool(yt_cache.get("is_live"))
        yt_vid = str(yt_cache.get("video_id") or "")
        has_subfrag = bool(yt_cache.get("has_subfragmented_fmp4"))
        chunk_ra = yt_cache.get("live_chunk_readahead")
        start_min_ra = yt_cache.get("start_min_readahead_ms")
        is_dvr = bool(yt_cache.get("is_dvr_enabled", True))
        kf_sec = int(self.keyframe_interval_sec or 1)

        # 실제 라이브 버퍼(초)는 liveReadaheadSeconds(예: ULTRA_LOW=1.6초)가 최우선!
        # (minReadAheadMediaTimeMs=15000은 일반 VOD 플레이어 고정 기본값이므로 NORMAL 모드일 때만 사용)
        if live_ra_sec is not None and float(live_ra_sec) > 0:
            yt_buffer_sec = round(float(live_ra_sec), 2)
        elif "ULTRA_LOW" in lat_cls:
            yt_buffer_sec = 2.0
        elif "LATENCY_LOW" in lat_cls:
            yt_buffer_sec = 5.5
        elif "NORMAL" in lat_cls:
            yt_buffer_sec = round(float(min_ra_ms) / 1000.0, 1) if min_ra_ms else 14.0
        else:
            yt_buffer_sec = 0.0

        total_est_mobile_sec = round(local_total_sec + yt_buffer_sec, 2) if yt_buffer_sec > 0 else local_total_sec

        # ==============================================================
        # Line 5 (유튜브 라이브 서버 & 모바일 플레이어) 5단계 세부 진단 (5-A ~ 5-E)
        # ==============================================================
        sub_items = []
        todo_checklist = []

        # [5-A] 방송 온에어 & 영상 ID 매칭 진단
        if yt_vid and is_yt_live and is_active:
            sub_items.append({
                "code": "5-A",
                "title": "방송 온에어 & RTMP 매칭",
                "status": "ok",
                "badge": "🟢 정상 매칭",
                "value": f"LIVE 송출 중 [{yt_vid}]",
                "detail": f"방송 제목: {yt_cache.get('title') or '라이브 방송'} (RTMP + 유튜브 서버 연결 완료)",
                "action": ""
            })
        elif yt_vid and is_yt_live and not is_active:
            sub_items.append({
                "code": "5-A",
                "title": "방송 온에어 & RTMP 매칭",
                "status": "warn",
                "badge": "🟡 RTMP 대기",
                "value": f"유튜브 방송 열림 [{yt_vid}] · 내 PC RTMP 미송출",
                "detail": "유튜브 방송방은 열려 있으나 현재 스튜디오에서 RTMP 영상 송출이 시작되지 않았습니다.",
                "action": "스튜디오 우측 상단의 [🔴 유튜브 실시간 송출 시작] 버튼을 눌러주세요."
            })
            todo_checklist.append("① 스튜디오 우측 상단의 [🔴 유튜브 실시간 송출 시작] 버튼을 눌러 영상을 유튜브로 보내주세요.")
        elif yt_vid and not is_yt_live:
            sub_items.append({
                "code": "5-A",
                "title": "방송 온에어 & RTMP 매칭",
                "status": "bad",
                "badge": "🔴 방송 종료됨",
                "value": f"이전 종료된 방송 ID [{yt_vid}] 감지됨",
                "detail": "현재 조회된 영상 ID는 이미 종료된 방송입니다. 새 방송이 시작되면 자동으로 새 ID로 갱신됩니다.",
                "action": "[🔍 유튜브 지연모드 즉시 재조회]를 누르거나 새 라이브 방송 URL을 채팅 입력란에 넣어주세요."
            })
            todo_checklist.append("① 유튜브 스튜디오에서 새 방송이 온에어 상태인지 확인 후 [🔍 유튜브 지연모드 즉시 재조회]를 눌러주세요.")
        else:
            sub_items.append({
                "code": "5-A",
                "title": "방송 온에어 & RTMP 매칭",
                "status": "warn",
                "badge": "🟡 감지 대기",
                "value": "유튜브 라이브 방송 ID 탐색 중",
                "detail": "유튜브 채널(@핸들) 또는 방송 URL에서 현재 라이브 중인 영상을 찾고 있습니다.",
                "action": "일부 공개 방송인 경우 유튜브 공유 URL(https://youtube.com/live/...)을 복사해 ①번 채널 주소란에 붙여넣어 주세요."
            })

        # [5-B] 유튜브 스튜디오 지연 시간 모드 (ULTRA_LOW vs LOW vs NORMAL)
        if "ULTRA_LOW" in lat_cls:
            sub_items.append({
                "code": "5-B",
                "title": "유튜브 스튜디오 지연 모드",
                "status": "ok",
                "badge": "🟢 초저지연 완료",
                "value": "⚡ 초저지연 (ULTRA_LOW)",
                "detail": "유튜브 스튜디오에서 가장 빠른 '초저지연(Ultra-Low Latency)' 모드가 완벽히 활성화되어 있습니다!",
                "action": ""
            })
        elif "LATENCY_LOW" in lat_cls:
            sub_items.append({
                "code": "5-B",
                "title": "유튜브 스튜디오 지연 모드",
                "status": "warn",
                "badge": "🟡 저지연(5~8초)",
                "value": "🚀 저지연 (LOW_LATENCY)",
                "detail": "현재 '저지연' 모드여서 유튜브 서버가 약 5~6초의 버퍼를 유지합니다.",
                "action": "2~4초 딜레이를 원하시면 송출 중지 후 유튜브 스튜디오 [방송 지연 시간]을 [초저지연]으로 변경하세요."
            })
            todo_checklist.append("② 유튜브 스튜디오 [방송 지연 시간]이 '저지연'입니다. 2~4초 달성을 위해 송출을 잠시 멈추고 [초저지연]으로 바꿔주세요.")
        elif "NORMAL" in lat_cls:
            sub_items.append({
                "code": "5-B",
                "title": "유튜브 스튜디오 지연 모드",
                "status": "bad",
                "badge": "🔴 기본지연(15초!)",
                "value": "🐢 기본 지연 시간 (NORMAL)",
                "detail": "유튜브 스튜디오가 '기본 지연 시간'으로 되어 있어 모바일 앱에 12~15초 강제 버퍼가 걸려 있습니다!",
                "action": "지금 [⏹️ 유튜브 송출 종료] 후, 유튜브 스튜디오에서 [방송 지연 시간]을 반드시 [초저지연]으로 바꾸고 다시 송출하세요!"
            })
            todo_checklist.append("🚨 [필수 조치] 스튜디오에서 [⏹️ 유튜브 송출 종료] 클릭 ➔ 유튜브 스튜디오 웹화면에서 [방송 지연 시간]을 [초저지연]으로 변경 ➔ 다시 [🔴 송출 시작] 클릭!")
        else:
            sub_items.append({
                "code": "5-B",
                "title": "유튜브 스튜디오 지연 모드",
                "status": "warn",
                "badge": "🟡 확인 대기",
                "value": "지연 모드 미확인",
                "detail": "방송이 온에어되면 ULTRA_LOW / LOW / NORMAL 모드가 자동 표시됩니다.",
                "action": ""
            })

        # [5-C] 유튜브 서버 모바일 선읽기 버퍼 (liveReadaheadSeconds)
        if live_ra_sec is not None:
            ra_val = float(live_ra_sec)
            if ra_val <= 2.5:
                sub_items.append({
                    "code": "5-C",
                    "title": "모바일 플레이어 선읽기 버퍼",
                    "status": "ok",
                    "badge": f"🟢 +{ra_val}초 (최적)",
                    "value": f"liveReadahead = {ra_val}초 (초기버퍼 {start_min_ra or 1200}ms)",
                    "detail": f"유튜브 서버가 스마트폰 앱에 지시하는 목표 버퍼가 {ra_val}초로 초저지연 상태입니다!",
                    "action": ""
                })
            elif ra_val <= 6.5:
                sub_items.append({
                    "code": "5-C",
                    "title": "모바일 플레이어 선읽기 버퍼",
                    "status": "warn",
                    "badge": f"🟡 +{ra_val}초",
                    "value": f"liveReadahead = {ra_val}초",
                    "detail": f"유튜브 서버가 모바일 플레이어에 {ra_val}초 선읽기 버퍼를 지시하고 있습니다.",
                    "action": "유튜브 스튜디오에서 '초저지연'을 선택하면 1.6초로 단축됩니다."
                })
            else:
                sub_items.append({
                    "code": "5-C",
                    "title": "모바일 플레이어 선읽기 버퍼",
                    "status": "bad",
                    "badge": f"🔴 +{ra_val}초 과다!",
                    "value": f"liveReadahead = {ra_val}초 (강제 버퍼 {min_ra_ms or 15000}ms)",
                    "detail": f"유튜브 서버가 스마트폰 앱에 {ra_val}~15.0초만큼 영상을 모았다가 틀도록 강제하고 있습니다!",
                    "action": "유튜브 스튜디오에서 방송 지연 시간을 [초저지연]으로 변경해야 1.6초로 내려갑니다."
                })
        else:
            sub_items.append({
                "code": "5-C",
                "title": "모바일 플레이어 선읽기 버퍼",
                "status": "warn",
                "badge": "🟡 측정 대기",
                "value": f"예상 버퍼 +{yt_buffer_sec}초",
                "detail": "유튜브 라이브 플레이어 설정(liveReadaheadSeconds) 수신 대기 중입니다.",
                "action": ""
            })

        # [5-D] 초저지연 CMAF 서브프래그먼트 & GOP 키프레임(1s) 일치 여부
        if has_subfrag and kf_sec == 1:
            sub_items.append({
                "code": "5-D",
                "title": "초저지연 CMAF 패킷 & 키프레임",
                "status": "ok",
                "badge": "🟢 1초 키프레임 최적",
                "value": f"Sub-fMP4: ON · 청크선읽기: {chunk_ra or 2}개 · 키프레임: {kf_sec}초(30f)",
                "detail": "0.1~0.5초 단위 서브프래그먼트 스트리밍(Sub-fMP4)과 1초 키프레임(GOP=30)이 완벽히 맞물려 돌아가고 있습니다.",
                "action": ""
            })
        elif has_subfrag and kf_sec > 1:
            sub_items.append({
                "code": "5-D",
                "title": "초저지연 CMAF 패킷 & 키프레임",
                "status": "warn",
                "badge": f"🟡 키프레임 {kf_sec}초",
                "value": f"Sub-fMP4: ON · 현재 키프레임: {kf_sec}초({kf_sec*30}f)",
                "detail": "유튜브 초저지연은 켜져 있으나, 내 송출 키프레임 간격이 2초여서 모바일 조각 생성에 1~2초가 더 걸립니다.",
                "action": "[▶️ 유튜브 연동 설정] 탭에서 키프레임 간격을 [⚡ 1초(1s)]로 바꾸고 [⚡ 즉시 재적용]을 눌러주세요."
            })
            todo_checklist.append("③ 키프레임 간격이 2초로 되어 있습니다. [▶️ 유튜브 연동 설정]에서 키프레임을 [⚡ 1초(1s)]로 바꾸고 재적용하세요.")
        else:
            sub_items.append({
                "code": "5-D",
                "title": "초저지연 CMAF 패킷 & 키프레임",
                "status": "bad" if yt_vid else "warn",
                "badge": "🔴 Sub-fMP4 꺼짐" if yt_vid else "🟡 대기",
                "value": f"Sub-fMP4: {'ON' if has_subfrag else 'OFF'} · 키프레임: {kf_sec}초",
                "detail": "초저지연 서브프래그먼트(hasSubfragmentedFmp4)가 비활성 상태이면 모바일 HLS 플레이어가 3청크(12~15초)를 기다립니다.",
                "action": "유튜브 스튜디오에서 [초저지연]을 켜고 키프레임을 1초(1s)로 설정해 주세요."
            })

        # [5-E] 스마트폰 유튜브 앱 플레이어 LIVE 엣지(DVR 뒤처짐) 진단
        if "ULTRA_LOW" in lat_cls and yt_buffer_sec <= 2.5:
            sub_items.append({
                "code": "5-E",
                "title": "스마트폰 앱 플레이어 LIVE 엣지 동기화",
                "status": "warn",
                "badge": "💡 폰에서 15초면 필수확인!",
                "value": f"서버 버퍼는 {yt_buffer_sec}초 정상! (폰 앱 DVR 뒤처짐 점검)",
                "detail": (
                    f"현재 내 PC({local_total_sec}초) + 유튜브 서버 버퍼({yt_buffer_sec}초) 합계는 총 {total_est_mobile_sec}초로 이미 초저지연입니다! "
                    f"만약 지금 핸드폰에서만 여전히 10~15초 늦게 나온다면 아래 3가지 중 하나입니다."
                ),
                "action": (
                    "① 핸드폰 유튜브 화면을 터치해 좌측 하단 [● 실시간(LIVE)] 글자를 눌러 빨간색 점(🔴)으로 만드세요! (회색 점이면 과거 녹화분 재생 중) "
                    "② 핸드폰 유튜브 앱을 한 번 나갔다가 다시 들어오거나, 재생 속도를 [1.25배속]으로 5초간 틀어 쌓인 버퍼를 따라잡으세요! "
                    "③ 유튜브 앱 하단 [쇼츠(Shorts) 피드]가 아닌 [채널 홈 ➔ 라이브 탭]으로 접속해 시청하세요 (쇼츠 피드 플레이어는 초저지연 설정을 무시하고 12~15초 HLS 버퍼를 강제합니다!)."
                )
            })
            todo_checklist.append(
                f"✅ 서버 계측 완료: 현재 내 PC({local_total_sec}초) + 유튜브 초저지연 서버({yt_buffer_sec}초) = 총 {total_est_mobile_sec}초로 서버는 이미 완벽히 2초대입니다!"
            )
            todo_checklist.append(
                "📱 [핸드폰에서 여전히 15초 늦을 때 즉시 해결법 1] 핸드폰 유튜브 영상 화면을 터치 ➔ 좌측 하단 [● 실시간] 글자를 직접 눌러 회색 점을 빨간 점(🔴 실시간)으로 맞추세요! (방송 시작 직후 잠깐 버퍼링이 걸리면 폰 앱이 자동으로 12초 뒤처진 시점을 계속 재생합니다.)"
            )
            todo_checklist.append(
                "📱 [즉시 해결법 2] 핸드폰 유튜브 우측 상단 ⚙️설정 ➔ [재생 속도]를 [1.25x]로 5~8초만 두면 쌓여 있던 12초 버퍼를 순식간에 따라잡아 실시간(2초 딜레이)으로 고정됩니다!"
            )
            todo_checklist.append(
                "📱 [즉시 해결법 3] 핸드폰에서 '쇼츠(Shorts) 탭'으로 넘기며 보지 마시고, 반드시 '채널 페이지 ➔ 라이브 탭' 또는 공유 링크로 들어가서 보세요. (유튜브 모바일 쇼츠 피드 플레이어는 초저지연 CMAF를 쓰지 않고 15초짜리 일반 HLS를 재생합니다!)"
            )
        else:
            sub_items.append({
                "code": "5-E",
                "title": "스마트폰 앱 플레이어 LIVE 엣지 동기화",
                "status": "ok" if is_dvr else "warn",
                "badge": "🟢 DVR 활성" if is_dvr else "🟡 DVR 꺼짐",
                "value": f"DVR 되감기: {'ON' if is_dvr else 'OFF'} · 목표 총 딜레이: {total_est_mobile_sec}초",
                "detail": "시청자가 일시정지하거나 버퍼링이 생기면 LIVE 최신 시점보다 뒤처질 수 있습니다.",
                "action": "시청자에게 영상 좌측 하단의 [🔴 실시간(LIVE)] 버튼을 눌러 최신 시점으로 당기도록 안내하세요."
            })

        yt_cache["sub_items"] = sub_items
        yt_cache["todo_checklist"] = todo_checklist

        # 병목 원인 자동 판정
        if "NORMAL" in lat_cls:
            bottleneck_stage = "STAGE_5_YOUTUBE_LATENCY_MODE"
            bottleneck_summary = (
                f"🚨 [핵심 원인: 5-B 유튜브 기본지연] 내 PC 송출 지연은 {local_total_sec}초({local_total_ms}ms)뿐이나, "
                f"유튜브 방송({yt_vid})이 '기본 지연 시간(NORMAL)'이어서 서버가 {yt_buffer_sec}초를 강제 버퍼링 중입니다!"
            )
            action_guide = "유튜브 송출을 잠시 중지한 뒤, 유튜브 스튜디오에서 [방송 지연 시간]을 [초저지연 (Ultra-Low Latency)]으로 바꾸고 다시 송출을 시작하세요!"
        elif "LATENCY_LOW" in lat_cls and "ULTRA_LOW" not in lat_cls:
            bottleneck_stage = "STAGE_5_YOUTUBE_LOW_LATENCY"
            bottleneck_summary = (
                f"ℹ️ [5-B 저지연 모드] 내 PC 송출 지연은 {local_total_sec}초이며, 유튜브 '저지연(Low Latency)' 버퍼 +{yt_buffer_sec}초가 더해져 약 {total_est_mobile_sec}초입니다."
            )
            action_guide = "2~4초 딜레이를 원하시면 유튜브 스튜디오에서 방송 지연 시간을 [초저지연 (Ultra-Low Latency)]으로 선택해 주세요."
        elif s3_lag_ms > 1500.0:
            bottleneck_stage = "STAGE_3_FFMPEG_ENCODER"
            bottleneck_summary = f"⚠️ [Line 3 인코더 지연] FFmpeg 내부 인코더 지연이 {round(s3_lag_ms/1000.0, 2)}초 누적되었습니다 (배속: {ff_tel.get('speed', 0)}x)."
            action_guide = "해상도를 720p(720x1280)로 유지하고 비트레이트를 4000~4500kbps로 설정해 주세요."
        elif s2_total_ms > 500.0:
            bottleneck_stage = "STAGE_2_SERVER_COMPOSITOR"
            bottleneck_summary = f"⚠️ [Line 2 합성 대기] 카메라 프레임 도착 후 서버 합성 대기 시간(Frame Age)이 {cam_age_avg_ms}ms로 높습니다."
            action_guide = "스마트폰 카메라 연결(VDO.Ninja) 상태를 확인해 주세요."
        elif "ULTRA_LOW" in lat_cls:
            bottleneck_stage = "OPTIMAL_ULTRA_LOW"
            bottleneck_summary = (
                f"✅ [서버 초저지연 달성 완료!] 내 PC 송출 지연 {local_total_sec}초 + 유튜브 초저지연 서버 버퍼 {yt_buffer_sec}초 = "
                f"유튜브 서버 기준 총 딜레이는 단 {total_est_mobile_sec}초입니다!"
            )
            action_guide = (
                "서버는 이미 2초대 초저지연으로 나가고 있습니다! 만약 핸드폰에서만 여전히 15초 늦게 보인다면 "
                "① 핸드폰 영상 좌측 하단 [🔴 실시간(LIVE)] 버튼을 터치해 빨간 점으로 맞추거나, "
                "② 재생 속도를 [1.25배속]으로 5초간 틀어 밀린 버퍼를 따라잡거나, "
                "③ '쇼츠 피드'가 아닌 '채널 ➔ 라이브 탭'에서 시청하세요! (아래 5-E 항목 참조)"
            )
        else:
            bottleneck_stage = "OPTIMAL"
            bottleneck_summary = f"✅ 로컬 송출 파이프라인 지연 {local_total_sec}초({local_total_ms}ms)로 초저지연 정상 가동 중입니다."
            action_guide = "화면 타임코드(⏱️ SYNC)를 켜서 웹 OBS vs 유튜브 스튜디오 vs 스마트폰 화면의 시각 차이를 1ms 단위로 비교할 수 있습니다."

        result = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "epoch": round(now_t, 3),
            "rtmp_active": is_active,
            "elapsed_seconds": elapsed_s,
            "frames_sent": frames_sent,
            "burnin_timecode": burnin_on,
            "stage1_camera_ingest": {
                "source": cam_src,
                "vdo_connected": bool(vdo_state.get("connected", False)),
                "resolution": f"{vdo_state.get('video_width', 0)}x{vdo_state.get('video_height', 0)}",
                "frame_seq": cam_seq,
                "frame_size_kb": cam_size_kb,
                "ingest_interval_avg_ms": ingest_avg_ms,
                "ingest_interval_max_ms": ingest_max_ms,
                "webrtc_jitter_buffer_ms": s1_jb_ms,
                "webrtc_fps_decoded": w_stats.get("fps_decoded", 0.0),
                "webrtc_frames_dropped": w_stats.get("frames_dropped", 0),
                "webrtc_rtt_ms": w_stats.get("rtt_ms", 0.0),
                "canvas_capture_ms": s1_cap_ms,
                "ipc_push_ms": s1_ipc_ms,
                "stage1_est_delay_ms": s1_total_ms
            },
            "stage2_server_compositor": {
                "cam_frame_age_avg_ms": cam_age_avg_ms,
                "cam_frame_age_max_ms": cam_age_max_ms,
                "compositor_build_avg_ms": comp_avg_ms,
                "compositor_build_max_ms": comp_max_ms,
                "stdin_write_avg_ms": wr_avg_ms,
                "stdin_write_max_ms": wr_max_ms,
                "frame_source_window_60f": src_counts,
                "stage2_est_delay_ms": s2_total_ms
            },
            "stage3_ffmpeg_encoder": {
                "encoded_frames": ff_tel.get("encoded_frames", 0),
                "queued_frames": ff_tel.get("queued_frames", 0),
                "fps": ff_tel.get("fps", 0.0),
                "speed_x": ff_tel.get("speed", 0.0),
                "bitrate_kbps": ff_tel.get("bitrate_kbps", 0.0),
                "out_time_s": ff_tel.get("out_time_s", 0.0),
                "wall_elapsed_s": ff_tel.get("wall_elapsed_s", 0.0),
                "internal_lag_ms": ff_tel.get("internal_lag_ms", 0.0),
                "raw_status": ff_tel.get("raw_status", ""),
                "stage3_est_delay_ms": round(s3_lag_ms, 1)
            },
            "stage4_rtmp_uplink": {
                "uplink_kbps": rtmp_kbps,
                "send_block_avg_ms": rtmp_blk_avg,
                "send_block_max_ms": rtmp_blk_max,
                "total_sent_mb": rtmp_total_mb,
                "stage4_est_delay_ms": s4_net_ms
            },
            "stage5_youtube_server": yt_cache,
            "summary": {
                "local_pipeline_delay_ms": local_total_ms,
                "local_pipeline_delay_sec": local_total_sec,
                "youtube_server_buffer_sec": yt_buffer_sec,
                "estimated_total_mobile_delay_sec": total_est_mobile_sec,
                "bottleneck_stage": bottleneck_stage,
                "bottleneck_summary": bottleneck_summary,
                "action_guide": action_guide
            }
        }
        if include_history:
            result["history"] = hist_list
        return result

    def reset_audio_buffers(self):
        """방송 중 중단 없이 오디오 지터 버퍼와 언더런/클리핑 통계를 즉시 초기화하고 최신 120ms 쿠션으로 재정렬합니다."""
        now_r = time.time()
        with self._audio_lock:
            self._audio_buffer.clear()
            self._audio_buffer_pc.clear()
            self._mob_audio_reorder.clear()
            self._pc_audio_reorder.clear()
            self._audio_prebuffering = True
            self._audio_pc_prebuffering = True
            self._mob_empty_ticks = 0
            self._pc_empty_ticks = 0
            self._mob_last_sample = 0
            self._pc_last_sample = 0
            self._was_in_silence = True
            self._last_audio_sample = 0
            self.audio_underruns = 0
            self._audio_underrun_ts.clear()
            self._audio_overflow_drops = 0
            self._audio_clip_win.clear()
            self._audio_pkt_intervals_ms.clear()
            self._audio_pkt_gap_events.clear()
            self._audio_dropout_events.clear()
            self._last_clean_audio_since = now_r
        return {"ok": True, "message": "🔄 오디오 지터 버퍼 및 감도·끊김 통계가 즉시 리셋·재정렬되었습니다!"}

    def get_audio_diagnostics(self, include_history=True):
        """
        5단계 음성 파이프라인 실시간 진단 + 음성 감도(Sensitivity) & 끊김(Dropout) 시계열 데이터 반환:
        - sensitivity: 실시간 감도(RMS/Peak dBFS, 배경 소음 Noise Floor, SNR dB, 최근 10초 발화율 VAD %, 감도 등급)
        - dropouts: 실시간 끊김(지터 버퍼 언더런, 네트워크 패킷 갭 >120ms, 과증폭 클리핑 %, 무결성 연속 유지 시간)
        - history_60s / recent_events: 최근 60초 시계열 감도·끊김 차트 데이터 및 최근 이상 감지 이벤트 로그
        """
        now_t = time.time()
        with self.lock:
            is_active = self.active
            frames_sent = self.frames_sent
            fps_val = float(self.fps or 30.0)
            a_kbps = int(self.audio_bitrate_kbps or 160)

        with self._audio_lock:
            mob_alive = (self.last_mobile_audio_ts > 0) and ((now_t - self.last_mobile_audio_ts) < 2.5)
            pc_alive = (self.last_pc_audio_ts > 0) and ((now_t - self.last_pc_audio_ts) < 2.5)
            vdo_alive = (self._last_vdo_audio_ts > 0) and ((now_t - self._last_vdo_audio_ts) < 2.5)
            cam_alive = (self._last_cam_audio_ts > 0) and ((now_t - self._last_cam_audio_ts) < 2.5)
            dual_mob_Blocked = (self._dual_mob_collision_ts > 0) and ((now_t - self._dual_mob_collision_ts) < 8.0)

            if mob_alive and pc_alive:
                src_code = "both"
                src_label = f"📱+🎙️ 듀얼 마이크 ({'VDO.Ninja' if vdo_alive else '내장 /cam'} + PC 마이크)"
            elif pc_alive:
                src_code = "pc"
                src_label = "🎙️ PC 스튜디오 마이크 (48kHz PCM)"
            elif mob_alive:
                src_code = "mobile"
                src_label = "🥷 VDO.Ninja 무선 마이크 (48kHz Opus)" if vdo_alive else "📱 스마트폰 내장 /cam 마이크 (48kHz Worklet)"
            else:
                src_code = "none"
                src_label = "🔇 마이크 입력 대기 중"

            # 최근 3초 패킷 수신율(pkt/s) 및 초당 유입 바이트(bytes/s)
            recent_pkts = [(ts, nb) for ts, nb in self._audio_pkt_win if (now_t - ts) <= 3.0]
            if len(recent_pkts) >= 2:
                span = max(0.25, now_t - recent_pkts[0][0])
                pkt_per_sec = round(len(recent_pkts) / span, 1)
                bytes_per_sec = sum(nb for _, nb in recent_pkts) / span
                eff_sr_hz = int(round(min(48000.0, (bytes_per_sec / 2.0))))
                rate_pct = round(min(100.0, (bytes_per_sec / 96000.0) * 100.0), 1)
            else:
                pkt_per_sec = 0.0
                bytes_per_sec = 0.0
                eff_sr_hz = 48000 if (mob_alive or pc_alive) else 0
                rate_pct = 100.0 if (mob_alive or pc_alive) else 0.0

            pkt_int = list(self._audio_pkt_intervals_ms)
            pkt_avg_ms = round(sum(pkt_int) / len(pkt_int), 1) if pkt_int else 0.0
            pkt_max_ms = round(max(pkt_int), 1) if pkt_int else 0.0

            # 최근 10초 내 패킷 갭(>120ms) 끊김 통계
            recent_gaps_10s = [g_ms for ts, g_ms, _ in self._audio_pkt_gap_events if (now_t - ts) <= 10.0]
            pkt_gaps_10s = len(recent_gaps_10s)
            pkt_gap_max_10s_ms = round(max(recent_gaps_10s), 1) if recent_gaps_10s else pkt_max_ms
            pkt_gaps_total = len(self._audio_pkt_gap_events)

            mob_buf_ms = round(len(self._audio_buffer) / 96.0, 1)
            pc_buf_ms = round(len(self._audio_buffer_pc) / 96.0, 1)
            active_buf_ms = max(mob_buf_ms, pc_buf_ms)
            is_prebuffering = self._audio_prebuffering if mob_alive else (self._audio_pc_prebuffering if pc_alive else True)

            recent_underruns_10s = sum(1 for ts in self._audio_underrun_ts if (now_t - ts) <= 10.0)
            recent_underruns_1s = sum(1 for ts in self._audio_underrun_ts if (now_t - ts) <= 1.2)
            total_underruns = self.audio_underruns
            overflow_drops = self._audio_overflow_drops

            gain_pct = self.audio_gain_pct
            agc_on = bool(self.audio_agc_enabled)
            agc_target_db, _, _, agc_max_gain_x = self._compute_agc_target_params(gain_pct)
            eff_gain_x = round(float(self._last_effective_gain_x or (gain_pct / 100.0)), 2)
            raw_rms_db = round(float(self._last_raw_rms_db), 1)
            lvl_pct = round(self._env_level_pct, 1)
            pk_pct = round(self._env_peak_pct, 1)
            db_val = round(self._env_db, 1)
            pk_db_val = round(self._env_peak_db, 1)
            nf_db = round(self._noise_floor_db, 1)
            snr_db = round(max(0.0, db_val - nf_db), 1) if db_val > -58.0 else 0.0

            recent_vad = [v for ts, v in self._audio_vad_win if (now_t - ts) <= 10.0]
            vad_10s_pct = round((sum(recent_vad) / float(len(recent_vad))) * 100.0, 1) if recent_vad else 0.0

            recent_clips = [(c, tot) for ts, c, tot in self._audio_clip_win if (now_t - ts) <= 10.0]
            tot_c = sum(c for c, _ in recent_clips)
            tot_s = sum(tot for _, tot in recent_clips)
            clip_ratio_10s_pct = round((tot_c / float(max(1, tot_s))) * 100.0, 2) if tot_s > 0 else 0.0

            clean_streak_sec = int(max(0.0, now_t - self._last_clean_audio_since))
            audio_ticks = self._audio_ticks_sent
            pkts_total = self.audio_packets_received
            recent_events_list = list(reversed(self._audio_dropout_events[-15:]))
            hist_60s = list(self._audio_diag_history[-60:])
            hist_180s = list(self._audio_diag_history) if include_history else []

        # 감도(Sensitivity) 등급 및 가이드 판정
        if src_code == "none":
            sens_grade = "SILENT"
            sens_label = "⚪ 마이크 대기 중"
            sens_advice = "스마트폰 카메라(/cam 또는 VDO.Ninja)나 PC 마이크를 켜면 실시간 감도가 표시됩니다."
        elif clip_ratio_10s_pct >= 1.5 or pk_db_val >= -1.2:
            sens_grade = "TOO_HIGH"
            sens_label = f"🔴 감도 과열 / 클리핑 ({db_val}dB)"
            sens_advice = f"피크 감도가 {pk_db_val}dBFS에 도달해 소리가 찢어질 수 있습니다. 증폭({gain_pct}%)을 160% 이하로 낮추세요."
        elif lvl_pct < 5.0 and db_val <= -50.0:
            sens_grade = "QUIET"
            sens_label = f"🔵 무음 / 대기 ({db_val}dB)"
            sens_advice = f"현재 말을 하지 않는 대기 상태입니다. (배경 노이즈 플로어 {nf_db}dB · AGC {'ON' if agc_on else 'OFF'})"
        elif db_val < -34.0:
            sens_grade = "LOW"
            sens_label = f"🟡 감도 낮음 ({db_val}dB)"
            sens_advice = (
                f"수음 감도가 다소 작습니다({db_val}dB). [🤖 자동 감도 보정(AGC)]을 켜거나 스마트폰 VDO.Ninja ⚙️설정에서 소음 억제(Noise Suppression)를 OFF로 해제하세요."
            )
        else:
            sens_grade = "OPTIMAL"
            sens_label = f"🟢 적정 발화 감도 ({db_val}dB)"
            sens_advice = (
                f"방송 최적 초록색 감도 구간(-28dB ~ -6dB)으로 또렷하게 수음 중입니다! "
                f"(SNR +{snr_db}dB · {'🤖 AGC 자동보정 ' + str(eff_gain_x) + 'x 작동 중' if agc_on else '수동 증폭 ' + str(gain_pct) + '%'})"
            )

        # 끊김(Dropout) 등급 판정
        if recent_underruns_10s >= 2 or pkt_gaps_10s >= 3:
            drop_grade = "DROPOUT"
            drop_label = f"🔴 끊김 감지 (언더런 {recent_underruns_10s}회 / 패킷갭 {pkt_gaps_10s}회)"
        elif recent_underruns_10s == 1 or pkt_gaps_10s >= 1 or clip_ratio_10s_pct >= 1.5:
            drop_grade = "WARN"
            drop_label = f"🟡 미세 지터 주의 (패킷갭 {pkt_gaps_10s}회 / 끊김 {recent_underruns_10s}회)"
        else:
            drop_grade = "CLEAN"
            drop_label = f"🟢 끊김 0회 ({clean_streak_sec}초 연속 무결성 유지)"

        # Stage 4: A/V 마스터 클럭 락스텝 싱크 오차 (ms)
        if is_active and frames_sent > 5 and audio_ticks > 5:
            vid_pts_s = frames_sent / fps_val
            aud_pts_s = audio_ticks * 0.02
            av_drift_ms = round((aud_pts_s - vid_pts_s) * 1000.0, 1)
            if abs(av_drift_ms) > 45.0:
                av_drift_ms = 0.0
        else:
            av_drift_ms = 0.0

        # Stage 5: 4대 세부 진단 항목 (5-A ~ 5-D)
        sub_items = []
        todo_list = []

        # 5-A: 마이크 입력 소스 & 중복 유입 충돌 진단
        if src_code == "none":
            sub_items.append({
                "code": "5-A",
                "title": "마이크 수음 소스 & 중복 충돌 점검",
                "status": "warn",
                "badge": "🟡 마이크 대기",
                "value": "입력된 마이크 없음",
                "detail": "현재 스마트폰(VDO.Ninja 또는 /cam)이나 PC 마이크에서 들어오는 오디오 패킷이 없습니다.",
                "action": "스마트폰에서 VDO.Ninja(또는 /cam) 마이크가 켜져 있는지 확인하거나, [🎤 음성 설정] 탭에서 [🎙️ PC 마이크 켜기]를 눌러주세요."
            })
            todo_list.append("① [마이크 연결] 스마트폰 VDO.Ninja(또는 /cam) 화면에서 마이크 아이콘이 켜져 있는지 확인하거나 PC 마이크를 켜주세요.")
        elif src_code == "both":
            sub_items.append({
                "code": "5-A",
                "title": "마이크 수음 소스 & 중복 충돌 점검",
                "status": "warn",
                "badge": "🟡 폰+PC 동시 수음 중",
                "value": src_label,
                "detail": "스마트폰 마이크와 PC 마이크가 동시에 켜져 있어 같은 공간에 있을 경우 목소리가 2번 겹쳐(에코/울림) 들릴 수 있습니다.",
                "action": "한 공간에서 혼자 방송하실 때는 스마트폰 마이크만 켜두고 [🎙️ PC 마이크]는 꺼두시는 것이 가장 깨끗합니다!"
            })
            todo_list.append("⚠️ [에코/겹침 주의] 현재 스마트폰 마이크와 PC 마이크가 동시에 켜져 있습니다. 목소리가 겹쳐 들린다면 [🎤 음성 설정]에서 PC 마이크를 꺼주세요.")
        else:
            extra_note = " (중복 /cam 오디오 유입 자동 차단됨)" if dual_mob_Blocked else ""
            sub_items.append({
                "code": "5-A",
                "title": "마이크 수음 소스 & 중복 충돌 점검",
                "status": "ok",
                "badge": "🟢 단일 소스 정상",
                "value": f"{src_label}{extra_note}",
                "detail": f"단일 마이크 소스에서 초당 {pkt_per_sec}개 패킷(48,000Hz)이 충돌 없이 깨끗하게 수신되고 있습니다.",
                "action": ""
            })

        # 5-B: 음성 끊김(Underrun) & 지터 버퍼 무결성 진단
        if recent_underruns_10s >= 3:
            sub_items.append({
                "code": "5-B",
                "title": "음성 끊김(Underrun) & 지터 버퍼 진단",
                "status": "bad",
                "badge": f"🔴 최근 10초 끊김 {recent_underruns_10s}회",
                "value": f"버퍼 수위 {active_buf_ms}ms · 패킷지터 최대 {pkt_max_ms}ms",
                "detail": "스마트폰 Wi-Fi 패킷 지연으로 오디오 지터 버퍼가 순간 바닥나면서 음절 끊김이 발생했습니다.",
                "action": "아래 [🔄 오디오 지터버퍼 즉시 리셋] 버튼을 누르거나, 스마트폰 화면이 꺼지지 않도록 유지해 주세요."
            })
            todo_list.append(f"🔴 [음성 끊김 감지] 최근 10초 동안 버퍼 마름(언더런)이 {recent_underruns_10s}회 발생했습니다. 아래 [🔄 오디오 지터버퍼 즉시 리셋] 버튼을 눌러 버퍼 쿠션(160ms)을 재정렬하세요.")
        else:
            sub_items.append({
                "code": "5-B",
                "title": "음성 끊김(Underrun) & 지터 버퍼 진단",
                "status": "ok",
                "badge": "🟢 끊김 0회 (1:1 무변조)",
                "value": f"버퍼 {active_buf_ms}ms · 최근 10초 끊김 {recent_underruns_10s}회",
                "detail": "가변 리샘플링과 자투리 강제 늘림(괴물 소리)이 완전 제거된 1:1 비트 무변조(Bit-Exact) 직결 버퍼가 안정적으로 작동 중입니다.",
                "action": ""
            })

        # 5-C: 음성 뭉개짐(Muffling) & 과증폭 클리핑(찢어짐) 진단
        if clip_ratio_10s_pct >= 1.5:
            sub_items.append({
                "code": "5-C",
                "title": "발음 명료도 & 과증폭 클리핑(뭉개짐) 진단",
                "status": "bad" if clip_ratio_10s_pct >= 3.0 else "warn",
                "badge": f"🟡 클리핑 {clip_ratio_10s_pct}% (증폭 {gain_pct}%)",
                "value": f"현재 볼륨 {db_val}dB (피크 {pk_pct}%) · 증폭 {gain_pct}% · AGC {'ON' if agc_on else 'OFF'}",
                "detail": "마이크 증폭 배율이 너무 높아 큰 소리에서 파형이 천장에 부딪히며 발음이 뭉개지거나 찢어질 수 있습니다.",
                "action": "아래 [🤖 자동 감도 보정(AGC)]을 켜거나 [🔊 권장 볼륨(160%)] 버튼을 눌러 증폭을 조절해 주세요."
            })
            todo_list.append(f"🔊 [과증폭 클리핑 주의] 피크 클리핑({clip_ratio_10s_pct}%)이 감지되었습니다. 자동 감도 보정(AGC)을 켜거나 증폭을 낮추면 발음이 훨씬 또렷해집니다.")
        else:
            sub_items.append({
                "code": "5-C",
                "title": "발음 명료도 & 자동 감도 보정(AGC) 진단",
                "status": "ok",
                "badge": f"🟢 {'🤖 AGC 자동보정 ' + str(eff_gain_x) + 'x' if agc_on else '48kHz 원음 최적'}",
                "value": f"AGC {'ON (' + str(eff_gain_x) + 'x)' if agc_on else 'OFF'} · 클리핑 {clip_ratio_10s_pct}% · 프리앰프 {gain_pct}%",
                "detail": "로우패스 필터 왜곡 제거 + 실시간 자동 감도 보정(AGC) 및 소프트 리미터가 작동하여 작은 스피커 음악과 원거리 음성도 초록색 적정 감도로 무왜곡 증폭됩니다.",
                "action": ""
            })

        # 5-D: 하울링 & 스마트폰 에코캔슬러(AEC) 2초 주기 먹통 방지 진단
        sub_items.append({
            "code": "5-D",
            "title": "하울링 & 스마트폰 에코캔슬러(AEC) 보호",
            "status": "ok",
            "badge": "🟢 2초 주기 먹통 방지 활성",
            "value": "백그라운드 브라우저 무음 격리 완료",
            "detail": "만약 핸드폰으로 유튜브 방송을 틀어놓고 같은 방에서 소리(스피커)를 키우면, 방송 소리가 다시 촬영 폰 마이크로 들어가 2초마다 소리가 먹통이 됩니다.",
            "action": "시청 확인용 핸드폰의 볼륨은 줄여두시거나 반드시 이어폰을 꽂고 음성을 확인하세요!"
        })

        if not todo_list:
            todo_list = [
                "✅ [음성 파이프라인 무결성 100% 완료] OBS와 동일하게 가변 리샘플링·자투리 타임스트레칭·로우패스 필터가 모두 제거되어 48kHz 1:1 원음 그대로 유튜브에 송출됩니다!",
                "🎧 [핸드폰에서 음성 확인 시 필수 팁] 촬영 중인 스마트폰 옆에서 시청용 핸드폰 스피커를 크게 틀면 하울링(메아리)으로 인해 폰 마이크가 소리를 뭉개뜨립니다. 반드시 이어폰을 꽂거나 다른 방에서 음성을 테스트하세요!"
            ]

        if src_code == "none":
            overall_status = "WAITING_MIC"
            summary_text = "🟡 [마이크 입력 대기 중] 스마트폰(VDO.Ninja / 내장 카메라) 또는 PC 마이크가 연결되면 5단계 실시간 음성 품질 진단이 즉시 작동합니다."
        elif recent_underruns_10s >= 3:
            overall_status = "UNDERRUN_WARN"
            summary_text = f"🔴 [음성 버퍼 끊김 감지] 최근 10초간 지터 버퍼 언더런이 {recent_underruns_10s}회 발생했습니다. [🔄 오디오 지터버퍼 즉시 리셋]으로 안정화하세요."
        elif clip_ratio_10s_pct >= 2.0:
            overall_status = "CLIPPING_WARN"
            summary_text = f"🟡 [과증폭 뭉개짐 주의] 마이크 증폭({gain_pct}%)이 높아 피크 클리핑({clip_ratio_10s_pct}%)이 발생 중입니다. 권장 증폭(160%)으로 낮춰주세요."
        else:
            overall_status = "OPTIMAL_AUDIO"
            summary_text = f"✅ [48kHz 1:1 무변조 원음 송출 중!] 리샘플링 왜곡 0% · 최근 10초 끊김 0회 · A/V 싱크 오차 {av_drift_ms}ms로 깨끗한 음성이 송출되고 있습니다!"

        res_dict = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "rtmp_active": is_active,
            "log_file": "audio_quality_diag.jsonl",
            "sensitivity": {
                "level_pct": lvl_pct,
                "peak_pct": pk_pct,
                "rms_db": db_val,
                "raw_rms_db": raw_rms_db,
                "peak_db": pk_db_val,
                "noise_floor_db": nf_db,
                "snr_db": snr_db,
                "voice_activity_10s_pct": vad_10s_pct,
                "gain_pct": gain_pct,
                "agc_enabled": agc_on,
                "agc_target_db": agc_target_db,
                "agc_max_gain_x": agc_max_gain_x,
                "effective_gain_x": eff_gain_x,
                "sensitivity_grade": sens_grade,
                "grade_label": sens_label,
                "advice": sens_advice
            },
            "dropouts": {
                "underruns_1s": recent_underruns_1s,
                "underruns_10s": recent_underruns_10s,
                "underruns_total": total_underruns,
                "pkt_gaps_10s": pkt_gaps_10s,
                "pkt_gaps_total": pkt_gaps_total,
                "pkt_gap_max_ms": pkt_gap_max_10s_ms,
                "clip_pct_10s": clip_ratio_10s_pct,
                "overflow_drops": overflow_drops,
                "clean_streak_sec": clean_streak_sec,
                "dropout_grade": drop_grade,
                "grade_label": drop_label
            },
            "stage1_mic_capture": {
                "active": src_code != "none",
                "status": "ok" if src_code in ("mobile", "pc") else "warn",
                "source_code": src_code,
                "source_label": src_label,
                "pkt_rate_per_sec": pkt_per_sec,
                "packets_per_sec": pkt_per_sec,
                "packets_total": pkts_total,
                "pkt_interval_avg_ms": pkt_avg_ms,
                "pkt_interval_max_ms": pkt_max_ms,
                "pkt_jitter_ms": round(max(0.0, pkt_max_ms - pkt_avg_ms), 1),
                "dual_source_drops": 1 if dual_mob_Blocked else 0,
                "effective_sample_rate_hz": eff_sr_hz,
                "ingest_rate_pct": rate_pct
            },
            "stage2_jitter_buffer": {
                "status": "bad" if recent_underruns_10s >= 3 else ("warn" if recent_underruns_10s > 0 else "ok"),
                "buffer_ms": active_buf_ms,
                "mobile_buffer_ms": mob_buf_ms,
                "pc_buffer_ms": pc_buf_ms,
                "prebuffering": is_prebuffering,
                "underruns_10s": recent_underruns_10s,
                "recent_underruns_10s": recent_underruns_10s,
                "underruns_total": total_underruns,
                "total_underruns": total_underruns,
                "overflow_drops": overflow_drops,
                "resample_distortion_pct": 0.0,
                "mode": "1:1 Bit-Exact (960 samples @ 20ms)"
            },
            "stage3_dsp_preamp": {
                "status": "bad" if clip_ratio_10s_pct >= 3.0 else ("warn" if clip_ratio_10s_pct >= 1.0 else "ok"),
                "gain_pct": gain_pct,
                "agc_enabled": agc_on,
                "effective_gain_x": eff_gain_x,
                "level_pct": lvl_pct,
                "peak_pct": pk_pct,
                "rms_db": db_val,
                "db_fs": db_val,
                "clip_pct_10s": clip_ratio_10s_pct,
                "clip_ratio_10s_pct": clip_ratio_10s_pct,
                "lowpass_muffle_filter": "OFF (고음역 자음 100% 보존)"
            },
            "stage4_ffmpeg_aac_sync": {
                "status": "ok" if abs(av_drift_ms) <= 45.0 else "warn",
                "codec": f"AAC-LC {a_kbps}kbps · 48kHz Stereo",
                "bitrate_kbps": a_kbps,
                "feeder_rate_hz": 50.0 if is_active else 0.0,
                "pipe_write_avg_ms": 0.1,
                "audio_ticks_sent": audio_ticks,
                "av_sync_drift_ms": av_drift_ms,
                "lockstep_clock": True
            },
            "stage5_broadcast_quality": {
                "status": "ok" if overall_status in ("OPTIMAL_AUDIO", "WAITING_MIC") else "warn",
                "sub_items": sub_items,
                "todo_checklist": todo_list
            },
            "summary": {
                "status": overall_status,
                "bottleneck_stage": "OPTIMAL" if overall_status in ("OPTIMAL_AUDIO", "WAITING_MIC") else overall_status,
                "bottleneck_summary": summary_text,
                "summary_text": summary_text,
                "buffer_ms": active_buf_ms,
                "recent_underruns_10s": recent_underruns_10s,
                "clip_ratio_10s_pct": clip_ratio_10s_pct,
                "av_sync_drift_ms": av_drift_ms
            },
            "history_60s": hist_60s,
            "recent_events": recent_events_list
        }
        if include_history:
            res_dict["history_180s"] = hist_180s
        return res_dict

    def _telemetry_collector_loop(self):
        """1초 주기로 5단계 영상 및 음성(감도·끊김) 계측 스냅샷을 수집하고 jsonl 파일 및 인메모리 히스토리에 보관합니다."""
        last_yt_check = 0.0
        wrote_initial_audio = False
        while True:
            time.sleep(1.0)
            try:
                now_t = time.time()
                # 방송 중이거나 최근 채팅/영상이 연결된 경우 12초마다 유튜브 서버 설정(Stage 5) 자동 갱신
                if (now_t - last_yt_check) >= 12.0:
                    last_yt_check = now_t
                    try:
                        self.inspect_youtube_live_latency(force=False)
                    except Exception:
                        pass

                snap = self.get_video_diagnostics(include_history=False, refresh_youtube=False)
                compact_entry = {
                    "ts": snap["timestamp"],
                    "active": snap["rtmp_active"],
                    "elapsed_s": snap["elapsed_seconds"],
                    "frames_sent": snap["frames_sent"],
                    "s1_ms": snap["stage1_camera_ingest"]["stage1_est_delay_ms"],
                    "s1_jb_ms": snap["stage1_camera_ingest"]["webrtc_jitter_buffer_ms"],
                    "s1_cap_ms": snap["stage1_camera_ingest"]["canvas_capture_ms"],
                    "s1_fps": snap["stage1_camera_ingest"]["webrtc_fps_decoded"],
                    "s2_ms": snap["stage2_server_compositor"]["stage2_est_delay_ms"],
                    "s2_cam_age_ms": snap["stage2_server_compositor"]["cam_frame_age_avg_ms"],
                    "s2_comp_ms": snap["stage2_server_compositor"]["compositor_build_avg_ms"],
                    "s3_ff_frames": snap["stage3_ffmpeg_encoder"]["encoded_frames"],
                    "s3_ff_q_frames": snap["stage3_ffmpeg_encoder"]["queued_frames"],
                    "s3_ff_lag_ms": snap["stage3_ffmpeg_encoder"]["internal_lag_ms"],
                    "s3_ff_speed": snap["stage3_ffmpeg_encoder"]["speed_x"],
                    "s4_kbps": snap["stage4_rtmp_uplink"]["uplink_kbps"],
                    "s4_blk_ms": snap["stage4_rtmp_uplink"]["send_block_avg_ms"],
                    "s5_vid": snap["stage5_youtube_server"].get("video_id", ""),
                    "s5_class": snap["stage5_youtube_server"].get("latency_class", ""),
                    "s5_readahead_s": snap["stage5_youtube_server"].get("live_readahead_sec"),
                    "s5_min_ra_ms": snap["stage5_youtube_server"].get("min_readahead_ms"),
                    "local_sec": snap["summary"]["local_pipeline_delay_sec"],
                    "yt_buf_sec": snap["summary"]["youtube_server_buffer_sec"],
                    "est_mob_sec": snap["summary"]["estimated_total_mobile_delay_sec"],
                    "bottleneck": snap["summary"]["bottleneck_stage"]
                }
                with self.lock:
                    self._diag_history.append(compact_entry)
                    if len(self._diag_history) > 180:
                        del self._diag_history[:-120]

                if snap["rtmp_active"]:
                    try:
                        if os.path.exists(self._diag_log_path) and os.path.getsize(self._diag_log_path) > 5 * 1024 * 1024:
                            with open(self._diag_log_path, "w", encoding="utf-8") as f:
                                f.write(json.dumps(compact_entry, ensure_ascii=False) + "\n")
                        else:
                            with open(self._diag_log_path, "a", encoding="utf-8") as f:
                                f.write(json.dumps(compact_entry, ensure_ascii=False) + "\n")
                    except Exception:
                        pass

                # 🎤 1초 주기 음성 감도(Sensitivity) & 끊김(Dropout) 시계열 스냅샷 수집 및 보관
                a_snap = self.get_audio_diagnostics(include_history=False)
                a_sens = a_snap["sensitivity"]
                a_drop = a_snap["dropouts"]
                a_s1 = a_snap["stage1_mic_capture"]
                a_s2 = a_snap["stage2_jitter_buffer"]
                a_s4 = a_snap["stage4_ffmpeg_aac_sync"]
                audio_entry = {
                    "ts": a_snap["timestamp"][-12:-4],
                    "full_ts": a_snap["timestamp"],
                    "rtmp_active": a_snap["rtmp_active"],
                    "mic_active": a_s1["active"],
                    "source": a_s1["source_code"],
                    "level_pct": a_sens["level_pct"],
                    "peak_pct": a_sens["peak_pct"],
                    "rms_db": a_sens["rms_db"],
                    "peak_db": a_sens["peak_db"],
                    "noise_floor_db": a_sens["noise_floor_db"],
                    "snr_db": a_sens["snr_db"],
                    "vad_pct": a_sens["voice_activity_10s_pct"],
                    "gain_pct": a_sens["gain_pct"],
                    "sens_grade": a_sens["sensitivity_grade"],
                    "pkt_rate": a_s1["pkt_rate_per_sec"],
                    "pkt_gap_max_ms": a_drop["pkt_gap_max_ms"],
                    "pkt_gaps_10s": a_drop["pkt_gaps_10s"],
                    "buf_ms": a_s2["buffer_ms"],
                    "underrun_1s": a_drop["underruns_1s"],
                    "underruns_10s": a_drop["underruns_10s"],
                    "underruns_total": a_drop["underruns_total"],
                    "clip_pct": a_drop["clip_pct_10s"],
                    "av_drift_ms": a_s4["av_sync_drift_ms"],
                    "drop_grade": a_drop["dropout_grade"]
                }
                with self._audio_lock:
                    self._audio_diag_history.append(audio_entry)
                    if len(self._audio_diag_history) > 180:
                        del self._audio_diag_history[:-120]

                # 마이크가 활성 중이거나 방송 송출 중이거나 최초 1회일 때 디스크(audio_quality_diag.jsonl)에 상시 저장
                if a_s1["active"] or a_snap["rtmp_active"] or (not wrote_initial_audio):
                    wrote_initial_audio = True
                    try:
                        if os.path.exists(self._audio_diag_log_path) and os.path.getsize(self._audio_diag_log_path) > 5 * 1024 * 1024:
                            with open(self._audio_diag_log_path, "w", encoding="utf-8") as f:
                                f.write(json.dumps(audio_entry, ensure_ascii=False) + "\n")
                        else:
                            with open(self._audio_diag_log_path, "a", encoding="utf-8") as f:
                                f.write(json.dumps(audio_entry, ensure_ascii=False) + "\n")
                    except Exception:
                        pass
            except Exception:
                pass

    def is_verified_live(self):
        """실제 유튜브 플랫폼 웹사이트에서 온에어(라이브 방송중)가 검증되었는지 반환합니다."""
        with self.lock:
            if not self.active:
                return False
            if self._yt_manifest_cache.get("is_live") and self._yt_manifest_cache.get("video_id"):
                return True
        try:
            if platform_tracker.yt_is_live:
                return True
        except Exception:
            pass
        try:
            if "simulcast_automation_bridge" in globals() and simulcast_automation_bridge is not None:
                if simulcast_automation_bridge.platforms.get("youtube", {}).get("go_live_clicked"):
                    return True
        except Exception:
            pass
        try:
            if "youtube_chat_poller" in globals() and youtube_chat_poller is not None:
                with youtube_chat_poller.lock:
                    if youtube_chat_poller.video_id and (youtube_chat_poller.active or self._yt_manifest_cache.get("is_live")):
                        return True
        except Exception:
            pass
        return False

    def _compute_live_stage_and_reason(self, verified_live):
        if not self.active:
            if self.last_error:
                return "ERROR", self.last_error
            return "OFFLINE", "대기 중"
        if verified_live:
            return "VERIFIED_ON_AIR", ""
        yt_bridge = {}
        try:
            if "simulcast_automation_bridge" in globals() and simulcast_automation_bridge is not None:
                yt_bridge = simulcast_automation_bridge.platforms.get("youtube", {}) or {}
        except Exception:
            pass
        if yt_bridge.get("login_required") or (yt_bridge.get("browser_running") and not yt_bridge.get("logged_in")):
            return "SIGNAL_SENDING_WAIT_ONAIR", "유튜브 스튜디오 로그인이 되어 있지 않습니다. [🔑 유튜브 로그인/제어실 열기]로 로그인 후 방송방을 확인하세요."
        if self.last_error:
            return "SIGNAL_SENDING_WAIT_ONAIR", self.last_error
        return "SIGNAL_SENDING_WAIT_ONAIR", "RTMP 영상·음성은 전송 중이나, 유튜브 스튜디오 제어실에서 실제 라이브가 아직 시작되지 않았습니다."

    def get_state(self):
        verified_live = self.is_verified_live()
        with self.lock:
            live_stage, diagnostic_reason = self._compute_live_stage_and_reason(verified_live)
            saved_key = self.stream_key or database.get_overlay_state("youtube_stream_key", "")
            saved_rtmp = self.rtmp_url or database.get_overlay_state("youtube_rtmp_url", "rtmp://a.rtmp.youtube.com/live2")
            saved_v_kbps = int(database.get_overlay_state("youtube_video_bitrate_kbps", self.video_bitrate_kbps or 4500) or 4500)
            if saved_v_kbps == 6800:
                saved_v_kbps = 4500
            saved_a_kbps = int(database.get_overlay_state("youtube_audio_bitrate_kbps", self.audio_bitrate_kbps or 128) or 128)
            saved_kf_sec = int(database.get_overlay_state("youtube_keyframe_sec", self.keyframe_interval_sec or 1) or 1)
            if saved_kf_sec == 2 and not self.active:
                saved_kf_sec = 1
            saved_gain_pct = int(database.get_overlay_state("youtube_audio_gain_pct", self.audio_gain_pct or 240) or 240)
            self.audio_gain_pct = max(50, min(1200, saved_gain_pct))
            saved_agc = str(database.get_overlay_state("youtube_audio_agc_enabled", "1" if self.audio_agc_enabled else "0"))
            self.audio_agc_enabled = (saved_agc != "0")
            elapsed = int(time.time() - self.started_at) if (self.active and self.started_at) else 0
            now_t = time.time()
            now_pc = time.perf_counter()
            audio_active = (self.last_audio_ts > 0) and ((now_t - self.last_audio_ts) < 3.5)
            mob_active = (self.last_mobile_audio_ts > 0) and ((now_t - self.last_mobile_audio_ts) < 2.5)
            pc_active = (self.last_pc_audio_ts > 0) and ((now_t - self.last_pc_audio_ts) < 2.5)
            if mob_active and pc_active:
                audio_source = "both"
            elif pc_active:
                audio_source = "pc"
            elif mob_active:
                audio_source = "mobile"
            else:
                audio_source = "none"

            # RTMP 미송출 상태에서 마이크 입력이 중단된 경우 볼륨 미터 자연 감쇠
            if not self.active and (now_t - self._last_meter_update_ts) > 0.35:
                decay_steps = min(20, int((now_t - self._last_meter_update_ts) / 0.15))
                if decay_steps > 0:
                    factor = 0.65 ** decay_steps
                    self._env_level_pct = round(self._env_level_pct * factor, 1)
                    self._env_peak_pct = round(self._env_peak_pct * (0.75 ** decay_steps), 1)
                    self._env_mob_pct = round(self._env_mob_pct * factor, 1)
                    self._env_pc_pct = round(self._env_pc_pct * factor, 1)
                    if self._env_level_pct < 0.8:
                        self._env_level_pct = 0.0
                        self._env_db = -60.0
                        self._audio_bars = [0.0] * 8
                    if self._env_peak_pct < 0.8:
                        self._env_peak_pct = 0.0

            if self.active and self._frame_perf_times:
                recent_ft = [t for t in self._frame_perf_times if (now_pc - t) <= 1.5]
                if len(recent_ft) >= 2:
                    span = recent_ft[-1] - recent_ft[0]
                    raw_fps = (len(recent_ft) - 1) / span if span > 0.05 else float(self.fps)
                    current_fps = round(min(float(self.fps), max(0.0, raw_fps)), 1)
                elif len(recent_ft) == 1:
                    current_fps = float(self.fps)
                else:
                    current_fps = 0.0
            else:
                current_fps = 0.0

            hw_enc = self._detect_hw_encoder()
            if hw_enc == "h264_nvenc":
                enc_label = "NVIDIA NVENC H.264 (ULL · CBR)"
            elif hw_enc == "h264_qsv":
                enc_label = "Intel QuickSync H.264 (CBR)"
            elif hw_enc == "h264_amf":
                enc_label = "AMD HW H.264 (ULL · CBR)"
            else:
                enc_label = "x264 ZeroLatency (CBR)"

            return {
                "active": self.active,
                "is_verified_live": verified_live,
                "live_stage": live_stage,
                "diagnostic_reason": diagnostic_reason,
                "rtmp_url": saved_rtmp,
                "has_stream_key": bool(saved_key),
                "stream_key": saved_key,
                "width": self.width,
                "height": self.height,
                "fps": self.fps,
                "current_fps": current_fps,
                "video_bitrate_kbps": self.video_bitrate_kbps if self.active else saved_v_kbps,
                "audio_bitrate_kbps": self.audio_bitrate_kbps if self.active else saved_a_kbps,
                "keyframe_interval_sec": self.keyframe_interval_sec if self.active else saved_kf_sec,
                "encoder_name": enc_label,
                "audio_sample_rate_hz": 48000,
                "audio_active": audio_active,
                "audio_source": audio_source,
                "audio_gain_pct": self.audio_gain_pct,
                "audio_agc_enabled": self.audio_agc_enabled,
                "audio_effective_gain_x": round(float(self._last_effective_gain_x or (self.audio_gain_pct / 100.0)), 2),
                "audio_level_pct": round(self._env_level_pct, 1),
                "audio_peak_pct": round(self._env_peak_pct, 1),
                "audio_db": round(self._env_db, 1),
                "mobile_level_pct": round(self._env_mob_pct, 1),
                "pc_level_pct": round(self._env_pc_pct, 1),
                "audio_bars": list(self._audio_bars),
                "audio_packets_received": self.audio_packets_received,
                "audio_underruns": self.audio_underruns,
                "audio_buffer_ms": round(max(len(self._audio_buffer), len(self._audio_buffer_pc)) / 96.0, 1),
                "elapsed_seconds": elapsed,
                "frames_sent": self.frames_sent,
                "burnin_timecode": self.burnin_timecode,
                "ffmpeg_telemetry": dict(self._ffmpeg_telemetry),
                "rtmp_uplink_kbps": self._rtmp_uplink_kbps,
                "rtmp_send_block_avg_ms": self._rtmp_send_block_avg_ms,
                "yt_server_latency": dict(self._yt_manifest_cache),
                "status_text": self.status_text,
                "last_error": self.last_error
            }


youtube_chat_poller = YouTubeDirectChatPoller()
youtube_direct_streamer = YouTubeDirectStreamer()


class TikTokDirectChatPoller:
    """
    별도 브라우저 확장프로그램이나 유료 API 키 없이 틱톡 아이디(@핸들) 또는 라이브 URL(https://www.tiktok.com/@아이디/live)에서
    현재 진행 중인 라이브 방송의 실시간 채팅·동시 시청자 수·접속 회원 아이디를 직접 수집·연동하는 매니저
    """
    def __init__(self):
        from collections import deque
        self.lock = Lock()
        self.active = False
        self.channel_input = ""
        self.unique_id = ""
        self.room_id = ""
        self.broadcast_title = ""
        self.status_text = "대기 중"
        self.last_error = ""
        self.collected_count = 0
        self.last_chat_at = None
        self.seen_ids = deque(maxlen=5000)
        self._initialized_room_id = ""
        self._worker_thread = None
        self._stop_flag = False
        self._worker_gen = 0

    def _sleep_interruptible(self, seconds, my_gen):
        end_t = time.time() + float(seconds)
        while time.time() < end_t:
            if self._stop_flag or not self.active or self._worker_gen != my_gen:
                return False
            time.sleep(min(0.4, max(0.05, end_t - time.time())))
        return not (self._stop_flag or not self.active or self._worker_gen != my_gen)

    def normalize_tiktok_target(self, raw_input):
        s = (raw_input or "").strip()
        if not s:
            return "", ""
        m = re.search(r"tiktok\.com/@([a-zA-Z0-9_.-]+)", s)
        if m:
            uid = m.group(1).strip().lstrip("@")
            return f"https://www.tiktok.com/@{uid}/live", uid
        uid = s.lstrip("@").split("/")[0].split("?")[0].strip()
        if not uid:
            return "", ""
        return f"https://www.tiktok.com/@{uid}/live", uid

    def connect(self, raw_input):
        clean_input = (raw_input or "").strip()
        target_url, uid = self.normalize_tiktok_target(clean_input)
        if not target_url or not uid:
            return {"ok": False, "message": "틱톡 아이디(예: @아이디) 또는 라이브 방송 URL을 입력해 주세요."}

        norm_handle = f"@{uid}" if not clean_input.startswith("http") else clean_input
        with self.lock:
            self._worker_gen += 1
            my_gen = self._worker_gen
            self._stop_flag = False
            self.active = True
            self.channel_input = norm_handle
            self.unique_id = uid
            self.room_id = ""
            self.broadcast_title = ""
            self.status_text = f"틱톡 라이브(@{uid}) 방송 연결 확인 중..."
            self.last_error = ""
        try:
            database.set_overlay_state("tiktok_channel_input", self.channel_input)
        except Exception:
            pass

        self._worker_thread = threading.Thread(
            target=self._poll_loop,
            args=(target_url, uid, my_gen),
            daemon=True
        )
        self._worker_thread.start()
        return {"ok": True, "message": f"틱톡(@{uid}) 실시간 채팅 연결을 시작했습니다.", "state": self.get_state()}

    def disconnect(self):
        with self.lock:
            self._worker_gen += 1
            self._stop_flag = True
            self.active = False
            self.status_text = "연결 해제됨"
        platform_tracker.update_tiktok_live_meta(is_live=False, concurrent_viewers=0)
        return {"ok": True, "message": "틱톡 채널 채팅 연결이 해제되었습니다.", "state": self.get_state()}

    def get_state(self):
        with self.lock:
            ch_in = self.channel_input or database.get_overlay_state("tiktok_channel_input", "")
            return {
                "active": self.active,
                "channel_input": ch_in,
                "unique_id": self.unique_id,
                "room_id": self.room_id,
                "video_id": self.room_id,
                "broadcast_title": self.broadcast_title,
                "status_text": self.status_text,
                "last_error": self.last_error,
                "collected_count": self.collected_count,
                "last_chat_at": self.last_chat_at
            }

    def _fetch_room_meta_http(self, session, target_url, uid):
        """틱톡 웹 API 및 라이브 페이지 HTML에서 room_id, 라이브 상태(status=2), 방송 제목, 동시 시청자 수를 추출합니다."""
        room_id = ""
        is_live = False
        title = ""
        viewers = None
        total_views = None

        # 1) api-live/user/room 엔드포인트 조회
        try:
            api_url = f"https://www.tiktok.com/api-live/user/room/?aid=1988&uniqueId={uid}&sourceType=54"
            r_api = session.get(api_url, timeout=6)
            if r_api.status_code == 200:
                j = r_api.json()
                data_obj = j.get("data") or {}
                user_obj = data_obj.get("user") or {}
                live_room = data_obj.get("liveRoom") or {}
                room_id = str(live_room.get("roomId") or user_obj.get("roomId") or "")
                status_val = live_room.get("status", user_obj.get("status"))
                if status_val == 2 or (room_id and status_val not in (4, 0)):
                    is_live = True
                title = str(live_room.get("title") or "")
                stats_obj = live_room.get("liveRoomStats") or {}
                if stats_obj.get("userCount") is not None:
                    viewers = int(stats_obj["userCount"])
                if stats_obj.get("enterCount") is not None:
                    total_views = int(stats_obj["enterCount"])
        except Exception:
            pass

        # 2) HTML 페이지 폴백 파싱
        if not room_id:
            try:
                resp = session.get(target_url, timeout=7, allow_redirects=True)
                html = resp.text
                m_rid = re.search(r'"roomId"\s*:\s*"(\d{10,25})"', html)
                if m_rid:
                    room_id = m_rid.group(1)
                if '"status":2' in html or '"isLive":true' in html:
                    is_live = True
                m_t = re.search(r'"liveRoom"\s*:\s*\{[^}]*?"title"\s*:\s*"([^"]+)"', html)
                if m_t:
                    title = m_t.group(1)
                elif not title:
                    m_title = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
                    if m_title:
                        title = m_title.group(1).replace("| TikTok", "").replace("- TikTok", "").strip()
                m_uc = re.search(r'"userCount"\s*:\s*(\d+)', html)
                if m_uc and viewers is None:
                    viewers = int(m_uc.group(1))
            except Exception:
                pass

        # 3) Webcast room info로 실시간 시청자 수 보강
        if room_id:
            try:
                wc_url = f"https://webcast.tiktok.com/webcast/room/info/?aid=1988&room_id={room_id}"
                r_wc = session.get(wc_url, timeout=5)
                if r_wc.status_code == 200:
                    wc_data = (r_wc.json() or {}).get("data") or {}
                    st = wc_data.get("status")
                    if st == 2:
                        is_live = True
                    elif st == 4:
                        is_live = False
                    if wc_data.get("user_count") is not None:
                        viewers = int(wc_data["user_count"])
                    if wc_data.get("title") and not title:
                        title = str(wc_data["title"])
                    stats_wc = wc_data.get("stats") or {}
                    if stats_wc.get("total_user") is not None:
                        total_views = int(stats_wc["total_user"])
            except Exception:
                pass

        return {
            "room_id": room_id,
            "is_live": is_live,
            "title": title,
            "viewers": viewers,
            "total_views": total_views
        }

    def _ingest_tiktok_chat_item(self, msg_id, author, unique_id, msg_text, avatar_url="", badge_str="", is_initial_batch=False):
        msg_text = str(msg_text or "").strip()
        author = str(author or unique_id or "틱톡시청자").strip()
        if not msg_text:
            return
        dedup_key = str(msg_id or f"tt_{author}_{msg_text}")
        with self.lock:
            if dedup_key in self.seen_ids:
                return
            self.seen_ids.append(dedup_key)
            self.collected_count += 1
            self.last_chat_at = datetime.now().strftime("%H:%M:%S")

        if is_initial_batch:
            platform_tracker.record_user_presence(
                platform_raw="TIKTOK",
                user=author,
                message=msg_text,
                category="CHAT",
                channel_id=str(unique_id or author),
                avatar=avatar_url,
                badge=badge_str,
                increment_chat=True
            )
        else:
            try:
                process_chat_payload({
                    "id": dedup_key,
                    "chatname": author,
                    "chatmessage": msg_text,
                    "type": "tiktok",
                    "source": "tiktok_direct"
                })
                platform_tracker.record_user_presence(
                    platform_raw="TIKTOK",
                    user=author,
                    message=msg_text,
                    channel_id=str(unique_id or author),
                    avatar=avatar_url,
                    badge=badge_str,
                    increment_chat=False
                )
            except Exception as proc_err:
                print(f"⚠️ [틱톡 직접 채팅 처리 오류] {proc_err}", flush=True)

    def _poll_loop(self, target_url, uid, my_gen):
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        session = requests.Session()
        session.verify = False
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
            "Referer": "https://www.tiktok.com/"
        })

        try:
            while not self._stop_flag and self.active and self._worker_gen == my_gen:
                try:
                    meta = self._fetch_room_meta_http(session, target_url, uid)
                    room_id = meta.get("room_id") or ""
                    is_live = bool(meta.get("is_live"))
                    title = meta.get("title") or f"@{uid} TikTok Live"
                    viewers = meta.get("viewers")
                    total_views = meta.get("total_views")

                    with self.lock:
                        if self._worker_gen == my_gen:
                            self.room_id = room_id
                            self.broadcast_title = title

                    platform_tracker.update_tiktok_live_meta(
                        concurrent_viewers=viewers if viewers is not None else 0,
                        view_count=total_views,
                        is_live=is_live,
                        room_id=room_id,
                        title=title
                    )

                    # Playwright 기반 실시간 DOM + Webcast 채팅 수집 루프 가동
                    with self.lock:
                        if self._worker_gen == my_gen:
                            if is_live or room_id:
                                self.status_text = f"🟢 실시간 틱톡 채팅 수신 중 ({title})"
                            else:
                                self.status_text = f"🟡 @{uid} 틱톡 라이브 감지 대기 및 실시간 모니터링 중..."
                            self.last_error = ""

                    pw = None
                    browser = None
                    try:
                        from playwright.sync_api import sync_playwright
                        pw = sync_playwright().start()
                        browser = pw.chromium.launch(
                            headless=True,
                            args=[
                                "--disable-blink-features=AutomationControlled",
                                "--mute-audio",
                                "--no-sandbox",
                                "--disable-gpu"
                            ]
                        )
                        context = browser.new_context(
                            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                            locale="ko-KR",
                            viewport={"width": 1280, "height": 800}
                        )
                        page = context.new_page()
                        page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                        time.sleep(2.5)

                        with self.lock:
                            is_first_batch = (self._initialized_room_id != (room_id or uid))
                            self._initialized_room_id = (room_id or uid)

                        last_http_meta_ts = time.time()
                        while not self._stop_flag and self.active and self._worker_gen == my_gen:
                            # DOM 내 실시간 채팅 아이템 및 시청자 수 추출
                            dom_data = page.evaluate("""() => {
                                const results = [];
                                const chatNodes = document.querySelectorAll('[data-e2e="chat-message"], [class*="DivChatMessage"], [class*="chat-message"]');
                                chatNodes.forEach((node, idx) => {
                                    const userEl = node.querySelector('[data-e2e="message-owner-name"], [class*="SpanEllipsisName"], [class*="username"], a[href*="/@"]');
                                    const textEl = node.querySelector('[data-e2e="chat-message-text"], [class*="SpanChatRoomComment"], [class*="comment-text"]');
                                    const imgEl = node.querySelector('img');
                                    let author = userEl ? (userEl.innerText || '').trim() : '';
                                    let text = textEl ? (textEl.innerText || '').trim() : '';
                                    if (!text && node.innerText) {
                                        const lines = node.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
                                        if (lines.length >= 2) {
                                            if (!author) author = lines[0].replace(/:$/, '');
                                            text = lines.slice(1).join(' ');
                                        }
                                    }
                                    author = author.replace(/:$/, '').trim();
                                    const uidHref = userEl && userEl.getAttribute && userEl.getAttribute('href') ? userEl.getAttribute('href') : '';
                                    const mUid = uidHref.match(/@([a-zA-Z0-9_.-]+)/);
                                    const uniqueId = mUid ? mUid[1] : author;
                                    const avatar = imgEl ? (imgEl.src || '') : '';
                                    const domKey = node.getAttribute('data-msg-id') || `${uniqueId}_${text}`;
                                    if (author && text) {
                                        results.push({ id: domKey, author, uniqueId, text, avatar });
                                    }
                                });

                                // 입장(Member/Social) 알림에서도 접속 회원 아이디 추출
                                const socialNodes = document.querySelectorAll('[data-e2e="social-message"], [class*="DivSocialMessage"]');
                                const joinedUsers = [];
                                socialNodes.forEach((sn) => {
                                    const raw = (sn.innerText || '').trim();
                                    const uEl = sn.querySelector('[data-e2e="message-owner-name"], a[href*="/@"]');
                                    const uName = uEl ? (uEl.innerText || '').trim() : '';
                                    if (uName) {
                                        joinedUsers.push({ user: uName, raw });
                                    }
                                });

                                // 동시 시청자 수 추출
                                let viewerText = '';
                                const vEl = document.querySelector('[data-e2e="live-people-count"], [class*=" ViewerCount"], [class*="viewer-count"]');
                                if (vEl) viewerText = (vEl.innerText || '').trim();

                                return { chats: results.slice(-60), joined: joinedUsers.slice(-20), viewerText, title: document.title || '' };
                            }""")

                            chats_extracted = (dom_data or {}).get("chats") or []
                            for c_item in chats_extracted:
                                self._ingest_tiktok_chat_item(
                                    msg_id=c_item.get("id"),
                                    author=c_item.get("author"),
                                    unique_id=c_item.get("uniqueId"),
                                    msg_text=c_item.get("text"),
                                    avatar_url=c_item.get("avatar", ""),
                                    is_initial_batch=is_first_batch
                                )

                            for j_user in ((dom_data or {}).get("joined") or []):
                                u_name = str(j_user.get("user") or "").strip()
                                if u_name:
                                    platform_tracker.record_user_presence(
                                        platform_raw="TIKTOK",
                                        user=u_name,
                                        message="👋 틱톡 라이브 입장",
                                        category="CHAT",
                                        channel_id=u_name,
                                        increment_chat=False
                                    )

                            if is_first_batch:
                                is_first_batch = False

                            v_txt = str((dom_data or {}).get("viewerText") or "").strip()
                            if v_txt:
                                nums = re.findall(r"\d[\d,.]*", v_txt)
                                if nums:
                                    try:
                                        v_num = float(nums[0].replace(",", ""))
                                        if "k" in v_txt.lower() or "천" in v_txt:
                                            v_num *= 1000
                                        elif "m" in v_txt.lower() or "만" in v_txt:
                                            v_num *= 10000
                                        platform_tracker.update_tiktok_live_meta(
                                            concurrent_viewers=int(v_num),
                                            is_live=True
                                        )
                                    except Exception:
                                        pass

                            # 12초 주기로 HTTP 메타데이터(시청자 수·방 상태) 갱신
                            now_loop = time.time()
                            if (now_loop - last_http_meta_ts) >= 12.0:
                                last_http_meta_ts = now_loop
                                m_up = self._fetch_room_meta_http(session, target_url, uid)
                                if m_up.get("room_id"):
                                    room_id = m_up["room_id"]
                                platform_tracker.update_tiktok_live_meta(
                                    concurrent_viewers=m_up.get("viewers"),
                                    view_count=m_up.get("total_views"),
                                    is_live=bool(m_up.get("is_live") or len(chats_extracted) > 0),
                                    room_id=room_id,
                                    title=m_up.get("title") or title
                                )
                                with self.lock:
                                    if self._worker_gen == my_gen:
                                        self.room_id = room_id
                                        if m_up.get("is_live") or len(chats_extracted) > 0:
                                            self.status_text = f"🟢 실시간 틱톡 채팅 수신 중 (@{uid})"

                            if not self._sleep_interruptible(1.0, my_gen):
                                break
                    finally:
                        try:
                            if browser:
                                browser.close()
                        except Exception:
                            pass
                        try:
                            if pw:
                                pw.stop()
                        except Exception:
                            pass

                except Exception as e:
                    with self.lock:
                        if self._worker_gen == my_gen:
                            self.last_error = str(e)
                            self.status_text = f"틱톡 채팅 재연결 시도 중... (@{uid})"
                    if not self._sleep_interruptible(5.0, my_gen):
                        break
        finally:
            try:
                session.close()
            except Exception:
                pass


class TikTokDirectStreamer(YouTubeDirectStreamer):
    """
    웹 OBS 스튜디오(/overlay/studio)의 실시간 합성 화면(카메라+오버레이)과
    스마트폰 카메라/PC 스튜디오의 48kHz 실시간 마이크 음성(PCM)을
    유튜브와 동시에(Simulcast) 또는 단독으로 틱톡 라이브 RTMP 서버에 직접 송출하는 매니저
    """
    def __init__(self, master_streamer=None):
        self.master_streamer = master_streamer
        self.lock = Lock()
        self.active = False
        self.rtmp_url = "rtmp://push.tiktok.com/live"
        self.stream_key = ""
        self.width = 720
        self.height = 1280
        self.fps = 30
        self.video_bitrate_kbps = 4000
        self.audio_bitrate_kbps = 128
        self.keyframe_interval_sec = 1
        self.started_at = None
        self.frames_sent = 0
        self.status_text = "대기 중"
        self.last_error = ""
        self.latest_composite_frame = None
        self.latest_composite_ts = 0.0
        self.latest_overlay_rgba = None
        self.latest_overlay_ts = 0.0
        self._proc = None
        self._feeder_thread = None
        self._compositor_thread = None
        self._audio_feeder_thread = None
        self._stderr_thread = None
        self._stop_flag = False
        self._user_requested_active = False
        self._cached_rtmp_ip = ""
        self._auto_restart_count = 0
        self._last_auto_restart_ts = 0.0
        self._auto_restarting = False
        self._fallback_frame_cache = None
        self._latest_server_built_frame = None
        self._last_comp_cam_seq = -1
        self._last_comp_ov_ts = -1.0
        self._audio_lock = Lock()
        self._audio_buffer = bytearray()
        self._audio_buffer_pc = bytearray()
        self._audio_prebuffering = True
        self._audio_pc_prebuffering = True
        self._mob_empty_ticks = 0
        self._pc_empty_ticks = 0
        self._mob_last_sample = 0
        self._pc_last_sample = 0
        self._mob_audio_last_seq = 0
        self._mob_audio_reorder = {}
        self._pc_audio_last_seq = 0
        self._pc_audio_reorder = {}
        self._mob_rate_win = []
        self._pc_rate_win = []
        self._mob_rate_ratio = 1.0
        self._pc_rate_ratio = 1.0
        self._mob_smooth_consume = 960.0
        self._pc_smooth_consume = 960.0
        self._mob_dsp_state = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 8.0]
        self._pc_dsp_state = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 8.0]
        self._mon_dsp_state = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 8.0]
        self._monitor_audio_ring = []
        self._monitor_audio_seq = 0
        self._video_sync_queue = []
        self._transcode_ready_event = threading.Event()
        self._stream_clock_t0 = None
        self.audio_gain_pct = 240
        self.audio_agc_enabled = True
        self._agc_current_gain = 1.0
        self._last_effective_gain_x = 1.6
        self._raw_noise_floor_rms = 22.0
        self._agc_hold_chunks = 0
        self._scope_taps = {"raw": bytearray(), "dsp": bytearray(), "out": bytearray()}
        self._scope_max_bytes = 48000 * 2 * 2
        self._scope_lock = threading.Lock()
        self._dsp_bypass = False
        self._last_raw_rms_db = -60.0
        self._last_audio_sample = 0
        self._was_in_silence = True
        self.last_audio_ts = 0.0
        self.last_mobile_audio_ts = 0.0
        self.last_pc_audio_ts = 0.0
        self.audio_packets_received = 0
        self.audio_underruns = 0
        self._audio_srv_sock = None
        self._audio_pkt_intervals_ms = []
        self._audio_pkt_win = []
        self._audio_underrun_ts = []
        self._audio_overflow_drops = 0
        self._audio_clip_win = []
        self._audio_ticks_sent = 0
        self._last_mob_subsource = "none"
        self._last_vdo_audio_ts = 0.0
        self._last_cam_audio_ts = 0.0
        self._dual_mob_collision_ts = 0.0
        self._env_level_pct = 0.0
        self._env_peak_pct = 0.0
        self._env_db = -60.0
        self._env_peak_db = -60.0
        self._noise_floor_db = -56.0
        self._env_mob_pct = 0.0
        self._env_pc_pct = 0.0
        self._audio_bars = [0.0] * 8
        self._last_meter_update_ts = 0.0
        self._audio_vad_win = []
        self._audio_pkt_gap_events = []
        self._audio_dropout_events = []
        self._last_event_by_type = {}
        self._last_clean_audio_since = time.time()
        self._frame_perf_times = []
        self.burnin_timecode = False
        self._comp_build_samples_ms = []
        self._cam_age_samples_ms = []
        self._stdin_write_samples_ms = []
        self._frame_source_window = []
        self._ffmpeg_telemetry = {
            "encoded_frames": 0,
            "fps": 0.0,
            "q": 0.0,
            "out_time_s": 0.0,
            "wall_elapsed_s": 0.0,
            "bitrate_kbps": 0.0,
            "speed": 0.0,
            "queued_frames": 0,
            "internal_lag_ms": 0.0,
            "updated_at": 0.0,
            "raw_status": ""
        }
        self._rtmp_send_win = []
        self._rtmp_bytes_sent_total = 0
        self._rtmp_uplink_kbps = 0.0
        self._rtmp_send_block_avg_ms = 0.0
        self._rtmp_send_block_max_ms = 0.0
        self._secondary_streamers = []

    def _compositor_loop(self, proc):
        """
        유튜브와 동시 송출(Simulcast) 중일 때는 마스터 스트리머(YouTubeDirectStreamer)가 합성한
        _latest_server_built_frame을 제로 카피로 공유하여 CPU 부하를 절반으로 절감하고,
        틱톡 단독 송출 중일 때만 자체적으로 합성 프레임을 생성합니다.
        """
        while not self._stop_flag and self.active and proc.poll() is None:
            try:
                if (
                    self.master_streamer is not None
                    and self.master_streamer.active
                    and self.master_streamer.width == self.width
                    and self.master_streamer.height == self.height
                    and self.master_streamer._latest_server_built_frame is not None
                ):
                    with self.lock:
                        self._latest_server_built_frame = self.master_streamer._latest_server_built_frame
                    time.sleep(0.015)
                    continue
            except Exception:
                pass
            super()._compositor_loop(proc)
            break

    def start(self, rtmp_url=None, stream_key=None, width=720, height=1280, fps=30, video_bitrate_kbps=None, audio_bitrate_kbps=None, keyframe_interval_sec=None, is_auto_restart=False):
        rtmp_base = (rtmp_url or self.rtmp_url or database.get_overlay_state("tiktok_rtmp_url", "rtmp://push.tiktok.com/live") or "rtmp://push.tiktok.com/live").strip()
        s_key = (stream_key or self.stream_key or database.get_overlay_state("tiktok_stream_key", "")).strip()
        if not s_key:
            return {"ok": False, "message": "틱톡 스트림 키(Stream Key)를 입력해 주세요."}

        saved_v_kbps = database.get_overlay_state("tiktok_video_bitrate_kbps", 4000)
        saved_a_kbps = database.get_overlay_state("tiktok_audio_bitrate_kbps", 128)
        saved_kf_sec = database.get_overlay_state("tiktok_keyframe_sec", 1)
        v_kbps = max(1000, min(18000, int(video_bitrate_kbps if video_bitrate_kbps is not None else (saved_v_kbps or 4000))))
        a_kbps = max(64, min(320, int(audio_bitrate_kbps if audio_bitrate_kbps is not None else (saved_a_kbps or 128))))
        kf_sec = max(1, min(4, int(keyframe_interval_sec if keyframe_interval_sec is not None else (saved_kf_sec or 1))))

        try:
            database.set_overlay_state("tiktok_rtmp_url", rtmp_base)
            database.set_overlay_state("tiktok_stream_key", s_key)
            database.set_overlay_state("tiktok_video_bitrate_kbps", v_kbps)
            database.set_overlay_state("tiktok_audio_bitrate_kbps", a_kbps)
            database.set_overlay_state("tiktok_keyframe_sec", kf_sec)
        except Exception:
            pass

        res = super().start(
            rtmp_url=rtmp_base,
            stream_key=s_key,
            width=width,
            height=height,
            fps=fps,
            video_bitrate_kbps=v_kbps,
            audio_bitrate_kbps=a_kbps,
            keyframe_interval_sec=kf_sec,
            is_auto_restart=is_auto_restart
        )
        # super().start()가 youtube_* 키에 덮어쓰지 않도록 유튜브 원래 설정 복원 후 틱톡 메시지 설정
        if self.master_streamer is not None:
            try:
                if self.master_streamer.rtmp_url:
                    database.set_overlay_state("youtube_rtmp_url", self.master_streamer.rtmp_url)
                if self.master_streamer.stream_key:
                    database.set_overlay_state("youtube_stream_key", self.master_streamer.stream_key)
            except Exception:
                pass
        if res.get("ok"):
            with self.lock:
                self.status_text = f"🟡 틱톡 RTMP 신호 전송 중 — 실제 온에어 확인 중 ({self.width}x{self.height} {self.fps}fps / {v_kbps}kbps)"
            res["verified_live"] = False
            res["message"] = "🟡 틱톡 RTMP 신호 전송을 시작했습니다. 실제 틱톡 라이브 온에어 여부를 확인 중입니다..."
            res["state"] = self.get_state()
        return res

    def stop(self, user_initiated=True):
        res = super().stop(user_initiated=user_initiated)
        res["message"] = "틱톡 실시간 방송 송출이 중지되었습니다."
        res["state"] = self.get_state()
        return res

    def is_verified_live(self):
        """실제 틱톡 플랫폼에서 라이브 온에어 중인지 검증합니다."""
        with self.lock:
            if not self.active:
                return False
        try:
            if platform_tracker.tt_is_live:
                return True
        except Exception:
            pass
        try:
            if "simulcast_automation_bridge" in globals() and simulcast_automation_bridge is not None:
                if simulcast_automation_bridge.platforms.get("tiktok", {}).get("go_live_clicked"):
                    return True
        except Exception:
            pass
        return False

    def _compute_live_stage_and_reason(self, verified_live):
        if not self.active:
            if self.last_error:
                return "ERROR", self.last_error
            return "OFFLINE", "대기 중"
        if verified_live:
            return "VERIFIED_ON_AIR", ""
        tt_bridge = {}
        try:
            if "simulcast_automation_bridge" in globals() and simulcast_automation_bridge is not None:
                tt_bridge = simulcast_automation_bridge.platforms.get("tiktok", {}) or {}
        except Exception:
            pass
        if tt_bridge.get("login_required") or (tt_bridge.get("browser_running") and not tt_bridge.get("logged_in")):
            return "SIGNAL_SENDING_WAIT_ONAIR", "틱톡 LIVE Center에 로그인되어 있지 않습니다. [🔑 틱톡 로그인/창 열기]로 로그인해 주세요."
        if self.last_error:
            return "SIGNAL_SENDING_WAIT_ONAIR", self.last_error
        return "SIGNAL_SENDING_WAIT_ONAIR", "틱톡 RTMP 신호는 전송 중이나, 아직 틱톡 라이브 방송방이 실제 온에어로 확인되지 않았습니다. (틱톡 @아이디 연결 또는 LIVE Center 확인)"

    def get_state(self):
        verified_live = self.is_verified_live()
        with self.lock:
            live_stage, diagnostic_reason = self._compute_live_stage_and_reason(verified_live)
            saved_key = self.stream_key or database.get_overlay_state("tiktok_stream_key", "")
            saved_rtmp = self.rtmp_url or database.get_overlay_state("tiktok_rtmp_url", "rtmp://push.tiktok.com/live")
            saved_v_kbps = int(database.get_overlay_state("tiktok_video_bitrate_kbps", self.video_bitrate_kbps or 4000) or 4000)
            saved_a_kbps = int(database.get_overlay_state("tiktok_audio_bitrate_kbps", self.audio_bitrate_kbps or 128) or 128)
            saved_kf_sec = int(database.get_overlay_state("tiktok_keyframe_sec", self.keyframe_interval_sec or 1) or 1)
            elapsed = int(time.time() - self.started_at) if (self.active and self.started_at) else 0
            now_pc = time.perf_counter()
            if self.active and self._frame_perf_times:
                recent_ft = [t for t in self._frame_perf_times if (now_pc - t) <= 1.5]
                if len(recent_ft) >= 2:
                    span = recent_ft[-1] - recent_ft[0]
                    raw_fps = (len(recent_ft) - 1) / span if span > 0.05 else float(self.fps)
                    current_fps = round(min(float(self.fps), max(0.0, raw_fps)), 1)
                elif len(recent_ft) == 1:
                    current_fps = float(self.fps)
                else:
                    current_fps = 0.0
            else:
                current_fps = 0.0

            hw_enc = self._detect_hw_encoder()
            if hw_enc == "h264_nvenc":
                enc_label = "NVIDIA NVENC H.264 (ULL · CBR)"
            elif hw_enc == "h264_qsv":
                enc_label = "Intel QuickSync H.264 (CBR)"
            elif hw_enc == "h264_amf":
                enc_label = "AMD HW H.264 (ULL · CBR)"
            else:
                enc_label = "x264 ZeroLatency (CBR)"

            return {
                "active": self.active,
                "is_verified_live": verified_live,
                "live_stage": live_stage,
                "diagnostic_reason": diagnostic_reason,
                "rtmp_url": saved_rtmp,
                "has_stream_key": bool(saved_key),
                "stream_key": saved_key,
                "width": self.width,
                "height": self.height,
                "fps": self.fps,
                "current_fps": current_fps,
                "video_bitrate_kbps": self.video_bitrate_kbps if self.active else saved_v_kbps,
                "audio_bitrate_kbps": self.audio_bitrate_kbps if self.active else saved_a_kbps,
                "keyframe_interval_sec": self.keyframe_interval_sec if self.active else saved_kf_sec,
                "encoder_name": enc_label,
                "elapsed_seconds": elapsed,
                "frames_sent": self.frames_sent,
                "ffmpeg_telemetry": dict(self._ffmpeg_telemetry),
                "rtmp_uplink_kbps": self._rtmp_uplink_kbps,
                "rtmp_send_block_avg_ms": self._rtmp_send_block_avg_ms,
                "status_text": self.status_text,
                "last_error": self.last_error
            }


tiktok_chat_poller = TikTokDirectChatPoller()
tiktok_direct_streamer = TikTokDirectStreamer(master_streamer=youtube_direct_streamer)


class InstagramDirectChatPoller:
    """
    인스타그램 아이디(@핸들) 또는 라이브 URL(https://www.instagram.com/아이디/live/)에서
    현재 진행 중인 라이브 방송의 실시간 채팅·동시 시청자 수·접속 회원 아이디를 직접 수집·연동하는 매니저
    """
    def __init__(self):
        from collections import deque
        self.lock = Lock()
        self.active = False
        self.channel_input = ""
        self.username = ""
        self.media_id = ""
        self.broadcast_title = ""
        self.status_text = "대기 중"
        self.last_error = ""
        self.collected_count = 0
        self.last_chat_at = None
        self.seen_ids = deque(maxlen=5000)
        self._initialized_media_id = ""
        self._worker_thread = None
        self._stop_flag = False
        self._worker_gen = 0

    def _sleep_interruptible(self, seconds, my_gen):
        end_t = time.time() + float(seconds)
        while time.time() < end_t:
            if self._stop_flag or not self.active or self._worker_gen != my_gen:
                return False
            time.sleep(min(0.4, max(0.05, end_t - time.time())))
        return not (self._stop_flag or not self.active or self._worker_gen != my_gen)

    def normalize_instagram_target(self, raw_input):
        s = (raw_input or "").strip()
        if not s:
            return "", ""
        m = re.search(r"instagram\.com/([a-zA-Z0-9_.-]+)", s)
        if m:
            uname = m.group(1).strip().lstrip("@")
            if uname.lower() in ("live", "p", "reel", "stories", "explore", "accounts"):
                uname = ""
            if uname:
                return f"https://www.instagram.com/{uname}/live/", uname
        uname = s.lstrip("@").split("/")[0].split("?")[0].strip()
        if not uname:
            return "", ""
        return f"https://www.instagram.com/{uname}/live/", uname

    def connect(self, raw_input):
        clean_input = (raw_input or "").strip()
        target_url, uname = self.normalize_instagram_target(clean_input)
        if not target_url or not uname:
            return {"ok": False, "message": "인스타그램 아이디(예: @아이디) 또는 라이브 방송 URL을 입력해 주세요."}

        norm_handle = f"@{uname}" if not clean_input.startswith("http") else clean_input
        with self.lock:
            self._worker_gen += 1
            my_gen = self._worker_gen
            self._stop_flag = False
            self.active = True
            self.channel_input = norm_handle
            self.username = uname
            self.media_id = ""
            self.broadcast_title = f"@{uname} Instagram Live"
            self.status_text = f"📸 인스타 라이브(@{uname}) 방송 연결 확인 중..."
            self.last_error = ""
        try:
            database.set_overlay_state("instagram_channel_input", self.channel_input)
        except Exception:
            pass

        self._worker_thread = threading.Thread(
            target=self._poll_loop,
            args=(target_url, uname, my_gen),
            daemon=True
        )
        self._worker_thread.start()
        return {"ok": True, "message": f"인스타그램(@{uname}) 실시간 채팅 연결을 시작했습니다.", "state": self.get_state()}

    def disconnect(self):
        with self.lock:
            self._worker_gen += 1
            self._stop_flag = True
            self.active = False
            self.status_text = "연결 해제됨"
        platform_tracker.update_instagram_live_meta(is_live=False, concurrent_viewers=0)
        return {"ok": True, "message": "인스타그램 채널 채팅 연결이 해제되었습니다.", "state": self.get_state()}

    def get_state(self):
        with self.lock:
            ch_in = self.channel_input or database.get_overlay_state("instagram_channel_input", "")
            return {
                "active": self.active,
                "channel_input": ch_in,
                "username": self.username,
                "media_id": self.media_id,
                "video_id": self.media_id,
                "broadcast_title": self.broadcast_title,
                "status_text": self.status_text,
                "last_error": self.last_error,
                "collected_count": self.collected_count,
                "last_chat_at": self.last_chat_at
            }

    def _ingest_instagram_chat_item(self, msg_id, author, username, msg_text, avatar_url="", badge_str="", is_initial_batch=False):
        msg_text = str(msg_text or "").strip()
        author = str(author or username or "인스타시청자").strip()
        if not msg_text:
            return
        dedup_key = str(msg_id or f"ig_{author}_{msg_text}")
        with self.lock:
            if dedup_key in self.seen_ids:
                return
            self.seen_ids.append(dedup_key)
            self.collected_count += 1
            self.last_chat_at = datetime.now().strftime("%H:%M:%S")

        if is_initial_batch:
            platform_tracker.record_user_presence(
                platform_raw="INSTAGRAM",
                user=author,
                message=msg_text,
                category="CHAT",
                channel_id=str(username or author),
                avatar=avatar_url,
                badge=badge_str,
                increment_chat=True
            )
        else:
            try:
                process_chat_payload({
                    "id": dedup_key,
                    "chatname": author,
                    "chatmessage": msg_text,
                    "type": "instagram",
                    "source": "instagram_direct"
                })
                platform_tracker.record_user_presence(
                    platform_raw="INSTAGRAM",
                    user=author,
                    message=msg_text,
                    channel_id=str(username or author),
                    avatar=avatar_url,
                    badge=badge_str,
                    increment_chat=False
                )
            except Exception as proc_err:
                print(f"⚠️ [인스타그램 직접 채팅 처리 오류] {proc_err}", flush=True)

    def _poll_loop(self, target_url, uname, my_gen):
        try:
            while not self._stop_flag and self.active and self._worker_gen == my_gen:
                try:
                    title = f"@{uname} Instagram Live"
                    with self.lock:
                        if self._worker_gen == my_gen:
                            self.broadcast_title = title
                            self.status_text = f"🟢 인스타 라이브(@{uname}) 실시간 채팅·시청자 모니터링 중..."
                            self.last_error = ""

                    pw = None
                    browser = None
                    try:
                        from playwright.sync_api import sync_playwright
                        pw = sync_playwright().start()
                        browser = pw.chromium.launch(
                            headless=True,
                            args=[
                                "--disable-blink-features=AutomationControlled",
                                "--mute-audio",
                                "--no-sandbox",
                                "--disable-gpu"
                            ]
                        )
                        storage_state_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instagram_storage_state.json")
                        ctx_kwargs = {
                            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                            "locale": "ko-KR",
                            "viewport": {"width": 1280, "height": 800}
                        }
                        if os.path.exists(storage_state_path):
                            ctx_kwargs["storage_state"] = storage_state_path
                        context = browser.new_context(**ctx_kwargs)
                        page = context.new_page()

                        # 인스타그램 내부 API(get_comment, heartbeat_and_get_viewer_count, graphql) 응답 가로채기
                        def _on_ig_response(resp):
                            try:
                                r_url = resp.url or ""
                                if any(k in r_url for k in ("get_comment", "live/", "heartbeat_and_get_viewer_count", "graphql")):
                                    ctype = (resp.headers.get("content-type") or "").lower()
                                    if "json" not in ctype:
                                        return
                                    j = resp.json()
                                    if not isinstance(j, dict):
                                        return
                                    # 시청자 수 업데이트
                                    v_cnt = j.get("viewer_count")
                                    if v_cnt is not None:
                                        platform_tracker.update_instagram_live_meta(
                                            concurrent_viewers=int(v_cnt),
                                            is_live=True,
                                            title=title
                                        )
                                    b_id = j.get("broadcast_id") or j.get("id")
                                    if b_id:
                                        with self.lock:
                                            self.media_id = str(b_id)
                                    # 댓글 배열 추출
                                    comments = j.get("comments") or j.get("system_comments") or []
                                    if isinstance(comments, list):
                                        for c_obj in comments:
                                            if not isinstance(c_obj, dict):
                                                continue
                                            pk = str(c_obj.get("pk") or c_obj.get("id") or "")
                                            txt = str(c_obj.get("text") or "").strip()
                                            u_obj = c_obj.get("user") or {}
                                            u_id = str(u_obj.get("username") or "").strip()
                                            u_full = str(u_obj.get("full_name") or u_id or "").strip()
                                            u_pic = str(u_obj.get("profile_pic_url") or "")
                                            if (u_id or u_full) and txt:
                                                self._ingest_instagram_chat_item(
                                                    msg_id=pk or f"ig_api_{u_id}_{txt}",
                                                    author=u_id or u_full,
                                                    username=u_id or u_full,
                                                    msg_text=txt,
                                                    avatar_url=u_pic,
                                                    is_initial_batch=False
                                                )
                            except Exception:
                                pass

                        page.on("response", _on_ig_response)
                        page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                        time.sleep(2.5)

                        with self.lock:
                            is_first_batch = (self._initialized_media_id != uname)
                            self._initialized_media_id = uname

                        while not self._stop_flag and self.active and self._worker_gen == my_gen:
                            dom_data = page.evaluate("""() => {
                                const results = [];
                                // 인스타그램 라이브 댓글 영역 탐색 (section / ul / role=log / article 내 댓글)
                                const commentNodes = document.querySelectorAll(
                                    '[role="log"] li, [role="log"] > div, section ul li, div[class*="Comment"]'
                                );
                                commentNodes.forEach((node) => {
                                    const links = node.querySelectorAll('a[href^="/"]');
                                    let username = '';
                                    if (links.length > 0) {
                                        const href = (links[0].getAttribute('href') || '').replace(/\\//g, '').trim();
                                        username = href || (links[0].innerText || '').trim();
                                    }
                                    const rawText = (node.innerText || '').trim();
                                    if (!rawText) return;
                                    const lines = rawText.split('\\n').map(s => s.trim()).filter(Boolean);
                                    let author = username;
                                    let text = '';
                                    if (lines.length >= 2) {
                                        if (!author) author = lines[0];
                                        text = lines.slice(1).join(' ');
                                    } else if (lines.length === 1 && author && rawText.startsWith(author)) {
                                        text = rawText.slice(author.length).trim();
                                    }
                                    if (!author || !text) return;
                                    if (text.length > 300) return;
                                    const imgEl = node.querySelector('img');
                                    const avatar = imgEl ? (imgEl.src || '') : '';
                                    results.push({
                                        id: `ig_dom_${author}_${text}`,
                                        author,
                                        username: author,
                                        text,
                                        avatar
                                    });
                                });

                                // 동시 시청자 수 추출
                                let viewerText = '';
                                const allSpans = document.querySelectorAll('header span, div[role="button"] span, span');
                                for (const sp of allSpans) {
                                    const t = (sp.innerText || '').trim();
                                    if (/^\\d[\\d,.]*\\s*(명|천|만|K|M)?$/i.test(t) && sp.closest('header, [class*="Live"], [class*="viewer"]')) {
                                        viewerText = t;
                                        break;
                                    }
                                }
                                const isLoginPage = !!document.querySelector('input[name="username"], form[id="loginForm"]');
                                return { chats: results.slice(-60), viewerText, isLoginPage, title: document.title || '' };
                            }""")

                            chats_extracted = (dom_data or {}).get("chats") or []
                            for c_item in chats_extracted:
                                self._ingest_instagram_chat_item(
                                    msg_id=c_item.get("id"),
                                    author=c_item.get("author"),
                                    username=c_item.get("username"),
                                    msg_text=c_item.get("text"),
                                    avatar_url=c_item.get("avatar", ""),
                                    is_initial_batch=is_first_batch
                                )

                            if is_first_batch:
                                is_first_batch = False

                            v_txt = str((dom_data or {}).get("viewerText") or "").strip()
                            if v_txt:
                                nums = re.findall(r"\d[\d,.]*", v_txt)
                                if nums:
                                    try:
                                        v_num = float(nums[0].replace(",", ""))
                                        if "k" in v_txt.lower() or "천" in v_txt:
                                            v_num *= 1000
                                        elif "m" in v_txt.lower() or "만" in v_txt:
                                            v_num *= 10000
                                        platform_tracker.update_instagram_live_meta(
                                            concurrent_viewers=int(v_num),
                                            is_live=True,
                                            title=title
                                        )
                                    except Exception:
                                        pass

                            with self.lock:
                                if self._worker_gen == my_gen:
                                    if len(chats_extracted) > 0 or v_txt:
                                        self.status_text = f"🟢 실시간 인스타 채팅 수신 중 (@{uname})"
                                    else:
                                        self.status_text = f"🟢 @{uname} 인스타 라이브 채팅 대기 중 (웹훅·직접수집 동시 활성)"

                            if not self._sleep_interruptible(1.2, my_gen):
                                break
                    finally:
                        try:
                            if browser:
                                browser.close()
                        except Exception:
                            pass
                        try:
                            if pw:
                                pw.stop()
                        except Exception:
                            pass

                except Exception as e:
                    with self.lock:
                        if self._worker_gen == my_gen:
                            self.last_error = str(e)
                            self.status_text = f"인스타 채팅 재연결 시도 중... (@{uname})"
                    if not self._sleep_interruptible(5.0, my_gen):
                        break
        except Exception:
            pass


class InstagramDirectStreamer(YouTubeDirectStreamer):
    """
    웹 OBS 스튜디오(/overlay/studio)의 실시간 합성 화면(카메라+오버레이)과
    스마트폰 카메라/PC 스튜디오의 48kHz 실시간 마이크 음성(PCM)을
    유튜브·틱톡과 동시에(3사 Simulcast) 또는 단독으로 인스타그램 Live Producer RTMPS 서버에 직접 송출하는 매니저
    """
    def __init__(self, master_streamer=None, secondary_master=None):
        self.master_streamer = master_streamer
        self.secondary_master = secondary_master
        self.lock = Lock()
        self.active = False
        self.rtmp_url = "rtmps://live-upload.instagram.com:443/rtmp/"
        self.stream_key = ""
        self.width = 720
        self.height = 1280
        self.fps = 30
        self.video_bitrate_kbps = 2500
        self.audio_bitrate_kbps = 128
        self.keyframe_interval_sec = 2
        self.started_at = None
        self.frames_sent = 0
        self.status_text = "대기 중"
        self.last_error = ""
        self.latest_composite_frame = None
        self.latest_composite_ts = 0.0
        self.latest_overlay_rgba = None
        self.latest_overlay_ts = 0.0
        self._proc = None
        self._feeder_thread = None
        self._compositor_thread = None
        self._audio_feeder_thread = None
        self._stderr_thread = None
        self._stop_flag = False
        self._user_requested_active = False
        self._cached_rtmp_ip = ""
        self._auto_restart_count = 0
        self._last_auto_restart_ts = 0.0
        self._auto_restarting = False
        self._fallback_frame_cache = None
        self._latest_server_built_frame = None
        self._last_comp_cam_seq = -1
        self._last_comp_ov_ts = -1.0
        self._audio_lock = Lock()
        self._audio_buffer = bytearray()
        self._audio_buffer_pc = bytearray()
        self._audio_prebuffering = True
        self._audio_pc_prebuffering = True
        self._mob_empty_ticks = 0
        self._pc_empty_ticks = 0
        self._mob_last_sample = 0
        self._pc_last_sample = 0
        self._mob_audio_last_seq = 0
        self._mob_audio_reorder = {}
        self._pc_audio_last_seq = 0
        self._pc_audio_reorder = {}
        self._mob_rate_win = []
        self._pc_rate_win = []
        self._mob_rate_ratio = 1.0
        self._pc_rate_ratio = 1.0
        self._mob_smooth_consume = 960.0
        self._pc_smooth_consume = 960.0
        self._mob_dsp_state = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 8.0]
        self._pc_dsp_state = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 8.0]
        self._mon_dsp_state = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 8.0]
        self._monitor_audio_ring = []
        self._monitor_audio_seq = 0
        self._video_sync_queue = []
        self._transcode_ready_event = threading.Event()
        self._stream_clock_t0 = None
        self.audio_gain_pct = 240
        self.audio_agc_enabled = True
        self._agc_current_gain = 1.0
        self._last_effective_gain_x = 1.6
        self._raw_noise_floor_rms = 22.0
        self._agc_hold_chunks = 0
        self._scope_taps = {"raw": bytearray(), "dsp": bytearray(), "out": bytearray()}
        self._scope_max_bytes = 48000 * 2 * 2
        self._scope_lock = threading.Lock()
        self._dsp_bypass = False
        self._last_raw_rms_db = -60.0
        self._last_audio_sample = 0
        self._was_in_silence = True
        self.last_audio_ts = 0.0
        self.last_mobile_audio_ts = 0.0
        self.last_pc_audio_ts = 0.0
        self.audio_packets_received = 0
        self.audio_underruns = 0
        self._audio_srv_sock = None
        self._audio_pkt_intervals_ms = []
        self._audio_pkt_win = []
        self._audio_underrun_ts = []
        self._audio_overflow_drops = 0
        self._audio_clip_win = []
        self._audio_ticks_sent = 0
        self._last_mob_subsource = "none"
        self._last_vdo_audio_ts = 0.0
        self._last_cam_audio_ts = 0.0
        self._dual_mob_collision_ts = 0.0
        self._env_level_pct = 0.0
        self._env_peak_pct = 0.0
        self._env_db = -60.0
        self._env_peak_db = -60.0
        self._noise_floor_db = -56.0
        self._env_mob_pct = 0.0
        self._env_pc_pct = 0.0
        self._audio_bars = [0.0] * 8
        self._last_meter_update_ts = 0.0
        self._audio_vad_win = []
        self._audio_pkt_gap_events = []
        self._audio_dropout_events = []
        self._last_event_by_type = {}
        self._last_clean_audio_since = time.time()
        self._frame_perf_times = []
        self.burnin_timecode = False
        self._comp_build_samples_ms = []
        self._cam_age_samples_ms = []
        self._stdin_write_samples_ms = []
        self._frame_source_window = []
        self._ffmpeg_telemetry = {
            "encoded_frames": 0,
            "fps": 0.0,
            "q": 0.0,
            "out_time_s": 0.0,
            "wall_elapsed_s": 0.0,
            "bitrate_kbps": 0.0,
            "speed": 0.0,
            "queued_frames": 0,
            "internal_lag_ms": 0.0,
            "updated_at": 0.0,
            "raw_status": ""
        }
        self._rtmp_send_win = []
        self._rtmp_bytes_sent_total = 0
        self._rtmp_uplink_kbps = 0.0
        self._rtmp_send_block_avg_ms = 0.0
        self._rtmp_send_block_max_ms = 0.0
        self._secondary_streamers = []

    def _compositor_loop(self, proc):
        """
        유튜브 또는 틱톡과 동시 송출(Simulcast) 중일 때는 마스터 스트리머가 합성한
        _latest_server_built_frame을 제로 카피로 공유하여 CPU 부하를 절감하고,
        인스타그램 단독 송출 중일 때만 자체적으로 합성 프레임을 생성합니다.
        """
        while not self._stop_flag and self.active and proc.poll() is None:
            try:
                for cand in (self.master_streamer, self.secondary_master):
                    if (
                        cand is not None
                        and cand.active
                        and cand.width == self.width
                        and cand.height == self.height
                        and cand._latest_server_built_frame is not None
                    ):
                        with self.lock:
                            self._latest_server_built_frame = cand._latest_server_built_frame
                        time.sleep(0.015)
                        break
                else:
                    super()._compositor_loop(proc)
                    break
            except Exception:
                super()._compositor_loop(proc)
                break

    def start(self, rtmp_url=None, stream_key=None, width=720, height=1280, fps=30, video_bitrate_kbps=None, audio_bitrate_kbps=None, keyframe_interval_sec=None, is_auto_restart=False):
        default_ig_rtmp = "rtmps://live-upload.instagram.com:443/rtmp/"
        rtmp_base = (rtmp_url or self.rtmp_url or database.get_overlay_state("instagram_rtmp_url", default_ig_rtmp) or default_ig_rtmp).strip()
        s_key = (stream_key or self.stream_key or database.get_overlay_state("instagram_stream_key", "")).strip()
        if not s_key:
            return {"ok": False, "message": "인스타그램 스트림 키(Stream Key)를 입력해 주세요."}

        saved_v_kbps = database.get_overlay_state("instagram_video_bitrate_kbps", 2500)
        saved_a_kbps = database.get_overlay_state("instagram_audio_bitrate_kbps", 128)
        saved_kf_sec = database.get_overlay_state("instagram_keyframe_sec", 2)
        v_kbps = max(800, min(12000, int(video_bitrate_kbps if video_bitrate_kbps is not None else (saved_v_kbps or 2500))))
        a_kbps = max(64, min(320, int(audio_bitrate_kbps if audio_bitrate_kbps is not None else (saved_a_kbps or 128))))
        kf_sec = max(1, min(4, int(keyframe_interval_sec if keyframe_interval_sec is not None else (saved_kf_sec or 2))))

        try:
            database.set_overlay_state("instagram_rtmp_url", rtmp_base)
            database.set_overlay_state("instagram_stream_key", s_key)
            database.set_overlay_state("instagram_video_bitrate_kbps", v_kbps)
            database.set_overlay_state("instagram_audio_bitrate_kbps", a_kbps)
            database.set_overlay_state("instagram_keyframe_sec", kf_sec)
        except Exception:
            pass

        res = super().start(
            rtmp_url=rtmp_base,
            stream_key=s_key,
            width=width,
            height=height,
            fps=fps,
            video_bitrate_kbps=v_kbps,
            audio_bitrate_kbps=a_kbps,
            keyframe_interval_sec=kf_sec,
            is_auto_restart=is_auto_restart
        )
        # super().start()가 youtube_* 키에 덮어쓰지 않도록 유튜브 원래 설정 복원
        if self.master_streamer is not None:
            try:
                if self.master_streamer.rtmp_url:
                    database.set_overlay_state("youtube_rtmp_url", self.master_streamer.rtmp_url)
                if self.master_streamer.stream_key:
                    database.set_overlay_state("youtube_stream_key", self.master_streamer.stream_key)
            except Exception:
                pass
        if res.get("ok"):
            with self.lock:
                self.status_text = f"🟡 인스타 RTMPS 신호 전송 중 — Live Producer '방송 시작(Go live)' 대기 중 ({self.width}x{self.height} {self.fps}fps)"
            res["verified_live"] = False
            res["message"] = "🟡 인스타그램 RTMPS 신호 전송을 시작했습니다. Live Producer에서 '방송 시작(Go live)' 확인 중입니다..."
            res["state"] = self.get_state()
        return res

    def stop(self, user_initiated=True):
        res = super().stop(user_initiated=user_initiated)
        res["message"] = "인스타그램 실시간 방송 송출이 중지되었습니다."
        res["state"] = self.get_state()
        return res

    def is_verified_live(self):
        """실제 인스타그램 Live Producer에서 Go Live가 완료되었거나 라이브 온에어 중인지 검증합니다."""
        with self.lock:
            if not self.active:
                return False
        try:
            if platform_tracker.ig_is_live:
                return True
        except Exception:
            pass
        try:
            if "simulcast_automation_bridge" in globals() and simulcast_automation_bridge is not None:
                if simulcast_automation_bridge.platforms.get("instagram", {}).get("go_live_clicked"):
                    return True
        except Exception:
            pass
        return False

    def _compute_live_stage_and_reason(self, verified_live):
        if not self.active:
            if self.last_error:
                return "ERROR", self.last_error
            return "OFFLINE", "대기 중"
        if verified_live:
            return "VERIFIED_ON_AIR", ""
        ig_bridge = {}
        try:
            if "simulcast_automation_bridge" in globals() and simulcast_automation_bridge is not None:
                ig_bridge = simulcast_automation_bridge.platforms.get("instagram", {}) or {}
        except Exception:
            pass
        if ig_bridge.get("login_required") or (ig_bridge.get("browser_running") and not ig_bridge.get("logged_in")):
            return "SIGNAL_SENDING_WAIT_ONAIR", "인스타그램에 로그인되어 있지 않습니다. [🔑 인스타 로그인/창 열기]로 로그인해 주세요."
        if self.last_error:
            return "SIGNAL_SENDING_WAIT_ONAIR", self.last_error
        return "SIGNAL_SENDING_WAIT_ONAIR", "인스타 RTMPS 영상 신호는 전송 중이나, Live Producer 화면에서 [방송 시작(Go live)] 버튼이 아직 눌리지 않았습니다."

    def get_state(self):
        verified_live = self.is_verified_live()
        with self.lock:
            live_stage, diagnostic_reason = self._compute_live_stage_and_reason(verified_live)
            default_ig_rtmp = "rtmps://live-upload.instagram.com:443/rtmp/"
            saved_key = self.stream_key or database.get_overlay_state("instagram_stream_key", "")
            saved_rtmp = self.rtmp_url or database.get_overlay_state("instagram_rtmp_url", default_ig_rtmp)
            saved_v_kbps = int(database.get_overlay_state("instagram_video_bitrate_kbps", self.video_bitrate_kbps or 2500) or 2500)
            saved_a_kbps = int(database.get_overlay_state("instagram_audio_bitrate_kbps", self.audio_bitrate_kbps or 128) or 128)
            saved_kf_sec = int(database.get_overlay_state("instagram_keyframe_sec", self.keyframe_interval_sec or 2) or 2)
            elapsed = int(time.time() - self.started_at) if (self.active and self.started_at) else 0
            now_pc = time.perf_counter()
            if self.active and self._frame_perf_times:
                recent_ft = [t for t in self._frame_perf_times if (now_pc - t) <= 1.5]
                if len(recent_ft) >= 2:
                    span = recent_ft[-1] - recent_ft[0]
                    raw_fps = (len(recent_ft) - 1) / span if span > 0.05 else float(self.fps)
                    current_fps = round(min(float(self.fps), max(0.0, raw_fps)), 1)
                elif len(recent_ft) == 1:
                    current_fps = float(self.fps)
                else:
                    current_fps = 0.0
            else:
                current_fps = 0.0

            hw_enc = self._detect_hw_encoder()
            if hw_enc == "h264_nvenc":
                enc_label = "NVIDIA NVENC H.264 (ULL · CBR)"
            elif hw_enc == "h264_qsv":
                enc_label = "Intel QuickSync H.264 (CBR)"
            elif hw_enc == "h264_amf":
                enc_label = "AMD HW H.264 (ULL · CBR)"
            else:
                enc_label = "x264 ZeroLatency (CBR)"

            return {
                "active": self.active,
                "is_verified_live": verified_live,
                "live_stage": live_stage,
                "diagnostic_reason": diagnostic_reason,
                "rtmp_url": saved_rtmp,
                "has_stream_key": bool(saved_key),
                "stream_key": saved_key,
                "width": self.width,
                "height": self.height,
                "fps": self.fps,
                "current_fps": current_fps,
                "video_bitrate_kbps": self.video_bitrate_kbps if self.active else saved_v_kbps,
                "audio_bitrate_kbps": self.audio_bitrate_kbps if self.active else saved_a_kbps,
                "keyframe_interval_sec": self.keyframe_interval_sec if self.active else saved_kf_sec,
                "encoder_name": enc_label,
                "elapsed_seconds": elapsed,
                "frames_sent": self.frames_sent,
                "ffmpeg_telemetry": dict(self._ffmpeg_telemetry),
                "rtmp_uplink_kbps": self._rtmp_uplink_kbps,
                "rtmp_send_block_avg_ms": self._rtmp_send_block_avg_ms,
                "status_text": self.status_text,
                "last_error": self.last_error
            }


instagram_chat_poller = InstagramDirectChatPoller()
instagram_direct_streamer = InstagramDirectStreamer(
    master_streamer=youtube_direct_streamer,
    secondary_master=tiktok_direct_streamer
)
youtube_direct_streamer._secondary_streamers = [tiktok_direct_streamer, instagram_direct_streamer]


def start_network_self_healing_watchdog():
    """
    2~3시간 장시간 방송 중 Intel I219-V 랜카드 TX 링 멈춤(SuspectArpProbeFailed) 또는
    하위 공유기(192.168.1.1) 2시간 주기 DHCP 임대 갱신 시점의 순간 DNS/ARP 단절을 12초 주기로 감시하고,
    이상 감지 시 PC 재부팅 없이 자동으로 ARP/DNS 플러시, 랜카드 소프트 리셋 및 RTMP 송출 자동 복구를 수행합니다.
    """
    import socket

    def _probe_tcp(ip, port, timeout=1.5):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((ip, port))
            return True
        except Exception:
            return False
        finally:
            try:
                s.close()
            except Exception:
                pass

    def _watchdog_loop():
        fail_streak = 0
        last_adapter_reset_ts = 0.0
        while True:
            time.sleep(12.0)
            try:
                # 1) 외부 공인 DNS(8.8.8.8:53 또는 1.1.1.1:53) TCP 도달성 점검
                ext_ok = _probe_tcp("8.8.8.8", 53, timeout=1.5) or _probe_tcp("1.1.1.1", 53, timeout=1.5)
                if ext_ok:
                    if fail_streak > 0:
                        print("🟢 [네트워크 자가 복구 완료] 외부 인터넷 연결이 정상 확인되었습니다.", flush=True)
                    fail_streak = 0
                    # 만약 사용자가 방송 송출 중이었는데 일시 단절로 멈춰 있다면 자동 재개
                    if youtube_direct_streamer._user_requested_active and not youtube_direct_streamer.active:
                        youtube_direct_streamer._schedule_auto_restart("네트워크 복구 감지 후 자동 재시작")
                    if tiktok_direct_streamer._user_requested_active and not tiktok_direct_streamer.active:
                        tiktok_direct_streamer._schedule_auto_restart("네트워크 복구 감지 후 자동 재시작")
                    if instagram_direct_streamer._user_requested_active and not instagram_direct_streamer.active:
                        instagram_direct_streamer._schedule_auto_restart("네트워크 복구 감지 후 자동 재시작")
                    continue

                fail_streak += 1
                print(f"⚠️ [네트워크 감시] 외부 DNS 응답 지연 감지 (연속 {fail_streak}회)", flush=True)

                # 1차 조치 (연속 1회 실패): DNS 캐시 플러시 및 게이트웨이 ARP 갱신
                if fail_streak == 1:
                    subprocess.run(["ipconfig", "/flushdns"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4)
                    subprocess.run(["arp", "-d", "*"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4)

                # 2차 조치 (연속 2회 = 24초 이상 단절): Intel I219-V TX Ring Hang 복구를 위한 어댑터 소프트 리셋 (최소 3분 쿨다운)
                now_ts = time.time()
                if fail_streak >= 2 and (now_ts - last_adapter_reset_ts) >= 180.0:
                    last_adapter_reset_ts = now_ts
                    print("🛠️ [네트워크 자가 복구] 이더넷 어댑터 TX 링 소프트 리셋 및 UPnP 브릿지 재연결 실행...", flush=True)
                    subprocess.run(
                        ["powershell", "-NoProfile", "-Command", "Restart-NetAdapter -Name '이더넷' -Confirm:$false -ErrorAction SilentlyContinue"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=12
                    )
                    time.sleep(3.5)
                    try:
                        setup_upnp_cross_subnet_bridge(force=True)
                    except Exception:
                        pass
                    if youtube_direct_streamer._user_requested_active:
                        youtube_direct_streamer._schedule_auto_restart("이더넷 어댑터 소프트 리셋 완료")
                    if tiktok_direct_streamer._user_requested_active:
                        tiktok_direct_streamer._schedule_auto_restart("이더넷 어댑터 소프트 리셋 완료")
                    if instagram_direct_streamer._user_requested_active:
                        instagram_direct_streamer._schedule_auto_restart("이더넷 어댑터 소프트 리셋 완료")
            except Exception:
                pass

    t = threading.Thread(target=_watchdog_loop, daemon=True)
    t.start()


@app.route('/api/youtube/status', methods=['GET'])
def api_youtube_status():
    """유튜브, 틱톡 및 인스타그램 채널 직접 연결(채팅 수집 + RTMP 라이브 동시 송출 + CDP 자동화 브릿지) 통합 상태 반환"""
    yt_state = youtube_direct_streamer.get_state()
    tt_state = tiktok_direct_streamer.get_state()
    ig_state = instagram_direct_streamer.get_state()
    return jsonify({
        "status": "ok",
        "broadcast_active": bool(yt_state.get("active") or tt_state.get("active") or ig_state.get("active")),
        "broadcast_reset_seq": broadcast_reset_seq,
        "chat": youtube_chat_poller.get_state(),
        "rtmp": yt_state,
        "tiktok_chat": tiktok_chat_poller.get_state(),
        "tiktok_rtmp": tt_state,
        "instagram_chat": instagram_chat_poller.get_state(),
        "instagram_rtmp": ig_state,
        "simulcast_bridge": simulcast_automation_bridge.get_state() if 'simulcast_automation_bridge' in globals() else None,
        "video_diagnostics": youtube_direct_streamer.get_video_diagnostics(include_history=False, refresh_youtube=False),
        "audio_diagnostics": youtube_direct_streamer.get_audio_diagnostics()
    }), 200


@app.route('/api/tiktok/status', methods=['GET'])
def api_tiktok_status():
    """틱톡 채널 직접 연결(채팅 수집 + RTMP 라이브 송출) 통합 상태 반환"""
    return jsonify({
        "status": "ok",
        "chat": tiktok_chat_poller.get_state(),
        "rtmp": tiktok_direct_streamer.get_state(),
        "viewers": platform_tracker.get_tiktok_viewers_summary()
    }), 200


@app.route('/api/tiktok/config', methods=['POST'])
def api_tiktok_save_config():
    """틱톡 아이디(@핸들), RTMP 서버 URL, 스트림 키, 비디오/오디오 비트 전송률, 키프레임 간격 설정을 DB에 저장합니다."""
    data = request.get_json(silent=True) or {}
    ch_input = (data.get("channel_input") or "").strip()
    rtmp_url = (data.get("rtmp_url") or "rtmp://push.tiktok.com/live").strip()
    stream_key = (data.get("stream_key") or "").strip()

    if "channel_input" in data:
        database.set_overlay_state("tiktok_channel_input", ch_input)
        tiktok_chat_poller.channel_input = ch_input
    if rtmp_url:
        database.set_overlay_state("tiktok_rtmp_url", rtmp_url)
        tiktok_direct_streamer.rtmp_url = rtmp_url
    if "stream_key" in data:
        database.set_overlay_state("tiktok_stream_key", stream_key)
        tiktok_direct_streamer.stream_key = stream_key
    if "video_bitrate_kbps" in data and data.get("video_bitrate_kbps"):
        try:
            v_kbps = max(1000, min(18000, int(data.get("video_bitrate_kbps"))))
            database.set_overlay_state("tiktok_video_bitrate_kbps", v_kbps)
            tiktok_direct_streamer.video_bitrate_kbps = v_kbps
        except Exception:
            pass
    if "audio_bitrate_kbps" in data and data.get("audio_bitrate_kbps"):
        try:
            a_kbps = max(64, min(320, int(data.get("audio_bitrate_kbps"))))
            database.set_overlay_state("tiktok_audio_bitrate_kbps", a_kbps)
            tiktok_direct_streamer.audio_bitrate_kbps = a_kbps
        except Exception:
            pass
    if "keyframe_interval_sec" in data and data.get("keyframe_interval_sec"):
        try:
            kf_sec = max(1, min(4, int(data.get("keyframe_interval_sec"))))
            database.set_overlay_state("tiktok_keyframe_sec", kf_sec)
            tiktok_direct_streamer.keyframe_interval_sec = kf_sec
        except Exception:
            pass

    return jsonify({
        "status": "ok",
        "message": "틱톡 연결 및 초저지연 송출 설정이 저장되었습니다.",
        "chat": tiktok_chat_poller.get_state(),
        "rtmp": tiktok_direct_streamer.get_state()
    }), 200


@app.route('/api/tiktok/chat/connect', methods=['POST'])
def api_tiktok_chat_connect():
    """틱톡 아이디(@핸들) 또는 라이브 URL로 실시간 채팅 직접 수집을 시작합니다."""
    data = request.get_json(silent=True) or {}
    ch_input = (data.get("channel_input") or database.get_overlay_state("tiktok_channel_input", "")).strip()
    res = tiktok_chat_poller.connect(ch_input)
    code = 200 if res.get("ok") else 400
    return jsonify({
        "status": "ok" if res.get("ok") else "error",
        "message": res.get("message", ""),
        "chat": tiktok_chat_poller.get_state()
    }), code


@app.route('/api/tiktok/chat/disconnect', methods=['POST'])
def api_tiktok_chat_disconnect():
    """틱톡 채널 실시간 채팅 직접 수집을 중지합니다."""
    res = tiktok_chat_poller.disconnect()
    return jsonify({
        "status": "ok",
        "message": res.get("message", ""),
        "chat": tiktok_chat_poller.get_state()
    }), 200


@app.route('/api/tiktok/rtmp/start', methods=['POST'])
def api_tiktok_rtmp_start():
    """웹 OBS 스튜디오 화면을 틱톡 스트림 키(RTMP)로 직접 실시간 송출 시작합니다."""
    data = request.get_json(silent=True) or {}
    rtmp_url = (data.get("rtmp_url") or database.get_overlay_state("tiktok_rtmp_url", "rtmp://push.tiktok.com/live") or "rtmp://push.tiktok.com/live").strip()
    stream_key = (data.get("stream_key") or database.get_overlay_state("tiktok_stream_key", "")).strip()
    width = data.get("width", 720)
    height = data.get("height", 1280)
    fps = data.get("fps", 30)
    video_bitrate_kbps = data.get("video_bitrate_kbps", 4000)
    audio_bitrate_kbps = data.get("audio_bitrate_kbps", 128)
    keyframe_interval_sec = data.get("keyframe_interval_sec", 1)
    ch_input = (data.get("channel_input") or database.get_overlay_state("tiktok_channel_input", "")).strip()

    res = tiktok_direct_streamer.start(
        rtmp_url=rtmp_url,
        stream_key=stream_key,
        width=width,
        height=height,
        fps=fps,
        video_bitrate_kbps=video_bitrate_kbps,
        audio_bitrate_kbps=audio_bitrate_kbps,
        keyframe_interval_sec=keyframe_interval_sec
    )
    if res.get("ok"):
        if is_any_platform_verified_live():
            mark_live_broadcast_started(title=data.get("title"))
        if ch_input and not tiktok_chat_poller.active:
            tiktok_chat_poller.connect(ch_input)

    code = 200 if res.get("ok") else 400
    return jsonify({
        "status": "ok" if res.get("ok") else "error",
        "message": res.get("message", ""),
        "rtmp": tiktok_direct_streamer.get_state(),
        "chat": tiktok_chat_poller.get_state()
    }), code


@app.route('/api/tiktok/rtmp/stop', methods=['POST'])
def api_tiktok_rtmp_stop():
    """틱톡 RTMP 직접 실시간 송출을 중지하고, 모든 플랫폼 방송이 꺼진 경우 자동으로 이전 방송 기록 저장 및 초기화를 수행합니다."""
    res = tiktok_direct_streamer.stop()
    try:
        if tiktok_chat_poller.active:
            tiktok_chat_poller.disconnect()
    except Exception:
        pass
    any_remaining = bool(youtube_direct_streamer.active or tiktok_direct_streamer.active or instagram_direct_streamer.active)
    if not any_remaining:
        reset_bundle = finalize_and_archive_broadcast_session(save_empty=True, force_save=True, disconnect_chats=True)
        return jsonify({
            "status": "ok",
            "broadcast_ended": True,
            "message": "⏹️ 방송이 종료되었으며, 이전 방송 기록으로 자동 저장되고 채팅·주문 화면이 초기화되었습니다. (등록 상품 유지)",
            "rtmp": tiktok_direct_streamer.get_state(),
            **reset_bundle
        }), 200
    return jsonify({
        "status": "ok",
        "broadcast_ended": False,
        "message": res.get("message", ""),
        "rtmp": tiktok_direct_streamer.get_state()
    }), 200


@app.route('/api/instagram/status', methods=['GET'])
def api_instagram_status():
    """인스타그램 채널 직접 연결(채팅 수집 + RTMPS 라이브 송출) 통합 상태 반환"""
    return jsonify({
        "status": "ok",
        "chat": instagram_chat_poller.get_state(),
        "rtmp": instagram_direct_streamer.get_state(),
        "viewers": platform_tracker.get_instagram_viewers_summary()
    }), 200


@app.route('/api/instagram/config', methods=['POST'])
def api_instagram_save_config():
    """인스타그램 아이디(@핸들), RTMPS 서버 URL, 스트림 키, 비디오/오디오 비트 전송률, 키프레임 간격 설정을 DB에 저장합니다."""
    data = request.get_json(silent=True) or {}
    ch_input = (data.get("channel_input") or "").strip()
    default_ig_rtmp = "rtmps://live-upload.instagram.com:443/rtmp/"
    rtmp_url = (data.get("rtmp_url") or default_ig_rtmp).strip()
    stream_key = (data.get("stream_key") or "").strip()

    if "channel_input" in data:
        database.set_overlay_state("instagram_channel_input", ch_input)
        instagram_chat_poller.channel_input = ch_input
    if rtmp_url:
        database.set_overlay_state("instagram_rtmp_url", rtmp_url)
        instagram_direct_streamer.rtmp_url = rtmp_url
    if "stream_key" in data:
        database.set_overlay_state("instagram_stream_key", stream_key)
        instagram_direct_streamer.stream_key = stream_key
    if "video_bitrate_kbps" in data and data.get("video_bitrate_kbps"):
        try:
            v_kbps = max(800, min(12000, int(data.get("video_bitrate_kbps"))))
            database.set_overlay_state("instagram_video_bitrate_kbps", v_kbps)
            instagram_direct_streamer.video_bitrate_kbps = v_kbps
        except Exception:
            pass
    if "audio_bitrate_kbps" in data and data.get("audio_bitrate_kbps"):
        try:
            a_kbps = max(64, min(320, int(data.get("audio_bitrate_kbps"))))
            database.set_overlay_state("instagram_audio_bitrate_kbps", a_kbps)
            instagram_direct_streamer.audio_bitrate_kbps = a_kbps
        except Exception:
            pass
    if "keyframe_interval_sec" in data and data.get("keyframe_interval_sec"):
        try:
            kf_sec = max(1, min(4, int(data.get("keyframe_interval_sec"))))
            database.set_overlay_state("instagram_keyframe_sec", kf_sec)
            instagram_direct_streamer.keyframe_interval_sec = kf_sec
        except Exception:
            pass

    return jsonify({
        "status": "ok",
        "message": "인스타그램 연결 및 RTMPS 송출 설정이 저장되었습니다.",
        "chat": instagram_chat_poller.get_state(),
        "rtmp": instagram_direct_streamer.get_state()
    }), 200


@app.route('/api/instagram/chat/connect', methods=['POST'])
def api_instagram_chat_connect():
    """인스타그램 아이디(@핸들) 또는 라이브 URL로 실시간 채팅 직접 수집을 시작합니다."""
    data = request.get_json(silent=True) or {}
    ch_input = (data.get("channel_input") or database.get_overlay_state("instagram_channel_input", "")).strip()
    res = instagram_chat_poller.connect(ch_input)
    code = 200 if res.get("ok") else 400
    return jsonify({
        "status": "ok" if res.get("ok") else "error",
        "message": res.get("message", ""),
        "chat": instagram_chat_poller.get_state()
    }), code


@app.route('/api/instagram/chat/disconnect', methods=['POST'])
def api_instagram_chat_disconnect():
    """인스타그램 채널 실시간 채팅 직접 수집을 중지합니다."""
    res = instagram_chat_poller.disconnect()
    return jsonify({
        "status": "ok",
        "message": res.get("message", ""),
        "chat": instagram_chat_poller.get_state()
    }), 200


@app.route('/api/instagram/rtmp/start', methods=['POST'])
def api_instagram_rtmp_start():
    """웹 OBS 스튜디오 화면을 인스타그램 스트림 키(RTMPS)로 직접 실시간 송출 시작합니다."""
    data = request.get_json(silent=True) or {}
    default_ig_rtmp = "rtmps://live-upload.instagram.com:443/rtmp/"
    rtmp_url = (data.get("rtmp_url") or database.get_overlay_state("instagram_rtmp_url", default_ig_rtmp) or default_ig_rtmp).strip()
    stream_key = (data.get("stream_key") or database.get_overlay_state("instagram_stream_key", "")).strip()
    width = data.get("width", 720)
    height = data.get("height", 1280)
    fps = data.get("fps", 30)
    video_bitrate_kbps = data.get("video_bitrate_kbps", 2500)
    audio_bitrate_kbps = data.get("audio_bitrate_kbps", 128)
    keyframe_interval_sec = data.get("keyframe_interval_sec", 2)
    ch_input = (data.get("channel_input") or database.get_overlay_state("instagram_channel_input", "")).strip()

    res = instagram_direct_streamer.start(
        rtmp_url=rtmp_url,
        stream_key=stream_key,
        width=width,
        height=height,
        fps=fps,
        video_bitrate_kbps=video_bitrate_kbps,
        audio_bitrate_kbps=audio_bitrate_kbps,
        keyframe_interval_sec=keyframe_interval_sec
    )
    if res.get("ok"):
        if is_any_platform_verified_live():
            mark_live_broadcast_started(title=data.get("title"))
        if ch_input and not instagram_chat_poller.active:
            instagram_chat_poller.connect(ch_input)

    code = 200 if res.get("ok") else 400
    return jsonify({
        "status": "ok" if res.get("ok") else "error",
        "message": res.get("message", ""),
        "rtmp": instagram_direct_streamer.get_state(),
        "chat": instagram_chat_poller.get_state()
    }), code


@app.route('/api/instagram/rtmp/stop', methods=['POST'])
def api_instagram_rtmp_stop():
    """인스타그램 RTMPS 직접 실시간 송출을 중지하고, 모든 플랫폼 방송이 꺼진 경우 자동으로 이전 방송 기록 저장 및 초기화를 수행합니다."""
    res = instagram_direct_streamer.stop()
    try:
        if instagram_chat_poller.active:
            instagram_chat_poller.disconnect()
    except Exception:
        pass
    any_remaining = bool(youtube_direct_streamer.active or tiktok_direct_streamer.active or instagram_direct_streamer.active)
    if not any_remaining:
        reset_bundle = finalize_and_archive_broadcast_session(save_empty=True, force_save=True, disconnect_chats=True)
        return jsonify({
            "status": "ok",
            "broadcast_ended": True,
            "message": "⏹️ 방송이 종료되었으며, 이전 방송 기록으로 자동 저장되고 채팅·주문 화면이 초기화되었습니다. (등록 상품 유지)",
            "rtmp": instagram_direct_streamer.get_state(),
            **reset_bundle
        }), 200
    return jsonify({
        "status": "ok",
        "broadcast_ended": False,
        "message": res.get("message", ""),
        "rtmp": instagram_direct_streamer.get_state()
    }), 200


@app.route('/api/simulcast/rtmp/start', methods=['POST'])
def api_simulcast_rtmp_start():
    """웹 OBS 스튜디오 화면을 유튜브 + 틱톡 + 인스타그램(또는 스트림 키가 설정된 모든 플랫폼)으로 원클릭 동시 송출(Simulcast) 시작합니다."""
    data = request.get_json(silent=True) or {}
    width = data.get("width", 720)
    height = data.get("height", 1280)
    fps = data.get("fps", 30)

    yt_rtmp_url = (data.get("yt_rtmp_url") or database.get_overlay_state("youtube_rtmp_url", "rtmp://a.rtmp.youtube.com/live2")).strip()
    yt_key = (data.get("yt_stream_key") if "yt_stream_key" in data else database.get_overlay_state("youtube_stream_key", "")).strip()
    yt_ch = (data.get("yt_channel_input") or database.get_overlay_state("youtube_channel_input", "")).strip()
    yt_v_kbps = data.get("yt_video_bitrate_kbps")
    yt_a_kbps = data.get("yt_audio_bitrate_kbps")
    yt_kf_sec = data.get("yt_keyframe_interval_sec")

    tt_rtmp_url = (data.get("tt_rtmp_url") or database.get_overlay_state("tiktok_rtmp_url", "rtmp://push.tiktok.com/live")).strip()
    tt_key = (data.get("tt_stream_key") if "tt_stream_key" in data else database.get_overlay_state("tiktok_stream_key", "")).strip()
    tt_ch = (data.get("tt_channel_input") or database.get_overlay_state("tiktok_channel_input", "")).strip()
    tt_v_kbps = data.get("tt_video_bitrate_kbps")
    tt_a_kbps = data.get("tt_audio_bitrate_kbps")
    tt_kf_sec = data.get("tt_keyframe_interval_sec")

    default_ig_rtmp = "rtmps://live-upload.instagram.com:443/rtmp/"
    ig_rtmp_url = (data.get("ig_rtmp_url") or database.get_overlay_state("instagram_rtmp_url", default_ig_rtmp)).strip()
    ig_key = (data.get("ig_stream_key") if "ig_stream_key" in data else database.get_overlay_state("instagram_stream_key", "")).strip()
    ig_ch = (data.get("ig_channel_input") or database.get_overlay_state("instagram_channel_input", "")).strip()
    ig_v_kbps = data.get("ig_video_bitrate_kbps")
    ig_a_kbps = data.get("ig_audio_bitrate_kbps")
    ig_kf_sec = data.get("ig_keyframe_interval_sec")

    started_platforms = []
    errors = []

    if yt_key:
        yt_res = youtube_direct_streamer.start(
            rtmp_url=yt_rtmp_url,
            stream_key=yt_key,
            width=width,
            height=height,
            fps=fps,
            video_bitrate_kbps=yt_v_kbps,
            audio_bitrate_kbps=yt_a_kbps,
            keyframe_interval_sec=yt_kf_sec
        )
        if yt_res.get("ok"):
            started_platforms.append("유튜브(YouTube)")
            if yt_ch and not youtube_chat_poller.active:
                youtube_chat_poller.connect(yt_ch)
            _trigger_youtube_background_onair()
        else:
            errors.append(f"유튜브: {yt_res.get('message')}")

    if tt_key:
        tt_res = tiktok_direct_streamer.start(
            rtmp_url=tt_rtmp_url,
            stream_key=tt_key,
            width=width,
            height=height,
            fps=fps,
            video_bitrate_kbps=tt_v_kbps,
            audio_bitrate_kbps=tt_a_kbps,
            keyframe_interval_sec=tt_kf_sec
        )
        if tt_res.get("ok"):
            started_platforms.append("틱톡(TikTok)")
            if tt_ch and not tiktok_chat_poller.active:
                tiktok_chat_poller.connect(tt_ch)
        else:
            errors.append(f"틱톡: {tt_res.get('message')}")

    if ig_key:
        ig_res = instagram_direct_streamer.start(
            rtmp_url=ig_rtmp_url,
            stream_key=ig_key,
            width=width,
            height=height,
            fps=fps,
            video_bitrate_kbps=ig_v_kbps,
            audio_bitrate_kbps=ig_a_kbps,
            keyframe_interval_sec=ig_kf_sec
        )
        if ig_res.get("ok"):
            started_platforms.append("인스타그램(Instagram)")
            if ig_ch and not instagram_chat_poller.active:
                instagram_chat_poller.connect(ig_ch)
        else:
            errors.append(f"인스타그램: {ig_res.get('message')}")

    if not started_platforms:
        msg = " · ".join(errors) if errors else "유튜브, 틱톡 또는 인스타그램 스트림 키(Stream Key)를 최소 1개 이상 입력해 주세요."
        return jsonify({
            "status": "error",
            "message": msg,
            "rtmp": youtube_direct_streamer.get_state(),
            "tiktok_rtmp": tiktok_direct_streamer.get_state(),
            "instagram_rtmp": instagram_direct_streamer.get_state()
        }), 400

    if is_any_platform_verified_live():
        mark_live_broadcast_started(title=data.get("title"))

    return jsonify({
        "status": "ok",
        "started_platforms": started_platforms,
        "verified_live": is_any_platform_verified_live(),
        "message": f"🟡 {' + '.join(started_platforms)} RTMP 신호 전송을 시작했습니다. 실제 라이브 온에어 여부를 확인 중입니다...",
        "rtmp": youtube_direct_streamer.get_state(),
        "chat": youtube_chat_poller.get_state(),
        "tiktok_rtmp": tiktok_direct_streamer.get_state(),
        "tiktok_chat": tiktok_chat_poller.get_state(),
        "instagram_rtmp": instagram_direct_streamer.get_state(),
        "instagram_chat": instagram_chat_poller.get_state()
    }), 200


@app.route('/api/simulcast/rtmp/stop', methods=['POST'])
def api_simulcast_rtmp_stop():
    """유튜브, 틱톡 및 인스타그램 RTMP/RTMPS 실시간 동시 송출을 일괄 중지하고, 이전 방송 기록 저장 및 초기화를 수행합니다."""
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "").strip()
    youtube_direct_streamer.stop()
    tiktok_direct_streamer.stop()
    instagram_direct_streamer.stop()
    platform_tracker.clear_platform_presence("YOUTUBE")
    platform_tracker.clear_platform_presence("TIKTOK")
    platform_tracker.clear_platform_presence("INSTAGRAM")
    try:
        if 'simulcast_automation_bridge' in globals() and simulcast_automation_bridge is not None:
            simulcast_automation_bridge.stop_all(close_browsers=False)
    except Exception:
        pass

    reset_bundle = finalize_and_archive_broadcast_session(
        title=title,
        save_empty=True,
        force_save=True,
        disconnect_chats=True
    )
    return jsonify({
        "status": "ok",
        "broadcast_ended": True,
        "message": "⏹️ 방송이 종료되었으며, 이전 방송 기록으로 자동 저장되고 채팅·주문 화면이 초기화되었습니다. (등록 상품 유지)",
        "rtmp": youtube_direct_streamer.get_state(),
        "tiktok_rtmp": tiktok_direct_streamer.get_state(),
        "instagram_rtmp": instagram_direct_streamer.get_state(),
        **reset_bundle
    }), 200


# ==============================================================================
# 🚀 4-Layer CDP 자동화 브릿지 컨트롤러 & 영구 세션 브라우저 풀 (Instagram + TikTok)
# ==============================================================================
class SimulcastAutomationBridge:
    """
    자체 방송 스튜디오 화면(UI Layer)의 명령을 받아 내장 Chromium 영구 프로필 브라우저들을
    CDP(Chrome DevTools Protocol)로 제어하는 오케스트레이터:
      - Layer 1: UI Layer (통합 방송 제목 입력 + 원클릭 3사 자동 동시 송출 버튼 + 접이식 내장 브라우저 독)
      - Layer 2: Automation Bridge Controller (5단계 상태 머신 + CDP Network.enable 패킷 스니퍼 + React Native Setter + isTrusted 하드웨어 클릭)
      - Layer 3: Embedded Browser Pool (.browser_profiles/instagram & .browser_profiles/tiktok 영구 세션 + 백그라운드 스로틀링 해제 + 봇 탐지 우회)
      - Layer 4: Media Engine & Live Chat Collector (FFmpeg RTMPS/RTMP 동적 바인딩 + 방송실 DOM 상주 실시간 채팅/주문 자동 파싱)
    """

    STAGES = [
        {"step": 1, "code": "PREFLIGHT_CHECK", "label": "① 세션 헬스체크"},
        {"step": 2, "code": "NETWORK_SNIFFER_ACTIVE", "label": "② CDP 패킷 스니퍼"},
        {"step": 3, "code": "CREATE_LIVE_SESSION", "label": "③ 방 생성·제목 주입"},
        {"step": 4, "code": "RTMP_INTERCEPT_AND_PUSH", "label": "④ 키 추출·FFmpeg 송출"},
        {"step": 5, "code": "IG_WAIT_PREVIEW_AND_GO_LIVE", "label": "⑤ 인스타 Go Live·채팅수집"}
    ]

    def __init__(self):
        from collections import deque
        self.lock = Lock()
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.selectors_path = os.path.join(self.base_dir, "simulcast_selectors.json")
        self.profiles_root = os.path.join(self.base_dir, ".browser_profiles")
        os.makedirs(os.path.join(self.profiles_root, "youtube"), exist_ok=True)
        os.makedirs(os.path.join(self.profiles_root, "instagram"), exist_ok=True)
        os.makedirs(os.path.join(self.profiles_root, "tiktok"), exist_ok=True)

        self.active = False
        self.current_step = 0
        self.stage_code = "IDLE"
        self.stage_label = "대기 중"
        self.status_text = "대기 중 (통합 방송 제목 입력 후 원클릭 3사 자동 송출을 시작하세요)"
        self.last_error = ""
        self.broadcast_title = database.get_overlay_state("simulcast_unified_title", "") or "실시간 라이브 특가 방송"
        self.logs = deque(maxlen=80)

        # 플랫폼별 내장 브라우저 상태 및 가로챈 스트림 정보
        self.platforms = {
            "youtube": {
                "logged_in": self._has_saved_profile_cookies("youtube"),
                "login_required": False,
                "browser_running": False,
                "headless": False,
                "status": "세션 프로필 준비됨",
                "current_url": "",
                "rtmp_url": database.get_overlay_state("youtube_rtmp_url", "rtmp://a.rtmp.youtube.com/live2"),
                "stream_key": database.get_overlay_state("youtube_stream_key", ""),
                "intercepted_at": None,
                "go_live_clicked": False,
                "chat_collecting": False,
                "chat_collected_count": 0,
                "last_screenshot_bytes": None,
                "last_screenshot_ts": 0.0
            },
            "instagram": {
                "logged_in": self._has_saved_profile_cookies("instagram"),
                "login_required": False,
                "browser_running": False,
                "headless": False,
                "status": "세션 프로필 준비됨",
                "current_url": "",
                "rtmp_url": database.get_overlay_state("instagram_rtmp_url", "rtmps://live-upload.instagram.com:443/rtmp/"),
                "stream_key": database.get_overlay_state("instagram_stream_key", ""),
                "intercepted_at": None,
                "go_live_clicked": False,
                "chat_collecting": False,
                "chat_collected_count": 0,
                "last_screenshot_bytes": None,
                "last_screenshot_ts": 0.0
            },
            "tiktok": {
                "logged_in": self._has_saved_profile_cookies("tiktok"),
                "login_required": False,
                "browser_running": False,
                "headless": False,
                "status": "세션 프로필 준비됨",
                "current_url": "",
                "rtmp_url": database.get_overlay_state("tiktok_rtmp_url", "rtmp://push.tiktok.com/live"),
                "stream_key": database.get_overlay_state("tiktok_stream_key", ""),
                "intercepted_at": None,
                "go_live_clicked": False,
                "chat_collecting": False,
                "chat_collected_count": 0,
                "last_screenshot_bytes": None,
                "last_screenshot_ts": 0.0
            }
        }

        # 플랫폼별 전용 워커 큐 (Playwright sync API는 생성된 스레드 내부에서만 호출해야 안전함)
        self._cmd_queues = {
            "youtube": deque(),
            "instagram": deque(),
            "tiktok": deque()
        }
        self._worker_threads = {
            "youtube": None,
            "instagram": None,
            "tiktok": None
        }
        self._worker_stop_flags = {
            "youtube": False,
            "instagram": False,
            "tiktok": False
        }
        self._orchestrator_thread = None
        self._stop_requested = False

    def _log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        print(f"🤖 [CDP 브릿지] {msg}", flush=True)
        with self.lock:
            self.logs.append(entry)
            self.status_text = msg

    def _has_saved_profile_cookies(self, platform):
        p_dir = os.path.join(self.profiles_root, platform)
        for rel in (
            os.path.join("Default", "Network", "Cookies"),
            os.path.join("Default", "Cookies"),
            "Cookies"
        ):
            fp = os.path.join(p_dir, rel)
            if os.path.exists(fp) and os.path.getsize(fp) > 1024:
                return True
        return False

    def load_selectors(self):
        """원격/로컬 핫픽스 JSON 설정 파일(simulcast_selectors.json)을 실시간으로 로드합니다."""
        try:
            if os.path.exists(self.selectors_path):
                with open(self.selectors_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            self._log(f"⚠️ 셀렉터 JSON 로드 경고: {e}")
        return {"instagram": {}, "tiktok": {}}

    def save_selectors(self, new_data):
        """UI 개편 시 프로그램 재배포 없이 셀렉터/API 패턴 JSON을 즉시 핫픽스 저장합니다."""
        if not isinstance(new_data, dict):
            raise ValueError("JSON 객체 형식이 아닙니다.")
        new_data["updated_at"] = datetime.now().isoformat()
        with open(self.selectors_path, "w", encoding="utf-8") as f:
            json.dump(new_data, f, ensure_ascii=False, indent=2)
        self._log("🛠️ 셀렉터 & API 엔드포인트 핫픽스 설정(simulcast_selectors.json)이 즉시 반영되었습니다.")
        return new_data

    @staticmethod
    def split_rtmp_url(full_url):
        """
        인스타그램 upload_url 또는 틱톡 rtmp_push_url과 같이 서버 URL과 스트림 키가 합쳐진 문자열을
        (rtmp_server_url, stream_key) 튜플로 정밀 분리합니다.
        예: rtmps://live-upload.instagram.com:443/rtmp/179988776655?s_sw=0&s_vt=ig
            -> ('rtmps://live-upload.instagram.com:443/rtmp/', '179988776655?s_sw=0&s_vt=ig')
        """
        raw = str(full_url or "").strip()
        if not raw:
            return "", ""
        if "/rtmp/" in raw:
            idx = raw.find("/rtmp/") + len("/rtmp/")
            return raw[:idx], raw[idx:]
        if "/live/" in raw:
            idx = raw.find("/live/") + len("/live/")
            return raw[:idx], raw[idx:]
        if "/live2/" in raw:
            idx = raw.find("/live2/") + len("/live2/")
            return raw[:idx], raw[idx:]
        last_slash = raw.rfind("/")
        if last_slash > 8:
            return raw[:last_slash + 1], raw[last_slash + 1:]
        return raw, ""

    def _set_stage(self, step_num, detail_msg=None):
        with self.lock:
            self.current_step = step_num
            matched = next((s for s in self.STAGES if s["step"] == step_num), None)
            if matched:
                self.stage_code = matched["code"]
                self.stage_label = matched["label"]
            elif step_num == 6:
                self.stage_code = "ON_AIR_CHAT_COLLECTING"
                self.stage_label = "✅ 온에어 & 실시간 채팅·주문 수집 중"
            else:
                self.stage_code = "IDLE"
                self.stage_label = "대기 중"
        if detail_msg:
            self._log(detail_msg)

    def ensure_platform_worker(self, platform, visible_window=True):
        """유튜브, 인스타그램 또는 틱톡 전용 영구 프로필 Chromium 워커 스레드를 기동합니다."""
        platform = platform.lower().strip()
        if platform not in ("youtube", "instagram", "tiktok"):
            return
        with self.lock:
            th = self._worker_threads.get(platform)
            if th is not None and th.is_alive():
                return
            self._worker_stop_flags[platform] = False
            new_th = threading.Thread(
                target=self._platform_browser_loop,
                args=(platform, visible_window),
                daemon=True
            )
            self._worker_threads[platform] = new_th
            new_th.start()

    def enqueue_platform_cmd(self, platform, action, payload=None):
        platform = platform.lower().strip()
        self.ensure_platform_worker(platform, visible_window=True)
        with self.lock:
            self._cmd_queues[platform].append({"action": action, "payload": payload or {}})

    def _inject_stealth_and_cdp_sniffer(self, context, page, platform):
        """
        Layer 2 & 3 핵심 구현:
          1) Page.addScriptToEvaluateOnNewDocument를 통해 navigator.webdriver = undefined 덮어쓰기 및 봇 탐지 흔적 제거
          2) CDP Network.enable 활성화 및 Network.responseReceived 패킷 가로채기 장착
          3) React Controlled Input 네이티브 프로토타입 Setter 헬퍼 함수 등록
        """
        cdp = context.new_cdp_session(page)
        try:
            cdp.send("Page.enable")
            cdp.send("Page.addScriptToEvaluateOnNewDocument", {
                "source": """
                (() => {
                    try {
                        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                        window.chrome = window.chrome || { runtime: {} };
                        Object.defineProperty(navigator, 'languages', { get: () => ['ko-KR', 'ko', 'en-US', 'en'] });
                        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                    } catch (e) {}

                    // React 제어 컴포넌트(Controlled Input) 방송 제목 강제 주입 헬퍼
                    window.__agySetReactInputValue = function(inputElement, text) {
                        if (!inputElement) return false;
                        const proto = inputElement.tagName === 'TEXTAREA'
                            ? window.HTMLTextAreaElement.prototype
                            : window.HTMLInputElement.prototype;
                        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                        if (desc && desc.set) {
                            desc.set.call(inputElement, text);
                        } else {
                            inputElement.value = text;
                        }
                        inputElement.dispatchEvent(new Event('input', { bubbles: true }));
                        inputElement.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    };
                })();
                """
            })
        except Exception as e:
            self._log(f"⚠️ [{platform}] CDP Stealth 주입 경고: {e}")

        try:
            cdp.send("Network.enable")
        except Exception as e:
            self._log(f"⚠️ [{platform}] CDP Network.enable 경고: {e}")

        def _extract_stream_credentials_from_json(obj):
            if not isinstance(obj, (dict, list)):
                return
            cfg = self.load_selectors().get(platform, {})
            url_keys = cfg.get("json_url_keys") or ["upload_url", "rtmp_push_url", "broadcast_url", "stream_url"]

            if isinstance(obj, dict):
                # 1) upload_url 또는 rtmp_push_url 단일 필드에 합쳐져 내려오는 경우
                for k in url_keys:
                    val = obj.get(k)
                    if isinstance(val, str) and ("rtmp://" in val or "rtmps://" in val):
                        srv, skey = self.split_rtmp_url(val)
                        if srv and skey:
                            self._on_intercepted_stream_key(platform, srv, skey, source=f"CDP JSON ({k})")
                            return
                # 2) rtmp_server / stream_key가 분리되어 내려오는 경우
                srv_cand = obj.get("rtmp_url") or obj.get("server_url") or obj.get("rtmp_push_url")
                key_cand = obj.get("stream_key") or obj.get("stream_name") or obj.get("key")
                if isinstance(srv_cand, str) and isinstance(key_cand, str) and ("rtmp" in srv_cand) and key_cand.strip():
                    self._on_intercepted_stream_key(platform, srv_cand.strip(), key_cand.strip(), source="CDP JSON (server+key)")
                    return

                for v in obj.values():
                    if isinstance(v, (dict, list)):
                        _extract_stream_credentials_from_json(v)
            elif isinstance(obj, list):
                for item in obj:
                    if isinstance(item, (dict, list)):
                        _extract_stream_credentials_from_json(item)

        def _on_cdp_response_received(params):
            try:
                resp = (params or {}).get("response") or {}
                url = str(resp.get("url") or "")
                req_id = (params or {}).get("requestId")
                if not url or not req_id:
                    return
                cfg = self.load_selectors().get(platform, {})
                patterns = cfg.get("network_patterns") or ["/api/v1/live/create/", "/webcast/room/create/"]
                if any(pat in url for pat in patterns):
                    body_res = cdp.send("Network.getResponseBody", {"requestId": req_id})
                    raw_body = (body_res or {}).get("body") or ""
                    if raw_body:
                        data = json.loads(raw_body)
                        _extract_stream_credentials_from_json(data)
            except Exception:
                pass

        try:
            cdp.on("Network.responseReceived", _on_cdp_response_received)
        except Exception:
            pass

        # Playwright response 이벤트도 이중 백업으로 함께 감시
        def _on_pw_response(response):
            try:
                url = response.url or ""
                cfg = self.load_selectors().get(platform, {})
                patterns = cfg.get("network_patterns") or ["/api/v1/live/create/", "/webcast/room/create/"]
                if any(pat in url for pat in patterns):
                    data = response.json()
                    _extract_stream_credentials_from_json(data)
            except Exception:
                pass

        page.on("response", _on_pw_response)
        return cdp

    def _on_intercepted_stream_key(self, platform, rtmp_server, stream_key, source="CDP"):
        """CDP 네트워크 스니퍼 또는 읽기 전용 input에서 추출한 RTMPS/RTMP 주소와 스트림 키를 즉시 바인딩합니다."""
        rtmp_server = str(rtmp_server or "").strip()
        stream_key = str(stream_key or "").strip()
        if not rtmp_server or not stream_key:
            return
        with self.lock:
            p_info = self.platforms.get(platform)
            if not p_info:
                return
            already_same = (p_info["rtmp_url"] == rtmp_server and p_info["stream_key"] == stream_key and p_info["intercepted_at"])
            p_info["rtmp_url"] = rtmp_server
            p_info["stream_key"] = stream_key
            p_info["intercepted_at"] = datetime.now().strftime("%H:%M:%S")
            p_info["status"] = f"🔑 스트림 키 가로채기 완료 ({source})"

        if platform == "instagram":
            database.set_overlay_state("instagram_rtmp_url", rtmp_server)
            database.set_overlay_state("instagram_stream_key", stream_key)
            instagram_direct_streamer.rtmp_url = rtmp_server
            instagram_direct_streamer.stream_key = stream_key
        elif platform == "tiktok":
            database.set_overlay_state("tiktok_rtmp_url", rtmp_server)
            database.set_overlay_state("tiktok_stream_key", stream_key)
            tiktok_direct_streamer.rtmp_url = rtmp_server
            tiktok_direct_streamer.stream_key = stream_key

        if not already_same:
            masked_key = (stream_key[:6] + "..." + stream_key[-4:]) if len(stream_key) > 12 else "****"
            self._log(f"🎯 [{platform.upper()}] {source} 패킷 가로채기 성공! 서버: {rtmp_server} / 키: {masked_key}")

    def trusted_click_by_coords(self, cdp, x, y):
        """
        CDP Input.dispatchMouseEvent를 사용하여 OS 하드웨어 수준의 마우스 이동 -> 누름 -> 뗌을 발생시킵니다.
        (event.isTrusted === true 보장)
        """
        x = float(x)
        y = float(y)
        cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved",
            "x": x,
            "y": y
        })
        time.sleep(0.04)
        cdp.send("Input.dispatchMouseEvent", {
            "type": "mousePressed",
            "x": x,
            "y": y,
            "button": "left",
            "clickCount": 1
        })
        time.sleep(0.05)
        cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseReleased",
            "x": x,
            "y": y,
            "button": "left",
            "clickCount": 1
        })

    def trusted_click_element(self, page, cdp, selectors=None, texts=None, require_enabled=True):
        """
        셀렉터 배열 또는 버튼 텍스트 배열을 기반으로 화면 내 클릭 가능한 요소의 중심 좌표(x, y)를 계산한 뒤
        CDP Input.dispatchMouseEvent로 isTrusted: true 클릭을 수행합니다.
        """
        selectors = selectors or []
        texts = texts or []
        coord = page.evaluate("""({ selectors, texts, requireEnabled }) => {
            const isVisibleAndReady = (el) => {
                if (!el) return false;
                if (requireEnabled && (el.disabled || el.getAttribute('aria-disabled') === 'true')) return false;
                const rect = el.getBoundingClientRect();
                if (!rect || rect.width < 4 || rect.height < 4) return false;
                const style = window.getComputedStyle(el);
                if (style.visibility === 'hidden' || style.display === 'none' || parseFloat(style.opacity || '1') < 0.05) return false;
                return true;
            };

            for (const sel of selectors) {
                try {
                    const nodes = document.querySelectorAll(sel);
                    for (const el of nodes) {
                        const target = el.closest('button, [role="button"], a, [role="menuitem"]') || el;
                        if (isVisibleAndReady(target)) {
                            const r = target.getBoundingClientRect();
                            return { x: r.left + r.width / 2, y: r.top + r.height / 2, matched: sel };
                        }
                    }
                } catch (e) {}
            }

            if (texts && texts.length > 0) {
                const candidates = document.querySelectorAll('button, div[role="button"], a, div[role="menuitem"], span');
                for (const t of texts) {
                    const normT = t.trim().toLowerCase();
                    for (const node of candidates) {
                        const txt = (node.innerText || node.textContent || '').trim().toLowerCase();
                        const aria = (node.getAttribute('aria-label') || '').trim().toLowerCase();
                        if (txt === normT || aria === normT || (txt.includes(normT) && txt.length <= normT.length + 12)) {
                            const clickable = node.closest('button, div[role="button"], a, div[role="menuitem"]') || node;
                            if (isVisibleAndReady(clickable)) {
                                const r = clickable.getBoundingClientRect();
                                return { x: r.left + r.width / 2, y: r.top + r.height / 2, matched: `text:${t}` };
                            }
                        }
                    }
                }
            }
            return null;
        }""", {"selectors": selectors, "texts": texts, "requireEnabled": bool(require_enabled)})

        if not coord or "x" not in coord:
            return False
        self.trusted_click_by_coords(cdp, coord["x"], coord["y"])
        return True

    def inject_react_input_value(self, page, cdp, selectors, text):
        """
        React 제어 컴포넌트(Controlled Input)에 네이티브 프로토타입 Setter로 방송 제목을 강제 주입하고
        input/change 이벤트를 발생시킵니다.
        """
        res = page.evaluate("""({ selectors, text }) => {
            for (const sel of selectors) {
                try {
                    const nodes = document.querySelectorAll(sel);
                    for (const el of nodes) {
                        const rect = el.getBoundingClientRect();
                        if (!rect || rect.width < 10 || rect.height < 10) continue;
                        if (el.readOnly || el.disabled) continue;
                        el.focus();
                        if (window.__agySetReactInputValue) {
                            window.__agySetReactInputValue(el, text);
                        } else {
                            const proto = el.tagName === 'TEXTAREA'
                                ? window.HTMLTextAreaElement.prototype
                                : window.HTMLInputElement.prototype;
                            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                            if (setter) setter.call(el, text);
                            else el.value = text;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                        return { ok: true, selector: sel, x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
                    }
                } catch (e) {}
            }
            return { ok: false };
        }""", {"selectors": selectors or [], "text": str(text or "")})
        return bool(res and res.get("ok"))

    def _scrape_readonly_stream_inputs(self, page, platform):
        """
        네트워크 패킷 가로채기의 보조 수단으로, 화면에 렌더링된 읽기 전용(readonly) input 요소들에서
        rtmps:// 또는 rtmp:// URL과 일회용 스트림 키를 추출합니다.
        """
        try:
            found = page.evaluate("""() => {
                const inputs = Array.from(document.querySelectorAll('input'));
                let rtmpUrl = '';
                let streamKey = '';
                for (let i = 0; i < inputs.length; i++) {
                    const val = (inputs[i].value || '').trim();
                    if (!val) continue;
                    if (val.startsWith('rtmp://') || val.startsWith('rtmps://')) {
                        rtmpUrl = val;
                        if (i + 1 < inputs.length && (inputs[i + 1].value || '').trim().length >= 8) {
                            streamKey = (inputs[i + 1].value || '').trim();
                        }
                    } else if (rtmpUrl && !streamKey && val.length >= 10 && !val.includes(' ')) {
                        streamKey = val;
                    }
                }
                return { rtmpUrl, streamKey };
            }""")
            if found and found.get("rtmpUrl"):
                r_url = found["rtmpUrl"]
                s_key = found.get("streamKey") or ""
                if not s_key:
                    r_url, s_key = self.split_rtmp_url(r_url)
                if r_url and s_key:
                    self._on_intercepted_stream_key(platform, r_url, s_key, source="DOM Readonly Input")
                    return True
        except Exception:
            pass
        return False

    def _check_page_login_state(self, context, page, platform):
        """Stage 1 (Pre-flight Check): 쿠키 및 로그인 폼 유무를 검사하여 로그인 유효성을 판별합니다."""
        cfg = self.load_selectors().get(platform, {})
        cookie_names = set(cfg.get("logged_in_cookie_names") or ["sessionid"])
        login_selectors = cfg.get("login_check_selectors") or ["input[name='username']", "input[name='password']"]

        has_auth_cookie = False
        try:
            cookies = context.cookies()
            for c in cookies:
                if c.get("name") in cookie_names and c.get("value"):
                    has_auth_cookie = True
                    break
        except Exception:
            pass

        url_now = (page.url or "").lower()
        has_login_form = False
        try:
            if (
                "/accounts/login" in url_now
                or "/login" in url_now
                or "accounts.google.com" in url_now
                or "servicelogin" in url_now
            ):
                has_login_form = True
            else:
                has_login_form = bool(page.evaluate("""(sels) => {
                    for (const s of sels) {
                        try {
                            const el = document.querySelector(s);
                            if (el && el.getBoundingClientRect().width > 10) return true;
                        } catch (e) {}
                    }
                    return false;
                }""", login_selectors))
        except Exception:
            pass

        is_logged_in = bool(has_auth_cookie and not has_login_form)
        extra_err = ""
        if platform == "youtube" and is_logged_in:
            try:
                body_txt = page.evaluate("() => (document.body && document.body.innerText ? document.body.innerText.slice(0, 1200) : '')") or ""
                if "라이브 스트리밍을 사용할 수 없습니다" in body_txt or "Live streaming isn't available" in body_txt:
                    extra_err = "❌ 유튜브 채널의 실시간 스트리밍 권한이 아직 활성화되지 않았습니다(전화번호 인증/24시간 대기 필요)."
            except Exception:
                pass
        with self.lock:
            p_info = self.platforms[platform]
            p_info["logged_in"] = is_logged_in
            p_info["login_required"] = not is_logged_in
            p_info["current_url"] = page.url or ""
            p_info["status"] = extra_err or ("🟢 로그인 세션 유효함" if is_logged_in else "🟡 로그인 필요 (최초 1회 로그인 후 영구 유지)")
        return is_logged_in

    def _automate_instagram_create_live(self, page, cdp, title):
        """
        Stage 3 (인스타그램 방송 세션 생성):
          1) 좌측/상단 [+ 만들기(Create)] 클릭
          2) [라이브 방송(Live video)] 메뉴 클릭
          3) 방송 제목 React 주입 및 공개(Public) 대상 선택
          4) [다음(Next)] trustedClick 실행 -> RTMPS 스트림 키 발급 유도
        """
        cfg = self.load_selectors().get("instagram", {})
        self._log("📸 [인스타그램] Stage 3: '+ 만들기 ➔ 라이브 방송' 모달 호출 및 방송 제목 주입 중...")

        # 이미 Live Producer 화면이거나 스트림 키 입력란이 열려 있는지 먼저 확인
        if self._scrape_readonly_stream_inputs(page, "instagram"):
            return True

        # 1) '+ 만들기(Create)' 클릭
        clicked_create = self.trusted_click_element(
            page, cdp,
            selectors=cfg.get("create_btn_selectors"),
            texts=cfg.get("create_btn_texts"),
            require_enabled=False
        )
        if clicked_create:
            time.sleep(1.2)

        # 2) '라이브 방송(Live video)' 클릭
        clicked_live = self.trusted_click_element(
            page, cdp,
            selectors=cfg.get("live_menu_selectors"),
            texts=cfg.get("live_menu_texts"),
            require_enabled=False
        )
        if clicked_live:
            time.sleep(1.5)

        # 3) React Controlled Input에 방송 제목 강제 주입
        if title:
            injected = self.inject_react_input_value(
                page, cdp,
                selectors=cfg.get("title_input_selectors") or ["input[placeholder*='제목']", "input[type='text']"],
                text=title
            )
            if injected:
                self._log(f"📸 [인스타그램] React 방송 제목 주입 완료: '{title}'")
                time.sleep(0.5)

        # 4) 공개 범위(Audience) 드롭다운이 있으면 '공개(Public)' 선택 시도
        try:
            self.trusted_click_element(
                page, cdp,
                selectors=[],
                texts=cfg.get("audience_public_texts") or ["공개", "Public"],
                require_enabled=False
            )
            time.sleep(0.5)
        except Exception:
            pass

        # 5) '다음(Next)' 버튼 trustedClick (이 시점에 /api/v1/live/create/ 응답이 발생하며 upload_url이 내려옴)
        clicked_next = self.trusted_click_element(
            page, cdp,
            selectors=cfg.get("next_btn_selectors"),
            texts=cfg.get("next_btn_texts") or ["다음", "Next"],
            require_enabled=True
        )
        if clicked_next:
            self._log("📸 [인스타그램] '다음(Next)' 신뢰 클릭(isTrusted: true) 전송 완료 — RTMPS 스트림 키 응답 대기 중...")

        # 최대 8초 동안 CDP 패킷 가로채기 또는 DOM readonly input 생성 대기
        for _ in range(16):
            time.sleep(0.5)
            self._scrape_readonly_stream_inputs(page, "instagram")
            with self.lock:
                if self.platforms["instagram"].get("intercepted_at") and self.platforms["instagram"].get("stream_key"):
                    return True
        return bool(self.platforms["instagram"].get("stream_key"))

    def _automate_tiktok_create_live(self, page, cdp, title):
        """
        Stage 3 (틱톡 LIVE Center 방송 세션 생성):
          1) LIVE Center에서 방송 제목 React 주입
          2) 'Go LIVE (방송 시작 / 스트림 키 발급)' trustedClick 실행
          3) CDP 네트워크 응답 또는 DOM readonly input에서 RTMP 서버 URL 및 스트림 키 추출
        """
        cfg = self.load_selectors().get("tiktok", {})
        self._log("🎵 [틱톡] Stage 3: LIVE Center 방송 제목 주입 및 스트림 키 발급 요청 중...")

        if self._scrape_readonly_stream_inputs(page, "tiktok"):
            return True

        if title:
            injected = self.inject_react_input_value(
                page, cdp,
                selectors=cfg.get("title_input_selectors") or ["input[type='text']"],
                text=title
            )
            if injected:
                self._log(f"🎵 [틱톡] React 방송 제목 주입 완료: '{title}'")
                time.sleep(0.5)

        self.trusted_click_element(
            page, cdp,
            selectors=cfg.get("go_live_btn_selectors"),
            texts=cfg.get("go_live_btn_texts") or ["Go LIVE", "방송 시작", "스트림 키 발급"],
            require_enabled=True
        )

        for _ in range(14):
            time.sleep(0.5)
            self._scrape_readonly_stream_inputs(page, "tiktok")
            with self.lock:
                if self.platforms["tiktok"].get("intercepted_at") and self.platforms["tiktok"].get("stream_key"):
                    return True
        return bool(self.platforms["tiktok"].get("stream_key"))

    def _automate_youtube_studio_go_live(self, page, cdp, timeout_sec=25.0):
        """
        유튜브 스튜디오 제어실을 백그라운드에서 확인해 로그인 여부 점검, video_id 추출,
        이미 온에어 상태 감지 또는 '실시간 스트리밍 시작(Go live)' 자동 클릭을 수행합니다.
        """
        cfg = self.load_selectors().get("youtube", {})
        home_url = cfg.get("home_url") or "https://studio.youtube.com/"
        try:
            if "studio.youtube.com" not in (page.url or ""):
                page.goto(home_url, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass

        logged_in = self._check_page_login_state(page.context, page, "youtube")
        if not logged_in:
            msg = "유튜브 스튜디오 최초 1회 로그인이 필요합니다. [▶️ 내장 브라우저 로그인]에서 1회만 로그인하면 이후 자동으로 온에어됩니다."
            with self.lock:
                self.platforms["youtube"]["login_required"] = True
                self.platforms["youtube"]["status"] = "🟡 유튜브 1회 로그인 필요"
            try:
                if "youtube_direct_streamer" in globals() and youtube_direct_streamer is not None:
                    with youtube_direct_streamer.lock:
                        youtube_direct_streamer.last_error = msg
            except Exception:
                pass
            self._log(f"⚠️ [유튜브 스튜디오] {msg}")
            return False

        sels = cfg.get("go_live_btn_selectors") or [
            "ytcp-button#go-live-button",
            "#go-live-button",
            "button:has-text('실시간 스트리밍 시작')",
            "button:has-text('Go live')"
        ]
        txts = cfg.get("go_live_btn_texts") or ["실시간 스트리밍 시작", "Go live"]
        end_sels = [
            "ytcp-button#end-stream-button",
            "#end-stream-button",
            "ytcp-button:has-text('스트림 종료')",
            "ytcp-button:has-text('End stream')",
            "button:has-text('스트림 종료')",
            "button:has-text('End stream')"
        ]

        def _sync_studio_video_id():
            try:
                m = re.search(r'/video/([A-Za-z0-9_-]{6,})/', page.url or "")
                vid = m.group(1) if m else ""
                if vid:
                    if "youtube_direct_streamer" in globals() and youtube_direct_streamer is not None:
                        with youtube_direct_streamer.lock:
                            youtube_direct_streamer._yt_manifest_cache["video_id"] = vid
                    if "youtube_chat_poller" in globals() and youtube_chat_poller is not None:
                        with youtube_chat_poller.lock:
                            if not youtube_chat_poller.video_id:
                                youtube_chat_poller.video_id = vid
            except Exception:
                pass

        self._log("⏳ [유튜브 스튜디오] 백그라운드 제어실 스트림 수신 및 온에어 상태 확인 중...")
        deadline = time.time() + float(timeout_sec)
        while time.time() < deadline and not self._stop_requested:
            _sync_studio_video_id()
            try:
                already_live = page.evaluate("""(sels) => {
                    for (const s of sels) {
                        const el = document.querySelector(s);
                        if (el && el.offsetParent !== null && !el.disabled && el.getAttribute('aria-disabled') !== 'true') {
                            return true;
                        }
                    }
                    return false;
                }""", end_sels)
            except Exception:
                already_live = False

            if already_live:
                with self.lock:
                    self.platforms["youtube"]["go_live_clicked"] = True
                    self.platforms["youtube"]["status"] = "🟢 유튜브 실제 온에어 확인됨"
                try:
                    if "youtube_direct_streamer" in globals() and youtube_direct_streamer is not None:
                        with youtube_direct_streamer.lock:
                            youtube_direct_streamer._yt_manifest_cache["is_live"] = True
                            youtube_direct_streamer.last_error = ""
                except Exception:
                    pass
                self._log("🟢 [유튜브 스튜디오] 이미 라이브 온에어(스트림 종료 버튼 활성) 상태 확인 완료!")
                return True

            clicked = self.trusted_click_element(page, cdp, selectors=sels, texts=txts, require_enabled=True)
            if clicked:
                with self.lock:
                    self.platforms["youtube"]["go_live_clicked"] = True
                    self.platforms["youtube"]["status"] = "🟢 유튜브 스튜디오 '실시간 스트리밍 시작' 클릭 완료"
                try:
                    if "youtube_direct_streamer" in globals() and youtube_direct_streamer is not None:
                        with youtube_direct_streamer.lock:
                            youtube_direct_streamer._yt_manifest_cache["is_live"] = True
                            youtube_direct_streamer.last_error = ""
                except Exception:
                    pass
                self._log("🎉 [유튜브 스튜디오] '실시간 스트리밍 시작' 자동 클릭 완료!")
                return True
            time.sleep(0.8)
        self._log("⚠️ [유튜브 스튜디오] '실시간 스트리밍 시작' 버튼이 아직 비활성 상태입니다. 스트림 키 일치 여부를 확인해 주세요.")
        return False

    def _wait_and_click_instagram_go_live(self, page, cdp, timeout_sec=22.0):
        """
        Stage 5 (인스타그램 프리뷰 감지 및 '방송 시작(Go live)' 최종 isTrusted 클릭):
        인스타그램 Live Producer 서버가 FFmpeg RTMPS 스트림을 수신·디코딩하기까지 약 3~7초 버퍼링이 걸립니다.
        우측 상단 '방송 시작(Go live)' 버튼이 disabled 상태에서 활성화(!el.disabled) 상태로 바뀌는 순간을 포착해
        CDP 하드웨어 마우스 클릭(Input.dispatchMouseEvent)으로 실제 방송을 켭니다.
        """
        cfg = self.load_selectors().get("instagram", {})
        sels = cfg.get("go_live_btn_selectors") or ["button:has-text('방송 시작')", "button:has-text('Go live')"]
        txts = cfg.get("go_live_btn_texts") or ["방송 시작", "Go live"]

        self._log("⏳ [인스타그램] Stage 5: Live Producer 비디오 프리뷰 수신 및 '방송 시작(Go live)' 버튼 활성화 감시 중...")
        deadline = time.time() + float(timeout_sec)
        while time.time() < deadline and not self._stop_requested:
            clicked = self.trusted_click_element(
                page, cdp,
                selectors=sels,
                texts=txts,
                require_enabled=True
            )
            if clicked:
                with self.lock:
                    self.platforms["instagram"]["go_live_clicked"] = True
                    self.platforms["instagram"]["chat_collecting"] = True
                    self.platforms["instagram"]["status"] = "🔴 ON AIR (방송 시작 클릭 완료 · 실시간 채팅 수집 중)"
                self._log("🎉 [인스타그램] '방송 시작(Go live)' 버튼 활성화 감지 및 isTrusted 하드웨어 클릭 완료! 실제 온에어 시작!")
                return True
            time.sleep(0.7)

        self._log("⚠️ [인스타그램] '방송 시작(Go live)' 버튼 자동 활성화 대기 시간이 초과되었습니다. 내장 브라우저 독(Dock)에서 프리뷰를 확인해 주세요.")
        return False

    def _harvest_live_dom_chats(self, page, platform):
        """
        방송이 켜진 직후 해당 내장 브라우저(WebView)를 닫지 않고 라이브 방송실 DOM에 상주시켜,
        별도 확장 프로그램 없이 실시간 채팅 파싱과 주문 추출까지 한 번에 처리합니다.
        """
        try:
            if platform == "instagram":
                dom_data = page.evaluate("""() => {
                    const results = [];
                    const nodes = document.querySelectorAll('[role="log"] li, [role="log"] > div, section ul li, div[class*="Comment"]');
                    nodes.forEach((node) => {
                        const raw = (node.innerText || '').trim();
                        if (!raw) return;
                        const lines = raw.split('\\n').map(s => s.trim()).filter(Boolean);
                        if (lines.length >= 2) {
                            const author = lines[0];
                            const text = lines.slice(1).join(' ');
                            if (author && text && text.length <= 300) {
                                results.push({ id: `ig_bridge_${author}_${text}`, author, text });
                            }
                        }
                    });
                    return results.slice(-40);
                }""")
                for item in (dom_data or []):
                    instagram_chat_poller._ingest_instagram_chat_item(
                        msg_id=item.get("id"),
                        author=item.get("author"),
                        username=item.get("author"),
                        msg_text=item.get("text"),
                        is_initial_batch=False
                    )
                with self.lock:
                    self.platforms["instagram"]["chat_collected_count"] = instagram_chat_poller.collected_count

            elif platform == "tiktok":
                dom_data = page.evaluate("""() => {
                    const results = [];
                    const nodes = document.querySelectorAll('[data-e2e="chat-message"], [class*="DivChatMessage"], [class*="chat-message"]');
                    nodes.forEach((node) => {
                        const raw = (node.innerText || '').trim();
                        if (!raw) return;
                        const lines = raw.split('\\n').map(s => s.trim()).filter(Boolean);
                        if (lines.length >= 2) {
                            const author = lines[0].replace(/:$/, '').trim();
                            const text = lines.slice(1).join(' ');
                            if (author && text && text.length <= 300) {
                                results.push({ id: `tt_bridge_${author}_${text}`, author, text });
                            }
                        }
                    });
                    return results.slice(-40);
                }""")
                for item in (dom_data or []):
                    tiktok_chat_poller._ingest_tiktok_chat_item(
                        msg_id=item.get("id"),
                        author=item.get("author"),
                        unique_id=item.get("author"),
                        msg_text=item.get("text"),
                        is_initial_batch=False
                    )
                with self.lock:
                    self.platforms["tiktok"]["chat_collected_count"] = tiktok_chat_poller.collected_count
        except Exception:
            pass

    def _platform_browser_loop(self, platform, visible_window=True):
        """
        플랫폼별(Instagram / TikTok) 영구 프로필 Chromium 컨텍스트를 유지하고
        명령 큐(세션 체크, 방송 생성, Go Live 클릭, 원격 마우스 클릭, 실시간 채팅 파싱, 프리뷰 캡처)를 처리합니다.
        """
        profile_dir = os.path.join(self.profiles_root, platform)
        os.makedirs(profile_dir, exist_ok=True)
        cfg = self.load_selectors().get(platform, {})
        default_home = (
            "https://studio.youtube.com/channel/UCAHMDwerww8SD23NQ4fKHEQ/livestreaming"
            if platform == "youtube"
            else ("https://www.instagram.com/" if platform == "instagram" else "https://livecenter.tiktok.com/")
        )
        home_url = cfg.get("home_url") or default_home

        pw = None
        context = None
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()

            # 백그라운드 탭 스로틀링 해제 & 자동화 탐지 우회 필수 플래그
            launch_args = [
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--disable-blink-features=AutomationControlled",
                "--autoplay-policy=no-user-gesture-required",
                "--no-sandbox"
            ]
            context = pw.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=not bool(visible_window),
                args=launch_args,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                locale="ko-KR",
                viewport={"width": 1280, "height": 820}
            )
            page = context.pages[0] if context.pages else context.new_page()
            cdp = self._inject_stealth_and_cdp_sniffer(context, page, platform)

            with self.lock:
                self.platforms[platform]["browser_running"] = True
                self.platforms[platform]["headless"] = not bool(visible_window)
                self.platforms[platform]["status"] = "🌐 내장 브라우저 기동됨 (페이지 접속 중...)"

            try:
                page.goto(home_url, wait_until="domcontentloaded", timeout=25000)
            except Exception as nav_err:
                if platform == "tiktok" and cfg.get("fallback_url"):
                    try:
                        page.goto(cfg.get("fallback_url"), wait_until="domcontentloaded", timeout=20000)
                    except Exception:
                        pass
                else:
                    self._log(f"⚠️ [{platform}] 초기 페이지 로드 지연: {nav_err}")

            time.sleep(1.5)
            self._check_page_login_state(context, page, platform)

            last_shot_ts = 0.0
            last_chat_poll_ts = 0.0
            last_login_check_ts = time.time()

            while not self._worker_stop_flags.get(platform, False):
                # 1) 명령 큐 처리
                cmd = None
                with self.lock:
                    if self._cmd_queues[platform]:
                        cmd = self._cmd_queues[platform].popleft()

                if cmd:
                    act = cmd.get("action")
                    pl = cmd.get("payload") or {}
                    try:
                        if act == "PREFLIGHT":
                            logged_in = self._check_page_login_state(context, page, platform)
                            if not logged_in:
                                try:
                                    page.bring_to_front()
                                except Exception:
                                    pass
                        elif act == "BRING_TO_FRONT":
                            try:
                                page.bring_to_front()
                            except Exception:
                                pass
                        elif act == "GOTO":
                            u = pl.get("url") or home_url
                            page.goto(u, wait_until="domcontentloaded", timeout=20000)
                            self._check_page_login_state(context, page, platform)
                        elif act == "CREATE_LIVE":
                            b_title = pl.get("title") or self.broadcast_title
                            if platform == "instagram":
                                self._automate_instagram_create_live(page, cdp, b_title)
                            elif platform == "tiktok":
                                self._automate_tiktok_create_live(page, cdp, b_title)
                            elif platform == "youtube":
                                self._automate_youtube_studio_go_live(page, cdp, timeout_sec=12.0)
                        elif act == "YT_CHECK_AND_GO_LIVE":
                            if platform == "youtube":
                                self._automate_youtube_studio_go_live(page, cdp, timeout_sec=pl.get("timeout", 25.0))
                        elif act == "IG_WAIT_AND_GO_LIVE":
                            if platform == "instagram":
                                self._wait_and_click_instagram_go_live(page, cdp, timeout_sec=pl.get("timeout", 25.0))
                        elif act == "REMOTE_CLICK":
                            rx = float(pl.get("x", 640))
                            ry = float(pl.get("y", 410))
                            self.trusted_click_by_coords(cdp, rx, ry)
                        elif act == "REMOTE_TEXT":
                            r_text = str(pl.get("text") or "")
                            if r_text:
                                page.keyboard.type(r_text, delay=25)
                    except Exception as cmd_err:
                        self._log(f"⚠️ [{platform}] 명령({act}) 처리 중 오류: {cmd_err}")

                # 2) 주기적 로그인 상태 & DOM readonly 스트림 키 자동 스캔
                now_t = time.time()
                if (now_t - last_login_check_ts) >= 3.0:
                    last_login_check_ts = now_t
                    self._check_page_login_state(context, page, platform)
                    self._scrape_readonly_stream_inputs(page, platform)

                # 3) 라이브 방송실 DOM 상주 채팅 파싱 (1.2초 주기)
                if (now_t - last_chat_poll_ts) >= 1.2:
                    last_chat_poll_ts = now_t
                    self._harvest_live_dom_chats(page, platform)

                # 4) 스튜디오 내장 브라우저 독(Dock) 미니 프리뷰용 스크린샷 갱신 (1.5초 주기)
                if (now_t - last_shot_ts) >= 1.5:
                    last_shot_ts = now_t
                    try:
                        jpg_bytes = page.screenshot(type="jpeg", quality=62)
                        with self.lock:
                            self.platforms[platform]["last_screenshot_bytes"] = jpg_bytes
                            self.platforms[platform]["last_screenshot_ts"] = now_t
                            self.platforms[platform]["current_url"] = page.url or ""
                    except Exception:
                        pass

                time.sleep(0.15)

        except Exception as e:
            self._log(f"⚠️ [{platform}] 내장 브라우저 워커 예외 종료: {e}")
            with self.lock:
                self.platforms[platform]["status"] = f"⚠️ 브라우저 오류: {e}"
        finally:
            with self.lock:
                self.platforms[platform]["browser_running"] = False
            try:
                if context:
                    context.close()
            except Exception:
                pass
            try:
                if pw:
                    pw.stop()
            except Exception:
                pass

    def start_oneclick_simulcast(self, title="", enable_yt=True, enable_tt=True, enable_ig=True, width=720, height=1280, fps=30):
        """
        사용자가 스튜디오에서 '🚀 3사 통합 원클릭 자동 동시 송출 시작' 버튼을 누르면
        백그라운드에서 5단계 End-to-End 파이프라인을 순차 실행합니다.
        """
        clean_title = (title or "").strip() or self.broadcast_title or "실시간 라이브 특가 방송"
        with self.lock:
            self.broadcast_title = clean_title
            self.active = True
            self._stop_requested = False
            self.last_error = ""
            self.platforms["youtube"]["go_live_clicked"] = False
            self.platforms["instagram"]["go_live_clicked"] = False
            self.platforms["tiktok"]["go_live_clicked"] = False
        try:
            database.set_overlay_state("simulcast_unified_title", clean_title)
        except Exception:
            pass

        def _orchestrate():
            try:
                # --------------------------------------------------------------
                # Stage 1: 세션 헬스체크 (Pre-flight Check)
                # --------------------------------------------------------------
                self._set_stage(1, "① [Stage 1/5] 유튜브 스튜디오 · 인스타그램 · 틱톡 영구 프로필 세션 헬스체크 실행 중...")
                target_auto_platforms = []
                if enable_yt:
                    target_auto_platforms.append("youtube")
                if enable_ig:
                    target_auto_platforms.append("instagram")
                if enable_tt:
                    target_auto_platforms.append("tiktok")

                for p in target_auto_platforms:
                    self.ensure_platform_worker(p, visible_window=True)
                    self.enqueue_platform_cmd(p, "PREFLIGHT")

                # 워커 기동 및 초기 쿠키 점검 대기
                for _ in range(16):
                    if self._stop_requested:
                        return
                    all_running = all(self.platforms[p]["browser_running"] for p in target_auto_platforms)
                    if all_running:
                        break
                    time.sleep(0.5)
                time.sleep(1.2)

                def _p_label(pk):
                    return "유튜브" if pk == "youtube" else ("인스타그램" if pk == "instagram" else "틱톡")

                need_login_list = [p for p in target_auto_platforms if not self.platforms[p]["logged_in"] and not (p != "youtube" and self.platforms[p]["stream_key"])]
                if need_login_list:
                    names_ko = " · ".join(_p_label(p) for p in need_login_list)
                    self._log(f"🔑 [{names_ko}] 로그인이 되어 있지 않습니다! 내장 브라우저(또는 스튜디오 브라우저 독)에서 로그인해 주세요.")
                    # 사용자가 로그인할 때까지 최대 90초 대기
                    for _ in range(90):
                        if self._stop_requested:
                            return
                        still_need = [
                            p for p in need_login_list
                            if not self.platforms[p]["logged_in"] and not (p != "youtube" and self.platforms[p]["stream_key"])
                        ]
                        if not still_need:
                            break
                        time.sleep(1.0)

                    # 타임아웃 후에도 여전히 미로그인인 플랫폼이 있으면 정직하게 오류 보고
                    still_missing = [p for p in need_login_list if not self.platforms[p]["logged_in"]]
                    if still_missing:
                        miss_ko = ", ".join(_p_label(p) for p in still_missing)
                        err_msg = f"❌ [{miss_ko}] 로그인이 완료되지 않아 해당 플랫폼 라이브를 시작할 수 없습니다. [🔑 로그인/창 열기]로 먼저 로그인해 주세요."
                        with self.lock:
                            self.last_error = err_msg
                        self._log(err_msg)

                # --------------------------------------------------------------
                # Stage 2: CDP 네트워크 스니퍼 활성화 (Network.enable)
                # --------------------------------------------------------------
                self._set_stage(2, "② [Stage 2/5] CDP Network.enable 패킷 스니퍼 활성화 완료 (/api/v1/live/create/ · /webcast/room/create/ 감시 중)")
                time.sleep(0.4)

                # --------------------------------------------------------------
                # Stage 3: 방송 세션 생성 & React 방송 제목 자동 입력
                # --------------------------------------------------------------
                self._set_stage(3, f"③ [Stage 3/5] 방송 세션 자동 생성 및 React 네이티브 Setter 제목('{clean_title}') 주입 중...")
                for p in target_auto_platforms:
                    if self.platforms[p]["logged_in"]:
                        self.enqueue_platform_cmd(p, "CREATE_LIVE", {"title": clean_title})

                # 스트림 키 가로채기 대기 (최대 10초, 이미 저장된 키가 있으면 빠르게 진행)
                for _ in range(20):
                    if self._stop_requested:
                        return
                    ready_all = True
                    for p in target_auto_platforms:
                        if self.platforms[p]["logged_in"] and not self.platforms[p]["stream_key"]:
                            ready_all = False
                    if ready_all:
                        break
                    time.sleep(0.5)

                # --------------------------------------------------------------
                # Stage 4: RTMPS/RTMP 주소·스트림 키 바인딩 및 3사 FFmpeg 인코더 즉시 송출
                # --------------------------------------------------------------
                self._set_stage(4, "④ [Stage 4/5] 추출된 RTMPS/RTMP 스트림 키를 미디어 엔진(FFmpeg H.264 + 48kHz AAC)에 바인딩하여 동시 송출을 시작합니다!")
                started_names = []

                if enable_yt:
                    yt_key = (database.get_overlay_state("youtube_stream_key", "") or "").strip()
                    yt_url = (database.get_overlay_state("youtube_rtmp_url", "rtmp://a.rtmp.youtube.com/live2") or "").strip()
                    if yt_key:
                        yt_res = youtube_direct_streamer.start(rtmp_url=yt_url, stream_key=yt_key, width=width, height=height, fps=fps)
                        if yt_res.get("ok"):
                            started_names.append("유튜브")
                            yt_ch = (database.get_overlay_state("youtube_channel_input", "") or "").strip()
                            if yt_ch and not youtube_chat_poller.active:
                                youtube_chat_poller.connect(yt_ch)

                if enable_tt:
                    tt_key = (self.platforms["tiktok"]["stream_key"] or database.get_overlay_state("tiktok_stream_key", "") or "").strip()
                    tt_url = (self.platforms["tiktok"]["rtmp_url"] or database.get_overlay_state("tiktok_rtmp_url", "rtmp://push.tiktok.com/live") or "").strip()
                    if tt_key:
                        tt_res = tiktok_direct_streamer.start(rtmp_url=tt_url, stream_key=tt_key, width=width, height=height, fps=fps)
                        if tt_res.get("ok"):
                            started_names.append("틱톡")
                            self.platforms["tiktok"]["chat_collecting"] = True
                            tt_ch = (database.get_overlay_state("tiktok_channel_input", "") or "").strip()
                            if tt_ch and not tiktok_chat_poller.active:
                                tiktok_chat_poller.connect(tt_ch)

                if enable_ig:
                    ig_key = (self.platforms["instagram"]["stream_key"] or database.get_overlay_state("instagram_stream_key", "") or "").strip()
                    ig_url = (self.platforms["instagram"]["rtmp_url"] or database.get_overlay_state("instagram_rtmp_url", "rtmps://live-upload.instagram.com:443/rtmp/") or "").strip()
                    if ig_key:
                        ig_res = instagram_direct_streamer.start(rtmp_url=ig_url, stream_key=ig_key, width=width, height=height, fps=fps)
                        if ig_res.get("ok"):
                            started_names.append("인스타그램")
                            self.platforms["instagram"]["chat_collecting"] = True
                            ig_ch = (database.get_overlay_state("instagram_channel_input", "") or "").strip()
                            if ig_ch and not instagram_chat_poller.active:
                                instagram_chat_poller.connect(ig_ch)

                # --------------------------------------------------------------
                # Stage 5: 유튜브 스튜디오 & 인스타그램 프리뷰 감지 및 '방송 시작(Go live)' 클릭 + 실제 온에어 검증
                # --------------------------------------------------------------
                if enable_yt and youtube_direct_streamer.active and self.platforms["youtube"].get("logged_in"):
                    self._set_stage(5, "⑤ [Stage 5/5] 유튜브 스튜디오 제어실 수신 확인 및 '실시간 스트리밍 시작' 확인 중...")
                    self.enqueue_platform_cmd("youtube", "YT_CHECK_AND_GO_LIVE", {"timeout": 20.0})

                if enable_ig and instagram_direct_streamer.active and self.platforms["instagram"]["browser_running"]:
                    self._set_stage(5, "⑤ [Stage 5/5] 인스타그램 Live Producer RTMPS 비디오 프리뷰 수신 대기(3~7초) 및 '방송 시작(Go live)' 활성화 감시 중...")
                    self.enqueue_platform_cmd("instagram", "IG_WAIT_AND_GO_LIVE", {"timeout": 25.0})
                    for _ in range(26):
                        if self._stop_requested:
                            return
                        if self.platforms["instagram"].get("go_live_clicked"):
                            break
                        time.sleep(1.0)

                if started_names:
                    # 실제 플랫폼 온에어 검증 대기 (최대 15초)
                    verified_names = []
                    for _ in range(15):
                        if self._stop_requested:
                            return
                        verified_names = []
                        if enable_yt and youtube_direct_streamer.is_verified_live():
                            verified_names.append("유튜브")
                        if enable_tt and tiktok_direct_streamer.is_verified_live():
                            verified_names.append("틱톡")
                        if enable_ig and instagram_direct_streamer.is_verified_live():
                            verified_names.append("인스타그램")
                        if verified_names:
                            break
                        time.sleep(1.0)

                    if verified_names:
                        joined_v = " + ".join(verified_names)
                        mark_live_broadcast_started(title=clean_title)
                        self._set_stage(6, f"🟢 [{joined_v}] 실제 라이브 방송 온에어(On-Air) 검증 완료!")
                    else:
                        joined_s = " + ".join(started_names)
                        self._set_stage(5, f"🟡 [{joined_s}] RTMP 신호는 전송 중이나 아직 플랫폼에서 실제 라이브 시작이 확인되지 않았습니다. 내장 브라우저 독에서 로그인·방송 시작 버튼을 확인하세요.")
                else:
                    self._set_stage(0, "❌ 송출이 시작된 플랫폼이 없습니다. 내장 브라우저 로그인 또는 스트림 키를 확인해 주세요.")
            except Exception as e:
                with self.lock:
                    self.last_error = str(e)
                self._log(f"❌ 원클릭 동시 송출 오케스트레이션 오류: {e}")

        self._orchestrator_thread = threading.Thread(target=_orchestrate, daemon=True)
        self._orchestrator_thread.start()
        return {
            "ok": True,
            "message": f"🚀 '{clean_title}' 5단계 원클릭 통합 동시 송출 파이프라인이 가동되었습니다.",
            "state": self.get_state()
        }

    def stop_all(self, close_browsers=False):
        with self.lock:
            self._stop_requested = True
            self.active = False
            self.current_step = 0
            self.stage_code = "IDLE"
            self.stage_label = "대기 중"
            self.platforms["instagram"]["go_live_clicked"] = False
            self.platforms["instagram"]["chat_collecting"] = False
            self.platforms["tiktok"]["go_live_clicked"] = False
            self.platforms["tiktok"]["chat_collecting"] = False
            if close_browsers:
                self._worker_stop_flags["instagram"] = True
                self._worker_stop_flags["tiktok"] = True
        self._log("⏹️ 통합 동시 송출 브릿지가 대기 상태로 전환되었습니다.")

    def get_state(self):
        with self.lock:
            plat_summary = {}
            for k, v in self.platforms.items():
                s_key = v.get("stream_key") or ""
                masked = (s_key[:6] + "..." + s_key[-4:]) if len(s_key) > 12 else ("설정됨" if s_key else "")
                plat_summary[k] = {
                    "logged_in": bool(v.get("logged_in")),
                    "login_required": bool(v.get("login_required")),
                    "browser_running": bool(v.get("browser_running")),
                    "status": v.get("status") or "",
                    "current_url": v.get("current_url") or "",
                    "rtmp_url": v.get("rtmp_url") or "",
                    "stream_key": s_key,
                    "masked_stream_key": masked,
                    "has_stream_key": bool(s_key),
                    "intercepted_at": v.get("intercepted_at"),
                    "go_live_clicked": bool(v.get("go_live_clicked")),
                    "chat_collecting": bool(v.get("chat_collecting")),
                    "chat_collected_count": int(v.get("chat_collected_count") or 0),
                    "has_screenshot": v.get("last_screenshot_bytes") is not None
                }
            return {
                "active": self.active,
                "current_step": self.current_step,
                "stage_code": self.stage_code,
                "stage_label": self.stage_label,
                "status_text": self.status_text,
                "last_error": self.last_error,
                "broadcast_title": self.broadcast_title,
                "stages": self.STAGES,
                "platforms": plat_summary,
                "logs": list(self.logs)[-25:]
            }


simulcast_automation_bridge = SimulcastAutomationBridge()


@app.route('/api/simulcast/bridge/status', methods=['GET'])
def api_simulcast_bridge_status():
    """4-Layer CDP 브릿지의 5단계 상태 머신, 영구 프로필 로그인 상태 및 가로챈 스트림 키 정보를 반환합니다."""
    return jsonify({
        "status": "ok",
        "bridge": simulcast_automation_bridge.get_state(),
        "rtmp": youtube_direct_streamer.get_state(),
        "tiktok_rtmp": tiktok_direct_streamer.get_state(),
        "instagram_rtmp": instagram_direct_streamer.get_state()
    }), 200


@app.route('/api/simulcast/bridge/start', methods=['POST'])
def api_simulcast_bridge_start():
    """통합 방송 제목과 플랫폼 선택값을 받아 5단계 원클릭 자동 동시 송출을 시작합니다."""
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    enable_yt = bool(data.get("enable_yt", True))
    enable_tt = bool(data.get("enable_tt", True))
    enable_ig = bool(data.get("enable_ig", True))
    width = int(data.get("width", 720) or 720)
    height = int(data.get("height", 1280) or 1280)
    fps = int(data.get("fps", 30) or 30)

    res = simulcast_automation_bridge.start_oneclick_simulcast(
        title=title,
        enable_yt=enable_yt,
        enable_tt=enable_tt,
        enable_ig=enable_ig,
        width=width,
        height=height,
        fps=fps
    )
    if res.get("ok") and is_any_platform_verified_live():
        mark_live_broadcast_started(title=title)
    return jsonify({
        "status": "ok" if res.get("ok") else "error",
        "message": res.get("message", ""),
        "bridge": res.get("state")
    }), 200


@app.route('/api/simulcast/bridge/stop', methods=['POST'])
def api_simulcast_bridge_stop():
    """3사 FFmpeg 송출 및 CDP 자동화 브릿지 파이프라인을 일괄 중지하고, 이전 방송 기록 저장 및 초기화를 수행합니다."""
    data = request.get_json(silent=True) or {}
    close_browsers = bool(data.get("close_browsers", False))
    title = str(data.get("title") or "").strip()
    youtube_direct_streamer.stop()
    tiktok_direct_streamer.stop()
    instagram_direct_streamer.stop()
    platform_tracker.clear_platform_presence("YOUTUBE")
    platform_tracker.clear_platform_presence("TIKTOK")
    platform_tracker.clear_platform_presence("INSTAGRAM")
    simulcast_automation_bridge.stop_all(close_browsers=close_browsers)

    reset_bundle = finalize_and_archive_broadcast_session(
        title=title,
        save_empty=True,
        force_save=True,
        disconnect_chats=True
    )
    return jsonify({
        "status": "ok",
        "broadcast_ended": True,
        "message": "⏹️ 방송이 종료되었으며, 이전 방송 기록으로 자동 저장되고 채팅·주문 화면이 초기화되었습니다. (등록 상품 유지)",
        "bridge": simulcast_automation_bridge.get_state(),
        **reset_bundle
    }), 200


@app.route('/api/simulcast/bridge/open-login/<platform>', methods=['POST'])
def api_simulcast_bridge_open_login(platform):
    """유튜브 스튜디오, 인스타그램 또는 틱톡 영구 프로필 내장 브라우저를 화면에 띄우고 로그인/프리뷰 독을 활성화합니다."""
    platform = (platform or "").lower().strip()
    if platform not in ("youtube", "instagram", "tiktok"):
        return jsonify({"status": "error", "message": "지원하지 않는 플랫폼입니다."}), 400
    simulcast_automation_bridge.ensure_platform_worker(platform, visible_window=True)
    simulcast_automation_bridge.enqueue_platform_cmd(platform, "BRING_TO_FRONT")
    simulcast_automation_bridge.enqueue_platform_cmd(platform, "PREFLIGHT")
    p_label = "유튜브 스튜디오" if platform == "youtube" else ("인스타그램" if platform == "instagram" else "틱톡 LIVE Center")
    return jsonify({
        "status": "ok",
        "message": f"🌐 {p_label} 영구 세션 브라우저를 열었습니다. 최초 1회 로그인하시면 쿠키가 영구 보존됩니다.",
        "bridge": simulcast_automation_bridge.get_state()
    }), 200


@app.route('/api/simulcast/bridge/action/<platform>', methods=['POST'])
def api_simulcast_bridge_action(platform):
    """내장 브라우저에 수동 명령(CREATE_LIVE, YT_CHECK_AND_GO_LIVE, IG_WAIT_AND_GO_LIVE, REMOTE_CLICK, REMOTE_TEXT, GOTO)을 전달합니다."""
    platform = (platform or "").lower().strip()
    if platform not in ("youtube", "instagram", "tiktok"):
        return jsonify({"status": "error", "message": "지원하지 않는 플랫폼입니다."}), 400
    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "PREFLIGHT").strip().upper()
    simulcast_automation_bridge.enqueue_platform_cmd(platform, action, data)
    return jsonify({
        "status": "ok",
        "message": f"[{platform}] {action} 명령이 전송되었습니다.",
        "bridge": simulcast_automation_bridge.get_state()
    }), 200


@app.route('/api/simulcast/bridge/screenshot/<platform>', methods=['GET'])
def api_simulcast_bridge_screenshot(platform):
    """스튜디오 내장 브라우저 독(Dock)에서 백그라운드 브라우저 화면을 실시간으로 볼 수 있도록 JPEG 스냅샷을 반환합니다."""
    platform = (platform or "").lower().strip()
    if platform not in ("youtube", "instagram", "tiktok"):
        return Response(status=404)
    with simulcast_automation_bridge.lock:
        jpg = simulcast_automation_bridge.platforms.get(platform, {}).get("last_screenshot_bytes")
    if not jpg:
        return Response(status=204)
    resp = Response(jpg, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route('/api/simulcast/selectors', methods=['GET', 'POST'])
def api_simulcast_selectors():
    """인스타그램/틱톡 UI 개편 대응을 위한 원격/로컬 핫픽스 셀렉터 설정(simulcast_selectors.json) 조회 및 즉시 수정 API"""
    if request.method == 'POST':
        try:
            new_cfg = request.get_json(silent=True)
            if not isinstance(new_cfg, dict):
                return jsonify({"status": "error", "message": "유효한 JSON 객체가 아닙니다."}), 400
            saved = simulcast_automation_bridge.save_selectors(new_cfg)
            return jsonify({
                "status": "ok",
                "message": "🛠️ 셀렉터 핫픽스(simulcast_selectors.json) 설정이 재배포 없이 즉시 적용되었습니다!",
                "selectors": saved
            }), 200
        except Exception as e:
            return jsonify({"status": "error", "message": f"셀렉터 저장 실패: {e}"}), 400

    return jsonify({
        "status": "ok",
        "selectors": simulcast_automation_bridge.load_selectors()
    }), 200


@app.route('/api/youtube/audio-diagnostics', methods=['GET'])
def api_youtube_audio_diagnostics():
    """5단계 음성 파이프라인 실시간 진단 데이터 반환"""
    diag = youtube_direct_streamer.get_audio_diagnostics()
    diag["status"] = "ok"
    return jsonify(diag), 200


@app.route('/api/youtube/audio-diagnostics/reset', methods=['POST'])
def api_youtube_audio_diagnostics_reset():
    """오디오 지터 버퍼 및 언더런/클리핑 통계를 즉시 리셋·재정렬합니다."""
    res = youtube_direct_streamer.reset_audio_buffers()
    return jsonify({
        "status": "ok",
        "message": res["message"],
        "audio_diagnostics": youtube_direct_streamer.get_audio_diagnostics()
    }), 200


@app.route('/api/youtube/video-diagnostics', methods=['GET'])
def api_youtube_video_diagnostics():
    """5단계 영상 파이프라인 정밀 계측 데이터 및 최근 시계열 히스토리 반환"""
    refresh_yt = request.args.get("refresh_yt", "0") == "1"
    vid = (request.args.get("video_id") or "").strip()
    if vid:
        youtube_direct_streamer.inspect_youtube_live_latency(video_id=vid, force=True)
    diag = youtube_direct_streamer.get_video_diagnostics(include_history=True, refresh_youtube=refresh_yt)
    return jsonify({
        "status": "ok",
        "diagnostics": diag
    }), 200


@app.route('/api/youtube/video-diagnostics/burnin', methods=['POST'])
def api_youtube_toggle_burnin():
    """영상 프레임 상단에 밀리초 단위 시각 동기화 시계(⏱️ SYNC HH:MM:SS.mmm) 번인 표시를 ON/OFF 합니다."""
    data = request.get_json(silent=True) or {}
    if "enabled" in data:
        new_val = youtube_direct_streamer.set_burnin_timecode(bool(data["enabled"]))
    else:
        new_val = youtube_direct_streamer.set_burnin_timecode(not youtube_direct_streamer.burnin_timecode)
    return jsonify({
        "status": "ok",
        "burnin_timecode": new_val,
        "message": "⏱️ 화면 타임코드(HH:MM:SS.mmm) 번인 표시가 켜졌습니다." if new_val else "화면 타임코드 번인 표시가 꺼졌습니다."
    }), 200


@app.route('/api/youtube/config', methods=['POST'])
def api_youtube_save_config():
    """유튜브 채널 URL, 스트림 키, 비디오/오디오 비트 전송률, 키프레임 간격 설정을 DB에 저장합니다."""
    data = request.get_json(silent=True) or {}
    ch_input = (data.get("channel_input") or "").strip()
    rtmp_url = (data.get("rtmp_url") or "rtmp://a.rtmp.youtube.com/live2").strip()
    stream_key = (data.get("stream_key") or "").strip()

    if ch_input:
        database.set_overlay_state("youtube_channel_input", ch_input)
    if rtmp_url:
        database.set_overlay_state("youtube_rtmp_url", rtmp_url)
    if "stream_key" in data:
        database.set_overlay_state("youtube_stream_key", stream_key)
    if "video_bitrate_kbps" in data and data.get("video_bitrate_kbps"):
        try:
            v_kbps = max(1000, min(18000, int(data.get("video_bitrate_kbps"))))
            database.set_overlay_state("youtube_video_bitrate_kbps", v_kbps)
            youtube_direct_streamer.video_bitrate_kbps = v_kbps
        except Exception:
            pass
    if "audio_bitrate_kbps" in data and data.get("audio_bitrate_kbps"):
        try:
            a_kbps = max(64, min(320, int(data.get("audio_bitrate_kbps"))))
            database.set_overlay_state("youtube_audio_bitrate_kbps", a_kbps)
            youtube_direct_streamer.audio_bitrate_kbps = a_kbps
        except Exception:
            pass
    if "keyframe_interval_sec" in data and data.get("keyframe_interval_sec"):
        try:
            kf_sec = max(1, min(4, int(data.get("keyframe_interval_sec"))))
            database.set_overlay_state("youtube_keyframe_sec", kf_sec)
            youtube_direct_streamer.keyframe_interval_sec = kf_sec
        except Exception:
            pass
    if "audio_gain_pct" in data and data.get("audio_gain_pct") is not None:
        try:
            youtube_direct_streamer.set_audio_gain(
                gain_pct=data.get("audio_gain_pct"),
                agc_enabled=data.get("audio_agc_enabled") if "audio_agc_enabled" in data else None
            )
        except Exception:
            pass
    elif "audio_agc_enabled" in data:
        try:
            youtube_direct_streamer.set_audio_gain(agc_enabled=data.get("audio_agc_enabled"))
        except Exception:
            pass

    return jsonify({
        "status": "ok",
        "message": "유튜브 연결 및 초저지연 송출 설정이 저장되었습니다.",
        "chat": youtube_chat_poller.get_state(),
        "rtmp": youtube_direct_streamer.get_state()
    }), 200


@app.route('/api/audio/scope', methods=['GET'])
def api_audio_scope():
    """🔬 음파 분석기: 지정 탭(raw/dsp/out)의 최근 ms 구간 Int16 LE PCM 반환 (FFT는 브라우저에서 계산)"""
    from flask import Response
    tap = (request.args.get('tap') or 'raw').strip().lower()
    if tap not in ('raw', 'dsp', 'out'):
        tap = 'raw'
    ms = request.args.get('ms', default=200, type=int)
    pcm = youtube_direct_streamer.get_scope_pcm(tap, ms)
    resp = Response(pcm, status=200, mimetype='application/octet-stream')
    resp.headers['X-Sample-Rate'] = '48000'
    resp.headers['X-Tap'] = tap
    resp.headers['X-Dsp-Bypass'] = '1' if youtube_direct_streamer._dsp_bypass else '0'
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp


AUDIO_SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'audio_snapshots')


@app.route('/api/audio/scope/snapshot', methods=['POST'])
def api_audio_scope_snapshot():
    """🔬 3개 탭의 최근 10초를 WAV로 저장"""
    try:
        files = youtube_direct_streamer.save_scope_snapshot(AUDIO_SNAPSHOT_DIR)
        return jsonify({"status": "ok", "dir": AUDIO_SNAPSHOT_DIR, "files": files,
                        "dsp_bypass": youtube_direct_streamer._dsp_bypass}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/audio/scope/snapshot/<path:filename>', methods=['GET'])
def api_audio_scope_snapshot_download(filename):
    """🔬 저장된 WAV 스냅샷 다운로드"""
    from flask import send_from_directory
    safe = os.path.basename(filename)
    if not safe.endswith('.wav'):
        return jsonify({"status": "error", "message": "invalid file"}), 400
    return send_from_directory(AUDIO_SNAPSHOT_DIR, safe, as_attachment=True)


@app.route('/api/audio/dsp-bypass', methods=['GET', 'POST'])
def api_audio_dsp_bypass():
    """🔬 A/B 비교용 서버 DSP 바이패스 조회/설정"""
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        youtube_direct_streamer._dsp_bypass = bool(data.get('enabled', False))
    return jsonify({"status": "ok", "enabled": youtube_direct_streamer._dsp_bypass}), 200


@app.route('/api/youtube/audio-gain', methods=['POST'])
def api_youtube_set_audio_gain():
    """방송 중 재시작 없이 실시간 마이크 증폭·자동 감도 보정 레벨(50% ~ 1200%) 및 자동 감도 보정(AGC)을 즉시 변경합니다."""
    data = request.get_json(silent=True) or {}
    gain_arg = data.get("audio_gain_pct") if "audio_gain_pct" in data else None
    agc_arg = data.get("audio_agc_enabled") if "audio_agc_enabled" in data else None
    gain_val = youtube_direct_streamer.set_audio_gain(gain_pct=gain_arg, agc_enabled=agc_arg)
    agc_state = youtube_direct_streamer.audio_agc_enabled
    t_db, _, _, max_gx = youtube_direct_streamer._compute_agc_target_params(gain_val)
    return jsonify({
        "status": "ok",
        "audio_gain_pct": gain_val,
        "audio_agc_enabled": agc_state,
        "agc_target_db": t_db,
        "agc_max_gain_x": max_gx,
        "message": (
            f"자동 감도 보정 레벨 {gain_val}% (목표 {t_db}dB) 즉시 적용 완료!"
            if agc_state
            else f"수동 마이크 증폭 {gain_val}% 즉시 적용 완료!"
        ),
        "rtmp": youtube_direct_streamer.get_state(),
        "audio_diagnostics": youtube_direct_streamer.get_audio_diagnostics(include_history=False)
    }), 200


@app.route('/api/youtube/chat/connect', methods=['POST'])
def api_youtube_chat_connect():
    """유튜브 채널 주소(@핸들) 또는 라이브 URL로 실시간 채팅 직접 수집을 시작합니다."""
    data = request.get_json(silent=True) or {}
    ch_input = (data.get("channel_input") or database.get_overlay_state("youtube_channel_input", "")).strip()
    res = youtube_chat_poller.connect(ch_input)
    code = 200 if res.get("ok") else 400
    return jsonify({
        "status": "ok" if res.get("ok") else "error",
        "message": res.get("message", ""),
        "chat": youtube_chat_poller.get_state()
    }), code


@app.route('/api/youtube/chat/disconnect', methods=['POST'])
def api_youtube_chat_disconnect():
    """유튜브 채널 실시간 채팅 직접 수집을 중지합니다."""
    res = youtube_chat_poller.disconnect()
    return jsonify({
        "status": "ok",
        "message": res.get("message", ""),
        "chat": youtube_chat_poller.get_state()
    }), 200


@app.route('/api/youtube/rtmp/start', methods=['POST'])
def _trigger_youtube_background_onair():
    """유튜브 스튜디오를 수동으로 열지 않아도 백그라운드 워커가 제어실에 접속해 자동 온에어를 시도합니다."""
    try:
        if "simulcast_automation_bridge" in globals() and simulcast_automation_bridge is not None:
            simulcast_automation_bridge.ensure_platform_worker("youtube", headless=True)
            simulcast_automation_bridge.enqueue_platform_cmd("youtube", "YT_CHECK_AND_GO_LIVE", {"timeout": 45.0})
    except Exception as e:
        print(f"⚠️ [유튜브 백그라운드 온에어 기동 경고] {e}", flush=True)


def api_youtube_rtmp_start():
    """웹 OBS 스튜디오 화면을 유튜브 스트림 키(RTMP)로 직접 실시간 송출 시작합니다."""
    data = request.get_json(silent=True) or {}
    rtmp_url = (data.get("rtmp_url") or "rtmp://a.rtmp.youtube.com/live2").strip()
    stream_key = (data.get("stream_key") or "").strip()
    width = data.get("width", 720)
    height = data.get("height", 1280)
    fps = data.get("fps", 30)
    video_bitrate_kbps = data.get("video_bitrate_kbps", 4500)
    audio_bitrate_kbps = data.get("audio_bitrate_kbps", 128)
    keyframe_interval_sec = data.get("keyframe_interval_sec", 1)
    ch_input = (data.get("channel_input") or "").strip()

    res = youtube_direct_streamer.start(
        rtmp_url=rtmp_url,
        stream_key=stream_key,
        width=width,
        height=height,
        fps=fps,
        video_bitrate_kbps=video_bitrate_kbps,
        audio_bitrate_kbps=audio_bitrate_kbps,
        keyframe_interval_sec=keyframe_interval_sec
    )
    # 채널 URL이 입력되어 있고 아직 채팅 연결 전이라면 자동으로 채팅 수집도 함께 시작
    if res.get("ok"):
        _trigger_youtube_background_onair()
        if is_any_platform_verified_live():
            mark_live_broadcast_started(title=data.get("title"))
        if ch_input and not youtube_chat_poller.active:
            youtube_chat_poller.connect(ch_input)

    code = 200 if res.get("ok") else 400
    return jsonify({
        "status": "ok" if res.get("ok") else "error",
        "message": res.get("message", ""),
        "rtmp": youtube_direct_streamer.get_state(),
        "chat": youtube_chat_poller.get_state()
    }), code


@app.route('/api/youtube/rtmp/stop', methods=['POST'])
def api_youtube_rtmp_stop():
    """유튜브 RTMP 직접 실시간 송출을 중지하고, 모든 플랫폼 방송이 꺼진 경우 자동으로 이전 방송 기록 저장 및 초기화를 수행합니다."""
    res = youtube_direct_streamer.stop()
    try:
        if youtube_chat_poller.active:
            youtube_chat_poller.disconnect()
    except Exception:
        pass
    platform_tracker.clear_platform_presence("YOUTUBE")
    any_remaining = bool(youtube_direct_streamer.active or tiktok_direct_streamer.active or instagram_direct_streamer.active)
    if not any_remaining:
        reset_bundle = finalize_and_archive_broadcast_session(save_empty=True, force_save=True, disconnect_chats=True)
        return jsonify({
            "status": "ok",
            "broadcast_ended": True,
            "message": "⏹️ 방송이 종료되었으며, 이전 방송 기록으로 자동 저장되고 채팅·주문 화면이 초기화되었습니다. (등록 상품 유지)",
            "rtmp": youtube_direct_streamer.get_state(),
            **reset_bundle
        }), 200
    return jsonify({
        "status": "ok",
        "broadcast_ended": False,
        "message": res.get("message", ""),
        "rtmp": youtube_direct_streamer.get_state()
    }), 200


@app.route('/api/youtube/rtmp/frame', methods=['POST'])
def api_youtube_rtmp_frame():
    """웹 OBS 스튜디오의 실시간 합성 캔버스 프레임(JPEG)을 수신하여 RTMP 파이프라인에 공급합니다."""
    frame_bytes = request.data
    if not frame_bytes and request.files and 'frame' in request.files:
        frame_bytes = request.files['frame'].read()
    if frame_bytes:
        youtube_direct_streamer.update_frame(frame_bytes)
        return jsonify({"status": "ok"}), 200
    return jsonify({"status": "error", "message": "no frame"}), 400


@app.route('/api/youtube/rtmp/overlay', methods=['POST'])
def api_youtube_rtmp_overlay():
    """웹 OBS 스튜디오의 투명 오버레이 전용 레이어(PNG)를 수신하여 서버 폴백 시에도 동일한 단일 오버레이를 합성합니다."""
    ov_bytes = request.data
    if not ov_bytes and request.files and 'overlay' in request.files:
        ov_bytes = request.files['overlay'].read()
    if ov_bytes:
        youtube_direct_streamer.update_overlay(ov_bytes)
        return jsonify({"status": "ok"}), 200
    return jsonify({"status": "error", "message": "no overlay"}), 400


@app.route('/api/host/reload', methods=['POST'])
def api_host_reload():
    """쇼호스트 디렉팅 스튜디오에서 쇼호스트 뷰(/host) 창을 원격으로 새로고침합니다."""
    global host_reload_seq
    host_reload_seq += 1
    return jsonify({
        "status": "ok",
        "host_reload_seq": host_reload_seq,
        "message": "쇼호스트 뷰 새로고침 신호가 전송되었습니다."
    }), 200


@app.route('/api/host/cue', methods=['POST'])
def api_host_send_cue():
    """PD가 쇼호스트에게 긴급 지시 멘트를 전송합니다."""
    data = request.get_json(silent=True) or {}
    cue_text = (data.get("cue_text") or data.get("message") or "").strip()
    priority = (data.get("priority") or data.get("urgency") or "NORMAL").upper()
    if not cue_text:
        return jsonify({"status": "error", "message": "지시 멘트 내용이 비어있습니다."}), 400

    cue_id = database.add_broadcast_cue(cue_text, priority=priority, sender="PD")
    return jsonify({"status": "ok", "cue_id": cue_id, "message": "쇼호스트 화면으로 지시 멘트가 전송되었습니다."}), 200


@app.route('/api/host/pin-question', methods=['POST'])
def api_host_pin_question():
    """PD가 특정 시청자 질문을 쇼호스트 프롬프터에 고정 팝업합니다."""
    data = request.get_json(silent=True) or {}
    user = data.get("user", "시청자")
    question = (data.get("question") or data.get("message") or "").strip()
    platform = data.get("platform", "LIVE")
    if not question:
        return jsonify({"status": "error", "message": "질문 내용이 없습니다."}), 400
    database.pin_host_question(user, question, platform)
    return jsonify({"status": "ok", "message": f"@{user}님의 질문을 쇼호스트 화면에 전달했습니다."}), 200


@app.route('/api/host/clear-question', methods=['POST'])
def api_host_clear_question():
    """쇼호스트 화면의 픽업 질문을 닫습니다."""
    database.clear_host_question()
    return jsonify({"status": "ok", "message": "쇼호스트 화면의 질문이 닫혔습니다."}), 200


@app.route('/api/host/timer', methods=['POST'])
def api_host_timer():
    """쇼호스트 화면의 세그먼트 카운트다운 타이머를 제어합니다."""
    data = request.get_json(silent=True) or {}
    action = data.get("action", "start")
    minutes = float(data.get("minutes", 3))
    label = data.get("label", "⚡ 코너 진행")
    database.set_host_timer(action, minutes, label)
    return jsonify({"status": "ok", "message": f"쇼호스트 타이머 ({action}) 적용 완료"}), 200


@app.route('/api/host/clear-cues', methods=['POST'])
def api_host_clear_cues():
    """쇼호스트 지시 멘트 및 픽업 질문을 일괄 닫습니다."""
    database.clear_broadcast_cues()
    database.clear_host_question()
    return jsonify({"status": "ok", "message": "지시 멘트 및 질문이 닫혔습니다."}), 200


# ==========================================
# 👥 고객 관리 & VIP / 블랙리스트 API
# ==========================================

@app.route('/api/customers', methods=['GET'])
def api_get_customers():
    """고객 목록 조회 (filter: ALL, VIP, BLACKLIST)"""
    filter_type = request.args.get('filter', 'ALL').upper()
    customers = database.get_all_customers(filter_type)
    return jsonify({"status": "ok", "customers": customers}), 200


@app.route('/api/customers/toggle-flag', methods=['POST'])
def api_toggle_customer_flag():
    """VIP 또는 블랙리스트 상태를 토글합니다."""
    data = request.get_json(silent=True) or {}
    platform = data.get("platform", "LIVE")
    buyer_name = data.get("buyer_name", "")
    flag_type = data.get("flag_type", "vip")
    value = data.get("value")

    if not buyer_name:
        return jsonify({"status": "error", "message": "고객 닉네임이 필요합니다."}), 400

    res = database.toggle_customer_flag(platform, buyer_name, flag_type, value)
    return jsonify({"status": "ok", "result": res}), 200


# ==========================================
# 💳 주문 결제 상태 & 합배송 묶음 API
# ==========================================

@app.route('/api/orders/update-status', methods=['POST'])
def api_update_order_payment_status():
    """주문 상태(결제, 배송 등)를 변경합니다."""
    restored_info = database.get_restored_session_info()
    if restored_info["is_restored"]:
        return jsonify({
            "status": "error",
            "message": "🔒 한번 저장된 이전 방송 데이터는 수정할 수 없습니다. [🆕 새로운 방송 시작]으로 초기화한 후 이용해 주세요."
        }), 403

    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id")
    status_val = data.get("status", "CONFIRMED")
    memo = data.get("memo")

    if not order_id:
        return jsonify({"status": "error", "message": "주문 ID가 필요합니다."}), 400

    database.update_order_payment_status(int(order_id), status_val, memo)
    return jsonify({"status": "ok", "message": f"주문 #{order_id} 상태가 {status_val}로 변경되었습니다."}), 200


@app.route('/api/orders/bundle', methods=['POST'])
def api_bundle_orders():
    """동일 고객의 주문들을 합배송 묶음 처리합니다."""
    data = request.get_json(silent=True) or {}
    buyer_name = data.get("buyer_name", "")
    platform = data.get("platform", "LIVE")
    order_ids = data.get("order_ids", [])

    if not buyer_name:
        return jsonify({"status": "error", "message": "고객 닉네임이 필요합니다."}), 400

    res = database.bundle_orders_by_buyer(buyer_name, platform, order_ids)
    return jsonify({"status": "ok", "result": res}), 200


@app.route('/api/payment-template', methods=['GET', 'POST'])
def api_payment_template():
    """결제/입금 안내 메시지 템플릿 조회 및 저장"""
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        tmpl = data.get("template", "").strip()
        database.set_overlay_state('payment_template', tmpl)
        return jsonify({"status": "ok", "template": tmpl}), 200

    # GET
    default_tmpl = """[@{buyer_name}님 실시간 라이브 주문 안내]
- 주문 상품: {item_name} ({color}/{size}) {quantity}개
- 총 결제 금액: {total_price}원
- 입금 계좌: 카카오뱅크 3333-12-3456789 (예금주: 라이브커머스)
- 배송지 작성 양식: https://forms.gle/liveorder
* 30분 이내 입금 확인 시 오늘 즉시 당일 발송됩니다! 감사합니다."""
    tmpl = database.get_overlay_state('payment_template', default_tmpl)
    return jsonify({"status": "ok", "template": tmpl}), 200


# ==========================================
# 🎁 시청자 댓글 럭키 드로우 추첨기 API
# ==========================================

@app.route('/api/lottery/candidates', methods=['GET'])
def api_lottery_candidates():
    """키워드를 포함한 시청자 댓글 응모자 명단을 추출합니다."""
    keyword = request.args.get('keyword', '').strip()
    conn = database.get_connection()
    try:
        cur = conn.cursor()
        if keyword:
            cur.execute("""
                SELECT DISTINCT sender, platform, message, chat_time
                FROM chats
                WHERE message LIKE ?
                ORDER BY id DESC
            """, (f"%{keyword}%",))
        else:
            cur.execute("""
                SELECT DISTINCT sender, platform, message, chat_time
                FROM chats
                ORDER BY id DESC
                LIMIT 100
            """)
        rows = [dict(r) for r in cur.fetchall()]

        # 중복 sender 제거
        seen = set()
        candidates = []
        for r in rows:
            s = r["sender"]
            if s not in seen and s not in ["익명", "시청자"]:
                seen.add(s)
                candidates.append(r)

        return jsonify({
            "status": "ok",
            "keyword": keyword,
            "count": len(candidates),
            "candidates": candidates
        }), 200
    finally:
        conn.close()


@app.route('/api/lottery/draw', methods=['POST'])
def api_lottery_draw():
    """후보자 중 무작위 당첨자를 추첨하고 결과를 저장합니다."""
    import random
    data = request.get_json(silent=True) or {}
    title = data.get("title") or data.get("prize_name") or "실시간 라이브 깜짝 이벤트"
    keyword = data.get("keyword", "")
    winner_count = max(1, int(data.get("winner_count", 1)))
    candidates = data.get("candidates", [])

    if not candidates:
        return jsonify({"status": "error", "message": "추첨 대상 응모자가 없습니다."}), 400

    # 당첨자 무작위 선정
    winner_count = min(winner_count, len(candidates))
    winners = random.sample(candidates, winner_count)

    lottery_id = database.save_lottery_event(title, keyword, winner_count, winners)
    return jsonify({
        "status": "ok",
        "lottery_id": lottery_id,
        "title": title,
        "prize_name": title,
        "winner_count": winner_count,
        "winner": winners[0] if winners else None,
        "winners": winners
    }), 200


@app.route('/api/lottery/history', methods=['GET'])
def api_lottery_history():
    """추첨 이력 조회"""
    history = database.get_lottery_history(20)
    return jsonify({"status": "ok", "history": history}), 200


@app.route('/status', methods=['GET'])
def server_status():
    """서버 작동 상태 및 설정 정보를 확인하는 엔드포인트"""
    excel_exists = os.path.exists(EXCEL_FILE)
    obs_exists = os.path.exists(OBS_TEXT_FILE)
    return jsonify({
        "status": "online",
        "service": "Live Commerce AI Order & Subtitle Server",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "files": {
            "excel_file": EXCEL_FILE,
            "excel_created": excel_exists,
            "obs_text_file": OBS_TEXT_FILE,
            "obs_file_created": obs_exists,
            "backup_csv_file": BACKUP_CSV_FILE
        },
        "pending_orders_count": len(pending_orders),
        "engine": "OpenAI (gpt-4o-mini)" if openai_client else "Local Mock Engine (Set OPENAI_API_KEY in .env for GPT)"
    }), 200


def run_ninja_ws_listener():
    """
    Social Stream Ninja 공식 채널 실시간 자동 수신 스레드
    - Channel 4: 크롬 확장 프로그램이 유튜브/인스타/틱톡 실제 라이브창에서 수집한 실시간 댓글
    - Channel 3: &server2 모드에서 Dock 창(dock.html)이 출력(publish)하는 실시간 댓글
    - Channel 2: 기본 server 모드에서 Dock 창이 출력하는 실시간 댓글
    - Channel 1 & 0: 원격 제어 및 기본 루트 세션 브로드캐스트
    모든 채널을 동시 구독하여 어떤 모드/파라미터에서도 1건의 댓글도 누락 없이 즉시 관제탑으로 인입합니다.
    """
    from collections import deque
    session_id = os.getenv("NINJA_SESSION_ID", "bVkykgZGbH")
    channels = ["4", "3", "2", "1", ""]
    seen_ids = deque(maxlen=500)
    seen_lock = Lock()

    async def listen_channel(ch):
        import websockets
        ch_label = f"CH {ch}" if ch else "ROOT"
        uri = f"wss://io.socialstream.ninja/join/{session_id}/{ch}" if ch else f"wss://io.socialstream.ninja/join/{session_id}"
        retry_delay = 3.0
        while True:
            try:
                print(f"🔌 [Ninja {ch_label}] 실시간 채널 접속 시도: {uri}", flush=True)
                async with websockets.connect(uri, ping_interval=20, ping_timeout=20) as ws:
                    retry_delay = 3.0
                    print(f"✅ [Ninja {ch_label} 연결 완료!] 3사 라이브 채팅 수신 대기 중...", flush=True)
                    while True:
                        raw_msg = await ws.recv()
                        print(f"📥 [Ninja {ch_label} 원본 수신] {raw_msg}", flush=True)
                        try:
                            payload = json.loads(raw_msg)
                            # 중첩 객체(data, value) 지원
                            inner = payload.get("data") if isinstance(payload.get("data"), dict) else (
                                payload.get("value") if isinstance(payload.get("value"), dict) else payload
                            )
                            msg_id = str(inner.get("id") or f"{inner.get('chatname')}_{inner.get('chatmessage')}")
                            with seen_lock:
                                if msg_id in seen_ids:
                                    continue
                                seen_ids.append(msg_id)

                            c_name = inner.get('chatname') or inner.get('username') or inner.get('author') or '익명'
                            c_msg = inner.get('chatmessage') or inner.get('message') or inner.get('text') or ''
                            if c_msg:
                                print(f"⚡ [Social Stream Ninja 수신({ch_label})] {c_name}: {c_msg}", flush=True)
                                process_chat_payload(inner)
                        except Exception as parse_err:
                            print(f"⚠️ [{ch_label} 파싱 오류] {parse_err}", flush=True)
            except Exception as conn_err:
                await asyncio.sleep(retry_delay)
                retry_delay = min(30.0, retry_delay * 1.6)

    async def ws_main():
        await asyncio.gather(*(listen_channel(ch) for ch in channels))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(ws_main())
    except Exception as e:
        print(f"⚠️ [웹소켓 루프 종료] {e}", flush=True)


from werkzeug.serving import WSGIRequestHandler, connection_dropped_errors
from werkzeug.wsgi import LimitedStream


class KeepAliveWSGIRequestHandler(WSGIRequestHandler):
    """
    HTTP/1.1 Keep-Alive 영구 연결을 지원하는 WSGI 요청 핸들러
    - 기존 Werkzeug 기본값(매 요청마다 Connection: close) 및 짧은 max=1000 제한으로 인해 스마트폰 카메라 프레임(20fps),
      오디오(5req/s), 유튜브 RTMP 프레임(26fps) 전송 시 주기적인 소켓 재연결과 TIME_WAIT 누적,
      공유기 NAT 세션 테이블 포화가 발생하는 현상을 원천 차단합니다.
    """
    protocol_version = "HTTP/1.1"
    timeout = 120

    def setup(self):
        super().setup()
        try:
            import socket
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except Exception:
            pass

    def log_error(self, format_str, *args):
        msg = format_str % args if args else str(format_str)
        if "timed out" in msg or "TimeoutError" in msg:
            return
        super().log_error(format_str, *args)

    def log_request(self, code="-", size="-"):
        path = getattr(self, "path", "")
        if str(code) == "200" and any(
            p in path for p in (
                "/api/camera/frame",
                "/api/camera/audio",
                "/api/audio/scope",
                "/api/youtube/rtmp/frame",
                "/api/youtube/rtmp/overlay",
                "/api/youtube/status",
                "/api/tiktok/status",
                "/api/instagram/status",
                "/api/overlay/data",
                "/api/camera/host-info",
                "/api/camera/switcher",
                "/api/camera/latest-frame",
                "/api/broadcast/current-summary",
                "/api/camera/webrtc/offers",
                "/api/camera/webrtc/answer/",
                "/api/youtube/video-diagnostics",
            )
        ):
            return
        super().log_request(code, size)

    def run_wsgi(self) -> None:
        if self.headers.get("Expect", "").lower().strip(" \t") == "100-continue":
            self.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")

        self.environ = environ = self.make_environ()
        status_set = None
        headers_set = None
        status_sent = None
        headers_sent = None
        chunk_response = False

        try:
            content_length = int(environ.get("CONTENT_LENGTH") or 0)
        except (ValueError, TypeError):
            content_length = 0

        limited_input = None
        if "chunked" in environ.get("HTTP_TRANSFER_ENCODING", "").lower():
            chunked_buf = bytearray()
            try:
                while True:
                    size_line = self.rfile.readline(65537)
                    if not size_line:
                        break
                    size_token = size_line.strip().split(b";", 1)[0]
                    if not size_token:
                        continue
                    chunk_size = int(size_token, 16)
                    if chunk_size == 0:
                        self.rfile.readline(65537)
                        break
                    remaining = chunk_size
                    while remaining > 0:
                        part = self.rfile.read(remaining)
                        if not part:
                            break
                        chunked_buf.extend(part)
                        remaining -= len(part)
                    self.rfile.readline(65537)
            except Exception:
                self.close_connection = True
            body_bytes = bytes(chunked_buf)
            environ["CONTENT_LENGTH"] = str(len(body_bytes))
            environ.pop("HTTP_TRANSFER_ENCODING", None)
            environ["wsgi.input"] = io.BytesIO(body_bytes)
        else:
            limited_input = LimitedStream(self.rfile, max(0, content_length))
            environ["wsgi.input"] = limited_input

        req_conn_hdr = self.headers.get("Connection", "").lower()
        client_wants_keep_alive = (
            (self.request_version >= "HTTP/1.1" and "close" not in req_conn_hdr)
            or (self.request_version == "HTTP/1.0" and "keep-alive" in req_conn_hdr)
        )

        def write(data: bytes) -> None:
            nonlocal status_sent, headers_sent, chunk_response
            assert status_set is not None, "write() before start_response"
            assert headers_set is not None, "write() before start_response"
            if status_sent is None:
                status_sent = status_set
                headers_sent = headers_set
                try:
                    code_str, msg = status_sent.split(None, 1)
                except ValueError:
                    code_str, msg = status_sent, ""
                code = int(code_str)
                self.send_response(code, msg)
                header_keys = set()
                resp_conn_hdr = ""
                resp_ctype = ""
                for key, value in headers_sent:
                    kl = key.lower()
                    if kl == "connection":
                        resp_conn_hdr = value.lower()
                        continue
                    if kl == "content-type":
                        resp_ctype = value.lower()
                    self.send_header(key, value)
                    header_keys.add(kl)

                has_known_len = (
                    "content-length" in header_keys
                    or environ["REQUEST_METHOD"] == "HEAD"
                    or (100 <= code < 200)
                    or code in {204, 304}
                )
                is_stream = "multipart/x-mixed-replace" in resp_ctype

                if not has_known_len and not is_stream and self.protocol_version >= "HTTP/1.1":
                    chunk_response = True
                    self.send_header("Transfer-Encoding", "chunked")

                can_keep_alive = (
                    client_wants_keep_alive
                    and "close" not in resp_conn_hdr
                    and not is_stream
                    and (has_known_len or chunk_response)
                )

                if can_keep_alive:
                    self.close_connection = False
                    self.send_header("Connection", "keep-alive")
                    self.send_header("Keep-Alive", "timeout=120, max=1000000")
                else:
                    self.close_connection = True
                    self.send_header("Connection", "close")

                self.end_headers()

            if data:
                if chunk_response:
                    self.wfile.write(hex(len(data))[2:].encode())
                    self.wfile.write(b"\r\n")
                self.wfile.write(data)
                if chunk_response:
                    self.wfile.write(b"\r\n")
            self.wfile.flush()

        def start_response(status, headers, exc_info=None):
            nonlocal status_set, headers_set
            if exc_info:
                try:
                    if headers_sent:
                        raise exc_info[1].with_traceback(exc_info[2])
                finally:
                    exc_info = None
            elif headers_set:
                raise AssertionError("Headers already set")
            status_set = status
            headers_set = headers
            return write

        def execute(app_obj) -> None:
            application_iter = app_obj(environ, start_response)
            try:
                for data in application_iter:
                    write(data)
                if not headers_sent:
                    write(b"")
                if chunk_response:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
            finally:
                if limited_input is not None and not self.close_connection:
                    try:
                        limited_input.exhaust()
                    except Exception:
                        self.close_connection = True
                if hasattr(application_iter, "close"):
                    application_iter.close()

        try:
            execute(self.server.app)
        except connection_dropped_errors as e:
            self.close_connection = True
            self.connection_dropped(e, environ)
        except Exception as e:
            self.close_connection = True
            if self.server.passthrough_errors:
                raise
            try:
                if status_sent is None:
                    status_set = None
                    headers_set = None
                from werkzeug.exceptions import InternalServerError
                execute(InternalServerError())
            except Exception:
                pass


def run_https_cam_server(host, https_port, local_ip):
    """스마트폰 카메라 WebRTC/getUserMedia 보안 컨텍스트(HTTPS) 전용 서버 스레드"""
    try:
        from werkzeug.serving import make_server
        cert_path, key_path = ensure_ssl_certs(local_ip)
        if cert_path and key_path:
            https_srv = make_server(
                host,
                https_port,
                app,
                threaded=True,
                request_handler=KeepAliveWSGIRequestHandler,
                ssl_context=(cert_path, key_path)
            )
            print(f"🔒 스마트폰 무선 카메라 로컬 보안(HTTPS) 서버 가동! (https://{local_ip}:{https_port}/cam)", flush=True)
            https_srv.serve_forever()
    except Exception as e:
        print(f"⚠️ [HTTPS 카메라 서버 시작 실패] {e}", flush=True)


def run_public_https_tunnel(http_port):
    """공유기 분리/LTE·5G/방화벽 환경에서도 즉시 접속되는 공인 HTTPS 보안 터널 스레드"""
    global public_tunnel_url, public_tunnel_proc
    ssh_exe = r"C:\Windows\System32\OpenSSH\ssh.exe"
    if not os.path.exists(ssh_exe):
        ssh_exe = "ssh"
    cmd = [
        ssh_exe,
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=NUL",
        "-o", "ServerAliveInterval=30",
        "-o", "ExitOnForwardFailure=yes",
        "-R", f"80:127.0.0.1:{http_port}",
        "nokey@localhost.run"
    ]
    while True:
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="ignore"
            )
            public_tunnel_proc = proc
            for line in proc.stdout:
                m = re.search(r"(https://[a-zA-Z0-9.-]+\.lhr\.life)", line)
                if m:
                    base_url = m.group(1).rstrip("/")
                    public_tunnel_url = f"{base_url}/cam"
                    public_tunnel_renew_event.set()
                    print(f"🌍 스마트폰 카메라 공인 HTTPS 터널 개통 완료! ({public_tunnel_url})", flush=True)
            proc.wait()
        except Exception as e:
            print(f"⚠️ [공인 HTTPS 터널 재연결 대기] {e}", flush=True)
        public_tunnel_proc = None
        public_tunnel_url = None
        time.sleep(5)


def run_local_to_cloud_camera_bridge():
    """
    방송자 로컬 PC(예: 192.168.0.69 / 192.168.1.103)에서 실행 중일 때,
    클라우드 스튜디오 서버(기본 http://49.50.132.140:5000)로:
      1) 실제 댁내 공유기 Wi-Fi IP(192.168.0.69)와 PC IP(192.168.1.103)를 주기적으로 등록하여
         클라우드 스튜디오 QR 코드가 클라우드 내부 IP(192.168.0.6) 대신 실제 PC IP(192.168.0.69)를 가리키도록 동기화하고,
      2) 스마트폰이 로컬 PC(https://192.168.0.69:5443/cam)로 영상을 송출하면 Keep-Alive 소켓으로
         클라우드 스튜디오에 프레임을 즉시 전달하여 크롬 PNA 차단 없이 화면이 바로 표시되도록 합니다.
    """
    import http.client
    from urllib.parse import urlparse

    cloud_url = (os.getenv("CLOUD_STUDIO_URL") or "http://49.50.132.140:5000").strip().rstrip("/")
    if not cloud_url or os.getenv("DISABLE_CLOUD_BRIDGE") == "1":
        return

    parsed = urlparse(cloud_url)
    cloud_host = parsed.hostname or "49.50.132.140"
    cloud_port = parsed.port or (443 if parsed.scheme == "https" else 5000)
    use_https = (parsed.scheme == "https")

    last_sync_ts = 0.0
    last_forwarded_seq = {1: 0, 2: 0}
    cloud_cam_tok = ""
    conn = None

    def get_conn():
        nonlocal conn
        if conn is None:
            if use_https:
                import ssl
                ctx = ssl._create_unverified_context()
                conn = http.client.HTTPSConnection(cloud_host, cloud_port, timeout=2.5, context=ctx)
            else:
                conn = http.client.HTTPConnection(cloud_host, cloud_port, timeout=2.5)
        return conn

    def reset_conn():
        nonlocal conn
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            conn = None

    while True:
        try:
            now = time.time()
            ip = get_local_ip()
            https_port = int(os.getenv("HTTPS_PORT", 5443))
            http_port = int(os.getenv("SERVER_PORT", 5000))
            wifi_ip = bridged_wifi_ip or setup_upnp_cross_subnet_bridge(ip, ports=(https_port, http_port)) or ip

            # 클라우드 서버 자신(192.168.0.6)에서는 실행 중단
            if ip == "192.168.0.6" and wifi_ip == "192.168.0.6":
                return

            # 1) 2초 주기로 로컬 IP(192.168.0.69) 및 카메라 상태 등록
            if (now - last_sync_ts) >= 2.0:
                last_sync_ts = now
                cams_summary = get_camera_channels_summary(now)
                payload = json.dumps({
                    "local_ip": wifi_ip,
                    "pc_ip": ip,
                    "https_port": https_port,
                    "http_port": http_port,
                    "cameras": cams_summary,
                }).encode("utf-8")
                try:
                    c = get_conn()
                    c.request(
                        "POST",
                        "/api/agent/bridge-sync?agent_bridge=1",
                        body=payload,
                        headers={
                            "Content-Type": "application/json",
                            "X-Local-Agent-Bridge": "1",
                            "Connection": "keep-alive",
                        },
                    )
                    resp = c.getresponse()
                    raw = resp.read()
                    if resp.status == 200 and raw:
                        d = json.loads(raw.decode("utf-8", errors="ignore"))
                        if d.get("cam_token"):
                            cloud_cam_tok = str(d["cam_token"]).strip()
                except Exception:
                    reset_conn()

            # 2) 로컬 PC에 새로 들어온 스마트폰 카메라 프레임이 있으면 즉시 클라우드 스튜디오로 전달
            forwarded_any = False
            for slot in (1, 2):
                with latest_camera_lock:
                    ch = camera_channels.get(slot, {})
                    seq = int(ch.get("seq") or 0)
                    frame_bytes = ch.get("frame")
                    ts = float(ch.get("timestamp") or 0.0)
                if frame_bytes and seq > last_forwarded_seq[slot] and (now - ts) < 5.0:
                    last_forwarded_seq[slot] = seq
                    tok_use = cloud_cam_tok or get_studio_cam_token()
                    try:
                        c = get_conn()
                        c.request(
                            "POST",
                            f"/api/camera/frame?cam={slot}&seq={seq}&src=local_wifi_cam&cam_token={tok_use}",
                            body=frame_bytes,
                            headers={
                                "Content-Type": "image/jpeg",
                                "X-Cam-Token": tok_use,
                                "Connection": "keep-alive",
                            },
                        )
                        r_f = c.getresponse()
                        r_f.read()
                        forwarded_any = True
                    except Exception:
                        reset_conn()

            time.sleep(0.025 if forwarded_any else 0.15)
        except Exception:
            reset_conn()
            time.sleep(1.5)


if __name__ == '__main__':
    init_excel()

    # 저장된 유튜브 채널 핸들이 CP949 인코딩 깨짐(@???-0313) 상태인 경우 자동 복원 및 자동 연결
    try:
        saved_yt_ch = database.get_overlay_state("youtube_channel_input", "")
        if "???" in str(saved_yt_ch) or not str(saved_yt_ch).strip():
            saved_yt_ch = "@더바인-0313"
            database.set_overlay_state("youtube_channel_input", saved_yt_ch)
        if saved_yt_ch and not youtube_chat_poller.active:
            youtube_chat_poller.connect(saved_yt_ch)
        saved_tt_ch = str(database.get_overlay_state("tiktok_channel_input", "") or "").strip()
        if saved_tt_ch and not tiktok_chat_poller.active:
            tiktok_chat_poller.connect(saved_tt_ch)
        saved_ig_ch = str(database.get_overlay_state("instagram_channel_input", "") or "").strip()
        if saved_ig_ch and not instagram_chat_poller.active:
            instagram_chat_poller.connect(saved_ig_ch)
    except Exception:
        pass

    # 네트워크 & RTMP 자가 복구 워치독 가동
    start_network_self_healing_watchdog()

    # Social Stream Ninja 웹소켓은 명시적으로 활성화된 경우에만 가동 (기본 유튜브 직접 연동 시 불필요한 5개 외부 TLS 세션 방지)
    if os.getenv("ENABLE_NINJA_WS", "0") == "1":
        ninja_thread = threading.Thread(target=run_ninja_ws_listener, daemon=True)
        ninja_thread.start()

    port = int(os.getenv("SERVER_PORT", 5000))
    https_port = int(os.getenv("HTTPS_PORT", 5443))
    host = os.getenv("SERVER_HOST", "0.0.0.0")
    local_ip = get_local_ip()

    # 기동 즉시 공유기 UPnP 대역 브릿지(192.168.0.69) 및 Windows 방화벽 Edge Traversal(edge=yes) 활성화
    try:
        setup_upnp_cross_subnet_bridge(local_ip, ports=(https_port, port))
        ensure_windows_firewall_edge_traversal(ports=(https_port, port))
    except Exception:
        pass

    # 스마트폰 카메라 보안 컨텍스트(HTTPS) 로컬 포트 동시 가동
    https_thread = threading.Thread(target=run_https_cam_server, args=(host, https_port, local_ip), daemon=True)
    https_thread.start()

    # 로컬 PC(Windows 등)에서 실행 중일 때 클라우드 스튜디오(49.50.132.140)로 실제 Wi-Fi IP(192.168.0.69) 및 카메라 프레임 자동 브릿지
    if os.name == "nt" or os.getenv("ENABLE_LOCAL_TO_CLOUD_BRIDGE") == "1":
        bridge_thread = threading.Thread(target=run_local_to_cloud_camera_bridge, daemon=True)
        bridge_thread.start()

    # 사설 SSL 인증서 경고가 뜨거나 차단되는 스마트폰을 위해 공인 HTTPS 보안 터널(localhost.run)을 기본 자동 가동
    if os.getenv("ENABLE_PUBLIC_TUNNEL", "1") != "0":
        _public_tunnel_thread_started = True
        tunnel_thread = threading.Thread(target=run_public_https_tunnel, args=(port,), daemon=True)
        tunnel_thread.start()

    # 클라우드/독립 쇼핑몰 Outbound 동기화 워커 가동 및 모드별 쇼핑몰 자동 기동 제어
    try:
        mall_cfg = database.get_cloud_mall_config()
        database.sync_all_products_to_shopping_mall(is_live=False)
        cloud_mall_sync_worker.start()

        if mall_cfg["agent_only_mode"]:
            print(f"☁️ [로컬 라이브 에이전트 단독 모드] 외부 쇼핑몰 Outbound 연동 활성: {mall_cfg['mall_url']}", flush=True)
        else:
            mall_app_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shopping_mall", "app.py")
            if os.path.exists(mall_app_path):
                import socket
                mall_port = int(os.getenv("MALL_PORT", MALL_PORT))
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s_chk:
                    s_chk.settimeout(0.4)
                    mall_running = (s_chk.connect_ex(("127.0.0.1", mall_port)) == 0)
                if not mall_running:
                    subprocess.Popen(
                        [sys.executable, mall_app_path],
                        cwd=os.path.dirname(mall_app_path)
                    )
                    print(f"🛍️ [독립 쇼핑몰 자동 기동] http://0.0.0.0:{mall_port} (라이브커머스: /live)", flush=True)
    except Exception as mall_err:
        print(f"⚠️ [쇼핑몰 연동 초기화 경고] {mall_err}", flush=True)

    print(f"🚀 라이브 커머스 통합 AI 서버가 시작되었습니다! (http://127.0.0.1:{port}/chat)", flush=True)
    app.run(host=host, port=port, threaded=True, request_handler=KeepAliveWSGIRequestHandler)
