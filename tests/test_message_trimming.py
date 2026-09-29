"""上下文修剪 middleware 测试（P0-1）

覆盖：middleware 协议合规、阈值、奇偶分支、系统消息按类型定位、端到端被框架触发。
用 FakeMessagesListChatModel 与直接调用 .before_model，不消耗真实 LLM。
"""

import sys
import types
from unittest.mock import MagicMock

import pytest

# Milvus 未启动时 vector_store_manager 在 import 期硬失败；本测试不涉及检索
sys.modules.setdefault("app.services.vector_store_manager", MagicMock())

from langchain.agents import create_agent  # noqa: E402
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage  # noqa: E402

from app.services.rag_agent_service import trim_messages_middleware  # noqa: E402

RUNTIME = types.SimpleNamespace()  # 该 middleware 不使用 runtime
SYS = SystemMessage(content="sys")


def trim(messages):
    """调用 middleware 的真实入口，返回修剪后的消息列表（去掉 RemoveMessage 哨兵）。"""
    out = trim_messages_middleware.before_model({"messages": messages}, RUNTIME)
    return None if out is None else out["messages"][1:]


def test_is_langchain_middleware():
    assert type(trim_messages_middleware).__module__.startswith("langchain")
    assert callable(trim_messages_middleware.before_model)


def test_at_or_below_threshold_returns_none():
    for n in (1, 5, 7):
        msgs = [SYS] + [HumanMessage(content=f"m{i}") for i in range(n - 1)]
        assert trim(msgs) is None, f"{n} 条不应修剪"


def test_even_length_keeps_seven():
    msgs = [SYS] + [HumanMessage(content=f"m{i}") for i in range(19)]  # 20 条
    kept = trim(msgs)
    assert len(kept) == 7
    assert isinstance(kept[0], SystemMessage)


def test_odd_length_keeps_eight():
    msgs = [SYS] + [HumanMessage(content=f"m{i}") for i in range(20)]  # 21 条
    kept = trim(msgs)
    assert len(kept) == 8
    assert kept[-1].content == "m19"


def test_keeps_most_recent_not_oldest_without_system_message():
    """没有系统消息时，位置 0 是最旧用户消息，不应被当成系统提示永久保留。"""
    msgs = [HumanMessage(content=f"旧{i}") for i in range(21)]
    kept = [m.content for m in trim(msgs)]
    assert "旧0" not in kept
    assert kept[-1] == "旧20"


def test_system_message_kept_even_when_not_first():
    msgs = [HumanMessage(content=f"旧{i}") for i in range(21)] + [SYS]
    kept = trim(msgs)
    assert any(isinstance(m, SystemMessage) for m in kept)


def test_system_message_not_duplicated():
    """系统消息落在最近窗口内时，不应因"提到最前"而出现两次。"""
    msgs = [HumanMessage(content=f"旧{i}") for i in range(4)] + [SYS] + [
        HumanMessage(content=f"新{i}") for i in range(16)
    ]
    kept = trim(msgs)
    assert sum(isinstance(m, SystemMessage) for m in kept) == 1


def test_end_to_end_trimming_fires_through_create_agent():
    fake = FakeMessagesListChatModel(responses=[AIMessage(content="ok")])
    agent = create_agent(fake, tools=[], middleware=[trim_messages_middleware])

    cfg = {"configurable": {"thread_id": "t-e2e"}}
    history = [SYS] + [HumanMessage(content=f"旧消息{i}") for i in range(19)]
    out = agent.invoke({"messages": history + [HumanMessage(content="新问题")]}, cfg)

    contents = [m.content for m in out["messages"]]
    assert len(out["messages"]) < 21, "端到端未修剪"
    assert "旧消息0" not in contents, "最旧消息应被丢弃"
    assert "新问题" in contents
    assert "sys" in contents, "系统消息应保留"
