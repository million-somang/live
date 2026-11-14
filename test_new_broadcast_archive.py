# -*- coding: utf-8 -*-
"""
OBS 스튜디오 '새로운 방송 시작 (이전 방송 별도 보관 & 초기화)' 및
'이전 방송 기록 불러오기/복원' 기능 통합 검증 테스트
"""

import database
from live_commerce_server import app, platform_tracker


def test_new_broadcast_archive_and_restore():
    client = app.test_client()

    # 0. 테스트 전 기존 방송 데이터를 안전하게 스냅샷 백업
    backup = database.archive_and_start_new_broadcast(title="__TEST_PRE_BACKUP__", save_empty=True, force_save=True)
    backup_session_id = backup["archived_session_id"]

    try:
        # 1. OBS 스튜디오 UI에 버튼과 모달이 존재하는지 확인
        res_studio = client.get("/overlay/studio")
        assert res_studio.status_code == 200
        studio_html = res_studio.data.decode("utf-8")
        assert "btnStartNewBroadcast" in studio_html
        assert "새로운 방송 시작" in studio_html
        assert "btnOpenBroadcastHistory" in studio_html
        assert "이전 방송 기록" in studio_html
        assert "newBroadcastModal" in studio_html
        assert "broadcastHistoryModal" in studio_html

        # 2. 샘플 방송 데이터(채팅, 주문, CS 문의, 핀 상품, 타이머) 생성
        chat_id = database.save_chat("YOUTUBE", "테스트주문자", "1번 블랙 L 2개 주문합니다", "ORDER", "주문 접수 완료")
        platform_tracker.record_chat("YOUTUBE", "테스트주문자", "1번 블랙 L 2개 주문합니다", "ORDER", "주문 접수 완료", chat_id=chat_id)
        order_id = database.create_order({
            "platform": "YOUTUBE",
            "buyer_name": "테스트주문자",
            "item_name": "1번 테스트 상품",
            "color": "블랙",
            "size": "L",
            "quantity": 2,
            "unit_price": 25000,
            "unit_cost": 10000,
            "raw_message": "1번 블랙 L 2개 주문합니다",
            "reply_text": "주문 접수 완료"
        })
        database.save_cs_inquiry("INSTAGRAM", "문의고객", "배송/출고", "오늘 배송 언제 출발하나요?", "오후 4시 이전 당일 출고됩니다.")
        database.set_overlay_state("pinned_product_num", 1)
        database.set_overlay_state("flash_timer", {"active": True, "label": "특가 마감", "minutes": 5})

        # 현재 요약 확인
        res_sum = client.get("/api/broadcast/current-summary")
        assert res_sum.status_code == 200
        sum_data = res_sum.get_json()
        seq_before = sum_data["broadcast_reset_seq"]
        assert isinstance(seq_before, int)
        assert sum_data["current"]["total_chats"] >= 1
        assert sum_data["current"]["total_orders"] >= 1
        assert sum_data["current"]["total_revenue"] == 50000
        assert sum_data["current"]["total_cs"] >= 1

        # 3. [새로운 방송 시작] API 호출 -> 이전 데이터 별도 저장 & 현재 방송 초기화
        res_new = client.post("/api/broadcast/new", json={"title": "테스트 특가 1차 방송"})
        assert res_new.status_code == 200
        new_data = res_new.get_json()
        assert new_data["status"] == "ok"
        seq_after_new = new_data["broadcast_reset_seq"]
        assert seq_after_new > seq_before
        archived_id = new_data["archive"]["archived_session_id"]
        assert archived_id is not None
        assert new_data["archive"]["archived_counts"]["orders"] == 1
        assert new_data["archive"]["archived_counts"]["revenue"] == 50000

        # 4. 초기화 후 현재 상태가 모두 0(초기 상태)이고 모든 실시간 API가 동일한 broadcast_reset_seq를 반환하는지 검증
        res_dash = client.get("/api/dashboard-summary")
        dash_data = res_dash.get_json()
        assert dash_data["broadcast_reset_seq"] == seq_after_new
        assert dash_data["stats"]["total"] == 0
        assert len(dash_data["recent_chats"]) == 0
        assert len(dash_data["pending_orders"]) == 0

        res_host = client.get("/api/host/data")
        host_data = res_host.get_json()
        assert host_data["broadcast_reset_seq"] == seq_after_new
        assert host_data["stats"]["orders"] == 0
        assert host_data["stats"]["sales"] == 0
        assert host_data["stats"]["chats"] == 0
        assert len(host_data["recent_chats"]) == 0
        assert host_data["latest_cue"] is None
        assert host_data["pinned_question"] is None

        res_cs_api = client.get("/api/cs/list")
        cs_api_data = res_cs_api.get_json()
        assert cs_api_data["broadcast_reset_seq"] == seq_after_new
        assert len(cs_api_data["questions"]) == 0

        res_ov = client.get("/api/overlay/data")
        ov_data = res_ov.get_json()
        assert ov_data["broadcast_reset_seq"] == seq_after_new
        assert ov_data["pinned_product"] is None
        assert ov_data["timer"]["active"] is False
        assert len(ov_data["recent_orders"]) == 0

        # 4-1. 채팅 관리창(/broadcast), 쇼호스트 뷰(/host), CS(/cs), 통계(/analytics), 메인(/) 자동 초기화 스크립트 포함 검증
        res_broadcast_view = client.get("/broadcast")
        assert res_broadcast_view.status_code == 200
        b_html = res_broadcast_view.data.decode("utf-8")
        assert "resetBroadcastDashboardUI" in b_html
        assert "broadcast_reset_seq" in b_html

        res_host_view = client.get("/host")
        assert res_host_view.status_code == 200
        h_html = res_host_view.data.decode("utf-8")
        assert "resetHostViewForNewBroadcast" in h_html
        assert "obs_overlay_sync" in h_html
        assert "broadcast_reset_seq" in h_html
        assert "쇼호스트 프롬프트" in h_html
        assert "현재 시간" in h_html
        assert "방송시간" in h_html
        assert "전체화면" in h_html
        assert "btnScale100" not in h_html
        assert "btnSoundToggle" not in h_html
        assert "모니터 PiP" not in h_html

        res_cs_view = client.get("/cs")
        assert res_cs_view.status_code == 200
        cs_html = res_cs_view.data.decode("utf-8")
        assert "resetCsDashboardUI" in cs_html
        assert "obs_overlay_sync" in cs_html

        res_analytics_view = client.get("/analytics")
        assert res_analytics_view.status_code == 200
        an_html = res_analytics_view.data.decode("utf-8")
        assert "checkAnalyticsBroadcastReset" in an_html
        assert "obs_overlay_sync" in an_html

        res_portal_view = client.get("/")
        assert res_portal_view.status_code == 200
        p_html = res_portal_view.data.decode("utf-8")
        assert "checkPortalBroadcastReset" in p_html
        assert "obs_overlay_sync" in p_html

        # 5. 별도 저장된 [이전 방송 기록] 상세 조회 검증
        res_hist = client.get(f"/api/broadcast/history/{archived_id}")
        assert res_hist.status_code == 200
        hist_detail = res_hist.get_json()["session"]
        assert hist_detail["title"] == "테스트 특가 1차 방송"
        assert len(hist_detail["orders"]) == 1
        assert hist_detail["orders"][0]["buyer_name"] == "테스트주문자"
        assert len(hist_detail["chats"]) == 1
        assert len(hist_detail["cs_inquiries"]) == 1

        # 6. 이전 방송 주문 엑셀 다운로드 검증
        res_excel = client.get(f"/api/broadcast/history/{archived_id}/excel")
        assert res_excel.status_code == 200

        # 7. 저장된 이전 방송 데이터를 현재 화면으로 다시 불러오기(복원) 검증
        res_restore = client.post(f"/api/broadcast/restore/{archived_id}", json={"auto_backup_current": False})
        assert res_restore.status_code == 200
        restore_data = res_restore.get_json()
        assert restore_data["status"] == "ok"
        assert restore_data["broadcast_reset_seq"] > seq_after_new
        assert restore_data["restored"]["restored_counts"]["orders"] == 1

        res_dash_after = client.get("/api/dashboard-summary")
        dash_after = res_dash_after.get_json()
        assert dash_after["stats"]["total"] == 1
        assert len(dash_after["recent_chats"]) == 1

        # 8. 불러온 이전 방송은 수정 불가(Read-Only)인지 검증
        res_sum_restored = client.get("/api/broadcast/current-summary").get_json()
        assert res_sum_restored["is_restored_session"] is True
        assert res_sum_restored["restored_session_id"] == archived_id
        count_before_second_new = res_sum_restored["history_count"]

        res_mod_confirm = client.post("/api/orders/confirm", json={"order_id": 1})
        assert res_mod_confirm.status_code == 403
        res_mod_reject = client.post("/api/orders/reject", json={"order_id": 1})
        assert res_mod_reject.status_code == 403
        res_mod_reply = client.post("/api/chat/reply", json={"chat_id": 1, "reply_text": "수정 시도"})
        assert res_mod_reply.status_code == 403

        # 9. 이전 방송을 불러온 상태에서 다시 [새로운 방송 시작] 클릭 시 2중(중복) 저장되지 않고 초기화만 되는지 검증
        res_new_again = client.post("/api/broadcast/new", json={"title": "중복 저장 시도 방송"})
        assert res_new_again.status_code == 200
        new_again_data = res_new_again.get_json()
        assert new_again_data["status"] == "ok"
        assert new_again_data["archive"]["skipped_duplicate"] is True
        assert new_again_data["archive"]["archived_session_id"] == archived_id
        assert new_again_data["history_count"] == count_before_second_new

        # 초기화 후 복원 잠금이 해제되고 데이터는 0건인지 확인
        res_sum_after_reset = client.get("/api/broadcast/current-summary").get_json()
        assert res_sum_after_reset["is_restored_session"] is False
        assert res_sum_after_reset["current"]["total_chats"] == 0
        assert res_sum_after_reset["current"]["total_orders"] == 0

        # 10. 빈 방송(데이터 0건) 상태에서 [새로운 방송 시작]을 또 클릭해도 빈 방송이 추가 저장되지 않는지 검증
        res_empty_new = client.post("/api/broadcast/new", json={"title": "빈 방송"})
        assert res_empty_new.status_code == 200
        empty_new_data = res_empty_new.get_json()
        assert empty_new_data["archive"]["archived_session_id"] is None
        assert empty_new_data["history_count"] == count_before_second_new

        # 11. 새 방송 후 상품을 선택하지 않았을 때 호스트뷰(/api/host/data)의 pinned_product가 None인지 검증
        host_after_new = client.get("/api/host/data").get_json()
        assert host_after_new["pinned_product"] is None
        initial_reload_seq = host_after_new["host_reload_seq"]

        # 상품을 핀 고정하면 호스트뷰에 해당 상품이 보이고, 핀 해제 시 다시 None이 되는지 검증
        if host_after_new.get("all_products"):
            first_num = host_after_new["all_products"][0]["num"]
            res_pin = client.post("/api/overlay/pin-product", json={"product_num": first_num})
            assert res_pin.status_code == 200
            host_pinned = client.get("/api/host/data").get_json()
            assert host_pinned["pinned_product"] is not None
            assert host_pinned["pinned_product"]["num"] == first_num

            # 핀 해제
            res_unpin = client.post("/api/overlay/pin-product", json={"product_num": None})
            assert res_unpin.status_code == 200
            host_unpinned = client.get("/api/host/data").get_json()
            assert host_unpinned["pinned_product"] is None

        # 12. 쇼호스트 디렉팅 스튜디오의 [🔄 쇼호스트 뷰 새로고침] API 및 버튼 검증
        res_reload = client.post("/api/host/reload")
        assert res_reload.status_code == 200
        reload_data = res_reload.get_json()
        assert reload_data["status"] == "ok"
        assert reload_data["host_reload_seq"] == initial_reload_seq + 1

        b_html = client.get("/broadcast").data.decode("utf-8")
        assert "btnReloadHostView" in b_html
        assert "reloadHostPrompterView()" in b_html
        assert "btnHostSiteMirror" in b_html
        assert "startHostSiteMirror()" in b_html

        # 13. 쇼호스트 디렉팅 스튜디오의 [🌐 사이트 보여주기] 상태, 별도 컨트롤 창 및 뷰어 검증
        assert "site_mirror" in host_after_new
        assert host_after_new["site_mirror"]["active"] is False

        h_html_latest = client.get("/host").data.decode("utf-8")
        assert "hostSiteMirrorBox" in h_html_latest
        assert "hostMetricsGrid" in h_html_latest
        assert "hostProductLineupSection" in h_html_latest
        assert "renderSiteMirror" in h_html_latest
        assert "runHostSiteFrameLoop" in h_html_latest

        c_html = client.get("/host/site-controller?url=https://www.naver.com").data.decode("utf-8")
        assert "쇼호스트 뷰 실시간 송출 중" in c_html
        assert "sendSiteAction" in c_html
        assert "navigateToInputUrl" in c_html
        assert "navigator.sendBeacon" not in c_html

        res_frame = client.get("/api/host/site-mirror/frame.jpg")
        assert res_frame.status_code == 200
        assert res_frame.mimetype == "image/jpeg"
        assert len(res_frame.data) > 100

        res_stop_site = client.post("/api/host/site-mirror/stop")
        assert res_stop_site.status_code == 200
        assert res_stop_site.get_json()["site_mirror"]["active"] is False

        # 테스트용 아카이브 세션 삭제
        res_del = client.delete(f"/api/broadcast/history/{archived_id}")
        assert res_del.status_code == 200

        print("✅ 모든 테스트 통과: 새 방송 시작(아카이브 별도 저장 + 초기화), 중복 2중 저장 원천 차단, 저장된 방송 수정 잠금 검증 완료!")
    finally:
        # 원래 백업해 둔 방송 데이터 복원 후 임시 백업 레코드 정리
        if backup_session_id:
            database.restore_broadcast_session(backup_session_id, auto_backup_current=False, mark_restored=False)
            platform_tracker.clear_all()
            platform_tracker.load_from_db(limit=500)
            database.delete_broadcast_session(backup_session_id)


if __name__ == "__main__":
    test_new_broadcast_archive_and_restore()
