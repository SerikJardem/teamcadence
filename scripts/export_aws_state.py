#!/usr/bin/env python3
"""Export every TeamCadence DynamoDB item to a portable JSON snapshot."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import boto3


def json_value(value):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(f"Unsupported value: {type(value)!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="hostai")
    parser.add_argument("--region", default="eu-west-1")
    parser.add_argument("--table", default="prod-telegram-bot-team-ddb")
    parser.add_argument("--output", default=".migration/aws-state.json")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    table = session.resource("dynamodb").Table(args.table)
    items: list[dict] = []
    params = {}
    while True:
        page = table.scan(**params)
        items.extend(page.get("Items", []))
        key = page.get("LastEvaluatedKey")
        if not key:
            break
        params["ExclusiveStartKey"] = key

    items.sort(key=lambda item: (str(item.get("PK", "")), str(item.get("SK", ""))))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": {
            "provider": "aws",
            "region": args.region,
            "table": args.table,
            "exported_at": datetime.now(timezone.utc).isoformat(),
        },
        "count": len(items),
        "items": items,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=json_value) + "\n")
    print(f"Exported {len(items)} items to {output}")


if __name__ == "__main__":
    main()
