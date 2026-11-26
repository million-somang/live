"""Browser multi-monitor virtualization & opaque-box Portal Page Object Model."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import BrowserContext, Locator, Page, expect


def make_screen(
    label: str,
    left: int,
    top: int,
    width: int,
    height: int,
    *,
    avail_left: int | None = None,
    avail_top: int | None = None,
    avail_width: int | None = None,
    avail_height: int | None = None,
    is_primary: bool = False,
    is_internal: bool = False,
    device_pixel_ratio: float = 1.0,
) -> dict[str, Any]:
    """Build a W3C ScreenDetailed-compatible monitor descriptor."""
    return {
        "label": label,
        "left": left,
        "top": top,
        "width": width,
        "height": height,
        "availLeft": left if avail_left is None else avail_left,
        "availTop": top if avail_top is None else avail_top,
        "availWidth": width if avail_width is None else avail_width,
        "availHeight": (height - 40) if avail_height is None else avail_height,
        "isPrimary": is_primary,
        "isInternal": is_internal,
        "devicePixelRatio": device_pixel_ratio,
        "colorDepth": 24,
        "pixelDepth": 24,
        "orientation": {
            "type": "portrait-primary" if height > width else "landscape-primary",
            "angle": 0,
        },
    }


PRESET_SINGLE_MONITOR: list[dict[str, Any]] = [
    make_screen(
        "Built-in Studio Laptop",
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
    )
]

PRESET_DUAL_MONITORS: list[dict[str, Any]] = [
    make_screen(
        "LG FHD Primary",
        0,
        0,
        1920,
        1080,
        avail_left=0,
        avail_top=0,
        avail_width=1920,
        avail_height=1040,
        is_primary=True,
        is_internal=True,
        device_pixel_ratio=1.0,
    ),
    make_screen(
        "Dell 27 QHD",
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

PRESET_TRIPLE_MONITORS: list[dict[str, Any]] = [
    make_screen(
        "Left Studio FHD",
        -1920,
        0,
        1920,
        1080,
        avail_left=-1920,
        avail_top=0,
        avail_width=1920,
        avail_height=1040,
        is_primary=False,
        is_internal=False,
        device_pixel_ratio=1.0,
    ),
    make_screen(
        "Center Primary FHD",
        0,
        0,
        1920,
        1080,
        avail_left=0,
        avail_top=0,
        avail_width=1920,
        avail_height=1040,
        is_primary=True,
        is_internal=True,
        device_pixel_ratio=1.0,
    ),
    make_screen(
        "Right Prompter QHD",
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

PRESET_FIVE_MONITORS: list[dict[str, Any]] = [
    make_screen("Laptop Internal", 0, 0, 1920, 1080, avail_height=1032, is_primary=True, is_internal=True, device_pixel_ratio=1.25),
    make_screen("Studio QHD", 1920, 0, 2560, 1440, avail_height=1400),
    make_screen("Broadcast 4K", 4480, 0, 3840, 2160, avail_height=2100, device_pixel_ratio=2.0),
    make_screen("Chat Portrait", -1080, 0, 1080, 1920, avail_height=1880),
    make_screen("Top Aux HD", 0, -1080, 1920, 1080, avail_top=-1080, avail_height=1040),
]


@dataclass
class VirtualEnvConfig:
    """Configuration for browser-side W3C Window Management virtualization."""

    screens: list[dict[str, Any]] = field(default_factory=lambda: list(PRESET_DUAL_MONITORS))
    permission_mode: str = "granted"  # 'granted' | 'prompt' | 'denied' | 'unsupported' | 'insecure'
    prompt_outcome: str = "granted"   # 'granted' | 'denied'
    fallback_screen: dict[str, Any] | None = None
    popup_block_rule: str | dict[str, Any] = "none"  # 'none' | 'all' | {'matchSubstrings': [...]}
    current_screen_index: int = 0


def build_virtual_screen_init_script(config: VirtualEnvConfig) -> str:
    """Return JS injected via context.add_init_script to virtualize W3C multi-monitor APIs and window telemetry."""
    payload = {
        "screens": config.screens,
        "permissionMode": config.permission_mode,
        "promptOutcome": config.prompt_outcome,
        "fallbackScreen": config.fallback_screen,
        "popupBlockRule": config.popup_block_rule,
        "currentScreenIndex": config.current_screen_index,
    }
    cfg_json = json.dumps(payload, ensure_ascii=False)
    return f"""
(() => {{
  const INITIAL_CFG = {cfg_json};

  function createSimpleEventTarget() {{
    const listeners = new Map();
    return {{
      addEventListener(type, fn) {{
        if (typeof fn !== 'function') return;
        if (!listeners.has(type)) listeners.set(type, new Set());
        listeners.get(type).add(fn);
      }},
      removeEventListener(type, fn) {{
        const set = listeners.get(type);
        if (set) set.delete(fn);
      }},
      dispatchEvent(evt) {{
        const type = evt && evt.type ? evt.type : '';
        const propHandler = this['on' + type];
        if (typeof propHandler === 'function') {{
          try {{ propHandler.call(this, evt); }} catch (e) {{}}
        }}
        const set = listeners.get(type);
        if (set) {{
          for (const fn of Array.from(set)) {{
            try {{ fn.call(this, evt); }} catch (e) {{}}
          }}
        }}
        return true;
      }},
    }};
  }}

  function buildScreenDetailed(spec) {{
    const target = createSimpleEventTarget();
    return Object.assign(target, {{
      label: String(spec.label || ''),
      left: Number(spec.left || 0),
      top: Number(spec.top || 0),
      width: Number(spec.width || 1920),
      height: Number(spec.height || 1080),
      availLeft: Number(spec.availLeft !== undefined ? spec.availLeft : (spec.left || 0)),
      availTop: Number(spec.availTop !== undefined ? spec.availTop : (spec.top || 0)),
      availWidth: Number(spec.availWidth !== undefined ? spec.availWidth : (spec.width || 1920)),
      availHeight: Number(spec.availHeight !== undefined ? spec.availHeight : (spec.height || 1040)),
      isPrimary: Boolean(spec.isPrimary),
      isInternal: Boolean(spec.isInternal),
      devicePixelRatio: Number(spec.devicePixelRatio || 1),
      colorDepth: Number(spec.colorDepth || 24),
      pixelDepth: Number(spec.pixelDepth || 24),
      orientation: Object.assign({{}}, spec.orientation || {{ type: 'landscape-primary', angle: 0 }}),
      onchange: null,
    }});
  }}

  let currentSpecs = Array.isArray(INITIAL_CFG.screens) ? INITIAL_CFG.screens.slice() : [];
  let currentIdx = Number(INITIAL_CFG.currentScreenIndex || 0);
  let permMode = INITIAL_CFG.permissionMode || 'granted';
  let permState = (permMode === 'granted' || permMode === 'prompt' || permMode === 'denied')
    ? permMode
    : 'denied';
  let popupBlockRule = INITIAL_CFG.popupBlockRule || 'none';

  const activeScreenDetails = [];
  const activePermStatuses = [];

  function applyFallbackScreen(spec) {{
    if (!spec) return;
    const props = {{
      left: Number(spec.left || 0),
      top: Number(spec.top || 0),
      width: Number(spec.width || 1920),
      height: Number(spec.height || 1080),
      availLeft: Number(spec.availLeft !== undefined ? spec.availLeft : (spec.left || 0)),
      availTop: Number(spec.availTop !== undefined ? spec.availTop : (spec.top || 0)),
      availWidth: Number(spec.availWidth !== undefined ? spec.availWidth : (spec.width || 1920)),
      availHeight: Number(spec.availHeight !== undefined ? spec.availHeight : (spec.height || 1040)),
    }};
    for (const [k, v] of Object.entries(props)) {{
      try {{
        Object.defineProperty(window.screen, k, {{ configurable: true, get: () => v }});
      }} catch (e) {{}}
    }}
    if (spec.devicePixelRatio !== undefined) {{
      try {{
        Object.defineProperty(window, 'devicePixelRatio', {{
          configurable: true,
          get: () => Number(spec.devicePixelRatio),
        }});
      }} catch (e) {{}}
    }}
  }}

  applyFallbackScreen(INITIAL_CFG.fallbackScreen);

  // XSS detection trap
  window.__xssTriggered = false;
  window.__alertMessages = [];
  window.alert = function(msg) {{
    window.__xssTriggered = true;
    window.__alertMessages.push(String(msg));
  }};

  // Synchronous click + window operation telemetry
  const telemetry = {{
    openCalls: [],
    parentWindowOps: [],
    childWindowOps: [],
    getScreenDetailsCalls: 0,
  }};
  window.__testWindowTelemetry = telemetry;

  let syncHandlerDepth = 0;
  const origAddEventListener = EventTarget.prototype.addEventListener;
  const origRemoveEventListener = EventTarget.prototype.removeEventListener;
  const clickListenerWrappers = new WeakMap();

  function getWrappedClickListener(listener) {{
    if (!listener) return listener;
    if (typeof listener !== 'function' && (typeof listener !== 'object' || typeof listener.handleEvent !== 'function')) {{
      return listener;
    }}
    let wrapped = clickListenerWrappers.get(listener);
    if (!wrapped) {{
      if (typeof listener === 'function') {{
        wrapped = function(...args) {{
          syncHandlerDepth += 1;
          try {{
            return listener.apply(this, args);
          }} finally {{
            syncHandlerDepth -= 1;
          }}
        }};
      }} else {{
        wrapped = {{
          handleEvent(ev) {{
            syncHandlerDepth += 1;
            try {{
              return listener.handleEvent(ev);
            }} finally {{
              syncHandlerDepth -= 1;
            }}
          }},
        }};
      }}
      clickListenerWrappers.set(listener, wrapped);
    }}
    return wrapped;
  }}

  origAddEventListener.call(document, 'click', (e) => {{
    const path = typeof e.composedPath === 'function' ? e.composedPath() : [];
    for (const node of path) {{
      if (node && typeof node.onclick === 'function' && !node.onclick.__isSyncClickWrapped) {{
        const origOnclick = node.onclick;
        const wrappedOnclick = function(ev) {{
          syncHandlerDepth += 1;
          try {{
            return origOnclick.call(this, ev);
          }} finally {{
            syncHandlerDepth -= 1;
          }}
        }};
        wrappedOnclick.__isSyncClickWrapped = true;
        try {{
          node.onclick = wrappedOnclick;
        }} catch (err) {{}}
      }}
    }}
  }}, true);

  EventTarget.prototype.addEventListener = function(type, listener, options) {{
    if (type === 'click') {{
      return origAddEventListener.call(this, type, getWrappedClickListener(listener), options);
    }}
    return origAddEventListener.call(this, type, listener, options);
  }};

  EventTarget.prototype.removeEventListener = function(type, listener, options) {{
    if (type === 'click' && listener && clickListenerWrappers.has(listener)) {{
      return origRemoveEventListener.call(this, type, clickListenerWrappers.get(listener), options);
    }}
    return origRemoveEventListener.call(this, type, listener, options);
  }};

  function parseFeaturesString(featuresStr) {{
    const out = {{ raw: String(featuresStr || '') }};
    if (!featuresStr) return out;
    for (const part of String(featuresStr).split(',')) {{
      const eq = part.indexOf('=');
      if (eq === -1) continue;
      const k = part.slice(0, eq).trim().toLowerCase();
      const v = part.slice(eq + 1).trim();
      if (/^-?\\d+(\\.\\d+)?$/.test(v)) {{
        out[k] = Number(v);
      }} else if (v === 'yes' || v === 'true') {{
        out[k] = true;
      }} else if (v === 'no' || v === 'false') {{
        out[k] = false;
      }} else {{
        out[k] = v;
      }}
    }}
    return out;
  }}

  function shouldBlockPopup(urlStr) {{
    const url = String(urlStr || '');
    if (popupBlockRule === 'all') return true;
    if (popupBlockRule && typeof popupBlockRule === 'object' && Array.isArray(popupBlockRule.matchSubstrings)) {{
      return popupBlockRule.matchSubstrings.some((sub) => url.includes(String(sub)));
    }}
    return false;
  }}

  const origOpen = window.open.bind(window);
  window.open = function(url, target, features) {{
    const inClickEvent = Boolean(syncHandlerDepth > 0);
    const blocked = shouldBlockPopup(url);
    const rec = {{
      url: String(url || ''),
      target: String(target || ''),
      features: String(features || ''),
      parsedFeatures: parseFeaturesString(features),
      calledSynchronouslyInClick: inClickEvent,
      blocked,
      timestamp: Date.now(),
    }};
    telemetry.openCalls.push(rec);
    if (blocked) {{
      return null;
    }}
    const win = origOpen(url, target, features);
    if (win) {{
      try {{
        const origMoveTo = typeof win.moveTo === 'function' ? win.moveTo.bind(win) : null;
        const origResizeTo = typeof win.resizeTo === 'function' ? win.resizeTo.bind(win) : null;
        const origFocus = typeof win.focus === 'function' ? win.focus.bind(win) : null;
        win.moveTo = function(x, y) {{
          telemetry.parentWindowOps.push({{ op: 'moveTo', url: rec.url, x: Number(x), y: Number(y) }});
          if (origMoveTo) try {{ return origMoveTo(x, y); }} catch (e) {{}}
        }};
        win.resizeTo = function(w, h) {{
          telemetry.parentWindowOps.push({{ op: 'resizeTo', url: rec.url, w: Number(w), h: Number(h) }});
          if (origResizeTo) try {{ return origResizeTo(w, h); }} catch (e) {{}}
        }};
        win.focus = function() {{
          telemetry.parentWindowOps.push({{ op: 'focus', url: rec.url }});
          if (origFocus) try {{ return origFocus(); }} catch (e) {{}}
        }};
      }} catch (e) {{}}
    }}
    return win;
  }};

  // Wrap child-side Window.prototype.moveTo / resizeTo / focus as well
  const protoMoveTo = Window.prototype.moveTo;
  const protoResizeTo = Window.prototype.resizeTo;
  const protoFocus = Window.prototype.focus;
  Window.prototype.moveTo = function(x, y) {{
    telemetry.childWindowOps.push({{ op: 'moveTo', href: String(location.href || ''), x: Number(x), y: Number(y) }});
    if (protoMoveTo) try {{ return protoMoveTo.call(this, x, y); }} catch (e) {{}}
  }};
  Window.prototype.resizeTo = function(w, h) {{
    telemetry.childWindowOps.push({{ op: 'resizeTo', href: String(location.href || ''), w: Number(w), h: Number(h) }});
    if (protoResizeTo) try {{ return protoResizeTo.call(this, w, h); }} catch (e) {{}}
  }};
  Window.prototype.focus = function() {{
    telemetry.childWindowOps.push({{ op: 'focus', href: String(location.href || '') }});
    if (protoFocus) try {{ return protoFocus.call(this); }} catch (e) {{}}
  }};

  // Permission virtualization
  if (navigator.permissions && typeof navigator.permissions.query === 'function') {{
    const origPermQuery = navigator.permissions.query.bind(navigator.permissions);
    const wrappedPermQuery = async function(desc) {{
      if (desc && desc.name === 'window-management') {{
        const statusObj = createSimpleEventTarget();
        Object.defineProperty(statusObj, 'name', {{ get: () => 'window-management' }});
        Object.defineProperty(statusObj, 'state', {{ get: () => permState }});
        statusObj.onchange = null;
        activePermStatuses.push(statusObj);
        return statusObj;
      }}
      return origPermQuery(desc);
    }};
    try {{
      Object.defineProperty(navigator.permissions, 'query', {{
        configurable: true,
        value: wrappedPermQuery,
      }});
    }} catch (e) {{
      try {{ Permissions.prototype.query = wrappedPermQuery; }} catch (_) {{}}
    }}
  }}

  // Insecure / unsupported / W3C ScreenDetails modes
  if (permMode === 'insecure') {{
    try {{
      Object.defineProperty(window, 'isSecureContext', {{
        configurable: true,
        get: () => false,
      }});
    }} catch (e) {{}}
  }}

  if (permMode === 'unsupported' || permMode === 'insecure') {{
    try {{ delete window.getScreenDetails; }} catch (e) {{}}
    try {{ delete Window.prototype.getScreenDetails; }} catch (e) {{}}
    if ('getScreenDetails' in window) {{
      try {{
        Object.defineProperty(window, 'getScreenDetails', {{
          configurable: true,
          value: undefined,
          writable: true,
        }});
      }} catch (e) {{}}
    }}
  }} else {{
    function createScreenDetailsInstance() {{
      const details = createSimpleEventTarget();
      let builtScreens = currentSpecs.map(buildScreenDetailed);
      let curIdx = Math.min(Math.max(0, currentIdx), Math.max(0, builtScreens.length - 1));
      details.onscreenschange = null;
      details.oncurrentscreenchange = null;
      Object.defineProperty(details, 'screens', {{
        configurable: true,
        get: () => builtScreens,
      }});
      Object.defineProperty(details, 'currentScreen', {{
        configurable: true,
        get: () => builtScreens[curIdx] || builtScreens[0] || null,
      }});
      details._updateSpecs = (newSpecs, nextIdx) => {{
        builtScreens = newSpecs.map(buildScreenDetailed);
        curIdx = Math.min(Math.max(0, nextIdx), Math.max(0, builtScreens.length - 1));
        details.dispatchEvent(new Event('screenschange'));
      }};
      activeScreenDetails.push(details);
      return details;
    }}

    window.getScreenDetails = async function() {{
      telemetry.getScreenDetailsCalls += 1;
      if (permState === 'prompt') {{
        permState = INITIAL_CFG.promptOutcome === 'denied' ? 'denied' : 'granted';
        for (const st of activePermStatuses) {{
          st.dispatchEvent(new Event('change'));
        }}
      }}
      if (permState === 'denied') {{
        throw new DOMException('Window management permission denied', 'NotAllowedError');
      }}
      return createScreenDetailsInstance();
    }};
  }}

  window.__testScreenControl = {{
    setScreens(newSpecs, newCurrentIdx = 0) {{
      currentSpecs = Array.isArray(newSpecs) ? newSpecs.slice() : [];
      currentIdx = Number(newCurrentIdx || 0);
      for (const inst of activeScreenDetails) {{
        inst._updateSpecs(currentSpecs, currentIdx);
      }}
    }},
    setPermission(newState) {{
      permState = String(newState);
      for (const st of activePermStatuses) {{
        st.dispatchEvent(new Event('change'));
      }}
    }},
    setPopupBlockRule(rule) {{
      popupBlockRule = rule;
    }},
    getTelemetry() {{
      return JSON.parse(JSON.stringify(telemetry));
    }},
    clearTelemetry() {{
      telemetry.openCalls = [];
      telemetry.parentWindowOps = [];
      telemetry.childWindowOps = [];
      telemetry.getScreenDetailsCalls = 0;
    }},
  }};
}})();
"""


@dataclass
class LaunchedWindowInfo:
    page: Page
    url: str
    path: str
    query: dict[str, list[str]]
    pageerrors: list[str]
    child_ops: list[dict[str, Any]]


@dataclass
class LaunchResult:
    windows: list[LaunchedWindowInfo]
    open_calls: list[dict[str, Any]]
    parent_ops: list[dict[str, Any]]
    portal_pageerrors: list[str]

    def find_open_call(self, path_substring: str) -> dict[str, Any]:
        for call in self.open_calls:
            if path_substring in call.get("url", ""):
                return call
        raise AssertionError(f"Expected window.open call containing {path_substring!r}, got {self.open_calls}")

    def find_window(self, path_substring: str) -> LaunchedWindowInfo:
        for win in self.windows:
            if path_substring in win.path or path_substring in win.url:
                return win
        raise AssertionError(f"Expected popup window for {path_substring!r}, got {[w.url for w in self.windows]}")


class PortalPage:
    """Opaque-box Page Object Model for /portal multi-monitor discovery, assignment, and launch."""

    PROGRAM_LABELS = {
        "obs_studio": re.compile(r"OBS\s*스튜디오"),
        "broadcast_chat": re.compile(r"채팅\s*관리"),
        "host_prompter": re.compile(r"호스트\s*프롬프트"),
    }

    def __init__(self, page: Page, base_url: str) -> None:
        self.page = page
        self.base_url = base_url
        self.pageerrors: list[str] = []
        self._pageerror_handler = lambda err: self.pageerrors.append(str(err))
        self.page.on("pageerror", self._pageerror_handler)

    def goto(self) -> None:
        self.page.goto(f"{self.base_url}/portal", wait_until="domcontentloaded")
        expect(self.launch_button).to_be_visible(timeout=15000)
        expect(self.discover_button).to_be_visible(timeout=15000)

    @property
    def monitor_section(self) -> Locator:
        return self.page.get_by_role("region", name=re.compile(r"모니터")).first

    @property
    def detected_monitors_region(self) -> Locator:
        return self.page.get_by_role("region", name=re.compile(r"검색된\s*모니터\s*목록"))

    @property
    def discover_button(self) -> Locator:
        return self.page.get_by_role("button", name=re.compile(r"모니터.*(검색|조회|탐지|갱신|새로고침)"))

    @property
    def save_button(self) -> Locator:
        return self.page.get_by_role("button", name=re.compile(r"(배치|설정).*저장|저장"))

    @property
    def launch_button(self) -> Locator:
        return self.page.locator("a, button").filter(has_text=re.compile(r"실시간\s*방송\s*관제탑\s*열기")).first

    @property
    def status_region(self) -> Locator:
        return self.page.get_by_role("status")

    @property
    def alert_region(self) -> Locator:
        return self.page.get_by_role("alert")

    def monitor_section_non_button_text(self) -> str:
        """Return visible text inside monitor_section excluding <button> labels."""
        return self.monitor_section.evaluate(
            """(el) => {
                const parts = [];
                function walk(node) {
                    if (!node) return;
                    if (node.nodeType === Node.TEXT_NODE) {
                        const t = (node.nodeValue || '').trim();
                        if (t) parts.push(t);
                        return;
                    }
                    if (node.nodeType !== Node.ELEMENT_NODE) return;
                    const tag = (node.tagName || '').toUpperCase();
                    if (tag === 'BUTTON' || tag === 'SCRIPT' || tag === 'STYLE') return;
                    const style = window.getComputedStyle(node);
                    if (style && (style.display === 'none' || style.visibility === 'hidden')) return;
                    for (const child of node.childNodes) {
                        walk(child);
                    }
                }
                walk(el);
                return parts.join(' ');
            }"""
        )

    def program_select(self, program: str) -> Locator:
        label_re = self.PROGRAM_LABELS[program]
        by_label = self.page.get_by_label(label_re)
        if by_label.count() > 0:
            return by_label.first
        return (
            self.page.get_by_text(label_re)
            .first
            .locator("xpath=ancestor::*[self::div or self::tr or self::section or self::li][.//select][1]//select")
            .first
        )

    def click_discover(self, settle_ms: int = 350) -> None:
        self.discover_button.click()
        self.page.wait_for_timeout(settle_ms)

    def get_options(self, program: str) -> list[dict[str, str]]:
        sel = self.program_select(program)
        return sel.evaluate(
            """(el) => Array.from(el.options).map((o) => ({
                value: o.value,
                text: (o.textContent || '').trim()
            }))"""
        )

    def get_selected_value(self, program: str) -> str:
        return self.program_select(program).input_value()

    def get_selected_text(self, program: str) -> str:
        return self.program_select(program).evaluate(
            """(el) => {
                const opt = el.options[el.selectedIndex];
                return opt ? (opt.textContent || '').trim() : '';
            }"""
        )

    def select_unassigned(self, program: str) -> str:
        options = self.get_options(program)
        for opt in options:
            if "미배치" in opt["text"] or opt["value"].lower() == "unassigned":
                self.program_select(program).select_option(value=opt["value"])
                return opt["value"]
        raise AssertionError(f"No unassigned option found for {program}: {options}")

    def select_monitor_matching(self, program: str, pattern: str | re.Pattern[str], occurrence: int = 0) -> dict[str, str]:
        regex = re.compile(pattern) if isinstance(pattern, str) else pattern
        options = self.get_options(program)
        matches = [
            opt
            for opt in options
            if opt["value"].lower() != "unassigned"
            and "미배치" not in opt["text"]
            and regex.search(opt["text"])
        ]
        if len(matches) <= occurrence:
            raise AssertionError(
                f"Expected at least {occurrence + 1} options matching {regex.pattern!r} for {program}, got {options}"
            )
        chosen = matches[occurrence]
        self.program_select(program).select_option(value=chosen["value"])
        return chosen

    def save_layout(self) -> dict[str, Any]:
        with self.page.expect_response(
            lambda resp: "/api/portal/monitor-layout" in resp.url and resp.request.method == "POST",
            timeout=10000,
        ) as resp_info:
            self.save_button.click()
        resp = resp_info.value
        assert resp.status == 200, f"Expected 200 from POST /api/portal/monitor-layout, got {resp.status}: {resp.text()}"
        return resp.json()

    def set_virtual_screens(self, screens: list[dict[str, Any]], current_idx: int = 0, settle_ms: int = 300) -> None:
        self.page.evaluate(
            "({ screens, idx }) => window.__testScreenControl.setScreens(screens, idx)",
            {"screens": screens, "idx": current_idx},
        )
        self.page.wait_for_timeout(settle_ms)

    def set_popup_block_rule(self, rule: str | dict[str, Any]) -> None:
        self.page.evaluate("(r) => window.__testScreenControl.setPopupBlockRule(r)", rule)

    def clear_telemetry(self) -> None:
        self.page.evaluate("() => window.__testScreenControl.clearTelemetry()")

    def get_telemetry(self) -> dict[str, Any]:
        return self.page.evaluate("() => window.__testScreenControl.getTelemetry()")

    def launch_control_tower(self, expected_popups: int | None = None, timeout_ms: int = 8000) -> LaunchResult:
        ctx: BrowserContext = self.page.context
        opened_pages: list[Page] = []
        popup_errors: dict[int, list[str]] = {}

        def _on_page(new_page: Page) -> None:
            errs: list[str] = []
            popup_errors[id(new_page)] = errs
            new_page.on("pageerror", lambda e: errs.append(str(e)))
            opened_pages.append(new_page)

        self.clear_telemetry()
        ctx.on("page", _on_page)
        try:
            self.launch_button.click()
            deadline = time.monotonic() + (timeout_ms / 1000.0)
            if expected_popups is not None and expected_popups > 0:
                while len(opened_pages) < expected_popups and time.monotonic() < deadline:
                    self.page.wait_for_timeout(100)
                self.page.wait_for_timeout(350)
            else:
                self.page.wait_for_timeout(450)
        finally:
            ctx.remove_listener("page", _on_page)

        if expected_popups is not None:
            assert len(opened_pages) == expected_popups, (
                f"Expected {expected_popups} popup windows, opened {len(opened_pages)}: {[p.url for p in opened_pages]}"
            )

        windows: list[LaunchedWindowInfo] = []
        for popup in opened_pages:
            popup.wait_for_load_state("domcontentloaded", timeout=12000)
            if popup.url == "about:blank":
                expect(popup).to_have_url(re.compile(r"^https?://"), timeout=10000)
                popup.wait_for_load_state("domcontentloaded", timeout=10000)
            parsed = urlparse(popup.url)
            child_tel = popup.evaluate("() => window.__testWindowTelemetry ? window.__testWindowTelemetry.childWindowOps : []")
            windows.append(
                LaunchedWindowInfo(
                    page=popup,
                    url=popup.url,
                    path=parsed.path,
                    query=parse_qs(parsed.query),
                    pageerrors=popup_errors.get(id(popup), []),
                    child_ops=child_tel,
                )
            )

        tel = self.get_telemetry()
        return LaunchResult(
            windows=windows,
            open_calls=tel.get("openCalls", []),
            parent_ops=tel.get("parentWindowOps", []),
            portal_pageerrors=list(self.pageerrors),
        )


def assert_bounds_match_monitor(
    launch_result: LaunchResult,
    path_substring: str,
    monitor: dict[str, Any],
    *,
    require_sync_click: bool = True,
) -> None:
    """Assert dual-layer placement bounds (window.open features, moveTo/resizeTo/focus, and URL query/popup) match monitor work-area."""
    avail_left = int(monitor["availLeft"])
    avail_top = int(monitor["availTop"])
    avail_width = int(monitor["availWidth"])
    avail_height = int(monitor["availHeight"])

    call = launch_result.find_open_call(path_substring)
    assert not call.get("blocked"), f"Expected {path_substring} not to be blocked: {call}"
    if require_sync_click:
        assert call.get("calledSynchronouslyInClick") is True, (
            f"Expected synchronous window.open inside click handler for {path_substring}: {call}"
        )

    feat = call.get("parsedFeatures", {})
    assert feat.get("left") == avail_left, f"{path_substring} features.left={feat.get('left')} != availLeft={avail_left} ({call})"
    assert feat.get("top") == avail_top, f"{path_substring} features.top={feat.get('top')} != availTop={avail_top} ({call})"
    assert feat.get("width") == avail_width, f"{path_substring} features.width={feat.get('width')} != availWidth={avail_width} ({call})"
    assert feat.get("height") == avail_height, f"{path_substring} features.height={feat.get('height')} != availHeight={avail_height} ({call})"

    win = launch_result.find_window(path_substring)
    assert win.pageerrors == [], f"Uncaught pageerror in {win.url}: {win.pageerrors}"
    assert win.query.get("mode") == ["window"], f"Expected mode=window in {win.url}"

    # Verify query hints if provided
    if {"ml", "mt", "mw", "mh"}.issubset(win.query.keys()):
        assert int(win.query["ml"][0]) == avail_left
        assert int(win.query["mt"][0]) == avail_top
        assert int(win.query["mw"][0]) == avail_width
        assert int(win.query["mh"][0]) == avail_height

    # Verify moveTo / resizeTo / focus across parent or child self-fit
    matching_parent_ops = [op for op in launch_result.parent_ops if path_substring in op.get("url", "")]
    all_ops = matching_parent_ops + win.child_ops
    move_ops = [op for op in all_ops if op.get("op") == "moveTo"]
    resize_ops = [op for op in all_ops if op.get("op") == "resizeTo"]
    focus_ops = [op for op in all_ops if op.get("op") == "focus"]

    assert len(move_ops) > 0, (
        f"Expected at least one moveTo({avail_left}, {avail_top}) for {path_substring}, got ops={all_ops}"
    )
    assert any(op["x"] == avail_left and op["y"] == avail_top for op in move_ops), (
        f"Expected moveTo({avail_left}, {avail_top}) for {path_substring}, got {move_ops}"
    )
    assert len(resize_ops) > 0, (
        f"Expected at least one resizeTo({avail_width}, {avail_height}) for {path_substring}, got ops={all_ops}"
    )
    assert any(op["w"] == avail_width and op["h"] == avail_height for op in resize_ops), (
        f"Expected resizeTo({avail_width}, {avail_height}) for {path_substring}, got {resize_ops}"
    )
    assert len(focus_ops) > 0, (
        f"Expected focus() call for {path_substring}, ops={all_ops}, features={feat}"
    )
