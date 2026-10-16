from types import SimpleNamespace

import asyncio
import sys
from unittest.mock import MagicMock, patch

sys.modules.setdefault("app.services.vector_store_manager", MagicMock())

from langchain.agents import create_agent  # noqa: E402
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402

import app.services.rag_agent_service as ras  # noqa: E402
from app.models.task import (  # noqa: E402
    DeliveryStatus, TaskRecord, TaskStatus, TriggerSource,
)
from app.services.task_delivery import TaskDelivery  # noqa: E402
from app.services.task_registry import TaskRegistry  # noqa: E402


def _done_task(reg, tid, sess, report="报告内容", status=TaskStatus.DONE):
    reg.create(TaskRecord(task_id=tid, session_id=sess, thread_id=tid, input="查日志",
                          status=status, trigger=TriggerSource.CHAT,
                          parent_session=sess, report=report))
    return reg.get(tid)


def test_agent_middleware_reads_thread_id_from_langgraph_config(tmp_path):
    from app.config import config

    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    delivery = TaskDelivery(registry=reg)
    _done_task(reg, "t-real", "sess-real")
    asyncio.run(delivery.mark_pending("t-real"))
    model = FakeMessagesListChatModel(responses=[AIMessage(content="ok")])
    agent = create_agent(model, tools=[], middleware=[ras.inject_task_delivery_middleware])

    with patch.object(ras, "task_delivery", delivery), \
         patch.object(config, "context_inject_enabled", True):
        asyncio.run(agent.ainvoke(
            {"messages": [{"role": "user", "content": "hi"}]},
            config={"configurable": {"thread_id": "sess-real"}},
        ))

    assert reg.get("t-real").delivery_status is DeliveryStatus.SENT


class FakeRuntime:
    def __init__(self, thread_id):
        self.execution_info = SimpleNamespace(thread_id=thread_id)


def test_injects_pending_notification_when_enabled(tmp_path):
    from app.config import config

    reg = TaskRegistry(str(tmp_path / "direct.sqlite"))
    delivery = TaskDelivery(registry=reg)
    _done_task(reg, "t-9", "sess-1")
    asyncio.run(delivery.mark_pending("t-9"))

    with patch.object(ras, "task_delivery", delivery), \
         patch.object(config, "context_inject_enabled", True):
        out = asyncio.run(ras.inject_task_delivery_middleware.abefore_model(
            {"messages": []}, FakeRuntime("sess-1")))

    assert out is not None and "messages" in out
    content = out["messages"][0].content
    assert "t-9" in content
    assert "报告内容" not in content
    assert reg.get("t-9").delivery_status is DeliveryStatus.SENT


def test_failure_notification_is_not_worded_as_completion(tmp_path):
    from app.config import config

    reg = TaskRegistry(str(tmp_path / "failed.sqlite"))
    delivery = TaskDelivery(registry=reg)
    _done_task(reg, "t-failed", "sess-failed", status=TaskStatus.FAILED)
    asyncio.run(delivery.mark_pending("t-failed"))

    with patch.object(ras, "task_delivery", delivery), \
         patch.object(config, "context_inject_enabled", True):
        out = asyncio.run(ras.inject_task_delivery_middleware.abefore_model(
            {"messages": []}, FakeRuntime("sess-failed")))

    content = out["messages"][0].content
    assert "失败" in content
    assert "已完成" not in content


def test_does_not_inject_when_disabled(tmp_path):
    from app.config import config

    reg = TaskRegistry(str(tmp_path / "disabled.sqlite"))
    delivery = TaskDelivery(registry=reg)
    _done_task(reg, "t-d", "sess-1")
    asyncio.run(delivery.mark_pending("t-d"))

    with patch.object(ras, "task_delivery", delivery), \
         patch.object(config, "context_inject_enabled", False):
        out = asyncio.run(ras.inject_task_delivery_middleware.abefore_model(
            {"messages": []}, FakeRuntime("sess-1")))

    assert out is None
    assert reg.get("t-d").delivery_status is DeliveryStatus.PENDING
