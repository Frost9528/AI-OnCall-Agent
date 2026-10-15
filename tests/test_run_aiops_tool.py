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
        assert "报告正文" in gtr.get_task_report.invoke({"task_id": "t-1"})
        out = gtr.get_task_report.invoke({"task_id": "nope"})
        assert "不存在" in out, "不存在应给友好错误而非抛异常"


def test_list_my_tasks_lists_session_tasks(tmp_path):
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    reg.create(TaskRecord(task_id="t-a", session_id="sess-1", thread_id="t-a",
                          input="查日志", status=TaskStatus.DONE, report="r"))
    lmt = importlib.import_module("app.tools.list_my_tasks")
    with patch.object(lmt, "task_registry", reg):
        out = lmt.list_my_tasks.invoke({"runtime": FakeRuntime("sess-1")})
    assert "t-a" in out
    assert "查日志" in out


def test_agent_only_tools_excluded_from_aiops_nodes():
    """对话侧三工具须在 DEFAULT_LOCAL_AGENT_TOOLS 里，但被 P-E-R 过滤掉（防递归排障）。"""
    from app.tools import DEFAULT_LOCAL_AGENT_TOOLS, _AGENT_ONLY_TOOLS

    names = {t.name for t in DEFAULT_LOCAL_AGENT_TOOLS}
    assert {"run_aiops", "get_task_report", "list_my_tasks"} <= names, \
        "对话侧三工具应注册进 DEFAULT_LOCAL_AGENT_TOOLS"

    filtered = {t.name for t in DEFAULT_LOCAL_AGENT_TOOLS if t.name not in _AGENT_ONLY_TOOLS}
    assert "run_aiops" not in filtered, "执行体必须排除 run_aiops（防排障递归调用排障）"
    assert "get_task_report" not in filtered
    assert "list_my_tasks" not in filtered
