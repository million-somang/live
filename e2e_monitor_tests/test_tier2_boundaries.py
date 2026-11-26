"""Tier 2: Boundary & Corner-Case Tests (24 tests — 7 F1, 5 F2, 6 F3, 6 F4)."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from e2e_monitor_tests.conftest import (
    DEFAULT_INVITE_CODE,
    login_studio_user_via_ui,
    register_studio_user_via_ui,
)
from e2e_monitor_tests.harness import (
    PRESET_DUAL_MONITORS,
    PRESET_FIVE_MONITORS,
    PRESET_SINGLE_MONITOR,
    PRESET_TRIPLE_MONITORS,
    PortalPage,
    VirtualEnvConfig,
    assert_bounds_match_monitor,
    build_virtual_screen_init_script,
    make_screen,
)


pytestmark = [pytest.mark.tier2]


# =====================================================================
# F1 Boundaries (7 tests)
# =====================================================================


@pytest.mark.f1
def test_t2_f1_01_single_monitor(live_server, make_context):
    """T2-F1-01: Single-monitor setup displays 1 monitor and allows assigning programs to it."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_SINGLE_MONITOR))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    assert "Built-in Studio Laptop" in portal.monitor_section.inner_text()
    chosen = portal.select_monitor_matching("broadcast_chat", "Built-in Studio Laptop")
    assert portal.get_selected_value("broadcast_chat") == chosen["value"]
    assert portal.pageerrors == []


@pytest.mark.f1
def test_t2_f1_02_many_monitors_five_screens(live_server, make_context):
    """T2-F1-02: 5 heterogeneous monitors all appear in discovery UI and dropdown options."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_FIVE_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    panel_text = portal.monitor_section.inner_text()
    for scr in PRESET_FIVE_MONITORS:
        assert scr["label"] in panel_text

    opts_text = "\n".join(o["text"] for o in portal.get_options("obs_studio"))
    for scr in PRESET_FIVE_MONITORS:
        assert scr["label"] in opts_text
    assert portal.pageerrors == []


@pytest.mark.f1
def test_t2_f1_03_negative_coordinates(live_server, make_context):
    """T2-F1-03: Negative monitor coordinates (left=-1920, top=-1080) are preserved without clamping to 0."""
    screens = [
        make_screen("Upper Left Neg", -1920, -1080, 1920, 1080, avail_left=-1920, avail_top=-1080, avail_width=1920, avail_height=1040),
        make_screen("Primary Origin", 0, 0, 1920, 1080, is_primary=True),
    ]
    ctx = make_context(VirtualEnvConfig(screens=screens))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    panel_text = portal.monitor_section.inner_text()
    assert "-1920" in panel_text
    assert "-1080" in panel_text

    portal.select_monitor_matching("obs_studio", "Upper Left Neg")
    saved = portal.save_layout()
    neg_mon = next(m for m in saved["layout"]["monitors"] if m["label"] == "Upper Left Neg")
    assert neg_mon["left"] == -1920
    assert neg_mon["top"] == -1080
    assert neg_mon["availLeft"] == -1920
    assert neg_mon["availTop"] == -1080


@pytest.mark.f1
def test_t2_f1_04_permission_denied_fallback(live_server, make_context):
    """T2-F1-04: Denied 'window-management' permission shows guidance and falls back to window.screen cleanly."""
    fallback = {"left": 0, "top": 0, "width": 1600, "height": 900, "availLeft": 0, "availTop": 0, "availWidth": 1600, "availHeight": 860}
    ctx = make_context(
        VirtualEnvConfig(
            screens=PRESET_DUAL_MONITORS,
            permission_mode="denied",
            fallback_screen=fallback,
        )
    )
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    expect(portal.status_region).to_be_visible(timeout=5000)
    expect(portal.status_region).to_contain_text(
        re.compile(r"거부|권한\s*거부|차단|denied|notallowed", re.IGNORECASE)
    )
    non_btn_text = portal.monitor_section_non_button_text()
    assert re.search(r"거부|권한\s*거부|차단|denied|notallowed", non_btn_text, re.IGNORECASE)
    assert "1600" in non_btn_text and "900" in non_btn_text
    assert portal.pageerrors == []


@pytest.mark.f1
def test_t2_f1_05_insecure_context_or_missing_api_fallback(live_server, make_context):
    """T2-F1-05: Insecure context (isSecureContext=false / no getScreenDetails) falls back cleanly with notice."""
    fallback = {"left": 0, "top": 0, "width": 1280, "height": 720, "availLeft": 0, "availTop": 0, "availWidth": 1280, "availHeight": 680}
    ctx = make_context(
        VirtualEnvConfig(
            screens=PRESET_DUAL_MONITORS,
            permission_mode="insecure",
            fallback_screen=fallback,
        )
    )
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    panel_text = portal.monitor_section.inner_text()
    assert re.search(r"HTTPS|보안\s*컨텍스트|미지원|지원하지|기본\s*화면", panel_text, re.IGNORECASE)
    assert "1280" in panel_text and "720" in panel_text
    assert portal.pageerrors == []


@pytest.mark.f1
def test_t2_f1_06_dynamic_screenschange_attach_and_detach(live_server, make_context):
    """T2-F1-06: Live screenschange attach and detach events update monitor list without reload."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()
    assert "Dell 27 QHD" in portal.monitor_section.inner_text()

    # Attach Monitor 3
    portal.set_virtual_screens(PRESET_TRIPLE_MONITORS)
    expect(portal.monitor_section).to_contain_text("Right Prompter QHD", timeout=5000)

    # Detach Monitor 2 (keep first and third)
    reduced = [PRESET_TRIPLE_MONITORS[0], PRESET_TRIPLE_MONITORS[2]]
    portal.set_virtual_screens(reduced)
    expect(portal.monitor_section).not_to_contain_text("Center Primary FHD", timeout=5000)
    expect(portal.monitor_section).to_contain_text("Right Prompter QHD", timeout=5000)
    assert portal.pageerrors == []


@pytest.mark.f1
def test_t2_f1_07_high_dpi_and_taskbar_inset_avail_bounds(live_server, make_context):
    """T2-F1-07: High-DPI (2.5) and asymmetric taskbar insets preserve avail* distinct from physical width/height."""
    screens = [
        make_screen(
            "HiDPI Docked 4K",
            0,
            0,
            3840,
            2160,
            avail_left=80,
            avail_top=40,
            avail_width=3760,
            avail_height=2060,
            is_primary=True,
            device_pixel_ratio=2.5,
        )
    ]
    ctx = make_context(VirtualEnvConfig(screens=screens))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    panel_text = portal.monitor_section.inner_text()
    assert "3840" in panel_text and "2160" in panel_text
    assert "3760" in panel_text and "2060" in panel_text

    portal.select_monitor_matching("obs_studio", "HiDPI Docked 4K")
    saved = portal.save_layout()
    mon = saved["layout"]["monitors"][0]
    assert mon["devicePixelRatio"] == 2.5
    assert mon["availLeft"] == 80
    assert mon["availTop"] == 40
    assert mon["availWidth"] == 3760
    assert mon["availHeight"] == 2060


# =====================================================================
# F2 Boundaries (5 tests)
# =====================================================================


@pytest.mark.f2
def test_t2_f2_01_all_three_programs_assigned_to_same_monitor(live_server, make_context):
    """T2-F2-01: Assigning all 3 programs to the same single monitor succeeds and persists."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    o1 = portal.select_monitor_matching("obs_studio", "Dell 27 QHD")
    o2 = portal.select_monitor_matching("broadcast_chat", "Dell 27 QHD")
    o3 = portal.select_monitor_matching("host_prompter", "Dell 27 QHD")
    saved = portal.save_layout()

    assert o1["value"] == o2["value"] == o3["value"]
    assert saved["status"] == "ok"
    assert portal.pageerrors == []


@pytest.mark.f2
def test_t2_f2_02_all_three_programs_set_to_unassigned(live_server, make_context):
    """T2-F2-02: Switching all 3 programs back to '미배치' saves an all-unassigned configuration."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    portal.select_monitor_matching("obs_studio", "LG FHD Primary")
    portal.select_monitor_matching("broadcast_chat", "Dell 27 QHD")
    portal.select_monitor_matching("host_prompter", "LG FHD Primary")
    portal.save_layout()

    for prog in ("obs_studio", "broadcast_chat", "host_prompter"):
        portal.select_unassigned(prog)
    saved = portal.save_layout()

    for prog in ("obs_studio", "broadcast_chat", "host_prompter"):
        assert saved["layout"]["assignments"][prog] == "unassigned"
        assert "미배치" in portal.get_selected_text(prog)


@pytest.mark.f2
def test_t2_f2_03_partial_assignment_one_assigned_two_unassigned(live_server, make_context):
    """T2-F2-03: Assigning only 1 program and leaving 2 as '미배치' preserves exact partial selection."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    portal.select_unassigned("obs_studio")
    portal.select_unassigned("broadcast_chat")
    host_opt = portal.select_monitor_matching("host_prompter", "Dell 27 QHD")
    portal.save_layout()

    portal.page.reload(wait_until="domcontentloaded")
    assert "미배치" in portal.get_selected_text("obs_studio")
    assert "미배치" in portal.get_selected_text("broadcast_chat")
    expect(portal.program_select("host_prompter")).to_have_value(host_opt["value"], timeout=8000)


@pytest.mark.f2
def test_t2_f2_04_rapid_consecutive_switching_before_save(live_server, make_context):
    """T2-F2-04: Rapidly cycling selectors across monitors and '미배치' settles accurately on final choice."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_TRIPLE_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    for _ in range(3):
        portal.select_monitor_matching("obs_studio", "Left Studio FHD")
        portal.select_monitor_matching("obs_studio", "Center Primary FHD")
        portal.select_unassigned("obs_studio")

        portal.select_monitor_matching("broadcast_chat", "Right Prompter QHD")
        portal.select_monitor_matching("broadcast_chat", "Left Studio FHD")

        portal.select_unassigned("host_prompter")
        portal.select_monitor_matching("host_prompter", "Center Primary FHD")

    final_obs = portal.select_monitor_matching("obs_studio", "Right Prompter QHD")
    final_chat = portal.select_unassigned("broadcast_chat")
    final_host = portal.select_monitor_matching("host_prompter", "Left Studio FHD")
    portal.save_layout()

    assert portal.get_selected_value("obs_studio") == final_obs["value"]
    assert portal.get_selected_value("broadcast_chat") == final_chat
    assert portal.get_selected_value("host_prompter") == final_host["value"]

    portal.page.reload(wait_until="domcontentloaded")
    expect(portal.program_select("obs_studio")).to_have_value(final_obs["value"], timeout=8000)
    assert "미배치" in portal.get_selected_text("broadcast_chat")
    expect(portal.program_select("host_prompter")).to_have_value(final_host["value"], timeout=8000)


@pytest.mark.f2
def test_t2_f2_05_special_character_xss_and_identical_hardware_labels(live_server, make_context):
    """T2-F2-05: Identical monitor hardware labels remain distinguishable, and XSS-like labels are escaped safely."""
    xss_label = '<img src=x onerror=alert(1)> "4K" & Main'
    screens = [
        make_screen("Dell U2723QE", 0, 0, 2560, 1440, avail_height=1400, is_primary=True),
        make_screen("Dell U2723QE", 2560, 0, 2560, 1440, avail_height=1400, is_primary=False),
        make_screen(xss_label, 5120, 0, 3840, 2160, avail_height=2100, is_primary=False),
    ]
    ctx = make_context(VirtualEnvConfig(screens=screens))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    assert portal.page.evaluate("() => Boolean(window.__xssTriggered)") is False
    assert portal.monitor_section.locator("img").count() == 0
    panel_text = portal.monitor_section.inner_text()
    assert "Dell U2723QE" in panel_text
    assert "<img" in panel_text

    # Select first and second identical Dell U2723QE monitors independently
    first_dell = portal.select_monitor_matching("obs_studio", "Dell U2723QE", occurrence=0)
    second_dell = portal.select_monitor_matching("broadcast_chat", "Dell U2723QE", occurrence=1)
    xss_opt = portal.select_monitor_matching("host_prompter", "4K")

    assert first_dell["value"] != second_dell["value"]
    portal.save_layout()

    portal.page.reload(wait_until="domcontentloaded")
    expect(portal.program_select("obs_studio")).to_have_value(first_dell["value"], timeout=8000)
    expect(portal.program_select("broadcast_chat")).to_have_value(second_dell["value"], timeout=8000)
    expect(portal.program_select("host_prompter")).to_have_value(xss_opt["value"], timeout=8000)
    assert portal.monitor_section.locator("img").count() == 0
    assert portal.page.evaluate("() => Boolean(window.__xssTriggered)") is False


# =====================================================================
# F3 Boundaries (6 tests)
# =====================================================================


@pytest.mark.f3
def test_t2_f3_01_disconnected_stale_monitor_preserved_not_overwritten(live_server, make_context):
    """T2-F3-01: When a previously assigned monitor is disconnected, UI flags it as stale without overwriting saved config."""
    ctx1 = make_context(VirtualEnvConfig(screens=PRESET_TRIPLE_MONITORS))
    p1 = PortalPage(ctx1.new_page(), live_server.base_url)
    p1.goto()
    p1.click_discover()
    obs_m1 = p1.select_monitor_matching("obs_studio", "Left Studio FHD")
    chat_m2 = p1.select_monitor_matching("broadcast_chat", "Center Primary FHD")
    host_m3 = p1.select_monitor_matching("host_prompter", "Right Prompter QHD")
    p1.save_layout()

    # Reopen in fresh context where Monitor 3 (Right Prompter QHD) is unplugged
    two_monitors = [PRESET_TRIPLE_MONITORS[0], PRESET_TRIPLE_MONITORS[1]]
    ctx2 = make_context(VirtualEnvConfig(screens=two_monitors))
    p2 = PortalPage(ctx2.new_page(), live_server.base_url)
    p2.goto()

    expect(p2.program_select("obs_studio")).to_have_value(obs_m1["value"], timeout=8000)
    expect(p2.program_select("broadcast_chat")).to_have_value(chat_m2["value"], timeout=8000)
    expect(p2.program_select("host_prompter")).to_have_value(host_m3["value"], timeout=8000)

    panel_text = p2.monitor_section.inner_text()
    assert re.search(r"연결\s*해제|미연결|연결되지|연결\s*끊김|disconnected|이전\s*모니터", panel_text, re.IGNORECASE)
    assert "Right Prompter QHD" in p2.get_selected_text("host_prompter")


@pytest.mark.f3
def test_t2_f3_02_multi_user_isolation_on_same_sqlite_db(live_server, make_context):
    """T2-F3-02: Operator A and Operator B maintain isolated monitor layouts in the same SQLite database."""
    ctx_a = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS), storage_state="operator_a")
    portal_a = PortalPage(ctx_a.new_page(), live_server.base_url)
    portal_a.goto()
    portal_a.click_discover()
    a_obs = portal_a.select_monitor_matching("obs_studio", "Dell 27 QHD")
    a_chat = portal_a.select_monitor_matching("broadcast_chat", "Dell 27 QHD")
    portal_a.select_unassigned("host_prompter")
    portal_a.save_layout()

    # Operator B opens /portal and should see clean defaults (not Operator A's Dell 27 QHD config)
    ctx_b = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS), storage_state="operator_b")
    portal_b = PortalPage(ctx_b.new_page(), live_server.base_url)
    portal_b.goto()
    portal_b.click_discover()

    assert "미배치" in portal_b.get_selected_text("obs_studio")
    assert "Dell 27 QHD" not in portal_b.get_selected_text("broadcast_chat")
    assert "미배치" in portal_b.get_selected_text("host_prompter")

    # Operator B saves a different config
    portal_b.select_unassigned("obs_studio")
    b_chat = portal_b.select_monitor_matching("broadcast_chat", "LG FHD Primary")
    b_host = portal_b.select_monitor_matching("host_prompter", "Dell 27 QHD")
    portal_b.save_layout()

    # Verify Operator A's config remains intact
    portal_a.page.reload(wait_until="domcontentloaded")
    expect(portal_a.program_select("obs_studio")).to_have_value(a_obs["value"], timeout=8000)
    expect(portal_a.program_select("broadcast_chat")).to_have_value(a_chat["value"], timeout=8000)
    assert "미배치" in portal_a.get_selected_text("host_prompter")

    # Verify Operator B's config remains intact across all 3 programs
    portal_b.page.reload(wait_until="domcontentloaded")
    assert "미배치" in portal_b.get_selected_text("obs_studio")
    expect(portal_b.program_select("broadcast_chat")).to_have_value(b_chat["value"], timeout=8000)
    expect(portal_b.program_select("host_prompter")).to_have_value(b_host["value"], timeout=8000)


@pytest.mark.f3
def test_t2_f3_03_malformed_corrupted_localstorage_fallback(live_server, make_context):
    """T2-F3-03: Corrupted JSON or invalid schema in localStorage recovers cleanly from SQLite backend."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    obs_sel = portal.select_monitor_matching("obs_studio", "Dell 27 QHD")
    chat_sel = portal.select_monitor_matching("broadcast_chat", "LG FHD Primary")
    portal.select_unassigned("host_prompter")
    portal.save_layout()

    # Phase 1: Corrupt all existing localStorage keys with invalid JSON
    portal.page.evaluate(
        """() => {
            const keys = Object.keys(window.localStorage);
            for (const k of keys) {
                window.localStorage.setItem(k, "{corrupt_json_value");
            }
        }"""
    )
    portal.page.reload(wait_until="domcontentloaded")
    expect(portal.program_select("obs_studio")).to_have_value(obs_sel["value"], timeout=8000)
    expect(portal.program_select("broadcast_chat")).to_have_value(chat_sel["value"], timeout=8000)
    assert portal.pageerrors == []

    # Phase 2: Overwrite localStorage keys with wrong-schema JSON
    portal.page.evaluate(
        """() => {
            const keys = Object.keys(window.localStorage);
            for (const k of keys) {
                window.localStorage.setItem(k, JSON.stringify({ monitors: "invalid", assignments: 12345 }));
            }
        }"""
    )
    portal.page.reload(wait_until="domcontentloaded")
    expect(portal.program_select("obs_studio")).to_have_value(obs_sel["value"], timeout=8000)
    expect(portal.program_select("broadcast_chat")).to_have_value(chat_sel["value"], timeout=8000)
    assert portal.pageerrors == []


@pytest.mark.f3
def test_t2_f3_04_session_logout_and_fresh_context_relogin(server_factory, browser):
    """T2-F3-04: Saving layout, logging out via /auth/logout, and re-logging in via fresh context restores layout."""
    srv = server_factory(None)
    register_studio_user_via_ui(browser, srv.base_url, "logout_op", "LogoutPass123!", DEFAULT_INVITE_CODE)

    ctx1 = browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        ctx1.add_init_script(build_virtual_screen_init_script(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS)))
        p1 = ctx1.new_page()
        login_studio_user_via_ui(p1, srv.base_url, "logout_op", "LogoutPass123!")
        portal1 = PortalPage(p1, srv.base_url)
        portal1.goto()
        portal1.click_discover()

        obs_sel = portal1.select_monitor_matching("obs_studio", "Dell 27 QHD")
        host_sel = portal1.select_monitor_matching("host_prompter", "LG FHD Primary")
        portal1.select_unassigned("broadcast_chat")
        portal1.save_layout()

        p1.goto(f"{srv.base_url}/auth/logout", wait_until="domcontentloaded")
        expect(p1).to_have_url(re.compile(r"(/$|/auth/login)"))
    finally:
        ctx1.close()

    # Fresh browser context logs back in
    ctx2 = browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        ctx2.add_init_script(build_virtual_screen_init_script(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS)))
        p2 = ctx2.new_page()
        login_studio_user_via_ui(p2, srv.base_url, "logout_op", "LogoutPass123!")
        portal2 = PortalPage(p2, srv.base_url)
        portal2.goto()

        expect(portal2.program_select("obs_studio")).to_have_value(obs_sel["value"], timeout=8000)
        assert "미배치" in portal2.get_selected_text("broadcast_chat")
        expect(portal2.program_select("host_prompter")).to_have_value(host_sel["value"], timeout=8000)
    finally:
        ctx2.close()


@pytest.mark.f3
def test_t2_f3_05_invalid_payload_and_csrf_rejection_on_api(live_server, make_context):
    """T2-F3-05: POST /api/portal/monitor-layout rejects missing CSRF (403) and malformed payloads (400) without clobbering saved layout."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    obs_sel = portal.select_monitor_matching("obs_studio", "Dell 27 QHD")
    portal.select_monitor_matching("broadcast_chat", "LG FHD Primary")
    portal.select_unassigned("host_prompter")
    portal.save_layout()

    csrf_token = portal.page.locator('meta[name="csrf-token"]').get_attribute("content")
    assert csrf_token

    api_url = f"{live_server.base_url}/api/portal/monitor-layout"
    valid_payload = {
        "monitors": [],
        "assignments": {"obs_studio": "unassigned", "broadcast_chat": "unassigned", "host_prompter": "unassigned"},
    }

    # 1. Missing CSRF token -> 403
    r_no_csrf = ctx.request.post(api_url, data=valid_payload)
    assert r_no_csrf.status == 403

    headers = {"X-CSRF-Token": csrf_token}

    # 2. Unknown program key -> 400
    r_unknown_prog = ctx.request.post(
        api_url,
        headers=headers,
        data={"monitors": [], "assignments": {"unknown_app": "m1"}},
    )
    assert r_unknown_prog.status == 400

    # 3. Out-of-range coordinates -> 400
    r_bad_coord = ctx.request.post(
        api_url,
        headers=headers,
        data={
            "monitors": [{"key": "m1", "index": 0, "width": 999999, "height": 1080, "left": 0, "top": 0}],
            "assignments": {"obs_studio": "m1", "broadcast_chat": "unassigned", "host_prompter": "unassigned"},
        },
    )
    assert r_bad_coord.status == 400

    # 4. Too many monitors (>32) -> 400
    too_many = [{"key": f"m{i}", "index": i, "width": 1920, "height": 1080, "left": 0, "top": 0} for i in range(33)]
    r_too_many = ctx.request.post(
        api_url,
        headers=headers,
        data={
            "monitors": too_many,
            "assignments": {"obs_studio": "unassigned", "broadcast_chat": "unassigned", "host_prompter": "unassigned"},
        },
    )
    assert r_too_many.status == 400

    # Verify previously saved layout remains intact
    r_get = ctx.request.get(api_url)
    assert r_get.status == 200
    saved_assign = r_get.json()["layout"]["assignments"]["obs_studio"]
    saved_key = saved_assign["monitorKey"] if isinstance(saved_assign, dict) else saved_assign
    assert saved_key == obs_sel["value"]


@pytest.mark.f3
def test_t2_f3_06_unauthenticated_access_guard(live_server, make_context):
    """T2-F3-06: Unauthenticated requests receive 401 on monitor-layout API and redirect to /auth/login on /portal."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS), storage_state=None)

    r_get = ctx.request.get(f"{live_server.base_url}/api/portal/monitor-layout")
    assert r_get.status == 401

    r_post = ctx.request.post(
        f"{live_server.base_url}/api/portal/monitor-layout",
        data={"monitors": [], "assignments": {}},
    )
    assert r_post.status == 401

    page = ctx.new_page()
    page.goto(f"{live_server.base_url}/portal", wait_until="domcontentloaded")
    expect(page).to_have_url(re.compile(r"/auth/login"))


# =====================================================================
# F4 Boundaries (6 tests)
# =====================================================================


@pytest.mark.f4
def test_t2_f4_01_all_unassigned_launch_zero_popups_and_notice(live_server, make_context):
    """T2-F4-01: Launching when all 3 programs are '미배치' opens 0 popups and shows a clear guidance message."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    for prog in ("obs_studio", "broadcast_chat", "host_prompter"):
        portal.select_unassigned(prog)
    portal.save_layout()

    result = portal.launch_control_tower(expected_popups=0)
    assert result.open_calls == []
    assert result.windows == []
    expect(portal.status_region).to_be_visible(timeout=5000)
    expect(portal.status_region).to_contain_text(
        re.compile(r"실행할\s*프로그램이\s*없|모두\s*미배치|열\s*프로그램이\s*없")
    )


@pytest.mark.f4
def test_t2_f4_02_partial_popup_blocking_one_of_three_blocked(live_server, make_context):
    """T2-F4-02: Blocking only /host popup opens the other 2 windows and warns specifically about the blocked program."""
    ctx = make_context(
        VirtualEnvConfig(
            screens=PRESET_TRIPLE_MONITORS,
            popup_block_rule={"matchSubstrings": ["/host"]},
        )
    )
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    portal.select_monitor_matching("obs_studio", "Left Studio FHD")
    portal.select_monitor_matching("broadcast_chat", "Center Primary FHD")
    portal.select_monitor_matching("host_prompter", "Right Prompter QHD")
    portal.save_layout()

    result = portal.launch_control_tower(expected_popups=2)
    assert_bounds_match_monitor(result, "/overlay/studio", PRESET_TRIPLE_MONITORS[0])
    assert_bounds_match_monitor(result, "/broadcast", PRESET_TRIPLE_MONITORS[1])
    assert result.find_open_call("/host")["blocked"] is True

    alert_text = portal.monitor_section.inner_text()
    assert re.search(r"차단", alert_text)
    assert re.search(r"호스트|프롬프트", alert_text)
    assert portal.pageerrors == []


@pytest.mark.f4
def test_t2_f4_03_total_popup_blocking_all_blocked(live_server, make_context):
    """T2-F4-03: Blocking all popups opens 0 windows, causes zero pageerrors, and displays popup-allowance guidance."""
    ctx = make_context(
        VirtualEnvConfig(
            screens=PRESET_DUAL_MONITORS,
            popup_block_rule="all",
        )
    )
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    portal.select_monitor_matching("obs_studio", "LG FHD Primary")
    portal.select_monitor_matching("broadcast_chat", "Dell 27 QHD")
    portal.select_unassigned("host_prompter")
    portal.save_layout()

    result = portal.launch_control_tower(expected_popups=0)
    assert len(result.open_calls) == 2
    assert all(c["blocked"] is True for c in result.open_calls)

    panel_text = portal.monitor_section.inner_text()
    assert re.search(r"팝업.*차단|차단.*허용", panel_text)
    assert portal.pageerrors == []


@pytest.mark.f4
def test_t2_f4_04_taskbar_work_area_vs_raw_screen_bounds(live_server, make_context):
    """T2-F4-04: Placement uses taskbar-inset work-area (1980, 40, 2500, 1360), not raw (1920, 0, 2560, 1440)."""
    screens = [
        make_screen("Primary", 0, 0, 1920, 1080, is_primary=True),
        make_screen(
            "Taskbar Inset QHD",
            1920,
            0,
            2560,
            1440,
            avail_left=1980,
            avail_top=40,
            avail_width=2500,
            avail_height=1360,
        ),
    ]
    ctx = make_context(VirtualEnvConfig(screens=screens))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    portal.select_monitor_matching("obs_studio", "Taskbar Inset QHD")
    portal.select_unassigned("broadcast_chat")
    portal.select_unassigned("host_prompter")
    portal.save_layout()

    result = portal.launch_control_tower(expected_popups=1)
    assert_bounds_match_monitor(result, "/overlay/studio", screens[1])


@pytest.mark.f4
def test_t2_f4_05_negative_monitor_coordinate_launch(live_server, make_context):
    """T2-F4-05: Negative work-area coordinates (-1920, -240) pass through to window.open/moveTo/query without clamping."""
    screens = [
        make_screen(
            "Negative Left Upper",
            -1920,
            -240,
            1920,
            1080,
            avail_left=-1920,
            avail_top=-240,
            avail_width=1920,
            avail_height=1040,
        ),
        make_screen("Primary", 0, 0, 1920, 1080, is_primary=True),
    ]
    ctx = make_context(VirtualEnvConfig(screens=screens))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    portal.select_unassigned("obs_studio")
    portal.select_monitor_matching("broadcast_chat", "Negative Left Upper")
    portal.select_unassigned("host_prompter")
    portal.save_layout()

    result = portal.launch_control_tower(expected_popups=1)
    assert_bounds_match_monitor(result, "/broadcast", screens[0])


@pytest.mark.f4
def test_t2_f4_06_synchronous_launch_inside_click_handler(live_server, make_context):
    """T2-F4-06: Every window.open() call runs synchronously inside the click event stack (no prior await)."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_TRIPLE_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    portal.select_monitor_matching("obs_studio", "Left Studio FHD")
    portal.select_monitor_matching("broadcast_chat", "Center Primary FHD")
    portal.select_monitor_matching("host_prompter", "Right Prompter QHD")
    portal.save_layout()

    result = portal.launch_control_tower(expected_popups=3)
    assert len(result.open_calls) == 3
    for call in result.open_calls:
        assert call["calledSynchronouslyInClick"] is True, f"window.open was not synchronous in click handler: {call}"
