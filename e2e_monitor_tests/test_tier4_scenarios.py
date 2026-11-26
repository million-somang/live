"""Tier 4: Real-World Live-Commerce Operator Scenarios (5 end-to-end tests)."""

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
    PRESET_SINGLE_MONITOR,
    PortalPage,
    VirtualEnvConfig,
    assert_bounds_match_monitor,
    build_virtual_screen_init_script,
    make_screen,
)


pytestmark = [pytest.mark.tier4, pytest.mark.f1, pytest.mark.f2, pytest.mark.f3, pytest.mark.f4]


def test_t4_op_01_dual_monitor_host_plus_chat_studio(live_server, make_context):
    """T4-OP-01: Dual-monitor studio laptop + external 27\" QHD: OBS on external, Chat + Host on primary."""
    screens = [
        make_screen(
            "Studio Laptop FHD",
            0,
            0,
            1920,
            1080,
            avail_left=0,
            avail_top=0,
            avail_width=1920,
            avail_height=1032,
            is_primary=True,
            is_internal=True,
            device_pixel_ratio=1.5,
        ),
        make_screen(
            "External 27 QHD",
            1920,
            0,
            2560,
            1440,
            avail_left=1920,
            avail_top=0,
            avail_width=2560,
            avail_height=1400,
            is_primary=False,
            is_internal=False,
            device_pixel_ratio=1.0,
        ),
    ]
    ctx = make_context(VirtualEnvConfig(screens=screens))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    obs_opt = portal.select_monitor_matching("obs_studio", "External 27 QHD")
    chat_opt = portal.select_monitor_matching("broadcast_chat", "Studio Laptop FHD")
    host_opt = portal.select_monitor_matching("host_prompter", "Studio Laptop FHD")
    portal.save_layout()

    portal.page.reload(wait_until="domcontentloaded")
    expect(portal.program_select("obs_studio")).to_have_value(obs_opt["value"], timeout=8000)
    expect(portal.program_select("broadcast_chat")).to_have_value(chat_opt["value"], timeout=8000)
    expect(portal.program_select("host_prompter")).to_have_value(host_opt["value"], timeout=8000)

    result = portal.launch_control_tower(expected_popups=3)
    assert_bounds_match_monitor(result, "/overlay/studio", screens[1])
    assert_bounds_match_monitor(result, "/broadcast", screens[0])
    assert_bounds_match_monitor(result, "/host", screens[0])


def test_t4_op_02_triple_monitor_broadcast_control_tower(server_factory, browser):
    """T4-OP-02: Triple-monitor control tower (vertical left, 4K center primary, FHD right) across cold login."""
    triple_tower = [
        make_screen("Left Vertical Chat", -1080, 0, 1080, 1920, avail_left=-1080, avail_top=0, avail_width=1080, avail_height=1880),
        make_screen("Center 4K Primary", 0, 0, 3840, 2160, avail_left=0, avail_top=0, avail_width=3840, avail_height=2100, is_primary=True),
        make_screen("Right Prompter FHD", 3840, 0, 1920, 1080, avail_left=3840, avail_top=0, avail_width=1920, avail_height=1040),
    ]
    srv = server_factory(None)
    register_studio_user_via_ui(browser, srv.base_url, "tower_pd", "TowerPdPass123!", DEFAULT_INVITE_CODE)

    ctx1 = browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        ctx1.add_init_script(build_virtual_screen_init_script(VirtualEnvConfig(screens=triple_tower)))
        p1 = ctx1.new_page()
        login_studio_user_via_ui(p1, srv.base_url, "tower_pd", "TowerPdPass123!")
        portal1 = PortalPage(p1, srv.base_url)
        portal1.goto()
        portal1.click_discover()
        portal1.select_monitor_matching("broadcast_chat", "Left Vertical Chat")
        portal1.select_monitor_matching("obs_studio", "Center 4K Primary")
        portal1.select_monitor_matching("host_prompter", "Right Prompter FHD")
        portal1.save_layout()
    finally:
        ctx1.close()

    ctx2 = browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        ctx2.add_init_script(build_virtual_screen_init_script(VirtualEnvConfig(screens=triple_tower)))
        p2 = ctx2.new_page()
        login_studio_user_via_ui(p2, srv.base_url, "tower_pd", "TowerPdPass123!")
        portal2 = PortalPage(p2, srv.base_url)
        portal2.goto()
        expect(portal2.program_select("obs_studio")).not_to_have_value("unassigned", timeout=8000)

        result = portal2.launch_control_tower(expected_popups=3)
        assert_bounds_match_monitor(result, "/broadcast", triple_tower[0])
        assert_bounds_match_monitor(result, "/overlay/studio", triple_tower[1])
        assert_bounds_match_monitor(result, "/host", triple_tower[2])
    finally:
        ctx2.close()


def test_t4_op_03_single_monitor_compact_stream_external_obs(live_server, make_context):
    """T4-OP-03: Single laptop monitor with external desktop OBS (OBS=미배치, Chat+Host on Primary)."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_SINGLE_MONITOR))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    portal.select_unassigned("obs_studio")
    portal.select_monitor_matching("broadcast_chat", "Built-in Studio Laptop")
    portal.select_monitor_matching("host_prompter", "Built-in Studio Laptop")
    portal.save_layout()

    result = portal.launch_control_tower(expected_popups=2)
    assert all("/overlay/studio" not in c["url"] for c in result.open_calls)
    assert_bounds_match_monitor(result, "/broadcast", PRESET_SINGLE_MONITOR[0])
    assert_bounds_match_monitor(result, "/host", PRESET_SINGLE_MONITOR[0])


def test_t4_op_04_mid_broadcast_monitor_unplug_and_recovery(live_server, make_context):
    """T4-OP-04: Disconnected Monitor 3 triggers stale badge without data loss; reassigning to Monitor 2 clears stale state and launches."""
    triple = [
        make_screen("Primary Control", 0, 0, 1920, 1080, avail_height=1040, is_primary=True),
        make_screen("Monitor 2 Backup", 1920, 0, 2560, 1440, avail_height=1400),
        make_screen("Monitor 3 Teleprompter", 4480, 0, 1920, 1080, avail_height=1040),
    ]
    ctx1 = make_context(VirtualEnvConfig(screens=triple))
    p1 = PortalPage(ctx1.new_page(), live_server.base_url)
    p1.goto()
    p1.click_discover()
    p1.select_monitor_matching("obs_studio", "Primary Control")
    p1.select_monitor_matching("broadcast_chat", "Monitor 2 Backup")
    m3_opt = p1.select_monitor_matching("host_prompter", "Monitor 3 Teleprompter")
    p1.save_layout()

    # Next shift: Monitor 3 is unplugged
    two_only = [triple[0], triple[1]]
    ctx2 = make_context(VirtualEnvConfig(screens=two_only))
    p2 = PortalPage(ctx2.new_page(), live_server.base_url)
    p2.goto()

    expect(p2.program_select("host_prompter")).to_have_value(m3_opt["value"], timeout=8000)
    assert re.search(r"연결\s*해제|미연결|연결되지|연결\s*끊김|연결\s*끊긴|이전\s*모니터", p2.monitor_section.inner_text(), re.IGNORECASE)

    # Operator recovers by reassigning Host Prompter to Monitor 2 Backup
    m2_opt = p2.select_monitor_matching("host_prompter", "Monitor 2 Backup")
    p2.save_layout()

    p2.page.reload(wait_until="domcontentloaded")
    expect(p2.program_select("host_prompter")).to_have_value(m2_opt["value"], timeout=8000)
    assert not re.search(r"Monitor 3 Teleprompter", p2.monitor_section.inner_text())

    result = p2.launch_control_tower(expected_popups=3)
    assert_bounds_match_monitor(result, "/overlay/studio", two_only[0])
    assert_bounds_match_monitor(result, "/broadcast", two_only[1])
    assert_bounds_match_monitor(result, "/host", two_only[1])


def test_t4_op_05_multi_operator_shift_handover_on_shared_pc(server_factory, browser):
    """T4-OP-05: Shift handover between Operator A and Operator B on the same studio workstation keeps each layout isolated."""
    shared_screens = [
        make_screen("Shift Desk Primary", 0, 0, 1920, 1080, avail_height=1040, is_primary=True),
        make_screen("Shift Studio Ext", 1920, 0, 2560, 1440, avail_height=1400),
        make_screen("Shift Prompter Side", 4480, 0, 1920, 1080, avail_height=1040),
    ]
    srv = server_factory(None)
    register_studio_user_via_ui(browser, srv.base_url, "shift_op_a", "ShiftAPass123!", DEFAULT_INVITE_CODE)
    register_studio_user_via_ui(browser, srv.base_url, "shift_op_b", "ShiftBPass123!", DEFAULT_INVITE_CODE)

    # Operator A configures 3-monitor layout and logs out
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        ctx.add_init_script(build_virtual_screen_init_script(VirtualEnvConfig(screens=shared_screens)))
        page = ctx.new_page()
        login_studio_user_via_ui(page, srv.base_url, "shift_op_a", "ShiftAPass123!")
        portal_a = PortalPage(page, srv.base_url)
        portal_a.goto()
        portal_a.click_discover()
        a_obs = portal_a.select_monitor_matching("obs_studio", "Shift Desk Primary")
        a_chat = portal_a.select_monitor_matching("broadcast_chat", "Shift Studio Ext")
        a_host = portal_a.select_monitor_matching("host_prompter", "Shift Prompter Side")
        portal_a.save_layout()

        page.goto(f"{srv.base_url}/auth/logout", wait_until="domcontentloaded")
        expect(page).to_have_url(re.compile(r"(/$|/auth/login)"))

        # Operator B logs in, configures OBS=미배치, Chat+Host on Ext, saves and launches
        login_studio_user_via_ui(page, srv.base_url, "shift_op_b", "ShiftBPass123!")
        portal_b = PortalPage(page, srv.base_url)
        portal_b.goto()
        portal_b.click_discover()
        portal_b.select_unassigned("obs_studio")
        portal_b.select_monitor_matching("broadcast_chat", "Shift Studio Ext")
        portal_b.select_monitor_matching("host_prompter", "Shift Studio Ext")
        portal_b.save_layout()

        res_b = portal_b.launch_control_tower(expected_popups=2)
        assert_bounds_match_monitor(res_b, "/broadcast", shared_screens[1])
        assert_bounds_match_monitor(res_b, "/host", shared_screens[1])
        for w in res_b.windows:
            w.page.close()

        page.goto(f"{srv.base_url}/auth/logout", wait_until="domcontentloaded")
        expect(page).to_have_url(re.compile(r"(/$|/auth/login)"))
    finally:
        ctx.close()

    # Operator A logs back in using a fresh BrowserContext (empty localStorage) to prove SQLite per-user hydration
    ctx_a2 = browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        ctx_a2.add_init_script(build_virtual_screen_init_script(VirtualEnvConfig(screens=shared_screens)))
        page_a2 = ctx_a2.new_page()
        login_studio_user_via_ui(page_a2, srv.base_url, "shift_op_a", "ShiftAPass123!")
        portal_a2 = PortalPage(page_a2, srv.base_url)
        portal_a2.goto()
        expect(portal_a2.program_select("obs_studio")).to_have_value(a_obs["value"], timeout=8000)
        expect(portal_a2.program_select("broadcast_chat")).to_have_value(a_chat["value"], timeout=8000)
        expect(portal_a2.program_select("host_prompter")).to_have_value(a_host["value"], timeout=8000)

        res_a = portal_a2.launch_control_tower(expected_popups=3)
        assert_bounds_match_monitor(res_a, "/overlay/studio", shared_screens[0])
        assert_bounds_match_monitor(res_a, "/broadcast", shared_screens[1])
        assert_bounds_match_monitor(res_a, "/host", shared_screens[2])
    finally:
        ctx_a2.close()
