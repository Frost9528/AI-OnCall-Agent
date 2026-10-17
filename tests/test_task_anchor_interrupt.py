"""P0-3 补充验证：time_anchor 沿用 + interrupt 判定 + 结果落库（E5/E3 部分）。

这三项是 §4.3② 里设计文档明确标注"未实现"的部分，本文件把它们补成可实现、
可验证的部分：

- ``time_anchor``：恢复时沿用**原任务时间窗**而非按 now 重查（时间敏感查询会答非所问）；
- ``interrupt``：按**失败留痕**确定性判定 auto/manual，不交给 LLM；
- 结果落库：任务跑完报告能凭 task_id 查回来（不再是无消费方的 ``result_ref``）。
"""
import asyncio
import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault(
    "app.services.vector_store_manager",
    __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(),
)

from app.agent.aiops.failure import FailureKind  # noqa: E402
from app.models.task import InterruptMode, TaskRecord, TaskStatus  # noqa: E402
from app.services.task_registry import TaskRegistry  # noqa: E402

executor_mod = importlib.import_module("app.agent.aiops.executor")
service_mod = importlib.import_module("app.services.aiops_service")


# ---------------- time_anchor（E5）----------------


def test_time_anchor_hint_present_when_set():
    """有锚点时，执行提示里必须出现时间窗约束。"""
    hint = executor_mod._time_window_hint("2026-10-13T14:00:00+00:00")
    assert "2026-10-13T14:00:00+00:00" in hint
    assert "时间窗" in hint
    # 关键语义：不要把锚定查询当成"当前现状"重查
    assert "当前" in hint


def test_time_anchor_hint_empty_when_unset():
    """无锚点时不注入噪声约束。"""
    assert executor_mod._time_window_hint("") == ""
    assert executor_mod._time_window_hint(None) == ""


def test_create_task_records_time_anchor(tmp_path):
    """新建任务必须落一个时间锚点（记录任务创建那一刻）。"""
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    with patch.object(service_mod, "task_registry", reg):
        svc = service_mod.AIOpsService.__new__(service_mod.AIOpsService)
        task = svc.create_task("诊断告警", session_id="s1")
        assert task.time_anchor is not None, "时间锚点应在创建时落库"
        assert reg.get(task.task_id).time_anchor is not None, "应持久化"


def test_time_anchor_survives_roundtrip(tmp_path):
    """锚点写库再读回，精度与值不变（恢复时靠它沿用）。"""
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    from datetime import datetime, timezone

    anchor = datetime(2026, 10, 13, 14, 0, 0, tzinfo=timezone.utc)
    reg.create(TaskRecord(
        task_id="a1", session_id="s", thread_id="a1", input="x",
        time_anchor=anchor,
    ))
    got = reg.get("a1").time_anchor
    assert got == anchor, f"锚点往返失真: {got} != {anchor}"


# ---------------- interrupt auto|manual（E5）----------------


def test_interrupt_defaults_to_manual(tmp_path):
    """缺省保守为 manual：不确定就要求人工决策，不擅自自动续跑。"""
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    reg.create(TaskRecord(
        task_id="d1", session_id="s", thread_id="d1", input="x",
    ))
    assert reg.get("d1").interrupt is InterruptMode.MANUAL


def test_set_interrupt_mode_persists(tmp_path):
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    reg.create(TaskRecord(
        task_id="m1", session_id="s", thread_id="m1", input="x",
    ))
    reg.set_interrupt_mode("m1", InterruptMode.AUTO)
    assert reg.get("m1").interrupt is InterruptMode.AUTO


def _classification_service(reg, failures):
    """构造只带 fake graph 的 service，用于判定逻辑单测。"""

    class FakeState:
        def __init__(self, values):
            self.values = values

    class FakeGraph:
        async def aget_state(self, _cfg):
            return FakeState({"failures": failures})

    svc = service_mod.AIOpsService.__new__(service_mod.AIOpsService)
    svc.graph = FakeGraph()
    return svc


def _mk_task(reg, task_id):
    reg.create(TaskRecord(
        task_id=task_id, session_id="s", thread_id=task_id, input="x",
        status=TaskStatus.EJECTED,
    ))
    return reg.get(task_id)


def test_no_failures_means_auto(tmp_path):
    """无失败留痕 → auto（只读工具集下可直接续）。"""
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    with patch.object(service_mod, "task_registry", reg):
        svc = _classification_service(reg, [])
        task = _mk_task(reg, "c1")
        asyncio.run(svc.classify_interrupt_modes([task]))
    assert reg.get("c1").interrupt is InterruptMode.AUTO


def test_permission_failure_means_manual(tmp_path):
    """权限/配置类失败 → manual：继续走也不会好转，需人看一眼。"""
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    failures = [{"stage": "executor", "kind": FailureKind.PERMISSION, "reason": "无权限"}]
    with patch.object(service_mod, "task_registry", reg):
        svc = _classification_service(reg, failures)
        task = _mk_task(reg, "c2")
        asyncio.run(svc.classify_interrupt_modes([task]))
    assert reg.get("c2").interrupt is InterruptMode.MANUAL


def test_schema_failure_means_manual(tmp_path):
    """schema 恒不匹配 → manual（重试无意义）。"""
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    failures = [{"stage": "planner", "kind": "schema", "reason": "解析失败"}]
    with patch.object(service_mod, "task_registry", reg):
        svc = _classification_service(reg, failures)
        task = _mk_task(reg, "c3")
        asyncio.run(svc.classify_interrupt_modes([task]))
    assert reg.get("c3").interrupt is InterruptMode.MANUAL


def test_transient_failure_still_auto(tmp_path):
    """瞬时类（超时/5xx）→ auto，不该因一次超时就要求人工介入。"""
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    failures = [{"stage": "executor", "kind": FailureKind.TRANSIENT, "reason": "超时"}]
    with patch.object(service_mod, "task_registry", reg):
        svc = _classification_service(reg, failures)
        task = _mk_task(reg, "c4")
        asyncio.run(svc.classify_interrupt_modes([task]))
    assert reg.get("c4").interrupt is InterruptMode.AUTO


def test_classification_error_is_conservative(tmp_path):
    """判定本身失败 → 保守 manual，绝不因读不到现场就自动续。"""
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))

    class BrokenGraph:
        async def aget_state(self, _cfg):
            raise RuntimeError("checkpoint 损坏")

    svc = service_mod.AIOpsService.__new__(service_mod.AIOpsService)
    svc.graph = BrokenGraph()
    with patch.object(service_mod, "task_registry", reg):
        task = _mk_task(reg, "c5")
        asyncio.run(svc.classify_interrupt_modes([task]))
    assert reg.get("c5").interrupt is InterruptMode.MANUAL


# ---------------- 结果落库 ----------------

def test_report_persisted_and_readable(tmp_path):
    """任务完成后报告可凭 task_id 查回（这是"结果去哪了"的答案）。"""
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    reg.create(TaskRecord(
        task_id="r1", session_id="s", thread_id="r1", input="x",
        status=TaskStatus.RUNNING,
    ))
    reg.update_status(
        "r1", TaskStatus.DONE,
        result_ref="aiops_tasks/r1#report",
        report="# 诊断报告\n根因：连接池耗尽",
    )
    rec = reg.get("r1")
    assert rec.status is TaskStatus.DONE
    assert rec.report and "连接池耗尽" in rec.report
    # result_ref 应指向报告实体，而非自指 task_id（旧实现的占位）
    assert rec.result_ref == "aiops_tasks/r1#report"


def test_report_absent_for_unfinished_task(tmp_path):
    """未完成任务无报告，字段为 None（不伪造）。"""
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    reg.create(TaskRecord(
        task_id="r2", session_id="s", thread_id="r2", input="x",
        status=TaskStatus.RUNNING,
    ))
    assert reg.get("r2").report is None


def test_report_survives_roundtrip_with_unicode(tmp_path):
    """中文报告往返不丢内容。"""
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    body = "# 故障诊断报告\n\n## 根因\n数据库连接池耗尽，导致上游超时。\n\n## 建议\n1. 扩容连接池"
    reg.create(TaskRecord(
        task_id="r3", session_id="s", thread_id="r3", input="x",
    ))
    reg.update_status("r3", TaskStatus.DONE, report=body)
    assert reg.get("r3").report == body
