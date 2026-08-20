"""
File Handler Module - Đọc và chuyển đổi file đa định dạng
Hỗ trợ: PDF, ảnh (JPG/PNG/BMP/TIFF), Word (.docx), Excel (.xlsx/.xls)
"""
import base64
import io
import os
from pathlib import Path
from typing import List, Dict, Any

import fitz  # PyMuPDF
from PIL import Image
from docx import Document
import openpyxl


class FileHandler:
    """Xử lý đọc file đa định dạng, chuyển thành dạng phù hợp cho AI."""

    SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"}
    SUPPORTED_DOC_EXTS = {".pdf", ".docx", ".xlsx", ".xls"}

    @staticmethod
    def get_file_type(filepath: str) -> str:
        ext = Path(filepath).suffix.lower()
        if ext in FileHandler.SUPPORTED_IMAGE_EXTS:
            return "image"
        elif ext == ".pdf":
            return "pdf"
        elif ext == ".docx":
            return "word"
        elif ext in {".xlsx", ".xls"}:
            return "excel"
        else:
            raise ValueError(f"Unsupported file type: {ext}")

    @staticmethod
    def image_to_base64(filepath: str, max_size: int = 2000) -> str:
        """Chuyển ảnh sang base64, resize nếu quá lớn."""
        with Image.open(filepath) as img:
            # Convert to RGB if needed
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")

            # Resize if too large
            w, h = img.size
            if max(w, h) > max_size:
                ratio = max_size / max(w, h)
                img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=85)
            return base64.b64encode(buffer.getvalue()).decode("utf-8")

    @staticmethod
    def pdf_to_images(filepath: str, max_size: int = 2000, max_pages: int = None) -> List[str]:
        """Chuyển từng trang PDF thành ảnh base64. max_pages: giới hạn số trang đầu (None = tất cả)."""
        images = []
        doc = fitz.open(filepath)
        total_pages = len(doc)
        if max_pages is not None:
            total_pages = min(total_pages, max_pages)
        for page_num in range(total_pages):
            page = doc[page_num]
            # Render at 200 DPI for good quality
            mat = fitz.Matrix(200 / 72, 200 / 72)
            pix = page.get_pixmap(matrix=mat)

            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

            # Resize if needed
            w, h = img.size
            if max(w, h) > max_size:
                ratio = max_size / max(w, h)
                img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=85)
            images.append(base64.b64encode(buffer.getvalue()).decode("utf-8"))

        doc.close()
        return images

    @staticmethod
    def read_word(filepath: str) -> str:
        """Đọc nội dung file Word (.docx) thành text."""
        doc = Document(filepath)
        lines = []

        for para in doc.paragraphs:
            if para.text.strip():
                lines.append(para.text.strip())

        # Đọc tables
        for table in doc.tables:
            for row in table.rows:
                row_data = [cell.text.strip() for cell in row.cells]
                lines.append(" | ".join(row_data))

        return "\n".join(lines)

    @staticmethod
    def read_excel(filepath: str) -> str:
        """Đọc nội dung file Excel thành text."""
        wb = openpyxl.load_workbook(filepath, data_only=True)
        lines = []

        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            lines.append(f"=== Sheet: {sheet_name} ===")
            for row in ws.iter_rows(values_only=True):
                row_data = [str(cell) if cell is not None else "" for cell in row]
                if any(r.strip() for r in row_data):
                    lines.append(" | ".join(row_data))

        wb.close()
        return "\n".join(lines)

    @staticmethod
    def process_file(filepath: str, first_page_only: bool = False) -> List[Dict[str, Any]]:
        """
        Xử lý file và trả về danh sách content blocks.

        Args:
            first_page_only: Nếu True, chỉ đọc TRANG ĐẦU (PDF) / phần đầu (Word/Excel).
                Dùng cho bước bóc nhanh mã số chứng từ (số HĐ/BBNT/PLHĐ).

        Returns:
            List of dicts: [{"type": "image"|"text", "content": base64_string|text_string}]
        """
        file_type = FileHandler.get_file_type(filepath)
        FIRST_PAGE_TEXT_LIMIT = 4000  # ký tự đầu cho file text khi chỉ đọc trang đầu

        if file_type == "image":
            b64 = FileHandler.image_to_base64(filepath)
            return [{"type": "image", "content": b64, "mime_type": "image/jpeg"}]

        elif file_type == "pdf":
            images = FileHandler.pdf_to_images(filepath, max_pages=1 if first_page_only else None)
            return [{"type": "image", "content": img, "mime_type": "image/jpeg"} for img in images]

        elif file_type == "word":
            text = FileHandler.read_word(filepath)
            if first_page_only:
                text = text[:FIRST_PAGE_TEXT_LIMIT]
            return [{"type": "text", "content": text}]

        elif file_type == "excel":
            text = FileHandler.read_excel(filepath)
            if first_page_only:
                text = text[:FIRST_PAGE_TEXT_LIMIT]
            return [{"type": "text", "content": text}]

        else:
            raise ValueError(f"Cannot process file type: {file_type}")
