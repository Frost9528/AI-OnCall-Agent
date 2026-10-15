"""投递：幂等、有界重试、失败不回滚任务状态。"""
import asyncio
import sys
from unittest.mock import MagicMock

sys.modules.setdefault("app.services.vector_store_manager", MagicMock())

from app.models.task import (  # noqa: E402
    DeliveryStatus, TaskRecord, TaskStatus, TriggerSource,
)
from app.services.task_delivery import TaskDelivery  # noqa: E402
from app.services.task_registry import TaskRegistry  # noqa: E402


def _mk(reg, tid, **kw):
    reg.create(TaskRecord(task_id=tid, session_id="s1", thread_id=tid, input="x", **kw))
    return reg.get(tid)


def test_mark_pending_only_for_terminal_with_target(tmp_path):
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    d = TaskDelivery(registry=reg)
    _mk(reg, "a", status=TaskStatus.RUNNING, trigger=TriggerSource.CHAT, parent_session="s1")
    asyncio.run(d.mark_pending("a"))
    assert reg.get("a").delivery_status is DeliveryStatus.NONE, "非终态不该投递"

    _mk(reg, "b", status=TaskStatus.DONE, trigger=TriggerSource.CHAT, parent_session="s1")
    asyncio.run(d.mark_pending("b"))
    assert reg.get("b").delivery_status is DeliveryStatus.PENDING

    _mk(reg, "c", status=TaskStatus.DONE, trigger=TriggerSource.ALERT, parent_session=None)
    asyncio.run(d.mark_pending("c"))
    assert reg.get("c").delivery_status is DeliveryStatus.NONE, "无回填目标不该投递"


def test_consume_is_idempotent(tmp_path):
    """重复消费同一会话只返回一次（不得注入两条通知）。"""
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    d = TaskDelivery(registry=reg)
    _mk(reg, "x", status=TaskStatus.DONE, trigger=TriggerSource.CHAT, parent_session="s1")
    asyncio.run(d.mark_pending("x"))

    first = asyncio.run(d.consume_pending_for_session("s1"))
    second = asyncio.run(d.consume_pending_for_session("s1"))
    assert [t.task_id for t in first] == ["x"]
    assert second == [], "第二次消费应为空（幂等）"
    assert reg.get("x").delivery_status is DeliveryStatus.SENT


def test_delivery_failure_does_not_rollback_task_status(tmp_path):
    """投递失败不得把任务从 done 改回其它状态。"""
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    d = TaskDelivery(registry=reg)
    _mk(reg, "y", status=TaskStatus.DONE, trigger=TriggerSource.CHAT, parent_session="s1")
    asyncio.run(d.mark_pending("y"))
    d._force_fail = True            # 测试钩子：强制投递失败
    for _ in range(5):
        asyncio.run(d.deliver_pending())
    t = reg.get("y")
    assert t.status is TaskStatus.DONE, "投递失败不应回滚任务状态"
    assert t.delivery_status in (DeliveryStatus.SENT, DeliveryStatus.FAILED)


def test_deliver_pending_bounded_retries(tmp_path, monkeypatch):
    """重试有界：超 max_delivery_attempts 后标 failed，不再重试。"""
    from app.config import config
    monkeypatch.setattr(config, "max_delivery_attempts", 2)

    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    d = TaskDelivery(registry=reg)
    _mk(reg, "z", status=TaskStatus.DONE, trigger=TriggerSource.CHAT, parent_session="s1")
    asyncio.run(d.mark_pending("z"))

    d._force_fail = True            # 测试钩子：强制投递失败
    asyncio.run(d.deliver_pending())
    assert reg.get("z").delivery_status is DeliveryStatus.PENDING
    assert reg.get("z").delivery_attempts == 1
    asyncio.run(d.deliver_pending())
    assert reg.get("z").delivery_status is DeliveryStatus.FAILED, "超限应标 failed"
    assert reg.get("z").delivery_attempts == 2
