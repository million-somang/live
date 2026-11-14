# -*- coding: utf-8 -*-
"""
OBS 오버레이 듀얼 스튜디오 (세팅창 / 송출창 분리 & 실제 화면 연동) 검증 테스트
1. /overlay/studio (세팅 전용 창) HTTP 200 & 필수 UI 마크업 검사
2. /overlay/obs (실제 송출 전용 창) HTTP 200 & Clean 뷰 (툴바 제거) & BroadcastChannel 수신부 검사
3. /api/overlay/settings GET/POST 환경설정 저장/조회 무결성 검사
4. /api/overlay/ticker 자막 변경 API 검사
5. 방송 관제탑(/broadcast) 연동 링크 검사
"""
import sys
import json
import urllib.request

sys.stdout.reconfigure(encoding='utf-8')

BASE_URL = "http://127.0.0.1:5000"

def test_overlay_studio():
    print("[1] GET /overlay/studio (세팅 전용 창) 검증 중...")
    url = f"{BASE_URL}/overlay/studio"
    req = urllib.request.Request(url, headers={'User-Agent': 'TestClient/1.0'})
    with urllib.request.urlopen(req, timeout=5) as res:
        assert res.status == 200, f"Expected 200, got {res.status}"
        html = res.read().decode('utf-8')
        assert "OBS 오버레이 세팅 스튜디오" in html, "FAIL: 스튜디오 타이틀 누락"
        assert "컴퓨터 / OBS 실제 화면 불러오기" in html, "FAIL: 실제 화면 불러오기 섹션 누락"
        assert "navigator.mediaDevices.getDisplayMedia" in html, "FAIL: getDisplayMedia API 누락"
        assert "obs_overlay_sync" in html, "FAIL: BroadcastChannel 누락"
        assert "overlayIframe" in html, "FAIL: 16:9 모니터 iframe 누락"
        print("  -> 세팅 스튜디오 200 OK & 16:9 모니터, getDisplayMedia, BroadcastChannel 탑재 확인 완료")

def test_overlay_obs_clean():
    print("[2] GET /overlay/obs (실제 송출 전용 Clean 창) 검증 중...")
    url = f"{BASE_URL}/overlay/obs"
    req = urllib.request.Request(url, headers={'User-Agent': 'TestClient/1.0'})
    with urllib.request.urlopen(req, timeout=5) as res:
        assert res.status == 200, f"Expected 200, got {res.status}"
        html = res.read().decode('utf-8')
        assert "overlay-sim-toolbar" not in html, "FAIL: 송출창에 시뮬레이터 툴바가 여전히 남아있음"
        assert "obs_overlay_sync" in html, "FAIL: BroadcastChannel 동기화 수신부 누락"
        assert "bg-transparent" in html, "FAIL: 투명 배경 기본 모드 누락"
        assert "overlayCamVideo" in html, "FAIL: 배경 비디오 레이어 누락"
        print("  -> 실제 송출창 200 OK & 시뮬레이터 툴바 완전 제거(Clean) & 실시간 동기화 수신부 확인 완료")

def test_overlay_settings_api():
    print("[3] /api/overlay/settings GET & POST API 검증 중...")
    # 1. POST 새 설정 저장
    post_url = f"{BASE_URL}/api/overlay/settings"
    payload = {
        "bg_mode": "chromakey",
        "product_position": "bottom-right",
        "theme": "vivid-pink",
        "show_timer": True,
        "show_toast": True
    }
    req = urllib.request.Request(
        post_url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=5) as res:
        assert res.status == 200, f"POST Expected 200, got {res.status}"
        data = json.loads(res.read().decode('utf-8'))
        assert data["status"] == "ok"
        assert data["settings"]["bg_mode"] == "chromakey"
        assert data["settings"]["product_position"] == "bottom-right"
        assert data["settings"]["theme"] == "vivid-pink"
        print("  -> POST /api/overlay/settings 성공")

    # 2. GET 설정 조회 검증
    get_url = f"{BASE_URL}/api/overlay/settings"
    req_get = urllib.request.Request(get_url)
    with urllib.request.urlopen(req_get, timeout=5) as res:
        assert res.status == 200
        get_data = json.loads(res.read().decode('utf-8'))
        assert get_data["settings"]["bg_mode"] == "chromakey"
        assert get_data["settings"]["product_position"] == "bottom-right"
        print("  -> GET /api/overlay/settings 영구 저장 및 반환 검증 완료")

def test_overlay_ticker_api():
    print("[4] POST /api/overlay/ticker API 검증 중...")
    url = f"{BASE_URL}/api/overlay/ticker"
    payload = {"message": "테스트 공지 자막입니다. 실시간 0ms 송출!"}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=5) as res:
        assert res.status == 200
        data = json.loads(res.read().decode('utf-8'))
        assert data["status"] == "ok"
        assert data["message"] == "테스트 공지 자막입니다. 실시간 0ms 송출!"
        print("  -> 띠자막 메시지 즉시 갱신 API 성공")

def test_broadcast_links():
    print("[5] 전체 페이지(포털, 관제탑, GNB 헤더)의 OBS 오버레이 클릭 시 스튜디오(/overlay/studio) 이동 검증 중...")
    
    # 1. 메인 포털 (/)
    with urllib.request.urlopen(f"{BASE_URL}/", timeout=5) as res:
        assert res.status == 200
        html = res.read().decode('utf-8')
        assert 'href="/overlay/studio"' in html, "FAIL: 메인 포털 카드에 /overlay/studio 링크 누락"
        print("  -> 메인 포털(/)의 OBS 오버레이 카드 클릭 시 /overlay/studio 이동 확인 완료")

    # 2. 웹방송 관제탑 (/broadcast)
    with urllib.request.urlopen(f"{BASE_URL}/broadcast", timeout=5) as res:
        assert res.status == 200
        html = res.read().decode('utf-8')
        assert 'href="/overlay/studio"' in html, "FAIL: 관제탑 상단 버튼에 /overlay/studio 링크 누락"
        print("  -> 방송 관제탑(/broadcast) 상단 및 사이드바에서 /overlay/studio 이동 확인 완료")

    # 3. 공통 GNB 헤더 (/inventory)
    with urllib.request.urlopen(f"{BASE_URL}/inventory", timeout=5) as res:
        assert res.status == 200
        html = res.read().decode('utf-8')
        assert 'href="/overlay/studio"' in html, "FAIL: GNB 헤더에 /overlay/studio 링크 누락"
        print("  -> 전 페이지 공통 GNB 헤더의 [🖥️ OBS 오버레이 스튜디오] 링크 연결 확인 완료")

if __name__ == "__main__":
    test_overlay_studio()
    test_overlay_obs_clean()
    test_overlay_settings_api()
    test_overlay_ticker_api()
    test_broadcast_links()
    print("\n========================================================")
    print("🎉 ALL OBS OVERLAY STUDIO & CLEAN DISPLAY TESTS PASSED 100%!")
    print("========================================================")
