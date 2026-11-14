# -*- coding: utf-8 -*-
"""
라이브 커머스 CS 및 고객 문의 관리 모듈 (cs_manager.py)
SQLite 데이터베이스(database.py)와 완벽히 연동되어 CS 문의 및 AI 응대 내역을 저장/조회합니다.
"""

import database


class CSManager:
    """database.py를 기반으로 동작하는 CS 관리자 (하위 호환 레이어)"""

    def __init__(self):
        pass

    def classify_type(self, message):
        """메시지 내용 기반 CS 유형 자동 분류"""
        msg = message.replace(" ", "")
        if any(k in msg for k in ["사이즈", "키", "몸무게", "오버핏", "착용"]):
            return "사이즈/스펙"
        elif any(k in msg for k in ["배송", "출고", "도착", "언제와", "택배"]):
            return "배송/출고"
        elif any(k in msg for k in ["가격", "얼마", "할인", "이벤트", "무료배송"]):
            return "가격/혜택"
        elif any(k in msg for k in ["환불", "교환", "반품"]):
            return "교환/환불"
        return "일반 문의"

    def record_question(self, platform, user, message, reply):
        """CS 질문 및 AI 답변을 DB에 영속 저장합니다."""
        q_type = self.classify_type(message)
        database.save_cs_inquiry(
            platform=platform,
            sender=user,
            category_type=q_type,
            question=message,
            answer=reply
        )

    def get_all(self, q_type=None):
        """모든 CS 문의 내역을 최신순으로 반환합니다."""
        return database.get_all_cs(q_type)

    def get_stats(self):
        """CS 문의 유형별 통계를 반환합니다."""
        return database.get_cs_stats()


# 싱글톤 인스턴스
cs_manager = CSManager()
