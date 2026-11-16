"""Loopback preview with the real HTTP, CSV and deadline workflow in memory."""
import argparse
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import secrets
import sys
from threading import RLock
from time import monotonic
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from domain import DEFAULT_WAREHOUSE_TIME_ZONE, MAX_BYTES, cutoff_for, utc_now
from http_api import error, response, route
from service import Workflow


DEMO_SKUS = ("STOCK-101", "STOCK-102")


class PreviewStore:
    def __init__(self, now):
        self.now = now
        self.objects = {}
        self.results = {}
        self.lock = RLock()

    def delivery_records(self, days):
        with self.lock:
            return {day: list(self.results.get(day, {}).values()) for day in days}

    def record(self, day, key):
        with self.lock:
            return self.results.get(day, {}).get(key)

    def record_once(self, day, data):
        with self.lock:
            records = self.results.setdefault(day, {})
            if data["record"] in records:
                return False
            records[data["record"]] = data
            return True

    def put_upload(self, key, content, digest):
        with self.lock:
            if key in self.objects:
                return False
            self.objects[key] = (content, self.now(), digest)
            return True

    def object_info(self, key):
        with self.lock:
            value = self.objects.get(key)
            return {"digest": value[2]} if value else None

    def read_upload(self, key):
        with self.lock:
            content, received_at, _ = self.objects[key]
            return content, received_at

    def uploads_for(self, day):
        with self.lock:
            return [
                (key, data[1])
                for key, data in self.objects.items()
                if key.startswith(f"incoming/{day.isoformat()}/")
            ]


def seed_history(workflow, today):
    outcomes = ["on_time", "on_time", "missing", "invalid_file", "on_time", "missing", "on_time"]
    for offset, outcome in enumerate(outcomes, 1):
        day = today - timedelta(days=offset)
        due = cutoff_for(day, workflow.cutoff, workflow.time_zone)
        if due is None:
            continue
        if outcome != "missing" or offset == 6:
            upload_id = f"{day.isoformat()}_{uuid4()}"
            key = f"incoming/{day.isoformat()}/{upload_id}.csv"
            received_at = due + timedelta(minutes=7 if offset == 6 else -(offset + 2))
            skus = DEMO_SKUS[:1] if outcome == "invalid_file" else DEMO_SKUS
            content = "snapshot_date,sku,quantity\n" + "".join(f"{day},{sku},24\n" for sku in skus)
            workflow.store.objects[key] = (content.encode(), received_at, "")
        workflow.close_deadline(due)
        if offset == 6:
            workflow.validate_object(key)


def preview_handler(workflow, sessions):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ROOT / "dist"), **kwargs)

        def send_result(self, result):
            body = result["body"].encode("utf-8")
            self.send_response(result["statusCode"])
            for name, value in result["headers"].items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/config.json":
                self.send_result(response(200, {"localPreview": True, "apiUrl": ""}))
            elif path == "/api/status":
                self.handle_api("GET /api/status")
            elif path == "/api/uploads":
                self.handle_api("GET /api/uploads")
            elif path.startswith("/api/uploads/"):
                self.handle_api("GET /api/uploads/{uploadId}", path.rsplit("/", 1)[-1])
            else:
                super().do_GET()

        def do_POST(self):
            path = urlsplit(self.path).path
            if path == "/local/session":
                token = secrets.token_urlsafe(32)
                expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).timestamp()
                sessions[token] = expires
                self.send_result(response(200, {"token": token, "expiresAt": int(expires * 1000)}))
            elif path == "/api/uploads":
                self.handle_api("POST /api/uploads")
            else:
                self.send_result(error(404, "not_found", "This route does not exist."))

        def handle_api(self, route_key, upload_id=None):
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            claims = {}
            if sessions.get(token, 0) > datetime.now(timezone.utc).timestamp():
                claims = {"sub": "local-operator", "token_use": "access", "scope": "file-arrival/upload"}
            event = {
                "routeKey": route_key,
                "pathParameters": {"uploadId": upload_id},
                "requestContext": {"authorizer": {"jwt": {"claims": claims}}},
            }
            length = int(self.headers.get("Content-Length", 0))
            if length < 0 or length > MAX_BYTES * 6 + 256:
                self.send_result(error(413, "file_too_large", "The CSV exceeds 256 KiB."))
                return
            if length:
                event["body"] = self.rfile.read(length)
            if route_key == "GET /api/status":
                check_deadline(workflow)
            result = route(event, workflow)
            if route_key == "POST /api/uploads" and result["statusCode"] == 202:
                accepted_id = json.loads(result["body"])["uploadId"]
                day = accepted_id.split("_", 1)[0]
                workflow.validate_object(f"incoming/{day}/{accepted_id}.csv")
            self.send_result(result)

    return Handler


def check_deadline(workflow):
    now = workflow.now()
    cutoff = cutoff_for(now.astimezone(workflow.time_zone).date(), workflow.cutoff, workflow.time_zone)
    if cutoff is not None and now >= cutoff:
        workflow.close_deadline(cutoff)


def preview_workflow(now=utc_now, *, time_zone=DEFAULT_WAREHOUSE_TIME_ZONE):
    today = now().astimezone(time_zone).date()
    workflow = Workflow(
        PreviewStore(now), "South Warehouse", "08:00", now, required_skus=DEMO_SKUS, time_zone=time_zone,
    )
    seed_history(workflow, today)
    return workflow


def serve_preview(workflow, port):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    check_deadline(workflow)
    server = ThreadingHTTPServer(("127.0.0.1", port), preview_handler(workflow, {}))
    print(f"File Arrival Watch preview: http://127.0.0.1:{server.server_port}/", flush=True)
    with server:
        server.serve_forever()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4173)
    parser.add_argument("--timezone", default=DEFAULT_WAREHOUSE_TIME_ZONE.key, help="IANA warehouse time zone.")
    parser.add_argument("--demo-after-cutoff", action="store_true", help="Simulate a post-cutoff clock for browser tests.")
    args = parser.parse_args()
    try:
        time_zone = ZoneInfo(args.timezone)
    except (ValueError, ZoneInfoNotFoundError):
        parser.error(f"Invalid IANA warehouse time zone: {args.timezone}")
    if not (ROOT / "dist" / "index.html").exists():
        parser.error("Run npm run build first.")
    now = utc_now
    if args.demo_after_cutoff:
        today = utc_now().astimezone(time_zone).date()
        cutoff = cutoff_for(today, "08:00", time_zone)
        if cutoff is None:
            parser.error("There is no scheduled cutoff today in this time zone.")
        base_time = cutoff + timedelta(minutes=2)
        started = monotonic()

        def now():
            return base_time + timedelta(seconds=monotonic() - started)
    serve_preview(preview_workflow(now, time_zone=time_zone), args.port)


if __name__ == "__main__":
    main()
