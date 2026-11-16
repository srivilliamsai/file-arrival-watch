"""AWS Lambda entry points."""
from functools import lru_cache
from datetime import datetime
import json
import logging
import os
from zoneinfo import ZoneInfo

import boto3

from aws_store import AwsStore
from domain import DEFAULT_WAREHOUSE_TIME_ZONE
from http_api import error, route
from service import Workflow

LOG = logging.getLogger(__name__)
logging.getLogger().setLevel(logging.INFO)


@lru_cache
def workflow():
    table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    store = AwsStore(boto3.client("s3"), table, os.environ["UPLOAD_BUCKET"])
    required_skus = [sku.strip() for sku in os.environ.get("REQUIRED_SKUS", "").split(",") if sku.strip()]
    return Workflow(
        store, os.environ["WAREHOUSE"], os.environ["CUTOFF"], required_skus=required_skus,
        time_zone=ZoneInfo(os.environ["WAREHOUSE_TIME_ZONE"]),
    )


def api(event, context):
    try:
        return route(event, workflow())
    except Exception as exc:
        LOG.error(json.dumps({"event": "api_error", "requestId": context.aws_request_id, "errorType": type(exc).__name__}))
        return error(503, "unavailable", "The delivery service is temporarily unavailable. Try again shortly.")


def worker(event, context):
    workflow().handle_event(event, os.environ["UPLOAD_BUCKET"])


def notify(event, context):
    sns = boto3.client("sns")
    for entry in event["Records"]:
        record = entry["dynamodb"]["NewImage"]
        outcome = record["outcome"]["S"]
        reason = {"missing": "No stock CSV arrived", "invalid_file": "No valid stock CSV arrived"}[outcome]
        reference = record["reference"]["S"]
        time_zone = ZoneInfo(record.get("timeZone", {"S": DEFAULT_WAREHOUSE_TIME_ZONE.key})["S"])
        cutoff = datetime.fromisoformat(record["cutoffAt"]["S"]).astimezone(time_zone)
        dashboard = os.environ["DASHBOARD_URL"]
        sns.publish(
            TopicArn=os.environ["ALERT_TOPIC"],
            Subject="File Arrival Watch — stock file deadline missed",
            Message=(
                f"{reason} before the warehouse cutoff.\n\n"
                f"Cutoff: {cutoff.isoformat(sep=' ', timespec='minutes')} ({time_zone.key})\n"
                f"Reference: {reference}\n\nDashboard: {dashboard}"
            ),
        )
        LOG.info(json.dumps({"event": "alert_published", "reference": reference}))
