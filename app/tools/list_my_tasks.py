"""list_my_tasks 工具：列出本会话的排障任务。

模型据此"主动发现"自己派发过的任务（配合通道2 的默认关闭语义：
不注入上下文时，模型靠本工具按需知晓）。
"""

from langchain.tools import ToolRuntime, tool

from app.services.task_registry import task_registry


@tool
def list_my_tasks(runtime: ToolRuntime) -> str:
    """列出当前会话发起过的排障任务及其状态。

    当用户问"我之前让你查的那个怎么样了""有哪些任务在跑"时使用。
    """
    cfg = getattr(runtime, "config", None) or {}
    session_id = (cfg.get("configurable") or {}).get("thread_id")
    if not session_id:
        return "当前会话没有排障任务记录。"
    tasks = task_registry.list_by_parent_session(session_id)
    if not tasks:
        return "当前会话没有排障任务记录。"
    lines = ["当前会话的排障任务："]
    for t in tasks:
        mark = "有报告" if t.report else "无报告"
        lines.append(f"- {t.task_id} [{t.status.value}] {t.input[:40]} ({mark})")
    return "\n".join(lines)
