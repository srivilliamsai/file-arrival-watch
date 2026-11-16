"""Application workflow. Storage methods are shared by AWS and local tests."""
from datetime import datetime, timedelta
import hashlib
import json
import logging
from time import perf_counter
from urllib.parse import unquote_plus
from zoneinfo import ZoneInfo

from domain import DEFAULT_WAREHOUSE_TIME_ZONE, cutoff_for, day_status, timestamp, upload_day, utc_now, validate_csv

LOG = logging.getLogger(__name__)


class Conflict(Exception):
    pass


class StaleDate(Exception):
    pass


def upload_result(record):
    return {
        "uploadId": record["record"].removeprefix("UPLOAD#"),
        "status": record["status"],
        "receivedAt": record["receivedAt"],
        "validatedAt": record.get("validatedAt"),
        "rowCount": int(record["rowCount"]),
        "errors": [
            {"row": int(issue["row"]) if issue["row"] is not None else None, "message": issue["message"]}
            for issue in record["errors"]
        ],
    }


class Workflow:
    def __init__(self, store, warehouse, cutoff, now=utc_now, *, required_skus=(), time_zone=DEFAULT_WAREHOUSE_TIME_ZONE):
        self.store = store
        self.warehouse = warehouse
        self.cutoff = cutoff
        self.now = now
        self.required_skus = frozenset(required_skus)
        self.time_zone = time_zone

    def status(self):
        now = self.now()
        today = now.astimezone(self.time_zone).date()
        days = [today - timedelta(days=offset) for offset in range(8)]
        records = self.store.delivery_records(days)
        summaries = [
            day_status(day, cutoff_for(day, self.cutoff, self.time_zone), now, records[day], today=day == today)
            for day in days
        ]
        return {"warehouse": self.warehouse, "timeZone": self.time_zone.key, "today": summaries[0], "history": summaries[1:]}

    def upload(self, upload_id, day, content):
        key = f"incoming/{day.isoformat()}/{upload_id}.csv"
        digest = hashlib.sha256(content).hexdigest()
        today = self.now().astimezone(self.time_zone).date()
        if day != today:
            existing = self.store.object_info(key)
            if existing is None:
                raise StaleDate()
            if existing["digest"] != digest:
                raise Conflict()
        elif not self.store.put_upload(key, content, digest):
            existing = self.store.object_info(key)
            if existing is None:
                raise RuntimeError("An upload changed during retry")
            if existing["digest"] != digest:
                raise Conflict()

    def upload_status(self, upload_id, day):
        record = self.store.record(day, f"UPLOAD#{upload_id}")
        if record is None:
            key = f"incoming/{day.isoformat()}/{upload_id}.csv"
            return {"uploadId": upload_id, "status": "processing"} if self.store.object_info(key) else None
        return upload_result(record)

    def recent_uploads(self):
        day = self.now().astimezone(self.time_zone).date()
        records = self.store.delivery_records([day])[day]
        uploads = sorted(
            (item for item in records if item["record"].startswith("UPLOAD#")),
            key=lambda item: (datetime.fromisoformat(item.get("validatedAt") or item["receivedAt"]), item["record"]),
            reverse=True,
        )
        return {"uploads": [upload_result(item) for item in uploads[:10]]}

    def validate_object(self, key):
        parts = key.split("/")
        if len(parts) != 3 or parts[0] != "incoming" or not parts[2].endswith(".csv"):
            return None
        upload_id = parts[2].removesuffix(".csv")
        day = upload_day(upload_id)
        if day is None or day.isoformat() != parts[1]:
            return None
        record_key = f"UPLOAD#{upload_id}"
        previous = self.store.record(day, record_key)
        if previous is not None:
            return previous
        started = perf_counter()
        content, received_at = self.store.read_upload(key)
        count, errors = validate_csv(content, day, self.required_skus)
        record = {
            "record": record_key,
            "receivedAt": timestamp(received_at),
            "validatedAt": timestamp(self.now(), timespec="microseconds"),
            "status": "rejected" if errors else "accepted",
            "rowCount": count,
            "errors": errors,
        }
        if not self.store.record_once(day, record):
            return self.store.record(day, record_key)
        LOG.info(json.dumps({
            "event": "upload_validated",
            "uploadId": upload_id,
            "date": day.isoformat(),
            "status": record["status"],
            "receivedAt": record["receivedAt"],
            "validatedAt": record["validatedAt"],
            "timeZone": self.time_zone.key,
            "receivedAtLocal": received_at.astimezone(self.time_zone).isoformat(timespec="milliseconds"),
            "validatedAtLocal": datetime.fromisoformat(record["validatedAt"]).astimezone(self.time_zone).isoformat(timespec="milliseconds"),
            "rowCount": count,
            "bytesRead": len(content),
            "issuesShown": len(errors),
            "validationMs": round((perf_counter() - started) * 1000, 1),
        }))
        return record

    def close_deadline(self, scheduled_at, *, time_zone=None):
        time_zone = self.time_zone if time_zone is None else time_zone
        day = scheduled_at.astimezone(time_zone).date()
        if self.now() < scheduled_at:
            raise ValueError("Cannot check a future deadline")
        if self.store.record(day, "DEADLINE") is not None:
            return
        eligible = []
        for key, received_at in self.store.uploads_for(day):
            if received_at < scheduled_at:
                result = self.validate_object(key)
                if result is not None and result["receivedAt"] < timestamp(scheduled_at):
                    eligible.append(result)
        if any(item["status"] == "accepted" for item in eligible):
            outcome = "on_time"
        else:
            outcome = "invalid_file" if eligible else "missing"
        record = {
            "record": "DEADLINE",
            "cutoffAt": timestamp(scheduled_at),
            "timeZone": time_zone.key,
            "outcome": outcome,
            "reference": f"warehouse-stock/{day.isoformat()}",
        }
        if self.store.record_once(day, record):
            LOG.info(json.dumps({
                "event": "deadline_checked",
                "date": day.isoformat(),
                "outcome": outcome,
                "timeZone": time_zone.key,
                "cutoffAtLocal": scheduled_at.astimezone(time_zone).isoformat(timespec="seconds"),
            }))

    def handle_event(self, event, bucket):
        if event.get("Event") == "s3:TestEvent":
            return
        if event.get("kind") == "deadline":
            time_zone = ZoneInfo(event["timeZone"]) if "timeZone" in event else DEFAULT_WAREHOUSE_TIME_ZONE
            self.close_deadline(datetime.fromisoformat(event["scheduledAt"]), time_zone=time_zone)
            return
        for item in event["Records"]:
            if item["s3"]["bucket"]["name"] != bucket:
                raise ValueError("Unexpected S3 event source")
            self.validate_object(unquote_plus(item["s3"]["object"]["key"]))
