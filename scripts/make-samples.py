"""Write synthetic CSVs for today's date in the warehouse."""
import argparse
import csv
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--timezone", default="Asia/Kolkata", help="IANA warehouse time zone for the default date.")
    parser.add_argument("--output", type=Path, default=Path("dist/samples"))
    args = parser.parse_args()
    try:
        time_zone = ZoneInfo(args.timezone)
    except (ValueError, ZoneInfoNotFoundError):
        parser.error(f"Invalid IANA warehouse time zone: {args.timezone}")
    today = args.date or datetime.now(time_zone).date()
    args.output.mkdir(parents=True, exist_ok=True)
    rows = (("STOCK-101", 24), ("STOCK-102", 0), ("STOCK-103", 137))
    for label, day, stock in (
        ("valid", today, rows),
        ("stale", today - timedelta(days=1), rows),
        ("incomplete", today, rows[:1]),
    ):
        path = args.output / f"stock-{label}.csv"
        with path.open("w", encoding="utf-8", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(("snapshot_date", "sku", "quantity"))
            for sku, quantity in stock:
                writer.writerow((day, sku, quantity))
        print(path)


if __name__ == "__main__":
    main()
