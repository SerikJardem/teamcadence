#!/usr/bin/env python3
"""Compare Firestore keys and payloads with an exported AWS snapshot."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from google.cloud import firestore
from import_firestore_state import gcloud_credentials, snapshot_items


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=".migration/aws-state.json")
    parser.add_argument("--project", default="hostai-505414")
    parser.add_argument("--database", default="teamcadence")
    parser.add_argument("--collection", default="partitions")
    args = parser.parse_args()

    expected_items = snapshot_items(json.loads(Path(args.input).read_text()))
    expected = {(str(item["PK"]), str(item["SK"])): item for item in expected_items}
    client = firestore.Client(
        project=args.project,
        database=args.database,
        credentials=gcloud_credentials(),
    )
    actual = {}
    for partition in client.collection(args.collection).list_documents():
        for snapshot in partition.collection("items").stream():
            item = snapshot.to_dict() or {}
            actual[(str(item.get("PK")), str(item.get("SK")))] = item

    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    changed = sorted(key for key in expected.keys() & actual.keys() if expected[key] != actual[key])
    print(
        f"expected={len(expected)} actual={len(actual)} "
        f"missing={len(missing)} extra={len(extra)} changed={len(changed)}"
    )
    if missing or extra or changed:
        for label, values in (("missing", missing), ("extra", extra), ("changed", changed)):
            if values:
                print(f"{label}: {values[:20]}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
