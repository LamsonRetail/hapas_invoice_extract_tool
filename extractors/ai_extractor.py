"""
AI Extractor Module - Sử dụng GPT-4o để bóc tách dữ liệu hoá đơn và chứng từ hợp đồng
"""
import json
import re
import time
from typing import Dict, Any, List, Optional

from openai import OpenAI


EXTRACTION_PROMPT = """Bạn là chuyên gia phân tích hoá đơn tài chính và chứng từ giao dịch ngân hàng Việt Nam. Hãy phân tích hoá đơn/chứng từ thanh toán/bill chuyển khoản này và trích xuất thông tin chi tiết.

QUAN TRỌNG:
- Với chứng từ giao dịch ngân hàng / bill chuyển khoản / ủy nhiệm chi:
  + "ben_a": Tên tài khoản trích nợ / Bên chuyển (ví dụ: "CTY TNHH KDTM HTC VIET NAM", đọc tại 'Tên tài khoản trích nợ', 'Dr A/C Name', 'Bên chuyển')
  + "so_tien_trich_no": Số tiền trích nợ / chuyển khoản (phải là số INTEGER sạch, không có dấu chấm/phẩy hay chữ VND, ví dụ 108000000 thay vì 108,000,000 VND)
  + "ben_b": Tên người hưởng / bên nhận (ví dụ: "TTNNL CO., LTD", đọc tại 'Người hưởng', 'Beneficiary', 'Tên tài khoản nhận')
  + "noi_dung": Nội dung thanh toán / Remarks / Diễn giải (ví dụ: "3053 htc thanh toan")
  + "thoi_gian_xu_ly": Ngày giờ thời gian xử lý giao dịch (ví dụ: "10/07/2026 16:59:52" hoặc "10/07/2026 16:59", đọc tại 'Thời gian xử lý', 'Process on', 'Ngày tạo giao dịch', 'Ngày NH nhận giao dịch')

- Với hoá đơn GTGT / tài chính:
  + Nếu hoá đơn CÓ VAT: tách riêng số tiền trước thuế, thuế suất %, tiền VAT, và tổng thanh toán sau thuế
  + Nếu KHÔNG CÓ VAT: chỉ lấy tổng số tiền cuối cùng cần thanh toán
  + Phân biệt rõ: Người bán (Seller/Đơn vị bán hàng) và Người mua (Buyer/Đơn vị mua hàng)

- ⚠️ QUY TẮC MST NGƯỜI BÁN / NGƯỜI MUA (TUYỆT ĐỐI KHÔNG ĐƯỢC ĐẢO NGƯỢC):
  + "ma_so_thue_nguoi_ban" = MST của NGƯỜI BÁN. Người bán là đơn vị PHÁT HÀNH hoá đơn, thường ở khối phía TRÊN, gắn nhãn "Đơn vị bán hàng" / "Người bán" / "Seller" / "Đơn vị cung cấp".
  + "ma_so_thue_nguoi_mua" = MST của NGƯỜI MUA. Người mua là KHÁCH HÀNG, thường ở khối phía DƯỚI, gắn nhãn "Đơn vị mua hàng" / "Người mua" / "Buyer" / "Khách hàng".
  + MST luôn thuộc về đúng đơn vị đứng NGAY CẠNH/BÊN DƯỚI tên đơn vị đó. Không được gán MST của người bán cho người mua và ngược lại.
  + Nếu chỉ chắc chắn được 1 bên, để bên còn lại là null, KHÔNG đoán bừa hay hoán đổi.

Trả về JSON theo ĐÚNG format sau (không thêm markdown code block):
{
    "so_hoa_don": "Số sê-ri / mã số hoá đơn / Mã giao dịch (ví dụ: PM1126071003050676). Nếu có nhiều hóa đơn thì ghép các giá trị lại bằng ký tự ',' (ví dụ: 00000515,00000516). Nếu không có ghi null",
    "ngay_hoa_don": "Ngày phát hành / lập hoá đơn, định dạng DD/MM/YYYY. Nếu có nhiều hóa đơn thì ghép lại bằng ',' (ví dụ: 01/08/2026,08/08/2026). Nếu không có ghi null",
    "thoi_gian_xu_ly": "Thời gian xử lý đầy đủ cả giờ phút giây (ví dụ: 10/07/2026 16:59:52 hoặc 10/07/2026 16:59). Nếu không có ghi null",
    "ben_a": "Tên tài khoản trích nợ / Bên chuyển / Bên A (ví dụ: CTY TNHH KDTM HTC VIET NAM). Nếu không có ghi null",
    "so_tien_trich_no": số tiền trích nợ dạng number integer (ví dụ: 108000000),
    "ben_b": "Tên người hưởng / Bên nhận / Bên B (ví dụ: TTNNL CO., LTD). Nếu không có ghi null",
    "ten_nguoi_ban": "Tên công ty/cá nhân BÁN hàng (Seller) hoặc Bên A. Lấy tên đầy đủ, nếu không rõ ghi null",
    "ma_so_thue_nguoi_ban": "MST người bán. Nếu không có ghi null",
    "ten_nguoi_mua": "Tên công ty/cá nhân MUA hàng (Buyer) hoặc Bên B. Lấy tên đầy đủ, nếu không rõ ghi null",
    "ma_so_thue_nguoi_mua": "MST người mua. Nếu không có ghi null",
    "ten_nha_cung_cap": "Giống ten_nguoi_ban - để tương thích ngược",
    "ma_so_thue": "MST nhà cung cấp (người bán), nếu không có ghi null",
    "noi_dung": "Mô tả / nội dung thanh toán / Remarks",
    "co_vat": true hoặc false,
    "danh_sach_hang_hoa": [
        {
            "ten": "tên hàng hoá/dịch vụ",
            "so_luong": số lượng (number),
            "don_gia": đơn giá (number),
            "thanh_tien": thành tiền (number)
        }
    ],
    "tong_tien_truoc_thue": "Tổng tiền trước thuế (Nếu có nhiều hóa đơn thì ghép bằng ',', VD: 10000000,20000000). Có thể trả về chuỗi hoặc số.",
    "thue_suat_vat": thuế suất % (number, ví dụ 10, 8, 5. null nếu không có VAT),
    "tien_vat": "Tiền VAT (Nếu có nhiều hóa đơn thì ghép bằng ',', VD: 1000000,2000000). Có thể trả về chuỗi hoặc số.",
    "tong_thanh_toan": "Tổng thanh toán (Nếu có nhiều hóa đơn thì ghép bằng ',', VD: 11000000,22000000). Có thể trả về chuỗi hoặc số.",
    "loai_tien": "VND" hoặc loại tiền khác nếu có,
    "ghi_chu": "ghi chú thêm nếu có"
}

CHÚ Ý:
- Tất cả giá trị tiền (so_tien_trich_no) phải là NUMBER nguyên sạch (không phân cách hàng nghìn).
- Với hoá đơn, nếu có nhiều hoá đơn, các trường tiền (tong_tien_truoc_thue, tien_vat, tong_thanh_toan) NÊN trả về dạng chuỗi nối bằng dấu phẩy (ví dụ: "10000000,20000000").
- Nếu chỉ có 1 hoá đơn, có thể trả về kiểu number.
- Nếu không đọc được rõ, ghi giá trị gần nhất có thể và ghi chú trong trường ghi_chu
- TRONG MỌI TRƯỜNG HỢP: TUYỆT ĐỐI CHỈ TRẢ VỀ JSON, không kèm bất kỳ văn bản giải thích nào khác.
"""


CONTRACT_EXTRACTION_PROMPT = """Bạn là chuyên gia phân tích hợp đồng kinh tế, biên bản nghiệm thu và phụ lục hợp đồng Việt Nam. Hãy phân tích tài liệu và trích xuất thông tin theo đúng yêu cầu sau.

LOẠI CHỨNG TỪ: {doc_type}

QUAN TRỌNG:
- Số hợp đồng / Số BBNT / Số phụ lục: thường ở đầu tài liệu, dạng "Số: .../..." hoặc "Contract No.: ..."
- ⚠️ MST A / MST B (TUYỆT ĐỐI KHÔNG ĐƯỢC ĐẢO NGƯỢC):
  + MST A = Mã số thuế của BÊN A = bên cung cấp / bên bán / bên A trong hợp đồng. Tìm trong khối thông tin "BÊN A", mục "Mã số thuế:".
  + MST B = Mã số thuế của BÊN B = bên mua / bên thuê / bên nhận dịch vụ / bên B. Tìm trong khối thông tin "BÊN B", mục "Mã số thuế:".
  + MST luôn thuộc về đúng bên đứng ngay cạnh/dưới tên bên đó. Không gán MST bên A cho bên B và ngược lại. Nếu không chắc 1 bên, để null, không hoán đổi.
- Số lần thanh toán: quét kỹ mục "ĐIỀU KHOẢN THANH TOÁN" hoặc "PHƯƠNG THỨC THANH TOÁN" hoặc "Khoản thanh toán". Ví dụ: "Thanh toán 2 lần" → so_lan_tt = 2
- Chi tiết từng lần TT: mô tả từng đợt, ví dụ "Lần 1: 70% (tức 5.000.000đ) sau khi ký hợp đồng; Lần 2: 30% (tức 3.000.000đ) sau nghiệm thu"
- Tổng giá trị net: giá trị hợp đồng/BBNT SAU KHI TRỪ THUẾ (chưa có VAT)
- Tổng giá trị gross: giá trị TOÀN BỘ bao gồm cả thuế VAT
- VAT chứng từ: số/ký hiệu chứng từ liên quan đến thuế VAT (nếu có)
- PIT chứng từ: số/ký hiệu chứng từ liên quan đến thuế TNCN/PIT (nếu có)

Trả về JSON theo ĐÚNG format sau (không thêm markdown code block):
{
    "so_hop_dong": "Số hợp đồng. Luôn đọc tất cả nếu có nhiều, và ngăn cách bằng dấu phẩy ',' (ví dụ: 123/HĐ,456/HĐ). null nếu không có",
    "so_bbnt": "Số biên bản nghiệm thu. Luôn đọc tất cả nếu có nhiều, và ngăn cách bằng dấu phẩy ',' (ví dụ: 01/BBNT,02/BBNT). null nếu không có",
    "so_plhd": "Số phụ lục hợp đồng. Luôn đọc tất cả nếu có nhiều, và ngăn cách bằng dấu phẩy ',' (ví dụ: PL01,PL02). null nếu không có",
    "mst_a": "MST bên A / bên cung cấp / bên bán. null nếu không tìm thấy",
    "mst_b": "MST bên B / bên mua / bên thuê / bên nhận dịch vụ. null nếu không tìm thấy",
    "so_lan_tt": số lần thanh toán (integer, ví dụ 1, 2, 3. null nếu không rõ),
    "gia_tri_tt": "Chỉ lấy số tiền của các lần thanh toán, ngăn cách bởi ký tự ',' (Ví dụ: 10000000,20000000). Không kèm chữ.",
    "tong_gia_tri_net": giá trị hợp đồng sau khi trừ thuế (number, VD: 10000000. null nếu không có),
    "tong_gia_tri_gross": giá trị hợp đồng bao gồm cả thuế (number. null nếu không có),
    "vat_chung_tu": "Số tiền thuế Giá trị gia tăng trong hợp đồng/BBNT/PLHĐ (phần chênh lệch giữa gross và net). Trả về số tiền hoặc chuỗi, null nếu không có",
    "pit_chung_tu": "Số tiền thuế TNCN (PIT) hoặc Số/ký hiệu chứng từ PIT/TNCN. null nếu không có",
    "loai_tien": "VND hoặc loại tiền khác nếu có",
    "ghi_chu": "ghi chú thêm nếu có"
}

CHÚ Ý:
- Tất cả giá trị tiền phải là NUMBER nguyên sạch (không có dấu phân cách hàng nghìn, VD: 10000000 thay vì 10.000.000)
- TUYỆT ĐỐI CHỈ TRẢ VỀ JSON, không kèm bất kỳ văn bản giải thích nào khác.
- Nếu tài liệu dài, hãy ưu tiên quét các mục: tiêu đề, thông tin các bên, điều khoản thanh toán, giá trị hợp đồng.
"""


class AIExtractor:
    """Sử dụng OpenAI/DeepSeek/Gemini để bóc tách dữ liệu từ hoá đơn và chứng từ hợp đồng."""

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
            if "gpt-4" in self.model.lower() or "gpt-3.5" in self.model.lower():
                api_kwargs["response_format"] = {"type": "json_object"}

            max_retries = 5
            last_error = None

            for attempt in range(max_retries):
                try:
                    response = self.client.chat.completions.create(**api_kwargs)
                    raw_text = response.choices[0].message.content.strip()
                    result = self._parse_response(raw_text)
                    result["_raw_response"] = raw_text
                    result["_filename"] = filename
                    return result
                except Exception as e:
                    last_error = e
                    error_str = str(e).lower()
                    is_quota = "429" in error_str or "quota" in error_str or "rate limit" in error_str
                    is_overload = "503" in error_str or "502" in error_str or "high demand" in error_str
                    if is_quota:
                        # 429 = giới hạn theo phút (RPM/TPM), KHÔNG phải hết tiền/credit.
                        # Cần đợi lâu hơn để qua cửa sổ 1 phút: 5s, 10s, 20s, 40s, 60s (cap).
                        if attempt < max_retries - 1:
                            time.sleep(min(60, 5 * (2 ** attempt)))
                            continue
                        break
                    elif is_overload:
                        time.sleep(2 ** attempt)  # Đợi 1s, 2s, 4s...
                        continue
                    else:
                        break # Dừng nếu là lỗi khác (VD: sai API key)

            # Nếu thử lại nhiều lần vẫn thất bại
            raise last_error

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

    def extract_contract(self, content_blocks: List[Dict[str, Any]], filename: str = "", doc_type: str = "Hợp đồng") -> Dict[str, Any]:
        """
        Bóc tách dữ liệu từ hợp đồng, BBNT hoặc phụ lục hợp đồng.

        Args:
            content_blocks: List of {"type": "image"|"text", "content": ...}
            filename: Tên file gốc (để context)
            doc_type: "Hợp đồng" | "BBNT" | "Phụ lục hợp đồng"

        Returns:
            Dict chứa dữ liệu chứng từ đã trích xuất
        """
        prompt = CONTRACT_EXTRACTION_PROMPT.replace("{doc_type}", doc_type)
        messages = self._build_messages_with_prompt(content_blocks, filename, prompt)

        try:
            api_kwargs = {
                "model": self.model,
                "messages": messages,
                "max_tokens": 8192,
                "temperature": 0.1,
            }
            if "gpt-4" in self.model.lower() or "gpt-3.5" in self.model.lower():
                api_kwargs["response_format"] = {"type": "json_object"}

            max_retries = 5
            last_error = None

            for attempt in range(max_retries):
                try:
                    response = self.client.chat.completions.create(**api_kwargs)
                    raw_text = response.choices[0].message.content.strip()
                    result = self._parse_response(raw_text)
                    result["_raw_response"] = raw_text
                    result["_filename"] = filename
                    result["_doc_type"] = doc_type
                    return result
                except Exception as e:
                    last_error = e
                    error_str = str(e).lower()
                    is_quota = "429" in error_str or "quota" in error_str or "rate limit" in error_str
                    is_overload = "503" in error_str or "502" in error_str or "high demand" in error_str
                    if is_quota:
                        if attempt < max_retries - 1:
                            time.sleep(min(60, 5 * (2 ** attempt)))
                            continue
                        break
                    elif is_overload:
                        time.sleep(2 ** attempt)
                        continue
                    else:
                        break

            raise last_error

        except Exception as e:
            return {
                "error": str(e),
                "_filename": filename,
                "_doc_type": doc_type,
                "so_hop_dong": None,
                "so_bbnt": None,
                "so_plhd": None,
                "mst_a": None,
                "mst_b": None,
                "so_lan_tt": None,
                "gia_tri_tt": None,
                "tong_gia_tri_net": None,
                "tong_gia_tri_gross": None,
                "vat_chung_tu": None,
                "pit_chung_tu": None,
                "loai_tien": "VND",
                "ghi_chu": f"Lỗi: {str(e)}"
            }

    def extract_doc_numbers(self, content_blocks: List[Dict[str, Any]], filename: str = "", doc_type: str = "Hợp đồng") -> Dict[str, Any]:
        """Bóc NHANH mã số chứng từ từ TRANG ĐẦU (số HĐ / số BBNT / số PLHĐ).
        Prompt tối giản, chỉ trả về 3 trường số để tiết kiệm token và tránh nhầm lẫn."""
        prompt = (
            "Bạn là chuyên gia đọc chứng từ Việt Nam. Đây là TRANG ĐẦU của một tài liệu loại: "
            f"{doc_type}.\n"
            "Chỉ trích xuất MÃ SỐ / SỐ HIỆU của chứng từ (thường ở đầu tài liệu, dạng 'Số: .../...').\n"
            "Trả về JSON ĐÚNG format sau (không markdown, không giải thích):\n"
            "{\n"
            '  "so_hop_dong": "Số hợp đồng nếu tài liệu là hợp đồng, nhiều số ngăn cách bằng \',\'. null nếu không có",\n'
            '  "so_bbnt": "Số biên bản nghiệm thu nếu là BBNT, nhiều số ngăn cách bằng \',\'. null nếu không có",\n'
            '  "so_plhd": "Số phụ lục hợp đồng nếu là phụ lục, nhiều số ngăn cách bằng \',\'. null nếu không có"\n'
            "}\n"
            "CHỈ điền số đúng với loại tài liệu đang đọc, các trường còn lại để null. TUYỆT ĐỐI chỉ trả về JSON."
        )
        messages = self._build_messages_with_prompt(content_blocks, filename, prompt)
        try:
            api_kwargs = {
                "model": self.model,
                "messages": messages,
                "max_tokens": 512,
                "temperature": 0.0,
            }
            if "gpt-4" in self.model.lower() or "gpt-3.5" in self.model.lower():
                api_kwargs["response_format"] = {"type": "json_object"}

            max_retries = 5
            last_error = None
            for attempt in range(max_retries):
                try:
                    response = self.client.chat.completions.create(**api_kwargs)
                    raw_text = response.choices[0].message.content.strip()
                    return self._parse_response(raw_text)
                except Exception as e:
                    last_error = e
                    error_str = str(e).lower()
                    is_quota = "429" in error_str or "quota" in error_str or "rate limit" in error_str
                    is_overload = "503" in error_str or "502" in error_str or "high demand" in error_str
                    if is_quota:
                        if attempt < max_retries - 1:
                            time.sleep(min(60, 5 * (2 ** attempt)))
                            continue
                        break
                    elif is_overload:
                        time.sleep(2 ** attempt)
                        continue
                    else:
                        break
            raise last_error
        except Exception as e:
            return {"error": str(e), "so_hop_dong": None, "so_bbnt": None, "so_plhd": None}

    def _build_messages(self, content_blocks: List[Dict[str, Any]], filename: str) -> list:
        """Xây dựng messages cho API call (dùng EXTRACTION_PROMPT mặc định)."""
        return self._build_messages_with_prompt(content_blocks, filename, EXTRACTION_PROMPT)

    def _build_messages_with_prompt(self, content_blocks: List[Dict[str, Any]], filename: str, prompt: str) -> list:
        """Xây dựng messages cho API call với prompt tùy chỉnh."""
        user_content = []

        # Add text instruction
        context = f"File: {filename}\n\n" if filename else ""
        user_content.append({
            "type": "text",
            "text": f"{context}{prompt}"
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
            
            # Cắt bỏ cặp key-value bị đứt ngang (VD: "noi_dung": "Cước phí...)
            truncated = re.sub(r',\s*"[^"]*"\s*:\s*"?[^"]*$', '', truncated)
            # Xóa dấu phẩy thừa ở cuối nếu có
            truncated = re.sub(r',\s*$', '', truncated)
            # Xóa key bị đứt ở cuối (VD: "noi_dung": )
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
