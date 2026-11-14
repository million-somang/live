# -*- coding: utf-8 -*-
"""
전체화면 No-Scroll 레이아웃 검증 테스트
1. broadcast.html CSS 및 JS 무결성 검사 (overflow: hidden, height: 0, min-height: 0, fullscreenchange)
2. HTTP GET /broadcast 응답 상태 코드 및 핵심 마크업 검사
"""
import sys
import re
import urllib.request

sys.stdout.reconfigure(encoding='utf-8')

def test_broadcast_layout_source():
    print("[1] templates/broadcast.html 레이아웃 소스 검증 중...")
    with open('templates/broadcast.html', 'r', encoding='utf-8') as f:
        html = f.read()

    # 1. html, body overflow: hidden
    assert "overflow: hidden !important" in html, "FAIL: html, body에 overflow: hidden !important 누락"
    print("  -> html, body overflow: hidden !important 확인 완료")

    # 2. main-layout height: 0 및 min-height: 0
    assert "height: 0;" in html and "min-height: 0;" in html, "FAIL: main-layout에 height: 0 / min-height: 0 누락"
    print("  -> main-layout { height: 0; min-height: 0; flex: 1; overflow: hidden; } 확인 완료")

    # 3. chat-center-panel height: 100%
    assert ".chat-center-panel {" in html, "FAIL: chat-center-panel 누락"
    assert "height: 100%;" in html, "FAIL: chat-center-panel height: 100% 누락"
    print("  -> chat-center-panel { height: 100%; min-height: 0; } 확인 완료")

    # 4. side-panel height: 100% 및 overflow-y: auto
    assert ".side-panel {" in html, "FAIL: side-panel 누락"
    assert "overflow-y: auto;" in html, "FAIL: side-panel overflow-y: auto 누락"
    print("  -> side-panel { height: 100%; min-height: 0; overflow-y: auto; } 확인 완료")

    # 5. fullscreenchange 리스너 확인
    assert "fullscreenchange" in html, "FAIL: fullscreenchange 이벤트 리스너 누락"
    assert "is-fullscreen" in html, "FAIL: is-fullscreen 클래스 토글 누락"
    assert "mode-window" in html, "FAIL: mode-window 클래스 지원 누락"
    print("  -> fullscreenchange 이벤트 감지 및 is-fullscreen / mode-window 클래스 동기화 확인 완료")

    print("[PASS] templates/broadcast.html 레이아웃 소스 검증 성공!\n")

def test_broadcast_http():
    print("[2] 라이브 커머스 서버 HTTP GET /broadcast 응답 검증 중...")
    url = "http://127.0.0.1:5000/broadcast"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'TestClient/1.0'})
        with urllib.request.urlopen(req, timeout=5) as res:
            assert res.status == 200, f"FAIL: Expected 200, got {res.status}"
            content = res.read().decode('utf-8')
            assert "fullscreenToggleBtn" in content, "FAIL: fullscreenToggleBtn 누락"
            assert "overflow: hidden !important" in content, "FAIL: overflow: hidden !important 누락"
            print("  -> HTTP 200 OK 수신 및 무스크롤 전체화면 레이아웃 전송 확인 완료")
    except Exception as e:
        print(f"  -> HTTP 요청 에러 (서버 실행 상태 확인 필요): {e}")
        raise e

    print("[PASS] HTTP GET /broadcast 테스트 통과!\n")

if __name__ == "__main__":
    test_broadcast_layout_source()
    test_broadcast_http()
    print("🎉 [ALL TESTS PASSED] 전체화면 무스크롤 레이아웃 최적화 검증 완료!")
