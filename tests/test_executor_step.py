"""Executor 单步超时 + 失败分类测试（P1-2）

覆盖三条分流路径（对齐 ENGINEERING-DESIGN §4.3①）：
- 瞬时（超时/5xx）→ 重试；
- 权限/配置 → 跳过并留痕，**不重试**；
- 数据为空 → 标"无数据"，**不编造**；
外加：LLM 决策失败、结果汇总失败回退、无条件兜底异常。

设计：把 executor 依赖的三处（LLM、ToolNode、MCP 客户端）分别注入假对象，
从而精确命中每条分支，不消耗真实 LLM、不依赖 Milvus。
"""

import asyncio
import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault("app.services.vector_store_manager", MagicMock())

from langchain_core.messages import AIMessage  # noqa: E402

from app.agent.aiops.failure import FailureKind  # noqa: E402

exec_mod = importlib.import_module("app.agent.aiops.executor")


# ---------- 测试替身 ----------

class TransientError(Exception):
    def __init__(self):
        super().__init__("Connection timeout while calling model")


class ForbiddenError(Exception):
    def __init__(self):
        super().__init__("403 forbidden: permission denied")


class SchemaError(Exception):
    def __init__(self):
        super().__init__("pydantic validation error: schema mismatch")


class FakeLLM:
    """假 LLM：按脚本依次返回响应或抛异常。

    ``script`` 是一个列表，每项是 (kind, payload)：
    - ("ok", AIMessage)  -> 返回该消息
    - ("raise", exc)     -> 抛该异常
    """

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, _messages):
        self.calls += 1
        if not self._script:
            return AIMessage(content="默认完成")
        kind, payload = self._script.pop(0)
        if kind == "raise":
            raise payload
        return payload


class FakeToolNode:
    """假 ToolNode：可返回工具消息或抛异常。"""

    def __init__(self, outcome):
        self._outcome = outcome
        self.calls = 0

    async def ainvoke(self, _payload):
        self.calls += 1
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return {"messages": list(self._outcome)}


def _empty_mcp():
    client = MagicMock()

    async def get_tools():
        return []

    client.get_tools = get_tools
    return client


def _state(plan=None):
    return {
        "input": "诊断告警",
        "plan": ["查询 CPU 使用率"] if plan is None else plan,
        "past_steps": [],
        "response": "",
        "failures": [],
    }


def _run_executor(state, fake_llm, fake_tool_node=None):
    """在注入替身的环境下跑一次 executor。"""
    patches = [
        patch.object(exec_mod, "build_llm", return_value=fake_llm),
        patch.object(exec_mod, "get_mcp_client_with_retry",
                     side_effect=lambda: _empty_mcp()),
    ]
    if fake_tool_node is not None:
        patches.append(patch.object(exec_mod, "ToolNode", return_value=fake_tool_node))

    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return asyncio.run(exec_mod.executor(state))


def _tool_message(content):
    from langchain_core.messages import ToolMessage

    return ToolMessage(content=content, tool_call_id="tc1")


# ---------- 1. 正常路径 ----------

def test_happy_path_without_tool_call():
    """LLM 直接给答案，无线程调用。"""
    llm = FakeLLM([("ok", AIMessage(content="CPU 使用率 42%，正常"))])
    out = _run_executor(_state(), llm)

    assert out["plan"] == [], "应消费掉首步"
    assert out["past_steps"] == [("查询 CPU 使用率", "CPU 使用率 42%，正常")]
    assert out["failures"] == [], "正常路径不应留痕"


def test_happy_path_with_tool_call():
    """LLM 调工具 -> 工具返回 -> LLM 汇总。"""
    tool_call_msg = AIMessage(
        content="",
        tool_calls=[{"name": "query_prometheus_alerts", "args": {}, "id": "tc1"}],
    )
    llm = FakeLLM([
        ("ok", tool_call_msg),                    # 决策：调工具
        ("ok", AIMessage(content="汇总：无活跃告警")),  # finalize
    ])
    tool = FakeToolNode([_tool_message('{"success": true, "alerts": []}')])

    out = _run_executor(_state(), llm, tool)

    assert tool.calls == 1
    assert out["past_steps"][0][1] == "汇总：无活跃告警"
    assert out["failures"] == []


# ---------- 2. 瞬时失败 -> 重试 ----------

def test_transient_failure_is_retried():
    """瞬时类失败应重试一次（共 2 次尝试）。"""
    llm = FakeLLM([
        ("raise", TransientError()),               # 第 1 次抛
        ("ok", AIMessage(content="重试后成功")),     # 重试成功
    ])
    out = _run_executor(_state(), llm)

    assert llm.calls == 2, f"应重试一次，实际调用 {llm.calls} 次"
    assert out["past_steps"][0][1] == "重试后成功"
    assert out["failures"] == [], "重试成功后不应留痕"


def test_transient_failure_exhausted_records_failure():
    """瞬时类持续失败：有界停止，失败可见并写入执行历史。"""
    llm = FakeLLM([
        ("raise", TransientError()),
        ("raise", TransientError()),
    ])
    out = _run_executor(_state(), llm)

    assert llm.calls == 2, "重试次数必须有界"
    step, result = out["past_steps"][0]
    assert "执行失败" in result and "transient" in result
    assert len(out["failures"]) == 1
    assert out["failures"][0].kind is FailureKind.TRANSIENT
    assert out["plan"] == [], "失败也必须消费掉该步，避免死循环"


# ---------- 3. 权限/配置 -> 跳过，不重试 ----------

def test_permission_failure_skipped_without_retry():
    """权限/配置问题重试无意义：只尝试一次，标'已跳过'并留痕。"""
    tool_call_msg = AIMessage(
        content="",
        tool_calls=[{"name": "query_metrics", "args": {}, "id": "tc1"}],
    )
    llm = FakeLLM([("ok", tool_call_msg)])
    tool = FakeToolNode(ForbiddenError())

    out = _run_executor(_state(), llm, tool)

    assert tool.calls == 1, "权限类不应重试"
    step, result = out["past_steps"][0]
    assert "已跳过" in result, f"应标为跳过，实际: {result}"
    assert out["failures"][0].kind is FailureKind.PERMISSION


def test_schema_failure_not_retried():
    """schema 恒不匹配：不重试，直接判失败。"""
    tool_call_msg = AIMessage(
        content="",
        tool_calls=[{"name": "t", "args": {}, "id": "tc1"}],
    )
    llm = FakeLLM([("ok", tool_call_msg)])
    tool = FakeToolNode(SchemaError())

    out = _run_executor(_state(), llm, tool)

    assert tool.calls == 1, "schema 类不应重试"
    assert out["failures"][0].kind is FailureKind.SCHEMA
    step, result = out["past_steps"][0]
    assert "执行失败" in result


# ---------- 4. 数据为空 -> 标"无数据"，不编造 ----------

@pytest.mark.parametrize(
    "empty_content",
    ["", "   ", "没有找到相关信息。", "No data available", "未找到相关文档"],
    ids=["empty", "whitespace", "cn_not_found", "en_no_data", "cn_weizhaodao"],
)
def test_empty_data_is_flagged(empty_content):
    """空/无匹配结果应被标注为[无数据]，并写入留痕，避免下游编造。"""
    llm = FakeLLM([("ok", AIMessage(content=empty_content))])
    out = _run_executor(_state(), llm)

    step, result = out["past_steps"][0]
    assert result.startswith("[无数据]"), f"未标注无数据: {result!r}"
    assert len(out["failures"]) == 1
    assert out["failures"][0].kind is FailureKind.EMPTY_DATA


def test_non_empty_result_not_flagged():
    """有数据时不得误标。"""
    llm = FakeLLM([("ok", AIMessage(content="CPU 使用率 42%"))])
    out = _run_executor(_state(), llm)

    assert not out["past_steps"][0][1].startswith("[无数据]")
    assert out["failures"] == []


# ---------- 5. 结果汇总失败 -> 回退工具原始结果 ----------

def test_finalize_failure_falls_back_to_raw_tool_result():
    """LLM 汇总失败时不得丢数据：回退为工具原始输出并留痕。"""
    tool_call_msg = AIMessage(
        content="",
        tool_calls=[{"name": "t", "args": {}, "id": "tc1"}],
    )
    llm = FakeLLM([
        ("ok", tool_call_msg),
        ("raise", SchemaError()),   # finalize 失败（不重试）
    ])
    tool = FakeToolNode([_tool_message("原始指标数据: cpu=0.42")])

    out = _run_executor(_state(), llm, tool)

    step, result = out["past_steps"][0]
    assert "原始指标数据" in result, f"应回退到工具原始结果，实际: {result}"
    assert len(out["failures"]) == 1, "汇总失败应留痕"


# ---------- 6. 兜底与边界 ----------

def test_empty_plan_is_noop():
    llm = FakeLLM([])
    out = _run_executor(_state(plan=[]), llm)
    assert out == {}
    assert llm.calls == 0


def test_unexpected_exception_is_caught_and_classified():
    """未预期异常也要被分类留痕，不能裸抛打断图执行。

    未识别类按"保守可重试"处理，故需两次抛错才会走到失败收尾。
    """
    llm = FakeLLM([
        ("raise", ValueError("完全没见过的错误")),
        ("raise", ValueError("完全没见过的错误")),
    ])
    out = _run_executor(_state(), llm)

    assert llm.calls == 2, "UNKNOWN 属于可重试，应有界重试一次"
    assert len(out["failures"]) == 1
    assert out["failures"][0].kind is FailureKind.UNKNOWN
    assert "执行失败" in out["past_steps"][0][1]


def test_unknown_failure_retries_then_succeeds():
    """未识别类失败重试一次后成功，不留痕。"""
    llm = FakeLLM([
        ("raise", ValueError("偶发怪错")),
        ("ok", AIMessage(content="重试成功")),
    ])
    out = _run_executor(_state(), llm)

    assert llm.calls == 2
    assert out["past_steps"][0][1] == "重试成功"
    assert out["failures"] == []


def test_mcp_tool_fetch_failure_degrades_gracefully():
    """MCP 取工具失败 -> 走兜底异常分支，不影响图继续。"""
    llm = FakeLLM([])

    async def boom():
        raise RuntimeError("mcp down")

    with patch.object(exec_mod, "build_llm", return_value=llm), \
         patch.object(exec_mod, "get_mcp_client_with_retry", side_effect=boom):
        out = asyncio.run(exec_mod.executor(_state()))

    assert out["plan"] == []
    assert len(out["failures"]) == 1
