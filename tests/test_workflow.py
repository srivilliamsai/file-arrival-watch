import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from preview import PreviewStore, Workflow, cutoff_for, route
from domain import MAX_BYTES, MAX_ROWS, validate_csv


DAY = date(2026, 11, 14)
DUE = cutoff_for(DAY, "08:00")
HEADER = "snapshot_date,sku,quantity\n"
GOOD = HEADER + f"{DAY},STOCK-101,24\n"
BAD = HEADER + "2026-11-13,STOCK-101,24\n"
CLAIMS = {"sub": "operator", "token_use": "access", "scope": "file-arrival/upload"}


class CsvTests(unittest.TestCase):
    def test_accepts_utf8_bom_quoted_fields_reordered_columns_and_zero(self):
        body = f'\ufeffsku,quantity,snapshot_date,note\r\n"A, B",0,{DAY},"a\r\nline"\r\n'
        self.assertEqual(validate_csv(body.encode(), DAY), (1, []))

    def test_rejects_bad_shape_and_values(self):
        cases = {
            "columns": ("sku,quantity\nA,3\n", "include snapshot_date"),
            "duplicate header": ("snapshot_date,sku,quantity,sku\n", "unique column names"),
            "stale date": (BAD, "Snapshot date must be"),
            "duplicate sku": (GOOD + f"{DAY}, STOCK-101 ,9\n", "SKU appears more than once"),
            "missing sku": (HEADER + f"{DAY},,9\n", "SKU is required"),
            "negative": (HEADER + f"{DAY},A,-1\n", "nonnegative whole number"),
            "decimal": (HEADER + f"{DAY},A,1.5\n", "nonnegative whole number"),
            "field count": (GOOD + f"{DAY},B\n", "number of fields"),
            "quote": (HEADER + f'{DAY},"A,3\n', "malformed"),
            "no rows": (HEADER, "at least one"),
        }
        for name, (source, reason) in cases.items():
            with self.subTest(name=name):
                _, errors = validate_csv(source.encode(), DAY)
                self.assertTrue(any(reason in item["message"] for item in errors), errors)

    def test_limits_errors_bytes_and_rows(self):
        _, errors = validate_csv((HEADER + f"{DAY},X,-1\n" * 30).encode(), DAY)
        self.assertEqual(len(errors), 10)
        self.assertIn("UTF-8", validate_csv(b"\xff", DAY)[1][0]["message"])
        self.assertIn("256 KiB", validate_csv(b"x" * (MAX_BYTES + 1), DAY)[1][0]["message"])
        content = (HEADER + "".join(f"{DAY},X-{i},1\n" for i in range(MAX_ROWS + 1))).encode()
        self.assertIn("5,000", validate_csv(content, DAY)[1][-1]["message"])


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.clock = DUE - timedelta(minutes=2)
        self.store = PreviewStore(lambda: self.clock)
        self.workflow = Workflow(self.store, "Test Warehouse", "08:00", lambda: self.clock)

    def upload(self, csv=GOOD):
        name = f"{DAY}_{uuid4()}"
        self.workflow.upload(name, DAY, csv.encode())
        return name, f"incoming/{DAY}/{name}.csv"

    def current(self):
        return self.workflow.status()["today"]

    def api(self, key, *, claims=CLAIMS, body=None, upload_id=None, encoded=False):
        result = route({
            "routeKey": key,
            "pathParameters": {"uploadId": upload_id},
            "requestContext": {"authorizer": {"jwt": {"claims": claims}}},
            "body": body,
            "isBase64Encoded": encoded,
        }, self.workflow)
        return result["statusCode"], json.loads(result["body"])

    def test_scheduler_recovers_on_time_upload_with_delayed_event(self):
        name, key = self.upload()
        self.assertEqual(self.workflow.upload_status(name, DAY)["status"], "processing")
        self.clock = DUE + timedelta(seconds=59)
        self.workflow.close_deadline(DUE)
        event = {"Records": [{"s3": {"bucket": {"name": "uploads"}, "object": {"key": key}}}]}
        self.workflow.handle_event(event, "uploads")
        self.workflow.handle_event(event, "uploads")
        self.assertEqual(self.current()["status"], "ready_on_time")
        self.assertEqual(self.current()["deadlineOutcome"], "on_time")
        self.assertEqual(self.current()["attempts"], 1)

    def test_warehouse_zone_controls_delivery_date_and_receipt_status(self):
        zone = ZoneInfo("America/New_York")
        self.clock = datetime.fromisoformat("2026-11-15T01:00:00Z")
        self.workflow = Workflow(self.store, "New York Warehouse", "08:00", lambda: self.clock, time_zone=zone)
        self.assertEqual(self.workflow.status()["timeZone"], zone.key)
        self.assertEqual(self.current()["date"], "2026-11-14")
        self.assertEqual(self.current()["cutoffAt"], "2026-11-14T13:00:00.000Z")
        self.assertEqual(cutoff_for(date(2026, 7, 1), "08:00", zone).isoformat(), "2026-07-01T12:00:00+00:00")
        self.workflow.handle_event({
            "kind": "deadline", "scheduledAt": "2026-11-14T13:00:00Z", "timeZone": zone.key,
        }, "uploads")
        self.assertEqual(self.current()["status"], "missing")
        upload_id, key = self.upload()
        self.workflow.validate_object(key)
        self.assertEqual(self.current()["status"], "ready_late")
        self.assertEqual(self.workflow.recent_uploads()["uploads"][0]["uploadId"], upload_id)
        self.assertEqual(self.store.record(DAY, "DEADLINE")["timeZone"], zone.key)
        self.clock += timedelta(hours=4)
        self.assertEqual(self.current()["date"], "2026-11-15")
        self.assertIsNone(self.current()["receivedAt"])

    def test_scheduler_retry_uses_its_original_time_zone(self):
        self.clock = datetime.fromisoformat("2026-11-14T01:00:00Z")
        self.workflow.handle_event({
            "kind": "deadline", "scheduledAt": "2026-11-14T00:30:00Z", "timeZone": "America/New_York",
        }, "uploads")
        prior_day = DAY - timedelta(days=1)
        self.assertEqual(self.store.record(prior_day, "DEADLINE")["outcome"], "missing")
        self.assertIsNone(self.store.record(DAY, "DEADLINE"))

    def test_daylight_saving_gap_skips_the_deadline(self):
        day = date(2026, 3, 8)
        zone = ZoneInfo("America/New_York")
        self.clock = datetime.fromisoformat("2026-03-08T13:00:00Z")
        self.workflow = Workflow(self.store, "New York Warehouse", "02:30", lambda: self.clock, time_zone=zone)
        self.assertIsNone(cutoff_for(day, "02:30", zone))
        self.assertEqual(self.current()["status"], "cutoff_skipped")
        self.assertIsNone(self.current()["cutoffAt"])
        upload_id = f"{day}_{uuid4()}"
        self.workflow.upload(upload_id, day, (HEADER + f"{day},STOCK-101,24\n").encode())
        self.workflow.validate_object(f"incoming/{day}/{upload_id}.csv")
        self.assertEqual(self.current()["status"], "ready_no_cutoff")

    def test_missing_invalid_then_ready_late_and_same_deadline(self):
        self.assertEqual(self.current()["status"], "awaiting_file")
        self.clock = DUE
        self.assertEqual(self.current()["status"], "checking_deadline")
        self.clock += timedelta(minutes=2)
        self.assertEqual(self.current()["status"], "check_delayed")
        self.workflow.close_deadline(DUE)
        self.assertEqual(self.current()["status"], "missing")
        rejected, rejected_key = self.upload(BAD)
        accepted, accepted_key = self.upload()
        self.workflow.validate_object(accepted_key)
        self.workflow.validate_object(rejected_key)
        self.workflow.close_deadline(DUE)
        self.assertEqual(self.current()["status"], "ready_late")
        self.assertEqual(self.current()["deadlineOutcome"], "missing")
        self.assertEqual(self.workflow.upload_status(accepted, DAY)["rowCount"], 1)
        self.assertEqual(self.workflow.upload_status(rejected, DAY)["errors"][0]["row"], 2)
        self.assertEqual(len(self.workflow.status()["history"]), 7)

    def test_invalid_before_deadline_and_exact_cutoff_is_late(self):
        _, invalid_key = self.upload(BAD)
        self.workflow.validate_object(invalid_key)
        self.assertEqual(self.current()["status"], "invalid_file")
        self.clock = DUE
        _, valid_key = self.upload()
        self.workflow.close_deadline(DUE)
        self.workflow.validate_object(valid_key)
        self.assertEqual(self.current()["deadlineOutcome"], "invalid_file")
        self.assertEqual(self.current()["status"], "ready_late")

    def test_earliest_valid_receipt_wins_even_with_reversed_events(self):
        _, first = self.upload()
        self.clock = DUE + timedelta(minutes=1)
        _, second = self.upload()
        self.workflow.validate_object(second)
        self.assertEqual(self.current()["status"], "ready_late")
        self.workflow.validate_object(first)
        self.workflow.close_deadline(DUE)
        self.assertEqual(self.current()["status"], "ready_on_time")
        self.assertEqual(self.current()["deadlineOutcome"], "on_time")

    def test_duplicate_scheduler_deliveries_write_one_record(self):
        self.clock = DUE + timedelta(seconds=60)
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(self.workflow.close_deadline, [DUE] * 12))
        self.assertEqual(list(self.store.results[DAY]), ["DEADLINE"])

    def test_s3_or_database_failure_does_not_report_a_miss(self):
        self.clock = DUE + timedelta(minutes=2)
        with patch.object(self.store, "uploads_for", side_effect=PermissionError("S3 unavailable")):
            with self.assertRaises(PermissionError):
                self.workflow.close_deadline(DUE)
        with patch.object(self.store, "record_once", side_effect=RuntimeError("DynamoDB unavailable")):
            with self.assertRaises(RuntimeError):
                self.workflow.close_deadline(DUE)
        self.assertEqual(self.current()["status"], "check_delayed")
        self.assertIsNone(self.current()["deadlineOutcome"])
        self.workflow.close_deadline(DUE)
        self.assertEqual(self.current()["status"], "missing")

    def test_direct_s3_upload_requires_complete_stock_before_cutoff(self):
        self.workflow.required_skus = frozenset({"STOCK-101", "STOCK-102"})
        upload_id = f"{DAY}_{uuid4()}"
        key = f"incoming/{DAY}/{upload_id}.csv"
        self.store.put_upload(key, GOOD.encode(), "checksum")
        self.clock = DUE
        self.workflow.close_deadline(DUE)
        self.assertEqual(self.current()["deadlineOutcome"], "invalid_file")
        self.assertEqual(self.workflow.upload_status(upload_id, DAY)["errors"], [
            {"row": None, "message": "Required SKUs missing: STOCK-102."},
        ])
        self.clock += timedelta(minutes=1)
        corrected_id = f"{DAY}_{uuid4()}"
        corrected_key = f"incoming/{DAY}/{corrected_id}.csv"
        self.store.put_upload(corrected_key, (GOOD + f"{DAY},STOCK-102,0\n").encode(), "checksum-corrected")
        event = {"Records": [{"s3": {"bucket": {"name": "uploads"}, "object": {"key": corrected_key}}}]}
        self.workflow.handle_event(event, "uploads")
        self.workflow.handle_event(event, "uploads")
        self.assertEqual(self.current()["status"], "ready_late")
        self.assertEqual(self.current()["deadlineOutcome"], "invalid_file")
        self.assertEqual(self.api("GET /api/uploads", claims={})[0], 401)
        self.assertEqual(self.api("GET /api/uploads", claims={**CLAIMS, "scope": "openid"})[0], 403)
        status, activity = self.api("GET /api/uploads")
        self.assertEqual(status, 200)
        self.assertEqual([item["uploadId"] for item in activity["uploads"]], [corrected_id, upload_id])
        _, public = self.api("GET /api/status", claims={})
        self.assertNotIn("STOCK-102", json.dumps(public))

    def test_activity_uses_validation_order_without_changing_receipt_time(self):
        first_id, first_key = self.upload()
        second_id, second_key = self.upload()
        self.clock += timedelta(seconds=1)
        self.workflow.validate_object(second_key)
        self.clock += timedelta(microseconds=1)
        self.workflow.validate_object(first_key)
        _, body = self.api("GET /api/uploads")
        first, second = body["uploads"]
        self.assertEqual([first["uploadId"], second["uploadId"]], [first_id, second_id])
        self.assertEqual(first["receivedAt"], second["receivedAt"])
        self.assertGreater(first["validatedAt"], second["validatedAt"])

    def test_api_access_idempotency_and_delivery_date(self):
        upload_id = f"{DAY}_{uuid4()}"
        body = json.dumps({"uploadId": upload_id, "csv": GOOD})
        post = "POST /api/uploads"
        get = "GET /api/uploads/{uploadId}"
        self.assertEqual(self.api("GET /api/status", claims={})[0], 200)
        self.assertEqual(self.api(post, claims={}, body=body)[0], 401)
        self.assertEqual(self.api(get, claims={}, upload_id=upload_id)[0], 401)
        self.assertEqual(self.api(post, claims={"sub": "operator", "token_use": "id"}, body=body)[0], 401)
        self.assertEqual(self.api(get, upload_id=upload_id)[0], 404)
        self.assertEqual(self.api(post, body=body), (202, {"uploadId": upload_id}))
        self.assertEqual(self.api(post, body=base64.b64encode(body.encode()), encoded=True)[0], 202)
        self.assertEqual(self.api(post, body=body.replace(",24", ",25"))[1]["code"], "upload_conflict")
        self.assertEqual(len(self.store.objects), 1)
        self.assertEqual(self.api(get, upload_id=upload_id)[1]["status"], "processing")
        self.clock += timedelta(days=1)
        self.assertEqual(self.api(post, body=body)[0], 202)
        self.assertEqual(self.api(post, body=body.replace(upload_id, f"{DAY}_{uuid4()}"))[1]["code"], "date_changed")

    def test_api_serializes_dynamodb_error_rows(self):
        upload_id = f"{DAY}_{uuid4()}"
        self.store.record_once(DAY, {
            "record": f"UPLOAD#{upload_id}",
            "status": "rejected",
            "receivedAt": "2026-11-14T02:00:00.000Z",
            "rowCount": Decimal(1),
            "errors": [
                {"row": Decimal(2), "message": "Snapshot date is stale."},
                {"row": None, "message": "A file-level issue."},
            ],
        })
        status, result = self.api("GET /api/uploads/{uploadId}", upload_id=upload_id)
        self.assertEqual(status, 200)
        self.assertEqual(result["rowCount"], 1)
        self.assertEqual([item["row"] for item in result["errors"]], [2, None])

    def test_api_rejects_bad_and_oversized_requests_without_creating_objects(self):
        post = "POST /api/uploads"
        for body in ("[]", "{}", '{"csv":22}', "invalid json"):
            self.assertEqual(self.api(post, body=body)[0], 400)
        self.assertEqual(self.api(post, body=json.dumps({"uploadId": "wrong", "csv": GOOD}))[0], 400)
        self.assertEqual(self.api(post, body=json.dumps({"uploadId": "", "csv": "x" * (MAX_BYTES + 1)}))[0], 413)
        self.assertFalse(self.store.objects)
