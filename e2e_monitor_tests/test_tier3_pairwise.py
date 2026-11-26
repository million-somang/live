"""Tier 3: Cross-Feature Pairwise Tests (6 tests covering all F1-F4 pairs)."""

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
    PRESET_TRIPLE_MONITORS,
    PortalPage,
    VirtualEnvConfig,
    assert_bounds_match_monitor,
    build_virtual_screen_init_script,
    make_screen,
)


pytestmark = [pytest.mark.tier3]


@pytest.mark.f1
@pytest.mark.f2
def test_t3_pw_01_f1_f2_live_hotplug_during_assignment(live_server, make_context):
    """T3-PW-01 (F1+F2): Hotplugging Monitor 3 via screenschange makes it immediately selectable without page reload."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    portal.select_monitor_matching("obs_studio", "LG FHD Primary")
    portal.select_monitor_matching("broadcast_chat", "Dell 27 QHD")

    # Hotplug Monitor 3 while staying on /portal
    portal.set_virtual_screens(PRESET_TRIPLE_MONITORS)
    expect(portal.monitor_section).to_contain_text("Right Prompter QHD", timeout=5000)

    host_opt = portal.select_monitor_matching("host_prompter", "Right Prompter QHD")
    assert portal.get_selected_value("host_prompter") == host_opt["value"]
    assert portal.pageerrors == []


@pytest.mark.f1
@pytest.mark.f3
def test_t3_pw_02_f1_f3_cached_saved_hydration_under_denied_permission(live_server, make_context):
    """T3-PW-02 (F1+F3): Saved multi-monitor assignments still hydrate cleanly when permission is later denied."""
    ctx1 = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS, permission_mode="granted"))
    p1 = PortalPage(ctx1.new_page(), live_server.base_url)
    p1.goto()
    p1.click_discover()
    obs_sel = p1.select_monitor_matching("obs_studio", "Dell 27 QHD")
    chat_sel = p1.select_monitor_matching("broadcast_chat", "LG FHD Primary")
    p1.select_unassigned("host_prompter")
    p1.save_layout()

    # Reopen in fresh context with window-management permission denied
    ctx2 = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS, permission_mode="denied"))
    p2 = PortalPage(ctx2.new_page(), live_server.base_url)
    p2.goto()

    expect(p2.program_select("obs_studio")).to_have_value(obs_sel["value"], timeout=8000)
    expect(p2.program_select("broadcast_chat")).to_have_value(chat_sel["value"], timeout=8000)
    assert "Dell 27 QHD" in p2.get_selected_text("obs_studio")
    expect(p2.monitor_section).to_contain_text(
        re.compile(r"거부|권한\s*거부|차단|denied|notallowed", re.IGNORECASE),
        timeout=5000,
    )
    non_btn_text = p2.monitor_section_non_button_text()
    assert re.search(r"거부|권한\s*거부|차단|denied|notallowed", non_btn_text, re.IGNORECASE)
    assert p2.pageerrors == []


@pytest.mark.f2
@pytest.mark.f3
def test_t3_pw_03_f2_f3_mixed_duplicate_and_unassigned_edit_reload_cycle(live_server, make_context):
    """T3-PW-03 (F2+F3): Editing mixed duplicate + unassigned config across two save/reload cycles persists accurately."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    # Cycle 1: (OBS=M1, Chat=M1, Host=unassigned)
    m1_obs = portal.select_monitor_matching("obs_studio", "LG FHD Primary")
    m1_chat = portal.select_monitor_matching("broadcast_chat", "LG FHD Primary")
    portal.select_unassigned("host_prompter")
    portal.save_layout()

    portal.page.reload(wait_until="domcontentloaded")
    expect(portal.program_select("obs_studio")).to_have_value(m1_obs["value"], timeout=8000)
    expect(portal.program_select("broadcast_chat")).to_have_value(m1_chat["value"], timeout=8000)
    assert "미배치" in portal.get_selected_text("host_prompter")

    # Cycle 2: (OBS=unassigned, Chat=M2, Host=M2)
    portal.select_unassigned("obs_studio")
    m2_chat = portal.select_monitor_matching("broadcast_chat", "Dell 27 QHD")
    m2_host = portal.select_monitor_matching("host_prompter", "Dell 27 QHD")
    portal.save_layout()

    portal.page.reload(wait_until="domcontentloaded")
    assert "미배치" in portal.get_selected_text("obs_studio")
    expect(portal.program_select("broadcast_chat")).to_have_value(m2_chat["value"], timeout=8000)
    expect(portal.program_select("host_prompter")).to_have_value(m2_host["value"], timeout=8000)


@pytest.mark.f2
@pytest.mark.f4
def test_t3_pw_04_f2_f4_mixed_duplicate_and_unassigned_launch(live_server, make_context):
    """T3-PW-04 (F2+F4): Launching (OBS=미배치, Chat=M2, Host=M2) opens only Chat and Host on Monitor 2 work-area."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    portal.select_unassigned("obs_studio")
    portal.select_monitor_matching("broadcast_chat", "Dell 27 QHD")
    portal.select_monitor_matching("host_prompter", "Dell 27 QHD")
    portal.save_layout()

    result = portal.launch_control_tower(expected_popups=2)
    assert all("/overlay/studio" not in c["url"] for c in result.open_calls)
    assert_bounds_match_monitor(result, "/broadcast", PRESET_DUAL_MONITORS[1])
    assert_bounds_match_monitor(result, "/host", PRESET_DUAL_MONITORS[1])


@pytest.mark.f3
@pytest.mark.f4
def test_t3_pw_05_f3_f4_cold_login_direct_launch_without_research(server_factory, browser):
    """T3-PW-05 (F3+F4): After cold login in a fresh context, clicking launch immediately uses persisted bounds without re-searching."""
    srv = server_factory(None)
    register_studio_user_via_ui(browser, srv.base_url, "cold_op", "ColdOpPass123!", DEFAULT_INVITE_CODE)

    ctx1 = browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        ctx1.add_init_script(build_virtual_screen_init_script(VirtualEnvConfig(screens=PRESET_TRIPLE_MONITORS)))
        p1 = ctx1.new_page()
        login_studio_user_via_ui(p1, srv.base_url, "cold_op", "ColdOpPass123!")
        portal1 = PortalPage(p1, srv.base_url)
        portal1.goto()
        portal1.click_discover()
        portal1.select_monitor_matching("obs_studio", "Left Studio FHD")
        portal1.select_monitor_matching("broadcast_chat", "Center Primary FHD")
        portal1.select_monitor_matching("host_prompter", "Right Prompter QHD")
        portal1.save_layout()
    finally:
        ctx1.close()

    # Cold context (empty localStorage) logs in and clicks launch directly
    ctx2 = browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        ctx2.add_init_script(build_virtual_screen_init_script(VirtualEnvConfig(screens=PRESET_TRIPLE_MONITORS)))
        p2 = ctx2.new_page()
        login_studio_user_via_ui(p2, srv.base_url, "cold_op", "ColdOpPass123!")
        portal2 = PortalPage(p2, srv.base_url)
        portal2.goto()

        # Wait for hydrated assignments before launching (no manual discover click)
        expect(portal2.program_select("obs_studio")).not_to_have_value("unassigned", timeout=8000)
        result = portal2.launch_control_tower(expected_popups=3)

        assert_bounds_match_monitor(result, "/overlay/studio", PRESET_TRIPLE_MONITORS[0])
        assert_bounds_match_monitor(result, "/broadcast", PRESET_TRIPLE_MONITORS[1])
        assert_bounds_match_monitor(result, "/host", PRESET_TRIPLE_MONITORS[2])
    finally:
        ctx2.close()


@pytest.mark.f1
@pytest.mark.f4
def test_t3_pw_06_f1_f4_monitor_geometry_change_updates_launch_bounds(live_server, make_context):
    """T3-PW-06 (F1+F4): Live screenschange resizing an assigned monitor updates subsequent launch window bounds."""
    initial_screens = [
        make_screen("Primary FHD", 0, 0, 1920, 1080, avail_height=1040, is_primary=True),
        make_screen("Ext Display", 1920, 0, 1920, 1080, avail_left=1920, avail_top=0, avail_width=1920, avail_height=1040),
    ]
    ctx = make_context(VirtualEnvConfig(screens=initial_screens))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    portal.select_monitor_matching("obs_studio", "Ext Display")
    portal.select_unassigned("broadcast_chat")
    portal.select_unassigned("host_prompter")
    portal.save_layout()

    # Monitor 2 changes resolution / work area via screenschange
    resized_ext = make_screen(
        "Ext Display",
        1920,
        0,
        2560,
        1440,
        avail_left=1920,
        avail_top=0,
        avail_width=2560,
        avail_height=1400,
    )
    portal.set_virtual_screens([initial_screens[0], resized_ext])
    expect(portal.monitor_section).to_contain_text("2560", timeout=5000)

    result = portal.launch_control_tower(expected_popups=1)
    assert_bounds_match_monitor(result, "/overlay/studio", resized_ext)
