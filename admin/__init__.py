# -*- coding: utf-8 -*-
"""
독립 관리자 모듈 패키지 (admin/)
- /admin 대시보드 및 /admin/login 관리자 전용 인증 제공
"""
from .routes import admin_bp

__all__ = ["admin_bp"]
