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

def post_raw(path, data, content_type='application/octet-stream'):
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, headers={'Content-Type': content_type})
    with urllib.request.urlopen(req) as resp:
        return resp.status, resp.read().decode('utf-8')

def main():
    print("[1] Verifying /cam (Smartphone Wireless Camera View)...")
    status, html = get("/cam")
    assert status == 200, f"Expected 200, got {status}"
    assert "cameraVideo" in html, "cameraVideo missing in /cam"
    assert "captureCanvas" in html, "captureCanvas missing in /cam"
    assert "switchCamera" in html, "switchCamera function missing in /cam"
    assert "toggleStreaming" in html, "toggleStreaming function missing in /cam"
    print("   -> /cam page verified OK!")

    print("[2] Testing GET /api/camera/host-info...")
    status, json_str = get("/api/camera/host-info")
    assert status == 200, f"Expected 200, got {status}"
    data = json.loads(json_str)
    assert data.get("status") == "ok", f"Expected status ok, got {data}"
    assert "local_ip" in data, "local_ip missing"
    assert "cam_url" in data, "cam_url missing"
    print(f"   -> Host Info verified: Local IP={data['local_ip']}, Cam URL={data['cam_url']}")

    print("[3] Testing POST /api/camera/frame (Upload frame)...")
    dummy_jpeg = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00\xff\xd9'
    status, res_str = post_raw("/api/camera/frame", dummy_jpeg, content_type='image/jpeg')
    res = json.loads(res_str)
    assert res.get("status") == "ok", f"Frame upload failed: {res}"
    print("   -> Camera frame uploaded successfully!")

    print("[4] Testing GET /api/camera/latest-frame...")
    req = urllib.request.Request(f"{BASE_URL}/api/camera/latest-frame")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200, f"Expected 200, got {resp.status}"
        assert resp.headers.get("Content-Type") == "image/jpeg", "Expected image/jpeg Content-Type"
        content = resp.read()
        assert content == dummy_jpeg, "Frame data mismatch"
    print("   -> Latest frame verified successfully!")

    print("[5] Verifying /overlay/obs (Clean Camera Layer) & /overlay/studio (Camera Controls & QR)...")
    status, html = get("/overlay/obs")
    assert status == 200, f"Expected 200, got {status}"
    assert "overlayCamVideo" in html, "overlayCamVideo element missing"
    assert "overlayStreamImg" in html, "overlayStreamImg element missing"

    # 세팅 스튜디오 검증
    status_st, html_st = get("/overlay/studio")
    assert status_st == 200, f"Expected 200, got {status_st}"
    assert "btnWebcam" in html_st, "btnWebcam missing in studio"
    assert "btnMobileCam" in html_st, "btnMobileCam missing in studio"
    assert "qrModal" in html_st, "qrModal missing in studio"
    print("   -> /overlay/obs and /overlay/studio camera integration verified OK!")

    print("[6] Verifying /host (Showhost View with Monitoring PiP)...")
    status, html = get("/host")
    assert status == 200, f"Expected 200, got {status}"
    assert "btnCameraPip" in html, "btnCameraPip missing in /host"
    assert "hostCameraPip" in html, "hostCameraPip container missing in /host"
    assert "hostPipVideo" in html, "hostPipVideo missing in /host"
    assert "hostPipStreamImg" in html, "hostPipStreamImg missing in /host"
    assert "toggleHostPip" in html, "toggleHostPip function missing in /host"
    assert "cycleHostPipSize" in html, "cycleHostPipSize function missing in /host"
    print("   -> /host page verified OK!")

    print("\n========================================================")
    print("🎉 ALL SMARTPHONE & WEBCAM CAMERA INTEGRATION TESTS PASSED 100%!")
    print("========================================================")

if __name__ == "__main__":
    main()
