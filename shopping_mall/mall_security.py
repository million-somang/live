# -*- coding: utf-8 -*-
"""
독립 쇼핑몰 전용 보안 코어 모듈 (shopping_mall/mall_security.py)
- shopping_mall 폴더 단독 Git 배포 시에도 외부 의존성 없이 동일 보안 계층을 제공합니다.
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
from typing import Any, Optional
from urllib.parse import urlparse
from werkzeug.security import generate_password_hash, check_password_hash


def load_or_create_secret_key(env_var_name: str, key_file_path: str) -> str:
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
    return generate_password_hash(str(password or ""), method="scrypt")


def verify_password(password_hash: str, password: str) -> bool:
    if not password_hash or not password:
        return False
    try:
        return check_password_hash(str(password_hash), str(password))
    except Exception:
        return False


def validate_password_policy(password: str, min_len: int = 8) -> tuple:
    pw = str(password or "")
    if len(pw) < min_len:
        return False, f"비밀번호는 최소 {min_len}자 이상이어야 합니다."
    if len(pw) > 128:
        return False, "비밀번호가 너무 깁니다 (최대 128자)."
    has_letter = bool(re.search(r"[A-Za-z가-힣]", pw))
    has_digit = bool(re.search(r"[0-9]", pw))
    if not (has_letter and has_digit):
        return False, "비밀번호는 문자와 숫자를 모두 포함해야 합니다."
    return True, "OK"


def validate_guest_password_policy(password: str, min_len: int = 4) -> tuple:
    pw = str(password or "").strip()
    if len(pw) < min_len:
        return False, f"비회원 주문 조회 비밀번호는 최소 {min_len}자 이상 입력해 주세요."
    if len(pw) > 64:
        return False, "비밀번호는 64자 이하로 입력해 주세요."
    return True, "OK"


def constant_time_equals(val_a: str, val_b: str) -> bool:
    if not val_a or not val_b:
        return False
    try:
        return hmac.compare_digest(
            str(val_a).encode("utf-8"),
            str(val_b).encode("utf-8")
        )
    except Exception:
        return False


def is_safe_redirect_url(target: Any) -> bool:
    if not target or not isinstance(target, str):
        return False
    target = target.strip()
    if not target.startswith("/") or target.startswith("//") or target.startswith("/\\"):
        return False
    parsed = urlparse(target)
    return not parsed.scheme and not parsed.netloc


def get_csrf_token(session_obj) -> str:
    tok = session_obj.get("_csrf_token")
    if not tok or not isinstance(tok, str) or len(tok) < 32:
        tok = secrets.token_hex(32)
        session_obj["_csrf_token"] = tok
    return tok


def rotate_session(session_obj, preserve_keys=None):
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


def create_signed_token(secret_key: str, purpose: str, payload: Optional[dict] = None, ttl_sec: int = 86400) -> str:
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


def verify_signed_token(secret_key: Any, purpose: Any, token: Any):
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
    def __init__(self):
        self._lock = RLock()
        self._hits = {}
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
        expired_keys = [k for k, arr in self._hits.items() if not arr or (now - arr[-1]) > 600.0]
        for k in expired_keys:
            self._hits.pop(k, None)


rate_limiter = SlidingWindowRateLimiter()


def apply_security_headers(response, allow_framing: bool = False):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-XSS-Protection", "1; mode=block")
    if not allow_framing:
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return response
