"""告警 webhook：接收 Alertmanager 推送，自动派发排障任务（路径1 入口）。"""

from fastapi import APIRouter
from loguru import logger

from app.models.aiops import AlertmanagerPayload
from app.models.task import TriggerSource
from app.services.task_dispatcher import dispatcher

router = APIRouter()


def _build_input(payload: AlertmanagerPayload) -> str:
    lines = [f"系统收到告警（{payload.status}），请排查："]
    for a in payload.alerts:
        labels = a.get("labels", {})
        ann = a.get("annotations", {})
        lines.append(
            f"- {labels.get('alertname', 'unknown')} "
            f"@ {labels.get('instance', 'unknown')} "
            f"[{labels.get('severity', 'unknown')}] "
            f"{ann.get('summary', '')}"
        )
    return "\n".join(lines)


@router.post("/aiops/webhook")
async def alert_webhook(payload: AlertmanagerPayload):
    """Alertmanager 告警入口：自动派发排障任务（无对话回填）。"""
    if payload.status != "firing" or not payload.alerts:
        return {"code": 200, "message": "ignored", "data": {"dispatched": 0}}

    task_input = _build_input(payload)
    # time_anchor 取告警 startsAt（时间语义：查告警当时窗口，而非 now）
    anchor = None
    starts = payload.alerts[0].get("startsAt")
    if isinstance(starts, str) and starts:
        try:
            from datetime import datetime
            anchor = datetime.fromisoformat(starts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            logger.warning(f"无法解析告警 startsAt: {starts}")

    task = await dispatcher.dispatch(
        task_input,
        trigger=TriggerSource.ALERT,
        session_id="alert",
        parent_session=None,
        time_anchor=anchor,
    )
    logger.info(f"告警触发排障任务 {task.task_id}")
    return {"code": 200, "message": "success", "data": {"task_id": task.task_id}}
