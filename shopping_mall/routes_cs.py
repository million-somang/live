# -*- coding: utf-8 -*-
"""
쇼핑몰 고객센터(공지사항/FAQ/1:1문의) 및 상품 Q&A Blueprint (routes_cs.py)
- /api/admin/* 경로는 app.py의 _mall_security_gateway가 ADMIN 권한 및 CSRF 자동 검증
- 회원/공개 POST 경로는 로그인(401), CSRF(403), Rate Limit(429) 직접 검증
"""

import io
import os
import re
import uuid
from typing import Any, Optional
from flask import Blueprint, current_app, jsonify, render_template, request, send_from_directory, session

import mall_cs_service
import mall_database
import mall_ops
import mall_security

cs_bp = Blueprint("cs_bp", __name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_UPLOAD_REVIEWS_DIR = os.path.join(_BASE_DIR, "static", "uploads", "reviews")
UPLOAD_REVIEWS_DIR = os.path.abspath(
    os.getenv("MALL_REVIEW_UPLOAD_DIR", "").strip() or _DEFAULT_UPLOAD_REVIEWS_DIR
)
os.makedirs(UPLOAD_REVIEWS_DIR, exist_ok=True)
_ALLOWED_REVIEW_IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}
_MAX_REVIEW_IMAGE_BYTES = 5 * 1024 * 1024


def _get_upload_reviews_dir() -> str:
    if UPLOAD_REVIEWS_DIR != os.path.abspath(_DEFAULT_UPLOAD_REVIEWS_DIR):
        return UPLOAD_REVIEWS_DIR
    env_dir = os.getenv("MALL_REVIEW_UPLOAD_DIR", "").strip()
    return os.path.abspath(env_dir) if env_dir else UPLOAD_REVIEWS_DIR


@cs_bp.route("/static/uploads/reviews/<path:filename>", methods=["GET"])
def serve_review_image(filename):
    safe_name = os.path.basename(str(filename or ""))
    if not safe_name or safe_name != str(filename or "").strip():
        return jsonify({"status": "error", "message": "유효하지 않은 파일명입니다."}), 404
    return send_from_directory(_get_upload_reviews_dir(), safe_name)


def _resolve_current_user() -> Optional[dict]:
    fn = current_app.config.get("MALL_GET_CURRENT_USER")
    if callable(fn):
        return fn()
    uid = session.get("mall_user_id")
    if uid:
        return mall_database.get_mall_user_by_id(int(uid))
    return None


def _read_request_payload() -> dict:
    if request.is_json:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}
    return request.form.to_dict()


@cs_bp.route("/cs", methods=["GET"])
def mall_cs_page():
    user = _resolve_current_user()
    notices = mall_cs_service.list_notices(limit=100)
    faqs = mall_cs_service.list_faqs(active_only=True, limit=200)
    inquiries = mall_cs_service.list_user_inquiries(int(user["id"]), limit=100) if user else []
    live_state = mall_database.get_full_live_state()
    return render_template(
        "cs.html",
        notices=notices,
        faqs=faqs,
        inquiries=inquiries,
        inquiry_categories=list(mall_cs_service.INQUIRY_CATEGORIES),
        live_state=live_state,
        active_nav="cs",
    )


@cs_bp.route("/api/products/<int:num>/qna", methods=["GET"])
def api_product_qna_list(num: int):
    p_num = mall_database.parse_strict_int64(num, min_val=1)
    if p_num is None:
        return jsonify({"status": "error", "message": "상품을 찾을 수 없습니다."}), 404
    prod = mall_database.get_mall_product_by_num(p_num)
    if not prod:
        return jsonify({"status": "error", "message": "상품을 찾을 수 없습니다."}), 404
    viewer = _resolve_current_user()
    items = mall_cs_service.list_product_qna(p_num, viewer=viewer, limit=100)
    return jsonify({"status": "ok", "ok": True, "items": items, "qna": items, "count": len(items)}), 200


@cs_bp.route("/api/products/<int:num>/qna", methods=["POST"])
def api_product_qna_create(num: int):
    user = _resolve_current_user()
    if not user:
        return jsonify({"status": "error", "code": "AUTH_REQUIRED", "message": "로그인한 회원만 상품 문의를 작성할 수 있습니다."}), 401
    if not mall_security.verify_csrf_token(session, request):
        return jsonify({"status": "error", "code": "CSRF_INVALID", "message": "CSRF 보안 토큰 검증에 실패했습니다."}), 403

    p_num = mall_database.parse_strict_int64(num, min_val=1)
    if p_num is None:
        return jsonify({"status": "error", "message": "상품을 찾을 수 없습니다."}), 404

    ip_addr = str(request.remote_addr or "unknown")
    allowed, retry_sec = mall_security.rate_limiter.check("cs_qna_create", f"{ip_addr}:{user['id']}", max_requests=20, window_sec=60.0)
    if not allowed:
        return jsonify({"status": "error", "message": f"요청이 너무 많습니다. {retry_sec}초 후 다시 시도해 주세요."}), 429

    data = _read_request_payload()
    res = mall_cs_service.create_product_qna(p_num, user, data)
    if not res.get("ok"):
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "message": res.get("message") or "상품 Q&A 등록에 실패했습니다."}), status

    items = mall_cs_service.list_product_qna(p_num, viewer=user, limit=100)
    created_item = res.get("item") or {}
    return jsonify({
        "status": "ok",
        "ok": True,
        "id": created_item.get("id"),
        "qna_id": created_item.get("id"),
        "item": created_item,
        "qna": created_item,
        "items": items,
        "qnas": items,
        "count": len(items),
    }), 200


@cs_bp.route("/api/admin/qna", methods=["GET"])
def api_admin_qna_list():
    raw_num = request.args.get("product_num")
    p_num = None
    if raw_num is not None and str(raw_num).strip() != "":
        p_num = mall_database.parse_strict_int64(raw_num, min_val=1)
        if p_num is None:
            return jsonify({"status": "error", "ok": False, "message": "유효하지 않은 상품 번호입니다."}), 400
    status_filter = str(request.args.get("status") or "ALL")
    items = mall_cs_service.list_admin_qna(product_num=p_num, status_filter=status_filter, limit=200)
    return jsonify({"status": "ok", "ok": True, "items": items, "qna": items, "count": len(items)}), 200


@cs_bp.route("/api/admin/qna/answer", methods=["POST"])
def api_admin_qna_answer():
    user = _resolve_current_user()
    data = _read_request_payload()
    qid = data.get("qna_id") or data.get("id")
    answer_content = str(data.get("answer_content") if data.get("answer_content") is not None else (data.get("answer") or ""))
    res = mall_cs_service.answer_product_qna(qid, answer_content, user or {})
    if not res.get("ok"):
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "message": res.get("message") or "Q&A 답변 등록에 실패했습니다."}), status
    item = res.get("item") or {}
    mall_ops.log_current_admin_action(
        action="QNA_ANSWER",
        target_type="qna",
        target_id=item.get("id") or qid,
        detail_dict={
            "qna_id": item.get("id") or qid,
            "product_num": item.get("product_num"),
            "answered_len": len(answer_content.strip()),
        },
    )
    return jsonify({"status": "ok", "ok": True, "item": item}), 200


@cs_bp.route("/api/admin/qna/delete", methods=["POST"])
def api_admin_qna_delete():
    data = _read_request_payload()
    qid = data.get("qna_id") or data.get("id")
    res = mall_cs_service.delete_product_qna(qid)
    if not res.get("ok"):
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "message": res.get("message") or "Q&A 삭제에 실패했습니다."}), status
    mall_ops.log_current_admin_action(
        action="QNA_DELETE",
        target_type="qna",
        target_id=res.get("deleted_id") or qid,
        detail_dict={"deleted_id": res.get("deleted_id")},
    )
    return jsonify({"status": "ok", "ok": True, "deleted_id": res.get("deleted_id")}), 200


@cs_bp.route("/api/inquiries", methods=["GET"])
def api_inquiries_list():
    user = _resolve_current_user()
    if not user:
        return jsonify({"status": "error", "code": "AUTH_REQUIRED", "message": "로그인이 필요합니다."}), 401
    scope = str(request.args.get("scope") or "").strip().lower()
    if scope == "all" and str(user.get("role") or "").upper() == "ADMIN":
        items = mall_cs_service.list_admin_inquiries(status_filter=str(request.args.get("status") or "ALL"), limit=200)
    else:
        items = mall_cs_service.list_user_inquiries(int(user["id"]), limit=100)
    return jsonify({"status": "ok", "ok": True, "inquiries": items, "items": items, "count": len(items)}), 200


@cs_bp.route("/api/inquiries/create", methods=["POST"])
def api_inquiries_create():
    user = _resolve_current_user()
    if not user:
        return jsonify({"status": "error", "code": "AUTH_REQUIRED", "message": "로그인한 회원만 1:1 문의를 작성할 수 있습니다."}), 401
    if not mall_security.verify_csrf_token(session, request):
        return jsonify({"status": "error", "code": "CSRF_INVALID", "message": "CSRF 보안 토큰 검증에 실패했습니다."}), 403

    ip_addr = str(request.remote_addr or "unknown")
    allowed, retry_sec = mall_security.rate_limiter.check("cs_inquiry_create", f"{ip_addr}:{user['id']}", max_requests=15, window_sec=60.0)
    if not allowed:
        return jsonify({"status": "error", "message": f"요청이 너무 많습니다. {retry_sec}초 후 다시 시도해 주세요."}), 429

    data = _read_request_payload()
    res = mall_cs_service.create_inquiry(user, data)
    if not res.get("ok"):
        return jsonify({"status": "error", "message": res.get("message") or "1:1 문의 등록에 실패했습니다."}), 400
    items = mall_cs_service.list_user_inquiries(int(user["id"]), limit=100)
    created_item = res.get("item") or {}
    return jsonify({
        "status": "ok",
        "ok": True,
        "id": created_item.get("id"),
        "item": created_item,
        "inquiry": created_item,
        "inquiries": items,
        "count": len(items),
    }), 200


@cs_bp.route("/api/admin/inquiries", methods=["GET"])
def api_admin_inquiries_list():
    status_filter = str(request.args.get("status") or "ALL")
    items = mall_cs_service.list_admin_inquiries(status_filter=status_filter, limit=200)
    return jsonify({"status": "ok", "ok": True, "inquiries": items, "items": items, "count": len(items)}), 200


@cs_bp.route("/api/admin/inquiries/answer", methods=["POST"])
def api_admin_inquiries_answer():
    user = _resolve_current_user()
    data = _read_request_payload()
    iid = data.get("inquiry_id") or data.get("id")
    ans = str(data.get("answer_content") if data.get("answer_content") is not None else (data.get("answer") or ""))
    next_status = str(data.get("status") or "답변완료")
    res = mall_cs_service.answer_inquiry(iid, ans, user or {}, status=next_status)
    if not res.get("ok"):
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "message": res.get("message") or "1:1 문의 답변 등록에 실패했습니다."}), status
    item = res.get("item") or {}
    mall_ops.log_current_admin_action(
        action="INQUIRY_ANSWER",
        target_type="inquiry",
        target_id=item.get("id") or iid,
        detail_dict={
            "inquiry_id": item.get("id") or iid,
            "status": item.get("status") or next_status,
        },
    )
    return jsonify({"status": "ok", "ok": True, "item": item}), 200


@cs_bp.route("/api/notices", methods=["GET"])
def api_notices_list():
    items = mall_cs_service.list_notices(limit=100)
    return jsonify({"status": "ok", "ok": True, "notices": items, "items": items, "count": len(items)}), 200


@cs_bp.route("/api/admin/notices/save", methods=["POST"])
def api_admin_notices_save():
    user = _resolve_current_user()
    data = _read_request_payload()
    res = mall_cs_service.save_notice(data, admin_user=user)
    if not res.get("ok"):
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "message": res.get("message") or "공지사항 저장에 실패했습니다."}), status
    saved_item = res.get("item") or {}
    mall_ops.log_current_admin_action(
        action="NOTICE_SAVE",
        target_type="notice",
        target_id=saved_item.get("id") or "",
        detail_dict={
            "id": saved_item.get("id"),
            "title": saved_item.get("title"),
            "is_pinned": saved_item.get("is_pinned"),
        },
    )
    return jsonify({
        "status": "ok",
        "ok": True,
        "id": saved_item.get("id"),
        "item": saved_item,
        "notice": saved_item,
    }), 200


@cs_bp.route("/api/admin/notices/delete", methods=["POST"])
def api_admin_notices_delete():
    data = _read_request_payload()
    nid = data.get("notice_id") or data.get("id")
    res = mall_cs_service.delete_notice(nid)
    if not res.get("ok"):
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "message": res.get("message") or "공지사항 삭제에 실패했습니다."}), status
    mall_ops.log_current_admin_action(
        action="NOTICE_DELETE",
        target_type="notice",
        target_id=res.get("deleted_id") or nid,
        detail_dict={"deleted_id": res.get("deleted_id")},
    )
    return jsonify({"status": "ok", "ok": True, "deleted_id": res.get("deleted_id")}), 200


@cs_bp.route("/api/faqs", methods=["GET"])
def api_faqs_list():
    user = _resolve_current_user()
    include_inactive = str(request.args.get("include_inactive") or "") in ("1", "true", "yes")
    active_only = not (include_inactive and user and str(user.get("role") or "").upper() == "ADMIN")
    cat = str(request.args.get("category") or "ALL")
    items = mall_cs_service.list_faqs(active_only=active_only, category=cat, limit=200)
    return jsonify({"status": "ok", "ok": True, "faqs": items, "items": items, "count": len(items)}), 200


@cs_bp.route("/api/admin/faqs", methods=["GET"])
def api_admin_faqs_list():
    cat = str(request.args.get("category") or "ALL")
    items = mall_cs_service.list_faqs(active_only=False, category=cat, limit=200)
    return jsonify({"status": "ok", "ok": True, "faqs": items, "items": items, "count": len(items)}), 200


@cs_bp.route("/api/admin/faqs/save", methods=["POST"])
def api_admin_faqs_save():
    data = _read_request_payload()
    res = mall_cs_service.save_faq(data)
    if not res.get("ok"):
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "message": res.get("message") or "FAQ 저장에 실패했습니다."}), status
    saved_item = res.get("item") or {}
    mall_ops.log_current_admin_action(
        action="FAQ_SAVE",
        target_type="faq",
        target_id=saved_item.get("id") or "",
        detail_dict={
            "id": saved_item.get("id"),
            "category": saved_item.get("category"),
            "is_active": saved_item.get("is_active"),
        },
    )
    return jsonify({
        "status": "ok",
        "ok": True,
        "id": saved_item.get("id"),
        "item": saved_item,
        "faq": saved_item,
    }), 200


@cs_bp.route("/api/admin/faqs/delete", methods=["POST"])
def api_admin_faqs_delete():
    data = _read_request_payload()
    fid = data.get("faq_id") or data.get("id")
    res = mall_cs_service.delete_faq(fid)
    if not res.get("ok"):
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "message": res.get("message") or "FAQ 삭제에 실패했습니다."}), status
    mall_ops.log_current_admin_action(
        action="FAQ_DELETE",
        target_type="faq",
        target_id=res.get("deleted_id") or fid,
        detail_dict={"deleted_id": res.get("deleted_id")},
    )
    return jsonify({"status": "ok", "ok": True, "deleted_id": res.get("deleted_id")}), 200


# ==============================================================================
# ⭐ 포토 리뷰 · 구매 인증 · 작성자 수정/삭제 · 관리자 모더레이션 API (F13)
# ==============================================================================


def _parse_strict_review_rating(raw_val: Any) -> tuple[bool, Optional[int], str]:
    if raw_val is None or isinstance(raw_val, (bool, float)):
        return False, None, "별점은 1~5 사이의 정수로 선택해 주세요."
    if isinstance(raw_val, int):
        parsed = raw_val
    else:
        s = str(raw_val).strip()
        if not re.fullmatch(r"[0-9]+", s) or len(s) > 10:
            return False, None, "별점은 1~5 사이의 정수로 선택해 주세요."
        parsed = int(s)
    if parsed < 1 or parsed > 5:
        return False, None, "별점은 1점에서 5점 사이여야 합니다."
    return True, parsed, ""


def _parse_strict_positive_int(raw_val: Any) -> Optional[int]:
    return mall_database.parse_strict_int64(raw_val, min_val=1)


def _validate_review_image_bytes(raw_bytes: bytes, ext: str) -> tuple[bool, str]:
    if not raw_bytes:
        return False, "빈 이미지 파일은 업로드할 수 없습니다."
    if len(raw_bytes) > _MAX_REVIEW_IMAGE_BYTES:
        return False, "리뷰 이미지는 5MB 이하만 업로드할 수 있습니다."

    e = ext.lower()
    magic_ok = False
    if e == "png":
        magic_ok = raw_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    elif e in ("jpg", "jpeg"):
        magic_ok = raw_bytes.startswith(b"\xff\xd8\xff")
    elif e == "gif":
        magic_ok = raw_bytes.startswith(b"GIF87a") or raw_bytes.startswith(b"GIF89a")
    elif e == "webp":
        magic_ok = len(raw_bytes) >= 12 and raw_bytes[:4] == b"RIFF" and raw_bytes[8:12] == b"WEBP"

    if not magic_ok:
        return False, "이미지 파일의 실제 헤더가 확장자와 일치하지 않습니다."

    try:
        from PIL import Image
        expected_fmt = "JPEG" if e in ("jpg", "jpeg") else e.upper()
        with Image.open(io.BytesIO(raw_bytes)) as im:
            detected_fmt = str(im.format or "").upper()
            im.verify()
        if detected_fmt != expected_fmt:
            return False, "이미지 포맷 검증에 실패했습니다."
    except ImportError:
        pass
    except Exception:
        return False, "손상되었거나 유효하지 않은 이미지 파일입니다."

    return True, ""


@cs_bp.route("/api/reviews/upload-image", methods=["POST"])
def api_review_upload_image():
    user = _resolve_current_user()
    if not user:
        return jsonify({
            "status": "error",
            "code": "AUTH_REQUIRED",
            "message": "리뷰 사진 업로드는 로그인한 회원만 이용할 수 있습니다.",
        }), 401
    if not mall_security.verify_csrf_token(session, request):
        return jsonify({
            "status": "error",
            "code": "CSRF_INVALID",
            "message": "보안 토큰(CSRF) 검증에 실패했습니다.",
        }), 403

    ip_addr = str(request.remote_addr or "unknown")
    allowed, retry_sec = mall_security.rate_limiter.check(
        "review_upload_image", f"{ip_addr}:{user['id']}", max_requests=25, window_sec=60.0
    )
    if not allowed:
        return jsonify({"status": "error", "message": f"요청이 너무 많습니다. {retry_sec}초 후 다시 시도해 주세요."}), 429

    file_obj = request.files.get("image") or request.files.get("file")
    if not file_obj or not str(file_obj.filename or "").strip():
        return jsonify({"status": "error", "message": "업로드할 이미지 파일을 선택해 주세요."}), 400

    orig_name = os.path.basename(str(file_obj.filename or ""))
    ext = os.path.splitext(orig_name)[1].lstrip(".").lower()
    if ext not in _ALLOWED_REVIEW_IMAGE_EXTS:
        return jsonify({"status": "error", "message": "PNG, JPG, JPEG, WEBP, GIF 형식의 이미지만 업로드할 수 있습니다."}), 400

    raw_bytes = file_obj.read(_MAX_REVIEW_IMAGE_BYTES + 1)
    ok_img, img_err = _validate_review_image_bytes(raw_bytes, ext)
    if not ok_img:
        return jsonify({"status": "error", "message": img_err}), 400

    upload_dir = _get_upload_reviews_dir()
    os.makedirs(upload_dir, exist_ok=True)
    safe_filename = f"{uuid.uuid4().hex}.{ext}"
    save_path = os.path.join(upload_dir, safe_filename)
    with open(save_path, "wb") as f:
        f.write(raw_bytes)

    url = f"/static/uploads/reviews/{safe_filename}"
    return jsonify({
        "status": "ok",
        "ok": True,
        "url": url,
        "image_url": url,
    }), 200


@cs_bp.route("/api/reviews/add", methods=["POST"])
def api_review_add():
    user = _resolve_current_user()
    if not user:
        return jsonify({
            "status": "error",
            "code": "AUTH_REQUIRED",
            "message": "리뷰 작성은 로그인한 회원만 이용하실 수 있습니다.",
        }), 401
    if not mall_security.verify_csrf_token(session, request):
        return jsonify({
            "status": "error",
            "code": "CSRF_INVALID",
            "message": "보안 토큰(CSRF) 검증에 실패했습니다. 페이지를 새로고침해 주세요.",
        }), 403

    ip_addr = str(request.remote_addr or "unknown")
    allowed, retry_sec = mall_security.rate_limiter.check(
        "review_add", f"{ip_addr}:{user['id']}", max_requests=20, window_sec=60.0
    )
    if not allowed:
        return jsonify({"status": "error", "message": f"요청이 너무 많습니다. {retry_sec}초 후 다시 시도해 주세요."}), 429

    data = _read_request_payload()
    p_num = _parse_strict_positive_int(data.get("product_num"))
    if not p_num or not mall_database.get_mall_product_by_num(p_num):
        return jsonify({"status": "error", "message": "리뷰를 등록할 상품을 찾을 수 없습니다."}), 404

    ok_rate, rating, rate_err = _parse_strict_review_rating(data.get("rating"))
    if not ok_rate or rating is None:
        return jsonify({"status": "error", "message": rate_err}), 400

    content = str(data.get("content") or "").strip()
    if len(content) < 2:
        return jsonify({"status": "error", "message": "리뷰 내용을 2자 이상 입력해 주세요."}), 400
    if len(content) > 1000:
        return jsonify({"status": "error", "message": "리뷰 내용은 1,000자 이내로 입력해 주세요."}), 400

    ok_imgs, imgs_list, img_err = mall_database.parse_review_images(
        raw_images=data.get("images"),
        raw_image_url=data.get("image_url"),
    )
    if not ok_imgs:
        return jsonify({"status": "error", "message": img_err}), 400

    author = str(user.get("name") or "구매회원").strip() or "구매회원"
    created = mall_database.add_mall_review(
        p_num,
        author,
        rating,
        content,
        user_id=user.get("id"),
        images=imgs_list,
        image_url=imgs_list[0] if imgs_list else "",
    )
    reviews = mall_database.get_mall_reviews(p_num, viewer=user)
    summary = mall_database.get_mall_review_summary(p_num)
    return jsonify({
        "status": "ok",
        "ok": True,
        "id": created.get("id"),
        "review_id": created.get("id"),
        "review": created,
        "reviews": reviews,
        "summary": summary,
    }), 200


@cs_bp.route("/api/reviews/update", methods=["POST"])
def api_review_update():
    user = _resolve_current_user()
    if not user:
        return jsonify({
            "status": "error",
            "code": "AUTH_REQUIRED",
            "message": "리뷰 수정은 로그인한 회원만 이용할 수 있습니다.",
        }), 401
    if not mall_security.verify_csrf_token(session, request):
        return jsonify({
            "status": "error",
            "code": "CSRF_INVALID",
            "message": "보안 토큰(CSRF) 검증에 실패했습니다.",
        }), 403

    data = _read_request_payload()
    rid = _parse_strict_positive_int(data.get("review_id") if data.get("review_id") is not None else data.get("id"))
    if not rid:
        return jsonify({"status": "error", "message": "올바른 리뷰 ID가 필요합니다."}), 400

    existing = mall_database.get_mall_review_by_id(rid, viewer=user)
    if not existing:
        return jsonify({"status": "error", "message": "수정할 리뷰를 찾을 수 없습니다."}), 404

    is_admin = str(user.get("role") or "").upper() == "ADMIN"
    is_owner = existing.get("user_id") is not None and int(existing["user_id"]) == int(user["id"])
    if not (is_owner or is_admin):
        return jsonify({"status": "error", "code": "FORBIDDEN", "message": "본인이 작성한 리뷰만 수정할 수 있습니다."}), 403

    next_rating = None
    if "rating" in data and data.get("rating") is not None:
        ok_rate, parsed_rate, rate_err = _parse_strict_review_rating(data.get("rating"))
        if not ok_rate or parsed_rate is None:
            return jsonify({"status": "error", "message": rate_err}), 400
        next_rating = parsed_rate

    next_content = None
    if "content" in data:
        c = str(data.get("content") or "").strip()
        if len(c) < 2:
            return jsonify({"status": "error", "message": "리뷰 내용을 2자 이상 입력해 주세요."}), 400
        if len(c) > 1000:
            return jsonify({"status": "error", "message": "리뷰 내용은 1,000자 이내로 입력해 주세요."}), 400
        next_content = c

    next_images = None
    next_image_url = None
    if "images" in data or "image_url" in data:
        ok_imgs, imgs_list, img_err = mall_database.parse_review_images(
            raw_images=data.get("images"),
            raw_image_url=data.get("image_url"),
        )
        if not ok_imgs:
            return jsonify({"status": "error", "message": img_err}), 400
        next_images = imgs_list
        next_image_url = imgs_list[0] if imgs_list else ""

    res = mall_database.update_mall_review(
        rid,
        rating=next_rating,
        content=next_content,
        images=next_images,
        image_url=next_image_url,
    )
    if not res.get("ok"):
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "message": res.get("message") or "리뷰 수정 실패"}), status

    p_num = int(existing["product_num"])
    reviews = mall_database.get_mall_reviews(p_num, viewer=user)
    summary = mall_database.get_mall_review_summary(p_num)
    updated_rev = mall_database.get_mall_review_by_id(rid, viewer=user)
    return jsonify({
        "status": "ok",
        "ok": True,
        "review": updated_rev or res.get("review"),
        "reviews": reviews,
        "summary": summary,
    }), 200


@cs_bp.route("/api/reviews/delete", methods=["POST"])
def api_review_delete():
    user = _resolve_current_user()
    if not user:
        return jsonify({
            "status": "error",
            "code": "AUTH_REQUIRED",
            "message": "리뷰 삭제는 로그인한 회원만 이용할 수 있습니다.",
        }), 401
    if not mall_security.verify_csrf_token(session, request):
        return jsonify({
            "status": "error",
            "code": "CSRF_INVALID",
            "message": "보안 토큰(CSRF) 검증에 실패했습니다.",
        }), 403

    data = _read_request_payload()
    rid = _parse_strict_positive_int(data.get("review_id") if data.get("review_id") is not None else data.get("id"))
    if not rid:
        return jsonify({"status": "error", "message": "올바른 리뷰 ID가 필요합니다."}), 400

    existing = mall_database.get_mall_review_by_id(rid, viewer=user)
    if not existing:
        return jsonify({"status": "error", "message": "삭제할 리뷰를 찾을 수 없습니다."}), 404

    is_admin = str(user.get("role") or "").upper() == "ADMIN"
    is_owner = existing.get("user_id") is not None and int(existing["user_id"]) == int(user["id"])
    if not (is_owner or is_admin):
        return jsonify({"status": "error", "code": "FORBIDDEN", "message": "본인이 작성한 리뷰만 삭제할 수 있습니다."}), 403

    res = mall_database.delete_mall_review(rid)
    if not res.get("ok"):
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "message": res.get("message") or "리뷰 삭제 실패"}), status

    p_num = int(existing["product_num"])
    reviews = mall_database.get_mall_reviews(p_num, viewer=user)
    summary = mall_database.get_mall_review_summary(p_num)
    return jsonify({
        "status": "ok",
        "ok": True,
        "deleted_id": rid,
        "reviews": reviews,
        "summary": summary,
    }), 200


@cs_bp.route("/api/admin/reviews", methods=["GET"])
def api_admin_reviews_list():
    raw_num = request.args.get("product_num")
    p_num = None
    if raw_num is not None and str(raw_num).strip() != "":
        p_num = mall_database.parse_strict_int64(raw_num, min_val=1)
        if p_num is None:
            return jsonify({"status": "error", "ok": False, "message": "유효하지 않은 상품 번호입니다."}), 400
    vis = str(request.args.get("visibility") or request.args.get("status") or "ALL")
    q = str(request.args.get("q") or "")
    items = mall_database.list_admin_reviews(product_num=p_num, visibility=vis, query=q, limit=200)
    return jsonify({"status": "ok", "ok": True, "reviews": items, "items": items, "count": len(items)}), 200


@cs_bp.route("/api/admin/reviews/moderate", methods=["POST"])
def api_admin_reviews_moderate():
    data = _read_request_payload()
    rid = _parse_strict_positive_int(data.get("review_id") if data.get("review_id") is not None else data.get("id"))
    if not rid:
        return jsonify({"status": "error", "message": "올바른 리뷰 ID가 필요합니다."}), 400

    res = mall_database.moderate_mall_review(
        rid,
        action=str(data.get("action") or ""),
        is_hidden=data.get("is_hidden") if "is_hidden" in data else None,
        admin_reply=data.get("admin_reply") if "admin_reply" in data else None,
        is_deleted=data.get("is_deleted") if "is_deleted" in data else None,
    )
    if not res.get("ok"):
        status = 404 if res.get("not_found") else 400
        return jsonify({"status": "error", "message": res.get("message") or "리뷰 관리 처리 실패"}), status
    rev_obj = res.get("review") or {}
    mall_ops.log_current_admin_action(
        action="REVIEW_MODERATE",
        target_type="review",
        target_id=rid,
        detail_dict={
            "review_id": rid,
            "action": str(data.get("action") or ""),
            "is_hidden": rev_obj.get("is_hidden"),
            "is_deleted": bool(res.get("deleted")),
            "has_reply": bool(rev_obj.get("admin_reply")),
        },
    )
    return jsonify({"status": "ok", "ok": True, **res}), 200
