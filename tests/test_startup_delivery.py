"""启动时补偿未投递的任务（上次进程死掉时留下的 pending）。"""
import asyncio
import sys
from unittest.mock import MagicMock, patch

sys.modules.setdefault("app.services.vector_store_manager", MagicMock())

from app.models.task import (  # noqa: E402
    DeliveryStatus, TaskRecord, TaskStatus, TriggerSource,
)
from app.services.task_delivery import TaskDelivery  # noqa: E402
from app.services.task_registry import TaskRegistry  # noqa: E402


def test_startup_deliver_pending_processes_backlog(tmp_path):
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    reg.create(TaskRecord(task_id="p1", session_id="s", thread_id="p1", input="x",
                          status=TaskStatus.DONE, trigger=TriggerSource.CHAT,
                          parent_session="s", delivery_status=DeliveryStatus.PENDING))
    d = TaskDelivery(registry=reg)
    n = asyncio.run(d.deliver_pending())
    assert n == 1
    assert reg.get("p1").delivery_status in (DeliveryStatus.PENDING, DeliveryStatus.SENT)
