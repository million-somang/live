import sys
import json
import urllib.request
import urllib.parse

sys.stdout.reconfigure(encoding='utf-8')

BASE_URL = "http://127.0.0.1:5000"

def get(path):
    req = urllib.request.Request(f"{BASE_URL}{path}")
    with urllib.request.urlopen(req) as resp:
        return resp.status, resp.read().decode('utf-8')

def post_json(path, data):
    body = json.dumps(data).encode('utf-8')
    req = urllib.request.Request(f"{BASE_URL}{path}", data=body, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.loads(resp.read().decode('utf-8'))

def main():
    print("[1] Verifying /host page...")
    status, html = get("/host")
    assert status == 200, f"Expected 200, got {status}"
    assert "pdCueBanner" in html, "pdCueBanner element missing in /host"
    assert "pinnedQuestionBox" in html, "pinnedQuestionBox element missing in /host"
    assert "hostTimerBadge" in html, "hostTimerBadge element missing in /host"
    assert "btnScale160" in html, "btnScale160 element missing in /host"
    print("   -> /host page verified OK!")

    print("[1-1] Verifying /broadcast page has director studio...")
    status, b_html = get("/broadcast")
    assert status == 200, f"Expected 200, got {status}"
    assert "pdCueInput" in b_html, "pdCueInput missing in /broadcast"
    assert "sendPDCue" in b_html, "sendPDCue function missing in /broadcast"
    assert "pushQuestionToHost" in b_html, "pushQuestionToHost function missing in /broadcast"
    assert "startHostTimer" in b_html, "startHostTimer function missing in /broadcast"
    print("   -> /broadcast page verified OK!")

    print("[2] Testing POST /api/host/cue (URGENT priority)...")
    status, res = post_json("/api/host/cue", {
        "cue_text": "🚨 긴급 알림: 메인 카메라 1번 정면 응시 부탁드립니다!",
        "priority": "URGENT"
    })
    assert res.get("status") == "ok", f"Cue post failed: {res}"
    print(f"   -> Cue posted: {res}")

    print("[3] Testing POST /api/host/pin-question...")
    status, res = post_json("/api/host/pin-question", {
        "user": "행복한쇼퍼",
        "question": "오늘 결제하면 내일 바로 출발하나요? 선물용이라 급해요!",
        "platform": "네이버"
    })
    assert res.get("status") == "ok", f"Pin question failed: {res}"
    print(f"   -> Question pinned: {res}")

    print("[4] Testing POST /api/host/timer (start 3 min timer)...")
    status, res = post_json("/api/host/timer", {
        "action": "start",
        "minutes": 3,
        "label": "🔥 3분 마감 특가"
    })
    assert res.get("status") == "ok", f"Timer start failed: {res}"
    print(f"   -> Timer started: {res}")

    print("[5] Verifying GET /api/host/data sync payload...")
    status, raw_data = get("/api/host/data")
    data = json.loads(raw_data)
    
    assert data.get("latest_cue") is not None, "latest_cue is missing"
    assert data["latest_cue"].get("priority") == "URGENT", f"Expected URGENT, got {data['latest_cue'].get('priority')}"
    print(f"   -> Verified latest_cue: [{data['latest_cue']['priority']}] {data['latest_cue']['cue_text']}")

    assert data.get("pinned_question") is not None, "pinned_question is missing"
    assert data["pinned_question"]["user"] == "행복한쇼퍼", "pinned_question user mismatch"
    print(f"   -> Verified pinned_question: {data['pinned_question']['user']} - {data['pinned_question']['question']}")

    assert data.get("timer") is not None, "timer is missing"
    assert data["timer"].get("active") is True, f"timer is not active: {data['timer']}"
    assert data["timer"].get("remaining_seconds", 0) > 0, "timer remaining_seconds should be > 0"
    print(f"   -> Verified timer: {data['timer'].get('label')} (remaining: {data['timer'].get('remaining_seconds')}s)")

    print("[6] Testing POST /api/host/clear-question...")
    status, res = post_json("/api/host/clear-question", {})
    assert res.get("status") == "ok", "Clear question failed"
    
    status, raw_data = get("/api/host/data")
    data = json.loads(raw_data)
    assert data.get("pinned_question") is None, "pinned_question should be None after clear"
    print("   -> Pinned question cleared successfully!")

    print("[7] Testing POST /api/host/clear-cues...")
    status, res = post_json("/api/host/clear-cues", {})
    assert res.get("status") == "ok", "Clear cues failed"
    print("   -> Cues cleared successfully!")

    print("\n==========================================")
    print("🎉 ALL HOST DIRECTOR API TESTS PASSED 100%!")
    print("==========================================")

if __name__ == "__main__":
    main()
