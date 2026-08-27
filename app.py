"""
Invoice Extractor v3 - Accounting Tool
FastAPI Web Server with Lark Integration + Dynamic Settings
"""
import json
import os
import re
import uuid
import asyncio
import traceback
from pathlib import Path
from typing import Dict, Any, List, Optional
import sys
from urllib.parse import urlparse, parse_qs

# Fix Unicode print errors on Windows
if sys.platform.startswith('win'):
    sys.stdout.reconfigure(encoding='utf-8')
from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from extractors.file_handler import FileHandler
from extractors.ai_extractor import AIExtractor
from extractors.lark_client import LarkClient
from processors.vat_calculator import VATCalculator
from exporters.excel_export import export_to_excel

# --- Config ---
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

DEFAULT_RESULT_FIELDS = {
    # Hóa đơn
    "co_vat": "AI_Có_VAT",
    "tien_truoc_thue": "AI_Tiền_Trước_Thuế",
    "tien_vat": "AI_Tiền_VAT",
    "tien_sau_thue": "AI_Tiền_Sau_Thuế",
    "trang_thai": "AI_Trạng_Thái",
    "ngay_hoa_don": "AI_Ngày_HĐ",
    "so_hoa_don": "AI_Số_HĐ",
    "mst_a": "AI_MST_A",
    "mst_b": "AI_MST_B",
    # Hợp đồng / BBNT / Phụ lục hợp đồng
    "so_hop_dong": "AI_Số_Hợp_đồng",
    "so_lan_tt": "AI_Số_lần_TT",
    "gia_tri_tt": "AI_Giá_trị_TT",
    "tong_gia_tri_net": "AI_Tổng_giá_trị_net",
    "tong_gia_tri_gross": "AI_Tổng_giá_trị_gross",
    "vat_chung_tu": "AI_VAT_chứng_từ",
    "pit_chung_tu": "AI_PIT_chứng_từ",
    "so_bbnt": "AI_Số_BBNT",
    "so_plhd": "AI_Số_PLHĐ",
}

# Lark field type constants
LARK_FIELD_NUMBER = 2


def _coerce_number(val):
    """Chuyển 1 giá trị (str/số) về số (int/float) để ghi vào field kiểu Number của Lark.
    Trả về None nếu không parse được (khi đó bỏ qua field, tránh làm hỏng cả update)."""
    if val is None:
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return val
    s = str(val).strip()
    if not s:
        return None
    # Chỉ giữ số, dấu phân cách và dấu âm
    s = re.sub(r"[^0-9.,\-]", "", s)
    if not s or s in ("-", ".", ","):
        return None
    if "," in s and "." in s:
        # Nếu dấu phẩy đứng sau -> phẩy là thập phân (định dạng VN): bỏ chấm, đổi phẩy thành chấm
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:  # định dạng EN: phẩy là ngăn cách nghìn
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) != 3:
            s = s.replace(",", ".")  # phẩy thập phân
        else:
            s = s.replace(",", "")  # phẩy ngăn cách nghìn
    if s.count(".") > 1:  # nhiều dấu chấm -> ngăn cách nghìn
        s = s.replace(".", "")
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except (ValueError, TypeError):
        return None


def _format_money_str(val):
    """Định dạng (các) giá trị tiền thành SỐ NGUYÊN dạng chuỗi (không phần thập phân).
    Hỗ trợ nhiều hóa đơn: mỗi giá trị ngăn cách bằng ',' (ví dụ '1837032,2500000').
    - Số 1837032.0 -> '1837032'
    - Chuỗi '10000000,20000000' -> '10000000,20000000'
    Trả về None nếu rỗng/không có giá trị."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return str(int(round(val)))
    s = str(val).strip()
    if not s:
        return None
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if not parts:
        return None
    out = []
    for p in parts:
        num = _coerce_number(p)
        out.append(str(int(round(num))) if num is not None else p)
    return ",".join(out)


def _field(result_fields_config: dict, key: str, default: str) -> str:
    """Lấy tên field Lark theo key cấu hình, fallback về mặc định."""
    return result_fields_config.get(key, default)


def _pick_money(raw_val, computed_val):
    """Chọn giá trị tiền để ghi.
    - Nếu raw là chuỗi nhiều hóa đơn (có dấu ','), dùng raw (computed đã bị gộp sai với nhiều HĐ).
    - Ngược lại ưu tiên giá trị computed (đã validate/ tính ngược), fallback về raw."""
    if raw_val is not None and "," in str(raw_val):
        return raw_val
    if computed_val is not None:
        return computed_val
    return raw_val


def _sanitize_fields_for_lark(update_fields: dict, field_type_map: dict) -> tuple:
    """Ép kiểu các giá trị theo kiểu field thực tế của Lark.
    - Field kiểu Number (type 2): chuyển giá trị về số; nếu không parse được thì bỏ field đó.
    Trả về (clean_fields, dropped) với dropped là danh sách field bị bỏ do không ép kiểu được."""
    clean = {}
    dropped = []
    for name, value in update_fields.items():
        ftype = field_type_map.get(name)
        if ftype == LARK_FIELD_NUMBER:
            num = _coerce_number(value)
            if num is None:
                dropped.append(name)
                continue
            clean[name] = num
        else:
            clean[name] = value
    return clean, dropped


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=4)


config = load_config()


def make_lark_client() -> LarkClient:
    cfg = load_config()
    return LarkClient(
        app_id=cfg.get("lark_app_id", ""),
        app_secret=cfg.get("lark_app_secret", ""),
        base_url=cfg.get("lark_base_url", "https://open.larksuite.com"),
    )


# --- App ---
app = FastAPI(title="Invoice Extractor", version="3.0.0")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# In-memory task storage
tasks: Dict[str, Dict[str, Any]] = {}

# Uploaded files kept for later push to Lark (file_id -> saved path)
uploaded_files: Dict[str, str] = {}


# ============================================================
# Helpers
# ============================================================

def _extract_text_field(fields: dict, field_name: str) -> str:
    """Extract text value from a Lark Bitable field, handling various types."""
    val = fields.get(field_name)
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, list):
        parts = []
        for v in val:
            if isinstance(v, dict):
                parts.append(v.get("text", str(v)))
            else:
                parts.append(str(v))
        return " ".join(parts)
    return str(val)


def parse_lark_url(url: str) -> dict:
    """
    Parse a Lark Bitable URL and extract app_token, table_id, view_id.
    Supports multiple URL formats:
      - https://xxx.larksuite.com/base/APP_TOKEN?table=TABLE_ID&view=VIEW_ID
      - https://xxx.feishu.cn/base/APP_TOKEN?table=TABLE_ID&view=VIEW_ID
      - https://xxx.larksuite.com/wiki/WIKI_TOKEN (wiki embed)
    """
    result = {"app_token": "", "table_id": "", "view_id": "", "base_url": ""}

    try:
        parsed = urlparse(url)
        host = parsed.netloc  # e.g. abc.larksuite.com

        # Determine base_url
        if "feishu" in host:
            result["base_url"] = "https://open.feishu.cn"
        else:
            result["base_url"] = "https://open.larksuite.com"

        # Get path segments
        path_parts = [p for p in parsed.path.split("/") if p]
        # Typical: /base/APP_TOKEN or /wiki/WIKI_TOKEN
        if len(path_parts) >= 2:
            result["app_token"] = path_parts[-1]

        # Query params
        qs = parse_qs(parsed.query)
        if "table" in qs:
            result["table_id"] = qs["table"][0]
        if "view" in qs:
            result["view_id"] = qs["view"][0]

    except Exception:
        pass

    return result


# ============================================================
# Static
# ============================================================

@app.get("/")
async def index():
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


# ============================================================
# Settings Endpoints
# ============================================================

@app.get("/api/settings")
async def get_settings():
    """Get current configuration (without secrets)."""
    cfg = load_config()
    return {
        "lark_app_id": cfg.get("lark_app_id", ""),
        "lark_app_secret": cfg.get("lark_app_secret", ""),
        "lark_base_url": cfg.get("lark_base_url", "https://open.larksuite.com"),
        "openai_api_key": cfg.get("openai_api_key", ""),
        "openai_model": cfg.get("openai_model", "gpt-4o"),
        "openai_base_url": cfg.get("openai_base_url", ""),
        # Current table config
        "lark_app_token": cfg.get("lark_app_token", ""),
        "lark_table_id": cfg.get("lark_table_id", ""),
        "lark_view_id": cfg.get("lark_view_id", ""),
        "lark_attachment_field": cfg.get("lark_attachment_field", ""),
        "lark_content_field": cfg.get("lark_content_field", ""),
        # New: attachment fields for contract document types
        "lark_att_field_hop_dong": cfg.get("lark_att_field_hop_dong", ""),
        "lark_att_field_bbnt": cfg.get("lark_att_field_bbnt", ""),
        "lark_att_field_phu_luc": cfg.get("lark_att_field_phu_luc", ""),
        "lark_doc_type_field": cfg.get("lark_doc_type_field", ""),
        "lark_result_fields": {**DEFAULT_RESULT_FIELDS, **cfg.get("lark_result_fields", {})},
        "lark_table_url": cfg.get("lark_table_url", ""),
        "lark_upload_url": cfg.get("lark_upload_url", "https://o4pvcegwn6b.sg.larksuite.com/base/Eaiabgix8a7mwqs63molRe6UgYf?table=tblYjTMstZfy1Ua6&view=vewViuZElT"),
        "lark_filters": cfg.get("lark_filters", []),
    }


@app.post("/api/settings")
async def save_settings(data: dict):
    """Save configuration."""
    cfg = load_config()
    # Allowed keys to update
    allowed = [
        "lark_app_id", "lark_app_secret", "lark_base_url",
        "openai_api_key", "openai_model", "openai_base_url",
        "lark_app_token", "lark_table_id", "lark_view_id",
        "lark_attachment_field", "lark_content_field",
        "lark_att_field_hop_dong", "lark_att_field_bbnt",
        "lark_att_field_phu_luc", "lark_doc_type_field",
        "lark_result_fields", "lark_table_url", "lark_upload_url", "lark_filters",
    ]
    for key in allowed:
        if key in data:
            cfg[key] = data[key]
    save_config(cfg)
    return {"ok": True}


# ============================================================
# Lark Utility Endpoints
# ============================================================

@app.post("/api/lark/resolve")
async def resolve_lark_url(data: dict):
    """
    Resolve a Lark Bitable URL to app_token + table_id + view_id.
    Also updates base_url in config if different.
    """
    url = data.get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    parsed = parse_lark_url(url)
    if not parsed["app_token"]:
        raise HTTPException(status_code=400, detail="Cannot parse app_token from URL")

    # Try to resolve wiki token to bitable app_token
    cfg = load_config()
    lark = make_lark_client()
    if parsed["base_url"] != cfg.get("lark_base_url"):
        # Update base_url to match the URL
        cfg["lark_base_url"] = parsed["base_url"]
        lark.base_url = parsed["base_url"]

    try:
        app_token = lark.get_bitable_app_token(parsed["app_token"])
        parsed["app_token"] = app_token
    except Exception:
        pass  # Use original token

    return parsed


@app.get("/api/lark/fields")
async def get_lark_fields(app_token: str, table_id: str):
    """Get list of fields (columns) for a Bitable table."""
    try:
        lark = make_lark_client()
        fields = lark.list_fields(app_token, table_id)
        return {"fields": [{"id": f["field_id"], "name": f["field_name"], "type": f["type"]} for f in fields]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot fetch fields: {e}")


@app.post("/api/lark/setup-columns")
async def setup_result_columns(data: dict):
    """
    Auto-create the result columns if they don't already exist.
    Returns the final field map.
    """
    app_token = data.get("app_token", "")
    table_id = data.get("table_id", "")
    result_fields = data.get("result_fields", DEFAULT_RESULT_FIELDS)

    if not app_token or not table_id:
        raise HTTPException(status_code=400, detail="app_token and table_id required")

    try:
        lark = make_lark_client()
        existing = lark.list_fields(app_token, table_id)
        existing_names = {f["field_name"]: f for f in existing}

        # field_type: 1=Text, 2=Number
        # Hóa đơn fields
        field_types = {
            result_fields.get("co_vat", "AI_Có_VAT"): 1,
            result_fields.get("tien_truoc_thue", "AI_Tiền_Trước_Thuế"): 1,  # Text to support multiple values (e.g., 1000,2000)
            result_fields.get("tien_vat", "AI_Tiền_VAT"): 1,  # Text to support multiple values
            result_fields.get("trang_thai", "AI_Trạng_Thái"): 1,
            result_fields.get("ngay_hoa_don", "AI_Ngày_HĐ"): 1,
            result_fields.get("so_hoa_don", "AI_Số_HĐ"): 1,
            result_fields.get("mst_a", "AI_MST_A"): 1,
            result_fields.get("mst_b", "AI_MST_B"): 1,
            # Hợp đồng / BBNT / Phụ lục fields
            result_fields.get("so_hop_dong", "AI_Số_Hợp_đồng"): 1,
            result_fields.get("so_lan_tt", "AI_Số_lần_TT"): 2,
            result_fields.get("gia_tri_tt", "AI_Giá_trị_TT"): 1,
            result_fields.get("tong_gia_tri_net", "AI_Tổng_giá_trị_net"): 2,
            result_fields.get("tong_gia_tri_gross", "AI_Tổng_giá_trị_gross"): 2,
            result_fields.get("vat_chung_tu", "AI_VAT_chứng_từ"): 1,
            result_fields.get("pit_chung_tu", "AI_PIT_chứng_từ"): 1,
            result_fields.get("so_bbnt", "AI_Số_BBNT"): 1,
            result_fields.get("so_plhd", "AI_Số_PLHĐ"): 1,
        }

        created = []
        already_existed = []
        for field_name, field_type in field_types.items():
            if not field_name:
                continue
            if field_name in existing_names:
                already_existed.append(field_name)
            else:
                lark.create_field(app_token, table_id, field_name, field_type)
                created.append(field_name)

        return {"created": created, "already_existed": already_existed}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot setup columns: {e}")


# ============================================================
# Records Endpoint
# ============================================================

@app.get("/api/lark/records")
async def get_lark_records(
    page_token: str = None,
    page_size: int = 50,
    app_token: str = None,
    table_id: str = None,
    view_id: str = None,
):
    """Lấy danh sách records từ Lark Bitable."""
    try:
        cfg = load_config()
        lark = make_lark_client()

        _app_token = app_token or cfg.get("lark_app_token", "")
        _table_id = table_id or cfg.get("lark_table_id", "")
        _view_id = view_id or cfg.get("lark_view_id", "")
        attachment_field = cfg.get("lark_attachment_field", "Tệp đính kèm")
        content_field = cfg.get("lark_content_field", "Text")
        result_fields = cfg.get("lark_result_fields", DEFAULT_RESULT_FIELDS)
        doc_type_field = cfg.get("lark_doc_type_field", "")

        records, next_token, total = lark.list_records(
            app_token=_app_token,
            table_id=_table_id,
            view_id=_view_id,
            page_size=page_size,
            page_token=page_token,
        )

        items = []
        for rec in records:
            fields = rec.get("fields", {})
            record_id = rec.get("record_id", "")

            # Attachments (invoice)
            attachments = fields.get(attachment_field, [])
            att_names = []
            if isinstance(attachments, list):
                att_names = [a.get("name", "file") for a in attachments if isinstance(a, dict)]

            # Also check other document type fields for attachments
            att_field_hop_dong = cfg.get("lark_att_field_hop_dong", "")
            att_field_bbnt = cfg.get("lark_att_field_bbnt", "")
            att_field_phu_luc = cfg.get("lark_att_field_phu_luc", "")

            for extra_field in [att_field_hop_dong, att_field_bbnt, att_field_phu_luc]:
                if extra_field and extra_field in fields:
                    extra_atts = fields.get(extra_field, [])
                    if isinstance(extra_atts, list):
                        extra_names = [a.get("name", "file") for a in extra_atts if isinstance(a, dict)]
                        att_names.extend(extra_names)

            # Result fields (if already processed)
            co_vat = _extract_text_field(fields, result_fields.get("co_vat", "AI_Có_VAT"))
            tien_truoc_thue = fields.get(result_fields.get("tien_truoc_thue", "AI_Tiền_Trước_Thuế"))
            tien_vat = fields.get(result_fields.get("tien_vat", "AI_Tiền_VAT"))
            trang_thai = _extract_text_field(fields, result_fields.get("trang_thai", "AI_Trạng_Thái"))

            # Content field
            noi_dung = _extract_text_field(fields, content_field)

            # Doc type field value (e.g. "Hóa đơn", "Hợp đồng", "BBNT", "Phụ lục hợp đồng")
            loai_chung_tu = ""
            if doc_type_field:
                loai_chung_tu = _extract_text_field(fields, doc_type_field)

            # Amount field (smart detect)
            so_tien = None
            for key in ["Số tiền ~ Số tiền", "Số tiền", "Số TIỀN", "Amount"]:
                if key in fields:
                    so_tien = fields[key]
                    break
            if so_tien is None:
                for key, val in fields.items():
                    if "tiền" in key.lower() and isinstance(val, (int, float)):
                        so_tien = val
                        break

            items.append({
                "record_id": record_id,
                "noi_dung": noi_dung[:100] if noi_dung else "",
                "attachments": att_names,
                "has_attachments": len(att_names) > 0,
                "so_tien": so_tien,
                "co_vat": co_vat,
                "tien_truoc_thue": tien_truoc_thue,
                "tien_vat": tien_vat,
                "trang_thai": trang_thai,
                "loai_chung_tu": loai_chung_tu,
                "is_processed": bool(trang_thai),
            })

        return {
            "items": items,
            "next_page_token": next_token,
            "total": total,
        }

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Lỗi kết nối Lark: {str(e)}")


# ============================================================
# Process Endpoint
# ============================================================

@app.post("/api/lark/process")
async def process_lark_records(data: dict, background_tasks: BackgroundTasks):
    """Bắt đầu xử lý batch records từ Lark."""
    record_ids = data.get("record_ids", [])
    if not record_ids:
        raise HTTPException(status_code=400, detail="Chưa chọn record nào")

    task_id = str(uuid.uuid4())
    tasks[task_id] = {
        "status": "processing",
        "total": len(record_ids),
        "processed": 0,
        "current_record": "",
        "results": [],
        "errors": [],
    }

    background_tasks.add_task(process_lark_batch, task_id, record_ids)
    return {"task_id": task_id, "total_records": len(record_ids)}


async def process_lark_batch(task_id: str, record_ids: List[str]):
    """Background task: xử lý batch records."""
    print(f"+++ [START] process_lark_batch task_id={task_id} +++", flush=True)

    cfg = load_config()
    lark = make_lark_client()

    app_token = cfg.get("lark_app_token", "")
    table_id = cfg.get("lark_table_id", "")
    attachment_field = cfg.get("lark_attachment_field", "Tệp đính kèm")
    att_field_hop_dong = cfg.get("lark_att_field_hop_dong", "")
    att_field_bbnt = cfg.get("lark_att_field_bbnt", "")
    att_field_phu_luc = cfg.get("lark_att_field_phu_luc", "")
    doc_type_field = cfg.get("lark_doc_type_field", "")
    result_fields_config = {**DEFAULT_RESULT_FIELDS, **cfg.get("lark_result_fields", {})}
    content_field = cfg.get("lark_content_field", "Text")

    api_key = cfg.get("openai_api_key", "")
    model = cfg.get("openai_model", "gpt-4o")
    base_url = cfg.get("openai_base_url", "")
    extractor = AIExtractor(api_key=api_key, model=model, base_url=base_url)
    calculator = VATCalculator()
    loop = asyncio.get_event_loop()
    print(f"  -> Init done. app_token={app_token} table_id={table_id}", flush=True)

    # Fetch all field IDs (required for Bitable file download)
    att_field_id_map: Dict[str, Optional[str]] = {}
    field_type_map: Dict[str, Optional[int]] = {}
    print(f"  -> Fetching fields...", flush=True)
    try:
        fields_list = await loop.run_in_executor(None, lark.list_fields, app_token, table_id)
        for f in fields_list:
            att_field_id_map[f["field_name"]] = f["field_id"]
            field_type_map[f["field_name"]] = f.get("type")
        print(f"  -> Fetched {len(fields_list)} fields.", flush=True)
    except Exception as e:
        print(f"  -> Warning: Could not fetch field_id: {e}", flush=True)

    print(f"  -> Starting loop for {len(record_ids)} record(s)...", flush=True)

    for i, record_id in enumerate(record_ids):
        print(f"\n[{i+1}/{len(record_ids)}] Fetching record {record_id}...", flush=True)
        try:
            rec = await loop.run_in_executor(None, lark.get_record, app_token, table_id, record_id)
        except Exception as e:
            print(f"  -> Cannot fetch record: {e}", flush=True)
            tasks[task_id]["results"].append({
                "record_id": record_id,
                "trang_thai": "LỖI",
                "error": f"Cannot fetch record: {e}",
            })
            tasks[task_id]["processed"] = i + 1
            continue

        fields = rec.get("fields", {})
        noi_dung = _extract_text_field(fields, content_field)
        tasks[task_id]["current_record"] = noi_dung[:50] or record_id

        # Determine doc type from the designated column
        loai_chung_tu = ""
        if doc_type_field:
            loai_chung_tu = _extract_text_field(fields, doc_type_field).strip()
        print(f"  -> Loại chứng từ: '{loai_chung_tu}'", flush=True)

        # Map doc type to attachment field name and display name
        CONTRACT_DOC_TYPES = {
            "Hợp đồng": (att_field_hop_dong, "Hợp đồng"),
            "BBNT": (att_field_bbnt, "BBNT"),
            "Phụ lục hợp đồng": (att_field_phu_luc, "Phụ lục hợp đồng"),
        }

        update_fields = {}
        result_entry = {
            "record_id": record_id,
            "noi_dung": noi_dung,
        }

        # ---- Process Invoice (Hóa đơn) - always if has attachment ----
        invoice_attachments = fields.get(attachment_field, [])
        should_process_invoice = isinstance(invoice_attachments, list) and len(invoice_attachments) > 0

        if should_process_invoice:
            print(f"  -> Processing Hóa đơn attachment(s)...", flush=True)
            try:
                all_content_blocks = []
                att_filenames = []
                att_field_id = att_field_id_map.get(attachment_field)

                for att in invoice_attachments:
                    if not isinstance(att, dict): continue
                    file_token = att.get("file_token", "")
                    att_name = att.get("name", "file")
                    att_filenames.append(att_name)
                    if not file_token: continue

                    print(f"  -> Downloading invoice: {att_name} ({file_token})", flush=True)
                    tmp_path = await loop.run_in_executor(
                        None, lark.save_attachment_temp,
                        file_token, att_name, app_token, table_id, att_field_id, record_id
                    )
                    try:
                        content_blocks = FileHandler.process_file(tmp_path)
                        all_content_blocks.extend(content_blocks)
                    finally:
                        try: os.unlink(tmp_path)
                        except: pass

                if all_content_blocks:
                    raw_result = await loop.run_in_executor(
                        None, extractor.extract_invoice, all_content_blocks, ", ".join(att_filenames)
                    )
                    processed = calculator.process(raw_result)
                    co_vat = processed.get("co_vat", False)

                    f_truoc = _field(result_fields_config, "tien_truoc_thue", "AI_Tiền_Trước_Thuế")
                    f_vat = _field(result_fields_config, "tien_vat", "AI_Tiền_VAT")
                    f_sau = result_fields_config.get("tien_sau_thue")

                    if co_vat:
                        update_fields[result_fields_config.get("co_vat", "AI_Có_VAT")] = "Có"
                        # Tiền trước thuế / VAT / sau thuế -> số nguyên, nhiều HĐ ngăn cách ','
                        v_truoc = _format_money_str(_pick_money(
                            raw_result.get("tong_tien_truoc_thue"), processed.get("tong_tien_truoc_thue_computed")))
                        v_vat = _format_money_str(_pick_money(
                            raw_result.get("tien_vat"), processed.get("tien_vat_computed")))
                        v_sau = _format_money_str(_pick_money(
                            raw_result.get("tong_thanh_toan"), processed.get("tong_thanh_toan_computed")))
                        if v_truoc is not None:
                            update_fields[f_truoc] = v_truoc
                        if v_vat is not None:
                            update_fields[f_vat] = v_vat
                        if v_sau is not None and f_sau:
                            update_fields[f_sau] = v_sau
                    else:
                        update_fields[result_fields_config.get("co_vat", "AI_Có_VAT")] = "Không"
                        # Không VAT: trước thuế = sau thuế = tổng thanh toán
                        v_tong = _format_money_str(_pick_money(
                            raw_result.get("tong_thanh_toan"), processed.get("tong_thanh_toan_computed")))
                        if v_tong is not None:
                            update_fields[f_truoc] = v_tong
                            if f_sau:
                                update_fields[f_sau] = v_tong

                    status_text = processed.get("trang_thai", "OK")
                    notes = []
                    if processed.get("canh_bao"): notes.append(processed["canh_bao"])
                    if processed.get("ghi_chu"): notes.append(processed["ghi_chu"])
                    if notes: status_text += f" | {'; '.join(notes)}"
                    update_fields[result_fields_config.get("trang_thai", "AI_Trạng_Thái")] = status_text[:500]

                    ngay_hoa_don = raw_result.get("ngay_hoa_don")
                    so_hoa_don = raw_result.get("so_hoa_don")
                    mst_a = raw_result.get("ma_so_thue_nguoi_ban") or raw_result.get("ma_so_thue")
                    mst_b = raw_result.get("ma_so_thue_nguoi_mua")
                    if ngay_hoa_don and result_fields_config.get("ngay_hoa_don"):
                        update_fields[result_fields_config["ngay_hoa_don"]] = str(ngay_hoa_don)
                    if so_hoa_don and result_fields_config.get("so_hoa_don"):
                        update_fields[result_fields_config["so_hoa_don"]] = str(so_hoa_don)
                    if mst_a and result_fields_config.get("mst_a"):
                        update_fields[result_fields_config["mst_a"]] = str(mst_a)
                    if mst_b and result_fields_config.get("mst_b"):
                        update_fields[result_fields_config["mst_b"]] = str(mst_b)

                    result_entry.update({
                        "co_vat": co_vat,
                        "so_hoa_don": raw_result.get("so_hoa_don"),
                        "ngay_hoa_don": raw_result.get("ngay_hoa_don"),
                        "mst_a": mst_a,
                        "mst_b": mst_b,
                        "tien_truoc_thue": processed.get("tong_tien_truoc_thue_computed"),
                        "tien_vat": processed.get("tien_vat_computed"),
                        "tong_thanh_toan": processed.get("tong_thanh_toan_computed"),
                    })
                    print(f"  -> Invoice processed OK.", flush=True)

            except Exception as e:
                traceback.print_exc()
                print(f"  -> Invoice ERROR: {str(e)}", flush=True)
                update_fields[result_fields_config.get("trang_thai", "AI_Trạng_Thái")] = f"LỖI HĐ: {str(e)[:200]}"
                result_entry["error"] = str(e)

        elif loai_chung_tu == "Hóa đơn" and not (isinstance(invoice_attachments, list) and len(invoice_attachments) > 0):
            # Marked as Hóa đơn but no file
            update_fields[result_fields_config.get("trang_thai", "AI_Trạng_Thái")] = "Bỏ qua - không có file hóa đơn"

        # ---- Xử lý chứng từ Hợp đồng / BBNT / Phụ lục (2 BƯỚC) ----
        lct_lower = loai_chung_tu.lower()
        tt_field = result_fields_config.get("trang_thai", "AI_Trạng_Thái")
        contract_specs = [
            # (c_key, attachment_field, doc_type, so_key, is_amount_source)
            ("Hợp đồng", att_field_hop_dong, "Hợp đồng", "so_hop_dong",
             ("hợp đồng" in lct_lower and "phụ lục" not in lct_lower)),
            ("BBNT", att_field_bbnt, "BBNT", "so_bbnt",
             ("bbnt" in lct_lower or "nghiệm thu" in lct_lower)),
            ("Phụ lục hợp đồng", att_field_phu_luc, "Phụ lục hợp đồng", "so_plhd",
             ("plhđ" in lct_lower or "phụ lục" in lct_lower)),
        ]

        # BƯỚC 1 (BẮT BUỘC): đọc TRANG ĐẦU của cả 3 cột (Hợp đồng / BBNT / Phụ lục)
        # để lấy đủ mã số (AI_Số_Hợp_đồng, AI_Số_BBNT, AI_Số_PLHĐ) — cột nào có file thì lấy số cột đó.
        for c_key, contract_att_field, contract_doc_type, so_key, _amt in contract_specs:
            if not contract_att_field:
                continue
            c_atts = fields.get(contract_att_field, [])
            if not isinstance(c_atts, list) or len(c_atts) == 0:
                continue
            print(f"  -> [Bước 1] Đọc số từ {c_key} (trang đầu)...", flush=True)
            try:
                first_blocks = []
                fnames = []
                field_id = att_field_id_map.get(contract_att_field)
                for att in c_atts:
                    if not isinstance(att, dict): continue
                    ftok = att.get("file_token", "")
                    fname = att.get("name", "file")
                    fnames.append(fname)
                    if not ftok: continue
                    tmp_path = await loop.run_in_executor(
                        None, lark.save_attachment_temp,
                        ftok, fname, app_token, table_id, field_id, record_id
                    )
                    try:
                        first_blocks.extend(FileHandler.process_file(tmp_path, first_page_only=True))
                    finally:
                        try: os.unlink(tmp_path)
                        except: pass
                if not first_blocks:
                    continue
                num_result = await loop.run_in_executor(
                    None, extractor.extract_doc_numbers, first_blocks, ", ".join(fnames), contract_doc_type
                )
                so_val = num_result.get(so_key)
                if so_val and result_fields_config.get(so_key):
                    update_fields[result_fields_config[so_key]] = str(so_val)
                    print(f"  -> [Bước 1] {c_key} số = {so_val}", flush=True)
            except Exception as e:
                print(f"  -> [Bước 1] Lỗi đọc số {c_key}: {e}", flush=True)

        # BƯỚC 2: chỉ đọc ĐẦY ĐỦ đúng loại chứng từ đang hiển thị ở cột "Loại chứng từ có chứa giá trị số tiền"
        # để lấy các trường giá trị (Số lần TT, Giá trị TT, Tổng net/gross, VAT, PIT, MST).
        for c_key, contract_att_field, contract_doc_type, so_key, is_amount_source in contract_specs:
            if not is_amount_source or not contract_att_field:
                continue
            contract_attachments = fields.get(contract_att_field, [])
            if not isinstance(contract_attachments, list) or len(contract_attachments) == 0:
                continue

            print(f"  -> [Bước 2] Xử lý chi tiết {c_key}...", flush=True)
            try:
                all_content_blocks = []
                att_filenames = []
                contract_field_id = att_field_id_map.get(contract_att_field)

                for att in contract_attachments:
                    if not isinstance(att, dict): continue
                    file_token = att.get("file_token", "")
                    att_name = att.get("name", "file")
                    att_filenames.append(att_name)
                    if not file_token: continue

                    print(f"  -> Downloading {c_key}: {att_name} ({file_token})", flush=True)
                    tmp_path = await loop.run_in_executor(
                        None, lark.save_attachment_temp,
                        file_token, att_name, app_token, table_id, contract_field_id, record_id
                    )
                    try:
                        content_blocks = FileHandler.process_file(tmp_path)
                        all_content_blocks.extend(content_blocks)
                    finally:
                        try: os.unlink(tmp_path)
                        except: pass

                if not all_content_blocks:
                    raise Exception(f"Không thể đọc file {c_key} đính kèm")

                contract_result = await loop.run_in_executor(
                    None, extractor.extract_contract,
                    all_content_blocks, ", ".join(att_filenames), contract_doc_type
                )

                mst_a = contract_result.get("mst_a")
                mst_b = contract_result.get("mst_b")

                # SỐ: cập nhật lại từ bản đọc đầy đủ (ưu tiên hơn bước 1)
                so_val = contract_result.get(so_key)
                if so_val and result_fields_config.get(so_key):
                    update_fields[result_fields_config[so_key]] = str(so_val)

                # MST của loại chứng từ chứa giá trị
                if mst_a and result_fields_config.get("mst_a"):
                    update_fields[result_fields_config["mst_a"]] = str(mst_a)
                if mst_b and result_fields_config.get("mst_b"):
                    update_fields[result_fields_config["mst_b"]] = str(mst_b)

                # GIÁ TRỊ (số tiền để số nguyên sạch; Giá trị TT hỗ trợ nhiều đợt ngăn cách ',')
                so_lan_tt = contract_result.get("so_lan_tt")
                gia_tri_tt = _format_money_str(contract_result.get("gia_tri_tt"))
                tong_net = contract_result.get("tong_gia_tri_net")
                tong_gross = contract_result.get("tong_gia_tri_gross")
                vat_ct = contract_result.get("vat_chung_tu")
                pit_ct = contract_result.get("pit_chung_tu")
                if so_lan_tt is not None and result_fields_config.get("so_lan_tt"):
                    update_fields[result_fields_config["so_lan_tt"]] = str(so_lan_tt)
                if gia_tri_tt and result_fields_config.get("gia_tri_tt"):
                    update_fields[result_fields_config["gia_tri_tt"]] = gia_tri_tt
                if tong_net is not None and result_fields_config.get("tong_gia_tri_net"):
                    update_fields[result_fields_config["tong_gia_tri_net"]] = str(tong_net)
                if tong_gross is not None and result_fields_config.get("tong_gia_tri_gross"):
                    update_fields[result_fields_config["tong_gia_tri_gross"]] = str(tong_gross)
                if vat_ct and result_fields_config.get("vat_chung_tu"):
                    update_fields[result_fields_config["vat_chung_tu"]] = str(vat_ct)
                if pit_ct and result_fields_config.get("pit_chung_tu"):
                    update_fields[result_fields_config["pit_chung_tu"]] = str(pit_ct)

                # Status (nối tiếp nếu đã có từ hóa đơn)
                contract_status = f"OK - {c_key}"
                if contract_result.get("error"):
                    contract_status = f"LỖI: {contract_result['error'][:200]}"
                elif contract_result.get("ghi_chu"):
                    contract_status += f" | {contract_result['ghi_chu'][:100]}"

                existing_status = update_fields.get(tt_field, "")
                if existing_status and "LỖI" not in existing_status:
                    update_fields[tt_field] = f"{existing_status}; {contract_status}"[:500]
                else:
                    update_fields[tt_field] = contract_status[:500]

                result_entry.setdefault("contracts", []).append({
                    "loai": c_key,
                    "so": so_val,
                    "mst_a": mst_a,
                    "mst_b": mst_b,
                    "is_amount_source": is_amount_source,
                })
                print(f"  -> [Bước 2] {c_key} processed OK.", flush=True)

            except Exception as e:
                traceback.print_exc()
                print(f"  -> {c_key} ERROR: {str(e)}", flush=True)
                error_msg = f"LỖI {c_key}: {str(e)[:200]}"
                existing_status = update_fields.get(tt_field, "")
                if existing_status:
                    update_fields[tt_field] = f"{existing_status}; {error_msg}"[:500]
                else:
                    update_fields[tt_field] = error_msg
                result_entry["error"] = str(e)
                result_entry["trang_thai"] = "LỖI"

        # Skip if no attachment fields have any files
        if not loai_chung_tu and not update_fields:
            all_att_names = []
            for af in [attachment_field, att_field_hop_dong, att_field_bbnt, att_field_phu_luc]:
                if af and af in fields:
                    atts = fields.get(af, [])
                    if isinstance(atts, list) and len(atts) > 0:
                        all_att_names.extend(atts)
            if not all_att_names:
                update_fields[result_fields_config.get("co_vat", "AI_Có_VAT")] = "Không có đính kèm"
                update_fields[result_fields_config.get("trang_thai", "AI_Trạng_Thái")] = "Bỏ qua - không có file"

        # Write all results back to Lark
        if update_fields:
            clean_fields, dropped = _sanitize_fields_for_lark(update_fields, field_type_map)
            if dropped:
                print(f"  -> Dropped non-numeric values for Number fields: {dropped}", flush=True)
            try:
                await loop.run_in_executor(None, lark.update_record, app_token, table_id, record_id, clean_fields)
                print(f"  -> Record updated OK ({len(clean_fields)} fields).", flush=True)
            except Exception as e:
                err_msg = f"Ghi Lark thất bại: {str(e)[:200]}"
                print(f"  -> Error updating record: {e}", flush=True)
                tasks[task_id]["errors"].append({"record_id": record_id, "error": err_msg})
                result_entry["error"] = err_msg
                result_entry["trang_thai"] = "LỖI GHI LARK"

        if "trang_thai" not in result_entry:
            result_entry["trang_thai"] = update_fields.get(result_fields_config.get("trang_thai", "AI_Trạng_Thái"), "OK")
        tasks[task_id]["results"].append(result_entry)
        tasks[task_id]["processed"] = i + 1
        await asyncio.sleep(0.3)

    tasks[task_id]["status"] = "done"
    tasks[task_id]["current_record"] = ""
    print(f"+++ [DONE] process_lark_batch task_id={task_id} +++", flush=True)


@app.get("/api/lark/status/{task_id}")
async def get_lark_task_status(task_id: str):
    """Get processing status."""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")

    task = tasks[task_id]
    return {
        "status": task["status"],
        "total": task["total"],
        "processed": task["processed"],
        "current_record": task["current_record"],
        "results": task["results"] if task["status"] == "done" else [],
        "errors": task["errors"],
    }


@app.post("/api/export")
async def export_excel(data: dict):
    """Export results to Excel."""
    results = data.get("results", [])
    if not results:
        raise HTTPException(status_code=400, detail="Không có dữ liệu để xuất")

    buffer = export_to_excel(results)
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=ket_qua_boc_tach_hoa_don.xlsx"}
    )


@app.post("/api/upload")
async def upload_and_extract(files: List[UploadFile] = File(...)):
    """
    Upload files directly and extract invoice / bill data via AI.
    Supports PDF, images (JPG, PNG), Word, Excel.
    Returns list of extracted results immediately.
    """
    cfg = load_config()
    api_key = cfg.get("openai_api_key", "")
    model = cfg.get("openai_model", "gpt-4o")
    base_url = cfg.get("openai_base_url", "")

    if not api_key:
        raise HTTPException(status_code=400, detail="Chưa cấu hình OpenAI API Key trong Cài đặt")

    extractor = AIExtractor(api_key=api_key, model=model, base_url=base_url)
    calculator = VATCalculator()
    results = []

    for upload in files:
        filename = upload.filename or "file"
        file_id = str(uuid.uuid4())
        saved_path = UPLOAD_DIR / f"{file_id}_{filename}"
        try:
            content = await upload.read()
            with open(saved_path, "wb") as f:
                f.write(content)

            content_blocks = FileHandler.process_file(str(saved_path))
            raw_result = extractor.extract_invoice(content_blocks, filename)
            processed = calculator.process(raw_result)

            uploaded_files[file_id] = str(saved_path)

            co_vat = processed.get("co_vat", False)
            results.append({
                "filename": filename,
                "file_id": file_id,
                "so_hoa_don": raw_result.get("so_hoa_don"),
                "ngay_hoa_don": raw_result.get("ngay_hoa_don"),
                "thoi_gian_xu_ly": raw_result.get("thoi_gian_xu_ly") or raw_result.get("ngay_hoa_don"),
                "ben_a": raw_result.get("ben_a") or raw_result.get("ten_nguoi_ban") or raw_result.get("ten_nha_cung_cap"),
                "so_tien_trich_no": raw_result.get("so_tien_trich_no") or processed.get("tong_thanh_toan_computed"),
                "ben_b": raw_result.get("ben_b") or raw_result.get("ten_nguoi_mua"),
                "ten_nguoi_ban": raw_result.get("ten_nguoi_ban") or raw_result.get("ten_nha_cung_cap"),
                "ten_nguoi_mua": raw_result.get("ten_nguoi_mua"),
                "ma_so_thue_nguoi_ban": raw_result.get("ma_so_thue_nguoi_ban") or raw_result.get("ma_so_thue"),
                "ma_so_thue_nguoi_mua": raw_result.get("ma_so_thue_nguoi_mua"),
                "noi_dung": raw_result.get("noi_dung", ""),
                "co_vat": co_vat,
                "tien_truoc_thue": processed.get("tong_tien_truoc_thue_computed"),
                "tien_vat": processed.get("tien_vat_computed"),
                "tong_thanh_toan": processed.get("tong_thanh_toan_computed"),
                "thue_suat_vat": raw_result.get("thue_suat_vat"),
                "loai_tien": raw_result.get("loai_tien", "VND"),
                "trang_thai": processed.get("trang_thai", "OK"),
                "ghi_chu": raw_result.get("ghi_chu", ""),
                "error": raw_result.get("error"),
            })
        except Exception as e:
            traceback.print_exc()
            try:
                os.unlink(saved_path)
            except:
                pass
            uploaded_files.pop(file_id, None)
            results.append({
                "filename": filename,
                "error": str(e),
                "trang_thai": "LỖI",
                "co_vat": False,
            })

    return {"results": results}


@app.post("/api/lark/push-upload")
async def push_upload_results_to_lark(data: dict):
    """
    Push extracted upload results to a specified Lark Base table.
    Smart mapping existing columns and gracefully handling field permissions.
    """
    results = data.get("results", [])
    if not results:
        raise HTTPException(status_code=400, detail="Không có dữ liệu để đẩy lên Lark Base")

    cfg = load_config()
    default_lark_url = cfg.get("lark_upload_url", "https://o4pvcegwn6b.sg.larksuite.com/base/Eaiabgix8a7mwqs63molRe6UgYf?table=tblYjTMstZfy1Ua6&view=vewViuZElT")
    lark_url = data.get("lark_url") or default_lark_url
    mode = data.get("mode", "bill")  # "bill" or "vat"

    parsed = parse_lark_url(lark_url)
    app_token = parsed.get("app_token")
    table_id = parsed.get("table_id")

    if not app_token or not table_id:
        raise HTTPException(status_code=400, detail="Không thể lấy app_token hoặc table_id từ đường dẫn Lark Base")

    lark = make_lark_client()
    if parsed.get("base_url"):
        lark.base_url = parsed["base_url"]

    # Fetch existing fields in target Lark table
    try:
        existing_fields = lark.list_fields(app_token, table_id)
        existing_names = {f["field_name"]: f for f in existing_fields}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Không thể kết nối bảng Lark Base: {str(e)}")

    if mode == "bill":
        target_columns = {
            "Bên A": 1,              # Text
            "Số tiền trích nợ": 2,    # Number
            "Bên B": 1,              # Text
            "Nội dung": 1,            # Text
            "Thời gian xử lý": 1,     # Text
            "Hóa đơn": 17,            # Attachment
            "Trạng thái": 1,          # Text
        }
    else:
        rf = {**DEFAULT_RESULT_FIELDS, **cfg.get("lark_result_fields", {})}
        target_columns = {
            rf.get("co_vat", "AI_Có_VAT"): 1,
            rf.get("tien_truoc_thue", "AI_Tiền_Trước_Thuế"): 2,
            rf.get("tien_vat", "AI_Tiền_VAT"): 2,
            rf.get("so_hoa_don", "AI_Số_HĐ"): 1,
            rf.get("ngay_hoa_don", "AI_Ngày_HĐ"): 1,
            rf.get("ten_nguoi_ban", "AI_Người_Bán"): 1,
            rf.get("ten_nguoi_mua", "AI_Người_Mua"): 1,
            rf.get("trang_thai", "AI_Trạng_Thái"): 1,
        }

    # Attempt to auto-create missing fields safely
    for col_name, col_type in target_columns.items():
        if col_name and col_name not in existing_names:
            try:
                lark.create_field(app_token, table_id, col_name, col_type)
                existing_names[col_name] = {"field_name": col_name, "type": col_type}
            except Exception as e:
                # If bot doesn't have field edit permission (403), ignore safely
                pass

    # Helper function to find best matching existing column name
    def find_best_column(preferred_name: str, candidates: list) -> Optional[str]:
        if preferred_name in existing_names:
            return preferred_name
        for cand in candidates:
            if cand in existing_names:
                return cand
        # Case-insensitive fallback
        lower_names = {k.lower(): k for k in existing_names}
        if preferred_name.lower() in lower_names:
            return lower_names[preferred_name.lower()]
        for cand in candidates:
            if cand.lower() in lower_names:
                return lower_names[cand.lower()]
        return None

    # Resolve field mappings
    col_map = {}
    if mode == "bill":
        col_map["ben_a"] = find_best_column("Bên A", ["Bên A (Trích nợ)", "Tên tài khoản trích nợ", "Trích nợ", "Bên chuyển", "Người bán", "AI_Người_Bán", "Text"])
        col_map["so_tien"] = find_best_column("Số tiền trích nợ", ["Số tiền", "SỐ TIỀN", "Amount", "Số tiền ~ Số tiền", "Tổng tiền", "AI_Tiền_Trước_Thuế"])
        col_map["ben_b"] = find_best_column("Bên B", ["Bên B (Người hưởng)", "Người hưởng", "Beneficiary", "Bên nhận", "Người mua", "AI_Người_Mua"])
        col_map["noi_dung"] = find_best_column("Nội dung", ["Nội dung thanh toán", "Text", "Mô tả", "Remarks", "Ghi chú"])
        col_map["thoi_gian"] = find_best_column("Thời gian xử lý", ["Thời gian", "Ngày HĐ", "AI_Ngày_HĐ", "Process on", "Ngày tạo"])
        col_map["file"] = find_best_column("Hóa đơn", ["Tệp đính kèm", "File", "Tệp", "Bill", "Tên file"])
        col_map["status"] = find_best_column("Trạng thái", ["AI_Trạng_Thái", "Status"])
    else:
        rf = {**DEFAULT_RESULT_FIELDS, **cfg.get("lark_result_fields", {})}
        col_map["co_vat"] = find_best_column(rf.get("co_vat", "AI_Có_VAT"), ["Có VAT", "VAT?"])
        col_map["tien_truoc_thue"] = find_best_column(rf.get("tien_truoc_thue", "AI_Tiền_Trước_Thuế"), ["Trước thuế", "Số tiền trước thuế"])
        col_map["tien_vat"] = find_best_column(rf.get("tien_vat", "AI_Tiền_VAT"), ["Tiền VAT", "VAT"])
        col_map["so_hoa_don"] = find_best_column(rf.get("so_hoa_don", "AI_Số_HĐ"), ["Số HĐ", "Số hoá đơn"])
        col_map["ngay_hoa_don"] = find_best_column(rf.get("ngay_hoa_don", "AI_Ngày_HĐ"), ["Ngày HĐ", "Ngày hoá đơn"])
        col_map["ten_nguoi_ban"] = find_best_column(rf.get("ten_nguoi_ban", "AI_Người_Bán"), ["Người bán", "Bên A"])
        col_map["ten_nguoi_mua"] = find_best_column(rf.get("ten_nguoi_mua", "AI_Người_Mua"), ["Người mua", "Bên B"])
        col_map["status"] = find_best_column(rf.get("trang_thai", "AI_Trạng_Thái"), ["Trạng thái", "Status"])

    # Ensure the "Hóa đơn" column is an Attachment field (type 17).
    # If it exists as another type (e.g. Text), convert it in place.
    file_field_type = None
    if mode == "bill":
        _c_file = col_map.get("file")
        if _c_file and _c_file in existing_names:
            _f = existing_names[_c_file]
            file_field_type = _f.get("type")
            if file_field_type != 17 and _f.get("field_id"):
                try:
                    lark.update_field(app_token, table_id, _f["field_id"], _c_file, 17)
                    file_field_type = 17
                except Exception as e:
                    print(f"  -> Could not convert '{_c_file}' to attachment: {e}", flush=True)

    uploaded_tokens: Dict[str, str] = {}

    # Map tên field -> kiểu field thực tế của bảng Lark (để ép kiểu Number trước khi ghi)
    field_type_map = {name: f.get("type") for name, f in existing_names.items()}

    records_payload = []
    for r in results:
        fields = {}
        if mode == "bill":
            c_a = col_map.get("ben_a")
            c_b = col_map.get("ben_b")
            c_nd = col_map.get("noi_dung")
            c_tg = col_map.get("thoi_gian")
            c_file = col_map.get("file")
            c_st = col_map.get("status")
            c_tien = col_map.get("so_tien")

            if c_a: fields[c_a] = r.get("ben_a") or r.get("ten_nguoi_ban") or ""
            if c_b: fields[c_b] = r.get("ben_b") or r.get("ten_nguoi_mua") or ""
            if c_nd: fields[c_nd] = r.get("noi_dung") or ""
            if c_tg: fields[c_tg] = r.get("thoi_gian_xu_ly") or r.get("ngay_hoa_don") or ""
            if c_file:
                if file_field_type == 17:
                    fid = r.get("file_id")
                    fpath = uploaded_files.get(fid) if fid else None
                    if fpath and os.path.exists(fpath):
                        try:
                            token = uploaded_tokens.get(fid)
                            if not token:
                                token = lark.upload_media(fpath, r.get("filename") or "file", app_token)
                                uploaded_tokens[fid] = token
                            if token:
                                fields[c_file] = [{"file_token": token}]
                        except Exception as e:
                            print(f"  -> Upload attachment failed: {e}", flush=True)
                else:
                    fields[c_file] = r.get("filename") or ""
            if c_st: fields[c_st] = r.get("trang_thai") or "OK"

            if c_tien:
                so_tien = r.get("so_tien_trich_no")
                if so_tien is None: so_tien = r.get("tong_thanh_toan")
                if so_tien is not None:
                    fields[c_tien] = str(so_tien)
        else:
            c_cv = col_map.get("co_vat")
            c_tt = col_map.get("tien_truoc_thue")
            c_tv = col_map.get("tien_vat")
            c_shd = col_map.get("so_hoa_don")
            c_nhd = col_map.get("ngay_hoa_don")
            c_nb = col_map.get("ten_nguoi_ban")
            c_nm = col_map.get("ten_nguoi_mua")
            c_st = col_map.get("status")

            if c_cv: fields[c_cv] = "Có" if r.get("co_vat") else "Không"
            if c_st: fields[c_st] = r.get("trang_thai") or "OK"
            if c_shd and r.get("so_hoa_don"): fields[c_shd] = str(r["so_hoa_don"])
            if c_nhd and r.get("ngay_hoa_don"): fields[c_nhd] = str(r["ngay_hoa_don"])
            if c_nb and r.get("ten_nguoi_ban"): fields[c_nb] = str(r["ten_nguoi_ban"])
            if c_nm and r.get("ten_nguoi_mua"): fields[c_nm] = str(r["ten_nguoi_mua"])
            if c_tt and r.get("tien_truoc_thue") is not None:
                fields[c_tt] = str(r["tien_truoc_thue"])
            if c_tv and r.get("tien_vat") is not None:
                fields[c_tv] = str(r["tien_vat"])

        if fields:
            # Ép kiểu các cột Number (vd "Số tiền trích nợ") để tránh NumberFieldConvFail
            fields, _dropped = _sanitize_fields_for_lark(fields, field_type_map)
            if _dropped:
                print(f"  -> Bỏ field không ép được kiểu số: {_dropped}", flush=True)
            if fields:
                records_payload.append(fields)

    if not records_payload:
        raise HTTPException(status_code=400, detail="Không có cột nào khớp giữa kết quả bóc tách và bảng Lark Base")

    try:
        created = lark.batch_create_records(app_token, table_id, records_payload)
        # Clean up local files after a successful push
        for r in results:
            fid = r.get("file_id")
            if fid and fid in uploaded_files:
                try:
                    os.unlink(uploaded_files[fid])
                except:
                    pass
                uploaded_files.pop(fid, None)
        return {
            "ok": True,
            "count": len(created),
            "message": f"Đã đẩy thành công {len(created)} bản ghi vào Lark Base!"
        }
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Lỗi khi ghi bản ghi vào Lark Base: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=True)
