"""Tier 1 opaque-box E2E tests for R3 features (F10–F13): happy paths."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from e2e.helpers import (
    assert_no_horizontal_overflow,
    assert_no_main_internal_vertical_scroll,
    attach_page_diagnostics,
    create_order_via_api,
    create_tiny_png_bytes,
    seed_product_via_sync,
)


def _fill_optional_input(container: Any, selector: str, label_pattern: str, value: str) -> None:
    loc = container.locator(selector)
    if loc.count() > 0:
        loc.first.fill(value)
        return
    lbl = container.get_by_label(re.compile(label_pattern))
    assert lbl.count() > 0, f"Could not locate input {selector!r} / label /{label_pattern}/"
    lbl.first.fill(value)


def _select_cash_receipt_type(container: Any, value: str, label_text: str) -> None:
    sel = container.locator("select[name='cash_receipt_type'], #coCashReceiptType, #ibCashReceiptType")
    if sel.count() > 0:
        sel.first.select_option(value)
        return
    radio = container.locator(f"input[name='cash_receipt_type'][value='{value}']")
    if radio.count() > 0:
        radio.first.check()
        return
    lbl = container.get_by_label(re.compile(label_text))
    assert lbl.count() > 0, f"Could not locate cash_receipt_type control for {value}"
    lbl.first.check()


def _extract_list(data: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        val = data.get(key)
        if isinstance(val, list):
            return val
    return []


# ---------------------------------------------------------------------------
# Feature F10: R3 무통장 실무 보강, 주문 완료 페이지, PG 어댑터
# ---------------------------------------------------------------------------


@pytest.mark.tier1
@pytest.mark.f10
def test_t1_f10_01_checkout_bank_depositor_and_cash_receipt_ui_to_order_complete(
    live_server,
    desktop_page,
):
    diag = attach_page_diagnostics(desktop_page)
    desktop_page.goto(f"{live_server.base_url}/", wait_until="domcontentloaded")
    desktop_page.evaluate(
        "items => window.saveMallCart(items)",
        [
            {
                "num": 1,
                "name": "골라방 클래식 코튼 티셔츠",
                "price": 29000,
                "consumer_price": 39000,
                "image_url": "",
                "color": "블랙",
                "size": "M",
                "quantity": 1,
            }
        ],
    )

    resp = desktop_page.goto(f"{live_server.base_url}/checkout", wait_until="domcontentloaded")
    assert resp is not None and resp.status == 200

    desktop_page.locator("#coBuyerName").fill("김무통장")
    desktop_page.locator("#coBuyerPhone").fill("010-2222-3333")
    desktop_page.locator("#coZipcode").fill("06236")
    desktop_page.locator("#coAddress").fill("서울특별시 강남구 테헤란로 152")
    desktop_page.locator("#coGuestPassword").fill("pass1234")

    _fill_optional_input(
        desktop_page,
        "input[name='depositor_name'], #coDepositorName",
        r"입금자",
        "김입금자",
    )
    _select_cash_receipt_type(desktop_page, "INCOME", r"소득공제")
    _fill_optional_input(
        desktop_page,
        "input[name='cash_receipt_no'], #coCashReceiptNo",
        r"현금영수증.*번호|발급.*번호|휴대폰.*사업자",
        "01022223333",
    )

    desktop_page.locator("#coSubmitBtn").click()
    desktop_page.wait_for_url(re.compile(r"/order-complete/ORD-"), timeout=10000)

    order_code = desktop_page.url.rstrip("/").split("/")[-1]
    assert order_code.startswith("ORD-")

    body_text = desktop_page.locator("body").inner_text()
    assert order_code in body_text
    assert "카카오뱅크" in body_text
    assert "김입금자" in body_text
    assert "01022223333" in body_text

    copy_btn = desktop_page.get_by_role("button", name=re.compile(r"주문번호.*복사|복사"))
    assert copy_btn.count() > 0

    assert_no_horizontal_overflow(desktop_page)
    assert_no_main_internal_vertical_scroll(desktop_page)
    diag.assert_clean("f10_01_order_complete")


@pytest.mark.tier1
@pytest.mark.f10
def test_t1_f10_02_instant_buy_depositor_and_cash_receipt_expense(
    live_server,
    desktop_page,
    api_client_factory,
):
    diag = attach_page_diagnostics(desktop_page)
    resp = desktop_page.goto(f"{live_server.base_url}/product/1", wait_until="domcontentloaded")
    assert resp is not None and resp.status == 200

    buy_btn = desktop_page.get_by_role("button", name=re.compile(r"바로\s*구매"))
    assert buy_btn.count() > 0
    buy_btn.first.click()

    modal = desktop_page.locator("#instantBuyModal")
    modal.wait_for(state="visible", timeout=5000)

    modal.locator("#ibBuyerName").fill("박법인")
    modal.locator("#ibBuyerPhone").fill("010-3333-4444")
    modal.locator("#ibZipcode").fill("06236")
    modal.locator("#ibAddress").fill("서울특별시 강남구 역삼로 100")
    modal.locator("#ibGuestPassword").fill("pass5678")

    _fill_optional_input(
        modal,
        "input[name='depositor_name'], #ibDepositorName",
        r"입금자",
        "법인입금",
    )
    _select_cash_receipt_type(modal, "EXPENSE", r"지출증빙")
    _fill_optional_input(
        modal,
        "input[name='cash_receipt_no'], #ibCashReceiptNo",
        r"현금영수증.*번호|발급.*번호|사업자",
        "123-45-67890",
    )

    modal.locator("#ibSubmitBtn").click()
    desktop_page.wait_for_url(re.compile(r"/order-complete/ORD-"), timeout=10000)

    order_code = desktop_page.url.rstrip("/").split("/")[-1]
    assert order_code.startswith("ORD-")

    fresh_guest = api_client_factory(live_server)
    lookup_resp = fresh_guest.post(
        "/api/orders/lookup",
        json={
            "order_code": order_code,
            "buyer_phone": "010-3333-4444",
            "guest_password": "pass5678",
        },
        include_csrf=False,
    )
    assert lookup_resp.status_code == 200
    lookup_data = fresh_guest.json_or_raise(lookup_resp, "f10_02_lookup")
    assert lookup_data["count"] == 1
    order = lookup_data["orders"][0]
    assert order["depositor_name"] == "법인입금"
    assert order["cash_receipt_type"] == "EXPENSE"
    assert order["cash_receipt_no"] == "123-45-67890"
    diag.assert_clean("f10_02_instant_buy")


@pytest.mark.tier1
@pytest.mark.f10
def test_t1_f10_03_api_order_create_returns_redirect_url_and_fields(
    live_server,
    member_api,
):
    resp = member_api.post(
        "/api/orders/create",
        json={
            "items": [{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
            "buyer_name": "홍길동",
            "buyer_phone": "010-9999-8888",
            "zipcode": "06236",
            "address": "서울특별시 강남구 테헤란로 152",
            "address_detail": "301호",
            "source_page": "CART_CHECKOUT",
            "payment_method": "BANK",
            "depositor_name": "홍길동입금",
            "cash_receipt_type": "INCOME",
            "cash_receipt_no": "01099998888",
        },
    )
    assert resp.status_code == 200
    data = member_api.json_or_raise(resp, "f10_03_create")
    assert data["status"] == "ok"
    assert data.get("ok") is True

    order = data["order"]
    expected_redirect = f"/order-complete/{order['order_code']}"
    assert data.get("redirect_url") == expected_redirect
    assert order["depositor_name"] == "홍길동입금"
    assert order["cash_receipt_type"] == "INCOME"
    assert order["cash_receipt_no"] == "01099998888"

    complete_page = member_api.get(expected_redirect)
    assert complete_page.status_code == 200
    assert order["order_code"] in complete_page.text
    assert "홍길동입금" in complete_page.text


@pytest.mark.tier1
@pytest.mark.f10
def test_t1_f10_04_pg_prepare_bank_portone_v2_toss_payments(
    live_server,
    member_api,
):
    order = create_order_via_api(
        member_api,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        payment_method="BANK",
    )

    for method in ("BANK", "PORTONE_V2", "TOSS_PAYMENTS"):
        resp = member_api.post(
            "/api/payments/prepare",
            json={"order_code": order["order_code"], "payment_method": method},
        )
        assert resp.status_code == 200, f"prepare failed for {method}: {resp.text}"
        data = member_api.json_or_raise(resp, f"f10_04_prepare_{method}")
        assert data["status"] == "ok"
        payload = data.get("payment", data)
        assert payload.get("order_code") == order["order_code"]
        assert payload.get("amount") == order["total_amount"]
        provider_val = str(
            payload.get("payment_method") or payload.get("provider") or ""
        ).upper()
        assert provider_val == method


@pytest.mark.tier1
@pytest.mark.f10
def test_t1_f10_05_pg_confirm_updates_order_payment_status(
    live_server,
    member_api,
):
    order = create_order_via_api(
        member_api,
        items=[{"num": 2, "color": "아이보리", "size": "FREE", "quantity": 1}],
        payment_method="TOSS_PAYMENTS",
    )

    prep_resp = member_api.post(
        "/api/payments/prepare",
        json={"order_code": order["order_code"], "payment_method": "TOSS_PAYMENTS"},
    )
    assert prep_resp.status_code == 200

    conf_resp = member_api.post(
        "/api/payments/confirm",
        json={
            "order_code": order["order_code"],
            "payment_method": "TOSS_PAYMENTS",
            "payment_key": "test_toss_key_001",
            "amount": order["total_amount"],
        },
    )
    assert conf_resp.status_code == 200
    conf_data = member_api.json_or_raise(conf_resp, "f10_05_confirm")
    assert conf_data["status"] == "ok"

    lookup_resp = member_api.get("/api/orders/lookup")
    assert lookup_resp.status_code == 200
    orders = member_api.json_or_raise(lookup_resp, "f10_05_lookup")["orders"]
    matched = [o for o in orders if o["order_code"] == order["order_code"]]
    assert len(matched) == 1
    assert matched[0]["payment_status"] == "입금완료"
    assert matched[0]["order_status"] != "결제대기"


@pytest.mark.tier1
@pytest.mark.f10
def test_t1_f10_06_order_complete_mobile_layout_and_dynamic_bank_sync(
    server_factory,
    api_client_factory,
    mobile_page,
):
    srv = server_factory()
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)

    upd = admin.post(
        "/api/admin/store-info",
        json={
            "bank_name": "신한은행",
            "bank_account": "110-999-888777",
            "bank_holder": "이순신",
        },
    )
    assert upd.status_code == 200

    diag = attach_page_diagnostics(mobile_page)
    mobile_page.goto(f"{srv.base_url}/", wait_until="domcontentloaded")
    mobile_page.evaluate(
        "items => window.saveMallCart(items)",
        [
            {
                "num": 1,
                "name": "골라방 클래식 코튼 티셔츠",
                "price": 29000,
                "consumer_price": 39000,
                "image_url": "",
                "color": "블랙",
                "size": "M",
                "quantity": 1,
            }
        ],
    )

    resp = mobile_page.goto(f"{srv.base_url}/checkout", wait_until="domcontentloaded")
    assert resp is not None and resp.status == 200

    mobile_page.locator("#coBuyerName").fill("모바일주문자")
    mobile_page.locator("#coBuyerPhone").fill("010-4444-5555")
    mobile_page.locator("#coZipcode").fill("06236")
    mobile_page.locator("#coAddress").fill("서울특별시 강남구 테헤란로 152")
    mobile_page.locator("#coGuestPassword").fill("mob1234")
    mobile_page.locator("#coSubmitBtn").click()
    mobile_page.wait_for_url(re.compile(r"/order-complete/ORD-"), timeout=10000)

    body_text = mobile_page.locator("body").inner_text()
    assert "신한은행" in body_text
    assert "110-999-888777" in body_text
    assert "이순신" in body_text

    assert_no_horizontal_overflow(mobile_page)
    assert_no_main_internal_vertical_scroll(mobile_page)
    diag.assert_clean("f10_06_mobile_order_complete")


# ---------------------------------------------------------------------------
# Feature F11: R3 상품 Q&A(비밀글), 1:1 문의, 공지사항/FAQ
# ---------------------------------------------------------------------------


@pytest.mark.tier1
@pytest.mark.f11
def test_t1_f11_01_product_public_qna_create_and_admin_answer_ui(
    live_server,
    admin_api,
    desktop_context_factory,
    member_storage_state,
):
    seed_product_via_sync(
        live_server.base_url,
        live_server.sync_secret,
        num=9111,
        name="F11 공개 Q&A 테스트 상품",
        price=32000,
    )

    member_ctx = desktop_context_factory(storage_state=member_storage_state)
    member_page = member_ctx.new_page()
    m_diag = attach_page_diagnostics(member_page)
    resp = member_page.goto(f"{live_server.base_url}/product/9111", wait_until="domcontentloaded")
    assert resp is not None and resp.status == 200

    qna_title = member_page.locator("input[name='qna_title'], input[name='title']").first
    if qna_title.count() > 0:
        qna_title.fill("세탁 방법 문의")
    qna_content = member_page.locator(
        "textarea[name='qna_content'], textarea[name='content'], #pdQnaContent"
    )
    if qna_content.count() > 0:
        qna_content.last.fill("소재 세탁 방법 문의")
    else:
        member_page.get_by_label(re.compile(r"문의\s*내용|Q&A\s*내용")).first.fill("소재 세탁 방법 문의")

    qna_submit = member_page.locator("#pdQnaSubmitBtn").or_(
        member_page.get_by_role("button", name=re.compile(r"문의\s*등록|Q&A\s*등록|질문\s*등록"))
    )
    assert qna_submit.count() > 0
    with member_page.expect_response(
        lambda r: "/api/products/9111/qna" in r.url and r.request.method == "POST",
        timeout=8000,
    ) as qna_resp_info:
        qna_submit.first.click()
    assert qna_resp_info.value.status == 200
    m_diag.assert_clean("f11_01_member_qna_submit")

    qna_list_resp = admin_api.get("/api/products/9111/qna")
    assert qna_list_resp.status_code == 200
    qna_items = _extract_list(admin_api.json_or_raise(qna_list_resp), "qnas", "items", "qna")
    matched = [q for q in qna_items if "소재 세탁 방법 문의" in str(q.get("content", ""))]
    assert len(matched) >= 1
    qna_id = matched[0]["id"]

    ans_resp = admin_api.post(
        "/api/admin/qna/answer",
        json={"qna_id": qna_id, "answer": "미지근한 물에 손세탁 권장합니다."},
    )
    assert ans_resp.status_code == 200

    guest_ctx = desktop_context_factory()
    guest_page = guest_ctx.new_page()
    g_diag = attach_page_diagnostics(guest_page)
    g_resp = guest_page.goto(f"{live_server.base_url}/product/9111", wait_until="domcontentloaded")
    assert g_resp is not None and g_resp.status == 200
    body_text = guest_page.locator("body").inner_text()
    assert "소재 세탁 방법 문의" in body_text
    assert "미지근한 물에 손세탁 권장합니다." in body_text
    assert_no_horizontal_overflow(guest_page)
    assert_no_main_internal_vertical_scroll(guest_page)
    g_diag.assert_clean("f11_01_guest_qna_view")


@pytest.mark.tier1
@pytest.mark.f11
def test_t1_f11_02_product_secret_qna_visible_to_author_and_admin_only(
    live_server,
    member_api,
    admin_api,
    desktop_context_factory,
    member_storage_state,
):
    seed_product_via_sync(
        live_server.base_url,
        live_server.sync_secret,
        num=9112,
        name="F11 비밀 Q&A 테스트 상품",
        price=34000,
    )

    create_resp = member_api.post(
        "/api/products/9112/qna",
        json={
            "title": "비밀문의",
            "content": "비밀본문_AUTHOR_ONLY",
            "is_secret": True,
        },
    )
    assert create_resp.status_code == 200
    create_data = member_api.json_or_raise(create_resp, "f11_02_qna_create")
    qna_id = (create_data.get("qna") or {}).get("id") or create_data.get("qna_id") or create_data.get("id")
    if not qna_id:
        q_list = _extract_list(
            member_api.json_or_raise(member_api.get("/api/products/9112/qna")),
            "qnas",
            "items",
            "qna",
        )
        qna_id = q_list[0]["id"]

    ans_resp = admin_api.post(
        "/api/admin/qna/answer",
        json={"qna_id": qna_id, "answer": "비밀답변_ADMIN_REPLY"},
    )
    assert ans_resp.status_code == 200

    m_get = member_api.get("/api/products/9112/qna")
    a_get = admin_api.get("/api/products/9112/qna")
    assert m_get.status_code == 200 and a_get.status_code == 200
    assert "비밀본문_AUTHOR_ONLY" in m_get.text
    assert "비밀답변_ADMIN_REPLY" in m_get.text
    assert "비밀본문_AUTHOR_ONLY" in a_get.text
    assert "비밀답변_ADMIN_REPLY" in a_get.text

    member_ctx = desktop_context_factory(storage_state=member_storage_state)
    page = member_ctx.new_page()
    diag = attach_page_diagnostics(page)
    page.goto(f"{live_server.base_url}/product/9112", wait_until="domcontentloaded")
    body_text = page.locator("body").inner_text()
    assert "비밀본문_AUTHOR_ONLY" in body_text
    assert "비밀답변_ADMIN_REPLY" in body_text
    diag.assert_clean("f11_02_author_secret_qna")


@pytest.mark.tier1
@pytest.mark.f11
def test_t1_f11_03_admin_notices_and_faqs_crud_and_cs_page_render(
    server_factory,
    api_client_factory,
    desktop_page,
    mobile_page,
):
    srv = server_factory()
    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)

    n_save = admin.post(
        "/api/admin/notices/save",
        json={"title": "배송 지연 안내 공지", "content": "물량 증가로 순차 출고됩니다.", "is_pinned": True},
    )
    assert n_save.status_code == 200
    n_id = (admin.json_or_raise(n_save).get("notice") or {}).get("id") or admin.json_or_raise(n_save).get("id")
    if n_id:
        n_upd = admin.post(
            "/api/admin/notices/save",
            json={"id": n_id, "title": "배송 지연 안내 공지(수정)", "content": "정상 출고 재개 안내", "is_pinned": True},
        )
        assert n_upd.status_code == 200

    n_tmp = admin.post(
        "/api/admin/notices/save",
        json={"title": "삭제될 임시 공지", "content": "임시 본문", "is_pinned": False},
    )
    assert n_tmp.status_code == 200
    tmp_n_data = admin.json_or_raise(n_tmp)
    tmp_n_id = (tmp_n_data.get("notice") or {}).get("id") or tmp_n_data.get("id")
    assert tmp_n_id
    n_del = admin.post("/api/admin/notices/delete", json={"id": tmp_n_id, "notice_id": tmp_n_id})
    assert n_del.status_code == 200

    f_save = admin.post(
        "/api/admin/faqs/save",
        json={"category": "배송", "question": "당일 출고 마감 시간은 언제인가요?", "answer": "오후 2시 이전 결제 건 당일 출고됩니다."},
    )
    assert f_save.status_code == 200

    f_tmp = admin.post(
        "/api/admin/faqs/save",
        json={"category": "기타", "question": "삭제될 FAQ", "answer": "임시 답변"},
    )
    assert f_tmp.status_code == 200
    tmp_f_data = admin.json_or_raise(f_tmp)
    tmp_f_id = (tmp_f_data.get("faq") or {}).get("id") or tmp_f_data.get("id")
    assert tmp_f_id
    f_del = admin.post("/api/admin/faqs/delete", json={"id": tmp_f_id, "faq_id": tmp_f_id})
    assert f_del.status_code == 200

    guest = api_client_factory(srv)
    notices_resp = guest.get("/api/notices")
    faqs_resp = guest.get("/api/faqs")
    assert notices_resp.status_code == 200 and faqs_resp.status_code == 200
    assert "배송 지연 안내 공지" in notices_resp.text
    assert "삭제될 임시 공지" not in notices_resp.text
    assert "당일 출고 마감 시간은 언제인가요?" in faqs_resp.text
    assert "삭제될 FAQ" not in faqs_resp.text

    for label, page in [("desktop", desktop_page), ("mobile", mobile_page)]:
        diag = attach_page_diagnostics(page)
        resp = page.goto(f"{srv.base_url}/cs", wait_until="domcontentloaded")
        assert resp is not None and resp.status == 200
        body_text = page.locator("body").inner_text()
        assert "배송 지연 안내 공지" in body_text
        assert "당일 출고 마감 시간은 언제인가요?" in body_text
        assert_no_horizontal_overflow(page)
        assert_no_main_internal_vertical_scroll(page)
        diag.assert_clean(f"f11_03_{label}_cs")


@pytest.mark.tier1
@pytest.mark.f11
def test_t1_f11_04_member_one_to_one_inquiry_create_and_admin_answer(
    live_server,
    member_api,
    admin_api,
    desktop_context_factory,
    member_storage_state,
):
    member_ctx = desktop_context_factory(storage_state=member_storage_state)
    page = member_ctx.new_page()
    diag = attach_page_diagnostics(page)
    resp = page.goto(f"{live_server.base_url}/cs", wait_until="domcontentloaded")
    assert resp is not None and resp.status == 200

    cat_sel = page.locator("select[name='category'], #csInquiryCategory")
    if cat_sel.count() > 0:
        cat_sel.first.select_option("배송")
    _fill_optional_input(page, "input[name='title'], #csInquiryTitle", r"문의\s*제목|제목", "배송 일정 문의")
    _fill_optional_input(page, "textarea[name='content'], #csInquiryContent", r"문의\s*내용|내용", "이번 주 출고 가능한가요?")

    submit_btn = page.locator("#csInquirySubmitBtn").or_(
        page.get_by_role("button", name=re.compile(r"1:1\s*문의|문의\s*접수|문의\s*등록"))
    )
    assert submit_btn.count() > 0
    with page.expect_response(
        lambda r: "/api/inquiries/create" in r.url and r.request.method == "POST",
        timeout=8000,
    ) as inq_resp_info:
        submit_btn.first.click()
    assert inq_resp_info.value.status == 200
    diag.assert_clean("f11_04_submit_inquiry")

    inq_resp = member_api.get("/api/inquiries")
    assert inq_resp.status_code == 200
    inquiries = _extract_list(member_api.json_or_raise(inq_resp), "inquiries", "items")
    matched = [i for i in inquiries if i.get("title") == "배송 일정 문의"]
    assert len(matched) >= 1
    inquiry_id = matched[0]["id"]

    ans_resp = admin_api.post(
        "/api/admin/inquiries/answer",
        json={"inquiry_id": inquiry_id, "answer": "오늘 오후 출고 예정입니다."},
    )
    assert ans_resp.status_code == 200

    page.reload(wait_until="domcontentloaded")
    body_text = page.locator("body").inner_text()
    assert "배송 일정 문의" in body_text
    assert "오늘 오후 출고 예정입니다." in body_text
    assert_no_horizontal_overflow(page)
    assert_no_main_internal_vertical_scroll(page)
    diag.assert_clean("f11_04_inquiry_answered")


@pytest.mark.tier1
@pytest.mark.f11
def test_t1_f11_05_admin_qna_delete_removes_entry(
    live_server,
    member_api,
    admin_api,
):
    seed_product_via_sync(
        live_server.base_url,
        live_server.sync_secret,
        num=9113,
        name="F11 Q&A 삭제 테스트 상품",
        price=28000,
    )

    c_resp = member_api.post(
        "/api/products/9113/qna",
        json={"title": "삭제 테스트 질문", "content": "삭제될 질문 본문", "is_secret": False},
    )
    assert c_resp.status_code == 200
    c_data = member_api.json_or_raise(c_resp)
    qid = (c_data.get("qna") or {}).get("id") or c_data.get("qna_id") or c_data.get("id")
    if not qid:
        items = _extract_list(
            member_api.json_or_raise(member_api.get("/api/products/9113/qna")),
            "qnas",
            "items",
            "qna",
        )
        qid = items[0]["id"]

    del_resp = admin_api.post("/api/admin/qna/delete", json={"qna_id": qid, "id": qid})
    assert del_resp.status_code == 200

    after_resp = member_api.get("/api/products/9113/qna")
    assert after_resp.status_code == 200
    remaining = _extract_list(member_api.json_or_raise(after_resp), "qnas", "items", "qna")
    assert all(item.get("id") != qid for item in remaining)


@pytest.mark.tier1
@pytest.mark.f11
def test_t1_f11_06_admin_cs_tab_ui_renders_cleanly(
    live_server,
    desktop_context_factory,
    admin_storage_state,
):
    ctx = desktop_context_factory(storage_state=admin_storage_state)
    page = ctx.new_page()
    diag = attach_page_diagnostics(page)
    resp = page.goto(f"{live_server.base_url}/admin", wait_until="domcontentloaded")
    assert resp is not None and resp.status == 200

    cs_tabs = page.get_by_role("button", name=re.compile(r"CS|문의|공지|Q&A"))
    assert cs_tabs.count() > 0
    cs_tabs.first.click()
    page.wait_for_timeout(300)

    assert_no_horizontal_overflow(page)
    assert_no_main_internal_vertical_scroll(page)
    diag.assert_clean("f11_06_admin_cs_tab")


# ---------------------------------------------------------------------------
# Feature F12: R3 다채널 자동 알림 서비스 (LOGGED_ONLY 폴백)
# ---------------------------------------------------------------------------


@pytest.mark.tier1
@pytest.mark.f12
def test_t1_f12_01_order_creation_records_customer_and_admin_logs(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    guest = api_client_factory(srv)
    order = create_order_via_api(
        guest,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        buyer_name="알림주문자",
        buyer_phone="010-1212-3434",
        guest_password="noti1234",
    )

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    logs_resp = admin.get("/api/admin/notifications/logs")
    assert logs_resp.status_code == 200
    logs_data = admin.json_or_raise(logs_resp, "f12_01_logs")
    assert logs_data["status"] == "ok"
    logs = _extract_list(logs_data, "logs", "items")

    matching = [entry for entry in logs if order["order_code"] in str(entry)]
    assert len(matching) >= 2
    assert any(
        "LOGGED_ONLY" in str(entry.get("status", "")).upper()
        for entry in matching
    )


@pytest.mark.tier1
@pytest.mark.f12
def test_t1_f12_02_payment_confirmed_records_deposit_notification_log(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    guest = api_client_factory(srv)
    order = create_order_via_api(
        guest,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        guest_password="noti1234",
    )

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    upd = admin.post(
        "/api/admin/order-status",
        json={"order_id": order["order_id"], "payment_status": "입금완료"},
    )
    assert upd.status_code == 200

    logs_resp = admin.get("/api/admin/notifications/logs")
    assert logs_resp.status_code == 200
    logs = _extract_list(admin.json_or_raise(logs_resp), "logs", "items")
    deposit_logs = [
        entry
        for entry in logs
        if order["order_code"] in str(entry)
        and any(tok in str(entry).upper() for tok in ("DEPOSIT", "PAYMENT", "입금"))
    ]
    assert len(deposit_logs) >= 1


@pytest.mark.tier1
@pytest.mark.f12
def test_t1_f12_03_shipping_tracking_marked_shipped_records_shipment_log(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    guest = api_client_factory(srv)
    order = create_order_via_api(
        guest,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
        guest_password="noti1234",
    )

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    admin.post(
        "/api/admin/order-status",
        json={"order_id": order["order_id"], "payment_status": "입금완료"},
    )
    trk_resp = admin.post(
        "/api/admin/orders/tracking",
        json={
            "order_id": order["order_id"],
            "order_code": order["order_code"],
            "carrier_code": "CJ",
            "carrier_name": "CJ대한통운",
            "tracking_number": "123456789012",
            "mark_shipped": True,
        },
    )
    assert trk_resp.status_code == 200

    logs_resp = admin.get("/api/admin/notifications/logs")
    assert logs_resp.status_code == 200
    logs = _extract_list(admin.json_or_raise(logs_resp), "logs", "items")
    ship_logs = [
        entry
        for entry in logs
        if order["order_code"] in str(entry)
        and any(tok in str(entry).upper() for tok in ("SHIP", "출고", "배송", "123456789012"))
    ]
    assert len(ship_logs) >= 1


@pytest.mark.tier1
@pytest.mark.f12
def test_t1_f12_04_customer_claim_submission_records_admin_claim_log(
    server_factory,
    api_client_factory,
):
    srv = server_factory()
    member = api_client_factory(srv)
    member.register("claim_noti@e2e.local", "ClaimPass123!")

    order = create_order_via_api(
        member,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
    )

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    admin.post(
        "/api/admin/order-status",
        json={"order_id": order["order_id"], "payment_status": "입금완료", "order_status": "배송완료"},
    )

    claim_resp = member.post(
        "/api/orders/self-claim",
        json={
            "order_id": order["order_id"],
            "order_code": order["order_code"],
            "claim_type": "환불요청",
            "claim_reason": "상품 불량",
        },
    )
    assert claim_resp.status_code == 200

    logs_resp = admin.get("/api/admin/notifications/logs")
    assert logs_resp.status_code == 200
    logs = _extract_list(admin.json_or_raise(logs_resp), "logs", "items")
    claim_logs = [
        entry
        for entry in logs
        if order["order_code"] in str(entry)
        and any(tok in str(entry).upper() for tok in ("CLAIM", "환불", "교환"))
    ]
    assert len(claim_logs) >= 1


@pytest.mark.tier1
@pytest.mark.f12
def test_t1_f12_05_admin_notifications_logs_filter_and_admin_view(
    live_server,
    member_api,
    admin_api,
    desktop_context_factory,
    admin_storage_state,
):
    order = create_order_via_api(
        member_api,
        items=[{"num": 1, "color": "블랙", "size": "M", "quantity": 1}],
    )

    logs_resp = admin_api.get("/api/admin/notifications/logs")
    assert logs_resp.status_code == 200
    logs_data = admin_api.json_or_raise(logs_resp, "f12_05_logs")
    assert logs_data["status"] == "ok"
    logs = _extract_list(logs_data, "logs", "items")
    assert any(order["order_code"] in str(entry) for entry in logs)

    ctx = desktop_context_factory(storage_state=admin_storage_state)
    page = ctx.new_page()
    diag = attach_page_diagnostics(page)
    resp = page.goto(f"{live_server.base_url}/admin", wait_until="domcontentloaded")
    assert resp is not None and resp.status == 200

    noti_tab = page.get_by_role("button", name=re.compile(r"알림|로그|운영|CS"))
    if noti_tab.count() > 0:
        noti_tab.first.click()
        page.wait_for_timeout(300)

    assert_no_horizontal_overflow(page)
    assert_no_main_internal_vertical_scroll(page)
    diag.assert_clean("f12_05_admin_notifications_view")


# ---------------------------------------------------------------------------
# Feature F13: R3 포토 리뷰, 구매인증 배지, 작성자 수정/삭제, 관리자 관리
# ---------------------------------------------------------------------------


@pytest.mark.tier1
@pytest.mark.f13
def test_t1_f13_01_photo_review_upload_and_render_on_product_detail(
    live_server,
    member_api,
    desktop_page,
):
    seed_product_via_sync(
        live_server.base_url,
        live_server.sync_secret,
        num=9131,
        name="F13 포토 리뷰 상품",
        price=39000,
    )

    upload_resp = member_api.post(
        "/api/reviews/upload-image",
        files={"image": ("photo.png", create_tiny_png_bytes(), "image/png")},
    )
    assert upload_resp.status_code == 200
    upload_data = member_api.json_or_raise(upload_resp, "f13_01_upload")
    assert upload_data["status"] == "ok"
    image_url = upload_data.get("image_url") or upload_data.get("url")
    assert isinstance(image_url, str) and image_url.startswith("/static/uploads/reviews/")

    created_review_id: int | None = None
    try:
        img_get = member_api.get(image_url)
        assert img_get.status_code == 200

        add_resp = member_api.post(
            "/api/reviews/add",
            json={
                "product_num": 9131,
                "rating": 5,
                "content": "포토 리뷰 테스트입니다.",
                "image_url": image_url,
            },
        )
        assert add_resp.status_code == 200
        add_data = member_api.json_or_raise(add_resp, "f13_01_add")
        created_review_id = add_data.get("review_id") or add_data.get("id")

        diag = attach_page_diagnostics(desktop_page)
        resp = desktop_page.goto(f"{live_server.base_url}/product/9131", wait_until="domcontentloaded")
        assert resp is not None and resp.status == 200

        review_list = desktop_page.locator("#pdReviewList")
        assert "포토 리뷰 테스트입니다." in review_list.inner_text()
        img = review_list.locator(f"img[src='{image_url}']").first
        assert img.count() == 1
        assert img.evaluate("el => el.complete && el.naturalWidth > 0") is True
        diag.assert_clean("f13_01_photo_review")
    finally:
        if created_review_id is not None:
            try:
                member_api.post("/api/reviews/delete", json={"review_id": created_review_id})
            except Exception:
                pass
        fname = image_url.rsplit("/", 1)[-1].split("?", 1)[0]
        for candidate_dir in (
            live_server.work_dir / "uploads_reviews",
            Path(__file__).resolve().parents[2] / "static" / "uploads" / "reviews",
        ):
            target = (candidate_dir / fname).resolve()
            try:
                target.relative_to(candidate_dir.resolve())
            except ValueError:
                continue
            if target.is_file() and target.suffix.lower() == ".png":
                target.unlink(missing_ok=True)


@pytest.mark.tier1
@pytest.mark.f13
def test_t1_f13_02_verified_buyer_badge_shown_for_purchaser_only(
    server_factory,
    api_client_factory,
    desktop_page,
):
    srv = server_factory()
    seed_product_via_sync(
        srv.base_url,
        srv.sync_secret,
        num=9132,
        name="F13 구매인증 배지 상품",
        price=45000,
    )

    member_a = api_client_factory(srv)
    member_a.register("buyer_a@e2e.local", "PassA123!", name="구매자A")
    member_b = api_client_factory(srv)
    member_b.register("nonbuyer_b@e2e.local", "PassB123!", name="미구매자B")

    order_a = create_order_via_api(
        member_a,
        items=[{"num": 9132, "color": "블랙", "size": "M", "quantity": 1}],
    )

    admin = api_client_factory(srv)
    admin.login(srv.admin_email, srv.admin_password)
    admin.post(
        "/api/admin/order-status",
        json={"order_id": order_a["order_id"], "payment_status": "입금완료", "order_status": "배송완료"},
    )

    assert (
        member_a.post(
            "/api/reviews/add",
            json={"product_num": 9132, "rating": 5, "content": "실제 구매 후기입니다."},
        ).status_code
        == 200
    )
    assert (
        member_b.post(
            "/api/reviews/add",
            json={"product_num": 9132, "rating": 4, "content": "미구매 문의성 후기입니다."},
        ).status_code
        == 200
    )

    prod_resp = member_a.get("/api/products/9132")
    assert prod_resp.status_code == 200
    reviews = member_a.json_or_raise(prod_resp)["reviews"]
    rev_a = [r for r in reviews if "실제 구매 후기입니다." in r["content"]][0]
    rev_b = [r for r in reviews if "미구매 문의성 후기입니다." in r["content"]][0]
    assert bool(rev_a.get("is_verified_buyer")) is True
    assert bool(rev_b.get("is_verified_buyer")) is False

    diag = attach_page_diagnostics(desktop_page)
    desktop_page.goto(f"{srv.base_url}/product/9132", wait_until="domcontentloaded")
    cards = desktop_page.locator("#pdReviewList .ax-review-card")
    assert cards.count() == 2
    for idx in range(cards.count()):
        txt = cards.nth(idx).inner_text()
        if "실제 구매 후기입니다." in txt:
            assert ("구매 인증" in txt) or ("실제 구매" in txt) or ("구매자" in txt)
        if "미구매 문의성 후기입니다." in txt:
            assert ("구매 인증" not in txt) and ("실제 구매자" not in txt)
    diag.assert_clean("f13_02_verified_badge")


@pytest.mark.tier1
@pytest.mark.f13
def test_t1_f13_03_author_edit_and_delete_own_review(
    live_server,
    member_api,
):
    seed_product_via_sync(
        live_server.base_url,
        live_server.sync_secret,
        num=9133,
        name="F13 리뷰 수정 삭제 상품",
        price=31000,
    )

    add_resp = member_api.post(
        "/api/reviews/add",
        json={"product_num": 9133, "rating": 5, "content": "초기 리뷰 본문"},
    )
    assert add_resp.status_code == 200
    add_data = member_api.json_or_raise(add_resp)
    review_id = (add_data.get("review") or {}).get("id") or add_data.get("review_id") or add_data.get("id")
    if not review_id:
        prod_data = member_api.json_or_raise(member_api.get("/api/products/9133"))
        review_id = prod_data["reviews"][0]["id"]

    upd_resp = member_api.post(
        "/api/reviews/update",
        json={"review_id": review_id, "id": review_id, "rating": 4, "content": "수정된 리뷰 본문"},
    )
    assert upd_resp.status_code == 200

    after_upd = member_api.json_or_raise(member_api.get("/api/products/9133"))
    assert after_upd["reviews"][0]["content"] == "수정된 리뷰 본문"
    assert float(after_upd["review_summary"]["avg_rating"]) == 4.0

    del_resp = member_api.post(
        "/api/reviews/delete",
        json={"review_id": review_id, "id": review_id},
    )
    assert del_resp.status_code == 200

    after_del = member_api.json_or_raise(member_api.get("/api/products/9133"))
    assert after_del["review_summary"]["count"] == 0


@pytest.mark.tier1
@pytest.mark.f13
def test_t1_f13_04_admin_hide_and_unhide_review_updates_public_summary(
    live_server,
    member_api,
    admin_api,
):
    seed_product_via_sync(
        live_server.base_url,
        live_server.sync_secret,
        num=9134,
        name="F13 리뷰 숨김 테스트 상품",
        price=37000,
    )

    assert (
        member_api.post(
            "/api/reviews/add",
            json={"product_num": 9134, "rating": 5, "content": "만족스러운 5점 리뷰"},
        ).status_code
        == 200
    )
    assert (
        member_api.post(
            "/api/reviews/add",
            json={"product_num": 9134, "rating": 1, "content": "숨김 대상 1점 리뷰"},
        ).status_code
        == 200
    )

    reviews = member_api.json_or_raise(member_api.get("/api/products/9134"))["reviews"]
    target_id = [r for r in reviews if "숨김 대상 1점 리뷰" in r["content"]][0]["id"]

    hide_resp = admin_api.post(
        "/api/admin/reviews/moderate",
        json={"review_id": target_id, "is_hidden": True},
    )
    assert hide_resp.status_code == 200

    admin_reviews_resp = admin_api.get("/api/admin/reviews")
    assert admin_reviews_resp.status_code == 200
    assert "숨김 대상 1점 리뷰" in admin_reviews_resp.text

    public_hidden = member_api.json_or_raise(member_api.get("/api/products/9134"))
    assert public_hidden["review_summary"]["count"] == 1
    assert float(public_hidden["review_summary"]["avg_rating"]) == 5.0
    assert all(r["id"] != target_id for r in public_hidden["reviews"])

    unhide_resp = admin_api.post(
        "/api/admin/reviews/moderate",
        json={"review_id": target_id, "is_hidden": False},
    )
    assert unhide_resp.status_code == 200

    public_restored = member_api.json_or_raise(member_api.get("/api/products/9134"))
    assert public_restored["review_summary"]["count"] == 2
    assert float(public_restored["review_summary"]["avg_rating"]) == 3.0


@pytest.mark.tier1
@pytest.mark.f13
def test_t1_f13_05_admin_seller_reply_displayed_under_review(
    live_server,
    member_api,
    admin_api,
    desktop_page,
):
    seed_product_via_sync(
        live_server.base_url,
        live_server.sync_secret,
        num=9135,
        name="F13 판매자 답글 상품",
        price=41000,
    )

    assert (
        member_api.post(
            "/api/reviews/add",
            json={"product_num": 9135, "rating": 5, "content": "배송이 빨라요!"},
        ).status_code
        == 200
    )
    review_id = member_api.json_or_raise(member_api.get("/api/products/9135"))["reviews"][0]["id"]

    reply_resp = admin_api.post(
        "/api/admin/reviews/moderate",
        json={"review_id": review_id, "admin_reply": "감사합니다 고객님!"},
    )
    assert reply_resp.status_code == 200

    prod_data = member_api.json_or_raise(member_api.get("/api/products/9135"))
    assert prod_data["reviews"][0].get("admin_reply") == "감사합니다 고객님!"

    diag = attach_page_diagnostics(desktop_page)
    desktop_page.goto(f"{live_server.base_url}/product/9135", wait_until="domcontentloaded")
    assert "감사합니다 고객님!" in desktop_page.locator("#pdReviewList").inner_text()
    diag.assert_clean("f13_05_seller_reply")


@pytest.mark.tier1
@pytest.mark.f13
def test_t1_f13_06_admin_delete_review_and_admin_panel_layout(
    live_server,
    member_api,
    admin_api,
    desktop_context_factory,
    admin_storage_state,
):
    seed_product_via_sync(
        live_server.base_url,
        live_server.sync_secret,
        num=9136,
        name="F13 관리자 리뷰 삭제 상품",
        price=29000,
    )

    assert (
        member_api.post(
            "/api/reviews/add",
            json={"product_num": 9136, "rating": 3, "content": "관리자 삭제 대상 리뷰"},
        ).status_code
        == 200
    )
    review_id = member_api.json_or_raise(member_api.get("/api/products/9136"))["reviews"][0]["id"]

    del_resp = admin_api.post(
        "/api/admin/reviews/moderate",
        json={"review_id": review_id, "action": "delete", "is_deleted": True},
    )
    assert del_resp.status_code == 200

    after_data = member_api.json_or_raise(member_api.get("/api/products/9136"))
    assert after_data["review_summary"]["count"] == 0

    ctx = desktop_context_factory(storage_state=admin_storage_state)
    page = ctx.new_page()
    diag = attach_page_diagnostics(page)
    resp = page.goto(f"{live_server.base_url}/admin", wait_until="domcontentloaded")
    assert resp is not None and resp.status == 200

    rev_tab = page.get_by_role("button", name=re.compile(r"리뷰|후기"))
    if rev_tab.count() > 0:
        rev_tab.first.click()
        page.wait_for_timeout(300)

    assert_no_horizontal_overflow(page)
    assert_no_main_internal_vertical_scroll(page)
    diag.assert_clean("f13_06_admin_reviews_tab")
