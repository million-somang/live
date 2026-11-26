"""Pytest fixtures for isolated Live Commerce OS server, SQLite DB, and Playwright contexts."""

from __future__ import annotations

import collections
import os
import shutil
import sqlite3
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
from playwright.sync_api import Browser, BrowserContext, sync_playwright

from e2e_monitor_tests.harness import (
    PRESET_DUAL_MONITORS,
    VirtualEnvConfig,
    build_virtual_screen_init_script,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SECRET_KEY = "e2e_monitor_test_secret_key_min_32_chars_long_2026"
DEFAULT_ADMIN_USER = "e2eadmin"
DEFAULT_ADMIN_EMAIL = "e2eadmin@studio.local"
DEFAULT_ADMIN_PASS = "E2eAdminPass123!"
DEFAULT_INVITE_CODE = "E2E-INVITE-2026"

_SERVER_BOOTSTRAP_CODE = r"""
import sys
import database
database.sync_to_shopping_mall = lambda *a, **kw: True
import live_commerce_server
from werkzeug.serving import make_server

srv = make_server("127.0.0.1", 0, live_commerce_server.app, threaded=True)
print(f"READY_PORT={srv.server_port}", flush=True)
srv.serve_forever()
"""


@dataclass
class IsolatedLiveServer:
    """Handle to a subprocess-isolated Flask + SQLite Live Commerce OS instance."""

    data_dir: Path
    port: int
    process: subprocess.Popen[str]
    log_lines: collections.deque[str] = field(default_factory=lambda: collections.deque(maxlen=200))
    _stopped: bool = False

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "live_commerce.db"

    def clear_monitor_layouts(self) -> None:
        if not self.db_path.exists():
            return
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        try:
            conn.execute("DELETE FROM overlay_state WHERE key LIKE 'portal_monitor_layout_%'")
            conn.commit()
        finally:
            conn.close()

    def get_overlay_state(self, state_key: str) -> str | None:
        if not self.db_path.exists():
            return None
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        try:
            row = conn.execute(
                "SELECT value FROM overlay_state WHERE key = ?",
                (state_key,),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

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


def _start_isolated_server(data_dir: Path) -> IsolatedLiveServer:
    data_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(PROJECT_ROOT),
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "STUDIO_SECRET_KEY": DEFAULT_SECRET_KEY,
            "STUDIO_ADMIN_USERNAME": DEFAULT_ADMIN_USER,
            "STUDIO_ADMIN_EMAIL": DEFAULT_ADMIN_EMAIL,
            "STUDIO_ADMIN_PASSWORD": DEFAULT_ADMIN_PASS,
            "STUDIO_DEFAULT_INVITE_CODE": DEFAULT_INVITE_CODE,
            "MALL_SERVER_URL": "http://127.0.0.2:1",
            "ENABLE_PUBLIC_TUNNEL": "0",
        }
    )

    proc = subprocess.Popen(
        [sys.executable, "-c", _SERVER_BOOTSTRAP_CODE],
        cwd=str(data_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    log_lines: collections.deque[str] = collections.deque(maxlen=200)
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
            time.sleep(0.1)
            raise RuntimeError(
                f"Isolated server exited early (code={proc.returncode}). Logs:\n" + "\n".join(log_lines)
            )
        if time.monotonic() > deadline:
            proc.kill()
            raise TimeoutError("Timed out waiting for READY_PORT. Logs:\n" + "\n".join(log_lines))
        port_ready.wait(timeout=0.1)

    port = port_holder["port"]
    status_url = f"http://127.0.0.1:{port}/status"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(status_url, timeout=2.0) as resp:
                if resp.status == 200:
                    return IsolatedLiveServer(
                        data_dir=data_dir,
                        port=port,
                        process=proc,
                        log_lines=log_lines,
                    )
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.1)

    proc.kill()
    raise TimeoutError(f"Server bound port {port} but /status never returned 200. Logs:\n" + "\n".join(log_lines))


def register_studio_user_via_ui(
    browser: Browser,
    base_url: str,
    username: str,
    password: str,
    invite_code: str = DEFAULT_INVITE_CODE,
) -> dict[str, Any]:
    """Register an APPROVED studio operator via /auth/register and return Playwright storage_state."""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        page = ctx.new_page()
        page.goto(f"{base_url}/auth/register", wait_until="domcontentloaded")
        page.locator('input[name="username"]').fill(username)
        page.locator('input[name="email"]').fill(f"{username}@studio.local")
        page.locator('input[name="display_name"]').fill(username)
        page.locator('input[name="password"]').fill(password)
        page.locator('input[name="invite_code"]').fill(invite_code)
        page.locator('button[type="submit"]').click()
        page.wait_for_url(lambda url: "/portal" in url, timeout=15000)
        page.wait_for_load_state("domcontentloaded")
        return ctx.storage_state()
    finally:
        ctx.close()


def login_studio_user_via_ui(
    page,
    base_url: str,
    login_id: str,
    password: str,
) -> None:
    """Log in through the /auth/login UI form and wait for redirect to /portal."""
    page.goto(f"{base_url}/auth/login", wait_until="domcontentloaded")
    page.locator('input[name="login_id"]').fill(login_id)
    page.locator('input[name="password"]').fill(password)
    page.locator('button[type="submit"]').click()
    page.wait_for_url(lambda url: "/portal" in url, timeout=15000)
    page.wait_for_load_state("domcontentloaded")


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


@pytest.fixture(scope="session")
def live_server(tmp_path_factory: pytest.TempPathFactory) -> IsolatedLiveServer:
    srv_dir = tmp_path_factory.mktemp("live_session_srv")
    srv = _start_isolated_server(srv_dir)
    try:
        yield srv
    finally:
        srv.stop()
        shutil.rmtree(srv_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def operator_sessions(live_server: IsolatedLiveServer, browser: Browser) -> dict[str, dict[str, Any]]:
    state_a = register_studio_user_via_ui(browser, live_server.base_url, "e2e_op_a", "OpAPass123!")
    state_b = register_studio_user_via_ui(browser, live_server.base_url, "e2e_op_b", "OpBPass123!")
    live_server.clear_monitor_layouts()
    return {
        "operator_a": state_a,
        "operator_b": state_b,
    }


@pytest.fixture(autouse=True)
def _clean_session_monitor_layouts(request: pytest.FixtureRequest) -> None:
    """Ensure every test starts and ends with clean monitor layout state in SQLite."""
    if "live_server" in request.fixturenames:
        srv: IsolatedLiveServer = request.getfixturevalue("live_server")
        srv.clear_monitor_layouts()
        yield
        srv.clear_monitor_layouts()
    else:
        yield


@pytest.fixture
def server_factory(tmp_path: Path) -> Callable[[Path | None], IsolatedLiveServer]:
    """Factory spawning fresh or restarted isolated servers without polluting session DB."""
    started: list[IsolatedLiveServer] = []

    def _spawn(data_dir: Path | None = None) -> IsolatedLiveServer:
        target_dir = data_dir if data_dir is not None else (tmp_path / f"srv_{uuid.uuid4().hex[:8]}")
        srv = _start_isolated_server(target_dir)
        started.append(srv)
        return srv

    try:
        yield _spawn
    finally:
        for srv in reversed(started):
            srv.stop()


@pytest.fixture
def make_context(
    browser: Browser,
    operator_sessions: dict[str, dict[str, Any]],
) -> Callable[..., BrowserContext]:
    """Create a fresh Playwright BrowserContext with virtual multi-monitor init script."""
    created: list[BrowserContext] = []

    def _create(
        virtual_env: VirtualEnvConfig | None = None,
        storage_state: str | dict[str, Any] | None = "operator_a",
    ) -> BrowserContext:
        resolved_state: dict[str, Any] | None
        if isinstance(storage_state, str):
            resolved_state = operator_sessions[storage_state]
        else:
            resolved_state = storage_state

        kwargs: dict[str, Any] = {"viewport": {"width": 1440, "height": 900}}
        if resolved_state is not None:
            kwargs["storage_state"] = resolved_state

        ctx = browser.new_context(**kwargs)
        cfg = virtual_env or VirtualEnvConfig(screens=PRESET_DUAL_MONITORS)
        ctx.add_init_script(build_virtual_screen_init_script(cfg))
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
