/**
 * 🌟 Live Commerce OS — Zero Server Video Traffic CSR <-> Local Agent Bridge
 * - 클라우드 서버(예: 49.50.132.140:5000)에서 OBS 스튜디오(/overlay/studio) 접속 시
 *   카메라 수신·프리뷰·WebRTC·FFmpeg RTMP 송출은 방송자 로컬 PC 에이전트(127.0.0.1:5000)로 직결하여
 *   호스팅 서버의 영상 트래픽을 0바이트(0원)로 유지합니다.
 */
(function () {
    'use strict';

    const LOCAL_AGENT_HTTP = 'http://127.0.0.1:5000';
    const LOCAL_AGENT_HTTPS = 'https://127.0.0.1:5443';

    function isLocalOrLanHostname(hostname) {
        if (!hostname) return true;
        const h = String(hostname).toLowerCase().trim();
        if (h === 'localhost' || h === '127.0.0.1' || h === '::1' || h === '[::1]') return true;
        if (/^192\.168\.\d+\.\d+$/.test(h)) return true;
        if (/^10\.\d+\.\d+\.\d+$/.test(h)) return true;
        if (/^172\.(1[6-9]|2\d|3[0-1])\.\d+\.\d+$/.test(h)) return true;
        return false;
    }

    const isRemoteCloudHost = !isLocalOrLanHostname(window.location.hostname);

    const bridgeState = {
        isRemoteCloudHost: isRemoteCloudHost,
        connected: !isRemoteCloudHost,
        agentBaseUrl: LOCAL_AGENT_HTTP,
        health: null,
        lastCheckTs: 0,
        serverVideoTrafficBytes: 0,
    };

    window.__ZERO_TRAFFIC_AGENT_BRIDGE__ = bridgeState;

    // 1. 로컬 에이전트로 직결 라우팅할 미디어/하드웨어/RTMP 전용 경로 판별
    //    (단, [방식 2: 다른 네트워크 원격 카메라(/remote-cam)] 사용 시 카메라 조회/프레임은 원격 서버 경로 유지)
    function isLocalAgentExclusivePath(pathStr) {
        if (!pathStr) return false;
        const isRemoteCamMode = (window.__CAMERA_NETWORK_MODE__ === 'remote_network');
        if (!isRemoteCamMode && (
            pathStr.startsWith('/api/camera/frame') ||
            pathStr.startsWith('/api/camera/audio') ||
            pathStr.startsWith('/api/camera/stream') ||
            pathStr.startsWith('/api/camera/latest-frame') ||
            pathStr.startsWith('/api/camera/renew-url') ||
            pathStr.startsWith('/api/camera/webrtc') ||
            pathStr.startsWith('/api/camera/diag')
        )) {
            return true;
        }
        return (
            pathStr.startsWith('/api/audio/') ||
            pathStr.startsWith('/api/youtube/rtmp/') ||
            pathStr.startsWith('/api/tiktok/rtmp/') ||
            pathStr.startsWith('/api/instagram/rtmp/') ||
            pathStr.startsWith('/api/youtube/video-diagnostics')
        );
    }

    // 2. 클라우드 서버와 로컬 에이전트 양쪽에 동시 미러링할 경량 상태 경로 판별
    function isDualMirrorStatePath(pathStr, method) {
        if (!pathStr || String(method || 'GET').toUpperCase() === 'GET') return false;
        return (
            pathStr.startsWith('/api/overlay/') ||
            pathStr === '/api/camera/switcher'
        );
    }

    function extractRelativeApiPath(inputUrl) {
        if (!inputUrl) return '';
        const raw = String(inputUrl).trim();
        if (raw.startsWith('/api/')) return raw;
        try {
            const u = new URL(raw, window.location.origin);
            if (u.origin === window.location.origin && u.pathname.startsWith('/api/')) {
                return u.pathname + u.search;
            }
        } catch (e) {}
        return '';
    }

    function buildLocalAgentUrl(relPath) {
        const sep = relPath.includes('?') ? '&' : '?';
        const tokParam = (bridgeState.health && bridgeState.health.cam_token && relPath.startsWith('/api/camera/'))
            ? `&cam_token=${encodeURIComponent(bridgeState.health.cam_token)}`
            : '';
        return `${bridgeState.agentBaseUrl}${relPath}${sep}agent_bridge=1${tokParam}`;
    }

    // 3. window.fetch 인터셉터: 원격 클라우드 접속 시 카메라·오디오·RTMP 요청을 127.0.0.1:5000으로 직결
    const nativeFetch = window.fetch.bind(window);
    window.fetch = async function (input, init) {
        const urlStr = typeof input === 'string' ? input : (input && input.url ? input.url : String(input));
        const method = (init && init.method) ? init.method : (input && input.method ? input.method : 'GET');
        const relPath = extractRelativeApiPath(urlStr);

        if (isRemoteCloudHost && relPath) {
            // (A) 카메라·오디오·WebRTC·FFmpeg RTMP 요청은 클라우드 서버로 절대 보내지 않고 로컬 에이전트(127.0.0.1)로 직결
            if (isLocalAgentExclusivePath(relPath)) {
                if (!bridgeState.connected) {
                    // RTMP 시작 또는 카메라 QR 요청 시 로컬 에이전트 실행 안내 모달 표시
                    if (relPath.includes('/rtmp/start') || relPath.includes('/camera/renew-url')) {
                        openLocalAgentZeroTrafficModal();
                    }
                    // 폴링 요청이 클라우드 서버로 흘러가 영상 트래픽을 유발하는 것을 차단
                    if (relPath.includes('/rtmp/frame') || relPath.includes('/camera/frame') || relPath.includes('/camera/audio')) {
                        return new Response(
                            JSON.stringify({ status: 'agent_offline', zero_traffic_guarded: true }),
                            { status: 200, headers: { 'Content-Type': 'application/json' } }
                        );
                    }
                } else {
                    const targetLocalUrl = buildLocalAgentUrl(relPath);
                    const nextInit = Object.assign({}, init || {});
                    const hdrs = new Headers(nextInit.headers || {});
                    hdrs.set('X-Local-Agent-Bridge', '1');
                    if (bridgeState.health && bridgeState.health.cam_token) {
                        hdrs.set('X-Cam-Token', bridgeState.health.cam_token);
                    }
                    nextInit.headers = hdrs;
                    nextInit.mode = 'cors';
                    return nativeFetch(targetLocalUrl, nextInit);
                }
            }

            // (B) 상품 핀 고정·자막·카메라 스위처 변경 시 클라우드 서버에 저장하면서 로컬 에이전트에도 비동기 미러링
            if (bridgeState.connected && isDualMirrorStatePath(relPath, method)) {
                try {
                    const mirrorUrl = buildLocalAgentUrl(relPath);
                    const mirrorInit = Object.assign({}, init || {});
                    const hdrs = new Headers(mirrorInit.headers || {});
                    hdrs.set('X-Local-Agent-Bridge', '1');
                    mirrorInit.headers = hdrs;
                    mirrorInit.mode = 'cors';
                    nativeFetch(mirrorUrl, mirrorInit).catch(() => {});
                } catch (e) {}
            }
        }

        return nativeFetch(input, init);
    };

    // 4. 카메라 프리뷰 <img> 태그 src를 로컬 에이전트(127.0.0.1:5000) 또는 원격 카메라 서버로 자동 리라이트
    function rewriteCameraPreviewImageSources() {
        if (!isRemoteCloudHost) return;
        const imgIds = ['cam1MultiviewImg', 'cam2MultiviewImg', 'monitorStreamImg', 'subMonitorStreamImg'];
        const isRemoteCamMode = (window.__CAMERA_NETWORK_MODE__ === 'remote_network');
        imgIds.forEach((id) => {
            const el = document.getElementById(id);
            if (!el) return;
            const rawSrc = el.getAttribute('src') || '';
            if (isRemoteCamMode || !bridgeState.connected) {
                if (rawSrc.startsWith(bridgeState.agentBaseUrl + '/api/camera/')) {
                    const rel = rawSrc.substring(bridgeState.agentBaseUrl.length).replace(/([?&])agent_bridge=1(&|$)/, '$1').replace(/[?&]$/, '');
                    el.setAttribute('src', rel);
                }
                return;
            }
            if (rawSrc.startsWith('/api/camera/')) {
                el.setAttribute('src', buildLocalAgentUrl(rawSrc));
            } else if (rawSrc.startsWith(window.location.origin + '/api/camera/')) {
                const rel = rawSrc.substring(window.location.origin.length);
                el.setAttribute('src', buildLocalAgentUrl(rel));
            }
        });
    }

    window.applyCameraNetworkModeRouting = function (mode) {
        window.__CAMERA_NETWORK_MODE__ = mode;
        rewriteCameraPreviewImageSources();
    };

    // MutationObserver로 동적 src 변경(`cam1MultiviewImg.src = '/api/camera/latest-frame...'`) 즉시 로컬 에이전트로 전환
    function installImageSrcObserver() {
        if (!isRemoteCloudHost || typeof MutationObserver === 'undefined') return;
        const observer = new MutationObserver((mutations) => {
            if (!bridgeState.connected || window.__CAMERA_NETWORK_MODE__ === 'remote_network') return;
            for (const m of mutations) {
                if (m.type === 'attributes' && m.attributeName === 'src' && m.target && m.target.tagName === 'IMG') {
                    const val = m.target.getAttribute('src') || '';
                    if (val.startsWith('/api/camera/')) {
                        m.target.setAttribute('src', buildLocalAgentUrl(val));
                    } else if (val.startsWith(window.location.origin + '/api/camera/')) {
                        const rel = val.substring(window.location.origin.length);
                        m.target.setAttribute('src', buildLocalAgentUrl(rel));
                    }
                }
            }
        });
        observer.observe(document.documentElement, {
            attributes: true,
            attributeFilter: ['src'],
            subtree: true,
        });
    }

    // 5. 로컬 에이전트 헬스체크 폴링 (127.0.0.1:5000 -> 폴백 5443)
    async function checkLocalAgentHealth() {
        if (!isRemoteCloudHost) {
            try {
                const res = await nativeFetch('/api/agent/health', {
                    headers: { 'X-Local-Agent-Bridge': '1' },
                });
                if (res.ok) {
                    bridgeState.health = await res.json();
                    bridgeState.connected = true;
                }
            } catch (e) {}
            updateHeaderAgentBadgeUI();
            return true;
        }

        const candidates = [LOCAL_AGENT_HTTP, LOCAL_AGENT_HTTPS];
        for (const base of candidates) {
            try {
                const ctrl = new AbortController();
                const timer = setTimeout(() => ctrl.abort(), 1600);
                const res = await nativeFetch(`${base}/api/agent/health?agent_bridge=1`, {
                    method: 'GET',
                    mode: 'cors',
                    headers: { 'X-Local-Agent-Bridge': '1' },
                    signal: ctrl.signal,
                });
                clearTimeout(timer);
                if (res.ok) {
                    const data = await res.json();
                    if (data && data.status === 'ok') {
                        const wasConnected = bridgeState.connected;
                        bridgeState.connected = true;
                        bridgeState.agentBaseUrl = base;
                        bridgeState.health = data;
                        bridgeState.lastCheckTs = Date.now();
                        updateHeaderAgentBadgeUI();
                        rewriteCameraPreviewImageSources();
                        if (!wasConnected && typeof fetchCameraHostInfo === 'function') {
                            try { fetchCameraHostInfo(); } catch (e) {}
                        }
                        return true;
                    }
                }
            } catch (err) {}
        }

        bridgeState.connected = false;
        updateHeaderAgentBadgeUI();
        return false;
    }

    // 6. 스튜디오 상단 헤더 배지 & 원클릭 실행 모달 UI 렌더링
    function updateHeaderAgentBadgeUI() {
        const btn = document.getElementById('btnLocalAgentZeroTrafficBadge');
        const modalStatus = document.getElementById('localAgentModalStatusBox');
        if (btn) {
            if (bridgeState.connected) {
                const ipTxt = (bridgeState.health && bridgeState.health.local_ip) ? ` · ${bridgeState.health.local_ip}` : '';
                btn.style.background = '#ecfdf5';
                btn.style.borderColor = '#10b981';
                btn.style.color = '#047857';
                btn.innerHTML = `<span>🟢 로컬 에이전트 직결 (서버트래픽 0B${ipTxt})</span>`;
            } else {
                btn.style.background = '#fff1f2';
                btn.style.borderColor = '#f43f5e';
                btn.style.color = '#be123c';
                btn.innerHTML = `<span>🚀 로컬 에이전트 실행 (트래픽 0원)</span>`;
            }
        }
        if (modalStatus) {
            if (bridgeState.connected) {
                const h = bridgeState.health || {};
                modalStatus.style.background = '#ecfdf5';
                modalStatus.style.borderColor = '#10b981';
                modalStatus.style.color = '#065f46';
                modalStatus.innerHTML = `
                    <div style="font-weight:900; font-size:0.92rem;">✅ 로컬 PC 에이전트 연결 완료 (${bridgeState.agentBaseUrl})</div>
                    <div style="font-size:0.8rem; margin-top:4px;">
                        • 로컬 Wi-Fi 카메라 IP: <strong>${h.local_ip || '127.0.0.1'}:${h.https_port || 5443}</strong><br>
                        • FFmpeg 인코더: <strong>${h.ffmpeg_available ? '정상 감지됨' : '확인 필요'}</strong> · 클라우드 서버 영상 트래픽: <strong>0 Byte</strong>
                    </div>
                `;
            } else {
                modalStatus.style.background = '#fffbeb';
                modalStatus.style.borderColor = '#f59e0b';
                modalStatus.style.color = '#92400e';
                modalStatus.innerHTML = `
                    <div style="font-weight:900; font-size:0.92rem;">⚠️ 현재 방송자 PC의 로컬 에이전트(127.0.0.1:5000)가 실행되지 않았습니다.</div>
                    <div style="font-size:0.8rem; margin-top:4px;">
                        아래 <strong>[🚀 로컬 에이전트 원클릭 실행]</strong> 버튼을 누르거나 PC에서 <code>start_local_agent.bat</code>를 실행하면 카메라·RTMP 송출이 서버 트래픽 0원으로 즉시 연결됩니다.
                    </div>
                `;
            }
        }
    }

    function ensureLocalAgentModalMounted() {
        if (document.getElementById('localAgentZeroTrafficModal')) return;
        const modal = document.createElement('div');
        modal.id = 'localAgentZeroTrafficModal';
        modal.className = 'session-modal-backdrop';
        modal.style.display = 'none';
        modal.onclick = function (e) {
            if (e.target === modal) closeLocalAgentZeroTrafficModal();
        };
        modal.innerHTML = `
            <div class="session-modal-card" style="width:100%; max-width:560px; padding:24px; gap:16px; background:#ffffff; color:#0f172a; border:1px solid #cbd5e1;">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <h3 style="font-size:1.12rem; font-weight:900; color:#0f172a; display:flex; align-items:center; gap:8px;">
                        <span>💻 서버 영상 트래픽 0원 — 로컬 PC 에이전트 연결</span>
                    </h3>
                    <button type="button" onclick="closeLocalAgentZeroTrafficModal()" style="background:#f1f5f9; border:1px solid #cbd5e1; color:#334155; padding:4px 10px; border-radius:8px; font-weight:800; cursor:pointer;">✕ 닫기</button>
                </div>

                <div id="localAgentModalStatusBox" style="padding:12px 14px; border-radius:12px; border:1.5px solid #cbd5e1; font-size:0.85rem; line-height:1.5;"></div>

                <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:12px; padding:14px; font-size:0.82rem; color:#334155; line-height:1.6;">
                    <div style="font-weight:900; color:#0f172a; margin-bottom:6px;">💡 왜 로컬 에이전트를 연결하나요?</div>
                    1. <strong>서버 영상 트래픽 0원</strong>: 스마트폰 무선 카메라 수신과 유튜브·틱톡·인스타 FFmpeg 인코딩을 내 PC(<code>127.0.0.1:5000</code>)에서 직접 처리하여 클라우드 서버(<code>49.50.132.140</code>) 영상 트래픽이 <strong>0 Byte</strong>가 됩니다.<br>
                    2. <strong>초저지연 로컬 Wi-Fi 카메라</strong>: 스마트폰 QR 스캔 시 공유기 내부 Wi-Fi(<code>192.168.x.x:5443</code>)로 직결되어 끊김이 없습니다.<br>
                    3. <strong>최초 1회 프로토콜 등록</strong>: 방송자 PC의 프로젝트 폴더에서 <code>register_agent_protocol.bat</code>를 1회 실행해 두면, 앞으로 웹에서 아래 버튼 클릭 한 번으로 자동 실행됩니다.
                </div>

                <div style="display:flex; gap:8px; flex-wrap:wrap;">
                    <a href="golabang-agent://start" onclick="onTriggerLocalAgentProtocol()" style="flex:1; text-align:center; background:#2563eb; color:#ffffff; font-weight:900; font-size:0.9rem; padding:12px 16px; border-radius:10px; text-decoration:none; box-shadow:0 4px 12px rgba(37,99,235,0.25);">
                        🚀 로컬 에이전트 원클릭 실행 (golabang-agent://)
                    </a>
                    <button type="button" onclick="checkLocalAgentHealthNow()" style="background:#f1f5f9; border:1px solid #cbd5e1; color:#0f172a; font-weight:800; font-size:0.85rem; padding:12px 14px; border-radius:10px; cursor:pointer;">
                        🔄 연결 상태 다시 확인
                    </button>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
    }

    window.openLocalAgentZeroTrafficModal = function () {
        ensureLocalAgentModalMounted();
        updateHeaderAgentBadgeUI();
        const m = document.getElementById('localAgentZeroTrafficModal');
        if (m) m.style.display = 'flex';
    };

    window.closeLocalAgentZeroTrafficModal = function () {
        const m = document.getElementById('localAgentZeroTrafficModal');
        if (m) m.style.display = 'none';
    };

    window.checkLocalAgentHealthNow = async function () {
        await checkLocalAgentHealth();
        if (bridgeState.connected && typeof showToast === 'function') {
            showToast('🟢 로컬 PC 에이전트(127.0.0.1:5000)와 직결되었습니다! (서버 영상트래픽 0B)');
        }
    };

    window.onTriggerLocalAgentProtocol = function () {
        let tries = 0;
        const fastPoll = setInterval(async () => {
            tries++;
            const ok = await checkLocalAgentHealth();
            if (ok || tries >= 10) {
                clearInterval(fastPoll);
                if (ok) closeLocalAgentZeroTrafficModal();
            }
        }, 1200);
    };

    document.addEventListener('DOMContentLoaded', () => {
        ensureLocalAgentModalMounted();
        installImageSrcObserver();
        checkLocalAgentHealth();
        setInterval(checkLocalAgentHealth, 3500);
    });
})();
