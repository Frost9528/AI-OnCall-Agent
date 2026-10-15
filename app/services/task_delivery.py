"""任务投递：标记待投递、幂等消费、有界重试。

三个通道（spec §3.4）中本模块负责通道1（状态落库）与通道2 的标记部分；
真正的"注入对话上下文"由对话侧 middleware 调用 consume_pending_for_session 完成。
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from loguru import logger

from app.config import config
from app.models.task import (
    DeliveryStatus, TaskRecord, TERMINAL_STATUSES, TriggerSource,
)
from app.services.task_registry import TaskRegistry, task_registry


class TaskDelivery:
    def __init__(self, registry: Optional[TaskRegistry] = None):
        self.registry = registry or task_registry
        self._force_fail = False            # 测试用：强制投递失败

    async def mark_pending(self, task_id: str) -> None:
        """任务结算后标记待投递。

        仅当：① 任务已终态；② 有回填目标（trigger=chat 且 parent_session 非空）。
        其它情况保持 none（如实：未实现通知出口，不假装发送）。
        """
        task = self.registry.get(task_id)
        if task is None:
            return
        if task.status not in TERMINAL_STATUSES:
            return
        if task.trigger is not TriggerSource.CHAT or not task.parent_session:
            return
        if task.delivery_status is not DeliveryStatus.NONE:
            return                          # 已投递/待投递，不重复标记
        self.registry.set_delivery(task_id, DeliveryStatus.PENDING)
        logger.info(f"[任务 {task_id}] 投递标记 pending 目标={task.parent_session}")

    async def consume_pending_for_session(self, session_id: str) -> List[TaskRecord]:
        """原子认领该会话待注入的交付并标 sent（幂等：第二次返回空）。"""
        return self.registry.claim_pending_for_session(session_id)

    async def deliver_pending(self, limit: int = 20) -> int:
        """扫描 pending 并重试；超限标 failed。返回处理条数。

        注意：等待消费（有父会话、待 middleware 取走）**不算**一次投递尝试，
        故该分支不递增 attempts —— 否则 attempts 会无界增长，且 cap 失去意义。
        attempts 只在真实投递尝试（失败/重试分支）递增。
        """
        rows = self.registry.list_pending_deliveries(limit)
        handled = 0
        for t in rows:
            attempts = (t.delivery_attempts or 0) + 1
            if self._force_fail or not t.parent_session:
                if attempts >= config.max_delivery_attempts:
                    self.registry.set_delivery(
                        t.task_id, DeliveryStatus.FAILED, attempts=attempts
                    )
                    logger.warning(f"[任务 {t.task_id}] 投递超限，标 failed")
                else:
                    self.registry.set_delivery(
                        t.task_id, DeliveryStatus.PENDING, attempts=attempts
                    )
            else:
                # 有父会话：待 middleware 在下一次对话时消费（通道2）。
                # 这是"等待消费"而非"投递尝试"，故不递增 attempts。
                logger.debug(f"[任务 {t.task_id}] 待对话侧消费，保持 pending")
            handled += 1
        return handled


task_delivery = TaskDelivery()
