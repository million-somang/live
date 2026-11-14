# -*- coding: utf-8 -*-
"""
OBS 오버레이 스튜디오 메뉴 한줄 정렬 & 실시간 재고 연동 상품 핀 검증 테스트
1. /api/products API 200 OK 및 total_stock / options 반환 검증
2. /overlay/studio 탭 메뉴 한줄 정렬(white-space: nowrap) 및 재고 상세 카드 마크업 검증
3. /api/overlay/pin-product 연동 및 /api/overlay/data 실시간 반영 검증
"""
import sys
import json
import urllib.request

sys.stdout.reconfigure(encoding='utf-8')

BASE_URL = "http://127.0.0.1:5000"

def test_api_products():
    print("[1] GET /api/products (상품 및 등록 재고 API) 검증 중...")
    url = f"{BASE_URL}/api/products"
    req = urllib.request.Request(url, headers={'User-Agent': 'TestClient/1.0'})
    with urllib.request.urlopen(req, timeout=5) as res:
        assert res.status == 200, f"Expected 200, got {res.status}"
        data = json.loads(res.read().decode('utf-8'))
        assert data["status"] == "ok", "FAIL: status not ok"
        products = data.get("products", [])
        assert len(products) > 0, "FAIL: 등록된 상품이 0개입니다."
        p = products[0]
        assert "num" in p, "FAIL: 상품 번호(num) 누락"
        assert "name" in p, "FAIL: 상품명(name) 누락"
        assert "price" in p, "FAIL: 판매가(price) 누락"
        assert "total_stock" in p, "FAIL: 총 잔여 재고(total_stock) 누락"
        assert "options" in p, "FAIL: 옵션 목록(options) 누락"
        print(f"  -> {len(products)}개 상품 로드 성공: {p['num']}번 '{p['name']}' (잔여 재고: {p['total_stock']}개, 옵션 {len(p['options'])}개)")

def test_studio_tabs_and_stock_markup():
    print("[2] GET /overlay/studio (탭 메뉴 한줄 정렬 & 재고 카드 마크업) 검증 중...")
    url = f"{BASE_URL}/overlay/studio"
    req = urllib.request.Request(url, headers={'User-Agent': 'TestClient/1.0'})
    with urllib.request.urlopen(req, timeout=5) as res:
        assert res.status == 200, f"Expected 200, got {res.status}"
        html = res.read().decode('utf-8')
        
        # 1. 탭 2줄 줄바꿈 방지 스타일 검증
        assert "white-space: nowrap !important" in html, "FAIL: tab-btn에 white-space: nowrap !important 누락"
        assert "repeat(4, 1fr)" in html, "FAIL: studio-tabs에 repeat(4, 1fr) 누락"
        print("  -> 탭 버튼 4종 2줄 줄바꿈 원천 차단(white-space: nowrap !important) 확인 완료")

        # 2. 재고 상세 카드 마크업 검증
        assert "selectedProductStockCard" in html, "FAIL: selectedProductStockCard 누락"
        assert "stockCardTotalTag" in html, "FAIL: stockCardTotalTag 누락"
        assert "stockCardOptionsList" in html, "FAIL: stockCardOptionsList 누락"
        assert "onProductSelectChange" in html, "FAIL: onProductSelectChange 함수 누락"
        print("  -> 선택한 상품의 실시간 재고 & 옵션 상세 카드(selectedProductStockCard) 마크업 확인 완료")

def test_pin_product_integration():
    print("[3] POST /api/overlay/pin-product 및 GET /api/overlay/data 실시간 연동 검증 중...")
    # 1. 상품 핀 고정 (1번 상품)
    pin_url = f"{BASE_URL}/api/overlay/pin-product"
    payload = {"product_num": 1}
    req = urllib.request.Request(
        pin_url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=5) as res:
        assert res.status == 200
        data = json.loads(res.read().decode('utf-8'))
        assert data["pinned_product_num"] == 1
        print("  -> 1번 상품 핀 고정 성공")

    # 2. /api/overlay/data 조회 시 pinned_product와 잔여 재고 확인
    data_url = f"{BASE_URL}/api/overlay/data"
    with urllib.request.urlopen(data_url, timeout=5) as res:
        assert res.status == 200
        data = json.loads(res.read().decode('utf-8'))
        p = data.get("pinned_product")
        assert p is not None, "FAIL: pinned_product가 None입니다."
        assert p.get("num") == 1, "FAIL: 핀 상품 번호 불일치"
        print(f"  -> /api/overlay/data에서 핀 상품 '{p['name']}' (총 잔여 재고: {p.get('total_stock')}개) 송출 확인 완료")

if __name__ == "__main__":
    test_api_products()
    test_studio_tabs_and_stock_markup()
    test_pin_product_integration()
    print("\n========================================================")
    print("🎉 ALL STUDIO STOCK & SINGLE-LINE TABS TESTS PASSED 100%!")
    print("========================================================")
