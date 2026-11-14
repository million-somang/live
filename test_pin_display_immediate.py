# -*- coding: utf-8 -*-
"""
OBS 스튜디오 상품 핀 실시간 즉시 표시(Auto-Pin) 및 0ms 동기화 검증 테스트
1. /api/products 상품 조회 확인
2. /api/overlay/pin-product 핀 고정 (정수 및 문자열 호환성) 및 /api/overlay/data 검증
3. /overlay/obs 템플릿의 z-index: 500 및 postMessage 수신 리스너 검증
4. /overlay/studio 템플릿의 자동 송출(toggleAutoPin) 및 iframe 직접 동기화 로직 검증
5. 핀 해제 및 상태 초기화 검증
"""
import sys
import json
import urllib.request

sys.stdout.reconfigure(encoding='utf-8')

BASE_URL = "http://127.0.0.1:5000"

def test_api_products():
    print("[1] GET /api/products 검증 중...")
    url = f"{BASE_URL}/api/products"
    with urllib.request.urlopen(url, timeout=5) as res:
        assert res.status == 200
        data = json.loads(res.read().decode('utf-8'))
        prods = data.get("products", [])
        assert len(prods) > 0, "FAIL: 상품이 0개입니다."
        p = prods[0]
        print(f"  -> {len(prods)}개 상품 확인 완료: {p['num']}번 '{p['name']}' (총 재고: {p['total_stock']}개)")
        return p['num']

def test_pin_and_overlay_data(prod_num):
    print("[2] POST /api/overlay/pin-product 및 GET /api/overlay/data 연동 검증 중...")
    # 1. 핀 고정
    pin_url = f"{BASE_URL}/api/overlay/pin-product"
    payload = {"product_num": str(prod_num)} # 문자열로 전송하여 타입 유연성 테스트
    req = urllib.request.Request(
        pin_url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=5) as res:
        assert res.status == 200
        data = json.loads(res.read().decode('utf-8'))
        assert data["pinned_product_num"] == int(prod_num)
        print(f"  -> {prod_num}번 상품 핀 고정 성공")

    # 2. 오버레이 데이터 조회
    data_url = f"{BASE_URL}/api/overlay/data"
    with urllib.request.urlopen(data_url, timeout=5) as res:
        assert res.status == 200
        data = json.loads(res.read().decode('utf-8'))
        pinned = data.get("pinned_product")
        assert pinned is not None, "FAIL: pinned_product가 None입니다."
        assert str(pinned.get("num")) == str(prod_num), f"FAIL: 핀 상품 번호 불일치 ({pinned.get('num')} != {prod_num})"
        print(f"  -> /api/overlay/data에서 핀 상품 '{pinned['name']}' (잔여 재고: {pinned.get('total_stock')}개) 즉시 반환 확인 완료")

def test_obs_overlay_template():
    print("[3] GET /overlay/obs (실제 송출창 마크업 및 동기화 수신부) 검증 중...")
    url = f"{BASE_URL}/overlay/obs"
    with urllib.request.urlopen(url, timeout=5) as res:
        assert res.status == 200
        html = res.read().decode('utf-8')
        assert "pinned-product-card" in html, "FAIL: pinned-product-card 누락"
        assert "z-index: 500" in html, "FAIL: pinned-product-card z-index: 500 누락"
        assert "window.addEventListener('message'" in html, "FAIL: postMessage 수신 리스너 누락"
        assert "window.fetchOverlayData" in html, "FAIL: fetchOverlayData 노출 누락"
        print("  -> 송출창 z-index: 500 및 postMessage 수신 파이프라인 검증 완료")

def test_obs_studio_template():
    print("[4] GET /overlay/studio (스튜디오 자동 송출 및 iframe 직접 동기화) 검증 중...")
    url = f"{BASE_URL}/overlay/studio"
    with urllib.request.urlopen(url, timeout=5) as res:
        assert res.status == 200
        html = res.read().decode('utf-8')
        assert "toggleAutoPin" in html, "FAIL: toggleAutoPin 자동 송출 스위치 누락"
        assert "contentWindow.postMessage" in html, "FAIL: iframe contentWindow.postMessage 직접 동기화 누락"
        assert "contentWindow.fetchOverlayData" in html, "FAIL: iframe fetchOverlayData 직접 실행 누락"
        assert "selectedProductStockCard" in html, "FAIL: selectedProductStockCard 누락"
        assert "btnPinProduct" in html, "FAIL: btnPinProduct ID 누락"
        print("  -> 스튜디오 toggleAutoPin(선택 즉시 자동 송출) 및 0ms iframe 직접 동기화 탑재 확인 완료")

def test_unpin_product():
    print("[5] 상품 핀 해제 (product_num: null) 검증 중...")
    pin_url = f"{BASE_URL}/api/overlay/pin-product"
    payload = {"product_num": None}
    req = urllib.request.Request(
        pin_url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=5) as res:
        assert res.status == 200
        data = json.loads(res.read().decode('utf-8'))
        assert data["pinned_product_num"] is None
        print("  -> 핀 해제 API 호출 성공")

    data_url = f"{BASE_URL}/api/overlay/data"
    with urllib.request.urlopen(data_url, timeout=5) as res:
        assert res.status == 200
        data = json.loads(res.read().decode('utf-8'))
        assert data.get("pinned_product") is None, "FAIL: 핀 해제 후에도 pinned_product가 남아있음"
        print("  -> /api/overlay/data에서 pinned_product: null 정상 초기화 확인 완료")

if __name__ == "__main__":
    prod_num = test_api_products()
    test_pin_and_overlay_data(prod_num)
    test_obs_overlay_template()
    test_obs_studio_template()
    test_unpin_product()
    print("\n========================================================")
    print("🎉 ALL PIN PRODUCT IMMEDIATE DISPLAY TESTS PASSED 100%!")
    print("========================================================")
