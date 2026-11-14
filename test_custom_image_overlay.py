import os
import time
import requests
from playwright.sync_api import sync_playwright

BASE_URL = "http://127.0.0.1:5000"

def test_api():
    print("[1] /api/overlay/settings GET 확인 중...")
    res = requests.get(f"{BASE_URL}/api/overlay/settings")
    assert res.status_code == 200, f"Settings GET failed: {res.text}"
    s = res.json()["settings"]
    print("  현재 설정:", s.get("show_custom_image"), s.get("custom_image_url"), s.get("custom_image_pos"))
    assert "show_custom_image" in s, "show_custom_image 누락"
    assert "custom_image_pos" in s, "custom_image_pos 누락"

    print("[2] /api/overlay/upload-image 테스트 이미지 업로드 중...")
    import io
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (240, 90), (15, 23, 42, 230))
    d = ImageDraw.Draw(img)
    d.rectangle([(4, 4), (235, 85)], outline=(56, 189, 248), width=3)
    d.text((25, 32), "★ LIVE SPONSOR ★", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    files = {"file": ("test_logo.png", buf, "image/png")}
    upload_res = requests.post(f"{BASE_URL}/api/overlay/upload-image", files=files)
    assert upload_res.status_code == 200, f"Upload failed: {upload_res.text}"
    upload_data = upload_res.json()
    assert upload_data["status"] == "ok"
    uploaded_url = upload_data["url"]
    print(f"  -> 업로드 성공: {uploaded_url}")
    return uploaded_url

def test_ui(uploaded_url):
    print("[3] Playwright 브라우저 UI 검증 시작...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        page = context.new_page()

        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.on("console", lambda msg: print(f"  [Console {msg.type}] {msg.text}") if msg.type == "error" else None)

        # 1. 스튜디오 페이지 접속
        page.goto(f"{BASE_URL}/overlay/studio")
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(1000)

        # 2. 테마/위치 탭 클릭
        page.click("button:has-text('🎨 테마/위치')")
        page.wait_for_timeout(500)

        # 3. 기타 토글 및 설정 카드 확인
        toggle = page.locator("#toggleCustomImage")
        assert toggle.count() > 0, "기타 토글 엘리먼트(#toggleCustomImage)를 찾을 수 없습니다."

        # 4. 이미지 URL 입력 및 적용
        page.fill("#customImageUrlInput", uploaded_url)
        page.click("#btnApplyCustomImageUrl")
        page.wait_for_timeout(600)

        # 토글 활성화 여부 확인
        is_checked = toggle.is_checked()
        print(f"  -> 토글 상태: {is_checked}")
        assert is_checked, "URL 적용 후 토글이 ON 상태가 되어야 합니다."

        # 미리보기 박스 표시 확인
        preview_box = page.locator("#customImagePreviewBox")
        assert preview_box.is_visible(), "미리보기 박스가 표시되어야 합니다."

        # 위치를 'top-right'로 설정
        page.select_option("#customImagePosSelect", "top-right")
        page.wait_for_timeout(500)

        # 5. 스튜디오 내 아이프레임 내부 확인
        iframe_element = page.locator("#overlayIframe")
        iframe = iframe_element.content_frame
        assert iframe is not None, "아이프레임 컨텐츠 로드 실패"

        custom_wrap = iframe.locator("#customOverlayImageWrapper")
        assert custom_wrap.is_visible(), "아이프레임 내 커스텀 이미지 래퍼가 표시되어야 합니다."
        img_src = iframe.locator("#customOverlayImage").get_attribute("src")
        print(f"  -> 아이프레임 내 이미지 src: {img_src}")
        assert uploaded_url in img_src, "이미지 URL이 일치하지 않습니다."

        # 스크린샷 1: 16:9 가로 모드에서 커스텀 이미지 오버레이
        screenshot_169 = r"C:\Users\USER\.gemini\antigravity-ide\brain\5ee14c29-c3bb-45b6-a23a-3431dfb75248\verified_custom_overlay_169.png"
        page.screenshot(path=screenshot_169)
        print(f"  -> 16:9 스튜디오 스크린샷 저장 완료: {screenshot_169}")

        # 6. 세로 모드(9:16)로 변경 후 오버레이 확인
        page.click("button:has-text('📺 화면/카메라')")
        page.wait_for_timeout(300)
        page.select_option("#aspectRatioSelect", "9:16")
        page.wait_for_timeout(800)

        # 스크린샷 2: 9:16 세로 모드에서 커스텀 이미지 오버레이
        screenshot_916 = r"C:\Users\USER\.gemini\antigravity-ide\brain\5ee14c29-c3bb-45b6-a23a-3431dfb75248\verified_custom_overlay_916.png"
        page.screenshot(path=screenshot_916)
        print(f"  -> 9:16 스튜디오 스크린샷 저장 완료: {screenshot_916}")

        # 7. 실제 송출 URL (/overlay/obs) 직접 열어 독립 검증
        obs_page = context.new_page()
        obs_page.goto(f"{BASE_URL}/overlay/obs")
        obs_page.wait_for_timeout(800)
        obs_wrap = obs_page.locator("#customOverlayImageWrapper")
        assert obs_wrap.is_visible(), "실제 OBS 송출창(/overlay/obs)에서도 커스텀 이미지가 보여야 합니다."
        print("  -> /overlay/obs 실제 송출창 커스텀 이미지 표시 확인 완료")
        toast_top = obs_page.locator("#toastContainer").evaluate("el => window.getComputedStyle(el).top")
        print("  -> obs_page toastContainer top:", toast_top)
        body_class = obs_page.locator("body").get_attribute("class")
        print("  -> obs_page body class:", body_class)

        screenshot_obs = r"C:\Users\USER\.gemini\antigravity-ide\brain\5ee14c29-c3bb-45b6-a23a-3431dfb75248\verified_obs_overlay_standalone.png"
        obs_page.screenshot(path=screenshot_obs)
        print(f"  -> OBS 송출창 스크린샷 저장 완료: {screenshot_obs}")

        assert len(errors) == 0, f"브라우저 콘솔 오류 발생: {errors}"
        browser.close()

if __name__ == "__main__":
    url = test_api()
    test_ui(url)
    print("\n[SUCCESS] ALL TESTS AND PLAYWRIGHT UI VERIFICATIONS PASSED!")
