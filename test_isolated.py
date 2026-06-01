import json
import os
from extractors.lark_client import LarkClient
from extractors.file_handler import FileHandler
from extractors.ai_extractor import AIExtractor

def main():
    print("Loading config...")
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
    attachment_field = config.get("lark_attachment_field", "Tệp đính kèm")

    print("Fetching fields...")
    fields = lark.list_fields(app_token, table_id)
    att_field_id = None
    for f in fields:
        if f["field_name"] == attachment_field:
            att_field_id = f["field_id"]
            break
            
    if not att_field_id:
        print(f"Không tìm thấy cột {attachment_field}")
        return

    print("Fetching records...")
    records, next_token, total = lark.list_records(
        app_token=app_token,
        table_id=table_id,
        view_id=view_id,
        page_size=10,
    )

    with_att = [r for r in records if r.get("fields", {}).get(attachment_field)]
    if not with_att:
        print("No attachment records.")
        return

    rec = with_att[0]
    rid = rec["record_id"]
    atts = rec["fields"][attachment_field]
    file_token = atts[0]["file_token"]
    file_name = atts[0]["name"]
    print(f"Record {rid}: found attachment '{file_name}' ({file_token})")

    print("Downloading attachment...")
    try:
        tmp_path = lark.save_attachment_temp(
            file_token, file_name,
            app_token, table_id, att_field_id, rid
        )
        print(f"Downloaded to {tmp_path}")
    except Exception as e:
        print(f"Download failed: {e}")
        return

    print("Processing file...")
    try:
        content_blocks = FileHandler.process_file(tmp_path)
        print(f"Processed into {len(content_blocks)} content blocks.")
    except Exception as e:
        print(f"File processing failed: {e}")
        return

    print("AI Extraction...")
    extractor = AIExtractor(
        api_key=config.get("openai_api_key", ""),
        model=config.get("openai_model", "gpt-4o")
    )
    try:
        res = extractor.extract_invoice(content_blocks, file_name)
        print("AI Extracted:", list(res.keys()))
    except Exception as e:
        print(f"AI extraction failed: {e}")

if __name__ == "__main__":
    main()
