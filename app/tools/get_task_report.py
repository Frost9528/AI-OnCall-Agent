"""get_task_report 工具：按需取排障报告全文。

这是"轻量通知 + 按需取"（spec 通道2 语义）的取侧：通知只说"任务完成可查"，
模型需要细节时才调本工具取全文——避免把上千 token 的报告塞进每轮上下文。
"""

from langchain_core.tools import tool
from loguru import logger

from app.services.task_registry import task_registry


@tool
def get_task_report(task_id: str) -> str:
    """获取某个排障任务的完整报告。

    Args:
        task_id: 任务 ID（如 t-8f3a1b2c3d4e）

    当用户追问"刚才那个排障结果怎么样""报告里说了什么"时使用本工具。
    """
    task = task_registry.get(task_id)
    if task is None:
        return f"任务不存在: {task_id}。请确认 task_id 是否正确。"
    if not task.report:
        return (
            f"任务 {task_id} 尚未产出报告（当前状态: {task.status.value}）。"
            f"若任务仍在 running，请稍后再查。"
        )
    logger.info(f"读取任务报告: {task_id}")
    return f"# 任务 {task_id} 排障报告\n\n{task.report}"
