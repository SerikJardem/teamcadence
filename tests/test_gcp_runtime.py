import ast
from pathlib import Path

from fastapi.testclient import TestClient

from bot import cloudrun, config


def public_functions(path: str) -> set[str]:
    tree = ast.parse(Path(path).read_text())
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_")
    }


def test_firestore_implements_legacy_storage_contract():
    legacy = public_functions("bot/storage/aws_dynamodb.py")
    firestore = public_functions("bot/storage/firestore.py")
    assert legacy <= firestore


def test_health_and_webhook_secret(monkeypatch):
    monkeypatch.setattr(config, "SERVICE_MODE", "webhook")
    monkeypatch.setattr(config, "TELEGRAM_WEBHOOK_SECRET", "expected-secret")
    client = TestClient(cloudrun.app)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True

    missing = client.post("/webhook", json={"update_id": 1})
    assert missing.status_code == 403

    wrong = client.post(
        "/webhook",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert wrong.status_code == 403


def test_scheduler_route_isolated_by_service_mode(monkeypatch):
    monkeypatch.setattr(config, "SERVICE_MODE", "webhook")
    response = TestClient(cloudrun.app).post("/run")
    assert response.status_code == 404
