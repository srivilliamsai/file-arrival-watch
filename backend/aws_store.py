"""AWS storage for the File Arrival Watch workflow."""
from datetime import datetime, time, timedelta, timezone

from botocore.exceptions import ClientError

from domain import MAX_BYTES


class AwsStore:
    def __init__(self, s3, table, bucket):
        self.s3 = s3
        self.table = table
        self.bucket = bucket

    def delivery_records(self, days):
        records = {}
        for day in days:
            request = {
                "KeyConditionExpression": "delivery = :partition",
                "ExpressionAttributeValues": {":partition": self.partition(day)},
                "ConsistentRead": True,
            }
            records[day] = []
            while True:
                page = self.table.query(**request)
                records[day].extend(page["Items"])
                if "LastEvaluatedKey" not in page:
                    break
                request["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return records

    @staticmethod
    def partition(day):
        return f"WAREHOUSE#{day.isoformat()}"

    def record(self, day, key):
        return self.table.get_item(
            Key={"delivery": self.partition(day), "record": key}, ConsistentRead=True
        ).get("Item")

    def record_once(self, day, data):
        expires = datetime.combine(day + timedelta(days=30), time.min, timezone.utc)
        try:
            self.table.put_item(
                Item={"delivery": self.partition(day), "expiresAt": int(expires.timestamp()), **data},
                ConditionExpression="attribute_not_exists(delivery)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def put_upload(self, key, body, digest):
        for attempt in range(3):
            try:
                self.s3.put_object(
                    Bucket=self.bucket, Key=key, Body=body, ContentType="text/csv",
                    Metadata={"sha256": digest}, IfNoneMatch="*",
                )
                return True
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code in {"PreconditionFailed", "412"}:
                    return False
                if code not in {"ConditionalRequestConflict", "409"} or attempt == 2:
                    raise
        raise RuntimeError("Conditional upload retry was exhausted")

    def object_info(self, key):
        try:
            value = self.s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return {"digest": value["Metadata"].get("sha256")}

    def read_upload(self, key):
        result = self.s3.get_object(Bucket=self.bucket, Key=key)
        with result["Body"] as stream:
            body = stream.read(MAX_BYTES + 1)
        return body, result["LastModified"]

    def uploads_for(self, day):
        pages = self.s3.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=f"incoming/{day.isoformat()}/"
        )
        for page in pages:
            for item in page.get("Contents", []):
                yield item["Key"], item["LastModified"]
