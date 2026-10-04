"""对话流式重试语义测试（P1-1）

核心口径（PLAN §P1-1）：首 token 前失败 → 重试；**已推流后失败 → 不重试**，
改为降级提示。用假 agent 精确控制"何时抛错"，不消耗真实 LLM。
"""

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("app.services.vector_store_manager", MagicMock())

from langchain_core.messages import AIMessageChunk  # noqa: E402

from app.services.rag_agent_service import RagAgentService  # noqa: E402


class TransientError(Exception):
    def __init__(self):
        super().__init__("Connection timeout while calling model")


class SchemaError(Exception):
    def __init__(self):
        super().__init__("pydantic validation error: schema mismatch")


def _chunk(text, node="agent"):
    """构造一个能被 query_stream 识别的 AIMessageChunk。

    content_blocks 是只读派生属性（由 content 计算而来），
    所以直接传 content 字符串即可产出 {'type': 'text'} 块。
    """
    return AIMessageChunk(content=text), {"langgraph_node": node}


class FakeAgent:
    """假 agent：按脚本推流。script 每项是 'chunk' 或 ('raise', exc)。

    每次调用消耗一个脚本条目；脚本耗尽即正常结束。
    """

    def __init__(self, scripts):
        # scripts: list[list] —— 每次 astream 调用对应一个脚本
        self._scripts = list(scripts)
        self.calls = 0

    async def astream(self, input, config, stream_mode):
        self.calls += 1
        if not self._scripts:
            return
        for item in self._scripts.pop(0):
            if isinstance(item, tuple) and item and item[0] == "raise":
                raise item[1]
            yield item


def _service(agent):
    """构造一个已初始化的服务，绕过真实 agent 构建。"""
    svc = RagAgentService(streaming=True)
    svc.agent = agent

    async def noop_init():
        return None

    svc._initialize_agent = noop_init
    return svc


def _collect(svc, question="你好"):
    async def run():
        out = []
        async for ev in svc.query_stream(question, session_id="s1"):
            out.append(ev)
        return out

    return asyncio.run(run())


# ---------- 1. 正常流式 ----------

def test_normal_stream_yields_content_then_complete():
    agent = FakeAgent([[_chunk("你好"), _chunk("世界")]])
    out = _collect(_service(agent))

    assert [e["type"] for e in out] == ["content", "content", "complete"]
    assert out[0]["data"] == "你好"
    assert out[1]["data"] == "世界"
    assert agent.calls == 1


# ---------- 2. 首 token 前失败 -> 重试 ----------

def test_retry_before_first_token_succeeds():
    """第一次立刻抛瞬时错误，重试后成功 —— 用户只看到一份内容。"""
    agent = FakeAgent([
        [("raise", TransientError())],   # 第 1 次：首 token 前就失败
        [_chunk("重试后成功")],            # 第 2 次：成功
    ])
    out = _collect(_service(agent))

    assert agent.calls == 2, "首 token 前失败应重试"
    types = [e["type"] for e in out]
    assert "content" in types and types[-1] == "complete"
    assert "".join(e["data"] for e in out if e["type"] == "content") == "重试后成功"


def test_schema_failure_before_token_not_retried():
    """schema 类不可重试：只尝试一次，直接报错。"""
    agent = FakeAgent([[("raise", SchemaError())]])
    out = _collect(_service(agent))

    assert agent.calls == 1, "schema 类不应重试"
    assert out[-1]["type"] == "error"


def test_retry_exhausted_yields_error():
    """瞬时类持续失败：有界重试后报错。"""
    agent = FakeAgent([
        [("raise", TransientError())],
        [("raise", TransientError())],
    ])
    out = _collect(_service(agent))

    assert agent.calls == 2, "重试次数应有界（默认 1 次额外尝试）"
    assert out[-1]["type"] == "error"


# ---------- 3. 已推流后失败 -> 不重试，降级提示 ----------

def test_failure_after_streaming_does_not_retry():
    """关键口径：已产出 token 后再失败，绝不重试（避免用户看到重复/矛盾内容）。"""
    agent = FakeAgent([
        [_chunk("已经输出的前半段"), ("raise", TransientError())],
    ])
    out = _collect(_service(agent))

    assert agent.calls == 1, "已推流后不得重试"
    types = [e["type"] for e in out]
    assert "content" in types, "已输出的内容应保留"
    assert types[-1] == "degraded", f"应以降级提示收尾，实际 {types}"
    assert "不完整" in out[-1]["data"]


def test_degraded_message_mentions_incomplete_output():
    agent = FakeAgent([[_chunk("部分回答"), ("raise", TransientError())]])
    out = _collect(_service(agent))
    degraded = [e for e in out if e["type"] == "degraded"]
    assert len(degraded) == 1
    assert degraded[0]["data"].strip() != ""
    assert "detail" in degraded[0], "降级事件应带原始错误便于排查"


def test_streamed_content_preserved_on_failure():
    """已推流的内容必须原样保留，不能被吞掉。"""
    agent = FakeAgent([[_chunk("A"), _chunk("B"), ("raise", TransientError())]])
    out = _collect(_service(agent))
    contents = "".join(e["data"] for e in out if e["type"] == "content")
    assert contents == "AB"


# ---------- 4. 初始化失败 ----------

def test_init_failure_yields_error_without_calling_agent():
    svc = RagAgentService(streaming=True)
    svc.agent = FakeAgent([[ _chunk("never") ]])

    async def boom():
        raise RuntimeError("mcp unavailable")

    svc._initialize_agent = boom

    out = _collect(svc)
    assert out[-1]["type"] == "error"
    assert svc.agent.calls == 0, "初始化失败不应触发 agent 调用"
