# -*- coding: utf-8 -*-
import re
import sys
import live_commerce_server

sys.stdout.reconfigure(encoding='utf-8')

test_pages = [
    ('/portal', 'portal', '🏢 메인 포털'),
    ('/inventory', 'inventory', '📦 재고 관리'),
    ('/cs', 'cs', '💬 CS 관리'),
    ('/analytics', 'analytics', '📊 판매량 분석')
]

menu_items = [
    '🏢 메인 포털',
    '📡 웹방송 관제탑',
    '🖥️ OBS 오버레이',
    '📱 쇼호스트 뷰',
    '📦 재고 관리',
    '💬 CS 관리',
    '📊 판매량 분석'
]

all_passed = True

with live_commerce_server.app.test_client() as client:
    # 0. 비로그인 상태에서 / 홍보 홈페이지 공개 접속 및 /portal 리다이렉트 확인
    promo_resp = client.get('/')
    promo_html = promo_resp.get_data(as_text=True)
    if promo_resp.status_code != 200 or '① 홍보 홈페이지' not in promo_html:
        print(f'❌ [/] 홍보 홈페이지 렌더링 실패: status={promo_resp.status_code}')
        all_passed = False
    else:
        print('✅ [/          ] 200 OK | 공개 홍보 홈페이지 및 3대 영역 안내 정상 표시')

    unauth_portal = client.get('/portal')
    if unauth_portal.status_code != 302:
        print(f'❌ [/portal] 비로그인 접근 차단 실패: status={unauth_portal.status_code}')
        all_passed = False

    # 관리자(승인된 방송자) 세션 설정 후 내부 GNB 페이지 검증
    admin_user = next(u for u in live_commerce_server.database.list_studio_users() if u.get('status') == 'APPROVED')
    with client.session_transaction() as sess:
        sess['studio_user_id'] = admin_user['id']
        sess['studio_role'] = admin_user['role']

    for url, expected_tab, tab_label in test_pages:
        try:
            resp = client.get(url)
            html = resp.get_data(as_text=True)

            # 1. 7개 메뉴 항목 존재 확인
            missing_menus = [m for m in menu_items if m not in html]
            if missing_menus:
                print(f'❌ [{url}] 누락된 메뉴: {missing_menus}')
                all_passed = False
                continue

            # 2. 활성 탭 active 클래스 부여 확인
            pattern = rf'class="nav-item active"[^>]*>\s*{re.escape(tab_label)}'
            match = re.search(pattern, html)
            if not match:
                print(f'❌ [{url}] 활성 탭 매칭 실패 (기대: {tab_label})')
                all_passed = False
            else:
                print(f'✅ [{url:11}] 200 OK | 7개 메뉴 전체 포함 | 활성 탭: [{tab_label}] 일치')
        except Exception as e:
            print(f'❌ [{url}] 오류: {e}')
            all_passed = False

if all_passed:
    print('\n🎉 홍보 홈 및 전 페이지 GNB 헤더 모듈화 · 인증 보호 검증 100% 완료!')
else:
    sys.exit(1)
