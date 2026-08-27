import importlib.util
from pathlib import Path

import pytest


def load_import_module():
    source = Path("scripts/import_firestore_state.py")
    spec = importlib.util.spec_from_file_location("import_firestore_state", source)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_snapshot_rejects_low_level_dynamodb_attribute_values():
    module = load_import_module()
    with pytest.raises(ValueError, match="PK and SK must be strings"):
        module.validate_items([{"PK": {"S": "REGISTRY"}, "SK": {"S": "TENANT#1"}}])


def test_snapshot_accepts_decoded_dynamodb_values():
    module = load_import_module()
    module.validate_items([{"PK": "REGISTRY", "SK": "TENANT#1"}])
