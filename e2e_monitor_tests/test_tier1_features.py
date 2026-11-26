"""Tier 1: Feature Coverage Tests (20 tests — 5 per feature F1-F4)."""

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
    make_screen,
)


pytestmark = [pytest.mark.tier1]


# =====================================================================
# F1: Browser Multi-Monitor Discovery (R1)
# =====================================================================


@pytest.mark.f1
def test_t1_f1_01_multi_monitor_discovery_on_click(live_server, make_context):
    """T1-F1-01: Clicking monitor search renders all connected monitors with labels, primary flag, and resolutions."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS, permission_mode="granted"))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()

    portal.click_discover()
    panel_text = portal.monitor_section.inner_text()

    assert "LG FHD Primary" in panel_text
    assert "Dell 27 QHD" in panel_text
    assert "1920" in panel_text and "1080" in panel_text
    assert "2560" in panel_text and "1440" in panel_text
    assert re.search(r"주\s*모니터|Primary|기본", panel_text)
    assert portal.pageerrors == []


@pytest.mark.f1
def test_t1_f1_02_work_area_and_coordinate_display(live_server, make_context):
    """T1-F1-02: Non-zero origin and taskbar-inset work-area bounds are surfaced in monitor discovery."""
    screens = [
        make_screen("Main Screen", 0, 0, 1920, 1080, avail_left=60, avail_top=30, avail_width=1860, avail_height=1050, is_primary=True),
        make_screen("Right Wing", 1920, 120, 2560, 1440, avail_left=1920, avail_top=160, avail_width=2560, avail_height=1360),
    ]
    ctx = make_context(VirtualEnvConfig(screens=screens, permission_mode="granted"))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    panel_text = portal.monitor_section.inner_text()
    assert "Main Screen" in panel_text
    assert "Right Wing" in panel_text
    assert "1860" in panel_text and "1050" in panel_text
    assert "1920" in panel_text and "1360" in panel_text
    assert portal.pageerrors == []


@pytest.mark.f1
def test_t1_f1_03_permission_prompt_to_granted_transition(live_server, make_context):
    """T1-F1-03: Starting in 'prompt' permission and granting on search discovers monitors and updates status."""
    ctx = make_context(
        VirtualEnvConfig(
            screens=PRESET_DUAL_MONITORS,
            permission_mode="prompt",
            prompt_outcome="granted",
        )
    )
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()

    portal.click_discover()
    panel_text = portal.monitor_section.inner_text()

    assert "LG FHD Primary" in panel_text
    assert "Dell 27 QHD" in panel_text
    assert re.search(r"허용|연결|탐지|2\s*대|2개", panel_text)
    assert portal.pageerrors == []


@pytest.mark.f1
def test_t1_f1_04_auto_hydration_when_permission_already_granted(live_server, make_context):
    """T1-F1-04: When 'window-management' is already granted on load, monitors auto-populate for selection."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS, permission_mode="granted"))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()

    expect(portal.monitor_section).to_contain_text("LG FHD Primary", timeout=8000)
    expect(portal.monitor_section).to_contain_text("Dell 27 QHD", timeout=8000)

    obs_opts = [o["text"] for o in portal.get_options("obs_studio")]
    assert any("LG FHD Primary" in t for t in obs_opts)
    assert any("Dell 27 QHD" in t for t in obs_opts)
    assert portal.pageerrors == []


@pytest.mark.f1
def test_t1_f1_05_research_refreshes_monitor_list(live_server, make_context):
    """T1-F1-05: Changing connected displays and clicking search refreshes the discovered monitor list."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS, permission_mode="granted"))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()
    assert "Dell 27 QHD" in portal.monitor_section.inner_text()

    updated_screens = [
        PRESET_DUAL_MONITORS[0],
        make_screen("Samsung Odyssey G9", 1920, 0, 5120, 1440, avail_height=1400),
    ]
    portal.set_virtual_screens(updated_screens)
    portal.click_discover()

    panel_text = portal.monitor_section.inner_text()
    assert "Samsung Odyssey G9" in panel_text
    assert "5120" in panel_text
    assert portal.pageerrors == []


# =====================================================================
# F2: 3-Program Monitor Assignment UI (R2)
# =====================================================================


@pytest.mark.f2
def test_t1_f2_01_all_three_program_selectors_rendered(live_server, make_context):
    """T1-F2-01: /portal renders selectors for OBS 스튜디오, 채팅관리, and 호스트 프롬프트 창 with 미배치 option."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()

    for prog in ("obs_studio", "broadcast_chat", "host_prompter"):
        sel = portal.program_select(prog)
        expect(sel).to_be_visible()
        opts = portal.get_options(prog)
        assert any("미배치" in o["text"] or o["value"] == "unassigned" for o in opts)
    assert portal.pageerrors == []


@pytest.mark.f2
def test_t1_f2_02_distinct_monitor_assignment_per_program(live_server, make_context):
    """T1-F2-02: Each of the 3 programs can be independently assigned to a distinct monitor."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_TRIPLE_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    m1 = portal.select_monitor_matching("obs_studio", "Left Studio FHD")
    m2 = portal.select_monitor_matching("broadcast_chat", "Center Primary FHD")
    m3 = portal.select_monitor_matching("host_prompter", "Right Prompter QHD")

    assert portal.get_selected_value("obs_studio") == m1["value"]
    assert portal.get_selected_value("broadcast_chat") == m2["value"]
    assert portal.get_selected_value("host_prompter") == m3["value"]
    assert len({m1["value"], m2["value"], m3["value"]}) == 3
    assert portal.pageerrors == []


@pytest.mark.f2
def test_t1_f2_03_duplicate_monitor_assignment_allowed(live_server, make_context):
    """T1-F2-03: Assigning multiple programs to the same monitor is allowed without conflict error."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    obs_opt = portal.select_monitor_matching("obs_studio", "Dell 27 QHD")
    chat_opt = portal.select_monitor_matching("broadcast_chat", "Dell 27 QHD")
    host_opt = portal.select_monitor_matching("host_prompter", "LG FHD Primary")

    assert obs_opt["value"] == chat_opt["value"]
    assert portal.get_selected_value("obs_studio") == obs_opt["value"]
    assert portal.get_selected_value("broadcast_chat") == chat_opt["value"]
    assert portal.get_selected_value("host_prompter") == host_opt["value"]
    assert portal.pageerrors == []


@pytest.mark.f2
def test_t1_f2_04_unassigned_selection_per_program(live_server, make_context):
    """T1-F2-04: Mixed assigned and '미배치(열지 않음)' selection is supported."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    portal.select_monitor_matching("obs_studio", "LG FHD Primary")
    portal.select_monitor_matching("broadcast_chat", "Dell 27 QHD")
    unassigned_val = portal.select_unassigned("host_prompter")

    assert portal.get_selected_value("host_prompter") == unassigned_val
    assert "미배치" in portal.get_selected_text("host_prompter")
    assert "LG FHD Primary" in portal.get_selected_text("obs_studio")
    assert "Dell 27 QHD" in portal.get_selected_text("broadcast_chat")
    assert portal.pageerrors == []


@pytest.mark.f2
def test_t1_f2_05_explicit_save_confirmation(live_server, make_context):
    """T1-F2-05: Clicking save submits POST /api/portal/monitor-layout and displays saved confirmation."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    portal.select_monitor_matching("obs_studio", "Dell 27 QHD")
    portal.select_monitor_matching("broadcast_chat", "LG FHD Primary")
    portal.select_unassigned("host_prompter")

    body = portal.save_layout()
    assert body.get("status") == "ok"
    assert body["layout"]["configured"] is True

    expect(portal.status_region).to_be_visible(timeout=5000)
    expect(portal.status_region).to_contain_text(re.compile(r"저장되었습니다|유지됩니다|저장\s*완료"))
    assert portal.pageerrors == []


# =====================================================================
# F3: Layout Persistence & Reconnect Hydration (R2)
# =====================================================================


@pytest.mark.f3
def test_t1_f3_01_persistence_across_hard_page_reload(live_server, make_context):
    """T1-F3-01: Saved monitor assignments hydrate identically after page.reload()."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    obs_sel = portal.select_monitor_matching("obs_studio", "Dell 27 QHD")
    chat_sel = portal.select_monitor_matching("broadcast_chat", "LG FHD Primary")
    host_sel = portal.select_monitor_matching("host_prompter", "Dell 27 QHD")
    portal.save_layout()

    portal.page.reload(wait_until="domcontentloaded")
    expect(portal.program_select("obs_studio")).to_have_value(obs_sel["value"], timeout=8000)
    expect(portal.program_select("broadcast_chat")).to_have_value(chat_sel["value"], timeout=8000)
    expect(portal.program_select("host_prompter")).to_have_value(host_sel["value"], timeout=8000)
    assert portal.pageerrors == []


@pytest.mark.f3
def test_t1_f3_02_persistence_across_server_restart_and_fresh_context(server_factory, browser):
    """T1-F3-02: Saved layout survives full server process restart and hydrates in a fresh browser context."""
    srv1 = server_factory(None)
    data_dir = srv1.data_dir

    # Register and save layout on server instance 1
    register_studio_user_via_ui(browser, srv1.base_url, "restart_op", "RestartPass123!", DEFAULT_INVITE_CODE)

    ctx1 = browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        from e2e_monitor_tests.harness import build_virtual_screen_init_script

        ctx1.add_init_script(build_virtual_screen_init_script(VirtualEnvConfig(screens=PRESET_TRIPLE_MONITORS)))
        p1 = ctx1.new_page()
        login_studio_user_via_ui(p1, srv1.base_url, "restart_op", "RestartPass123!")
        portal1 = PortalPage(p1, srv1.base_url)
        portal1.goto()
        portal1.click_discover()

        obs_sel = portal1.select_monitor_matching("obs_studio", "Left Studio FHD")
        chat_sel = portal1.select_monitor_matching("broadcast_chat", "Center Primary FHD")
        portal1.select_unassigned("host_prompter")
        portal1.save_layout()
    finally:
        ctx1.close()

    # Stop server 1 and restart server 2 against exact same SQLite data_dir
    srv1.stop()
    srv2 = server_factory(data_dir)

    # Fresh browser context (empty localStorage) logs in against restarted server
    ctx2 = browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        from e2e_monitor_tests.harness import build_virtual_screen_init_script

        ctx2.add_init_script(build_virtual_screen_init_script(VirtualEnvConfig(screens=PRESET_TRIPLE_MONITORS)))
        p2 = ctx2.new_page()
        login_studio_user_via_ui(p2, srv2.base_url, "restart_op", "RestartPass123!")
        portal2 = PortalPage(p2, srv2.base_url)
        portal2.goto()

        expect(portal2.program_select("obs_studio")).to_have_value(obs_sel["value"], timeout=8000)
        expect(portal2.program_select("broadcast_chat")).to_have_value(chat_sel["value"], timeout=8000)
        assert "미배치" in portal2.get_selected_text("host_prompter")
        assert portal2.pageerrors == []
    finally:
        ctx2.close()


@pytest.mark.f3
def test_t1_f3_03_persistence_of_duplicate_and_unassigned_combination(live_server, make_context):
    """T1-F3-03: Duplicate monitor + '미배치' mixed combination persists cleanly across reload."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    portal.select_unassigned("obs_studio")
    chat_sel = portal.select_monitor_matching("broadcast_chat", "Dell 27 QHD")
    host_sel = portal.select_monitor_matching("host_prompter", "Dell 27 QHD")
    portal.save_layout()

    portal.page.reload(wait_until="domcontentloaded")
    expect(portal.program_select("broadcast_chat")).to_have_value(chat_sel["value"], timeout=8000)
    expect(portal.program_select("host_prompter")).to_have_value(host_sel["value"], timeout=8000)
    assert "미배치" in portal.get_selected_text("obs_studio")
    assert portal.pageerrors == []


@pytest.mark.f3
def test_t1_f3_04_backend_api_reflects_saved_ui_layout(live_server, make_context):
    """T1-F3-04: GET /api/portal/monitor-layout returns exact monitors and assignments saved via /portal UI."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    obs_sel = portal.select_monitor_matching("obs_studio", "LG FHD Primary")
    chat_sel = portal.select_monitor_matching("broadcast_chat", "Dell 27 QHD")
    portal.select_unassigned("host_prompter")
    portal.save_layout()

    resp = ctx.request.get(f"{live_server.base_url}/api/portal/monitor-layout")
    assert resp.status == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    layout = payload["layout"]
    assert len(layout["monitors"]) == 2
    assert layout["assignments"]["host_prompter"] == "unassigned"

    obs_assign = layout["assignments"]["obs_studio"]
    chat_assign = layout["assignments"]["broadcast_chat"]
    obs_key = obs_assign["monitorKey"] if isinstance(obs_assign, dict) else obs_assign
    chat_key = chat_assign["monitorKey"] if isinstance(chat_assign, dict) else chat_assign
    assert obs_key == obs_sel["value"]
    assert chat_key == chat_sel["value"]


@pytest.mark.f3
def test_t1_f3_05_overwrite_and_resave_updates_persisted_layout(live_server, make_context):
    """T1-F3-05: Overwriting Config A with Config B and saving hydrates Config B on reload."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    # Config A
    portal.select_monitor_matching("obs_studio", "LG FHD Primary")
    portal.select_monitor_matching("broadcast_chat", "LG FHD Primary")
    portal.select_monitor_matching("host_prompter", "LG FHD Primary")
    portal.save_layout()

    # Config B
    obs_b = portal.select_monitor_matching("obs_studio", "Dell 27 QHD")
    portal.select_unassigned("broadcast_chat")
    host_b = portal.select_monitor_matching("host_prompter", "Dell 27 QHD")
    portal.save_layout()

    portal.page.reload(wait_until="domcontentloaded")
    expect(portal.program_select("obs_studio")).to_have_value(obs_b["value"], timeout=8000)
    assert "미배치" in portal.get_selected_text("broadcast_chat")
    expect(portal.program_select("host_prompter")).to_have_value(host_b["value"], timeout=8000)


# =====================================================================
# F4: Auto-Placed & Maximized Launch (R3)
# =====================================================================


@pytest.mark.f4
def test_t1_f4_01_launch_all_three_programs_on_distinct_monitors(live_server, make_context):
    """T1-F4-01: Launching with 3 distinct monitor assignments opens 3 popups at each monitor's work area."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_TRIPLE_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    portal.select_monitor_matching("obs_studio", "Left Studio FHD")
    portal.select_monitor_matching("broadcast_chat", "Center Primary FHD")
    portal.select_monitor_matching("host_prompter", "Right Prompter QHD")
    portal.save_layout()

    result = portal.launch_control_tower(expected_popups=3)
    assert result.portal_pageerrors == []

    assert_bounds_match_monitor(result, "/overlay/studio", PRESET_TRIPLE_MONITORS[0])
    assert_bounds_match_monitor(result, "/broadcast", PRESET_TRIPLE_MONITORS[1])
    assert_bounds_match_monitor(result, "/host", PRESET_TRIPLE_MONITORS[2])


@pytest.mark.f4
def test_t1_f4_02_unassigned_program_is_not_launched(live_server, make_context):
    """T1-F4-02: Programs set to '미배치' are never launched while assigned programs open on target monitors."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    portal.select_unassigned("obs_studio")
    portal.select_monitor_matching("broadcast_chat", "LG FHD Primary")
    portal.select_monitor_matching("host_prompter", "Dell 27 QHD")
    portal.save_layout()

    result = portal.launch_control_tower(expected_popups=2)
    assert all("/overlay/studio" not in c["url"] for c in result.open_calls)
    assert all("/overlay/studio" not in w.url for w in result.windows)

    assert_bounds_match_monitor(result, "/broadcast", PRESET_DUAL_MONITORS[0])
    assert_bounds_match_monitor(result, "/host", PRESET_DUAL_MONITORS[1])


@pytest.mark.f4
def test_t1_f4_03_duplicate_monitor_launch_opens_multiple_windows_on_same_monitor(live_server, make_context):
    """T1-F4-03: Two programs assigned to the same monitor both open targeting that monitor's work area."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_DUAL_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    portal.select_unassigned("obs_studio")
    portal.select_monitor_matching("broadcast_chat", "Dell 27 QHD")
    portal.select_monitor_matching("host_prompter", "Dell 27 QHD")
    portal.save_layout()

    result = portal.launch_control_tower(expected_popups=2)
    assert_bounds_match_monitor(result, "/broadcast", PRESET_DUAL_MONITORS[1])
    assert_bounds_match_monitor(result, "/host", PRESET_DUAL_MONITORS[1])


@pytest.mark.f4
def test_t1_f4_04_maximized_full_work_area_sizing_and_focus(live_server, make_context):
    """T1-F4-04: Launched popups apply full availWidth x availHeight sizing, moveTo/resizeTo, and focus()."""
    screens = [
        make_screen("Left Desk", 0, 0, 1920, 1080, avail_left=0, avail_top=32, avail_width=1920, avail_height=1048, is_primary=True),
        make_screen("Right 4K", 1920, 0, 3840, 2160, avail_left=1920, avail_top=0, avail_width=3840, avail_height=2100),
    ]
    ctx = make_context(VirtualEnvConfig(screens=screens))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    portal.select_monitor_matching("obs_studio", "Right 4K")
    portal.select_monitor_matching("broadcast_chat", "Left Desk")
    portal.select_unassigned("host_prompter")
    portal.save_layout()

    result = portal.launch_control_tower(expected_popups=2)
    assert_bounds_match_monitor(result, "/overlay/studio", screens[1])
    assert_bounds_match_monitor(result, "/broadcast", screens[0])

    # Confirm explicit moveTo, resizeTo, and focus operations occurred with monitor avail* bounds
    for path, target_scr in (("/overlay/studio", screens[1]), ("/broadcast", screens[0])):
        win = result.find_window(path)
        matching_parent_ops = [op for op in result.parent_ops if path in op.get("url", "")]
        all_ops = matching_parent_ops + win.child_ops
        move_ops = [op for op in all_ops if op.get("op") == "moveTo"]
        resize_ops = [op for op in all_ops if op.get("op") == "resizeTo"]
        focus_ops = [op for op in all_ops if op.get("op") == "focus"]
        assert len(move_ops) > 0 and any(
            op["x"] == target_scr["availLeft"] and op["y"] == target_scr["availTop"] for op in move_ops
        ), f"Expected moveTo({target_scr['availLeft']}, {target_scr['availTop']}) for {path}, got {all_ops}"
        assert len(resize_ops) > 0 and any(
            op["w"] == target_scr["availWidth"] and op["h"] == target_scr["availHeight"] for op in resize_ops
        ), f"Expected resizeTo({target_scr['availWidth']}, {target_scr['availHeight']}) for {path}, got {all_ops}"
        assert len(focus_ops) > 0, f"Expected focus() for {path}, got {all_ops}"


@pytest.mark.f4
def test_t1_f4_05_child_window_mode_window_rendering(live_server, make_context):
    """T1-F4-05: All 3 launched child windows preserve mode=window, render real DOM, and emit zero pageerror."""
    ctx = make_context(VirtualEnvConfig(screens=PRESET_TRIPLE_MONITORS))
    portal = PortalPage(ctx.new_page(), live_server.base_url)
    portal.goto()
    portal.click_discover()

    portal.select_monitor_matching("obs_studio", "Left Studio FHD")
    portal.select_monitor_matching("broadcast_chat", "Center Primary FHD")
    portal.select_monitor_matching("host_prompter", "Right Prompter QHD")
    portal.save_layout()

    result = portal.launch_control_tower(expected_popups=3)

    obs_win = result.find_window("/overlay/studio")
    chat_win = result.find_window("/broadcast")
    host_win = result.find_window("/host")

    assert "OBS" in obs_win.page.title()
    assert "관제탑" in chat_win.page.title() or "채팅" in chat_win.page.title()
    assert "프롬프터" in host_win.page.title()

    for w in (obs_win, chat_win, host_win):
        assert w.query.get("mode") == ["window"]
        assert w.pageerrors == []
        expect(w.page.locator("body")).not_to_be_empty()
