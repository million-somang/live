/**
 * Live Commerce OS — Browser-Only Multi-Monitor Discovery, Assignment & Placement
 * Zero external dependencies. Vanilla JS only.
 */
(function (global) {
    'use strict';

    const STORAGE_KEY_PREFIX = 'live_os_monitor_layout_v1';
    const API_ENDPOINT = '/api/portal/monitor-layout';

    const PROGRAMS = [
        {
            id: 'obs_studio',
            name: 'OBS 스튜디오',
            subtitle: '/overlay/studio · 송출 오버레이·카메라 스튜디오',
            url: '/overlay/studio',
            windowName: 'live_os_obs_studio',
            selectId: 'selectMonitorObsStudio',
            badgeId: 'badgeObsStudio',
            icon: '🎛️',
        },
        {
            id: 'broadcast_chat',
            name: '채팅관리',
            subtitle: '/broadcast?mode=window · 실시간 웹방송 관제탑',
            url: '/broadcast',
            windowName: 'live_os_broadcast_chat',
            selectId: 'selectMonitorBroadcastChat',
            badgeId: 'badgeBroadcastChat',
            icon: '💬',
        },
        {
            id: 'host_prompter',
            name: '호스트 프롬프트 창',
            subtitle: '/host · 쇼호스트 텔레프롬프터',
            url: '/host',
            windowName: 'live_os_host_prompter',
            selectId: 'selectMonitorHostPrompter',
            badgeId: 'badgeHostPrompter',
            icon: '📱',
        },
    ];

    const state = {
        initialized: false,
        userId: '',
        isSecureContext: Boolean(global.isSecureContext),
        isSupported: typeof global.getScreenDetails === 'function',
        permissionState: 'prompt', // 'granted' | 'prompt' | 'denied' | 'unsupported' | 'insecure' | 'unknown'
        isFallbackMode: false,
        screenDetails: null,
        screens: [], // Live normalized ScreenDetailed[]
        savedMonitors: [], // Monitors from last saved layout (used for stale lookup)
        observedMultiLabels: new Set(), // Labels seen with >1 monitor in a single live/saved snapshot
        configured: false,
        dirtyByUser: false,
        updatedAt: null,
        assignments: {
            obs_studio: 'unassigned',
            broadcast_chat: 'unassigned',
            host_prompter: 'unassigned',
        },
        blockedPrograms: [],
        lastOpenedSummary: null,
    };

    let boundScreensChangeHandler = null;
    let boundCurrentScreenChangeHandler = null;

    function getStorageKey() {
        return state.userId ? `${STORAGE_KEY_PREFIX}_u${state.userId}` : STORAGE_KEY_PREFIX;
    }

    function getCsrfToken() {
        if (typeof global.__LIVE_OS_CSRF_TOKEN__ === 'string' && global.__LIVE_OS_CSRF_TOKEN__) {
            return global.__LIVE_OS_CSRF_TOKEN__;
        }
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? String(meta.getAttribute('content') || '').trim() : '';
    }

    function toFiniteInt(val, fallback) {
        if (val === null || val === undefined || typeof val === 'boolean' || typeof val === 'object') {
            return fallback;
        }
        if (typeof val === 'string' && val.trim() === '') {
            return fallback;
        }
        const n = Number(val);
        return Number.isFinite(n) ? Math.round(n) : fallback;
    }

    function buildMonitorKey(scr, dupIndex) {
        const rawLabel = String((scr && (scr.rawLabel !== undefined ? scr.rawLabel : scr.label)) || '').trim();
        const labelPart = rawLabel || 'Display';
        const role = scr && scr.isPrimary ? 'P' : 'S';
        const left = toFiniteInt(scr && scr.left, 0);
        const top = toFiniteInt(scr && scr.top, 0);
        const width = Math.max(1, toFiniteInt(scr && scr.width, 1920));
        const height = Math.max(1, toFiniteInt(scr && scr.height, 1080));
        return `${labelPart}|${left},${top},${width}x${height}|${role}#${dupIndex}`;
    }

    function normalizeScreen(scr, index, dupCounter) {
        const rawLabel = String((scr && scr.label) || '').trim();
        const isPrimary = Boolean(scr && scr.isPrimary);
        const isInternal = Boolean(scr && scr.isInternal);
        const left = toFiniteInt(scr && scr.left, 0);
        const top = toFiniteInt(scr && scr.top, 0);
        const fallbackW = (global.screen && toFiniteInt(global.screen.width, 1920)) || 1920;
        const fallbackH = (global.screen && toFiniteInt(global.screen.height, 1080)) || 1080;
        const width = Math.max(1, toFiniteInt(scr && scr.width, fallbackW));
        const height = Math.max(1, toFiniteInt(scr && scr.height, fallbackH));
        const availLeft = toFiniteInt(scr && scr.availLeft !== undefined ? scr.availLeft : left, left);
        const availTop = toFiniteInt(scr && scr.availTop !== undefined ? scr.availTop : top, top);
        const availWidth = Math.max(1, toFiniteInt(scr && scr.availWidth !== undefined ? scr.availWidth : width, width));
        const availHeight = Math.max(1, toFiniteInt(scr && scr.availHeight !== undefined ? scr.availHeight : height, height));
        const dprNum = Number(scr && scr.devicePixelRatio);
        const devicePixelRatio = Number.isFinite(dprNum) && dprNum > 0 ? Math.round(dprNum * 1000) / 1000 : 1;

        const baseSig = `${rawLabel || 'Display'}|${left},${top},${width}x${height}|${isPrimary ? 'P' : 'S'}`;
        const dupIdx = dupCounter[baseSig] || 0;
        dupCounter[baseSig] = dupIdx + 1;

        const key = buildMonitorKey({ rawLabel, isPrimary, left, top, width, height }, dupIdx);
        const displayLabel = rawLabel || (isPrimary ? `주 모니터 ${index + 1}` : `보조 모니터 ${index + 1}`);

        return {
            key,
            index,
            label: displayLabel,
            rawLabel,
            isPrimary,
            isInternal,
            left,
            top,
            width,
            height,
            availLeft,
            availTop,
            availWidth,
            availHeight,
            devicePixelRatio,
            isFallback: false,
        };
    }

    function getFallbackScreenDescriptor() {
        const s = global.screen || {};
        const width = Math.max(1, toFiniteInt(s.width, 1920));
        const height = Math.max(1, toFiniteInt(s.height, 1080));
        const availWidth = Math.max(1, toFiniteInt(s.availWidth, width));
        const availHeight = Math.max(1, toFiniteInt(s.availHeight, height));
        const availLeft = toFiniteInt(s.availLeft !== undefined ? s.availLeft : 0, 0);
        const availTop = toFiniteInt(s.availTop !== undefined ? s.availTop : 0, 0);
        const dpr = Number(global.devicePixelRatio);
        return {
            key: `fallback-primary|0,0,${width}x${height}|P#0`,
            index: 0,
            label: '현재 화면 (단일 모니터 폴백)',
            rawLabel: '',
            isPrimary: true,
            isInternal: false,
            left: 0,
            top: 0,
            width,
            height,
            availLeft,
            availTop,
            availWidth,
            availHeight,
            devicePixelRatio: Number.isFinite(dpr) && dpr > 0 ? Math.round(dpr * 1000) / 1000 : 1,
            isFallback: true,
        };
    }

    function getActiveMonitorsList() {
        if (state.screens.length > 0) {
            return state.screens;
        }
        if (state.savedMonitors.length > 0 && !state.isFallbackMode) {
            return state.savedMonitors;
        }
        return [getFallbackScreenDescriptor()];
    }

    function getAssignmentKey(assignmentVal) {
        if (!assignmentVal || assignmentVal === 'unassigned') {
            return 'unassigned';
        }
        if (typeof assignmentVal === 'string') {
            const trimmed = assignmentVal.trim();
            return trimmed && trimmed.toLowerCase() !== 'unassigned' ? trimmed : 'unassigned';
        }
        if (typeof assignmentVal === 'object') {
            const key = String(assignmentVal.monitorKey || assignmentVal.key || '').trim();
            if (!key || key.toLowerCase() === 'unassigned') {
                return 'unassigned';
            }
            return key;
        }
        return 'unassigned';
    }

    function hasVerifiedBounds(val) {
        if (!val || typeof val !== 'object' || val.isUnresolvedStale) {
            return false;
        }
        const left = toFiniteInt(val.left, null);
        const top = toFiniteInt(val.top, null);
        const width = toFiniteInt(val.width, null);
        const height = toFiniteInt(val.height, null);
        return left !== null && top !== null && width !== null && width > 0 && height !== null && height > 0;
    }

    function buildVerifiedMonitorFromDesc(desc, fallbackKey) {
        const dLeft = toFiniteInt(desc.left, 0);
        const dTop = toFiniteInt(desc.top, 0);
        const dWidth = Math.max(1, toFiniteInt(desc.width, 1920));
        const dHeight = Math.max(1, toFiniteInt(desc.height, 1080));
        return {
            key: fallbackKey,
            index: Math.min(64, Math.max(0, toFiniteInt(desc.index, 0))),
            label: desc.label || fallbackKey,
            rawLabel: desc.rawLabel || '',
            isPrimary: Boolean(desc.isPrimary),
            isInternal: Boolean(desc.isInternal),
            left: dLeft,
            top: dTop,
            width: dWidth,
            height: dHeight,
            availLeft: toFiniteInt(desc.availLeft !== undefined ? desc.availLeft : desc.left, dLeft),
            availTop: toFiniteInt(desc.availTop !== undefined ? desc.availTop : desc.top, dTop),
            availWidth: Math.max(1, toFiniteInt(desc.availWidth !== undefined ? desc.availWidth : desc.width, dWidth)),
            availHeight: Math.max(1, toFiniteInt(desc.availHeight !== undefined ? desc.availHeight : desc.height, dHeight)),
            devicePixelRatio: Number(desc.devicePixelRatio) || 1,
            isFallback: Boolean(desc.isFallback),
            isUnresolvedStale: false,
        };
    }

    function getMonitorMatchLabel(monitor) {
        if (!monitor || typeof monitor !== 'object') {
            return '';
        }
        return String(monitor.rawLabel || monitor.label || '').trim();
    }

    function isFallbackAssignment(assignmentVal) {
        const key = getAssignmentKey(assignmentVal);
        if (key === 'unassigned') {
            return false;
        }
        if (typeof assignmentVal === 'object' && assignmentVal !== null && typeof assignmentVal.isFallback === 'boolean') {
            return assignmentVal.isFallback;
        }
        return key.indexOf('fallback-primary|') === 0;
    }

    function recordMultiLabelsFromList(monitorsList) {
        if (!Array.isArray(monitorsList) || monitorsList.length === 0) {
            return;
        }
        const counts = {};
        monitorsList.forEach((m) => {
            if (!m || m.isFallback) return;
            const lbl = getMonitorMatchLabel(m);
            if (!lbl) return;
            counts[lbl] = (counts[lbl] || 0) + 1;
            if (counts[lbl] > 1) {
                state.observedMultiLabels.add(lbl);
            }
        });
    }

    function rebuildObservedMultiLabels() {
        state.observedMultiLabels.clear();
        recordMultiLabelsFromList(state.screens);
        recordMultiLabelsFromList(state.savedMonitors);
    }

    function isAmbiguousMonitorLabel(rawLabel) {
        if (!rawLabel) {
            return false;
        }
        if (state.observedMultiLabels && state.observedMultiLabels.has(rawLabel)) {
            return true;
        }
        const savedCount = state.savedMonitors.filter(
            (m) => m && !m.isFallback && getMonitorMatchLabel(m) === rawLabel
        ).length;
        return savedCount > 1;
    }

    function findMatchingMonitor(assignmentVal, candidateMonitors) {
        const targetKey = getAssignmentKey(assignmentVal);
        if (targetKey === 'unassigned' || !Array.isArray(candidateMonitors) || candidateMonitors.length === 0) {
            return null;
        }

        // 1) Exact key match
        const exact = candidateMonitors.find((m) => m.key === targetKey);
        if (exact) {
            return exact;
        }

        // 1.5) Upgrade fallback placeholder to live primary screen once real screens are available
        if (isFallbackAssignment(assignmentVal)) {
            const liveCandidates = candidateMonitors.filter((m) => !m.isFallback);
            if (liveCandidates.length > 0) {
                return liveCandidates.find((m) => m.isPrimary) || liveCandidates[0];
            }
            const fallbackCandidate = candidateMonitors.find((m) => m.isFallback);
            return fallbackCandidate || null;
        }

        // 2) Geometry match (left, top, width, height) — requires verified bounds and never matches fallback
        if (hasVerifiedBounds(assignmentVal)) {
            const aLeft = toFiniteInt(assignmentVal.left, null);
            const aTop = toFiniteInt(assignmentVal.top, null);
            const aWidth = toFiniteInt(assignmentVal.width, null);
            const aHeight = toFiniteInt(assignmentVal.height, null);
            const byGeom = candidateMonitors.find(
                (m) =>
                    !m.isFallback &&
                    m.left === aLeft &&
                    m.top === aTop &&
                    m.width === aWidth &&
                    m.height === aHeight
            );
            if (byGeom) {
                return byGeom;
            }
        }

        // 3) Unique label match when live hardware changed resolution/position (never matches fallback)
        const rawLabel =
            typeof assignmentVal === 'object' && assignmentVal !== null && !assignmentVal.isUnresolvedStale
                ? getMonitorMatchLabel(assignmentVal)
                : '';
        if (rawLabel && !isAmbiguousMonitorLabel(rawLabel)) {
            const labelMatches = candidateMonitors.filter(
                (m) => !m.isFallback && getMonitorMatchLabel(m) === rawLabel
            );
            if (labelMatches.length === 1) {
                const candidate = labelMatches[0];
                const primaryMismatch =
                    typeof assignmentVal.isPrimary === 'boolean' &&
                    Boolean(candidate.isPrimary) !== assignmentVal.isPrimary;
                if (!primaryMismatch) {
                    return candidate;
                }
            }
        }

        return null;
    }

    function buildUnresolvedStaleMonitor(assignmentVal, key) {
        const label =
            typeof assignmentVal === 'object' && assignmentVal !== null && assignmentVal.label
                ? String(assignmentVal.label)
                : key;
        const rawLabel =
            typeof assignmentVal === 'object' && assignmentVal !== null && assignmentVal.rawLabel
                ? String(assignmentVal.rawLabel)
                : '';
        return {
            key,
            label,
            rawLabel,
            isUnresolvedStale: true,
        };
    }

    function resolveAssignmentState(assignmentVal) {
        const key = getAssignmentKey(assignmentVal);
        if (key === 'unassigned') {
            return { mode: 'unassigned', key: 'unassigned', status: 'unassigned', monitor: null };
        }

        if (state.screens.length > 0) {
            const liveMatch = findMatchingMonitor(assignmentVal, state.screens);
            if (liveMatch) {
                return { mode: 'assigned', key: liveMatch.key, status: 'connected', monitor: liveMatch };
            }
            // Previously saved monitor is not in live ScreenDetailed[] -> preserve as stale!
            const savedMatch = findMatchingMonitor(assignmentVal, state.savedMonitors);
            const verifiedDesc = hasVerifiedBounds(assignmentVal)
                ? assignmentVal
                : hasVerifiedBounds(savedMatch)
                    ? savedMatch
                    : null;
            if (verifiedDesc) {
                return {
                    mode: 'assigned',
                    key,
                    status: 'stale',
                    monitor: buildVerifiedMonitorFromDesc(verifiedDesc, key),
                };
            }
            return {
                mode: 'assigned',
                key,
                status: 'stale',
                monitor: buildUnresolvedStaleMonitor(assignmentVal, key),
            };
        }

        // No live ScreenDetailed[] yet (fallback or pre-permission)
        const activeList = getActiveMonitorsList();
        const activeMatch = findMatchingMonitor(assignmentVal, activeList);
        if (activeMatch) {
            return { mode: 'assigned', key: activeMatch.key, status: 'connected', monitor: activeMatch };
        }
        const savedMatch = findMatchingMonitor(assignmentVal, state.savedMonitors);
        const verifiedDesc = hasVerifiedBounds(assignmentVal)
            ? assignmentVal
            : hasVerifiedBounds(savedMatch)
                ? savedMatch
                : null;
        if (verifiedDesc) {
            return {
                mode: 'assigned',
                key,
                status: 'cached',
                monitor: buildVerifiedMonitorFromDesc(verifiedDesc, key),
            };
        }

        return {
            mode: 'assigned',
            key,
            status: 'cached',
            monitor: buildUnresolvedStaleMonitor(assignmentVal, key),
        };
    }

    function buildSavedAssignmentDescriptor(monitorObj) {
        if (!monitorObj) {
            return 'unassigned';
        }
        if (monitorObj.isUnresolvedStale || !hasVerifiedBounds(monitorObj)) {
            return getAssignmentKey(monitorObj);
        }
        const left = toFiniteInt(monitorObj.left, 0);
        const top = toFiniteInt(monitorObj.top, 0);
        const width = Math.max(1, toFiniteInt(monitorObj.width, 1920));
        const height = Math.max(1, toFiniteInt(monitorObj.height, 1080));
        return {
            monitorKey: monitorObj.key,
            label: monitorObj.label || monitorObj.key,
            rawLabel: monitorObj.rawLabel || '',
            index: Math.min(64, Math.max(0, toFiniteInt(monitorObj.index, 0))),
            isPrimary: Boolean(monitorObj.isPrimary),
            isInternal: Boolean(monitorObj.isInternal),
            left,
            top,
            width,
            height,
            availLeft: toFiniteInt(monitorObj.availLeft !== undefined ? monitorObj.availLeft : monitorObj.left, left),
            availTop: toFiniteInt(monitorObj.availTop !== undefined ? monitorObj.availTop : monitorObj.top, top),
            availWidth: Math.max(1, toFiniteInt(monitorObj.availWidth !== undefined ? monitorObj.availWidth : monitorObj.width, width)),
            availHeight: Math.max(1, toFiniteInt(monitorObj.availHeight !== undefined ? monitorObj.availHeight : monitorObj.height, height)),
            devicePixelRatio: Number(monitorObj.devicePixelRatio) || 1,
            isFallback: Boolean(monitorObj.isFallback),
        };
    }

    function readLocalStorageLayout() {
        try {
            const raw = global.localStorage.getItem(getStorageKey());
            if (!raw) return null;
            const parsed = JSON.parse(raw);
            return parsed && typeof parsed === 'object' ? parsed : null;
        } catch (e) {
            return null;
        }
    }

    function writeLocalStorageLayout(layout) {
        try {
            global.localStorage.setItem(getStorageKey(), JSON.stringify(layout));
        } catch (e) {}
    }

    function applyUnconfiguredDefaultsIfNeeded() {
        if (state.configured || state.dirtyByUser) {
            return;
        }
        const active = getActiveMonitorsList();
        const primary = active.find((m) => m.isPrimary) || active[0];
        state.assignments = {
            obs_studio: 'unassigned',
            broadcast_chat: primary ? buildSavedAssignmentDescriptor(primary) : 'unassigned',
            host_prompter: 'unassigned',
        };
    }

    function applyLayoutPayload(layout) {
        if (!layout || typeof layout !== 'object') return;
        const configured = Boolean(layout.configured);
        if (Array.isArray(layout.monitors)) {
            state.savedMonitors = layout.monitors.slice();
            rebuildObservedMultiLabels();
        }
        if (configured) {
            state.configured = true;
            state.updatedAt = layout.updated_at || null;
            const rawAssign = layout.assignments || {};
            PROGRAMS.forEach((prog) => {
                const val = rawAssign[prog.id];
                state.assignments[prog.id] = val ? val : 'unassigned';
            });
        } else {
            state.configured = false;
            state.updatedAt = null;
            applyUnconfiguredDefaultsIfNeeded();
        }
    }

    function reconcileAssignmentsWithLiveScreens() {
        if (state.screens.length === 0) return;
        if (!state.configured && !state.dirtyByUser) {
            applyUnconfiguredDefaultsIfNeeded();
            return;
        }
        PROGRAMS.forEach((prog) => {
            const current = state.assignments[prog.id];
            if (!current || current === 'unassigned') return;
            const matched = findMatchingMonitor(current, state.screens);
            if (matched) {
                state.assignments[prog.id] = buildSavedAssignmentDescriptor(matched);
            }
        });
    }

    function escapeHtml(str) {
        return String(str || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function setAlertBanner(type, messageHtml) {
        const box = document.getElementById('monitorStatusAlert');
        if (!box) return;
        if (!messageHtml) {
            box.style.display = 'none';
            box.innerHTML = '';
            return;
        }
        box.className = `ml-alert ml-alert-${type || 'info'}`;
        box.style.display = 'flex';
        box.innerHTML = messageHtml;
    }

    function renderPermissionBadge() {
        const badge = document.getElementById('monitorPermissionBadge');
        if (!badge) return;
        let text = '권한 확인 중';
        let cls = 'ml-badge-neutral';
        if (!state.isSecureContext) {
            text = '보안 컨텍스트 필요 (폴백)';
            cls = 'ml-badge-warn';
        } else if (!state.isSupported) {
            text = '브라우저 미지원 (폴백)';
            cls = 'ml-badge-warn';
        } else if (state.permissionState === 'granted' && state.screens.length > 0) {
            text = `창 관리 허용됨 · 모니터 ${state.screens.length}대`;
            cls = 'ml-badge-ok';
        } else if (state.permissionState === 'granted') {
            text = '창 관리 허용됨';
            cls = 'ml-badge-ok';
        } else if (state.permissionState === 'denied') {
            text = '창 관리 권한 거부됨 (폴백)';
            cls = 'ml-badge-err';
        } else {
            text = '모니터 검색 대기';
            cls = 'ml-badge-neutral';
        }
        badge.className = `ml-perm-badge ${cls}`;
        badge.textContent = text;
    }

    function renderMonitorCards() {
        const container = document.getElementById('detectedMonitorsList');
        if (!container) return;
        const monitors = getActiveMonitorsList();
        const cardsHtml = monitors
            .map((m, idx) => {
                const badges = [];
                if (m.isPrimary) badges.push('<span class="ml-tag ml-tag-primary">주 모니터</span>');
                else badges.push('<span class="ml-tag ml-tag-sub">보조 모니터</span>');
                if (m.isInternal) badges.push('<span class="ml-tag ml-tag-neutral">내장</span>');
                if (m.isFallback) badges.push('<span class="ml-tag ml-tag-warn">단일 폴백</span>');
                const dprPct = Math.round((Number(m.devicePixelRatio) || 1) * 100);
                return `
                    <div class="ml-monitor-card ${m.isFallback ? 'is-fallback' : ''}" data-monitor-key="${escapeHtml(m.key)}">
                        <div class="ml-monitor-card-top">
                            <strong class="ml-monitor-name">모니터 ${idx + 1} · ${escapeHtml(m.label)}</strong>
                            <div class="ml-monitor-tags">${badges.join('')}</div>
                        </div>
                        <div class="ml-monitor-meta">
                            <span>해상도 ${m.width}×${m.height}</span>
                            <span>작업영역 ${m.availWidth}×${m.availHeight}</span>
                            <span>좌표 (${m.availLeft}, ${m.availTop})</span>
                            <span>배율 ${dprPct}%</span>
                        </div>
                    </div>
                `;
            })
            .join('');
        container.innerHTML = cardsHtml;
    }

    function renderProgramSelects() {
        const activeMonitors = getActiveMonitorsList();
        const keyCounts = {};
        const resolvedMap = {};

        PROGRAMS.forEach((prog) => {
            const resolved = resolveAssignmentState(state.assignments[prog.id]);
            resolvedMap[prog.id] = resolved;
            if (resolved.mode === 'assigned' && resolved.key) {
                keyCounts[resolved.key] = (keyCounts[resolved.key] || 0) + 1;
            }
        });

        PROGRAMS.forEach((prog) => {
            const selectEl = document.getElementById(prog.selectId);
            const badgeEl = document.getElementById(prog.badgeId);
            const resolved = resolvedMap[prog.id];
            if (!selectEl) return;

            const options = ['<option value="unassigned">미배치 (열지 않음)</option>'];
            activeMonitors.forEach((m, idx) => {
                const roleLabel = m.isPrimary ? '주 모니터 · ' : '';
                const fbLabel = m.isFallback ? ' · 폴백' : '';
                const text = `모니터 ${idx + 1} · ${m.label} (${roleLabel}${m.width}×${m.height}${fbLabel})`;
                options.push(`<option value="${escapeHtml(m.key)}">${escapeHtml(text)}</option>`);
            });

            if (
                resolved.mode === 'assigned' &&
                resolved.key !== 'unassigned' &&
                !activeMonitors.some((m) => m.key === resolved.key)
            ) {
                const m = resolved.monitor || { label: resolved.key };
                const dimText = m.width && m.height ? ` (${m.width}×${m.height})` : '';
                const isStale = resolved.status === 'stale';
                const statusLabel = isStale
                    ? '연결 끊김 (저장됨)'
                    : state.permissionState === 'denied'
                      ? '저장된 모니터 (권한 거부됨)'
                      : state.isFallbackMode
                        ? '저장된 모니터 (폴백 모드)'
                        : '저장된 모니터';
                const prefixIcon = isStale ? '⚠️' : '💾';
                options.push(
                    `<option value="${escapeHtml(resolved.key)}" data-stale="${isStale ? 'true' : 'false'}">${prefixIcon} ${escapeHtml(m.label || resolved.key)}${dimText} — ${statusLabel}</option>`
                );
            }

            selectEl.innerHTML = options.join('');
            selectEl.value = resolved.mode === 'assigned' ? resolved.key : 'unassigned';

            if (badgeEl) {
                if (resolved.mode === 'unassigned') {
                    badgeEl.className = 'ml-row-badge ml-row-badge-muted';
                    badgeEl.textContent = '열지 않음';
                } else if (resolved.status === 'stale') {
                    badgeEl.className = 'ml-row-badge ml-row-badge-warn';
                    badgeEl.textContent = '⚠️ 연결 끊긴 모니터 (설정 유지)';
                } else if (resolved.status === 'cached') {
                    badgeEl.className = 'ml-row-badge ml-row-badge-info';
                    badgeEl.textContent =
                        state.permissionState === 'denied'
                            ? '저장된 설정 (권한 확인 필요)'
                            : state.isFallbackMode
                              ? '저장된 설정 (폴백 모드)'
                              : '저장된 설정 유지';
                } else if (keyCounts[resolved.key] > 1) {
                    badgeEl.className = 'ml-row-badge ml-row-badge-info';
                    badgeEl.textContent = `동일 모니터 중복 배치 (${keyCounts[resolved.key]}개 창)`;
                } else {
                    badgeEl.className = 'ml-row-badge ml-row-badge-ok';
                    badgeEl.textContent = '배치 준비됨';
                }
            }
        });

        renderLayoutSummary(resolvedMap);
    }

    function renderLayoutSummary(resolvedMap) {
        const summaryEl = document.getElementById('monitorLayoutSummary');
        if (!summaryEl) return;
        const activeNames = PROGRAMS.filter((p) => resolvedMap[p.id] && resolvedMap[p.id].mode === 'assigned').map((p) => p.name);
        const launchSummary =
            activeNames.length > 0
                ? `'실시간 방송 관제탑 열기' 클릭 시 <strong>${activeNames.length}개 창</strong> (${escapeHtml(activeNames.join(', '))})이 지정 모니터에 최대화 크기로 열립니다.`
                : `현재 <strong>모든 프로그램이 미배치</strong> 상태입니다. 실행하려면 최소 1개 프로그램에 모니터를 지정하세요.`;
        const savedText = state.dirtyByUser
            ? ' · 미저장 변경사항'
            : state.updatedAt
              ? ` · 저장됨 (마지막 서버 저장: ${escapeHtml(state.updatedAt)})`
              : state.configured
                ? ' · 저장됨'
                : ' · 기본값 (미저장)';
        summaryEl.innerHTML = `<span>${launchSummary}</span><span class="ml-summary-time">${savedText}</span>`;
    }

    function renderPopupBlockedBox() {
        const box = document.getElementById('monitorPopupBlockedBox');
        if (!box) return;
        if (!state.blockedPrograms || state.blockedPrograms.length === 0) {
            box.style.display = 'none';
            box.innerHTML = '';
            return;
        }
        const buttons = state.blockedPrograms
            .map(
                (item) =>
                    `<button type="button" class="ml-btn ml-btn-retry" data-retry-program="${escapeHtml(item.id)}">${escapeHtml(item.icon)} ${escapeHtml(item.name)} 다시 열기</button>`
            )
            .join('');
        box.style.display = 'flex';
        box.innerHTML = `
            <div class="ml-blocked-text">
                <strong>🚫 브라우저 팝업 차단이 감지되었습니다.</strong>
                <span>주소창 오른쪽 팝업 차단 아이콘에서 이 사이트의 팝업을 '항상 허용'으로 설정하거나 아래 버튼으로 개별 창을 여세요.</span>
            </div>
            <div class="ml-blocked-actions">${buttons}</div>
        `;
        box.querySelectorAll('[data-retry-program]').forEach((btn) => {
            btn.addEventListener('click', function () {
                const progId = btn.getAttribute('data-retry-program');
                retryOpenSingleProgram(progId);
            });
        });
    }

    function renderAll() {
        renderPermissionBadge();
        renderMonitorCards();
        renderProgramSelects();
        renderPopupBlockedBox();
    }

    function detachScreenListeners() {
        if (state.screenDetails) {
            if (boundScreensChangeHandler) {
                try {
                    state.screenDetails.removeEventListener('screenschange', boundScreensChangeHandler);
                } catch (e) {}
            }
            if (boundCurrentScreenChangeHandler) {
                try {
                    state.screenDetails.removeEventListener('currentscreenchange', boundCurrentScreenChangeHandler);
                } catch (e) {}
            }
        }
        boundScreensChangeHandler = null;
        boundCurrentScreenChangeHandler = null;
    }

    function syncScreensFromDetails(details) {
        const rawList = Array.isArray(details && details.screens) ? details.screens : [];
        const dupCounter = {};
        state.screens = rawList.map((scr, idx) => normalizeScreen(scr, idx, dupCounter));
        state.isFallbackMode = state.screens.length === 0;
        recordMultiLabelsFromList(state.screens);
        reconcileAssignmentsWithLiveScreens();
    }

    async function discoverMonitors(options) {
        const opts = options || {};
        const silent = Boolean(opts.silent);

        state.isSecureContext = Boolean(global.isSecureContext);
        state.isSupported = typeof global.getScreenDetails === 'function';

        if (!state.isSecureContext) {
            state.permissionState = 'insecure';
            state.isFallbackMode = true;
            state.screens = [];
            applyUnconfiguredDefaultsIfNeeded();
            renderAll();
            if (!silent) {
                setAlertBanner(
                    'warn',
                    '⚠️ <strong>보안 컨텍스트(HTTPS 또는 http://localhost / 127.0.0.1)</strong>에서만 다중 모니터 검색이 가능합니다. 현재 단일 화면 폴백 모드로 동작합니다.'
                );
            }
            return { ok: false, reason: 'insecure_context', monitors: getActiveMonitorsList() };
        }

        if (!state.isSupported) {
            state.permissionState = 'unsupported';
            state.isFallbackMode = true;
            state.screens = [];
            applyUnconfiguredDefaultsIfNeeded();
            renderAll();
            if (!silent) {
                setAlertBanner(
                    'warn',
                    '⚠️ 현재 브라우저는 <strong>Window Management API(getScreenDetails)</strong>를 지원하지 않습니다. 최신 Chrome 또는 Edge를 사용해 주세요. (단일 화면 폴백)'
                );
            }
            return { ok: false, reason: 'unsupported', monitors: getActiveMonitorsList() };
        }

        try {
            const details = await global.getScreenDetails();
            detachScreenListeners();
            state.screenDetails = details;
            state.permissionState = 'granted';
            syncScreensFromDetails(details);

            boundScreensChangeHandler = function () {
                syncScreensFromDetails(details);
                renderAll();
                setAlertBanner('info', `🖥️ 모니터 연결 구성 변경이 감지되었습니다. (현재 ${state.screens.length}대)`);
            };
            boundCurrentScreenChangeHandler = function () {
                syncScreensFromDetails(details);
                renderMonitorCards();
            };
            if (details && typeof details.addEventListener === 'function') {
                details.addEventListener('screenschange', boundScreensChangeHandler);
                details.addEventListener('currentscreenchange', boundCurrentScreenChangeHandler);
            }

            renderAll();
            if (!silent) {
                setAlertBanner('ok', `✅ 연결된 모니터 <strong>${state.screens.length}대</strong>를 검색했습니다.`);
            }
            return { ok: true, monitors: state.screens };
        } catch (err) {
            const errName = err && err.name ? String(err.name) : '';
            state.screens = [];
            state.isFallbackMode = true;
            if (errName === 'NotAllowedError' || errName === 'SecurityError') {
                state.permissionState = 'denied';
                applyUnconfiguredDefaultsIfNeeded();
                renderAll();
                if (!silent) {
                    setAlertBanner(
                        'err',
                        '🚫 브라우저의 <strong>창 관리(Window Management)</strong> 권한이 거부되었습니다. 주소창 왼쪽 사이트 설정에서 권한을 허용한 뒤 다시 검색해 주세요. (단일 화면 폴백)'
                    );
                }
                return { ok: false, reason: 'permission_denied', monitors: getActiveMonitorsList() };
            }
            applyUnconfiguredDefaultsIfNeeded();
            renderAll();
            if (!silent) {
                setAlertBanner(
                    'err',
                    `⚠️ 모니터 검색 중 오류가 발생했습니다: ${escapeHtml((err && err.message) || errName || '알 수 없는 오류')} (단일 화면 폴백)`
                );
            }
            return { ok: false, reason: 'error', monitors: getActiveMonitorsList() };
        }
    }

    function readCurrentSelectAssignments() {
        const active = getActiveMonitorsList();
        const result = {};
        const usedMonitorsByKey = {};

        active.forEach((m) => {
            usedMonitorsByKey[m.key] = m;
        });

        PROGRAMS.forEach((prog) => {
            const selectEl = document.getElementById(prog.selectId);
            const selectedKey = selectEl ? selectEl.value : getAssignmentKey(state.assignments[prog.id]);
            if (!selectedKey || selectedKey === 'unassigned') {
                result[prog.id] = 'unassigned';
                return;
            }
            if (usedMonitorsByKey[selectedKey]) {
                result[prog.id] = buildSavedAssignmentDescriptor(usedMonitorsByKey[selectedKey]);
                return;
            }
            const resolved = resolveAssignmentState(state.assignments[prog.id]);
            if (
                resolved.mode === 'assigned' &&
                resolved.key === selectedKey &&
                resolved.monitor &&
                !resolved.monitor.isUnresolvedStale &&
                hasVerifiedBounds(resolved.monitor)
            ) {
                result[prog.id] = buildSavedAssignmentDescriptor(resolved.monitor);
            } else {
                result[prog.id] = selectedKey;
            }
        });
        return result;
    }

    async function saveMonitorLayout() {
        const nextAssignments = readCurrentSelectAssignments();
        state.assignments = nextAssignments;

        // Collect active monitors + preserve any stale monitors still referenced
        const monitorsMap = {};
        getActiveMonitorsList().forEach((m) => {
            monitorsMap[m.key] = m;
        });
        PROGRAMS.forEach((prog) => {
            const val = nextAssignments[prog.id];
            if (val && typeof val === 'object' && val.monitorKey && !monitorsMap[val.monitorKey]) {
                const vLeft = toFiniteInt(val.left, 0);
                const vTop = toFiniteInt(val.top, 0);
                const vWidth = Math.max(1, toFiniteInt(val.width, 1920));
                const vHeight = Math.max(1, toFiniteInt(val.height, 1080));
                monitorsMap[val.monitorKey] = {
                    key: val.monitorKey,
                    index: Math.min(64, Math.max(0, toFiniteInt(val.index, 0))),
                    label: val.label || val.monitorKey,
                    rawLabel: val.rawLabel || '',
                    isPrimary: Boolean(val.isPrimary),
                    isInternal: Boolean(val.isInternal),
                    left: vLeft,
                    top: vTop,
                    width: vWidth,
                    height: vHeight,
                    availLeft: toFiniteInt(val.availLeft !== undefined ? val.availLeft : val.left, vLeft),
                    availTop: toFiniteInt(val.availTop !== undefined ? val.availTop : val.top, vTop),
                    availWidth: Math.max(1, toFiniteInt(val.availWidth !== undefined ? val.availWidth : val.width, vWidth)),
                    availHeight: Math.max(1, toFiniteInt(val.availHeight !== undefined ? val.availHeight : val.height, vHeight)),
                    devicePixelRatio: Number(val.devicePixelRatio) || 1,
                    isFallback: Boolean(val.isFallback),
                };
            }
        });

        const monitorsPayload = Object.values(monitorsMap);
        const localPayload = {
            configured: true,
            monitors: monitorsPayload,
            assignments: nextAssignments,
            updated_at: new Date().toISOString(),
        };
        writeLocalStorageLayout(localPayload);

        try {
            const headers = { 'Content-Type': 'application/json' };
            const csrf = getCsrfToken();
            if (csrf) {
                headers['X-CSRF-Token'] = csrf;
            }
            const res = await fetch(API_ENDPOINT, {
                method: 'POST',
                credentials: 'same-origin',
                headers,
                body: JSON.stringify({
                    monitors: monitorsPayload,
                    assignments: nextAssignments,
                }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || data.status !== 'ok') {
                throw new Error(data.message || `HTTP ${res.status}`);
            }
            state.configured = true;
            state.dirtyByUser = false;
            if (data.layout) {
                applyLayoutPayload(data.layout);
                writeLocalStorageLayout(data.layout);
            }
            renderAll();
            setAlertBanner('ok', '✅ 모니터 배치 설정이 저장되었습니다. 새로고침 및 재접속 후에도 유지됩니다.');
            return true;
        } catch (err) {
            renderAll();
            setAlertBanner(
                'warn',
                `⚠️ 로컬 브라우저에는 저장되었으나 서버 동기화에 실패했습니다 (${escapeHtml(err.message || '오류')}).`
            );
            return false;
        }
    }

    function resolveTargetBoundsSync(progId) {
        const assignment = state.assignments[progId];
        const resolved = resolveAssignmentState(assignment);
        if (resolved.mode === 'unassigned') {
            return null;
        }
        const isLiveStale = state.screens.length > 0 && resolved.status === 'stale';
        const livePrimary =
            state.screens.length > 0
                ? state.screens.find((s) => s.isPrimary) || state.screens[0]
                : null;
        const m = isLiveStale
            ? livePrimary || getFallbackScreenDescriptor()
            : resolved.monitor && !resolved.monitor.isUnresolvedStale && hasVerifiedBounds(resolved.monitor)
                ? resolved.monitor
                : getFallbackScreenDescriptor();
        const mLeft = toFiniteInt(m.left, 0);
        const mTop = toFiniteInt(m.top, 0);
        const mWidth = Math.max(320, toFiniteInt(m.width, 1920));
        const mHeight = Math.max(240, toFiniteInt(m.height, 1080));
        const availLeft = toFiniteInt(m.availLeft !== undefined ? m.availLeft : m.left, mLeft);
        const availTop = toFiniteInt(m.availTop !== undefined ? m.availTop : m.top, mTop);
        const availWidth = Math.max(320, toFiniteInt(m.availWidth !== undefined ? m.availWidth : m.width, mWidth));
        const availHeight = Math.max(240, toFiniteInt(m.availHeight !== undefined ? m.availHeight : m.height, mHeight));
        const displayLabel = isLiveStale
            ? m.label || resolved.key
            : (resolved.monitor && resolved.monitor.label) || m.label || resolved.key;
        const staleMonitorLabel = isLiveStale
            ? (resolved.monitor && resolved.monitor.label) || resolved.key
            : '';
        return {
            availLeft,
            availTop,
            availWidth,
            availHeight,
            label: displayLabel,
            fallbackFromStale: isLiveStale,
            staleMonitorLabel,
        };
    }

    function buildProgramLaunchUrl(prog, bounds) {
        const params = new URLSearchParams();
        params.set('mode', 'window');
        params.set('ml', String(bounds.availLeft));
        params.set('mt', String(bounds.availTop));
        params.set('mw', String(bounds.availWidth));
        params.set('mh', String(bounds.availHeight));
        return `${prog.url}?${params.toString()}`;
    }

    function buildWindowFeatures(bounds) {
        return [
            'popup=yes',
            `left=${bounds.availLeft}`,
            `top=${bounds.availTop}`,
            `width=${bounds.availWidth}`,
            `height=${bounds.availHeight}`,
            'menubar=no',
            'toolbar=no',
            'location=no',
            'status=no',
            'resizable=yes',
            'scrollbars=yes',
        ].join(',');
    }

    function applyBoundsToWindowHandle(win, bounds) {
        if (!win || win.closed) return;
        try {
            if (typeof win.moveTo === 'function') {
                win.moveTo(bounds.availLeft, bounds.availTop);
            }
            if (typeof win.resizeTo === 'function') {
                win.resizeTo(bounds.availWidth, bounds.availHeight);
            }
            if (typeof win.focus === 'function') {
                win.focus();
            }
        } catch (e) {}
    }

    function openProgramWindowSync(prog, bounds) {
        const url = buildProgramLaunchUrl(prog, bounds);
        const features = buildWindowFeatures(bounds);
        const win = global.open(url, prog.windowName, features);
        if (!win || win.closed || typeof win.closed === 'undefined') {
            return { opened: false, win: null };
        }
        applyBoundsToWindowHandle(win, bounds);
        try {
            if (typeof win.addEventListener === 'function') {
                win.addEventListener(
                    'load',
                    function () {
                        applyBoundsToWindowHandle(win, bounds);
                    },
                    { once: true }
                );
            }
        } catch (e) {}
        setTimeout(function () {
            applyBoundsToWindowHandle(win, bounds);
        }, 120);
        return { opened: true, win };
    }

    function retryOpenSingleProgram(progId) {
        const prog = PROGRAMS.find((p) => p.id === progId);
        if (!prog) return;
        const bounds = resolveTargetBoundsSync(prog.id) || getFallbackScreenDescriptor();
        const result = openProgramWindowSync(prog, bounds);
        if (result.opened) {
            state.blockedPrograms = state.blockedPrograms.filter((b) => b.id !== progId);
            renderPopupBlockedBox();
            setAlertBanner('ok', `✅ ${escapeHtml(prog.name)} 창을 지정 모니터에 열었습니다.`);
        } else {
            setAlertBanner('err', `🚫 ${escapeHtml(prog.name)} 창이 여전히 팝업 차단되었습니다. 브라우저 팝업 허용 설정을 확인하세요.`);
        }
    }

    /**
     * R3: Synchronously launches all non-'unassigned' programs onto their assigned monitors.
     * MUST NOT execute any `await` before `window.open()` so user activation is preserved!
     */
    function openControlTowerWindows(event) {
        if (event) {
            event.preventDefault();
            event.stopPropagation();
        }

        // Sync live dropdown state if DOM is present
        if (document.getElementById('monitorLayoutPanel')) {
            state.assignments = readCurrentSelectAssignments();
        } else {
            const localLayout = readLocalStorageLayout();
            if (localLayout) {
                applyLayoutPayload(localLayout);
            } else {
                applyUnconfiguredDefaultsIfNeeded();
            }
        }

        const targets = [];
        PROGRAMS.forEach((prog) => {
            const bounds = resolveTargetBoundsSync(prog.id);
            if (bounds) {
                targets.push({ prog, bounds });
            }
        });

        if (targets.length === 0) {
            state.blockedPrograms = [];
            renderPopupBlockedBox();
            setAlertBanner(
                'warn',
                '⚠️ 실행할 프로그램이 없습니다. 3개 프로그램이 모두 <strong>미배치 (열지 않음)</strong> 상태입니다. 최소 1개 프로그램에 모니터를 지정해 주세요.'
            );
            return { openedCount: 0, blockedCount: 0 };
        }

        const opened = [];
        const blocked = [];
        const staleFallbacks = [];

        // Synchronous loop of window.open() calls — zero awaits!
        for (let i = 0; i < targets.length; i += 1) {
            const item = targets[i];
            const res = openProgramWindowSync(item.prog, item.bounds);
            if (res.opened) {
                opened.push(item.prog.name);
                if (item.bounds.fallbackFromStale) {
                    staleFallbacks.push({
                        name: item.prog.name,
                        staleLabel: item.bounds.staleMonitorLabel,
                        targetLabel: item.bounds.label,
                    });
                }
            } else {
                blocked.push(item.prog);
            }
        }

        state.blockedPrograms = blocked;
        renderPopupBlockedBox();

        const staleNoticeHtml =
            staleFallbacks.length > 0
                ? ` 연결 끊긴 모니터에 지정된 프로그램(${escapeHtml(
                      staleFallbacks.map((s) => s.name).join(', ')
                  )})은 현재 연결된 주 모니터(${escapeHtml(
                      staleFallbacks[0].targetLabel || '기본 화면'
                  )})로 안전 배치되었습니다.`
                : '';

        if (blocked.length === 0) {
            if (staleFallbacks.length > 0) {
                setAlertBanner(
                    'warn',
                    `⚠️ 관제탑 프로그램 <strong>${opened.length}개 창</strong> (${escapeHtml(opened.join(', '))})을 실행했습니다.${staleNoticeHtml}`
                );
            } else {
                setAlertBanner(
                    'ok',
                    `🚀 관제탑 프로그램 <strong>${opened.length}개 창</strong> (${escapeHtml(opened.join(', '))})을 지정된 모니터에 최대화 크기로 실행했습니다.`
                );
            }
        } else if (opened.length > 0) {
            setAlertBanner(
                'warn',
                `⚠️ ${opened.length}개 창(${escapeHtml(opened.join(', '))})을 열었으나, ${blocked.length}개 창(${escapeHtml(
                    blocked.map((b) => b.name).join(', ')
                )})이 팝업 차단되었습니다.${staleNoticeHtml}`
            );
        } else {
            setAlertBanner(
                'err',
                `🚫 모든 창(${escapeHtml(blocked.map((b) => b.name).join(', '))})이 브라우저 팝업 차단으로 열리지 않았습니다. 아래에서 팝업 허용 후 다시 열어 주세요.`
            );
        }

        return { openedCount: opened.length, blockedCount: blocked.length };
    }

    /**
     * Single-window launcher for the individual '웹방송 관제탑' card & GNB button.
     */
    function openBroadcastSingleWindow(event) {
        if (event) {
            event.preventDefault();
            event.stopPropagation();
        }
        if (document.getElementById('monitorLayoutPanel')) {
            state.assignments = readCurrentSelectAssignments();
        } else {
            const localLayout = readLocalStorageLayout();
            if (localLayout) {
                applyLayoutPayload(localLayout);
            }
        }
        const chatProg = PROGRAMS.find((p) => p.id === 'broadcast_chat');
        const bounds = resolveTargetBoundsSync('broadcast_chat') || getFallbackScreenDescriptor();
        const res = openProgramWindowSync(chatProg, bounds);
        if (!res.opened) {
            global.open('/broadcast?mode=window', '_blank');
        }
    }

    async function hydrateFromServer() {
        try {
            const res = await fetch(API_ENDPOINT, {
                method: 'GET',
                credentials: 'same-origin',
            });
            if (!res.ok) return;
            const data = await res.json();
            if (!data || data.status !== 'ok' || !data.layout) return;

            if (!state.dirtyByUser) {
                if (data.layout.configured) {
                    applyLayoutPayload(data.layout);
                    writeLocalStorageLayout(data.layout);
                    reconcileAssignmentsWithLiveScreens();
                } else {
                    const localSaved = readLocalStorageLayout();
                    if (!localSaved || !localSaved.configured) {
                        applyLayoutPayload(data.layout);
                    }
                }
                renderAll();
            }
        } catch (e) {}
    }

    async function initPermissionAndAutoDiscover() {
        state.isSecureContext = Boolean(global.isSecureContext);
        state.isSupported = typeof global.getScreenDetails === 'function';

        if (!state.isSecureContext) {
            state.permissionState = 'insecure';
            state.isFallbackMode = true;
            renderAll();
            return;
        }
        if (!state.isSupported) {
            state.permissionState = 'unsupported';
            state.isFallbackMode = true;
            renderAll();
            return;
        }

        if (navigator.permissions && typeof navigator.permissions.query === 'function') {
            let permStatus = null;
            try {
                permStatus = await navigator.permissions.query({ name: 'window-management' });
            } catch (e) {
                if (e && e.name === 'TypeError') {
                    try {
                        permStatus = await navigator.permissions.query({ name: 'window-placement' });
                    } catch (e2) {
                        permStatus = null;
                    }
                }
            }
            if (permStatus) {
                state.permissionState = permStatus.state;
                permStatus.onchange = function () {
                    state.permissionState = permStatus.state;
                    if (permStatus.state === 'granted') {
                        discoverMonitors({ silent: true });
                    } else {
                        if (permStatus.state === 'denied') {
                            state.screens = [];
                            state.isFallbackMode = true;
                        }
                        renderAll();
                    }
                };
                if (permStatus.state === 'granted') {
                    await discoverMonitors({ silent: true });
                    return;
                }
                if (permStatus.state === 'denied') {
                    state.isFallbackMode = true;
                }
            }
        }
        renderAll();
    }

    function bindPortalDomEvents() {
        const btnDiscover = document.getElementById('btnDiscoverMonitors');
        if (btnDiscover) {
            btnDiscover.addEventListener('click', function () {
                discoverMonitors({ silent: false });
            });
        }

        const btnSave = document.getElementById('btnSaveMonitorLayout');
        if (btnSave) {
            btnSave.addEventListener('click', function () {
                saveMonitorLayout();
            });
        }

        PROGRAMS.forEach((prog) => {
            const selectEl = document.getElementById(prog.selectId);
            if (!selectEl) return;
            selectEl.addEventListener('change', function () {
                state.dirtyByUser = true;
                state.assignments = readCurrentSelectAssignments();
                renderProgramSelects();
            });
        });
    }

    function initPortalMonitorManager() {
        const panel = document.getElementById('monitorLayoutPanel');
        if (!panel || state.initialized) return;
        state.initialized = true;
        state.userId = String(panel.getAttribute('data-studio-user-id') || '').trim();

        // 1) Synchronous localStorage hydration
        const localSaved = readLocalStorageLayout();
        if (localSaved) {
            applyLayoutPayload(localSaved);
        } else {
            applyUnconfiguredDefaultsIfNeeded();
        }

        bindPortalDomEvents();
        renderAll();

        // 2) Async permission check (+ silent auto-discovery when granted) & server hydration
        initPermissionAndAutoDiscover();
        hydrateFromServer();
    }

    /**
     * Child window load-time self-fit hook for /overlay/studio, /broadcast, /host
     */
    function applyChildWindowPlacementFromUrl() {
        const search = global.location && global.location.search ? global.location.search : '';
        if (!search) return;
        const params = new URLSearchParams(search);
        const mode = params.get('mode');
        if (mode === 'window') {
            const markWindowMode = function () {
                if (document.body) {
                    document.body.classList.add('mode-window');
                }
            };
            if (document.body) {
                markWindowMode();
            } else {
                document.addEventListener('DOMContentLoaded', markWindowMode);
            }
        }

        const rawMl = params.get('ml');
        const rawMt = params.get('mt');
        const rawMw = params.get('mw');
        const rawMh = params.get('mh');
        if (
            rawMl === null || rawMl.trim() === '' ||
            rawMt === null || rawMt.trim() === '' ||
            rawMw === null || rawMw.trim() === '' ||
            rawMh === null || rawMh.trim() === ''
        ) {
            return;
        }
        const ml = Number(rawMl);
        const mt = Number(rawMt);
        const mw = Number(rawMw);
        const mh = Number(rawMh);
        if (!Number.isFinite(ml) || !Number.isFinite(mt) || !Number.isFinite(mw) || !Number.isFinite(mh)) {
            return;
        }
        if (mw < 200 || mh < 200) {
            return;
        }

        const bounds = {
            availLeft: Math.round(ml),
            availTop: Math.round(mt),
            availWidth: Math.round(mw),
            availHeight: Math.round(mh),
        };

        const reinforce = function () {
            const dx = Math.abs((Number(global.screenX) || 0) - bounds.availLeft);
            const dy = Math.abs((Number(global.screenY) || 0) - bounds.availTop);
            const dw = Math.abs((Number(global.outerWidth) || 0) - bounds.availWidth);
            const dh = Math.abs((Number(global.outerHeight) || 0) - bounds.availHeight);
            if (dx > 4 || dy > 4 || dw > 4 || dh > 4) {
                applyBoundsToWindowHandle(global, bounds);
            }
        };

        reinforce();
        document.addEventListener('DOMContentLoaded', reinforce);
        global.addEventListener('load', function () {
            reinforce();
            setTimeout(reinforce, 80);
            setTimeout(reinforce, 250);
        });
    }

    // Export public API
    global.LiveMonitorLayout = {
        PROGRAMS,
        state,
        initPortalMonitorManager,
        discoverMonitors,
        saveMonitorLayout,
        openControlTowerWindows,
        openBroadcastSingleWindow,
        retryOpenSingleProgram,
        getActiveMonitorsList,
    };
    global.openControlTowerWindows = openControlTowerWindows;

    // Auto-run child window placement hook immediately, and portal init on DOM ready
    applyChildWindowPlacementFromUrl();
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initPortalMonitorManager);
    } else {
        initPortalMonitorManager();
    }
})(window);
