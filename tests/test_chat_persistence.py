# tests/test_chat_persistence.py
"""对话侧持久化：跨实例（模拟重启）仍能读到会话历史。"""
import asyncio
import sys
from unittest.mock import MagicMock

sys.modules.setdefault("app.services.vector_store_manager", MagicMock())

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.outputs import ChatResult, ChatGeneration  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

from app.services.rag_agent_service import RagAgentService  # noqa: E402


@tool
def _fake_mcp_tool() -> str:
    """假的 MCP 工具（仅用于测试 attach 时的初始化标记）。"""
    return "fake"


class StubModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "stub"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="stub-reply-marker"))])


def _new_service():
    """构造一个可用 attach_checkpointer 的服务实例（模拟 __init__ 已执行）。"""
    svc = RagAgentService.__new__(RagAgentService)
    svc.model = StubModel()
    svc.tools = []
    svc.mcp_tools = []
    svc.agent = None
    svc._agent_initialized = False
    return svc


def test_attach_checkpointer_preserves_lazy_mcp_init(tmp_path):
    """mcp_tools 为空时 attach 不应把 agent 标记为已初始化，否则 _initialize_agent 早退、MCP 工具永不加载。"""
    db = str(tmp_path / "cp-lazy.sqlite")

    async def run():
        async with AsyncSqliteSaver.from_conn_string(db) as cp:
            # mcp_tools 为空 → attach 后仍待懒加载
            svc_empty = _new_service()
            assert svc_empty.mcp_tools == []
            svc_empty.attach_checkpointer(cp)
            assert svc_empty._agent_initialized is False, "MCP 未加载时，懒加载必须仍待执行"

            # mcp_tools 非空 → attach 后即视为已初始化
            svc_loaded = _new_service()
            svc_loaded.mcp_tools = [_fake_mcp_tool]
            svc_loaded.attach_checkpointer(cp)
            assert svc_loaded._agent_initialized is True, "MCP 已加载时，attach 后应标记初始化完成"

    asyncio.run(run())


def test_session_history_survives_new_instance(tmp_path):
    """新实例（进程重启）挂同一 db，应读得到旧会话历史（含助手回复，证明从磁盘读回）。"""
    db = str(tmp_path / "cp.sqlite")

    async def phase1():
        async with AsyncSqliteSaver.from_conn_string(db) as cp:
            svc = _new_service()
            svc.attach_checkpointer(cp)
            # 直接往图里跑一轮，制造一条历史
            await svc.agent.ainvoke(
                {"messages": [{"role": "user", "content": "你好"}]},
                {"configurable": {"thread_id": "sess-p"}},
            )

    async def phase2():
        async with AsyncSqliteSaver.from_conn_string(db) as cp:
            svc = _new_service()
            svc.attach_checkpointer(cp)
            return await svc.get_session_history("sess-p")

    asyncio.run(phase1())
    history = asyncio.run(phase2())
    assert history, "跨实例应能读到会话历史（持久化生效）"
    assert any("你好" in h["content"] for h in history)
    # 助手的 stub 回复也应被持久化读回，证明新实例是从磁盘恢复的完整历史
    assert any("stub-reply-marker" in h["content"] for h in history)


def test_clear_session_removes_history(tmp_path):
    db = str(tmp_path / "cp2.sqlite")

    async def run():
        async with AsyncSqliteSaver.from_conn_string(db) as cp:
            svc = _new_service()
            svc.attach_checkpointer(cp)
            await svc.agent.ainvoke(
                {"messages": [{"role": "user", "content": "待清空"}]},
                {"configurable": {"thread_id": "sess-c"}},
            )
            ok = await svc.clear_session("sess-c")
            assert ok is True
            return await svc.get_session_history("sess-c")

    assert asyncio.run(run()) == []
