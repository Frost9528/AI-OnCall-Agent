"""trigger / delivery 字段与迁移。"""
import sys
from unittest.mock import MagicMock

sys.modules.setdefault("app.services.vector_store_manager", MagicMock())

from app.models.task import (  # noqa: E402
    DeliveryStatus, TaskRecord, TaskStatus, TriggerSource,
)
from app.services.task_registry import TaskRegistry  # noqa: E402


def _mk(reg, tid, **kw):
    reg.create(TaskRecord(task_id=tid, session_id="s", thread_id=tid, input="x", **kw))
    return reg.get(tid)


def test_defaults_are_manual_and_none(tmp_path):
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    t = _mk(reg, "d1")
    assert t.trigger is TriggerSource.MANUAL
    assert t.delivery_status is DeliveryStatus.NONE
    assert t.delivery_attempts == 0
    assert t.delivered_at is None


def test_trigger_and_delivery_roundtrip(tmp_path):
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    _mk(reg, "d2", trigger=TriggerSource.CHAT, parent_session="sess-9")
    t = reg.get("d2")
    assert t.trigger is TriggerSource.CHAT
    assert t.parent_session == "sess-9"


def test_set_delivery_persists(tmp_path):
    from datetime import datetime, timezone
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    _mk(reg, "d3")
    now = datetime.now(timezone.utc)
    reg.set_delivery("d3", DeliveryStatus.SENT, attempts=1, delivered_at=now)
    t = reg.get("d3")
    assert t.delivery_status is DeliveryStatus.SENT
    assert t.delivery_attempts == 1
    assert t.delivered_at is not None


def test_old_db_migration_adds_columns(tmp_path):
    """老库（缺 4 新列）应被自动补齐，且旧记录保守为 manual/none。"""
    import sqlite3
    db = str(tmp_path / "old.sqlite")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE aiops_tasks (
            task_id TEXT PRIMARY KEY, owner TEXT NOT NULL DEFAULT 'anonymous',
            session_id TEXT NOT NULL, parent_session TEXT, thread_id TEXT NOT NULL,
            input TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, checkpoint_ref TEXT,
            plan_version INTEGER NOT NULL DEFAULT 1, time_anchor TEXT,
            result_ref TEXT, interrupt TEXT NOT NULL DEFAULT 'manual', report TEXT
        );
    """)
    conn.execute(
        "INSERT INTO aiops_tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("old-1","u","s",None,"old-1","x","running",
         "2026-10-01T00:00:00+00:00","2026-10-01T00:00:00+00:00",None,1,None,None,"manual",None),
    )
    conn.commit(); conn.close()

    reg = TaskRegistry(db)          # 触发迁移
    t = reg.get("old-1")
    assert t.trigger is TriggerSource.MANUAL
    assert t.delivery_status is DeliveryStatus.NONE
