# -*- coding: utf-8 -*-
"""
공통 보안 코어 모듈 (security_core.py)
- 비밀번호 해싱(scrypt) 및 복잡도 정책 검증
- 세션 고정(Session Fixation) 방지 및 영속 시크릿 키 관리
- CSRF 토큰 발급/검증 및 프론트엔드 fetch/form 자동 주입 스크립트
- HMAC-SHA256 서명 토큰(카메라 페어링, OBS 오버레이, 라이브 비회원 주문 위조 방지)
- 슬라이딩 윈도우 Rate Limiter (브루트포스 방어)
- Open Redirect 방지 및 보안 HTTP 응답 헤더 일괄 적용
"""

import os
import re
import json
import time
import hmac
import base64
import hashlib
import secrets
from threading import RLock
from urllib.parse import urlparse
from werkzeug.security import generate_password_hash, check_password_hash


def load_or_create_secret_key(env_var_name: str, key_file_path: str) -> str:
    """환경변수 또는 로컬 키 파일에서 256-bit 시크릿 키를 로드하거나 최초 1회 생성합니다."""
    env_val = (os.getenv(env_var_name) or "").strip()
    if len(env_val) >= 24:
        return env_val
    try:
        if os.path.exists(key_file_path):
            with open(key_file_path, "r", encoding="utf-8") as f:
                saved = f.read().strip()
                if len(saved) >= 32:
                    return saved
        new_key = secrets.token_hex(32)
        with open(key_file_path, "w", encoding="utf-8") as f:
            f.write(new_key)
        return new_key
    except Exception:
        return secrets.token_hex(32)


def hash_password(password: str) -> str:
    """단방향 솔트 해시(scrypt)를 생성합니다."""
    return generate_password_hash(str(password or ""), method="scrypt")


def verify_password(password_hash: str, password: str) -> bool:
    """저장된 해시와 평문 비밀번호를 상수 시간에 가깝게 검증합니다."""
    if not password_hash or not password:
        return False
    try:
        return check_password_hash(str(password_hash), str(password))
    except Exception:
        return False


def validate_password_policy(password: str, min_len: int = 8) -> tuple:
    """회원 비밀번호 복잡도 검증 (최소 8자, 영문 + 숫자 필수)."""
    pw = str(password or "")
    if len(pw) < min_len:
        return False, f"비밀번호는 최소 {min_len}자 이상이어야 합니다."
    if len(pw) > 128:
        return False, "비밀번호가 너무 깁니다 (최대 128자)."
    has_letter = bool(re.search(r"[A-Za-z가-힣]", pw))
    has_digit = bool(re.search(r"[0-9]", pw))
    if not (has_letter and has_digit):
        return False, "비밀번호는 영문(또는 문자)과 숫자를 모두 포함해야 합니다."
    return True, "OK"


def validate_guest_password_policy(password: str, min_len: int = 4) -> tuple:
    """비회원 주문 조회용 비밀번호 검증 (최소 4자 이상)."""
    pw = str(password or "").strip()
    if len(pw) < min_len:
        return False, f"비회원 주문 조회 비밀번호는 최소 {min_len}자 이상 입력해 주세요."
    if len(pw) > 64:
        return False, "비밀번호는 64자 이하로 입력해 주세요."
    return True, "OK"


def constant_time_equals(val_a: str, val_b: str) -> bool:
    """타이밍 공격 방지를 위한 상수 시간 문자열 비교."""
    if not val_a or not val_b:
        return False
    try:
        return hmac.compare_digest(
            str(val_a).encode("utf-8"),
            str(val_b).encode("utf-8")
        )
    except Exception:
        return False


def is_safe_redirect_url(target: str) -> bool:
    """Open Redirect 취약점 방지를 위해 내부 상대 경로인지 검증합니다."""
    if not target or not isinstance(target, str):
        return False
    target = target.strip()
    if not target.startswith("/") or target.startswith("//") or target.startswith("/\\"):
        return False
    parsed = urlparse(target)
    return not parsed.scheme and not parsed.netloc


def get_csrf_token(session_obj) -> str:
    """현재 세션에 바인딩된 CSRF 토큰을 반환합니다 (없으면 생성)."""
    tok = session_obj.get("_csrf_token")
    if not tok or not isinstance(tok, str) or len(tok) < 32:
        tok = secrets.token_hex(32)
        session_obj["_csrf_token"] = tok
    return tok


def rotate_session(session_obj, preserve_keys=None):
    """로그인 성공 시 Session Fixation 방지를 위해 세션을 초기화하고 새 CSRF 토큰을 발급합니다."""
    preserved = {}
    if preserve_keys:
        for k in preserve_keys:
            if k in session_obj:
                preserved[k] = session_obj[k]
    session_obj.clear()
    for k, v in preserved.items():
        session_obj[k] = v
    session_obj["_csrf_token"] = secrets.token_hex(32)


def verify_csrf_token(session_obj, req_obj) -> bool:
    """요청 헤더(X-CSRF-Token), 폼(csrf_token), 또는 JSON(_csrf_token)의 CSRF 토큰을 검증합니다."""
    expected = session_obj.get("_csrf_token")
    if not expected:
        return False

    candidate = (
        req_obj.headers.get("X-CSRF-Token")
        or req_obj.headers.get("X-CSRFToken")
        or req_obj.form.get("csrf_token")
        or ""
    ).strip()

    if not candidate and req_obj.is_json:
        data = req_obj.get_json(silent=True)
        if isinstance(data, dict):
            candidate = str(data.get("_csrf_token") or data.get("csrf_token") or "").strip()

    return constant_time_equals(expected, candidate)


def build_csrf_auto_inject_snippet(csrf_token: str) -> str:
    """
    HTML 응답의 </head> 또는 </body> 직전에 자동 삽입되어
    모든 동일 출처 fetch / XMLHttpRequest / POST <form>에 CSRF 토큰을 자동 주입합니다.
    """
    safe_tok = str(csrf_token or "").replace('"', "").replace("'", "")
    return f"""<meta name="csrf-token" content="{safe_tok}">
<script id="__ag_csrf_bootstrap__">
(function() {{
    const TOKEN = "{safe_tok}";
    window.__CSRF_TOKEN__ = TOKEN;
    function isSameOrigin(urlStr) {{
        if (!urlStr) return true;
        const s = String(urlStr);
        if (s.startsWith('/') && !s.startsWith('//')) return true;
        try {{
            const u = new URL(s, window.location.origin);
            return u.origin === window.location.origin;
        }} catch (e) {{
            return false;
        }}
    }}
    if (window.fetch && !window.fetch.__csrfWrapped) {{
        const origFetch = window.fetch.bind(window);
        const wrappedFetch = function(input, init) {{
            try {{
                const url = (typeof input === 'string') ? input : (input && input.url ? input.url : '');
                const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
                if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(method) && isSameOrigin(url)) {{
                    init = Object.assign({{}}, init || {{}});
                    if (init.headers instanceof Headers) {{
                        if (!init.headers.has('X-CSRF-Token')) init.headers.set('X-CSRF-Token', TOKEN);
                    }} else if (Array.isArray(init.headers)) {{
                        init.headers = init.headers.concat([['X-CSRF-Token', TOKEN]]);
                    }} else {{
                        init.headers = Object.assign({{'X-CSRF-Token': TOKEN}}, init.headers || {{}});
                    }}
                }}
            }} catch (e) {{}}
            return origFetch(input, init);
        }};
        wrappedFetch.__csrfWrapped = true;
        window.fetch = wrappedFetch;
    }}
    if (window.XMLHttpRequest && !window.XMLHttpRequest.prototype.__csrfWrapped) {{
        const origOpen = XMLHttpRequest.prototype.open;
        const origSend = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function(method, url) {{
            this.__csrfMethod = String(method || 'GET').toUpperCase();
            this.__csrfUrl = url;
            return origOpen.apply(this, arguments);
        }};
        XMLHttpRequest.prototype.send = function() {{
            try {{
                if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(this.__csrfMethod) && isSameOrigin(this.__csrfUrl)) {{
                    this.setRequestHeader('X-CSRF-Token', TOKEN);
                }}
            }} catch (e) {{}}
            return origSend.apply(this, arguments);
        }};
        XMLHttpRequest.prototype.__csrfWrapped = true;
    }}
    function injectForms() {{
        document.querySelectorAll('form').forEach(function(form) {{
            const m = String(form.getAttribute('method') || 'GET').toUpperCase();
            if (m === 'POST' && !form.querySelector('input[name="csrf_token"]')) {{
                const inp = document.createElement('input');
                inp.type = 'hidden';
                inp.name = 'csrf_token';
                inp.value = TOKEN;
                form.appendChild(inp);
            }}
        }});
    }}
    if (document.readyState === 'loading') {{
        document.addEventListener('DOMContentLoaded', injectForms);
    }} else {{
        injectForms();
    }}
    document.addEventListener('submit', function(e) {{
        const form = e.target;
        if (form && form.tagName === 'FORM' && String(form.getAttribute('method') || 'GET').toUpperCase() === 'POST') {{
            if (!form.querySelector('input[name="csrf_token"]')) {{
                const inp = document.createElement('input');
                inp.type = 'hidden';
                inp.name = 'csrf_token';
                inp.value = TOKEN;
                form.appendChild(inp);
            }}
        }}
    }}, true);
}})();
</script>"""


def inject_csrf_into_html_response(response, csrf_token: str):
    """HTML 응답 본문에 CSRF 메타태그와 fetch/form 자동 주입 스크립트를 삽입합니다."""
    try:
        ctype = response.headers.get("Content-Type", "")
        if "text/html" not in ctype.lower():
            return response
        if getattr(response, "direct_passthrough", False):
            return response
        body = response.get_data(as_text=True)
        if "__ag_csrf_bootstrap__" in body:
            return response
        snippet = build_csrf_auto_inject_snippet(csrf_token)
        if "</head>" in body:
            body = body.replace("</head>", snippet + "\n</head>", 1)
        elif "</body>" in body:
            body = body.replace("</body>", snippet + "\n</body>", 1)
        else:
            body = snippet + "\n" + body
        response.set_data(body)
    except Exception:
        pass
    return response


def create_signed_token(secret_key: str, purpose: str, payload: dict = None, ttl_sec: int = 86400) -> str:
    """만료시간(exp)과 용도(purpose)가 서명된 URL-safe HMAC-SHA256 토큰을 생성합니다."""
    now = int(time.time())
    body = {
        "p": str(purpose),
        "iat": now,
        "exp": now + int(ttl_sec),
        "d": payload or {}
    }
    raw_json = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    b64_body = base64.urlsafe_b64encode(raw_json).decode("ascii").rstrip("=")
    sig = hmac.new(
        str(secret_key).encode("utf-8"),
        b64_body.encode("ascii"),
        hashlib.sha256
    ).hexdigest()
    return f"{b64_body}.{sig}"


def verify_signed_token(secret_key: str, purpose: str, token: str):
    """HMAC-SHA256 서명 토큰의 무결성·만료시간·용도를 검증하고 payload dict를 반환합니다."""
    if not token or not isinstance(token, str) or "." not in token:
        return None
    try:
        b64_body, sig = token.rsplit(".", 1)
        expected_sig = hmac.new(
            str(secret_key).encode("utf-8"),
            b64_body.encode("ascii"),
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected_sig, sig):
            return None
        pad = "=" * ((4 - len(b64_body) % 4) % 4)
        raw_json = base64.urlsafe_b64decode((b64_body + pad).encode("ascii"))
        body = json.loads(raw_json.decode("utf-8"))
        if body.get("p") != str(purpose):
            return None
        if int(body.get("exp", 0)) < int(time.time()):
            return None
        return body.get("d") if isinstance(body.get("d"), dict) else {}
    except Exception:
        return None


class SlidingWindowRateLimiter:
    """IP 및 엔드포인트 버킷 기반 인메모리 슬라이딩 윈도우 Rate Limiter."""

    def __init__(self):
        self._lock = RLock()
        self._hits = {}  # (bucket, key) -> list[float]
        self._last_cleanup = time.time()

    def check(self, bucket: str, key: str, max_requests: int, window_sec: float) -> tuple:
        now = time.time()
        bkey = (str(bucket), str(key or "anon"))
        with self._lock:
            if now - self._last_cleanup > 120.0:
                self._cleanup(now)
                self._last_cleanup = now
            arr = self._hits.get(bkey, [])
            cutoff = now - float(window_sec)
            arr = [ts for ts in arr if ts > cutoff]
            if len(arr) >= int(max_requests):
                retry_after = max(1, int(round(window_sec - (now - arr[0]))))
                self._hits[bkey] = arr
                return False, retry_after
            arr.append(now)
            self._hits[bkey] = arr
            return True, 0

    def reset(self, bucket: str, key: str):
        with self._lock:
            self._hits.pop((str(bucket), str(key or "anon")), None)

    def _cleanup(self, now: float):
        expired_keys = []
        for k, arr in self._hits.items():
            if not arr or (now - arr[-1]) > 600.0:
                expired_keys.append(k)
        for k in expired_keys:
            self._hits.pop(k, None)


rate_limiter = SlidingWindowRateLimiter()


def apply_security_headers(response, allow_framing: bool = False):
    """공통 보안 HTTP 응답 헤더를 적용합니다."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-XSS-Protection", "1; mode=block")
    if not allow_framing:
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return response


class ServerTrafficMonitor:
    """
    서버로 들어오는 모든 HTTP/HTTPS 요청의 수신(Inbound)·송신(Outbound) 바이트,
    초당 요청 수(RPS), 60초 시계열 추이, 기능 카테고리별·엔드포인트별·접속 IP별 트래픽을 실시간 계측합니다.
    """

    CATEGORY_META = {
        "CAMERA": {"label": "📹 카메라 / 영상 (Camera & Video)", "color": "#0ea5e9"},
        "AUDIO": {"label": "🎤 오디오 / 음성 (Audio Stream)", "color": "#8b5cf6"},
        "RTMP": {"label": "📡 방송 송출 / RTMP (YouTube·TikTok·IG)", "color": "#ef4444"},
        "MALL_SYNC": {"label": "🛍️ 쇼핑몰 / 주문·상품 동기화", "color": "#10b981"},
        "STUDIO": {"label": "🖥️ 스튜디오 / 오버레이 관제", "color": "#2563eb"},
        "CHAT": {"label": "💬 실시간 채팅 / AI 분석", "color": "#f59e0b"},
        "ADMIN_AUTH": {"label": "🛡️ 관리자 / 인증 / 정적 리소스", "color": "#64748b"},
    }

    def __init__(self):
        from collections import deque
        self._lock = RLock()
        self._deque_cls = deque
        self.reset()

    def reset(self):
        with self._lock:
            self.started_at = time.time()
            self.total_requests = 0
            self.total_rx_bytes = 0
            self.total_tx_bytes = 0
            self.peak_rx_bps = 0.0
            self.peak_tx_bps = 0.0
            self.peak_rps = 0.0
            self.status_counts = {"2xx": 0, "304": 0, "4xx": 0, "5xx": 0}
            self.categories = {
                k: {
                    "key": k,
                    "label": meta["label"],
                    "color": meta["color"],
                    "requests": 0,
                    "rx_bytes": 0,
                    "tx_bytes": 0,
                }
                for k, meta in self.CATEGORY_META.items()
            }
            self.sec_buckets = {}  # int_sec -> dict
            self.endpoints = {}    # path -> dict
            self.clients = {}      # ip -> dict
            self.recent_requests = self._deque_cls(maxlen=40)

    @staticmethod
    def classify_path(path: str) -> str:
        p = str(path or "/")
        if p.startswith("/api/camera/audio") or p.startswith("/api/audio/"):
            return "AUDIO"
        if (
            p == "/cam"
            or p.startswith("/cam/")
            or p == "/remote-cam"
            or p.startswith("/remote-cam/")
            or p.startswith("/api/camera/")
            or p.startswith("/api/remote-cam/")
        ):
            return "CAMERA"
        if p.startswith("/api/youtube/") or p.startswith("/api/tiktok/") or p.startswith("/api/instagram/"):
            return "RTMP"
        if (
            p.startswith("/api/sync/")
            or p.startswith("/api/mall/")
            or p.startswith("/api/products")
            or p.startswith("/api/orders")
            or p.startswith("/api/inventory")
        ):
            return "MALL_SYNC"
        if (
            p.startswith("/overlay/")
            or p.startswith("/api/overlay/")
            or p.startswith("/api/host/")
            or p in ("/portal", "/broadcast", "/host")
        ):
            return "STUDIO"
        if p in ("/chat", "/webhook") or p.startswith("/api/chat") or p.startswith("/api/dashboard-summary"):
            return "CHAT"
        return "ADMIN_AUTH"

    def record_request(
        self,
        method: str,
        path: str,
        status_code: int,
        rx_bytes: int,
        tx_bytes: int,
        latency_ms: float,
        client_ip: str,
        user_agent: str = "",
    ):
        now = time.time()
        sec_key = int(now)
        norm_path = str(path or "/").split("?")[0] or "/"
        # 트래픽 관제 폴링 자체(/admin/api/traffic)는 관제 왜곡 방지를 위해 최근 로그에서 제외
        is_self_poll = norm_path.startswith("/admin/api/traffic")
        cat = self.classify_path(norm_path)
        rx = max(0, int(rx_bytes or 0))
        tx = max(0, int(tx_bytes or 0))
        code = int(status_code or 200)

        with self._lock:
            self.total_requests += 1
            self.total_rx_bytes += rx
            self.total_tx_bytes += tx

            if code == 304:
                self.status_counts["304"] += 1
            elif 200 <= code < 400:
                self.status_counts["2xx"] += 1
            elif 400 <= code < 500:
                self.status_counts["4xx"] += 1
            else:
                self.status_counts["5xx"] += 1

            cat_entry = self.categories.get(cat) or self.categories["ADMIN_AUTH"]
            cat_entry["requests"] += 1
            cat_entry["rx_bytes"] += rx
            cat_entry["tx_bytes"] += tx

            b = self.sec_buckets.get(sec_key)
            if b is None:
                b = {"rx": 0, "tx": 0, "reqs": 0, "cam_bytes": 0, "cats": {}}
                self.sec_buckets[sec_key] = b
                if len(self.sec_buckets) > 120:
                    cutoff = sec_key - 90
                    for old_k in [k for k in self.sec_buckets if k < cutoff]:
                        self.sec_buckets.pop(old_k, None)
            b["rx"] += rx
            b["tx"] += tx
            b["reqs"] += 1
            if cat == "CAMERA":
                b["cam_bytes"] += (rx + tx)
            b["cats"][cat] = b["cats"].get(cat, 0) + rx + tx

            ep_key = f"{method.upper()} {norm_path}"
            ep = self.endpoints.get(ep_key)
            if ep is None:
                if len(self.endpoints) > 200:
                    oldest_ep = min(self.endpoints.items(), key=lambda x: x[1]["last_seen"])[0]
                    self.endpoints.pop(oldest_ep, None)
                ep = {
                    "key": ep_key,
                    "method": method.upper(),
                    "path": norm_path,
                    "category": cat,
                    "requests": 0,
                    "rx_bytes": 0,
                    "tx_bytes": 0,
                    "total_ms": 0.0,
                    "last_status": code,
                    "last_seen": now,
                }
                self.endpoints[ep_key] = ep
            ep["requests"] += 1
            ep["rx_bytes"] += rx
            ep["tx_bytes"] += tx
            ep["total_ms"] += float(latency_ms or 0.0)
            ep["last_status"] = code
            ep["last_seen"] = now

            ip_str = str(client_ip or "unknown").strip() or "unknown"
            cl = self.clients.get(ip_str)
            if cl is None:
                if len(self.clients) > 200:
                    oldest_ip = min(self.clients.items(), key=lambda x: x[1]["last_seen"])[0]
                    self.clients.pop(oldest_ip, None)
                cl = {
                    "ip": ip_str,
                    "requests": 0,
                    "rx_bytes": 0,
                    "tx_bytes": 0,
                    "last_path": norm_path,
                    "last_seen": now,
                    "user_agent": str(user_agent or "")[:80],
                }
                self.clients[ip_str] = cl
            cl["requests"] += 1
            cl["rx_bytes"] += rx
            cl["tx_bytes"] += tx
            cl["last_path"] = norm_path
            cl["last_seen"] = now
            if user_agent:
                cl["user_agent"] = str(user_agent)[:80]

            if not is_self_poll:
                self.recent_requests.appendleft({
                    "time": time.strftime("%H:%M:%S", time.localtime(now)),
                    "method": method.upper(),
                    "path": norm_path,
                    "category": cat,
                    "status": code,
                    "rx_bytes": rx,
                    "tx_bytes": tx,
                    "total_bytes": rx + tx,
                    "latency_ms": round(float(latency_ms or 0.0), 1),
                    "ip": ip_str,
                })

    def get_snapshot(self) -> dict:
        now = time.time()
        cur_sec = int(now)
        with self._lock:
            uptime_sec = max(1.0, now - self.started_at)
            # 최근 5초 평균 실시간 속도 (현재 진행 중인 초 버킷을 제외한 직전 5초 또는 포함 5초)
            window_secs = [cur_sec - i for i in range(5)]
            win_rx = sum(self.sec_buckets.get(s, {}).get("rx", 0) for s in window_secs)
            win_tx = sum(self.sec_buckets.get(s, {}).get("tx", 0) for s in window_secs)
            win_reqs = sum(self.sec_buckets.get(s, {}).get("reqs", 0) for s in window_secs)
            win_cam = sum(self.sec_buckets.get(s, {}).get("cam_bytes", 0) for s in window_secs)

            cur_rx_bps = round(win_rx / 5.0, 1)
            cur_tx_bps = round(win_tx / 5.0, 1)
            cur_rps = round(win_reqs / 5.0, 2)
            cur_cam_bps = round(win_cam / 5.0, 1)

            if cur_rx_bps > self.peak_rx_bps:
                self.peak_rx_bps = cur_rx_bps
            if cur_tx_bps > self.peak_tx_bps:
                self.peak_tx_bps = cur_tx_bps
            if cur_rps > self.peak_rps:
                self.peak_rps = cur_rps

            timeline_60s = []
            for s in range(cur_sec - 59, cur_sec + 1):
                b = self.sec_buckets.get(s, {})
                rx_v = int(b.get("rx", 0))
                tx_v = int(b.get("tx", 0))
                rq_v = int(b.get("reqs", 0))
                timeline_60s.append({
                    "sec": s,
                    "label": time.strftime("%H:%M:%S", time.localtime(s)),
                    "rx_bytes": rx_v,
                    "tx_bytes": tx_v,
                    "total_bytes": rx_v + tx_v,
                    "reqs": rq_v,
                })

            total_bytes_all = max(1, self.total_rx_bytes + self.total_tx_bytes)
            categories_list = []
            for k in self.CATEGORY_META:
                c = self.categories[k]
                c_tot = c["rx_bytes"] + c["tx_bytes"]
                c_win = sum(self.sec_buckets.get(s, {}).get("cats", {}).get(k, 0) for s in window_secs)
                categories_list.append({
                    "key": k,
                    "label": c["label"],
                    "color": c["color"],
                    "requests": c["requests"],
                    "rx_bytes": c["rx_bytes"],
                    "tx_bytes": c["tx_bytes"],
                    "total_bytes": c_tot,
                    "current_bps": round(c_win / 5.0, 1),
                    "share_pct": round((c_tot / total_bytes_all) * 100.0, 1) if (self.total_rx_bytes + self.total_tx_bytes) > 0 else 0.0,
                })

            top_endpoints = sorted(
                [
                    {
                        "method": ep["method"],
                        "path": ep["path"],
                        "category": ep["category"],
                        "requests": ep["requests"],
                        "rx_bytes": ep["rx_bytes"],
                        "tx_bytes": ep["tx_bytes"],
                        "total_bytes": ep["rx_bytes"] + ep["tx_bytes"],
                        "avg_ms": round(ep["total_ms"] / max(1, ep["requests"]), 1),
                        "last_status": ep["last_status"],
                        "last_seen_ago": max(0, int(now - ep["last_seen"])),
                    }
                    for ep in self.endpoints.values()
                ],
                key=lambda x: (x["total_bytes"], x["requests"]),
                reverse=True,
            )[:12]

            active_ip_count_5m = sum(1 for c in self.clients.values() if (now - c["last_seen"]) <= 300.0)
            top_clients = sorted(
                [
                    {
                        "ip": cl["ip"],
                        "requests": cl["requests"],
                        "rx_bytes": cl["rx_bytes"],
                        "tx_bytes": cl["tx_bytes"],
                        "total_bytes": cl["rx_bytes"] + cl["tx_bytes"],
                        "last_path": cl["last_path"],
                        "last_seen_ago": max(0, int(now - cl["last_seen"])),
                        "is_active": (now - cl["last_seen"]) <= 60.0,
                        "user_agent": cl["user_agent"],
                    }
                    for cl in self.clients.values()
                ],
                key=lambda x: (x["total_bytes"], x["requests"]),
                reverse=True,
            )[:12]

            cam_cat = self.categories["CAMERA"]
            cam_total_bytes = cam_cat["rx_bytes"] + cam_cat["tx_bytes"]

            return {
                "status": "ok",
                "server_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                "uptime_sec": int(uptime_sec),
                "total_requests": self.total_requests,
                "total_rx_bytes": self.total_rx_bytes,
                "total_tx_bytes": self.total_tx_bytes,
                "total_bytes": self.total_rx_bytes + self.total_tx_bytes,
                "current_rx_bps": cur_rx_bps,
                "current_tx_bps": cur_tx_bps,
                "current_total_bps": round(cur_rx_bps + cur_tx_bps, 1),
                "current_rps": cur_rps,
                "current_cam_bps": cur_cam_bps,
                "cam_total_bytes": cam_total_bytes,
                "peak_rx_bps": self.peak_rx_bps,
                "peak_tx_bps": self.peak_tx_bps,
                "peak_rps": self.peak_rps,
                "status_counts": dict(self.status_counts),
                "active_ip_count_5m": active_ip_count_5m,
                "total_unique_ips": len(self.clients),
                "categories": categories_list,
                "timeline_60s": timeline_60s,
                "top_endpoints": top_endpoints,
                "top_clients": top_clients,
                "recent_requests": list(self.recent_requests),
            }


traffic_monitor = ServerTrafficMonitor()
