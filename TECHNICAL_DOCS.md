# Invoice Extractor — Tài liệu Kỹ thuật Chi tiết

> v4.1 · FastAPI + Đa Nền tảng AI (OpenAI/DeepSeek/Gemini) + Lark Bitable

---

## 1. Tổng quan kiến trúc

```
┌─────────────────────────────────────────────┐
│              Browser (Frontend)              │
│  index.html  ·  app.js  ·  style.css        │
│                                             │
│  Tab 1: Nạp File  │ Tab 2: Larkbase │ Tab 3: Cài đặt │
└────────────────────────┬────────────────────┘
                         │ HTTP / REST API
┌────────────────────────▼────────────────────┐
│              FastAPI Server (app.py)         │
│                                             │
│  /api/upload          /api/lark/records     │
│  /api/export          /api/lark/process     │
│  /api/settings        /api/lark/setup-cols  │
└──────┬────────────────────────┬─────────────┘
       │                        │
┌──────▼──────┐       ┌─────────▼──────────┐
│  OpenAI API │       │   Lark Open API    │
│  GPT-4o     │       │   (Bitable + Drive)│
└─────────────┘       └────────────────────┘
```

**Stack công nghệ:**
| Layer | Công nghệ |
|---|---|
| Backend | Python 3.10+, FastAPI, Uvicorn |
| AI | Đa mô hình: OpenAI (GPT-4o), Google Gemini (2.5/2.0), DeepSeek (V4/V3/R1), OpenRouter, Custom |
| Storage | Lark Bitable (cloud database) |
| Frontend | Vanilla HTML/CSS/JS |
| File parsing | PyMuPDF (PDF), Pillow (ảnh), python-docx, openpyxl |
| Export | openpyxl (Excel) |

---

## 2. Cấu trúc thư mục

```
invoice-extractor/
├── app.py                      # FastAPI server chính
├── config.json                 # Cấu hình lưu sau khi user save settings
├── static/
│   ├── index.html              # Giao diện người dùng
│   ├── app.js                  # Toàn bộ logic frontend
│   └── style.css               # Giao diện dark-mode
├── extractors/
│   ├── ai_extractor.py         # Gọi GPT-4o để bóc tách hoá đơn
│   ├── file_handler.py         # Đọc file (PDF/ảnh/Word/Excel)
│   └── lark_client.py          # Client giao tiếp Lark Open API
├── processors/
│   └── vat_calculator.py       # Tính toán và xác nhận VAT
└── exporters/
    └── excel_export.py         # Xuất kết quả ra Excel
```

---

## 3. Luồng xử lý — Tab Nạp File (Upload)

```
User kéo thả file
      │
      ▼
[Frontend] POST /api/upload (multipart/form-data)
      │
      ▼
[app.py] Lưu file tạm → gọi FileHandler.process_file()
      │
      ├── PDF  → PyMuPDF render từng trang thành ảnh JPEG base64
      ├── JPG/PNG → Chuyển thành JPEG base64
      ├── DOCX → python-docx trích xuất text thuần
      └── XLSX → openpyxl đọc dữ liệu dạng text
      │
      ▼
[AIExtractor.extract_invoice()] — Gọi GPT-4o
      │
      ▼
[VATCalculator.process()] — Kiểm tra và tính VAT
      │
      ▼
Return JSON kết quả → Render bảng trên frontend
```

### 3.1 FileHandler — Đọc file

`extractors/file_handler.py` trả về `List[Dict]` gọi là **content_blocks**, mỗi block có dạng:

```python
{"type": "image", "content": "<base64 string>"}  # cho PDF/ảnh
{"type": "text",  "content": "<nội dung text>"}   # cho Word/Excel
```

PDF nhiều trang → mỗi trang render thành 1 block ảnh, tất cả gộp lại gửi GPT cùng lúc.

### 3.2 AIExtractor — Bóc tách bằng GPT-4o

**Prompt engineering** (`ai_extractor.py`):
- Gửi tất cả content_blocks lên GPT-4o với prompt hướng dẫn rõ ràng
- Prompt yêu cầu trả về JSON chuẩn gồm: `so_hoa_don`, `ngay_hoa_don`, `ten_nha_cung_cap`, `co_vat`, `tong_tien_truoc_thue`, `tien_vat`, `tong_thanh_toan`...
- **[Mới cập nhật]** Ép buộc AI luôn trả về định dạng JSON thuần túy trong mọi trường hợp (kể cả ảnh mờ, file không đọc được, sai định dạng hóa đơn). Bắt buộc thiết lập các trường là `null` và điền lý do lỗi vào `ghi_chu`.
- Temperature = 0.1 (ưu tiên chính xác). Bắt buộc `max_tokens=8192` để hỗ trợ hóa đơn siêu dài.
- **[Mới cập nhật]** Gọi API với tham số `response_format={"type": "json_object"}` (cho mô hình hỗ trợ như GPT) để chặn lỗi trả về văn bản tự do.

**Xử lý response:**
- Strip thẻ `<think>...</think>` (để tương thích khi dùng mô hình DeepSeek Reasoner).
- Strip markdown code block nếu AI trả về đoạn văn bao ngoài ```` ```json ````.
- Parse JSON trực tiếp, fallback tìm kiếm bằng regex khối `{...}` nếu dữ liệu bị lỗi nhẹ.
- **[Mới cập nhật]** Fallback tự động vá lỗi cắt cụt chuỗi (thêm ngoặc đóng `}`, `]}`) nếu kết quả JSON trả về dở dang do vượt token (dù hiếm khi xảy ra).

### 3.3 VATCalculator — Xác nhận VAT

**3 trường hợp:**

| Trường hợp | Logic |
|---|---|
| Có VAT, số liệu đầy đủ | Dùng `tong_tien_truoc_thue` + `tien_vat` trực tiếp từ AI |
| Có VAT, thiếu một trong hai | Tính ngược: `truoc_thue = tong / (1 + vat%)`, hoặc `vat = tong - truoc_thue` |
| Không có VAT | `tong_thanh_toan` = toàn bộ số tiền |

Kiểm tra chéo (cross-check): nếu `tong_tien_truoc_thue + tien_vat ≠ tong_thanh_toan` → set trạng thái `CẢNH BÁO`.

---

## 4. Luồng xử lý — Tab Larkbase (Phần quan trọng)

### 4.1 Sơ đồ tổng thể Lark Integration

```
[Bảng Lark Bitable]
  Cột đính kèm: file hoá đơn (PDF/ảnh)
  Cột nội dung: mô tả giao dịch
  ↓
[Tool đọc records] → Download file → AI bóc tách → Ghi kết quả ngược lại
  ↑                                                       ↓
  └──────────────────────────────────────────────────────┘
    8 cột kết quả: AI_Có_VAT, AI_Tiền_Trước_Thuế,
                   AI_Tiền_VAT, AI_Trạng_Thái,
                   AI_Ngày_HĐ, AI_Số_HĐ,
                   AI_Người_Bán, AI_Người_Mua
```

### 4.2 Authentication — Lark Tenant Access Token

**File:** `extractors/lark_client.py` — `_get_token()`

Lark sử dụng **Tenant Access Token** (OAuth 2.0 client credentials):

```
POST https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal
Body: { "app_id": "...", "app_secret": "..." }
Response: { "tenant_access_token": "t-xxxx", "expire": 7200 }
```

- Token được **cache trong RAM** tới khi còn 60 giây hết hạn
- Mọi request Lark API đều kèm header: `Authorization: Bearer <token>`
- **Lark vs Feishu:** detect từ URL (`feishu.cn` vs `larksuite.com`) để chọn đúng base_url API endpoint

### 4.3 Parse URL Bảng Lark

**File:** `app.py` — `parse_lark_url()`

Lark Bitable URL có dạng:
```
https://company.larksuite.com/base/APP_TOKEN?table=TABLE_ID&view=VIEW_ID
```

Tool parse để lấy:
- `app_token`: token định danh Bitable app
- `table_id`: ID của sheet/bảng cụ thể
- `view_id`: ID của view (tuỳ chọn, ảnh hưởng thứ tự và bộ lọc hiển thị)

**Wiki embed:** Nếu URL dạng `/wiki/WIKI_TOKEN`, tool gọi thêm API `wiki/v2/spaces/get_node` để lấy `obj_token` thực sự của Bitable.

### 4.4 Lấy danh sách Records (Phân trang)

**Endpoint:** `GET /api/lark/records` → `lark_client.list_records()`

Lark API dùng **cursor-based pagination**:

```
POST /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search
Body: {
    "page_size": 100,         # max 500 per Lark API
    "page_token": "...",      # cursor từ response trước (null nếu đầu tiên)
    "view_id": "...",
    "automatic_fields": true  # trả thêm record_id
}
Response: {
    "items": [...],
    "page_token": "...",      # cursor trang tiếp
    "has_more": true/false,
    "total": 1200
}
```

**Frontend — Lazy loading:**
- Lần đầu load 100 records → hiển thị ngay
- Khi user bấm **"Tải thêm"** hoặc nhập range vượt số dòng hiện có → tự động gọi thêm
- Hàm `loadUntilRow(targetRow)` fetch liên tục (mỗi lần 100 records) cho đến khi đủ

**Xử lý dữ liệu trả về mỗi record:**
```python
{
    "record_id": "recABCDEF",
    "fields": {
        "Tệp đính kèm": [{"file_token": "xxx", "name": "hoadon.pdf", ...}],
        "Text": "Thanh toán tiền điện tháng 10",
        "AI_Có_VAT": "Có",       # đã xử lý trước đó
        "AI_Trạng_Thái": "OK",   # ...
    }
}
```

Tool map sang object gọn hơn để frontend dùng:
```javascript
{
    record_id, noi_dung, attachments, has_attachments,
    co_vat, tien_truoc_thue, tien_vat, trang_thai, is_processed
}
```

### 4.5 Tải file đính kèm từ Lark Drive

**File:** `lark_client.py` — `download_attachment()`

File đính kèm trong Bitable được lưu trên **Lark Drive**, truy cập bằng `file_token`:

```
GET /open-apis/drive/v1/medias/{file_token}/download
Headers: Authorization: Bearer <token>
Params: extra={"bitablePerm": {"tableId": ..., "attachments": {field_id: {record_id: [file_token]}}}}
```

> **Quan trọng:** Tham số `extra` mang thông tin Bitable permission (`bitablePerm`) là bắt buộc nếu bot không có quyền Drive rộng. Nếu thiếu, API trả 403.

File được lưu vào **temp file** (`tempfile.NamedTemporaryFile`) với đúng extension (`.pdf`, `.jpg`...) để FileHandler nhận dạng đúng loại.

### 4.6 Cập nhật kết quả ngược lại Lark

**Endpoint Lark:** `PUT /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}`

```python
update_fields = {
    "AI_Có_VAT": "Có",
    "AI_Tiền_Trước_Thuế": 9090909,   # số, không format
    "AI_Tiền_VAT": 909090,
    "AI_Trạng_Thái": "OK | Ghi chú thêm",
    "AI_Ngày_HĐ": "15/10/2025",
    "AI_Số_HĐ": "AA/22K-0001234",
    "AI_Người_Bán": "Công ty Cổ phần ABC",
    "AI_Người_Mua": "Công ty TNHH XYZ"
}
```

**Quy tắc ghi:**
- Cột Number → gửi `float/int` thuần (không dấu chấm, không đơn vị)
- Cột Text → gửi `str` (cắt tối đa 500 ký tự)
- Chỉ ghi các cột đã tồn tại / đã tạo sẵn — không tự thêm cột lạ

### 4.7 Tạo cột kết quả tự động

**Endpoint:** `POST /api/lark/setup-columns` (nút "Tự động tạo cột")

```
POST /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields
Body: {"field_name": "AI_Có_VAT", "type": 1}
```

**Lark field type:**
| type | Loại cột |
|---|---|
| 1 | Text |
| 2 | Number |
| 7 | Checkbox |

8 cột được tạo:
- `AI_Có_VAT` → Text (ghi "Có"/"Không")
- `AI_Tiền_Trước_Thuế` → Number
- `AI_Tiền_VAT` → Number
- `AI_Trạng_Thái` → Text
- `AI_Ngày_HĐ` → Text
- `AI_Số_HĐ` → Text
- `AI_Người_Bán` → Text
- `AI_Người_Mua` → Text

Logic: kiểm tra `list_fields` trước, chỉ tạo những cột **chưa tồn tại**.

### 4.8 Xử lý batch bất đồng bộ (Background Task)

**Luồng:**
```
POST /api/lark/process  →  tạo task_id  →  BackgroundTask bắt đầu chạy
      │
      ▼ (ngay lập tức trả về)
{ "task_id": "uuid" }
      │
Frontend poll mỗi 2 giây:
GET /api/lark/status/{task_id}
→ { "status": "processing"/"done", "processed": 5, "total": 20, ... }
```

**Background task** (`process_lark_batch`):
1. Load config (API keys, field names)
2. Fetch `field_id` của cột đính kèm (cần cho download permission)
3. Lặp qua từng `record_id`:
   - Fetch record
   - Download tất cả file đính kèm → temp files
   - Gọi FileHandler → content_blocks
   - Gọi AI → raw_result
   - Tính VAT → processed
   - Ghi kết quả ngược Lark
   - Xóa temp files
4. Set `status = "done"` khi xong

Dùng `asyncio.get_event_loop().run_in_executor(None, ...)` để chạy các tác vụ blocking (requests, file I/O) trong thread pool mà không block event loop.

---

## 5. Selection Range (Chọn dòng từ-đến)

**Vấn đề giải quyết:** Bảng có thể có hàng nghìn dòng, không thể scroll toàn bộ để tick từng checkbox.

**Cơ chế:**

```
User nhập: Từ [700] → Đến [800]  → Bấm Áp dụng
                │
                ▼
applyRangeSelection() [async]
    1. Kiểm tra: filteredRecords.length < 800?
       → YES + còn nextPageToken:
          loadUntilRow(800) — fetch Lark API nhiều lần cho đến đủ 800 dòng
       → NO: tiếp tục ngay
    2. clampTo = min(filteredRecords.length, 800)
    3. Set rangeView = {from: 700, to: 800}
    4. Loop i = 699..799:
         if filteredRecords[i].has_attachments → add to selectedIds
    5. renderTable() — chỉ hiện slice 700-800, STT đúng số thực
```

**State key:**
- `allRecords[]` — tất cả records đã fetch từ Lark (tích lũy khi load thêm)
- `filteredRecords[]` — kết quả sau khi áp bộ lọc "có đính kèm" / "chưa xử lý"
- `selectedIds Set` — record_id đã được chọn để xử lý
- `rangeView {from, to}` — điều khiển slice hiển thị trong bảng
- `nextPageToken` — cursor để fetch trang tiếp theo từ Lark

---

## 6. Cài đặt & Config

**File:** `config.json` (tự sinh, không commit)

```json
{
    "lark_app_id": "cli_xxx",
    "lark_app_secret": "xxx",
    "lark_base_url": "https://open.larksuite.com",
    "lark_app_token": "BxxxxxToken",
    "lark_table_id": "tblXXXX",
    "lark_view_id": "vewXXXX",
    "lark_attachment_field": "Tệp đính kèm",
    "lark_content_field": "Text",
    "lark_result_fields": {
        "co_vat": "AI_Có_VAT",
        "tien_truoc_thue": "AI_Tiền_Trước_Thuế",
        "tien_vat": "AI_Tiền_VAT",
        "trang_thai": "AI_Trạng_Thái",
        "ngay_hoa_don": "AI_Ngày_HĐ",
        "so_hoa_don": "AI_Số_HĐ",
        "ten_nguoi_ban": "AI_Người_Bán",
        "ten_nguoi_mua": "AI_Người_Mua"
    },
    "openai_api_key": "sk-proj-xxx",
    "openai_model": "gpt-4o",
    "openai_base_url": "https://api.openai.com/v1"
}
```

Tên cột kết quả là **cấu hình** — user có thể đổi tên khớp với bảng thực tế của họ.

---

## 7. Xử lý lỗi & Edge Cases

| Tình huống | Xử lý |
|---|---|
| Record không có đính kèm | Ghi "Bỏ qua - không có file" vào `AI_Trạng_Thái` |
| File không đọc được / File rác | Trả về JSON hợp lệ với mọi trường bằng `null`, ghi chú nguyên nhân vào trường `ghi_chu`. |
| GPT không parse được JSON | Tự đóng ngoặc JSON bị khuyết, regex fallback. Cấu hình sẵn `response_format=json_object` để triệt tiêu hoàn toàn rủi ro này. |
| VAT số liệu không khớp | Ghi `CẢNH BÁO | chênh lệch X VND` |
| Lark API rate limit | Request tự raise exception → ghi lỗi record đó, tiếp tục record sau |
| Token hết hạn giữa chừng | `_get_token()` tự fetch token mới (cache check) |
| URL wiki embed | Gọi thêm wiki API để lấy `obj_token` thực của Bitable |

---

## 8. Khởi chạy Local (Môi trường dev)

```bash
cd invoice-extractor
pip install -r requirements.txt
python app.py
# hoặc
uvicorn app:app --host 0.0.0.0 --port 8080 --reload
```

Truy cập: `http://localhost:8080`

## 9. Cấu hình Docker & Chuẩn bị Deploy lên Server (Production)

Vì bạn triển khai nhiều công cụ (tools) khác nhau trên cùng một server Google Cloud, cách tối ưu nhất là sử dụng **Docker Compose**. Docker Compose sẽ cho phép bạn quản lý tất cả các vùng chứa (container) một cách quy củ, phân biệt port dễ dàng và cấu hình tập trung.

File `docker-compose.yml` đã được tạo sẵn trong thư mục gốc với nội dung ánh xạ cổng `8080:8080` và mount thư mục `uploads` cũng như `config.json` ra ngoài.

### 9.1 Hướng dẫn đưa Source Code lên server Google Cloud

Bước đơn giản và an toàn nhất để đưa code này lên server GCP là thông qua việc **Nén thư mục (Zip)** và tải lên:

**Bước 1: Nén code tại máy tính của bạn**
- Tại máy tính Windows, bạn chuột phải vào thư mục `invoice-extractor` hiện tại và chọn nén (Zip) lại thành một file: `invoice-extractor.zip`.
*(Lưu ý: Nếu có thư mục `__pycache__` hay môi trường ảo `venv` thì có thể xóa đi trước khi nén để file nhẹ hơn).*

**Bước 2: Upload lên Google Cloud VM**
- Mở **Google Cloud Console**, vào phần **Compute Engine** -> **VM instances**.
- Nhấn vào nút **SSH** ở dòng tương ứng với server bạn muốn deploy (ví dụ: `digital-transformation-hosting`).
- Trên cửa sổ terminal SSH web vừa mở lên, nhấn vào biểu tượng hình Bánh răng / Quản lý tệp (Settings/Upload) ở góc trên bên phải màn hình và chọn **"Upload file"**.
- Chọn file `invoice-extractor.zip` tải lên. File mặc định sẽ được lưu ở thư mục gốc của user đang dùng (ví dụ `/home/user/`).

**Bước 3: Giải nén code trên server**
- Cài phần mềm giải nén (nếu chưa có) và giải nén thư mục ra:
```bash
sudo apt update && sudo apt install unzip -y
unzip invoice-extractor.zip
# Tới thư mục chứa source code
cd invoice-extractor
```

### 9.2 Chạy ứng dụng bằng Docker Compose

Sau khi đã di chuyển vào trong thư mục `invoice-extractor` ở trên server, bạn chỉ cần gõ đúng 1 dòng lệnh này để khởi động hệ thống:

```bash
docker-compose up -d --build
```

*(Lưu ý: Nếu server báo lỗi `Command not found` tức là server chưa cài docker-compose, hãy cài bằng lệnh: `sudo apt install docker-compose -y` hoặc cài qua plugin của Docker)*

**Lệnh này là tối ưu nhất vì nó sẽ tự động làm mọi thứ:**
1. Build image nhanh chóng từ `Dockerfile`.
2. Dựng và cấu hình container có tên `invoice-extractor`.
3. Ánh xạ cổng `8080` của server vào ứng dụng bên trong.
4. Mount `config.json` và thư mục `uploads/` thành ổ cứng ngoài (volume). Nhờ đó, nếu bạn lỡ xóa hoặc cập nhật container, các tùy chỉnh và dữ liệu tệp đính kèm không bao giờ bị mất đi.

Lúc này, server FastAPI đã chạy hoàn hảo trên port `8080`. 
**(Đừng quên cấu hình quy tắc tường lửa (Firewall Rule) mở cổng TCP `8080` trên Google Cloud VPC cho VM này để bên ngoài truy cập được nhé!).**

### 9.3 Tính mở rộng nhiều Tools (Multi-Tools Architecture)

Vì bạn muốn chạy nhiều tools trên cùng server, phương án dùng Docker Compose cực kỳ an toàn vì từng công cụ được đóng gói độc lập:
1. Giả sử sau này bạn làm thêm "Tool Check KPIs", bạn viết code trong thư mục mới, và chỉ cần đổi port Publish trong `docker-compose.yml` của nó thành `8081:8080`.
2. Cả 2 tool sẽ chạy song song, không xung đột thư viện Python, độc lập hoàn toàn bộ nhớ đệm lẫn cấu hình.
3. Khi bạn bảo trì Tool 1 (Restart/Stop), Tool 2 vẫn hoạt động 100% bình thường.

### 9.4 Cập nhật phiên bản tự động bằng CI/CD (GitHub Actions)

Để tự động hoá 100% quá trình cập nhật mà không cần truy cập SSH nén/giải nén thủ công, dự án đã được tích hợp **GitHub Actions** (file `.github/workflows/deploy.yml`).

**Quy trình:**
1. Mỗi khi có code mới được Push lên nhánh `master` (hoặc `main`) trên GitHub.
2. GitHub Actions sẽ dùng `scp-action` chép đè code sang máy chủ Google Cloud.
3. Chạy `ssh-action` gọi lệnh `docker-compose down` và `docker-compose up -d --build`.
4. Website được tự động nâng cấp version mới mà không cần thao tác tay.

**Yêu cầu cấu hình (Một lần duy nhất trên GitHub Repo > Settings > Secrets):**
- `SSH_HOST`: Địa chỉ IP của máy chủ Google Cloud
- `SSH_USERNAME`: Tên user trên máy chủ (vd: `thienquy_work1`)
- `SSH_PRIVATE_KEY`: Khóa bí mật (Private Key rsa 4096) đã sinh ra trên máy chủ (nhớ add public key vào `~/.ssh/authorized_keys` của server).

### 9.5 Khắc phục sự cố thường gặp (Troubleshooting)

| Dấu hiệu / Lỗi | Nguyên nhân | Cách khắc phục |
|---|---|---|
| **`KeyError: 'ContainerConfig'`** khi chạy docker-compose up | Do phiên bản `docker-compose` v1.x quá cũ, bị xung đột metadata khi cố khởi tạo lại vùng chứa (recreate) đè lên vùng chứa cũ. | Thêm lệnh `docker-compose down` để xóa dứt điểm container cũ trước khi chạy lại `docker-compose up -d --build`. |
| **`ModuleNotFoundError: No module named 'requests'`** hoặc `Invalid requirement: 'r\x00e\x00...'` | Thường xảy ra do sửa file `requirements.txt` bằng lệnh `echo` trên PowerShell (Windows). PowerShell tự động lưu dưới dạng UTF-16LE, đẩy lên Linux bị lỗi ký tự null `\x00`. | Mở file `requirements.txt` bằng code editor (như VSCode/Notepad), đảm bảo định dạng file lưu ở dạng **UTF-8** thuần. |
| **`Error code: 503 ... High demand`** hoặc đứt gãy kết quả JSON | API của Gemini/OpenAI bị quá tải cục bộ, hoặc bị hết tokens giữa chừng khi trả về kết quả JSON dẫn đến cắt ngang chuỗi. | Code đã được vá: (1) Thêm thuật toán thử lại (Exponential backoff retry) tự động đợi 1s, 2s, 4s... nếu gặp mã 503/429. (2) Regex vá lỗi `ai_extractor.py` tự động cắt bỏ phần string JSON bị đứt đoạn cuối cùng. |
| Truy cập port 8080 báo `ERR_CONNECTION_REFUSED` | 1. Firewall GCP chặn port.<br>2. Ứng dụng bên trong container bị Crash (chết) ngay sau khi khởi động. | Gõ lệnh `docker logs invoice-extractor` để xem log lỗi Python chi tiết. Nếu do Firewall, vào GCP Network mở cổng TCP 8080. |
