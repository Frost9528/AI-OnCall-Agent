"""告警 webhook：接收 Alertmanager 推送，自动派发排障任务（路径1 入口）。"""

from datetime import datetime
import hashlib
import hmac

from fastapi import APIRouter, Header, HTTPException
from loguru import logger

from app.config import config
from app.models.aiops import AlertmanagerPayload
from app.models.task import TriggerSource
from app.services.task_dispatcher import dispatcher

router = APIRouter()


def _build_input(payload: AlertmanagerPayload) -> str:
    lines = [f"系统收到告警（{payload.status}），请排查："]
    for a in payload.alerts:
        raw_labels = a.get("labels")
        raw_annotations = a.get("annotations")
        labels = raw_labels if isinstance(raw_labels, dict) else {}
        ann = raw_annotations if isinstance(raw_annotations, dict) else {}
        lines.append(
            f"- {labels.get('alertname', 'unknown')} "
            f"@ {labels.get('instance', 'unknown')} "
            f"[{labels.get('severity', 'unknown')}] "
            f"{ann.get('summary', '')}"
        )
    return "\n".join(lines)


@router.post("/aiops/webhook")
async def alert_webhook(
    payload: AlertmanagerPayload,
    authorization: str | None = Header(default=None),
):
    """Alertmanager 告警入口：验证 shared secret 后自动派发排障任务。"""
    if not config.alert_webhook_token:
        raise HTTPException(status_code=503, detail="Alert webhook authentication is not configured")
    # compare_digest 对含非 ASCII 的 str 会抛 TypeError（→ 500）；先编码为 bytes 再比较
    expected = f"Bearer {config.alert_webhook_token}".encode("utf-8")
    if not authorization or not hmac.compare_digest(authorization.encode("utf-8"), expected):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if payload.status != "firing" or not payload.alerts:
        return {"code": 200, "message": "ignored", "data": {"dispatched": 0}}

    task_input = _build_input(payload)
    # time_anchor 取告警 startsAt（时间语义：查告警当时窗口，而非 now）
    anchor = None
    starts = payload.alerts[0].get("startsAt")
    if isinstance(starts, str) and starts:
        try:
            anchor = datetime.fromisoformat(starts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            logger.warning(f"无法解析告警 startsAt: {starts}")

    fingerprints = sorted(
        (str(alert.get("fingerprint")) for alert in payload.alerts if alert.get("fingerprint"))
    )
    # 幂等键：同一条告警的重投必须去重（同一份 body → 同一个键），但不同来源的告警
    # 不能因 fingerprint/startsAt 巧合而互相吞掉，故把 receiver/groupKey 一并摘要。
    idempotency_source = "|".join(
        part
        for part in ("|".join(fingerprints), payload.receiver or "", payload.groupKey or "")
        if part
    )
    idempotency_key = None
    if idempotency_source:
        idempotency_key = hashlib.sha256(
            f"{idempotency_source}:{starts or ''}".encode("utf-8")
        ).hexdigest()

    task = await dispatcher.dispatch(
        task_input,
        trigger=TriggerSource.ALERT,
        session_id="alert",
        parent_session=None,
        time_anchor=anchor,
        idempotency_key=idempotency_key,
    )
    logger.info(f"告警触发排障任务 {task.task_id}")
    return {"code": 200, "message": "success", "data": {"task_id": task.task_id}}
