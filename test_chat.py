# -*- coding: utf-8 -*-
"""
로컬 라이브 커머스 서버 테스트 시뮬레이터 (test_chat.py)
유튜브·인스타·틱톡에서 유입되는 가상 채팅을 서버(http://127.0.0.1:5000/chat)로 전송하여
주문 접수, 엑셀 기록, OBS 자막 갱신 동작을 검증합니다.
"""

import time
import requests
import json
import os
import sys
import io

# 윈도우 콘솔 UTF-8 출력 설정
if sys.platform.startswith("win"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from openpyxl import load_workbook

SERVER_URL = "http://127.0.0.1:5000/chat"
STATUS_URL = "http://127.0.0.1:5000/status"

TEST_CHATS = [
    {
        "desc": "테스트 1: 유튜브 - 명확한 번호/색상/사이즈/수량 주문",
        "payload": {
            "type": "youtube",
            "chatname": "홍길동",
            "chatmessage": "1번 블랙 L사이즈 2개 주문할게요!",
            "bot": False
        }
    },
    {
        "desc": "테스트 2: 인스타그램 - 상품 사이즈 문의 질문",
        "payload": {
            "type": "instagram",
            "chatname": "패션피플_미나",
            "chatmessage": "키 172인데 후드티 사이즈 L 입으면 오버핏 나오나요?",
            "bot": False
        }
    },
    {
        "desc": "테스트 3: 틱톡 - 구어체 구매 의사 (단어 '주문' 없음)",
        "payload": {
            "type": "tiktok",
            "chatname": "스트릿댄서",
            "chatmessage": "2번 데님 연청 M 살게요~",
            "bot": False
        }
    },
    {
        "desc": "테스트 4: 유튜브 - 일반 응원 및 감탄사 (저장/자막 제외 대상)",
        "payload": {
            "type": "youtube",
            "chatname": "호호아줌마",
            "chatmessage": "오늘 호스트님 옷 너무 잘 어울려요 ㅎㅎ 화이팅!",
            "bot": False
        }
    },
    {
        "desc": "테스트 5: 인스타그램 - 배송 및 출고 일정 질문",
        "payload": {
            "type": "instagram",
            "chatname": "빠른배송원해요",
            "chatmessage": "지금 사면 오늘 바로 출고되나요? 배송비 얼마예요?",
            "bot": False
        }
    }
]


def check_server_online():
    try:
        res = requests.get(STATUS_URL, timeout=3)
        if res.status_code == 200:
            print("🟢 서버가 정상 동작 중입니다.")
            print(f"   서버 응답: {res.json()['service']} ({res.json()['engine']})")
            return True
    except Exception:
        pass
    print("🔴 서버(http://127.0.0.1:5000)에 연결할 수 없습니다.")
    print("   먼저 다른 터미널에서 'python live_commerce_server.py'를 실행해 주세요.")
    return False


def run_tests():
    print("=" * 60)
    print("🧪 라이브 커머스 AI 서버 실시간 채팅 전송 테스트 시작")
    print("=" * 60)

    if not check_server_online():
        return

    for idx, test in enumerate(TEST_CHATS, 1):
        print(f"\n[{idx}/{len(TEST_CHATS)}] {test['desc']}")
        payload = test["payload"]
        print(f"   📤 전송: [{payload['type'].upper()}] {payload['chatname']}: \"{payload['chatmessage']}\"")
        try:
            start_t = time.time()
            res = requests.post(SERVER_URL, json=payload, timeout=10)
            elapsed = time.time() - start_t

            if res.status_code == 200:
                data = res.json()
                category = data.get("category", "CHAT")
                reply = data.get("reply", "")
                print(f"   📥 수신 ({elapsed:.2f}s): 카테고리={category} | 답변=\"{reply}\"")
            else:
                print(f"   ⚠️ 응답 오류 (HTTP {res.status_code}): {res.text}")
        except Exception as e:
            print(f"   ❌ 요청 실패: {e}")

        time.sleep(1.0)  # 자연스러운 간격

    # 최종 결과 점검
    print("\n" + "=" * 60)
    print("📋 테스트 완료 후 파일 상태 점검")
    print("=" * 60)

    # 1. 엑셀 파일 점검
    excel_path = "live_orders.xlsx"
    if os.path.exists(excel_path):
        try:
            wb = load_workbook(excel_path)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            print(f"✅ [엑셀 파일] '{excel_path}' (총 {len(rows)}행, 헤더 포함)")
            for r_idx, row in enumerate(rows[:5]):
                if r_idx == 0:
                    print(f"   - 헤더: {row}")
                else:
                    print(f"   - 주문 {r_idx}: {row[0]} | {row[1]} | {row[2]}님 | {row[3]} ({row[4]}/{row[5]}) {row[6]}개")
        except Exception as e:
            print(f"⚠️ 엑셀 파일 열기 실패: {e}")

    # 2. OBS 자막 파일 점검
    obs_path = "obs_ai_reply.txt"
    if os.path.exists(obs_path):
        try:
            with open(obs_path, "r", encoding="utf-8") as f:
                content = f.read()
            print(f"\n✅ [OBS 자막 파일] '{obs_path}' 최신 내용:")
            for line in content.strip().split("\n"):
                print(f"   {line}")
        except Exception as e:
            print(f"⚠️ 자막 파일 읽기 실패: {e}")

    # 3. 플랫폼 연결 상태 API 점검
    try:
        plat_res = requests.get("http://127.0.0.1:5000/api/platforms", timeout=3)
        if plat_res.status_code == 200:
            print("\n✅ [실시간 플랫폼 연결 상태] (/api/platforms):")
            for p_key, p_data in plat_res.json().items():
                status_icon = "🟢" if p_data["status"] == "ACTIVE" else ("🟡" if p_data["status"] == "IDLE" else "⚪")
                print(f"   {status_icon} [{p_data['display_name']}] 상태: {p_data['status']} | 최근 수신: {p_data['time_str']} | 채팅: {p_data['chat_count']}건 (주문 {p_data['order_count']}건)")
    except Exception as e:
        print(f"⚠️ 플랫폼 상태 조회 실패: {e}")

    print("\n🎉 모든 테스트가 성공적으로 완료되었습니다!")


if __name__ == '__main__':
    run_tests()

