"""任务派发器：建任务 + 启动（可选后台）执行。

职责边界（见 spec §2.2）：只做派发，不碰 P-E-R 内部、不碰投递。
执行体通过钩子通知投递，故 dispatcher 不 import 投递器。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from loguru import logger

from app.config import config
from app.models.task import TaskRecord, TriggerSource
from app.services.task_registry import TaskRegistry, task_registry

Runner = Callable[[TaskRecord], Awaitable[None]]


class TaskDispatcher:
    """建任务并启动执行。

    background=True 时用 asyncio.create_task 起后台协程——**必须持引用**，
    否则协程可能被 GC 静默取消（asyncio 经典坑）。
    """

    def __init__(
        self,
        registry: Optional[TaskRegistry] = None,
        runner: Optional[Runner] = None,
    ):
        self.registry = registry or task_registry
        self._runner = runner
        self._bg: set[asyncio.Task] = set()

    async def _default_runner(self, task: TaskRecord) -> None:
        from app.services.aiops_service import aiops_service

        async for _ in aiops_service.run_task(task, resume=False):
            pass

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    def pending_background(self) -> int:
        return len(self._bg)

    async def dispatch(
        self,
        user_input: str,
        *,
        trigger: TriggerSource,
        session_id: str,
        owner: str = "anonymous",
        parent_session: Optional[str] = None,
        time_anchor: Optional[datetime] = None,
        idempotency_key: Optional[str] = None,
        background: Optional[bool] = None,
    ) -> TaskRecord:
        """建任务并（可选）后台执行；返回含 task_id 的记录。

        单飞行约束仅对 CHAT 生效（设计 §4.0）：同会话已有 running 排障时，
        不新建任务，直接返回已有任务。
        """
        if idempotency_key:
            existing = self.registry.get_by_idempotency_key(idempotency_key)
            if existing:
                return existing

        if trigger is TriggerSource.CHAT:
            existing = self.registry.find_running_by_session(session_id)
            if existing:
                logger.info(f"[会话 {session_id}] 已有在跑排障 {existing.task_id}，不重复派发")
                return existing

        from app.services.aiops_service import aiops_service

        task, created = aiops_service.create_task(
            user_input,
            session_id=session_id,
            owner=owner,
            parent_session=parent_session,
            time_anchor=time_anchor or datetime.now(timezone.utc),
            trigger=trigger,
            registry=self.registry,
            idempotency_key=idempotency_key,
            return_created=True,
        )

        if not created:
            return task

        runner = self._runner or self._default_runner
        use_bg = config.task_background_execution if background is None else background

        if use_bg:
            self._spawn(self._guarded(task, runner))
            logger.info(f"[任务 {task.task_id}] 已后台派发 trigger={trigger.value}")
        else:
            await self._guarded(task, runner)

        return task

    async def resume(self, task: TaskRecord, background: Optional[bool] = None) -> None:
        """从最新 checkpoint 续跑一个已存在的任务（后台）。

        与 dispatch 的区别：不新建任务、不重置 thread，按原 task_id 的 checkpoint
        续跑，故不会重放已完成步骤（design §4.3②）。

        续跑即重新进入 running：否则任务在运行期间仍标 ejected，
        会被 find_running_by_session 漏掉（单飞行失效），且下次扫描会重复续跑。
        """
        from app.services.aiops_service import aiops_service

        claimed = self.registry.transition_ejected_to_running(task.task_id)
        if claimed is None:
            raise ValueError("Only ejected tasks can resume")
        task = claimed

        async def runner(t: TaskRecord) -> None:
            async for _ in aiops_service.run_task(t, resume=True):
                pass

        use_bg = config.task_background_execution if background is None else background
        if use_bg:
            self._spawn(self._guarded(task, runner))
        else:
            await self._guarded(task, runner)

    async def _guarded(self, task: TaskRecord, runner: Runner) -> None:
        """后台执行的安全包装：异常必须被接住，否则静默丢失。"""
        from app.models.task import TaskStatus

        try:
            await runner(task)
        except asyncio.CancelledError:
            # 服务关闭导致的取消：任务是"可恢复的进行中工作"，不是失败。
            # 标 ejected（可恢复候选），而非 failed（终态、丢弃可恢复性）。
            logger.warning(f"[任务 {task.task_id}] 后台执行被取消（服务关闭），标为可恢复")
            self.registry.mark_ejected(task.task_id)
            raise                      # 不要吞掉取消信号，否则会破坏 asyncio 的关闭语义
        except Exception as e:
            logger.error(f"[任务 {task.task_id}] 后台执行异常: {e}", exc_info=True)
            self.registry.update_status(task.task_id, TaskStatus.FAILED)
            await self._notify_settled(task.task_id)
        else:
            await self._notify_settled(task.task_id)

    async def _notify_settled(self, task_id: str) -> None:
        """标记已结束的 chat 任务待投递。"""
        try:
            from app.services.task_delivery import task_delivery
            delivery = task_delivery
            if delivery.registry is self.registry:
                await delivery.mark_pending(task_id)
            else:
                from app.services.task_delivery import TaskDelivery
                await TaskDelivery(registry=self.registry).mark_pending(task_id)
        except Exception as e:                  # 投递不可用不应影响任务
            logger.warning(f"[任务 {task_id}] 投递钩子失败: {e}")


dispatcher = TaskDispatcher()
