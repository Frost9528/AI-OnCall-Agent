"""Planner / Replanner 失败可见性测试（P0-2）

用注入异常制造结构化输出失败，断言：
- Planner 降级默认计划时同时带出失败留痕（不再是"无声降级"）；
- Replanner 决策失败时产出显式失败收尾报告（不再是静默 return {}）。
"""

import asyncio
import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault("app.services.vector_store_manager", MagicMock())

from langchain_core.runnables import RunnableLambda  # noqa: E402

from app.agent.aiops.failure import FailureKind  # noqa: E402

planner_mod = importlib.import_module("app.agent.aiops.planner")
replanner_mod = importlib.import_module("app.agent.aiops.replanner")

# 结构化输出现统一经 llm_factory.create_structured_model 构造
# （原先各节点直接 ChatQwen(...).with_structured_output(...)），故 patch 目标为工厂方法。
llm_factory_mod = importlib.import_module("app.core.llm_factory")


class SchemaError(Exception):
    def __init__(self):
        super().__init__("pydantic validation error: schema mismatch")


async def _raise_schema(_input):
    raise SchemaError()


# with_structured_output 的结果要参与 `prompt | ...` 管道，故必须是 Runnable
BOOM = RunnableLambda(_raise_schema)


def _patch_mcp_empty(module):
    """让 MCP 工具获取返回空列表，避免真实网络调用。"""
    client = MagicMock()

    async def get_tools():
        return []

    client.get_tools = get_tools

    async def factory(*_a, **_k):
        return client

    return patch.object(module, "get_mcp_client_with_retry", side_effect=factory)


def _base_state(**over):
    state = {
        "input": "诊断当前系统告警",
        "plan": [],
        "past_steps": [],
        "response": "",
        "failures": [],
    }
    state.update(over)
    return state


def test_planner_failure_degrades_but_records_reason():
    with patch.object(planner_mod, "retrieve_knowledge") as rk, \
         _patch_mcp_empty(planner_mod), \
         patch.object(llm_factory_mod.LLMFactory, "create_structured_model", return_value=BOOM):

        async def empty_retrieve(*_a, **_k):
            return ""

        rk.ainvoke = empty_retrieve

        out = asyncio.run(planner_mod.planner(_base_state()))

    assert out["plan"] == planner_mod._DEFAULT_PLAN, "应降级为默认计划"
    assert out.get("failures"), "降级必须带出失败留痕，不能静默"
    rec = out["failures"][0]
    assert rec.kind is FailureKind.SCHEMA
    assert rec.stage == "planner"
    assert "schema mismatch" in rec.reason
    assert rec.retried is False, "schema 类不应重试"


def test_replanner_decision_failure_produces_explicit_failure_report():
    with _patch_mcp_empty(replanner_mod), \
         patch.object(llm_factory_mod.LLMFactory, "create_structured_model", return_value=BOOM):

        out = asyncio.run(replanner_mod.replanner(
            _base_state(
                plan=["步骤1", "步骤2", "步骤3"],
                past_steps=[("步骤A", "结果A"), ("步骤B", "结果B")],
            )
        ))

    # 关键：不是 return {}（静默 continue），而是给出 response
    assert out.get("response"), f"应产出失败收尾报告，实际 keys={list(out)}"
    report = out["response"]
    assert "失败" in report
    assert "schema" in report.lower()
    assert "replanner_decision" in report
    assert "步骤A" in report, "报告应保留已执行步骤作为审计依据"


def test_replanner_decision_failure_report_is_not_empty_dict():
    """回归：旧实现失败时 return {}，会被上层视同 continue 而无限继续。"""
    with _patch_mcp_empty(replanner_mod), \
         patch.object(llm_factory_mod.LLMFactory, "create_structured_model", return_value=BOOM):

        out = asyncio.run(replanner_mod.replanner(
            _base_state(plan=["步骤1"], past_steps=[("步骤A", "结果A")])
        ))

    assert out != {}, "失败不得静默返回空字典"
    assert "response" in out
