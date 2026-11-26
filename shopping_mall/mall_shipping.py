# -*- coding: utf-8 -*-
"""
쇼핑몰 배송비 및 무료배송 정책 계산 모듈 (mall_shipping.py)
- 단독 배포(shopping_mall/) 경계를 유지하며 c:\\live\\ 상위 모듈을 임포트하지 않습니다.
- 순환 참조 방지를 위해 모듈 최상위에서 mall_database를 임포트하지 않습니다.
"""

import csv
import io
import math
import re
from typing import Any, Optional
from urllib.parse import quote

DEFAULT_SHIPPING_FEE = 4000
DEFAULT_FREE_SHIPPING_THRESHOLD = 0

CARRIER_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "code": "CJ",
        "name": "CJ대한통운",
        "tracking_url_template": "https://trace.cjlogistics.com/next/tracking.html?wblNo={tracking_number}",
        "aliases": ("cj", "cj대한통운", "대한통운", "cjlogistics"),
    },
    {
        "code": "HANJIN",
        "name": "한진택배",
        "tracking_url_template": "https://www.hanjin.com/kor/CMS/DeliveryMgr/WaybillResult.do?mCode=MN038&schLang=KR&wblnumText2={tracking_number}",
        "aliases": ("hanjin", "한진", "한진택배"),
    },
    {
        "code": "LOTTE",
        "name": "롯데택배",
        "tracking_url_template": "https://www.lotteglogis.com/home/reservation/tracking/linkView?InvNo={tracking_number}",
        "aliases": ("lotte", "롯데", "롯데택배"),
    },
    {
        "code": "EPOST",
        "name": "우체국택배",
        "tracking_url_template": "https://service.epost.go.kr/trace.RetrieveDomRigiTraceList.comm?sid1={tracking_number}",
        "aliases": ("epost", "우체국", "우체국택배"),
    },
    {
        "code": "LOGEN",
        "name": "로젠택배",
        "tracking_url_template": "https://www.ilogen.com/web/personal/trace/{tracking_number}",
        "aliases": ("logen", "로젠", "로젠택배"),
    },
    {
        "code": "KDEXP",
        "name": "경동택배",
        "tracking_url_template": "https://kdexp.com/service/delivery/etc/delivery.do?barcode={tracking_number}",
        "aliases": ("kdexp", "경동", "경동택배"),
    },
    {
        "code": "CUPOST",
        "name": "CU 편의점택배",
        "tracking_url_template": "https://www.cupost.co.kr/postbox/delivery/localResult.cupost?invoice_no={tracking_number}",
        "aliases": ("cupost", "cu", "cu택배", "cu편의점택배", "cu 편의점택배"),
    },
    {
        "code": "GSPOST",
        "name": "GS25 택배",
        "tracking_url_template": "https://www.cvsnet.co.kr/invoice/tracking.do?invoice_no={tracking_number}",
        "aliases": ("gspost", "gs", "gs25", "gs25택배", "gs25 택배", "cvsnet"),
    },
    {
        "code": "DIRECT",
        "name": "직접배송/기타",
        "tracking_url_template": "",
        "aliases": ("direct", "직접배송", "직접배송/기타", "기타"),
    },
)

_CARRIER_BY_CODE = {c["code"]: c for c in CARRIER_CATALOG}
_CARRIER_ALIAS_MAP: dict[str, dict[str, Any]] = {}
for _c in CARRIER_CATALOG:
    _CARRIER_ALIAS_MAP[_c["code"].lower()] = _c
    _CARRIER_ALIAS_MAP[_c["name"].lower()] = _c
    _CARRIER_ALIAS_MAP[re.sub(r"\s+", "", _c["name"].lower())] = _c
    for _al in _c.get("aliases", ()):
        _CARRIER_ALIAS_MAP[str(_al).lower()] = _c
        _CARRIER_ALIAS_MAP[re.sub(r"\s+", "", str(_al).lower())] = _c

_TRACKING_TOKEN_RE = re.compile(r"^[A-Za-z0-9\-]{1,64}$")
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n", "\x0b", "\x0c")

SHIPPING_EXPORT_COLUMNS: tuple[str, ...] = (
    "order_code",
    "buyer_name",
    "buyer_phone",
    "zipcode",
    "address",
    "address_detail",
    "delivery_memo",
    "items_summary",
    "carrier_code",
    "tracking_number",
)


def _coerce_non_negative_int(value: Any, default: int = 0, max_val: int = 10_000_000) -> int:
    """음수가 아닌 정수로 안전하게 변환합니다. bool/None/유효하지 않은 문자열은 default를 반환합니다."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(0, min(max_val, value))
    if isinstance(value, float):
        if not math.isfinite(value):
            return default
        return max(0, min(max_val, int(value)))
    cleaned = str(value).replace(",", "").strip()
    if not cleaned:
        return default
    try:
        parsed = int(float(cleaned))
    except (ValueError, TypeError, OverflowError):
        return default
    return max(0, min(max_val, parsed))


def _parse_signed_int(value: Any, default: int = 0) -> int:
    """부호를 유지한 채 정수로 파싱합니다 (subtotal <= 0 경계 판별용)."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return default
        return int(value)
    cleaned = str(value).replace(",", "").strip()
    if not cleaned:
        return default
    try:
        return int(float(cleaned))
    except (ValueError, TypeError, OverflowError):
        return default


def calculate_shipping_fee(subtotal: Any, store_info: Optional[dict] = None) -> int:
    """
    주문 상품 소계(subtotal)와 스토어 설정(store_info)을 기준으로 최종 배송비를 계산합니다.
    - subtotal <= 0 이면 0원 반환
    - free_shipping_threshold > 0 이고 subtotal >= free_shipping_threshold 이면 0원 반환
    - 그 외에는 설정된 기본 배송비(shipping_fee) 반환
    """
    sub = _parse_signed_int(subtotal, default=0)
    if sub <= 0:
        return 0
    if store_info is None:
        import mall_database  # 지연 임포트로 순환 참조 방지

        store_info = mall_database.get_mall_store_info()
    info = store_info if isinstance(store_info, dict) else {}
    fee = _coerce_non_negative_int(info.get("shipping_fee"), DEFAULT_SHIPPING_FEE, max_val=1_000_000)
    threshold = _coerce_non_negative_int(
        info.get("free_shipping_threshold"),
        DEFAULT_FREE_SHIPPING_THRESHOLD,
        max_val=100_000_000,
    )
    if threshold > 0 and sub >= threshold:
        return 0
    return fee


def format_shipping_policy_line(store_info: Optional[dict] = None) -> str:
    """스토어 설정의 기본 배송비 및 무료배송 기준을 반영한 기본 배송 안내 문구를 생성합니다."""
    if store_info is None:
        import mall_database

        store_info = mall_database.get_mall_store_info()
    info = store_info if isinstance(store_info, dict) else {}
    fee = _coerce_non_negative_int(info.get("shipping_fee"), DEFAULT_SHIPPING_FEE, max_val=1_000_000)
    threshold = _coerce_non_negative_int(
        info.get("free_shipping_threshold"),
        DEFAULT_FREE_SHIPPING_THRESHOLD,
        max_val=100_000_000,
    )
    if fee == 0:
        return (
            "• 배송방법: 택배 발송 (오후 2시 이전 주문 당일 출고)\n"
            "• 배송비: 전 상품 무료배송 (도서/산간 지역 추가 비용 발생 가능)"
        )
    if threshold > 0:
        return (
            "• 배송방법: 택배 발송 (오후 2시 이전 주문 당일 출고)\n"
            f"• 배송비: 기본 {fee:,}원 ({threshold:,}원 이상 무료배송, 도서/산간 지역 추가 비용 발생 가능)"
        )
    return (
        "• 배송방법: 택배 발송 (오후 2시 이전 주문 당일 출고)\n"
        f"• 배송비: 기본 {fee:,}원 (도서/산간 지역 추가 비용 발생 가능)"
    )


def get_carrier_catalog() -> list[dict[str, Any]]:
    """프론트엔드 및 어드민 UI에 노출할 택배사 목록을 반환합니다."""
    return [
        {
            "code": c["code"],
            "name": c["name"],
            "supports_tracking": bool(c.get("tracking_url_template")),
        }
        for c in CARRIER_CATALOG
    ]


def resolve_carrier(carrier_code: Any = "", carrier_name: Any = "") -> tuple[str, str]:
    """
    택배사 코드 또는 이름/별칭을 표준 (carrier_code, carrier_name) 튜플로 정규화합니다.
    - carrier_code가 주어지면 해당 코드/별칭을 표준 카탈로그에서 조회합니다.
    - carrier_code가 비어 있고 carrier_name이 주어지면 이름/별칭을 표준 카탈로그에서 조회합니다.
    - DIRECT 코드 또는 명시적 DIRECT 별칭(직접배송/기타 등)이 주어진 경우 ('DIRECT', custom_or_default_name) 반환
    - 알 수 없는 코드나 알 수 없는 택배사명은 ('', '') 반환
    """
    raw_code = str(carrier_code or "").strip()
    raw_name = str(carrier_name or "").strip()

    candidate = raw_code if raw_code else raw_name
    if not candidate:
        return "", ""

    norm_key = candidate.lower()
    compact_key = re.sub(r"\s+", "", norm_key)
    matched = _CARRIER_ALIAS_MAP.get(norm_key) or _CARRIER_ALIAS_MAP.get(compact_key)
    if not matched:
        return "", ""

    if matched["code"] == "DIRECT" and raw_name:
        compact_name = re.sub(r"\s+", "", raw_name.lower())
        if compact_name not in ("direct", "직접배송", "직접배송/기타", "기타"):
            return "DIRECT", raw_name[:60]
    return str(matched["code"]), str(matched["name"])


def sanitize_tracking_number(raw_tracking: Any) -> str:
    """운송장 번호에서 공백을 제거하고 안전한 영숫자/하이픈 토큰([A-Za-z0-9\\-]{1,64})만 허용합니다."""
    cleaned = re.sub(r"\s+", "", str(raw_tracking or "").strip())
    if not cleaned:
        return ""
    if not _TRACKING_TOKEN_RE.fullmatch(cleaned):
        return ""
    return cleaned


def get_carrier_tracking_url(carrier: Any, tracking_number: Any) -> str:
    """
    택배사와 운송장 번호로 안전한 원클릭 HTTPS 배송조회 URL을 생성합니다.
    - DIRECT 또는 지원하지 않는 택배사, 유효하지 않은 운송장 번호는 빈 문자열 반환
    """
    if isinstance(carrier, dict):
        c_code = carrier.get("carrier_code") or carrier.get("code") or ""
        c_name = carrier.get("carrier_name") or carrier.get("name") or ""
    else:
        c_code = str(carrier or "").strip()
        c_name = c_code
    resolved_code, _ = resolve_carrier(c_code, c_name)
    if not resolved_code:
        return ""
    spec = _CARRIER_BY_CODE.get(resolved_code)
    if not spec:
        return ""
    tpl = str(spec.get("tracking_url_template") or "").strip()
    if not tpl:
        return ""
    clean_tracking = sanitize_tracking_number(tracking_number)
    if not clean_tracking:
        return ""
    url = tpl.format(tracking_number=quote(clean_tracking, safe="-"))
    if not url.startswith("https://"):
        return ""
    return url


def _sanitize_csv_cell(val: Any) -> str:
    """엑셀/스프레드시트 수식 인젝션(=, +, -, @, TAB, CR, LF, BOM 및 선행 공백 후 수식 기호)을 무력화합니다."""
    if val is None:
        return ""
    s = str(val)
    if not s:
        return s
    stripped = s.lstrip(" \t\r\n\v\f\ufeff\u00a0\u3000").lstrip()
    if s[0] in _CSV_FORMULA_PREFIXES or stripped[:1] in ("=", "+", "-", "@"):
        return "'" + s
    return s


def _format_items_summary(order: dict) -> str:
    items = order.get("order_items")
    if items is None:
        items = order.get("items")
    if not isinstance(items, list):
        return ""
    parts: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        pnum = it.get("product_num") or it.get("num") or ""
        pname = str(it.get("product_name") or it.get("name") or "").strip()
        color = str(it.get("color") or "-").strip() or "-"
        size = str(it.get("size") or "-").strip() or "-"
        qty = it.get("quantity") or 1
        prefix = f"#{pnum} " if pnum != "" else ""
        parts.append(f"{prefix}{pname} ({color}/{size}) x{qty}")
    return " | ".join(parts)


def build_shipping_export_csv(orders: list[dict]) -> bytes:
    """택배사 발송용 주문서 CSV(UTF-8 BOM, 10개 표준 컬럼, 수식 인젝션 방어)를 생성합니다."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(list(SHIPPING_EXPORT_COLUMNS))
    for ord_row in (orders or []):
        if not isinstance(ord_row, dict):
            continue
        row_vals = [
            _sanitize_csv_cell(ord_row.get("order_code") or ""),
            _sanitize_csv_cell(ord_row.get("buyer_name") or ""),
            _sanitize_csv_cell(ord_row.get("buyer_phone") or ""),
            _sanitize_csv_cell(ord_row.get("zipcode") or ""),
            _sanitize_csv_cell(ord_row.get("address") or ""),
            _sanitize_csv_cell(ord_row.get("address_detail") or ""),
            _sanitize_csv_cell(ord_row.get("delivery_memo") or ""),
            _sanitize_csv_cell(_format_items_summary(ord_row)),
            _sanitize_csv_cell(ord_row.get("carrier_code") or ""),
            _sanitize_csv_cell(ord_row.get("tracking_number") or ""),
        ]
        writer.writerow(row_vals)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


_IMPORT_HEADER_ALIASES: dict[str, str] = {
    "order_code": "order_code",
    "주문번호": "order_code",
    "주문코드": "order_code",
    "carrier_code": "carrier_code",
    "택배사코드": "carrier_code",
    "carrier_name": "carrier_name",
    "택배사": "carrier_name",
    "택배사명": "carrier_name",
    "carrier": "carrier_name",
    "tracking_number": "tracking_number",
    "운송장번호": "tracking_number",
    "송장번호": "tracking_number",
    "운송장": "tracking_number",
    "mark_shipped": "mark_shipped",
    "배송중전환": "mark_shipped",
    "출고처리": "mark_shipped",
}


def _decode_csv_bytes(raw_data: bytes | bytearray | str) -> Optional[str]:
    if isinstance(raw_data, str):
        return raw_data.lstrip("\ufeff")
    if not isinstance(raw_data, (bytes, bytearray)):
        return None
    raw_bytes = bytes(raw_data)
    for enc in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            return raw_bytes.decode(enc).lstrip("\ufeff")
        except UnicodeDecodeError:
            continue
    return None


def parse_tracking_import_csv(raw_data: bytes | bytearray | str) -> dict[str, Any]:
    """
    송장 일괄 업로드용 CSV(UTF-8 BOM / CP949 / EUC-KR, 영문·한글 헤더 지원)를 파싱합니다.
    """
    text = _decode_csv_bytes(raw_data)
    if text is None:
        return {
            "ok": False,
            "message": "CSV 파일 인코딩을 해석할 수 없습니다. UTF-8 또는 CP949(Excel CSV)로 저장해 주세요.",
        }
    if not text.strip():
        return {"ok": False, "message": "CSV 파일이 비어 있습니다."}

    reader = csv.reader(io.StringIO(text))
    header_row = None
    header_line = 0
    for idx, row in enumerate(reader, start=1):
        if any(str(c or "").strip() for c in row):
            header_row = row
            header_line = idx
            break
    if not header_row:
        return {"ok": False, "message": "CSV 헤더 행을 찾을 수 없습니다."}

    col_map: dict[int, str] = {}
    found_fields: set[str] = set()
    for col_idx, raw_h in enumerate(header_row):
        norm_h = re.sub(r"\s+", "", str(raw_h or "").lstrip("\ufeff").strip().lower())
        canonical = _IMPORT_HEADER_ALIASES.get(norm_h)
        if canonical and canonical not in found_fields:
            col_map[col_idx] = canonical
            found_fields.add(canonical)

    if "order_code" not in found_fields or "tracking_number" not in found_fields:
        return {
            "ok": False,
            "message": "CSV 헤더에 필수 컬럼(order_code/주문번호, tracking_number/운송장번호)이 포함되어야 합니다.",
        }

    rows: list[dict[str, Any]] = []
    for line_no, raw_row in enumerate(reader, start=header_line + 1):
        if not any(str(c or "").strip() for c in raw_row):
            continue
        item: dict[str, Any] = {
            "row_num": line_no,
            "order_code": "",
            "carrier_code": "",
            "carrier_name": "",
            "tracking_number": "",
            "mark_shipped": None,
        }
        for col_idx, field_name in col_map.items():
            val = str(raw_row[col_idx] if col_idx < len(raw_row) else "").strip()
            if field_name == "mark_shipped":
                if val == "":
                    item["mark_shipped"] = None
                else:
                    item["mark_shipped"] = val.lower() in ("1", "true", "y", "yes", "예", "배송중", "출고")
            else:
                item[field_name] = val
        rows.append(item)

    if not rows:
        return {"ok": False, "message": "업로드할 송장 데이터 행이 없습니다."}

    return {"ok": True, "rows": rows, "errors": []}
