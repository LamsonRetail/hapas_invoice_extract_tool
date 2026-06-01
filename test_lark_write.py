"""
Quick test: Kiểm tra bot có thể đọc + ghi dữ liệu vào Lark Bitable không
"""
import json
from extractors.lark_client import LarkClient

# Load config
with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

lark = LarkClient(
    app_id=config["lark_app_id"],
    app_secret=config["lark_app_secret"],
    base_url=config.get("lark_base_url", "https://open.larksuite.com"),
)

app_token = config["lark_app_token"]
table_id = config["lark_table_id"]
view_id = config.get("lark_view_id", "")
result_fields = config.get("lark_result_fields", {})

print("=" * 50)
print("TEST 1: Lấy tenant_access_token")
print("=" * 50)
try:
    token = lark._get_token()
    print(f"✅ Token OK: {token[:20]}...")
except Exception as e:
    print(f"❌ Lỗi auth: {e}")
    exit(1)

print()
print("=" * 50)
print("TEST 2: Đọc records từ Bitable")
print("=" * 50)
try:
    records, next_token, total = lark.list_records(
        app_token=app_token,
        table_id=table_id,
        view_id=view_id,
        page_size=5,
    )
    print(f"✅ Đọc được {total} records (hiển thị {len(records)} đầu tiên)")
    for rec in records[:3]:
        rid = rec.get("record_id", "?")
        fields = rec.get("fields", {})
        text = ""
        text_val = fields.get("Text", "")
        if isinstance(text_val, list):
            text = " ".join(v.get("text", str(v)) if isinstance(v, dict) else str(v) for v in text_val)
        elif isinstance(text_val, str):
            text = text_val
        print(f"   - {rid}: {text[:60]}")
except Exception as e:
    print(f"❌ Lỗi đọc records: {e}")
    import traceback; traceback.print_exc()
    exit(1)

print()
print("=" * 50)
print("TEST 3: Thử ghi vào record đầu tiên (cột AI_Trạng_Thái)")
print("=" * 50)
if records:
    first_record = records[0]
    rid = first_record["record_id"]
    
    # Ghi test vào cột trạng thái
    test_fields = {
        result_fields.get("trang_thai", "AI_Trạng_Thái"): "Test ghi từ bot - OK",
    }
    
    try:
        lark.update_record(app_token, table_id, rid, test_fields)
        print(f"✅ Ghi thành công vào record {rid}!")
        print(f"   Fields đã ghi: {test_fields}")
        print()
        print("🎉 Bot đã sẵn sàng! Bạn có thể chạy app chính bằng:")
        print("   python app.py")
    except Exception as e:
        print(f"❌ Lỗi ghi: {e}")
        import traceback; traceback.print_exc()
        print()
        print("👉 Kiểm tra:")
        print("   1. Bot có quyền 'Can edit' trên Base không?")
        print("   2. Đã publish version mới của app trên Lark Developer Console chưa?")
        print("   3. Scope 'bitable:app' đã được bật chưa?")
else:
    print("⚠️ Không có record nào để test")
