"""Pytest fixtures for isolated Flask/SQLite subprocess servers, API clients, and Playwright."""

from __future__ import annotations

import collections
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

TESTS_ROOT = Path(__file__).resolve().parent.parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

from e2e.helpers import MallApiClient, seed_product_via_sync  # noqa: E402

MALL_DIR = Path(__file__).resolve().parents[2]

DEFAULT_ADMIN_EMAIL = "admin@e2e.local"
DEFAULT_ADMIN_PASSWORD = "AdminPass123!"
DEFAULT_SYNC_SECRET = "e2e-sync-secret-123456"
DEFAULT_MALL_SECRET_KEY = "e2e_mall_test_secret_key_min_32_chars_2026"
DEFAULT_STUDIO_SECRET_KEY = "e2e-disabled-studio-secret-key-2026"

_BASELINE_CATALOG: list[dict[str, Any]] = [
    {
        "num": 1,
        "name": "골라방 클래식 코튼 티셔츠",
        "category": "TOP",
        "price": 29000,
        "consumer_price": 39000,
        "live_price": 24000,
        "options": [
            {"color": "블랙", "size": "M", "stock": 200},
            {"color": "화이트", "size": "L", "stock": 200},
        ],
        "specs": "면 100% / 국내 제작",
    },
    {
        "num": 2,
        "name": "골라방 데일리 리브드 니트",
        "category": "TOP",
        "price": 42000,
        "consumer_price": 54000,
        "live_price": 36000,
        "options": [
            {"color": "아이보리", "size": "FREE", "stock": 12},
        ],
        "specs": "소프트 아크릴 혼방 / FREE",
    },
    {
        "num": 3,
        "name": "골라방 테일러드 크롭 자켓",
        "category": "OUTER",
        "price": 89000,
        "consumer_price": 119000,
        "live_price": 79000,
        "options": [
            {"color": "차콜", "size": "M", "stock": 10},
        ],
        "specs": "폴리 레이온 혼방 / 세미 크롭핏",
    },
    {
        "num": 4,
        "name": "골라방 와이드 핀턱 슬랙스",
        "category": "BOTTOM",
        "price": 49000,
        "consumer_price": 62000,
        "live_price": 44000,
        "options": [
            {"color": "블랙", "size": "S", "stock": 15},
            {"color": "베이지", "size": "M", "stock": 15},
        ],
        "specs": "사계절용 트윌 원단",
    },
    {
        "num": 5,
        "name": "골라방 플리츠 미디 원피스",
        "category": "DRESS",
        "price": 64000,
        "consumer_price": 82000,
        "live_price": 58000,
        "options": [
            {"color": "네이비", "size": "FREE", "stock": 8},
        ],
        "specs": "안감 포함 / 백 지퍼",
    },
    {
        "num": 6,
        "name": "골라방 미니멀 레더 크로스백",
        "category": "ACC",
        "price": 35000,
        "consumer_price": 45000,
        "live_price": 31000,
        "options": [
            {"color": "블랙", "size": "ONE", "stock": 18},
        ],
        "specs": "스트랩 길이 조절 가능",
    },
]

_SERVER_BOOTSTRAP_CODE = r"""
import sys
from werkzeug.serving import make_server
import app as mall_app

srv = make_server("127.0.0.1", 0, mall_app.app, threaded=True)
if hasattr(srv, "daemon_threads"):
    srv.daemon_threads = True
print(f"READY_PORT={srv.server_port}", flush=True)
srv.serve_forever()
"""


@dataclass
class ServerHandle:
    """Handle to a subprocess-isolated Flask/Werkzeug shopping_mall server."""

    work_dir: Path
    db_path: Path
    admin_email: str
    admin_password: str
    sync_secret: str
    port: int
    process: subprocess.Popen[str]
    log_lines: collections.deque[str] = field(
        default_factory=lambda: collections.deque(maxlen=250)
    )
    _stopped: bool = False

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


def _start_mall_server(
    work_dir: Path,
    env_overrides: dict[str, str] | None = None,
) -> ServerHandle:
    work_dir.mkdir(parents=True, exist_ok=True)
    default_db_path = work_dir / "mall_test.db"
    disabled_obs_db_path = work_dir / "__disabled_obs__.db"
    product_upload_dir = work_dir / "uploads_products"
    review_upload_dir = work_dir / "uploads_reviews"
    backup_dir = work_dir / "backups"
    product_upload_dir.mkdir(parents=True, exist_ok=True)
    review_upload_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(MALL_DIR),
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "MALL_DB_PATH": str(default_db_path),
            "MALL_PRODUCT_UPLOAD_DIR": str(product_upload_dir),
            "MALL_REVIEW_UPLOAD_DIR": str(review_upload_dir),
            "MALL_BACKUP_DIR": str(backup_dir),
            "MALL_MODE": "cloud",
            "OBS_PULL_ENABLED": "0",
            "MALL_SCHEDULER_ENABLED": "0",
            "OBS_SERVER_URL": "http://127.0.0.2:1",
            "OBS_DB_PATH": str(disabled_obs_db_path),
            "STUDIO_SECRET_KEY": DEFAULT_STUDIO_SECRET_KEY,
            "MALL_SECRET_KEY": DEFAULT_MALL_SECRET_KEY,
            "MALL_ADMIN_EMAIL": DEFAULT_ADMIN_EMAIL,
            "MALL_ADMIN_PASSWORD": DEFAULT_ADMIN_PASSWORD,
            "MALL_SYNC_SECRET": DEFAULT_SYNC_SECRET,
            "MALL_ALLOW_TEST_BACKDATE": "1",
        }
    )
    if env_overrides:
        for key, value in env_overrides.items():
            env[str(key)] = str(value)

    proc = subprocess.Popen(
        [sys.executable, "-c", _SERVER_BOOTSTRAP_CODE],
        cwd=str(work_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    log_lines: collections.deque[str] = collections.deque(maxlen=250)
    port_holder: dict[str, int] = {}
    port_ready = threading.Event()

    def _drain_output() -> None:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\r\n")
            log_lines.append(line)
            if line.startswith("READY_PORT=") and "port" not in port_holder:
                try:
                    port_holder["port"] = int(line.split("=", 1)[1].strip())
                    port_ready.set()
                except ValueError:
                    pass

    reader = threading.Thread(target=_drain_output, daemon=True)
    reader.start()

    deadline = time.monotonic() + 25.0
    while not port_ready.is_set():
        if proc.poll() is not None:
            time.sleep(0.05)
            raise RuntimeError(
                f"Mall server exited early (code={proc.returncode}). Logs:\n"
                + "\n".join(log_lines)
            )
        if time.monotonic() > deadline:
            proc.kill()
            proc.wait(timeout=5)
            raise TimeoutError(
                "Timed out waiting for READY_PORT. Logs:\n" + "\n".join(log_lines)
            )
        port_ready.wait(timeout=0.1)

    port = port_holder["port"]
    health_url = f"http://127.0.0.1:{port}/api/live/state-lite"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            time.sleep(0.05)
            raise RuntimeError(
                f"Mall server crashed before health check passed (code={proc.returncode}). Logs:\n"
                + "\n".join(log_lines)
            )
        try:
            with urllib.request.urlopen(health_url, timeout=2.0) as resp:
                if resp.status == 200:
                    return ServerHandle(
                        work_dir=work_dir,
                        db_path=Path(env["MALL_DB_PATH"]),
                        admin_email=env["MALL_ADMIN_EMAIL"],
                        admin_password=env["MALL_ADMIN_PASSWORD"],
                        sync_secret=env["MALL_SYNC_SECRET"],
                        port=port,
                        process=proc,
                        log_lines=log_lines,
                    )
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.1)

    proc.kill()
    proc.wait(timeout=5)
    raise TimeoutError(
        f"Mall server bound port {port} but /api/live/state-lite never returned 200. Logs:\n"
        + "\n".join(log_lines)
    )


def _seed_baseline_catalog(server: ServerHandle) -> None:
    for item in _BASELINE_CATALOG:
        seed_product_via_sync(
            server.base_url,
            server.sync_secret,
            num=item["num"],
            name=item["name"],
            category=item["category"],
            price=item["price"],
            consumer_price=item["consumer_price"],
            live_price=item["live_price"],
            options=item["options"],
            image_url="",
            specs=item["specs"],
        )


@pytest.fixture
def server_factory(
    tmp_path: Path,
) -> Callable[..., ServerHandle]:
    """Spawn fresh or restarted isolated servers under temporary directories."""
    started: list[ServerHandle] = []

    def _spawn(
        env_overrides: dict[str, str] | None = None,
        *,
        seed_products: bool = True,
        work_dir: Path | None = None,
    ) -> ServerHandle:
        target_dir = (
            Path(work_dir)
            if work_dir is not None
            else (tmp_path / f"srv_{uuid.uuid4().hex[:8]}")
        )
        srv = _start_mall_server(target_dir, env_overrides=env_overrides)
        started.append(srv)
        if seed_products:
            _seed_baseline_catalog(srv)
        return srv

    try:
        yield _spawn
    finally:
        for srv in reversed(started):
            srv.stop()


@pytest.fixture(scope="session")
def live_server(tmp_path_factory: pytest.TempPathFactory) -> ServerHandle:
    """Session-scoped isolated server pre-seeded with the baseline catalog."""
    srv_dir = tmp_path_factory.mktemp("mall_live_session_srv")
    srv = _start_mall_server(srv_dir)
    try:
        _seed_baseline_catalog(srv)
        yield srv
    finally:
        srv.stop()
        shutil.rmtree(srv_dir, ignore_errors=True)


@pytest.fixture
def api_client_factory() -> Callable[[ServerHandle], MallApiClient]:
    """Return a factory creating isolated MallApiClient instances for any ServerHandle."""
    created: list[MallApiClient] = []

    def _create(server: ServerHandle) -> MallApiClient:
        client = MallApiClient(server.base_url)
        created.append(client)
        return client

    try:
        yield _create
    finally:
        for client in reversed(created):
            client.session.close()


@pytest.fixture(scope="session")
def _session_admin_client(live_server: ServerHandle) -> MallApiClient:
    client = MallApiClient(live_server.base_url)
    client.login(live_server.admin_email, live_server.admin_password)
    try:
        yield client
    finally:
        client.session.close()


@pytest.fixture(scope="session")
def member_account(live_server: ServerHandle) -> dict[str, Any]:
    """Pre-registered reusable member account on live_server (rate-limit safe)."""
    email = "e2e_member@e2e.local"
    password = "MemberPass123!"
    client = MallApiClient(live_server.base_url)
    try:
        reg = client.register(
            email=email,
            password=password,
            name="E2E기본회원",
            phone="010-5555-6666",
            zipcode="06236",
            address="서울특별시 강남구 테헤란로 152",
            address_detail="101호",
        )
        return {
            "email": email,
            "password": password,
            "name": "E2E기본회원",
            "phone": "010-5555-6666",
            "zipcode": "06236",
            "address": "서울특별시 강남구 테헤란로 152",
            "address_detail": "101호",
            "user": reg.get("user", {}),
            "_cookies": client.session.cookies.get_dict(),
        }
    finally:
        client.session.close()


@pytest.fixture(scope="session")
def _session_member_client(
    live_server: ServerHandle,
    member_account: dict[str, Any],
) -> MallApiClient:
    client = MallApiClient(live_server.base_url)
    client.login(member_account["email"], member_account["password"])
    try:
        yield client
    finally:
        client.session.close()


@pytest.fixture
def admin_api(_session_admin_client: MallApiClient) -> MallApiClient:
    """Function-scoped admin MallApiClient cloning session cookies + fresh CSRF."""
    client = _session_admin_client.clone_with_cookies(refresh_csrf=True)
    try:
        yield client
    finally:
        client.session.close()


@pytest.fixture
def member_api(_session_member_client: MallApiClient) -> MallApiClient:
    """Function-scoped member MallApiClient cloning session cookies + fresh CSRF."""
    client = _session_member_client.clone_with_cookies(refresh_csrf=True)
    try:
        yield client
    finally:
        client.session.close()


@pytest.fixture
def guest_api(live_server: ServerHandle) -> MallApiClient:
    """Function-scoped completely clean unauthenticated MallApiClient."""
    client = MallApiClient(live_server.base_url)
    try:
        yield client
    finally:
        client.session.close()


@pytest.fixture(scope="session")
def playwright_instance():
    pw = sync_playwright().start()
    try:
        yield pw
    finally:
        pw.stop()


@pytest.fixture(scope="session")
def browser(playwright_instance) -> Browser:
    b = playwright_instance.chromium.launch(headless=True)
    try:
        yield b
    finally:
        b.close()


def _build_storage_state_via_login(
    browser: Browser,
    base_url: str,
    email: str,
    password: str,
) -> dict[str, Any]:
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        page = ctx.new_page()
        page.goto(f"{base_url}/auth/login", wait_until="domcontentloaded")
        result = page.evaluate(
            """async ({ email, password }) => {
                const resp = await fetch('/auth/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ email, password, next: '/' })
                });
                const text = await resp.text();
                return { status: resp.status, text };
            }""",
            {"email": email, "password": password},
        )
        if result["status"] != 200:
            raise RuntimeError(
                f"Failed to establish Playwright storage_state for {email!r}: {result!r}"
            )
        return ctx.storage_state()
    finally:
        ctx.close()


@pytest.fixture(scope="session")
def admin_storage_state(
    browser: Browser,
    live_server: ServerHandle,
) -> dict[str, Any]:
    """Session-scoped pre-authenticated Playwright storage_state for admin."""
    return _build_storage_state_via_login(
        browser,
        live_server.base_url,
        live_server.admin_email,
        live_server.admin_password,
    )


@pytest.fixture(scope="session")
def member_storage_state(
    browser: Browser,
    live_server: ServerHandle,
    member_account: dict[str, Any],
) -> dict[str, Any]:
    """Session-scoped pre-authenticated Playwright storage_state for member."""
    return _build_storage_state_via_login(
        browser,
        live_server.base_url,
        member_account["email"],
        member_account["password"],
    )


@pytest.fixture
def desktop_context_factory(
    browser: Browser,
) -> Callable[..., BrowserContext]:
    """Create desktop (1440x900) Playwright contexts; closed in reverse order."""
    created: list[BrowserContext] = []

    def _create(
        *,
        storage_state: dict[str, Any] | str | None = None,
        **extra_kwargs: Any,
    ) -> BrowserContext:
        kwargs: dict[str, Any] = {"viewport": {"width": 1440, "height": 900}}
        kwargs.update(extra_kwargs)
        if storage_state is not None:
            kwargs["storage_state"] = storage_state
        ctx = browser.new_context(**kwargs)
        created.append(ctx)
        return ctx

    try:
        yield _create
    finally:
        for ctx in reversed(created):
            try:
                ctx.close()
            except Exception:
                pass


@pytest.fixture
def mobile_context_factory(
    browser: Browser,
) -> Callable[..., BrowserContext]:
    """Create mobile (390x844) Playwright contexts; closed in reverse order."""
    created: list[BrowserContext] = []

    def _create(
        *,
        storage_state: dict[str, Any] | str | None = None,
        **extra_kwargs: Any,
    ) -> BrowserContext:
        kwargs: dict[str, Any] = {"viewport": {"width": 390, "height": 844}}
        kwargs.update(extra_kwargs)
        if storage_state is not None:
            kwargs["storage_state"] = storage_state
        ctx = browser.new_context(**kwargs)
        created.append(ctx)
        return ctx

    try:
        yield _create
    finally:
        for ctx in reversed(created):
            try:
                ctx.close()
            except Exception:
                pass


@pytest.fixture
def desktop_context(desktop_context_factory: Callable[..., BrowserContext]) -> BrowserContext:
    return desktop_context_factory()


@pytest.fixture
def mobile_context(mobile_context_factory: Callable[..., BrowserContext]) -> BrowserContext:
    return mobile_context_factory()


@pytest.fixture
def desktop_page(desktop_context: BrowserContext) -> Page:
    return desktop_context.new_page()


@pytest.fixture
def mobile_page(mobile_context: BrowserContext) -> Page:
    return mobile_context.new_page()
