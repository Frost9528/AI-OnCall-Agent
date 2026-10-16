import sys
from unittest.mock import MagicMock

sys.modules.setdefault("app.services.vector_store_manager", MagicMock())

from app.models.task import TaskRecord  # noqa: E402
from app.services.task_registry import TaskRegistry  # noqa: E402


def test_create_is_idempotent_and_persists_key(tmp_path):
    registry = TaskRegistry(str(tmp_path / "tasks.sqlite"))
    first = TaskRecord(
        task_id="first", session_id="alert", thread_id="first", input="alert 1",
        idempotency_key="fingerprint-1",
    )
    duplicate = TaskRecord(
        task_id="second", session_id="alert", thread_id="second", input="alert 1 retry",
        idempotency_key="fingerprint-1",
    )

    created = registry.create(first)
    returned = registry.create(duplicate)

    assert created.task_id == returned.task_id == "first"
    assert registry.get("second") is None
    assert registry.get("first").idempotency_key == "fingerprint-1"
