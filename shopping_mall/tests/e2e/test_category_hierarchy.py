"""Playwright E2E tests for 3-level hierarchical categories & home count badge removal."""

from __future__ import annotations

import pytest
from playwright.sync_api import BrowserContext, Page, expect

from e2e.helpers import (
    assert_no_horizontal_overflow,
    assert_no_main_internal_vertical_scroll,
    attach_page_diagnostics,
)


def _login_browser_context(
    context: BrowserContext,
    base_url: str,
    email: str,
    password: str,
) -> Page:
    page = context.new_page()
    page.goto(f"{base_url}/auth/login", wait_until="domcontentloaded")
    res = page.evaluate(
        """async ({ email, password }) => {
            const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
            const resp = await fetch('/auth/login', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': csrf
                },
                body: JSON.stringify({ email, password, next: '/' })
            });
            return { status: resp.status, body: await resp.text() };
        }""",
        {"email": email, "password": password},
    )
    assert res["status"] == 200, f"Login failed: {res}"
    return page


@pytest.mark.tier1
def test_e2e_3_level_categories_admin_storefront_detail_and_mobile(
    server_factory,
    api_client_factory,
    desktop_context_factory,
    mobile_context_factory,
):
    srv = server_factory(seed_products=True)
    api = api_client_factory(srv)
    api.login(srv.admin_email, srv.admin_password)

    ctx = desktop_context_factory()
    page = _login_browser_context(ctx, srv.base_url, srv.admin_email, srv.admin_password)
    diag = attach_page_diagnostics(page)

    # 1. Admin: 1차(의류) -> 2차(상의) -> 3차(셔츠) 생성 및 상품 모달 3단 연동 선택
    page.goto(f"{srv.base_url}/admin", wait_until="domcontentloaded")
    page.locator("#adminTabCategories").click()
    expect(page.locator("#adminPanelCategories")).to_be_visible()

    # 1차 등록: 의류
    page.locator("#catNewName").fill("의류")
    page.locator("#catNewCode").fill("APPAREL")
    page.locator("#btnCreateCategory").click()
    expect(page.locator("#adminCategoriesTbody")).to_contain_text("의류")

    # 2차 등록: 의류 > 상의 (테이블의 '+ 하위' 버튼 활용)
    apparel_row = page.locator("#adminCategoriesTbody tr", has_text="APPAREL")
    apparel_row.get_by_role("button", name="+ 하위").click()
    page.locator("#catNewName").fill("상의")
    page.locator("#catNewCode").fill("APPAREL_TOP")
    page.locator("#btnCreateCategory").click()
    expect(page.locator("#adminCategoriesTbody")).to_contain_text("APPAREL_TOP")

    # 3차 등록: 의류 > 상의 > 셔츠
    top_row = page.locator("#adminCategoriesTbody tr", has_text="APPAREL_TOP")
    top_row.get_by_role("button", name="+ 하위").click()
    page.locator("#catNewName").fill("셔츠")
    page.locator("#catNewCode").fill("APPAREL_SHIRT")
    page.locator("#btnCreateCategory").click()
    expect(page.locator("#adminCategoriesTbody")).to_contain_text("APPAREL_SHIRT")

    # 3차 행에는 '+ 하위' 버튼이 없어야 함 (최대 3단계)
    shirt_row = page.locator("#adminCategoriesTbody tr", has_text="APPAREL_SHIRT")
    expect(shirt_row.get_by_role("button", name="+ 하위")).to_have_count(0)

    # API 단에서 4단계 등록 및 하위 있는 상위 삭제 차단 검증
    cats_resp = api.get("/api/admin/categories")
    assert cats_resp.status_code == 200
    cats = api.json_or_raise(cats_resp)["categories"]
    shirt_cat = next(c for c in cats if c["code"] == "APPAREL_SHIRT")
    apparel_cat = next(c for c in cats if c["code"] == "APPAREL")

    r4 = api.post("/api/admin/categories/save", json={"name": "반팔", "parent_id": shirt_cat["id"]})
    assert r4.status_code == 400
    del_parent = api.post("/api/admin/categories/delete", json={"id": apparel_cat["id"]})
    assert del_parent.status_code == 400

    # 2. Admin: 상품 등록 모달에서 3단 연동 드롭다운으로 상품 등록
    page.locator("#adminTabProducts").click()
    page.locator("#btnOpenNewProductModal").click()
    expect(page.locator("#adminProductModal")).to_be_visible()

    page.locator("#apCatL1").select_option("APPAREL")
    page.locator("#apCatL2").select_option("APPAREL_TOP")
    page.locator("#apCatL3").select_option("APPAREL_SHIRT")
    expect(page.locator("#apCategoryPathPreview")).to_contain_text("의류 > 상의 > 셔츠")

    page.locator("#apNum").fill("910")
    page.locator("#apName").fill("옥스포드 포플린 셔츠")
    page.locator("#apPrice").fill("49000")
    page.locator("#btnApSaveSubmit").click()
    expect(page.locator("#adminProductsListContainer")).to_contain_text("옥스포드 포플린 셔츠")
    expect(page.locator("#adminProductsListContainer")).to_contain_text("의류 > 상의 > 셔츠")

    assert_no_horizontal_overflow(page)
    assert_no_main_internal_vertical_scroll(page)

    # 3. Storefront (/): 등록 상품 갯수 배지 제거 + 1->2->3단 드릴다운 + 상품 카드 경로
    page.goto(f"{srv.base_url}/", wait_until="domcontentloaded")
    expect(page.locator("#homeProductCountBadge")).to_have_count(0)
    assert "등록 상품 " not in page.locator(".ax-toolbar").inner_text()

    # 1차 '의류' 클릭 -> 2차 바 오픈 + 하위 상품 포함 노출
    page.locator("#homeCategoryBarL1").get_by_role("link", name="의류", exact=True).click()
    expect(page.locator("#homeCategoryBarL2")).to_be_visible()
    expect(page.locator("#homeCategoryBarL2")).to_contain_text("의류 전체")
    expect(page.locator(".ax-product-grid")).to_contain_text("옥스포드 포플린 셔츠")
    expect(page.locator(".ax-product-grid")).to_contain_text("의류 > 상의 > 셔츠")

    # 2차 '상의' 클릭 -> 3차 바 오픈
    page.locator("#homeCategoryBarL2").get_by_role("link", name="상의", exact=True).click()
    expect(page.locator("#homeCategoryBarL3")).to_be_visible()
    expect(page.locator("#homeCategoryBarL3")).to_contain_text("상의 전체")

    # 3차 '셔츠' 클릭 -> 해당 상품 유지
    page.locator("#homeCategoryBarL3").get_by_role("link", name="셔츠", exact=True).click()
    expect(page.locator(".ax-product-grid")).to_contain_text("옥스포드 포플린 셔츠")
    assert_no_horizontal_overflow(page)
    assert_no_main_internal_vertical_scroll(page)

    # 4. Product detail (/product/910): 전체 경로 배지 + 클릭 가능 브레드크럼
    page.goto(f"{srv.base_url}/product/910", wait_until="domcontentloaded")
    crumb = page.locator("#pdCategoryBreadcrumb")
    expect(crumb).to_be_visible()
    expect(crumb).to_contain_text("홈")
    expect(crumb).to_contain_text("의류")
    expect(crumb).to_contain_text("상의")
    expect(crumb).to_contain_text("셔츠")
    expect(page.locator(".ax-badge", has_text="의류 > 상의 > 셔츠").first).to_be_visible()

    # 브레드크럼 중간 단계('상의') 클릭 시 해당 카테고리 목록으로 이동
    crumb.get_by_role("link", name="상의", exact=True).click()
    expect(page).to_have_url(f"{srv.base_url}/?category=APPAREL_TOP")
    diag.assert_clean("desktop_category_hierarchy")

    # 5. Mobile (390x844) 레이아웃 오버플로우 및 콘솔 에러 0건 확인
    mob_ctx = mobile_context_factory()
    mob_page = mob_ctx.new_page()
    mob_diag = attach_page_diagnostics(mob_page)
    mob_page.goto(f"{srv.base_url}/?category=APPAREL_SHIRT", wait_until="domcontentloaded")
    expect(mob_page.locator("#homeCategoryBarL2")).to_be_visible()
    expect(mob_page.locator("#homeCategoryBarL3")).to_be_visible()
    assert_no_horizontal_overflow(mob_page)
    assert_no_main_internal_vertical_scroll(mob_page)

    mob_page.goto(f"{srv.base_url}/product/910", wait_until="domcontentloaded")
    expect(mob_page.locator("#pdCategoryBreadcrumb")).to_be_visible()
    assert_no_horizontal_overflow(mob_page)
    assert_no_main_internal_vertical_scroll(mob_page)
    mob_diag.assert_clean("mobile_category_hierarchy")
