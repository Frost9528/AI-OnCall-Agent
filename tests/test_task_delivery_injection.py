"""通道2：middleware 在流内注入待投递通知（零竞态）。"""
import sys
from unittest.mock import MagicMock, patch

sys.modules.setdefault("app.services.vector_store_manager", MagicMock())

import app.services.rag_agent_service as ras  # noqa: E402
from app.models.task import (  # noqa: E402
    DeliveryStatus, TaskRecord, TaskStatus, TriggerSource,
)
from app.services.task_delivery import TaskDelivery  # noqa: E402
from app.services.task_registry import TaskRegistry  # noqa: E402


class FakeRuntime:
    def __init__(self, thread_id):
        self.config = {"configurable": {"thread_id": thread_id}}


def _done_task(reg, tid, sess, report="报告内容"):
    reg.create(TaskRecord(task_id=tid, session_id=sess, thread_id=tid, input="查日志",
                          status=TaskStatus.DONE, trigger=TriggerSource.CHAT,
                          parent_session=sess, report=report))
    return reg.get(tid)


def test_injects_pending_notification_when_enabled(tmp_path):
    from app.config import config
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    d = TaskDelivery(registry=reg)
    _done_task(reg, "t-9", "sess-1")
    import asyncio
    asyncio.run(d.mark_pending("t-9"))

    with patch.object(ras, "task_delivery", d), \
         patch.object(config, "context_inject_enabled", True):
        out = asyncio.run(ras.inject_task_delivery_middleware.abefore_model(
            {"messages": []}, FakeRuntime("sess-1")))

    assert out is not None and "messages" in out, "应注入消息"
    content = out["messages"][0].content
    assert "t-9" in content, "通知应含 task_id"
    assert "报告内容" not in content, "通知应是轻量的，不含报告全文"
    assert reg.get("t-9").delivery_status is DeliveryStatus.SENT


def test_does_not_inject_when_disabled(tmp_path):
    from app.config import config
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    d = TaskDelivery(registry=reg)
    _done_task(reg, "t-d", "sess-1")
    import asyncio
    asyncio.run(d.mark_pending("t-d"))

    with patch.object(ras, "task_delivery", d), \
         patch.object(config, "context_inject_enabled", False):
        out = asyncio.run(ras.inject_task_delivery_middleware.abefore_model(
            {"messages": []}, FakeRuntime("sess-1")))

    assert out is None, "开关关闭时不应注入"
    assert reg.get("t-d").delivery_status is DeliveryStatus.PENDING, "也不该消费"
