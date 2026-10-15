"""启动时补偿未投递的任务（上次进程死掉时留下的 pending）。"""
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

sys.modules.setdefault("app.services.vector_store_manager", MagicMock())

from app.models.task import (  # noqa: E402
    DeliveryStatus, InterruptMode, TaskRecord, TaskStatus, TriggerSource,
)
from app.services.task_delivery import TaskDelivery  # noqa: E402
from app.services.task_registry import TaskRegistry  # noqa: E402
from app import main as app_main  # noqa: E402


def _resumable_task(task_id: str, interrupt: InterruptMode) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        session_id="s",
        thread_id=task_id,
        input="x",
        status=TaskStatus.EJECTED,
        trigger=TriggerSource.CHAT,
        parent_session="s",
        interrupt=interrupt,
    )


def test_startup_deliver_pending_processes_backlog(tmp_path):
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    reg.create(TaskRecord(task_id="p1", session_id="s", thread_id="p1", input="x",
                          status=TaskStatus.DONE, trigger=TriggerSource.CHAT,
                          parent_session="s", delivery_status=DeliveryStatus.PENDING))
    d = TaskDelivery(registry=reg)
    n = asyncio.run(d.deliver_pending())
    assert n == 1
    assert reg.get("p1").delivery_status in (DeliveryStatus.PENDING, DeliveryStatus.SENT)


def test_startup_recovery_does_not_resume_when_disabled(monkeypatch):
    delivery = MagicMock()
    delivery.deliver_pending = AsyncMock(return_value=0)
    dispatcher = MagicMock()
    dispatcher.resume = AsyncMock()
    aiops = MagicMock()

    monkeypatch.setattr(app_main, "task_delivery", delivery)
    monkeypatch.setattr(app_main, "dispatcher", dispatcher)
    monkeypatch.setattr(app_main, "aiops_service", aiops)
    monkeypatch.setattr(app_main.config, "auto_resume_on_startup", False)

    asyncio.run(app_main.apply_startup_recovery())

    delivery.deliver_pending.assert_awaited_once()
    aiops.list_resumable_tasks.assert_not_called()
    dispatcher.resume.assert_not_awaited()


def test_startup_recovery_resumes_only_auto_tasks(monkeypatch):
    auto_task = _resumable_task("auto-1", InterruptMode.AUTO)
    manual_task = _resumable_task("manual-1", InterruptMode.MANUAL)
    delivery = MagicMock()
    delivery.deliver_pending = AsyncMock(return_value=0)
    dispatcher = MagicMock()
    dispatcher.resume = AsyncMock()
    dispatcher.dispatch = MagicMock()
    aiops = MagicMock()
    aiops.list_resumable_tasks.return_value = [auto_task, manual_task]

    monkeypatch.setattr(app_main, "task_delivery", delivery)
    monkeypatch.setattr(app_main, "dispatcher", dispatcher)
    monkeypatch.setattr(app_main, "aiops_service", aiops)
    monkeypatch.setattr(app_main.config, "auto_resume_on_startup", True)

    asyncio.run(app_main.apply_startup_recovery())

    aiops.list_resumable_tasks.assert_called_once_with()
    dispatcher.resume.assert_awaited_once_with(auto_task)
    dispatcher.dispatch.assert_not_called()


def test_startup_recovery_continues_after_delivery_failure(monkeypatch):
    auto_task = _resumable_task("auto-1", InterruptMode.AUTO)
    delivery = MagicMock()
    delivery.deliver_pending = AsyncMock(side_effect=RuntimeError("delivery unavailable"))
    dispatcher = MagicMock()
    dispatcher.resume = AsyncMock()
    aiops = MagicMock()
    aiops.list_resumable_tasks.return_value = [auto_task]

    monkeypatch.setattr(app_main, "task_delivery", delivery)
    monkeypatch.setattr(app_main, "dispatcher", dispatcher)
    monkeypatch.setattr(app_main, "aiops_service", aiops)
    monkeypatch.setattr(app_main.config, "auto_resume_on_startup", True)

    asyncio.run(app_main.apply_startup_recovery())

    dispatcher.resume.assert_awaited_once_with(auto_task)
