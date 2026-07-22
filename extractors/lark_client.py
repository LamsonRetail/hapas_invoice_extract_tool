"""
Lark Bitable API Client - Kết nối với Lark Base để đọc/ghi dữ liệu
"""
import json
import os
import tempfile
import time
import urllib.parse
from typing import Dict, Any, List, Optional, Tuple

import requests

LARK_TIMEOUT = 30  # seconds


class LarkClient:
    """Client tương tác với Lark Open API (Bitable + Drive)."""

    def __init__(self, app_id: str, app_secret: str, base_url: str = "https://open.larksuite.com"):
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self._token: Optional[str] = None
        self._token_expires: float = 0

    # ========== Auth ==========

    def _get_token(self) -> str:
        """Lấy tenant_access_token, cache cho đến khi hết hạn."""
        if self._token and time.time() < self._token_expires:
            return self._token

        url = f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal"
        resp = requests.post(url, json={
            "app_id": self.app_id,
            "app_secret": self.app_secret,
        }, timeout=LARK_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise Exception(f"Lark auth error: {data.get('msg', 'Unknown error')}")

        self._token = data["tenant_access_token"]
        self._token_expires = time.time() + data.get("expire", 7200) - 60
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }

    # ========== Bitable Records ==========

    def get_bitable_app_token(self, wiki_node_token: str) -> str:
        """Lấy bitable app_token từ wiki node token (cho base embed trong wiki)."""
        url = f"{self.base_url}/open-apis/wiki/v2/spaces/get_node"
        resp = requests.get(url, headers=self._headers(), params={"token": wiki_node_token}, timeout=LARK_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            # Fallback: dùng node token trực tiếp làm app_token
            return wiki_node_token

        node = data.get("data", {}).get("node", {})
        obj_token = node.get("obj_token", wiki_node_token)
        return obj_token

    def list_records(
        self, app_token: str, table_id: str,
        view_id: str = None, page_size: int = 100, page_token: str = None,
        fields: List[str] = None
    ) -> Tuple[List[dict], Optional[str], int]:
        """
        Lấy danh sách records từ Bitable.

        Returns: (records, next_page_token, total)
        """
        url = f"{self.base_url}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search"

        body = {
            "automatic_fields": True,  # Include auto fields like record_id
        }
        if view_id:
            body["view_id"] = view_id
        if fields:
            body["field_names"] = fields

        params = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token

        resp = requests.post(url, headers=self._headers(), params=params, json=body, timeout=LARK_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise Exception(f"Lark list_records error: {data.get('msg')}")

        items = data.get("data", {}).get("items", [])
        next_token = data.get("data", {}).get("page_token")
        has_more = data.get("data", {}).get("has_more", False)
        total = data.get("data", {}).get("total", len(items))

        return items, next_token if has_more else None, total

    def get_record(self, app_token: str, table_id: str, record_id: str) -> dict:
        """Lấy 1 record theo record_id."""
        url = f"{self.base_url}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
        resp = requests.get(url, headers=self._headers(), timeout=LARK_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise Exception(f"Lark get_record error: {data.get('msg')}")

        return data.get("data", {}).get("record", {})

    def update_record(self, app_token: str, table_id: str, record_id: str, fields: dict) -> bool:
        """Cập nhật fields của 1 record."""
        url = f"{self.base_url}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"

        resp = requests.put(url, headers=self._headers(), json={"fields": fields}, timeout=LARK_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise Exception(f"Lark update_record error: {data.get('msg')}")

        return True

    def create_record(self, app_token: str, table_id: str, fields: dict) -> dict:
        """Tạo 1 record mới trong Bitable."""
        url = f"{self.base_url}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"

        resp = requests.post(url, headers=self._headers(), json={"fields": fields}, timeout=LARK_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise Exception(f"Lark create_record error: {data.get('msg')}")

        return data.get("data", {}).get("record", {})

    def batch_create_records(self, app_token: str, table_id: str, records_fields: List[dict]) -> List[dict]:
        """Tạo nhiều records cùng lúc trong Bitable (batch up to 500 records)."""
        if not records_fields:
            return []

        url = f"{self.base_url}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"
        body = {
            "records": [{"fields": f} for f in records_fields]
        }

        resp = requests.post(url, headers=self._headers(), json=body, timeout=LARK_TIMEOUT)
        # Surface the real Lark error message even on HTTP 4xx/5xx
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise

        if data.get("code") != 0:
            # Do NOT retry with the same payload: attachment file_tokens are
            # single-use, so a silent single-record fallback would fail with
            # AttachFieldConvFail and hide the real cause. Surface it instead.
            raise Exception(f"Lark batch_create error ({data.get('code')}): {data.get('msg')}")

        return data.get("data", {}).get("records", [])

    # ========== Fields ==========

    def list_fields(self, app_token: str, table_id: str) -> List[dict]:
        """Lấy danh sách fields (columns) của table."""
        url = f"{self.base_url}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
        resp = requests.get(url, headers=self._headers(), timeout=LARK_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise Exception(f"Lark list_fields error: {data.get('msg')}")

        return data.get("data", {}).get("items", [])

    def create_field(self, app_token: str, table_id: str, field_name: str, field_type: int) -> dict:
        """
        Tạo field mới.
        field_type: 1=Text, 2=Number, 7=Checkbox
        """
        url = f"{self.base_url}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"

        body = {
            "field_name": field_name,
            "type": field_type,
        }

        # Number field formatting
        if field_type == 2:
            body["property"] = {
                "formatter": "0.00",
            }

        resp = requests.post(url, headers=self._headers(), json=body, timeout=LARK_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise Exception(f"Lark create_field error: {data.get('msg')}")

        return data.get("data", {}).get("field", {})

    def update_field(self, app_token: str, table_id: str, field_id: str, field_name: str, field_type: int) -> dict:
        """Đổi kiểu / tên của một field đã tồn tại."""
        url = f"{self.base_url}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields/{field_id}"
        resp = requests.put(url, headers=self._headers(), json={"field_name": field_name, "type": field_type}, timeout=LARK_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise Exception(f"Lark update_field error: {data.get('msg')}")

        return data.get("data", {}).get("field", {})

    def ensure_result_fields(self, app_token: str, table_id: str, result_fields: dict) -> dict:
        """
        Đảm bảo các cột kết quả tồn tại. Tạo nếu chưa có.

        result_fields: {"co_vat": "AI_Có_VAT", ...}
        Returns: mapping field_name -> field_id
        """
        existing = self.list_fields(app_token, table_id)
        existing_names = {f["field_name"]: f for f in existing}

        # Field types: 1=Text, 2=Number
        field_types = {
            result_fields["co_vat"]: 1,            # Text
            result_fields["tien_truoc_thue"]: 2,    # Number
            result_fields["tien_vat"]: 2,            # Number
            result_fields["trang_thai"]: 1,          # Text
            result_fields.get("ngay_hoa_don", "AI_Ngày_HĐ"): 1,  # Text
            result_fields.get("so_hoa_don", "AI_Số_HĐ"): 1,       # Text
        }

        field_map = {}
        for field_name, field_type in field_types.items():
            if not field_name:
                continue
            if field_name in existing_names:
                field_map[field_name] = existing_names[field_name].get("field_id")
            else:
                try:
                    result = self.create_field(app_token, table_id, field_name, field_type)
                    field_map[field_name] = result.get("field_id")
                except Exception as e:
                    print(f"Warning: Could not create field {field_name}: {e}")

        return field_map

    # ========== Attachments ==========

    def upload_media(self, file_path: str, file_name: str, app_token: str, parent_type: str = "bitable_file") -> str:
        """
        Upload a local file to Lark Drive as a Bitable attachment media.
        Returns the file_token to be placed into an attachment field.
        """
        url = f"{self.base_url}/open-apis/drive/v1/medias/upload_all"
        size = os.path.getsize(file_path)
        headers = {"Authorization": f"Bearer {self._get_token()}"}

        with open(file_path, "rb") as fh:
            data = {
                "file_name": file_name,
                "parent_type": parent_type,
                "parent_node": app_token,
                "size": str(size),
                "extra": json.dumps({"drive_route_token": app_token}),
            }
            files = {"file": (file_name, fh)}
            resp = requests.post(url, headers=headers, data=data, files=files, timeout=120)

        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != 0:
            raise Exception(f"Lark upload_media error: {result.get('msg')}")

        return result.get("data", {}).get("file_token", "")

    def download_attachment(
        self, file_token: str,
        app_token: str = None, table_id: str = None,
        field_id: str = None, record_id: str = None,
    ) -> Tuple[str, bytes]:
        """
        Tải file đính kèm về.

        Returns: (filename, file_bytes)
        """
        url = f"{self.base_url}/open-apis/drive/v1/medias/{file_token}/download"

        headers = {
            "Authorization": f"Bearer {self._get_token()}",
        }

        # Build extra param for advanced permissions
        params = {}
        if app_token and table_id and field_id and record_id:
            extra = {
                "bitablePerm": {
                    "tableId": table_id,
                    "attachments": {
                        field_id: {
                            record_id: [file_token]
                        }
                    }
                }
            }
            params["extra"] = json.dumps(extra)

        resp = requests.get(url, headers=headers, params=params, timeout=60)  # longer for downloads
        resp.raise_for_status()

        # Get filename from Content-Disposition
        filename = file_token
        cd = resp.headers.get("Content-Disposition", "")
        if "filename" in cd:
            try:
                # Try to parse filename from header
                if "filename*=" in cd:
                    parts = cd.split("filename*=")[1].split("'")
                    filename = urllib.parse.unquote(parts[-1].strip('"'))
                elif "filename=" in cd:
                    filename = cd.split("filename=")[1].strip('"').strip()
            except:
                pass

        return filename, resp.content

    def save_attachment_temp(
        self, file_token: str, original_name: str = None,
        app_token: str = None, table_id: str = None,
        field_id: str = None, record_id: str = None,
    ) -> str:
        """Tải attachment về file tạm, trả về đường dẫn."""
        filename, content = self.download_attachment(
            file_token, app_token, table_id, field_id, record_id
        )

        if original_name:
            filename = original_name

        # Determine extension
        ext = os.path.splitext(filename)[1] if filename else ".bin"

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext, prefix="lark_att_")
        tmp.write(content)
        tmp.close()

        return tmp.name
