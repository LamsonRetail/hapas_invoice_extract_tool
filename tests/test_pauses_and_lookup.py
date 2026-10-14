"""
Kiểm thử cho các bản sửa: retry/timeout của AIExtractor, xử lý batch song song,
đọc cột Lookup/Formula của Lark và chặn ghi vào cột chỉ-đọc.

Chạy: python -m unittest discover -s tests -v
Không gọi mạng: Lark client, model và render PDF đều được thay bằng bản giả.
"""
import asyncio
import os
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import httpx
import openai

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
from extractors import ai_extractor  # noqa: E402
from extractors.ai_extractor import AIExtractor, _get_client, _retry_delay  # noqa: E402
from extractors.lark_client import LarkClient  # noqa: E402

_REQ = httpx.Request("POST", "http://gateway.test/v1/chat/completions")


def _status_error(cls, code, msg="error", headers=None):
    return cls(msg, response=httpx.Response(code, request=_REQ, headers=headers or {}), body=None)


class RetryDelayTest(unittest.TestCase):
    def test_rate_limit_backs_off(self):
        err = _status_error(openai.RateLimitError, 429)
        self.assertEqual(_retry_delay(err, 0), 5.0)
        self.assertEqual(_retry_delay(err, 2), 20.0)
        self.assertEqual(_retry_delay(err, 4), 60.0)

    def test_rate_limit_honours_retry_after(self):
        err = _status_error(openai.RateLimitError, 429, headers={"retry-after": "7"})
        self.assertEqual(_retry_delay(err, 0), 7.0)

    def test_daily_quota_is_not_retried(self):
        err = _status_error(openai.RateLimitError, 429,
                            "Quota exceeded: GenerateRequestsPerDayPerProjectPerModel-FreeTier")
        self.assertIsNone(_retry_delay(err, 0))

    def test_bad_request_containing_503_is_not_retried(self):
        # Bản cũ dò chuỗi "503" trong message nên retry oan lỗi 400 kiểu này.
        err = _status_error(openai.BadRequestError, 400, "prompt has 15031 tokens, limit 8192")
        self.assertIsNone(_retry_delay(err, 0))

    def test_server_errors_back_off(self):
        err = _status_error(openai.InternalServerError, 503)
        self.assertEqual(_retry_delay(err, 0), 1.0)
        self.assertEqual(_retry_delay(err, 3), 8.0)

    def test_rate_limit_wrapped_in_5xx_waits_like_rate_limit(self):
        # Gateway có thể trả 500/502 nhưng nội dung là lỗi 429 của nhà cung cấp.
        err = _status_error(openai.InternalServerError, 502, "upstream: RESOURCE_EXHAUSTED rate limit")
        self.assertEqual(_retry_delay(err, 1), 10.0)

    def test_request_timeout_status_is_retried(self):
        self.assertEqual(_retry_delay(_status_error(openai.APIStatusError, 408), 0), 1.0)

    def test_timeout_retried_once(self):
        err = openai.APITimeoutError(request=_REQ)
        self.assertEqual(_retry_delay(err, 0), 2.0)
        self.assertIsNone(_retry_delay(err, 1))

    def test_connection_error_backs_off(self):
        err = openai.APIConnectionError(request=_REQ)
        self.assertEqual(_retry_delay(err, 2), 4.0)

    def test_unknown_error_not_retried(self):
        self.assertIsNone(_retry_delay(ValueError("boom"), 0))


class CreateWithRetryTest(unittest.TestCase):
    def _extractor(self, side_effect):
        ext = object.__new__(AIExtractor)
        ext.model = "test-model"
        ext.client = mock.Mock()
        ext.client.chat.completions.create.side_effect = side_effect
        return ext

    def test_no_sleep_after_last_attempt(self):
        ext = self._extractor(_status_error(openai.InternalServerError, 503))
        with mock.patch.object(ai_extractor.time, "sleep") as sleep:
            with self.assertRaises(openai.InternalServerError):
                ext._create_with_retry({})
        self.assertEqual(ext.client.chat.completions.create.call_count, ai_extractor.MAX_ATTEMPTS)
        # 5 lần thử -> chỉ 4 lần chờ (1, 2, 4, 8); bản cũ chờ thêm 16s sau lần cuối.
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.0, 2.0, 4.0, 8.0])

    def test_client_error_fails_fast(self):
        ext = self._extractor(_status_error(openai.BadRequestError, 400))
        with mock.patch.object(ai_extractor.time, "sleep") as sleep:
            with self.assertRaises(openai.BadRequestError):
                ext._create_with_retry({})
        self.assertEqual(ext.client.chat.completions.create.call_count, 1)
        sleep.assert_not_called()

    def test_recovers_after_transient_error(self):
        ok = object()
        ext = self._extractor([_status_error(openai.InternalServerError, 502), ok])
        with mock.patch.object(ai_extractor.time, "sleep") as sleep:
            self.assertIs(ext._create_with_retry({}), ok)
        sleep.assert_called_once_with(1.0)


class ClientReuseTest(unittest.TestCase):
    def test_client_is_shared_and_has_explicit_timeout(self):
        a = _get_client("key-reuse-test", "http://gateway.test/v1")
        b = _get_client("key-reuse-test", "http://gateway.test/v1")
        self.assertIs(a, b)
        self.assertEqual(a.max_retries, 0)
        self.assertEqual(a.timeout.connect, 15.0)
        self.assertIsNot(a, _get_client("other-key", "http://gateway.test/v1"))


class LarkValueHelpersTest(unittest.TestCase):
    def test_native_attachments(self):
        fields = {"Hóa đơn": [{"file_token": "tok-a", "name": "a.pdf"}]}
        atts = app._get_attachments(fields, "Hóa đơn")
        self.assertEqual([(a["file_token"], a["name"]) for a in atts], [("tok-a", "a.pdf")])

    def test_lookup_attachments_from_search(self):
        fields = {"HĐ lookup": {"type": 17, "value": [
            {"file_token": "tok-b", "name": "b.pdf", "url": "https://x/download"}]}}
        self.assertEqual([a["file_token"] for a in app._get_attachments(fields, "HĐ lookup")], ["tok-b"])

    def test_lookup_attachments_from_get_record(self):
        # GET /records/{id} trả cột Lookup dạng phẳng và dùng "attachmentToken".
        fields = {"HĐ lookup": [{"attachmentToken": "tok-c", "name": "c.pdf", "extra": "{}"}]}
        self.assertEqual([a["file_token"] for a in app._get_attachments(fields, "HĐ lookup")], ["tok-c"])

    def test_missing_or_non_attachment_values(self):
        self.assertEqual(app._get_attachments({}, "Hóa đơn"), [])
        self.assertEqual(app._get_attachments({"X": {"link_record_ids": None}}, "X"), [])
        self.assertEqual(app._get_attachments({"X": "text"}, "X"), [])
        self.assertEqual(app._get_attachments({"X": [{"name": "no-token"}]}, "X"), [])
        self.assertEqual(app._get_attachments({"X": [{"file_token": "t"}]}, ""), [])

    def test_text_values(self):
        cases = {
            "formula text": ({"type": 1, "value": [{"text": "4478 ADMIN", "type": "text"}]}, "4478 ADMIN"),
            "lookup select": ({"type": 3, "value": ["6422"]}, "6422"),
            "formula number": ({"type": 2, "value": [34709213]}, "34709213"),
            "lookup user": ({"type": 11, "value": [{"name": "An", "id": "ou_1"}]}, "An"),
            "link": ({"link_record_ids": ["rec1"]}, ""),
            "plain select": ("BBNT", "BBNT"),
            "rich text": ([{"text": "a", "type": "text"}, {"text": "b", "type": "text"}], "a b"),
            "number": (12, "12"),
        }
        for name, (value, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(app._extract_text_field({"F": value}, "F"), expected)
        self.assertEqual(app._extract_text_field({}, "F"), "")


class SanitizeFieldsTest(unittest.TestCase):
    def test_read_only_fields_are_dropped(self):
        types = {"L": 19, "F": 20, "Auto": 1005, "T": 1}
        clean, dropped = app._sanitize_fields_for_lark({"L": "x", "F": "y", "Auto": 1, "T": "ok"}, types)
        self.assertEqual(clean, {"T": "ok"})
        self.assertEqual(sorted(dropped), ["Auto", "F", "L"])


class ListFieldsPaginationTest(unittest.TestCase):
    def test_follows_page_token(self):
        pages = [
            {"code": 0, "data": {"items": [{"field_name": "a"}, {"field_name": "b"}],
                                 "has_more": True, "page_token": "p2"}},
            {"code": 0, "data": {"items": [{"field_name": "c"}], "has_more": False}},
        ]
        responses = [mock.Mock(json=mock.Mock(return_value=p)) for p in pages]
        client = LarkClient("id", "secret")
        client._token, client._token_expires = "tok", time.time() + 3600
        with mock.patch("extractors.lark_client.requests.get", side_effect=responses) as get:
            fields = client.list_fields("app", "tbl")
        self.assertEqual([f["field_name"] for f in fields], ["a", "b", "c"])
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[1].kwargs["params"]["page_token"], "p2")


# ---------------------------------------------------------------------------
# Batch Larkbase: chạy song song, tải mỗi file 1 lần, đọc cột Lookup
# ---------------------------------------------------------------------------

LOOKUP_HD = "ĐNTT có hợp đồng không?"
BATCH_CFG = {
    "lark_app_token": "app", "lark_table_id": "tbl",
    "lark_attachment_field": "Hóa đơn", "lark_att_field_hop_dong": LOOKUP_HD,
    "lark_att_field_bbnt": "BBNT", "lark_att_field_phu_luc": "",
    "lark_doc_type_field": "Loại", "lark_content_field": "Nội dung",
    "openai_api_key": "k", "openai_model": "m", "openai_base_url": "",
}
LLM_DELAY = 0.2


class FakeLark:
    def __init__(self):
        self.downloads = []
        self.temp_paths = []
        self.updates = {}
        self.lock = threading.Lock()

    def list_fields(self, app_token, table_id):
        return [
            {"field_name": "Hóa đơn", "field_id": "fldInv", "type": 17},
            {"field_name": LOOKUP_HD, "field_id": "fldHD", "type": 19},
            {"field_name": "BBNT", "field_id": "fldBB", "type": 17},
            {"field_name": "Nội dung", "field_id": "fldND", "type": 20},
            {"field_name": "AI_MST_A", "field_id": "fldMst", "type": 19},
        ]

    def get_record(self, app_token, table_id, record_id):
        return {"fields": {
            "Hóa đơn": [{"file_token": f"inv-{record_id}", "name": "inv.pdf"}],
            # Định dạng GET record của cột Lookup: phẳng, dùng "attachmentToken".
            LOOKUP_HD: [{"attachmentToken": f"hd-{record_id}", "name": "hd.pdf"}],
            "BBNT": [{"file_token": f"bb-{record_id}", "name": "bb.pdf"}],
            "Nội dung": {"type": 1, "value": [{"text": f"ND {record_id}", "type": "text"}]},
            "Loại": "Hợp đồng",
        }}

    def save_attachment_temp(self, file_token, name, app_token, table_id, field_id, record_id):
        fd, path = tempfile.mkstemp(prefix="test_att_")
        os.close(fd)
        with self.lock:
            self.downloads.append((file_token, field_id))
            self.temp_paths.append(path)
        return path

    def update_record(self, app_token, table_id, record_id, fields):
        with self.lock:
            self.updates[record_id] = fields


class FakeExtractor:
    contract_number = "HD-full"

    def __init__(self, *args, **kwargs):
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls = Counter()

    def _llm(self, kind):
        with self.lock:
            self.calls[kind] += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        time.sleep(LLM_DELAY)
        with self.lock:
            self.in_flight -= 1

    def extract_invoice(self, blocks, filename=""):
        self._llm("invoice")
        return {"so_hoa_don": "1", "ngay_hoa_don": "01/01/2026", "tong_thanh_toan": 1000,
                "co_vat": False, "ma_so_thue_nguoi_ban": "0101"}

    def extract_contract(self, blocks, filename="", doc_type=""):
        self._llm("contract")
        return {"so_hop_dong": self.contract_number}

    def extract_doc_numbers(self, blocks, filename="", doc_type=""):
        self._llm("numbers")
        return {"so_hop_dong": "HD-step1", "so_bbnt": "BB-step1", "so_plhd": None}


class BatchProcessingTest(unittest.TestCase):
    def _run(self, record_ids, contract_number="HD-full"):
        lark = FakeLark()
        extractor = FakeExtractor()
        extractor.contract_number = contract_number
        renders = []

        def fake_render(path, first_page_only=False):
            renders.append(first_page_only)
            return [{"type": "text", "content": "x"}]

        task_id = f"test-{time.time_ns()}"
        app.tasks[task_id] = {"status": "processing", "total": len(record_ids), "processed": 0,
                              "current_record": "", "results": [], "errors": []}
        with mock.patch.object(app, "load_config", return_value=BATCH_CFG), \
                mock.patch.object(app, "make_lark_client", return_value=lark), \
                mock.patch.object(app, "AIExtractor", return_value=extractor), \
                mock.patch.object(app.FileHandler, "process_file", side_effect=fake_render):
            started = time.perf_counter()
            asyncio.run(app.process_lark_batch(task_id, record_ids))
            elapsed = time.perf_counter() - started
        return app.tasks.pop(task_id), lark, extractor, renders, elapsed

    def test_records_run_concurrently_and_complete(self):
        rids = [f"rec{i}" for i in range(6)]
        task, lark, extractor, _, elapsed = self._run(rids)
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["processed"], 6)
        self.assertEqual(sorted(r["record_id"] for r in task["results"]), rids)
        self.assertEqual(extractor.max_in_flight, app.BATCH_CONCURRENCY)
        # Mỗi record: hóa đơn + bước 1 BBNT + bước 2 hợp đồng = 3 lần gọi model x 0.2s.
        sequential = len(rids) * 3 * LLM_DELAY
        self.assertLess(elapsed, sequential * 0.6, f"elapsed={elapsed:.2f}s, sequential≈{sequential:.2f}s")

    def test_results_keep_selection_order(self):
        rids = [f"rec{i}" for i in range(7)]
        task, _, _, _, _ = self._run(rids)
        self.assertEqual([r["record_id"] for r in task["results"]], rids)

    def test_each_file_downloaded_once_with_lookup_field_id(self):
        rids = ["recA", "recB"]
        _, lark, _, renders, _ = self._run(rids)
        counts = Counter(tok for tok, _ in lark.downloads)
        self.assertTrue(all(n == 1 for n in counts.values()), counts)
        self.assertEqual(set(counts), {f"{p}-{r}" for p in ("inv", "hd", "bb") for r in rids})
        # File của cột Lookup phải tải bằng field_id của chính cột Lookup (Lark yêu cầu cho quyền tải).
        self.assertIn(("hd-recA", "fldHD"), lark.downloads)
        self.assertTrue(all(not os.path.exists(p) for p in lark.temp_paths), "file tạm chưa được xoá")
        # Mỗi record: render đủ trang cho hóa đơn + hợp đồng, render trang đầu cho BBNT.
        self.assertEqual(Counter(renders), Counter({False: 4, True: 2}))

    def test_amount_source_skips_step1_when_step2_finds_number(self):
        _, lark, extractor, _, _ = self._run(["recA"])
        self.assertEqual(extractor.calls["contract"], 1)
        self.assertEqual(extractor.calls["numbers"], 1)  # chỉ cho BBNT, không cho hợp đồng
        update = lark.updates["recA"]
        self.assertEqual(update["AI_Số_Hợp_đồng"], "HD-full")
        self.assertEqual(update["AI_Số_BBNT"], "BB-step1")

    def test_step1_fallback_when_step2_has_no_number(self):
        _, lark, extractor, _, _ = self._run(["recA"], contract_number=None)
        self.assertEqual(extractor.calls["numbers"], 2)  # BBNT + dự phòng cho hợp đồng
        self.assertEqual(lark.updates["recA"]["AI_Số_Hợp_đồng"], "HD-step1")

    def test_read_only_result_columns_not_written(self):
        _, lark, _, _, _ = self._run(["recA"])
        self.assertNotIn("AI_MST_A", lark.updates["recA"])  # cột Lookup trong bảng

    def test_no_attachment_records_are_reported(self):
        lark = FakeLark()
        lark.get_record = lambda *a: {"fields": {"Nội dung": "x", "Hóa đơn": {"type": 17, "value": []}}}
        task_id = "test-empty"
        app.tasks[task_id] = {"status": "processing", "total": 1, "processed": 0,
                              "current_record": "", "results": [], "errors": []}
        with mock.patch.object(app, "load_config", return_value=BATCH_CFG), \
                mock.patch.object(app, "make_lark_client", return_value=lark), \
                mock.patch.object(app, "AIExtractor", return_value=FakeExtractor()):
            asyncio.run(app.process_lark_batch(task_id, ["recE"]))
        self.assertEqual(lark.updates["recE"]["AI_Trạng_Thái"], "Bỏ qua - không có file")
        app.tasks.pop(task_id)


class PushUploadGuardTest(unittest.TestCase):
    def test_lookup_file_column_is_not_converted_or_written(self):
        lark = mock.Mock()
        lark.list_fields.return_value = [
            {"field_name": "Hóa đơn", "field_id": "fldInv", "type": 19},  # Lookup
            {"field_name": "Bên A", "field_id": "fldA", "type": 1},
            {"field_name": "Trạng thái", "field_id": "fldS", "type": 1},
        ]
        lark.batch_create_records.return_value = [{"record_id": "r1"}]
        data = {"mode": "bill", "lark_url": "https://x.sg.larksuite.com/base/APPTOKEN?table=tblX&view=vewY",
                "results": [{"filename": "a.pdf", "ben_a": "Cty A", "trang_thai": "OK"}]}
        with mock.patch.object(app, "make_lark_client", return_value=lark), \
                mock.patch.object(app, "load_config", return_value={}):
            res = app.push_upload_results_to_lark(data)
        self.assertTrue(res["ok"])
        lark.update_field.assert_not_called()
        lark.upload_media.assert_not_called()
        payload = lark.batch_create_records.call_args.args[2]
        self.assertNotIn("Hóa đơn", payload[0])
        self.assertEqual(payload[0]["Bên A"], "Cty A")


class SaveConfigTest(unittest.TestCase):
    def _with_config_path(self):
        d = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(app, "CONFIG_PATH", d / "config.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        return d

    def test_atomic_replace(self):
        d = self._with_config_path()
        app.save_config({"openai_model": "m1", "ten": "Hóa đơn"})
        self.assertEqual(app.load_config(), {"openai_model": "m1", "ten": "Hóa đơn"})
        self.assertEqual([p.name for p in d.iterdir()], ["config.json"])

    def test_bind_mounted_file_falls_back_to_in_place_write(self):
        # Trong Docker config.json là bind mount một file: os.replace báo EBUSY.
        d = self._with_config_path()
        app.save_config({"openai_model": "old"})
        with mock.patch.object(app.os, "replace", side_effect=OSError(16, "Device or resource busy")):
            app.save_config({"openai_model": "new"})
        self.assertEqual(app.load_config(), {"openai_model": "new"})
        self.assertEqual([p.name for p in d.iterdir()], ["config.json"])


if __name__ == "__main__":
    unittest.main()
