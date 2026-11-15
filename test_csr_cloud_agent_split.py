# -*- coding: utf-8 -*-
"""
CSR 경량 클라우드 쇼핑몰 서버 + 방송자 PC 로컬 라이브 에이전트 2단 분리 통합 검증 테스트
"""
import os
import sys
import time
import base64
import tempfile

# 테스트용 임시 쇼핑몰 DB 경로 분리 (운영 shopping_mall.db 오염 방지)
_tmp_dir = tempfile.mkdtemp(prefix="mall_csr_test_")
os.environ["MALL_DB_PATH"] = os.path.join(_tmp_dir, "test_shopping_mall.db")
os.environ["MALL_MODE"] = "cloud"
os.environ["OBS_PULL_ENABLED"] = "0"
os.environ["MALL_SYNC_SECRET"] = "test-cloud-secret-999"

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
MALL_DIR = os.path.join(ROOT_DIR, "shopping_mall")
sys.path.insert(0, MALL_DIR)
sys.path.insert(0, ROOT_DIR)

import mall_database
import app as mall_app
import live_commerce_server


def run_tests():
    mall_database.init_mall_db()

    with mall_app.app.test_client() as client:
        # 1. 잘못된 시크릿 키 차단(403) 검증
        r_unauth = client.post("/api/sync/from-obs", json={"action": "live_state", "is_live": True})
        assert r_unauth.status_code == 403, f"Expected 403, got {r_unauth.status_code}"

        # 2. Outbound bulk_sync + image_b64 + live_state + youtube_embed_url 검증
        tiny_png_b64 = base64.b64encode(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        ).decode("ascii")
        sample_products = [
            {
                "id": i,
                "num": i,
                "name": f"테스트 라이브 상품 {i}",
                "category": "OUTER",
                "consumer_price": 100000,
                "price": 70000,
                "image_url": "/static/uploads/products/test_csr_sample.png" if i == 1 else "",
                "specs": "울 80%, 캐시미어 20% 프리미엄 코트 상세 설명 텍스트" * 5,
                "options": [{"color": "블랙", "size": "L", "stock": 15}]
            }
            for i in range(1, 22)
        ]

        headers = {"X-Mall-Sync-Secret": "test-cloud-secret-999"}
        r_sync = client.post(
            "/api/sync/from-obs",
            headers=headers,
            json={
                "action": "bulk_sync",
                "products": sample_products,
                "images_b64": {"test_csr_sample.png": tiny_png_b64},
                "live_state": {
                    "is_live": True,
                    "pinned_product_num": 1,
                    "broadcast_title": "클라우드 CSR 분리 테스트 방송",
                    "youtube_embed_url": "https://www.youtube.com/embed/dQw4w9WgXcQ?autoplay=1&mute=1",
                    "live_urls": {
                        "YOUTUBE": {
                            "active": True,
                            "live_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                            "embed_url": "https://www.youtube.com/embed/dQw4w9WgXcQ?autoplay=1&mute=1"
                        }
                    }
                }
            }
        )
        assert r_sync.status_code == 200, r_sync.get_data(as_text=True)
        assert r_sync.get_json()["synced_count"] == 21

        # 3. /live HTML 내 서버 MJPEG(/api/camera/stream) 완전 제거 및 YouTube iframe 임베드 마운트 검증
        live_html = client.get("/live").get_data(as_text=True)
        assert "/api/camera/stream" not in live_html, "MJPEG stream URL must not exist in /live HTML"
        assert "https://www.youtube.com/embed/dQw4w9WgXcQ?autoplay=1&amp;mute=1" in live_html or \
               "https://www.youtube.com/embed/dQw4w9WgXcQ?autoplay=1&mute=1" in live_html

        # 4. Full state vs Lite state 페이로드 크기 절감 및 ETag 304 Not Modified 검증
        r_full = client.get("/api/live/state")
        r_lite = client.get("/api/live/state-lite")
        full_bytes = len(r_full.get_data())
        lite_bytes = len(r_lite.get_data())
        print(f"[Payload Diet] full={full_bytes}B vs state-lite={lite_bytes}B (saved {(1 - lite_bytes / full_bytes) * 100:.1f}%)")
        assert lite_bytes < full_bytes * 0.35, "state-lite should be drastically smaller than full state"

        etag = r_lite.headers.get("ETag")
        assert etag, "ETag header missing on /api/live/state-lite"
        r_304 = client.get("/api/live/state-lite", headers={"If-None-Match": etag})
        assert r_304.status_code == 304 and len(r_304.get_data()) == 0

        r_cat = client.get("/api/catalog")
        cat_etag = r_cat.headers.get("ETag")
        r_cat_304 = client.get("/api/catalog", headers={"If-None-Match": cat_etag})
        assert r_cat_304.status_code == 304

        # 5. 클라우드 쇼핑몰 주문 생성 -> pending-orders 증분 Pull 검증
        t0 = time.time()
        r_ord = client.post(
            "/api/orders/create",
            json={
                "buyer_name": "홍길동",
                "buyer_phone": "010-1234-5678",
                "address": "서울시 강남구 테헤란로 1",
                "payment_method": "CARD",
                "source_page": "LIVE",
                "items": [{"num": 1, "color": "블랙", "size": "L", "quantity": 2}]
            }
        )
        elapsed_ms = (time.time() - t0) * 1000.0
        assert r_ord.status_code == 200, r_ord.get_data(as_text=True)
        print(f"[Cloud Order Latency] {elapsed_ms:.1f}ms (no reverse OBS timeout)")
        assert elapsed_ms < 500, "Cloud mode order creation must not block on OBS reverse webhook"

        r_pend = client.get("/api/sync/pending-orders?after_id=0", headers=headers)
        assert r_pend.status_code == 200
        pend_data = r_pend.get_json()
        assert pend_data["count"] == 1
        assert pend_data["orders"][0]["items"][0]["quantity"] == 2

        # 6. 하트비트 만료(방송자 PC 강제 종료 시뮬레이션) 시 자동 is_live=False 전환 검증
        mall_database.update_live_state_bundle({
            "is_live": True,
            "last_heartbeat_ts": time.time() - (mall_database.HEARTBEAT_TIMEOUT_SEC + 5.0)
        })
        r_expired = client.get("/api/live/state-lite").get_json()
        assert r_expired["is_live"] is False, "Stale heartbeat must auto-expire is_live to False"

    # 7. 로컬 라이브 에이전트 Private Network Access 헤더 검증
    with live_commerce_server.app.test_client() as agent_client:
        r_pna = agent_client.get(
            "/api/agent/cloud-config",
            headers={
                "Origin": "https://mall.example.com",
                "Access-Control-Request-Private-Network": "true"
            }
        )
        assert r_pna.status_code == 200
        assert r_pna.headers.get("Access-Control-Allow-Private-Network") == "true"

    print("ALL CSR + LOCAL AGENT SPLIT TESTS PASSED!")


if __name__ == "__main__":
    run_tests()
