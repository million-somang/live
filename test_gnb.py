# -*- coding: utf-8 -*-
import urllib.request
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')

test_pages = [
    ('/', 'portal', '🏢 메인 포털'),
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
for url, expected_tab, tab_label in test_pages:
    try:
        resp = urllib.request.urlopen('http://127.0.0.1:5000' + url)
        html = resp.read().decode('utf-8')
        
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
    print('\n🎉 전 페이지 GNB 헤더 모듈화 및 활성 탭 일치 검증 100% 완료!')
else:
    sys.exit(1)
