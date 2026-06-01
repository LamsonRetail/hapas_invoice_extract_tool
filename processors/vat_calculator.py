"""
VAT Calculator Module - Xử lý tính toán và validate VAT
"""
from typing import Dict, Any, Optional


class VATCalculator:
    """Tính toán và validate dữ liệu VAT từ kết quả AI."""

    @staticmethod
    def process(data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Xử lý và validate dữ liệu VAT.

        Logic:
        - Nếu hoá đơn CÓ VAT:
            + Kiểm tra: tổng_trước_thuế + tiền_VAT = tổng_thanh_toán
            + Nếu thiếu tiền_VAT → tính ngược từ tổng và thuế suất
            + Nếu thiếu tổng_trước_thuế → tính ngược từ tổng và VAT
        - Nếu hoá đơn KHÔNG CÓ VAT:
            + Chỉ lấy tổng_thanh_toán
            + Set tiền_trước_thuế = tổng_thanh_toán, VAT = 0

        Returns:
            Dict với thêm các trường computed và validation status
        """
        result = data.copy()
        warnings = []

        co_vat = data.get("co_vat", False)
        tong_thanh_toan = _to_number(data.get("tong_thanh_toan"))
        tong_truoc_thue = _to_number(data.get("tong_tien_truoc_thue"))
        thue_suat = _to_number(data.get("thue_suat_vat"))
        tien_vat = _to_number(data.get("tien_vat"))

        if co_vat:
            # Hoá đơn có VAT - cần tách
            if thue_suat is None:
                thue_suat = 10  # Default VAT rate in Vietnam
                warnings.append("Không tìm thấy thuế suất, mặc định 10%")

            # Case 1: Có đủ 3 số → validate
            if tong_thanh_toan and tong_truoc_thue and tien_vat:
                expected_total = tong_truoc_thue + tien_vat
                diff = abs(expected_total - tong_thanh_toan)
                if diff > 1:  # Cho phép sai số 1 đồng (làm tròn)
                    warnings.append(
                        f"Số liệu không khớp: {tong_truoc_thue:,.0f} + {tien_vat:,.0f} = {expected_total:,.0f} "
                        f"≠ tổng thanh toán {tong_thanh_toan:,.0f} (chênh {diff:,.0f})"
                    )

            # Case 2: Có tổng TT + thuế suất, thiếu các trường khác
            elif tong_thanh_toan and not tong_truoc_thue:
                rate = thue_suat / 100
                tong_truoc_thue = round(tong_thanh_toan / (1 + rate))
                tien_vat = tong_thanh_toan - tong_truoc_thue
                warnings.append("Tính ngược tiền trước thuế từ tổng thanh toán")

            # Case 3: Có tổng trước thuế + thuế suất, thiếu VAT
            elif tong_truoc_thue and not tien_vat:
                rate = thue_suat / 100
                tien_vat = round(tong_truoc_thue * rate)
                if not tong_thanh_toan:
                    tong_thanh_toan = tong_truoc_thue + tien_vat
                warnings.append("Tính tiền VAT từ tiền trước thuế và thuế suất")

            # Case 4: Có tổng trước thuế + VAT, thiếu tổng
            elif tong_truoc_thue and tien_vat and not tong_thanh_toan:
                tong_thanh_toan = tong_truoc_thue + tien_vat

        else:
            # Hoá đơn không có VAT
            tong_truoc_thue = tong_thanh_toan
            tien_vat = 0
            thue_suat = 0

        # Update result
        result["tong_tien_truoc_thue_computed"] = tong_truoc_thue
        result["thue_suat_vat_computed"] = thue_suat
        result["tien_vat_computed"] = tien_vat
        result["tong_thanh_toan_computed"] = tong_thanh_toan

        # Validation status
        if data.get("error"):
            result["trang_thai"] = "LỖI"
        elif warnings:
            result["trang_thai"] = "CẢNH BÁO"
        else:
            result["trang_thai"] = "OK"

        result["canh_bao"] = "; ".join(warnings) if warnings else ""

        return result


def _to_number(value) -> Optional[float]:
    """Convert value to number, handling various formats."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        # Remove common formatting
        cleaned = value.replace(",", "").replace(".", "").replace(" ", "")
        cleaned = cleaned.replace("VND", "").replace("đ", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None
