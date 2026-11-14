# -*- coding: utf-8 -*-
"""
라이브 커머스 실시간 재고 관리 모듈 (inventory_manager.py)
SQLite 데이터베이스(database.py)와 완벽히 연동되어 상품/옵션별 재고 관리 및 차감을 수행합니다.
목업 데이터 없이 순수 DB 기반으로 동작합니다.
"""

import database


class InventoryManager:
    """database.py를 기반으로 동작하는 재고 관리자 (하위 호환 레이어)"""

    def __init__(self):
        pass

    def get_all(self):
        """DB에 등록된 모든 상품 및 옵션별 재고 목록을 반환합니다."""
        return database.get_all_products()

    def get_item(self, item_id):
        """특정 상품 조회"""
        real_id = int(str(item_id).replace("item-", ""))
        return database.get_product_by_id(real_id)

    def add_product(self, product_data):
        """새 상품 및 옵션 매트릭스를 DB에 추가합니다."""
        return database.add_product(product_data)

    def update_product(self, item_id, update_data):
        """기존 상품 정보 및 옵션 수정"""
        return database.update_product(item_id, update_data)

    def delete_product(self, item_id):
        """상품 및 옵션 삭제"""
        return database.delete_product(item_id)

    def deduct_stock(self, item_name, color, size, qty=1):
        """주문 발생 시 해당 옵션의 재고를 실시간 DB에서 자동 차감합니다."""
        return database.deduct_stock(item_name, color, size, qty)

    def update_stock(self, item_id, color, size, new_stock):
        """재고 수량 수동 수정/입고"""
        return database.update_stock(item_id, color, size, new_stock)

    def get_catalog_text(self):
        """AI 프롬프트용 실시간 상품 카탈로그 텍스트를 생성합니다."""
        return database.get_catalog_text()

    def get_total_stock(self):
        """등록된 모든 상품의 잔여 재고 총합을 반환합니다."""
        items = self.get_all()
        return sum(it.get("total_stock", 0) for it in items)

    def confirm_order_and_deduct(self, order_id):
        """관리자 확인 후 주문 승인 및 재고 차감 실행"""
        return database.confirm_order_and_deduct(order_id)

    def reject_order(self, order_id):
        """관리자 주문 반려/취소 (재고 차감 없음)"""
        return database.reject_order(order_id)

    def get_pending_orders(self):
        """확인 대기 중(PENDING)인 주문 목록 반환"""
        return database.get_pending_orders()


# 싱글톤 인스턴스
inventory_manager = InventoryManager()
