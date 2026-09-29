"""P0-3 验证：任务状态机 + SQLite checkpointer + 跨进程断点恢复。

这是本期最关键的手测：模拟"跑一半进程被杀 → 重启 → resume 续跑"，
并断言已完成步骤不重放。

用真实 AsyncSqliteSaver + 真实 P-E-R 图，但把三个节点换成可控的假节点，
以便精确制造"中断"，且不消耗 LLM / 不依赖 Milvus。
"""
import asyncio
import os
import sys
import tempfile
from typing import Annotated, List, TypedDict
import operator

import pytest

sys.modules.setdefault("app.services.vector_store_manager", __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock())

from langgraph.graph import StateGraph, END  # noqa: E402
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # noqa: E402

from app.config import config  # noqa: E402
from app.models.task import TaskRecord, TaskStatus, TERMINAL_STATUSES  # noqa: E402
from app.services.task_registry import TaskRegistry  # noqa: E402


class PState(TypedDict):
    input: str
    plan: List[str]
    past_steps: Annotated[List[tuple], operator.add]
    response: str
    failures: Annotated[List[dict], operator.add]


# 记录每个节点真实执行次数（跨实例共享，用于验证"不重放"）
EXECUTED: List[str] = []


def build_graph(checkpointer, fail_at: str | None):
    """构造与 P-E-R 同拓扑的图；fail_at 指定某节点抛异常以模拟进程中断。"""

    async def planner_node(state):
        EXECUTED.append("planner")
        return {"plan": ["step1", "step2", "step3"]}

    async def executor_node(state):
        EXECUTED.append("executor")
        step = state["plan"][0]
        if fail_at == "executor" and step == "step2":
            raise RuntimeError("模拟进程中断：executor 处理 step2 时被杀")
        return {"plan": state["plan"][1:], "past_steps": [(step, f"结果-{step}")]}

    async def replanner_node(state):
        EXECUTED.append("replanner")
        if state.get("plan"):
            return {}
        return {"response": "最终报告：" + ",".join(s for s, _ in state["past_steps"])}

    def should_continue(state):
        if state.get("response"):
            return END
        if state.get("plan"):
            return "executor"
        return "replanner"

    g = StateGraph(PState)
    g.add_node("planner", planner_node)
    g.add_node("executor", executor_node)
    g.add_node("replanner", replanner_node)
    g.set_entry_point("planner")
    g.add_edge("planner", "executor")
    g.add_edge("executor", "replanner")
    g.add_conditional_edges("replanner", should_continue, {"executor": "executor", END: END})
    return g.compile(checkpointer=checkpointer)


def test_full_cycle_interrupt_then_resume(tmp_path):
    """核心验收：中断 → 新实例 → resume → 续跑且不重放已完成步。"""
    db = str(tmp_path / "cp.sqlite")
    reg_db = str(tmp_path / "tasks.sqlite")
    registry = TaskRegistry(reg_db)

    EXECUTED.clear()
    cfg = {"configurable": {"thread_id": "t-resume-1"}}

    # ---- 第一阶段：跑到 step2 中断 ----
    async def phase1():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            graph = build_graph(saver, fail_at="executor")
            record = TaskRecord(
                task_id="t-resume-1",
                session_id="s1",
                thread_id="t-resume-1",
                input="诊断告警",
                status=TaskStatus.RUNNING,
                checkpoint_ref="t-resume-1",
            )
            registry.create(record)
            try:
                async for _ in graph.astream(
                    {"input": "诊断告警", "plan": [], "past_steps": [], "response": "", "failures": []},
                    cfg,
                    stream_mode="updates",
                ):
                    pass
            except RuntimeError as e:
                print(f"阶段1 中断: {e}")
            st = await graph.aget_state(cfg)
            return list(st.next), dict(st.values)

    next_nodes, values = asyncio.run(phase1())
    phase1_executed = list(EXECUTED)
    print("阶段1 已执行:", phase1_executed)
    print("阶段1 状态 next:", next_nodes, "past_steps:", values.get("past_steps"))

    assert "planner" in phase1_executed
    assert values.get("past_steps"), "step1 应已完成"
    assert "step1" in str(values.get("past_steps"))

    # 模拟进程被杀：把遗留 running 标为 ejected（重启时的行为）
    registry.mark_running_as_ejected()
    assert registry.get("t-resume-1").status is TaskStatus.EJECTED

    # ---- 第二阶段：新实例（新 saver）读同一 db 续跑 ----
    EXECUTED.clear()

    async def phase2():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            graph = build_graph(saver, fail_at=None)  # 不再中断
            st = await graph.aget_state(cfg)
            assert st.next, "应能读到中断点"
            async for _ in graph.astream(None, cfg, stream_mode="updates"):
                pass
            fin = await graph.aget_state(cfg)
            return dict(fin.values)

    final_values = asyncio.run(phase2())
    phase2_executed = list(EXECUTED)
    print("阶段2 已执行:", phase2_executed)
    print("阶段2 最终 past_steps:", final_values.get("past_steps"))
    print("阶段2 response:", final_values.get("response"))

    # 关键断言：planner 不重放
    assert "planner" not in phase2_executed, f"planner 不应重放，实际执行了 {phase2_executed}"
    assert "executor" in phase2_executed, "应从 executor 继续"

    # step1 只出现一次（不重放已完成步）
    steps = [s for s, _ in final_values.get("past_steps", [])]
    print("最终执行步骤:", steps)
    assert steps.count("step1") == 1, f"step1 被重放了: {steps}"
    assert "step2" in steps and "step3" in steps, f"续跑未完成剩余步骤: {steps}"
    assert final_values.get("response"), "应产出最终报告"


def test_terminal_tasks_not_in_resumable_list(tmp_path):
    """终态任务不得出现在待恢复清单（'不重复展示'）。"""
    registry = TaskRegistry(str(tmp_path / "t.sqlite"))

    for i, status in enumerate(
        [TaskStatus.DONE, TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.EXPIRED]
    ):
        registry.create(TaskRecord(
            task_id=f"term-{i}", session_id="s", thread_id=f"term-{i}",
            input="x", status=status,
        ))

    registry.create(TaskRecord(
        task_id="ej-1", session_id="s", thread_id="ej-1", input="x",
        status=TaskStatus.EJECTED,
    ))

    resumable = registry.list_resumable()
    ids = [t.task_id for t in resumable]
    assert ids == ["ej-1"], f"待恢复清单应只含 ejected，实际 {ids}"


def test_startup_scan_marks_running_as_ejected(tmp_path):
    registry = TaskRegistry(str(tmp_path / "t2.sqlite"))
    registry.create(TaskRecord(
        task_id="r1", session_id="s", thread_id="r1", input="x",
        status=TaskStatus.RUNNING,
    ))
    n = registry.mark_running_as_ejected()
    assert n == 1
    assert registry.get("r1").status is TaskStatus.EJECTED


def test_expire_stale_converges_to_terminal(tmp_path):
    """停滞超 TTL 的 ejected 应收敛为 expired（不能无期限滞留）。"""
    registry = TaskRegistry(str(tmp_path / "t3.sqlite"))
    registry.create(TaskRecord(
        task_id="old", session_id="s", thread_id="old", input="x",
        status=TaskStatus.EJECTED,
    ))
    # TTL=0 使所有 ejected 立即过期
    n = registry.expire_stale(ttl_seconds=0)
    assert n == 1
    rec = registry.get("old")
    assert rec.status is TaskStatus.EXPIRED
    assert rec.status in TERMINAL_STATUSES
    assert registry.list_resumable() == []
