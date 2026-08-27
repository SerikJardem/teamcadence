#!/usr/bin/env python3
"""Import an AWS snapshot into the named TeamCadence Firestore database."""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
from pathlib import Path

from google.cloud import firestore
from google.oauth2.credentials import Credentials


def doc_id(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def gcloud_credentials() -> Credentials:
    token = subprocess.check_output(
        ["gcloud", "auth", "print-access-token"], text=True
    ).strip()
    return Credentials(token=token)


def snapshot_items(payload) -> list[dict]:
    if isinstance(payload, list):
        return payload
    return payload.get("items") or payload.get("Items") or []


def validate_items(items: list[dict]) -> None:
    for item in items:
        if not isinstance(item.get("PK"), str) or not isinstance(item.get("SK"), str):
            raise ValueError(
                "Snapshot must contain decoded DynamoDB values: PK and SK must be strings"
            )


def delete_extra_items(client, collection: str, expected: set[tuple[str, str]]) -> int:
    expected_refs = {(doc_id(pk), doc_id(sk)) for pk, sk in expected}
    expected_partitions = {partition_id for partition_id, _ in expected_refs}
    deleted = 0
    batch = client.batch()
    pending = 0

    def queue_delete(ref) -> None:
        nonlocal batch, pending, deleted
        batch.delete(ref)
        pending += 1
        deleted += 1
        if pending == 400:
            batch.commit()
            batch = client.batch()
            pending = 0

    for partition in client.collection(collection).list_documents():
        for item in partition.collection("items").list_documents():
            if (partition.id, item.id) not in expected_refs:
                queue_delete(item)
        if partition.id not in expected_partitions:
            queue_delete(partition)
    if pending:
        batch.commit()
    return deleted


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=".migration/aws-state.json")
    parser.add_argument("--project", default="hostai-505414")
    parser.add_argument("--database", default="teamcadence")
    parser.add_argument("--collection", default="partitions")
    parser.add_argument(
        "--delete-extra",
        action="store_true",
        help="Delete Firestore records absent from the snapshot (cutover only)",
    )
    args = parser.parse_args()

    payload = json.loads(Path(args.input).read_text())
    items = snapshot_items(payload)
    validate_items(items)
    client = firestore.Client(
        project=args.project,
        database=args.database,
        credentials=gcloud_credentials(),
    )

    for pk in sorted({str(item["PK"]) for item in items}):
        client.collection(args.collection).document(doc_id(pk)).set({"pk": pk}, merge=True)

    batch = client.batch()
    pending = 0
    for item in items:
        if "PK" not in item or "SK" not in item:
            raise ValueError("Every item must contain PK and SK")
        ref = (
            client.collection(args.collection)
            .document(doc_id(str(item["PK"])))
            .collection("items")
            .document(doc_id(str(item["SK"])))
        )
        batch.set(ref, item)
        pending += 1
        if pending == 400:
            batch.commit()
            batch = client.batch()
            pending = 0
    if pending:
        batch.commit()
    deleted = 0
    if args.delete_extra:
        expected = {(item["PK"], item["SK"]) for item in items}
        deleted = delete_extra_items(client, args.collection, expected)
    print(
        f"Imported {len(items)} items into {args.project}/{args.database}; "
        f"deleted_extra={deleted}"
    )


if __name__ == "__main__":
    main()
