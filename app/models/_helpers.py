"""Helpers compartidos por los modelos."""
import uuid
from datetime import datetime, timezone


def uuid_str() -> str:
    return str(uuid.uuid4())


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
