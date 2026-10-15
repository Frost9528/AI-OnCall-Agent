"""run_aiops 工具：对话 Agent 派发排障子任务（非阻塞）。

**不 await 排障结果**——只派发并返回 task_id。原因：排障可能跑几十秒到几分钟，
若阻塞会卡住主对话的 SSE 流（设计 §4.0 明确要避免的"supervisor 卡长工具"）。
结果由任务表承载，用户可追问、模型可按需取（见 get_task_report）。
"""

from langchain.tools import ToolRuntime, tool
from loguru import logger

from app.models.task import TriggerSource
from app.services.task_dispatcher import dispatcher


def _session_from_runtime(runtime: ToolRuntime) -> str:
    """从 ToolRuntime 取会话 id（即对话图的 thread_id，已实测可行）。"""
    cfg = getattr(runtime, "config", None) or {}
    return (cfg.get("configurable") or {}).get("thread_id") or "default"


@tool
async def run_aiops(target: str, runtime: ToolRuntime) -> str:
    """发起一次系统故障排障（异步，不阻塞对话）。

    适用场景：用户要求"排查某个告警/某个服务的故障""诊断系统问题"，
    需要按计划多步调用日志/指标/知识库工具做根因分析时使用。
    普通问答（解释概念、写代码、闲聊）**不要**调用本工具——它代价高、耗时长。
    """
    session_id = _session_from_runtime(runtime)
    task = await dispatcher.dispatch(
        target,
        trigger=TriggerSource.CHAT,
        session_id=session_id,
        parent_session=session_id,      # 结果可回填本会话
    )
    logger.info(f"[会话 {session_id}] run_aiops 派发任务 {task.task_id}")
    return (
        f"已派发排障任务，task_id={task.task_id}，正在后台执行。\n"
        f"请告知用户：排障已开始，完成后会通知；"
        f"稍后可用 get_task_report 工具（参数 task_id=\"{task.task_id}\"）查看完整报告。"
    )
