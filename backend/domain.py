"""Rules shared by the HTTP and event handlers."""
import csv
import re
from datetime import date, datetime, time, timedelta, timezone
from io import StringIO
from zoneinfo import ZoneInfo

DEFAULT_WAREHOUSE_TIME_ZONE = ZoneInfo("Asia/Kolkata")
MAX_BYTES = 256 * 1024
MAX_ROWS = 5_000
MAX_ERRORS = 10
COLUMNS = {"snapshot_date", "sku", "quantity"}
STATUS_REASONS = {
    "awaiting_file": "Today’s stock file is due by the warehouse cutoff.",
    "checking_deadline": "The cutoff has passed. The latest uploads are being verified.",
    "missing": "The deadline passed without a stock file. Wait for a valid delivery before using today’s stock counts.",
    "invalid_file": "The file did not pass validation. The warehouse operator can upload a corrected copy.",
    "ready_on_time": "Today’s stock file passed validation and arrived before the cutoff.",
    "ready_late": "Today’s stock file passed validation. It arrived after the cutoff.",
    "ready_no_cutoff": "The stock file passed validation. No cutoff was scheduled because of the local clock change.",
    "cutoff_skipped": "The scheduled cutoff falls in a skipped hour because of the local clock change.",
    "check_delayed": "The delivery service hasn’t confirmed the deadline result yet.",
    "not_monitored": "No delivery was recorded for this date.",
}
UPLOAD_ID = re.compile(
    r"(?P<day>\d{4}-\d{2}-\d{2})_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


def upload_day(upload_id):
    match = UPLOAD_ID.fullmatch(upload_id) if isinstance(upload_id, str) else None
    if match is None:
        return None
    try:
        return date.fromisoformat(match["day"])
    except ValueError:
        return None


def utc_now():
    return datetime.now(timezone.utc)


def timestamp(value, *, timespec="milliseconds"):
    return value.astimezone(timezone.utc).isoformat(timespec=timespec).replace("+00:00", "Z")


def cutoff_for(day, hhmm, time_zone=DEFAULT_WAREHOUSE_TIME_ZONE):
    wall_time = datetime.combine(day, time.fromisoformat(hhmm))
    cutoff = wall_time.replace(tzinfo=time_zone).astimezone(timezone.utc)
    # Scheduler skips cutoffs in a gap when the local clock moves forward.
    if cutoff.astimezone(time_zone).replace(tzinfo=None) != wall_time:
        return None
    return cutoff


def validate_csv(content, day, required_skus=frozenset()):
    errors = []
    count = 0
    seen = set()

    def error(row, message):
        if len(errors) < MAX_ERRORS:
            errors.append({"row": row, "message": message})

    if len(content) > MAX_BYTES:
        return 0, [{"row": None, "message": "The CSV exceeds 256 KiB."}]
    try:
        source = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return 0, [{"row": None, "message": "Save the CSV with UTF-8 encoding."}]
    reader = csv.reader(StringIO(source, newline=""), strict=True)
    try:
        headers = [column.strip() for column in next(reader, [])]
        if len(set(headers)) != len(headers) or not COLUMNS.issubset(headers):
            return 0, [{"row": 1, "message": "Use unique column names and include snapshot_date, sku, and quantity."}]
        snapshot, sku, quantity = (headers.index(field) for field in ("snapshot_date", "sku", "quantity"))
        expected = day.isoformat()
        for row in reader:
            if not row:
                continue
            count += 1
            row_number = reader.line_num
            if count > MAX_ROWS:
                limit_error = {"row": row_number, "message": "The CSV exceeds 5,000 data rows."}
                return count, errors[: MAX_ERRORS - 1] + [limit_error]
            if len(row) != len(headers):
                error(row_number, "The number of fields does not match the header.")
                continue
            if row[snapshot].strip() != expected:
                error(row_number, f"Snapshot date must be {expected}.")
            product = row[sku].strip()
            if not product:
                error(row_number, "SKU is required.")
            elif product in seen:
                error(row_number, "SKU appears more than once.")
            seen.add(product)
            if not re.fullmatch(r"[0-9]+", row[quantity].strip()):
                error(row_number, "Quantity must be a nonnegative whole number.")
    except csv.Error:
        error(reader.line_num or 1, "The CSV is malformed. Check quoting and column separators.")
    if not count and not errors:
        error(None, "Add at least one stock row.")
    if not errors:
        missing = sorted(required_skus - seen)
        if missing:
            examples = ", ".join(missing[:5])
            suffix = f" and {len(missing) - 5} more" if len(missing) > 5 else ""
            error(None, f"Required SKUs missing: {examples}{suffix}.")
    return count, errors


def day_status(day, cutoff, now, records, today=False):
    uploads = [item for item in records if item["record"].startswith("UPLOAD#")]
    accepted = sorted(
        (entry for entry in uploads if entry["status"] == "accepted"),
        key=lambda entry: (entry["receivedAt"], entry["record"]),
    )
    deadline = next((item for item in records if item["record"] == "DEADLINE"), None)
    result = deadline["outcome"] if deadline else None
    if deadline:
        cutoff = datetime.fromisoformat(deadline["cutoffAt"])
    received = accepted[0]["receivedAt"] if accepted else None
    status = "not_monitored"
    if cutoff is None:
        status = "ready_no_cutoff" if received else "invalid_file" if uploads else "cutoff_skipped"
    elif received:
        status = "ready_on_time" if received < timestamp(cutoff) else "ready_late"
    elif result in {"missing", "invalid_file"}:
        status = "invalid_file" if uploads else result
    elif today and now >= cutoff + timedelta(minutes=2):
        status = "check_delayed"
    elif today and now >= cutoff:
        status = "checking_deadline"
    elif uploads:
        status = "invalid_file"
    elif today:
        status = "awaiting_file"
    return {
        "date": day.isoformat(),
        "cutoffAt": timestamp(cutoff) if cutoff else None,
        "receivedAt": received,
        "latestUploadAt": max((item["receivedAt"] for item in uploads), default=None),
        "status": status,
        "reason": STATUS_REASONS[status],
        "deadlineOutcome": result,
        "attempts": len(uploads),
    }
