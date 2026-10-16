"""对话侧工具：非阻塞派发、取报告、列任务。"""
import asyncio
import importlib
import sys
import time
from unittest.mock import MagicMock, patch

sys.modules.setdefault("app.services.vector_store_manager", MagicMock())

from app.models.task import TaskRecord, TaskStatus, TriggerSource  # noqa: E402
from app.services.task_registry import TaskRegistry  # noqa: E402
from langchain.tools import ToolRuntime  # noqa: E402

# 注意：不能用 ``import app.tools.run_aiops as mod``——app/tools/__init__.py 的
# ``from ... import run_aiops`` 会以工具对象遮蔽子模块，导致拿到的是 StructuredTool。
# 须用 importlib 取真实模块，才能 patch 其模块级的 dispatcher/task_registry。
run_aiops_mod = importlib.import_module("app.tools.run_aiops")


def FakeRuntime(thread_id):
    """构造一个最小可用的 ToolRuntime（runtime 参数会被 pydantic 校验为真实 dataclass）。"""
    return ToolRuntime(
        state=None,
        context=None,
        config={"configurable": {"thread_id": thread_id}},
        stream_writer=None,
        tool_call_id=None,
        store=None,
    )


class FakeDispatcher:
    def __init__(self):
        self.calls = []

    async def dispatch(self, user_input, **kw):
        self.calls.append((user_input, kw))
        return TaskRecord(task_id="t-fake", session_id=kw["session_id"],
                          thread_id="t-fake", input=user_input,
                          trigger=kw["trigger"], parent_session=kw.get("parent_session"))


def test_run_aiops_returns_task_id_without_blocking():
    """非阻塞：立即返回 task_id，不 await 排障结果。"""
    fake = FakeDispatcher()
    with patch.object(run_aiops_mod, "dispatcher", fake):
        t0 = time.perf_counter()
        out = asyncio.run(run_aiops_mod.run_aiops.ainvoke(
            {"target": "查下 web-01", "runtime": FakeRuntime("sess-7")}
        ))
        elapsed = time.perf_counter() - t0
    assert elapsed < 0.5
    assert "t-fake" in out
    assert fake.calls[0][1]["trigger"] is TriggerSource.CHAT
    assert fake.calls[0][1]["session_id"] == "sess-7"
    assert fake.calls[0][1]["parent_session"] == "sess-7", "应带上回填目标"


def test_get_task_report_returns_body_and_friendly_error(tmp_path):
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    reg.create(TaskRecord(task_id="t-1", session_id="s", thread_id="t-1",
                          input="x", status=TaskStatus.DONE, report="# 报告正文"))
    gtr = importlib.import_module("app.tools.get_task_report")
    with patch.object(gtr, "task_registry", reg):
        assert "报告正文" in asyncio.run(gtr.get_task_report.ainvoke({
            "task_id": "t-1", "runtime": FakeRuntime("s")
        }))
        out = asyncio.run(gtr.get_task_report.ainvoke({
            "task_id": "nope", "runtime": FakeRuntime("s")
        }))
        assert "不存在" in out
        private = asyncio.run(gtr.get_task_report.ainvoke({
            "task_id": "t-1", "runtime": FakeRuntime("another-session")
        }))
        assert "不属于当前会话" in private, "不存在应给友好错误而非抛异常"


def test_list_my_tasks_lists_session_tasks(tmp_path):
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    reg.create(TaskRecord(task_id="t-a", session_id="sess-1", parent_session="sess-1", thread_id="t-a",
                          input="查日志", status=TaskStatus.DONE, report="r"))
    lmt = importlib.import_module("app.tools.list_my_tasks")
    with patch.object(lmt, "task_registry", reg):
        out = lmt.list_my_tasks.invoke({"runtime": FakeRuntime("sess-1")})
    assert "t-a" in out
    assert "查日志" in out


def test_chat_only_tools_registered_in_default_set():
    """对话侧三工具须注册进 DEFAULT_LOCAL_AGENT_TOOLS（供对话 Agent 绑定）。"""
    from app.tools import DEFAULT_LOCAL_AGENT_TOOLS

    names = {t.name for t in DEFAULT_LOCAL_AGENT_TOOLS}
    assert {"run_aiops", "get_task_report", "list_my_tasks"} <= names, \
        "对话侧三工具应注册进 DEFAULT_LOCAL_AGENT_TOOLS"


def test_aiops_nodes_exclude_chat_only_tools():
    """P-E-R 三节点必须真的过滤掉对话侧工具（防排障递归调用排障）。

    直接检查节点源码：若谁把过滤删了，本测试必须失败。
    （不启图 —— 启图需 Milvus/LLM，代价过高；这里校验的是过滤的存在性。）
    """
    import inspect
    import importlib

    for mod_name in ("app.agent.aiops.planner",
                     "app.agent.aiops.executor",
                     "app.agent.aiops.replanner"):
        mod = importlib.import_module(mod_name)
        src = inspect.getsource(mod)
        assert "DEFAULT_LOCAL_AGENT_TOOLS" in src, f"{mod_name} 应引用默认工具集"
        assert "_AGENT_ONLY_TOOLS" in src, (
            f"{mod_name} 未引用 _AGENT_ONLY_TOOLS —— 对话侧工具未被排除，排障会递归调用排障"
        )
        # 进一步确认是"过滤"而非"仅导入"：必须同时出现两者且构成排除式
        assert "not in _AGENT_ONLY_TOOLS" in src, (
            f"{mod_name} 缺少 `not in _AGENT_ONLY_TOOLS` 过滤式"
        )
