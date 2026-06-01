"""
AI Extractor Module - Sử dụng GPT-4o để bóc tách dữ liệu hoá đơn
"""
import json
import re
from typing import Dict, Any, List, Optional

from openai import OpenAI


EXTRACTION_PROMPT = """Bạn là chuyên gia phân tích hoá đơn tài chính Việt Nam. Hãy phân tích hoá đơn/chứng từ thanh toán này và trích xuất thông tin chi tiết.

QUAN TRỌNG:
- Nếu hoá đơn CÓ VAT (thuế GTGT): tách riêng số tiền trước thuế, thuế suất %, tiền VAT, và tổng thanh toán sau thuế
- Nếu hoá đơn KHÔNG CÓ VAT: chỉ lấy tổng số tiền cuối cùng cần thanh toán
- Đọc kỹ mọi số liệu trên hoá đơn, đặc biệt là phần tổng cộng
- PHÂN BIỆT rõ: Người bán (Seller/Đơn vị bán hàng) và Người mua (Buyer/Đơn vị mua hàng)

Trả về JSON theo ĐÚNG format sau (không thêm markdown code block):
{
    "so_hoa_don": "Số sê-ri / mã số hoá đơn (thường nằm ở PHẦN ĐẦU tài liệu, gần nhãn 'No.', 'Số HĐ', 'Invoice No', 'Số:', 'No:', 'Ký hiệu + Số'). Ghi NGUYÊN GIÁ TRỊ đọc được (có thể gồm chữ và số như AA/22K-0001234, hay chỉ số như 0001234). KHÔNG lấy mã số thuế, số tài khoản, số điện thoại. Nếu không tìm thấy ghi null",
    "ngay_hoa_don": "Ngày phát hành hoá đơn, định dạng DD/MM/YYYY. Thường nằm ở đầu tài liệu gần chữ 'Ngày', 'Date', 'Ngày lập', 'Ngày tháng năm'. Nếu không rõ ghi null",
    "ten_nguoi_ban": "Tên công ty/cá nhân BÁN hàng (Seller). Trên hoá đơn GTGT thường ghi là 'Đơn vị bán hàng', 'Seller', 'Người bán', 'Bên bán', 'Tên đơn vị' ở phần TRÊN. Lấy tên đầy đủ, nếu không rõ ghi null",
    "ma_so_thue_nguoi_ban": "MST của người bán, thường đi kèm với tên người bán. Nếu không có ghi null",
    "ten_nguoi_mua": "Tên công ty/cá nhân MUA hàng (Buyer). Trên hoá đơn GTGT thường ghi là 'Đơn vị mua hàng', 'Buyer', 'Người mua', 'Bên mua', 'Họ tên người mua hàng'. Lấy tên đầy đủ, nếu không rõ ghi null",
    "ma_so_thue_nguoi_mua": "MST của người mua, thường đi kèm với tên người mua. Nếu không có ghi null",
    "ten_nha_cung_cap": "Giống ten_nguoi_ban - để tương thích ngược",
    "ma_so_thue": "MST nhà cung cấp (người bán), nếu không có ghi null",
    "noi_dung": "mô tả ngắn gọn nội dung thanh toán",
    "co_vat": true hoặc false,
    "danh_sach_hang_hoa": [
        {
            "ten": "tên hàng hoá/dịch vụ",
            "so_luong": số lượng (number),
            "don_gia": đơn giá (number),
            "thanh_tien": thành tiền (number)
        }
    ],
    "tong_tien_truoc_thue": tổng tiền trước thuế (number, null nếu không có VAT),
    "thue_suat_vat": thuế suất % (number, ví dụ 10, 8, 5. null nếu không có VAT),
    "tien_vat": tiền VAT (number, null nếu không có VAT),
    "tong_thanh_toan": tổng tiền cuối cùng phải thanh toán (number),
    "loai_tien": "VND" hoặc loại tiền khác nếu có,
    "ghi_chu": "ghi chú thêm nếu có thông tin đặc biệt, ví dụ: số liệu không khớp, hoá đơn mờ,..."
}

CHÚ Ý:
- Tất cả giá trị tiền phải là NUMBER (không có dấu chấm phân cách hàng nghìn, không có ký tự đặc biệt)
- Ví dụ: 1500000 thay vì "1.500.000" hay "1,500,000 VND"
- Nếu không đọc được rõ, ghi giá trị gần nhất có thể và ghi chú trong trường ghi_chu
- Nếu hoá đơn có nhiều trang, tổng hợp tất cả thông tin lại
- PHÂN BIỆT RÕ người bán (đơn vị cung cấp hàng/dịch vụ) và người mua (đơn vị nhận hàng/thanh toán)
- TRONG MỌI TRƯỜNG HỢP (kể cả ảnh mờ, không có thông tin, hoặc không phải hoá đơn): TUYỆT ĐỐI CHỈ TRẢ VỀ JSON, không kèm bất kỳ văn bản giải thích nào khác. Các trường không có dữ liệu hãy để null và điền nguyên nhân vào "ghi_chu".
"""


class AIExtractor:
    """Sử dụng OpenAI/DeepSeek/Gemini để bóc tách dữ liệu từ hoá đơn."""

    def __init__(self, api_key: str, model: str = "gpt-4o", base_url: str = ""):
        self.client = OpenAI(api_key=api_key, base_url=base_url if base_url else None)
        self.model = model

    def extract_invoice(self, content_blocks: List[Dict[str, Any]], filename: str = "") -> Dict[str, Any]:
        """
        Bóc tách dữ liệu từ hoá đơn.

        Args:
            content_blocks: List of {"type": "image"|"text", "content": ...}
            filename: Tên file gốc (để context)

        Returns:
            Dict chứa dữ liệu hoá đơn đã trích xuất
        """
        messages = self._build_messages(content_blocks, filename)

        try:
            # Chuẩn bị tham số cho API
            api_kwargs = {
                "model": self.model,
                "messages": messages,
                "max_tokens": 8192,
                "temperature": 0.1,
            }
            
            # Chỉ bật JSON mode nếu là model OpenAI (như gpt-4o, gpt-4-turbo)
            # Một số proxy/model khác có thể lỗi nếu truyền response_format
            if "gpt-4" in self.model.lower() or "gpt-3.5" in self.model.lower():
                api_kwargs["response_format"] = {"type": "json_object"}

            response = self.client.chat.completions.create(**api_kwargs)

            raw_text = response.choices[0].message.content.strip()
            result = self._parse_response(raw_text)
            result["_raw_response"] = raw_text
            result["_filename"] = filename
            return result

        except Exception as e:
            return {
                "error": str(e),
                "_filename": filename,
                "so_hoa_don": None,
                "ngay_hoa_don": None,
                "ten_nha_cung_cap": None,
                "ma_so_thue": None,
                "noi_dung": f"Lỗi xử lý: {str(e)}",
                "co_vat": False,
                "danh_sach_hang_hoa": [],
                "tong_tien_truoc_thue": None,
                "thue_suat_vat": None,
                "tien_vat": None,
                "tong_thanh_toan": None,
                "loai_tien": "VND",
                "ghi_chu": f"Lỗi: {str(e)}"
            }

    def _build_messages(self, content_blocks: List[Dict[str, Any]], filename: str) -> list:
        """Xây dựng messages cho API call."""
        user_content = []

        # Add text instruction
        context = f"File: {filename}\n\n" if filename else ""
        user_content.append({
            "type": "text",
            "text": f"{context}{EXTRACTION_PROMPT}"
        })

        # Add content blocks
        for block in content_blocks:
            if block["type"] == "image":
                mime_type = block.get("mime_type", "image/jpeg")
                user_content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{block['content']}"
                        # Note: "detail" param is OpenAI-specific, omitted for Gemini compatibility
                    }
                })
            elif block["type"] == "text":
                user_content.append({
                    "type": "text",
                    "text": f"\n--- NỘI DUNG FILE ---\n{block['content']}\n--- HẾT ---"
                })

        return [{"role": "user", "content": user_content}]

    def _parse_response(self, raw_text: str) -> Dict[str, Any]:
        """Parse JSON response - robust for GPT, Gemini, DeepSeek."""
        cleaned = raw_text.strip()

        # Strategy 0: Strip <think> tags (DeepSeek Reasoner)
        cleaned = re.sub(r'<think>[\s\S]*?</think>', '', cleaned).strip()

        # Strategy 1: Strip markdown code block (```json ... ``` or ``` ... ```)
        # Use DOTALL so newlines are included
        code_block_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', cleaned)
        if code_block_match:
            cleaned = code_block_match.group(1).strip()

        # Strategy 2: Try to parse directly
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # Strategy 3: Find the first { ... } block in raw text (greedy)
        json_match = re.search(r'(\{[\s\S]*\})', cleaned)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        # Strategy 4: Handle truncated JSON by adding closing brackets
        # (This is basic, might need more robust handling for deeply nested JSON)
        if "{" in cleaned:
            start_idx = cleaned.find("{")
            truncated = cleaned[start_idx:]
            
            # Remove any trailing commas or incomplete keys/values at the end
            truncated = re.sub(r',\s*$', '', truncated)
            truncated = re.sub(r',?\s*"[^"]*"?\s*:\s*$', '', truncated)
            
            # Try to fix by appending } or ]}
            for suffix in ["", "}", "]}", "}]}", '"}', '"]}', '"}]']:
                try:
                    return json.loads(truncated + suffix)
                except json.JSONDecodeError:
                    continue

        return {
            "error": "Cannot parse AI response",
            "noi_dung": "Không thể parse kết quả AI",
            "co_vat": False,
            "tong_thanh_toan": None,
            "ghi_chu": f"Raw response: {raw_text[:500]}"
        }
