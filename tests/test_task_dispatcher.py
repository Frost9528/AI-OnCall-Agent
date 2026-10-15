"""派发：建任务、单飞行、非阻塞、后台持引用。"""
import asyncio
import sys
from unittest.mock import MagicMock

sys.modules.setdefault("app.services.vector_store_manager", MagicMock())

from app.models.task import TaskRecord, TaskStatus, TriggerSource  # noqa: E402
from app.services.task_dispatcher import TaskDispatcher  # noqa: E402
from app.services.task_registry import TaskRegistry  # noqa: E402


def _dispatcher(tmp_path, runner):
    return TaskDispatcher(registry=TaskRegistry(str(tmp_path / "t.sqlite")), runner=runner)


def test_dispatch_creates_task_with_trigger(tmp_path):
    seen = []

    async def runner(task):
        seen.append(task.task_id)

    d = _dispatcher(tmp_path, runner)

    async def go():
        rec = await d.dispatch("诊断", trigger=TriggerSource.CHAT,
                               session_id="s1", parent_session="s1", background=False)
        return rec

    rec = asyncio.run(go())
    assert rec.trigger is TriggerSource.CHAT
    assert rec.session_id == "s1"
    assert rec.parent_session == "s1"
    assert rec.time_anchor is not None            # 自动落锚点
    assert d.registry.get(rec.task_id) is not None


def test_chat_single_flight_returns_existing(tmp_path):
    """同会话已有 running 任务时，第二次派发返回同一 task_id（不新建）。"""
    gate = asyncio.Event()

    async def runner(task):
        await gate.wait()

    d = _dispatcher(tmp_path, runner)

    async def go():
        first = await d.dispatch("第一次", trigger=TriggerSource.CHAT, session_id="s9")
        await asyncio.sleep(0)                 # 让后台起来
        second = await d.dispatch("第二次", trigger=TriggerSource.CHAT, session_id="s9")
        gate.set()
        return first, second

    first, second = asyncio.run(go())
    assert second.task_id == first.task_id, "单飞行：应返回已有任务"
    assert len(d.registry.list_by_session("s9")) == 1


def test_alert_trigger_is_not_single_flight(tmp_path):
    """告警触发不适用单飞行（不同告警应并行）。"""
    gate = asyncio.Event()

    async def runner(task):
        await gate.wait()

    d = _dispatcher(tmp_path, runner)

    async def go():
        a = await d.dispatch("告警A", trigger=TriggerSource.ALERT, session_id="s1")
        b = await d.dispatch("告警B", trigger=TriggerSource.ALERT, session_id="s1")
        gate.set()
        return a, b

    a, b = asyncio.run(go())
    assert a.task_id != b.task_id
    assert a.parent_session is None            # alert 无对话可回填


def test_dispatch_is_non_blocking(tmp_path):
    """background=True 时 dispatch 必须立即返回，不等任务跑完。"""
    finished = []

    async def runner(task):
        await asyncio.sleep(0.3)
        finished.append(task.task_id)

    d = _dispatcher(tmp_path, runner)

    async def go():
        import time
        t0 = time.perf_counter()
        rec = await d.dispatch("慢任务", trigger=TriggerSource.MANUAL, session_id="s1")
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.2, f"dispatch 应非阻塞，实际耗时 {elapsed:.2f}s"
        assert finished == [], "任务不应已完成"
        await asyncio.sleep(0.4)               # 收尾
        return rec

    rec = asyncio.run(go())
    assert rec.task_id in finished
    assert d.pending_background() == 0         # 完成后引用被释放


def test_background_false_runs_inline(tmp_path):
    """background=False 时内联执行（降级路径），返回时任务已完成。"""
    done = []

    async def runner(task):
        done.append(task.task_id)

    d = _dispatcher(tmp_path, runner)
    rec = asyncio.run(d.dispatch("同步", trigger=TriggerSource.MANUAL,
                                 session_id="s1", background=False))
    assert rec.task_id in done


def test_resume_marks_ejected_task_running(tmp_path, monkeypatch):
    """续跑前先进入 running，避免单飞行与下次启动扫描失效。"""
    registry = TaskRegistry(str(tmp_path / "t.sqlite"))
    task = TaskRecord(
        task_id="resume-1",
        session_id="resume-session",
        thread_id="resume-1",
        input="诊断",
        status=TaskStatus.EJECTED,
    )
    registry.create(task)
    d = TaskDispatcher(registry=registry)

    async def stub_run_task(resumed_task, resume=False):
        assert resumed_task.task_id == task.task_id
        assert resume is True
        if False:
            yield {}

    from app.services.aiops_service import aiops_service
    monkeypatch.setattr(aiops_service, "run_task", stub_run_task)

    asyncio.run(d.resume(task, background=False))

    stored = registry.get(task.task_id)
    assert stored.status is TaskStatus.RUNNING
    assert registry.find_running_by_session(task.session_id).task_id == task.task_id


def test_cancelled_task_marked_ejected_not_failed(tmp_path):
    """服务关闭导致的取消应标 ejected，而非 failed。"""
    gate = asyncio.Event()

    async def runner(task):
        await gate.wait()

    d = _dispatcher(tmp_path, runner)

    async def go():
        rec = await d.dispatch("被取消", trigger=TriggerSource.MANUAL, session_id="s1")
        await asyncio.sleep(0)                 # 让后台起来
        (bg_task,) = list(d._bg)               # 当前唯一后台协程
        bg_task.cancel()
        try:
            await bg_task
        except asyncio.CancelledError:
            pass                                # 取消正是我们制造的场景
        return rec

    rec = asyncio.run(go())
    stored = d.registry.get(rec.task_id)
    assert stored.status is TaskStatus.EJECTED, f"应为 ejected，实际 {stored.status}"
    assert stored.status is not TaskStatus.FAILED
