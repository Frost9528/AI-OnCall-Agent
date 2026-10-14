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

from app.services.rag_agent_service import RagAgentService  # noqa: E402


class StubModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "stub"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])


def _new_service():
    """构造一个可用 attach_checkpointer 的服务实例（模拟 __init__ 已执行）。"""
    svc = RagAgentService.__new__(RagAgentService)
    svc.model = StubModel()
    svc.tools = []
    svc.mcp_tools = []
    svc.agent = None
    svc._agent_initialized = False
    return svc


def test_session_history_survives_new_instance(tmp_path):
    """新实例（进程重启）挂同一 db，应读得到旧会话历史。"""
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
