# -*- coding: utf-8 -*-
"""
2-도메인 · 3-영역 분리 아키텍처 및 심층 보안(인증·인가·CSRF·개인정보보호·토큰·잠금) 통합 검증 테스트
"""
import os
import sys
import uuid
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

_tmp_dir = tempfile.mkdtemp(prefix="two_domain_sec_test_")
os.environ["MALL_DB_PATH"] = os.path.join(_tmp_dir, "test_mall_sec.db")
os.environ["MALL_MODE"] = "cloud"
os.environ["OBS_PULL_ENABLED"] = "0"
os.environ["MALL_SYNC_SECRET"] = "sec-sync-key-2026"
os.environ["STUDIO_ADMIN_USERNAME"] = "test_studio_admin"
os.environ["STUDIO_ADMIN_EMAIL"] = "admin@studio.test"
os.environ["STUDIO_ADMIN_PASSWORD"] = "StudioAdmin123!"
os.environ["MALL_ADMIN_EMAIL"] = "admin@mall.test"
os.environ["MALL_ADMIN_PASSWORD"] = "MallAdmin123!"

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
MALL_DIR = os.path.join(ROOT_DIR, "shopping_mall")
sys.path.insert(0, MALL_DIR)
sys.path.insert(0, ROOT_DIR)

import database
import live_commerce_server
import mall_database
import app as mall_app


def run_security_tests():
    database.ensure_default_studio_admin()
    mall_database.init_mall_db()
    mall_database.ensure_default_mall_admin()

    # 쿠키명 분리 검증 (도메인1 studio_sid vs 도메인2 mall_sid)
    assert live_commerce_server.app.config["SESSION_COOKIE_NAME"] == "studio_sid"
    assert mall_app.app.config["SESSION_COOKIE_NAME"] == "mall_sid"
    print("✅ [1/10] 2-도메인 세션 쿠키 격리(studio_sid vs mall_sid) 확인 완료")

    # =========================================================================
    # 도메인 1 (홍보 홈페이지 + 승인제 OBS 스튜디오) 검증
    # =========================================================================
    with live_commerce_server.app.test_client() as studio_client:
        # 1. 홍보 홈페이지(GET /) 비회원 전체 공개, OBS 설명·3대 영역 노출 및 서버 Host 기반 :6001 동적 쇼핑몰 주소 확인
        r_promo = studio_client.get("/", headers={"Host": "203.0.113.55:5000"})
        assert r_promo.status_code == 200
        promo_html = r_promo.get_data(as_text=True)
        assert "① 홍보 홈페이지" in promo_html
        assert "② 쇼핑몰" in promo_html or "② 독립 쇼핑몰" in promo_html
        assert "③ 웹 OBS 스튜디오" in promo_html or "③ OBS 스튜디오" in promo_html
        assert "웹 OBS 스튜디오는 어떻게 동작하나요?" in promo_html
        assert "http://203.0.113.55:6001" in promo_html
        assert "localhost:6000" not in promo_html and "localhost:3000" not in promo_html
        r_cfg = studio_client.get("/api/agent/cloud-config", headers={"Host": "203.0.113.55:5000"}).get_json()
        assert r_cfg["public_mall_url"] == "http://203.0.113.55:6001"
        print("✅ [2/10] 도메인 1 홍보 홈페이지(/) 비로그인 공개 및 서버 Host 기반 쇼핑몰(http://203.0.113.55:6001) 동적 연결 확인 완료")

        # 2. 비로그인 상태에서 OBS 스튜디오 및 내부 API 접근 차단 확인
        assert studio_client.get("/overlay/studio").status_code == 302
        assert studio_client.get("/portal").status_code == 302
        assert studio_client.get("/inventory").status_code == 302
        assert studio_client.post("/api/clear-all").status_code == 401
        assert studio_client.post("/api/inventory/product/add", json={"name": "해킹시도"}).status_code == 401
        print("✅ [3/10] 비로그인 사용자의 OBS 스튜디오 화면 및 제어 API 접근 차단(302/401) 확인 완료")

        # 3. 무선 카메라(/cam) 무토큰 외부 접근 차단 & 유효 cam_token 허용 검증
        r_cam_no_tok = studio_client.get("/cam")
        assert r_cam_no_tok.status_code == 403
        r_frame_no_tok = studio_client.post("/api/camera/frame", data=b"fake")
        assert r_frame_no_tok.status_code == 403

        valid_cam_tok = live_commerce_server.get_studio_cam_token()
        r_cam_ok = studio_client.get(f"/cam?cam_token={valid_cam_tok}")
        assert r_cam_ok.status_code == 200
        print("✅ [4/10] 무선 카메라(/cam) 무단 접근 차단(403) 및 서명된 cam_token 페어링 검증 완료")

    # 4. 방송자 회원가입(PENDING) -> 스튜디오 차단 -> 관리자 승인(APPROVED) -> 스튜디오 진입 검증
    with live_commerce_server.app.test_client() as seller_client:
        # 먼저 로그인 페이지를 열어 CSRF 토큰 발급
        seller_client.get("/auth/register")
        with seller_client.session_transaction() as sess:
            csrf_tok = sess["_csrf_token"]

        uname = f"seller_{uuid.uuid4().hex[:6]}"
        r_reg = seller_client.post(
            "/auth/register",
            headers={"X-CSRF-Token": csrf_tok},
            json={
                "username": uname,
                "email": f"{uname}@example.com",
                "display_name": "테스트 방송자",
                "phone": "010-1111-2222",
                "password": "Password123!"
            }
        )
        assert r_reg.status_code == 200, r_reg.get_data(as_text=True)
        reg_json = r_reg.get_json()
        assert reg_json["user_status"] == "PENDING"

        # 승인 전에는 /overlay/studio 접근 시 /auth/pending으로 리다이렉트되고 API는 403 반환
        r_studio_pending = seller_client.get("/overlay/studio")
        assert r_studio_pending.status_code == 302 and "/auth/pending" in r_studio_pending.headers["Location"]
        assert seller_client.post("/api/overlay/ticker", json={"text": "무단자막"}).status_code == 403

        # 관리자 전용 콘솔(/admin, /admin/login)으로 승인 처리 및 초대코드 발급 검증
        with live_commerce_server.app.test_client() as admin_client:
            # 비로그인 시 /admin 접근하면 /admin/login으로 리다이렉트
            r_unauth_admin = admin_client.get("/admin")
            assert r_unauth_admin.status_code == 302 and "/admin/login" in r_unauth_admin.headers["Location"]

            admin_client.get("/admin/login")
            with admin_client.session_transaction() as asess:
                a_csrf = asess["_csrf_token"]

            # 일반 방송자 계정으로 /admin/login 시도 시 403 ADMIN_REQUIRED 거부 확인
            r_seller_admin_try = admin_client.post(
                "/admin/login",
                headers={"X-CSRF-Token": a_csrf},
                json={"login_id": uname, "password": "Password123!"}
            )
            assert r_seller_admin_try.status_code == 403
            assert r_seller_admin_try.get_json()["code"] == "ADMIN_REQUIRED"

            # 정식 관리자 계정으로 /admin/login 로그인 -> redirect == "/admin" 확인
            r_alogin = admin_client.post(
                "/admin/login",
                headers={"X-CSRF-Token": a_csrf},
                json={"login_id": "test_studio_admin", "password": "StudioAdmin123!"}
            )
            assert r_alogin.status_code == 200
            alogin_json = r_alogin.get_json()
            assert alogin_json["redirect"] == "/admin"
            a_csrf = alogin_json["csrf_token"]

            # /admin 대시보드 정상 렌더링 확인 (OBS 스튜디오로 넘어가지 않고 관리자 페이지 표시)
            r_admin_dash = admin_client.get("/admin")
            assert r_admin_dash.status_code == 200
            assert "통합 관리자 대시보드 (/admin)" in r_admin_dash.get_data(as_text=True)

            # CSRF 토큰 없이 관리자 API 호출 시 403 차단 확인
            r_no_csrf = admin_client.post("/admin/api/invite-codes", json={"memo": "no-csrf"})
            assert r_no_csrf.status_code == 403

            # 신규 가입한 seller를 /admin/api/users/status 로 승인(APPROVED) 처리
            users = database.list_studio_users()
            target_u = next(u for u in users if u["username"] == uname)
            r_app = admin_client.post(
                "/admin/api/users/status",
                headers={"X-CSRF-Token": a_csrf},
                json={"user_id": target_u["id"], "status": "APPROVED"}
            )
            assert r_app.status_code == 200

            # 초대코드 발급 (/admin/api/invite-codes)
            inv_code = f"INV-{uuid.uuid4().hex[:6].upper()}"
            r_inv = admin_client.post(
                "/admin/api/invite-codes",
                headers={"X-CSRF-Token": a_csrf},
                json={"code": inv_code, "memo": "즉시승인테스트", "max_uses": 2}
            )
            assert r_inv.status_code == 200

        # 승인 완료된 seller는 이제 /overlay/studio 정상 진입하되, /admin 접근 시 403 차단 확인
        r_studio_ok = seller_client.get("/overlay/studio")
        assert r_studio_ok.status_code == 200
        assert seller_client.get("/admin").status_code == 403
        print("✅ [5/10] 전용 관리자 콘솔(/admin) 로그인·승인(PENDING➔APPROVED)·초대코드 발급·권한 분리 검증 완료")

    # 5. 초대 코드로 가입 시 즉시 APPROVED 검증 & 5회 실패 계정 잠금 검증
    with live_commerce_server.app.test_client() as inv_client:
        inv_client.get("/auth/register")
        with inv_client.session_transaction() as isess:
            i_csrf = isess["_csrf_token"]
        uname2 = f"fast_{uuid.uuid4().hex[:6]}"
        r_reg2 = inv_client.post(
            "/auth/register",
            headers={"X-CSRF-Token": i_csrf},
            json={
                "username": uname2,
                "email": f"{uname2}@example.com",
                "display_name": "초대코드방송자",
                "password": "Password123!",
                "invite_code": inv_code
            }
        )
        assert r_reg2.status_code == 200
        assert r_reg2.get_json()["user_status"] == "APPROVED"
        assert inv_client.get("/overlay/studio").status_code == 200

        # 5회 연속 잘못된 비밀번호 시도 시 423 LOCKED 확인
        for i in range(5):
            res_fail = database.authenticate_studio_user(uname2, "WrongPass999!")
        assert res_fail["code"] == "LOCKED"
        res_locked = database.authenticate_studio_user(uname2, "Password123!")
        assert res_locked["ok"] is False and res_locked["code"] == "LOCKED"
        print("✅ [6/10] 초대코드 즉시 승인 가입 및 5회 비밀번호 실패 시 10분 계정 잠금(LOCKED) 확인 완료")

    # =========================================================================
    # 도메인 2 (회원제 독립 쇼핑몰 + 비회원 구매 허용 라이브커머스) 검증
    # =========================================================================
    with mall_app.app.test_client() as mclient:
        # 테스트용 상품 동기화
        r_sync = mclient.post(
            "/api/sync/from-obs",
            headers={"X-Mall-Sync-Secret": "sec-sync-key-2026"},
            json={
                "action": "bulk_sync",
                "products": [{
                    "id": 1, "num": 1, "name": "보안검증 캐시미어 코트",
                    "category": "OUTER", "consumer_price": 120000, "price": 89000,
                    "options": [{"color": "블랙", "size": "M", "stock": 20}]
                }],
                "live_state": {"is_live": True, "pinned_product_num": 1}
            }
        )
        assert r_sync.status_code == 200

        # 6. 비회원의 일반 쇼핑몰 결제(/checkout, source_page='MALL') 차단 검증
        assert mclient.get("/checkout").status_code == 302
        r_mall_guest_ord = mclient.post(
            "/api/orders/create",
            json={
                "buyer_name": "비회원일반구매시도",
                "buyer_phone": "010-2222-3333",
                "address": "서울시 강남구",
                "source_page": "MALL",
                "items": [{"num": 1, "color": "블랙", "size": "M", "quantity": 1}]
            }
        )
        assert r_mall_guest_ord.status_code == 401
        assert r_mall_guest_ord.get_json()["code"] == "AUTH_REQUIRED"
        print("✅ [7/10] 일반 쇼핑몰 비회원 결제 차단(401 AUTH_REQUIRED & /checkout 로그인 리다이렉트) 확인 완료")

        # 7. 라이브커머스(/live)에서는 비회원도 바로구매 허용 + 위조 토큰 차단 검증
        r_forged_live = mclient.post(
            "/api/orders/create",
            json={
                "buyer_name": "위조토큰시도",
                "buyer_phone": "010-3333-4444",
                "address": "서울시 서초구",
                "source_page": "LIVE",
                "live_order_token": "forged.token.signature",
                "guest_password": "guest7788",
                "items": [{"num": 1, "color": "블랙", "size": "M", "quantity": 1}]
            }
        )
        assert r_forged_live.status_code == 403

        valid_live_tok = mall_app.create_live_order_token()
        r_live_guest_ok = mclient.post(
            "/api/orders/create",
            json={
                "buyer_name": "라이브비회원고객",
                "buyer_phone": "010-5555-6666",
                "address": "서울시 송파구 올림픽로 1",
                "source_page": "LIVE",
                "live_order_token": valid_live_tok,
                "guest_password": "myLivePw99",
                "items": [{"num": 1, "color": "블랙", "size": "M", "quantity": 1}]
            }
        )
        assert r_live_guest_ok.status_code == 200, r_live_guest_ok.get_data(as_text=True)
        guest_order_code = r_live_guest_ok.get_json()["order"]["order_code"]
        print(f"✅ [8/10] 라이브커머스(/live) 비회원 즉시 구매 성공({guest_order_code}) 및 위조 토큰 차단(403) 확인 완료")

        # 8. 주문 조회 개인정보 보호 검증 (무조건 조회 시 0건, 비밀번호 오입력 시 0건, 3중 일치 시에만 1건 반환)
        r_lookup_empty = mclient.get("/api/orders/lookup").get_json()
        assert r_lookup_empty["count"] == 0, "비로그인 무조건 조회 시 타인 주문이 노출되면 안 됩니다!"

        r_lookup_wrong_pw = mclient.post(
            "/api/orders/lookup",
            json={"order_code": guest_order_code, "buyer_phone": "010-5555-6666", "guest_password": "wrongPassword"}
        ).get_json()
        assert r_lookup_wrong_pw["count"] == 0

        r_lookup_ok = mclient.post(
            "/api/orders/lookup",
            json={"order_code": guest_order_code, "buyer_phone": "010-5555-6666", "guest_password": "myLivePw99"}
        ).get_json()
        assert r_lookup_ok["count"] == 1
        assert r_lookup_ok["orders"][0]["buyer_name"] == "라이브비회원고객"
        assert "guest_password_hash" not in r_lookup_ok["orders"][0]
        print("✅ [9/10] 비회원 주문 개인정보 노출 차단 및 주문번호+연락처+비밀번호 3중 검증 조회 확인 완료")

        # 9. 쇼핑몰 회원가입·로그인 후 일반 쇼핑몰 주문 성공 & 일반 회원의 /admin 접근 차단 확인
        mclient.get("/auth/register")
        with mclient.session_transaction() as msess:
            m_csrf = msess["_csrf_token"]
        m_email = f"buyer_{uuid.uuid4().hex[:6]}@mall.test"
        r_mreg = mclient.post(
            "/auth/register",
            headers={"X-CSRF-Token": m_csrf},
            json={
                "email": m_email,
                "password": "BuyerPass123!",
                "name": "정식쇼핑몰회원",
                "phone": "010-7777-8888",
                "address": "경기도 성남시 분당구"
            }
        )
        assert r_mreg.status_code == 200
        m_csrf = r_mreg.get_json()["csrf_token"]

        assert mclient.get("/checkout").status_code == 200
        r_member_ord = mclient.post(
            "/api/orders/create",
            headers={"X-CSRF-Token": m_csrf},
            json={
                "buyer_name": "정식쇼핑몰회원",
                "buyer_phone": "010-7777-8888",
                "address": "경기도 성남시 분당구",
                "source_page": "CART_CHECKOUT",
                "items": [{"num": 1, "color": "블랙", "size": "M", "quantity": 1}]
            }
        )
        assert r_member_ord.status_code == 200

        # 회원이 /api/orders/lookup 호출 시 본인의 주문 1건만 조회되고 타인(라이브비회원고객) 주문은 보이지 않는지 확인
        r_my_orders = mclient.get("/api/orders/lookup").get_json()
        assert r_my_orders["count"] == 1
        assert r_my_orders["orders"][0]["buyer_name"] == "정식쇼핑몰회원"

        # 일반 회원이 쇼핑몰 /admin 또는 할인율 변경 API 호출 시 403 차단 확인
        assert mclient.get("/admin").status_code == 403
        assert mclient.post("/api/admin/apply-discount", headers={"X-CSRF-Token": m_csrf}, json={"discount_pct": 90}).status_code == 403
        print("✅ [10/10] 쇼핑몰 정식 회원 결제·본인 주문 격리 조회 및 비관리자 /admin 접근 차단(403) 검증 완료")

    # 테스트용 임시 계정 정리 및 기본 관리자 복원
    conn = database.get_connection()
    conn.execute("DELETE FROM studio_users WHERE username LIKE 'seller_%' OR username LIKE 'fast_%' OR username = 'test_studio_admin'")
    conn.execute("DELETE FROM studio_invite_codes WHERE code LIKE 'INV-%'")
    conn.commit()
    for k in ("STUDIO_ADMIN_USERNAME", "STUDIO_ADMIN_EMAIL", "STUDIO_ADMIN_PASSWORD"):
        os.environ.pop(k, None)
    database.ensure_default_studio_admin()

    print("\n🎉 ALL 2-DOMAIN ARCHITECTURE & SECURITY TESTS PASSED!")


if __name__ == "__main__":
    run_security_tests()
