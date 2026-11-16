"""HTTP API v2 routes, using API Gateway JWT authorizer claims."""
import base64
import binascii
import json

from domain import MAX_BYTES, upload_day
from service import Conflict, StaleDate


def response(status, payload):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json; charset=utf-8", "cache-control": "no-store"},
        "body": json.dumps(payload),
    }


def error(status, code, message):
    return response(status, {"code": code, "message": message})


def route(event, workflow):
    route_key = event["routeKey"]
    if route_key == "GET /api/status":
        return response(200, workflow.status())
    if route_key not in {"GET /api/uploads", "POST /api/uploads", "GET /api/uploads/{uploadId}"}:
        return error(404, "not_found", "This route does not exist.")
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    if claims.get("token_use") != "access" or not claims.get("sub"):
        return error(401, "sign_in_required", "Sign in as the warehouse operator.")
    if "file-arrival/upload" not in claims.get("scope", "").split():
        return error(403, "forbidden", "This account does not have warehouse upload access.")

    if route_key == "GET /api/uploads":
        return response(200, workflow.recent_uploads())
    if route_key.startswith("GET"):
        upload_id = (event.get("pathParameters") or {}).get("uploadId")
        day = upload_day(upload_id)
        if day is None:
            return error(400, "invalid_upload_id", "The upload reference is invalid.")
        result = workflow.upload_status(upload_id, day)
        return response(200, result) if result else error(404, "not_found", "This upload could not be found.")

    body = event.get("body") or ""
    if len(body) > (MAX_BYTES * 6 + 256) * 4 // 3 + 4:
        return error(413, "file_too_large", "The CSV exceeds 256 KiB.")
    try:
        decoded = base64.b64decode(body, validate=True) if event.get("isBase64Encoded") else body
        data = json.loads(decoded)
        upload_id = data.get("uploadId")
        content = data["csv"].encode("utf-8")
    except (AttributeError, KeyError, UnicodeError, ValueError, binascii.Error):
        return error(400, "invalid_request", "Choose a UTF-8 CSV and try again.")

    if len(content) > MAX_BYTES:
        return error(413, "file_too_large", "The CSV exceeds 256 KiB.")
    day = upload_day(upload_id)
    if day is None:
        return error(400, "invalid_upload_id", "The upload reference is invalid.")
    try:
        workflow.upload(upload_id, day, content)
    except StaleDate:
        return error(409, "date_changed", "The warehouse date changed. Refresh the dashboard and choose today’s CSV.")
    except Conflict:
        return error(409, "upload_conflict", "This upload reference already belongs to a different file.")
    return response(202, {"uploadId": upload_id})
