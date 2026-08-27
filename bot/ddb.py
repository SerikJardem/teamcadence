"""Storage facade selected by ``STORAGE_BACKEND``.

AWS production keeps using DynamoDB. GCP uses Firestore while exposing the same
function-level API to the Telegram handlers and scheduler.
"""
from . import config

if config.STORAGE_BACKEND == "firestore":
    from .storage.firestore import *  # noqa: F403
else:
    from .storage.aws_dynamodb import *  # noqa: F403
