"""
通用 Plan-Execute-Replan 服务
基于 LangGraph 官方教程实现

任务持久化（P0-3）：
- graph checkpoint 用 SQLite（跨进程可恢复），由 FastAPI lifespan 传入；
- 任务级状态由 ``task_registry`` 维护，两者靠 ``thread_id`` 关联。
"""

import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator, Dict, Any, Optional

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from loguru import logger

from app.agent.aiops import PlanExecuteState, planner, executor, replanner
from app.agent.aiops.failure import FailureKind
from app.models.task import InterruptMode, TaskRecord, TaskStatus, TriggerSource
from app.services.task_registry import TaskRegistry, task_registry


# 内置诊断模板正文（见文件末尾 default_diagnosis_input()）
_DEFAULT_DIAGNOSIS_TEMPLATE = """诊断当前系统是否存在告警，如果存在告警请详细分析告警原因并生成诊断报告，诊断报告输出格式要求：
                ```
                # 告警分析报告

                ---

                ## 📋 活跃告警清单

                | 告警名称 | 级别 | 目标服务 | 首次触发时间 | 最新触发时间 | 状态 |
                |---------|------|----------|-------------|-------------|------|
                | [告警1名称] | [级别] | [服务名] | [时间] | [时间] | 活跃 |
                | [告警2名称] | [级别] | [服务名] | [时间] | [时间] | 活跃 |

                ---

                ## 🔍 告警根因分析1 - [告警名称]

                ### 告警详情
                - **告警级别**: [级别]
                - **受影响服务**: [服务名]
                - **持续时间**: [X分钟]

                ### 症状描述
                [根据监控指标描述症状]

                ### 日志证据
                [引用查询到的关键日志]

                ### 根因结论
                [基于证据得出的根本原因]

                ---

                ## 🛠️ 处理方案执行1 - [告警名称]

                ### 已执行的排查步骤
                1. [步骤1]
                2. [步骤2]

                ### 处理建议
                [给出具体的处理建议]

                ### 预期效果
                [说明预期的效果]

                ---

                ## 🔍 告警根因分析2 - [告警名称]
                [如果有第2个告警，重复上述格式]

                ---

                ## 📊 结论

                ### 整体评估
                [总结所有告警的整体情况]

                ### 关键发现
                - [发现1]
                - [发现2]

                ### 后续建议
                1. [建议1]
                2. [建议2]

                ### 风险评估
                [评估当前风险等级和影响范围]
                ```

                **重要提醒**：
                - 最终输出必须是纯 Markdown 文本，不要包含 JSON 结构
                - 所有内容必须基于工具查询的真实数据，严禁编造
                - 如果某个步骤失败，在结论中如实说明，不要跳过"""


# 节点名称常量
NODE_PLANNER = "planner"
NODE_EXECUTOR = "executor"
NODE_REPLANNER = "replanner"


def new_task_id() -> str:
    """生成任务 ID（短且可读，便于日志与手工 resume）。"""
    return f"t-{uuid.uuid4().hex[:12]}"


def default_diagnosis_input() -> str:
    """内置诊断模板（调用方未自定义排障目标时使用）。"""
    from textwrap import dedent

    # 模板正文较长，单独放在文件末尾的常量里，避免与逻辑混杂
    return dedent(_DEFAULT_DIAGNOSIS_TEMPLATE).strip()


class AIOpsService:
    """通用 Plan-Execute-Replan 服务"""

    def __init__(self, checkpointer: Optional[Any] = None):
        """初始化服务

        Args:
            checkpointer: 外部注入的 checkpointer（通常为 AsyncSqliteSaver）。
                未注入时用 MemorySaver（进程内），保证服务仍能启动。
        """
        self.checkpointer = checkpointer or MemorySaver()
        self.graph = self._build_graph()
        self._persistent = not isinstance(self.checkpointer, MemorySaver)
        logger.info(
            f"Plan-Execute-Replan Service 初始化完成 "
            f"(checkpointer={type(self.checkpointer).__name__}, 持久化={self._persistent})"
        )

    def _build_graph(self):
        """构建 Plan-Execute-Replan 工作流"""
        logger.info("构建工作流图...")
        # 创建状态图
        workflow = StateGraph(PlanExecuteState)

        # 添加节点
        workflow.add_node(NODE_PLANNER, planner)      # 制定计划
        workflow.add_node(NODE_EXECUTOR, executor)  # 执行步骤
        workflow.add_node(NODE_REPLANNER, replanner)  # 重新规划

        # 设置入口点
        workflow.set_entry_point(NODE_PLANNER)

        # 定义边
        workflow.add_edge(NODE_PLANNER, NODE_EXECUTOR)     # planner -> executor
        workflow.add_edge(NODE_EXECUTOR, NODE_REPLANNER)   # executor -> replanner

        # replanner 的条件边
        def should_continue(state: PlanExecuteState) -> str:
            """判断是否继续执行"""
            # 如果已经生成了最终响应，结束
            if state.get("response"):
                logger.info("已生成最终响应，结束流程")
                return END

            # 如果还有计划步骤，继续执行
            plan = state.get("plan", [])
            if plan:
                logger.info(f"继续执行，剩余 {len(plan)} 个步骤")
                return NODE_EXECUTOR

            # 计划为空但没有响应，返回 replanner 生成响应
            logger.info("计划执行完毕，生成最终响应")
            return END

        workflow.add_conditional_edges(
            NODE_REPLANNER,
            should_continue,
            {
                NODE_EXECUTOR: NODE_EXECUTOR,
                END: END
            }
        )

        # 编译工作流
        compiled_graph = workflow.compile(checkpointer=self.checkpointer)

        logger.info("工作流图构建完成")
        return compiled_graph

    # ---------- 任务生命周期 ----------

    def attach_checkpointer(self, checkpointer: Any) -> None:
        """在 lifespan 中挂载持久化 checkpointer 并重建图。

        图在编译时绑定 checkpointer，无法替换实例，故重建图。
        """
        self.checkpointer = checkpointer
        self.graph = self._build_graph()
        self._persistent = not isinstance(checkpointer, MemorySaver)
        logger.info(f"已挂载 checkpointer: {type(checkpointer).__name__}（持久化={self._persistent}）")

    def create_task(
        self,
        user_input: str,
        session_id: str = "default",
        owner: str = "anonymous",
        parent_session: Optional[str] = None,
        time_anchor: Optional[datetime] = None,
        trigger: TriggerSource = TriggerSource.MANUAL,
        registry: Optional[TaskRegistry] = None,
        idempotency_key: Optional[str] = None,
        return_created: bool = False,
    ) -> TaskRecord | tuple[TaskRecord, bool]:
        """登记一个新排障任务。

        thread_id 用 task_id（不再是 session_id）——同一会话可先后有多个任务，
        各自独立的图现场，避免互相覆盖 checkpoint。

        time_anchor 记录"任务被创建的那一刻"：时间敏感查询（查某告警当时活跃窗口）
        恢复时应沿用它而非按 now 重查，避免答非所问（E5）。

        registry 默认用全局单例；派发器可注入自有注册表（测试/隔离场景）。
        """
        task_id = new_task_id()
        task = TaskRecord(
            task_id=task_id,
            owner=owner,
            session_id=session_id,
            parent_session=parent_session,
            thread_id=task_id,
            input=user_input,
            status=TaskStatus.RUNNING,
            checkpoint_ref=task_id,
            time_anchor=time_anchor or datetime.now(timezone.utc),
            trigger=trigger,
            idempotency_key=idempotency_key,
        )
        task, created = (registry or task_registry).create_idempotent(task)
        if created:
            logger.info(f"任务已登记: {task.task_id} (session={task.session_id}, status={task.status.value})")
        if return_created:
            return task, created
        return task

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        return task_registry.get(task_id)

    def list_resumable_tasks(self) -> list[TaskRecord]:
        """待恢复清单：只含 ejected（终态不重复展示）。"""
        return task_registry.list_resumable()

    def startup_scan(self) -> list[TaskRecord]:
        """服务启动时扫描：把遗留 running 标为 ejected，并收敛超 TTL 的。

        Returns:
            list[TaskRecord]: 当前待恢复清单（仅 ejected）
        """
        from app.config import config

        task_registry.mark_running_as_ejected()
        task_registry.expire_stale(config.task_ttl_seconds)
        resumable = task_registry.list_resumable()
        if resumable:
            logger.warning(f"启动扫描：发现 {len(resumable)} 个待恢复任务（ejected）")
        return resumable

    async def classify_interrupt_modes(self, tasks: list[TaskRecord]) -> None:
        """为待恢复任务判定 auto/manual（E5）。

        判定依据是**失败留痕**而非猜测：权限/配置、schema 类失败意味着
        "继续走也不会好转，需要人看一眼"，故为 manual；纯瞬时类（超时/5xx）
        在只读工具集下可直接续，为 auto。无失败记录 → auto。
        """
        manual_kinds = {FailureKind.PERMISSION, FailureKind.SCHEMA}

        for task in tasks:
            mode = InterruptMode.AUTO
            try:
                state = await self.graph.aget_state(
                    {"configurable": {"thread_id": task.thread_id}}
                )
                failures = (state.values or {}).get("failures", []) if state else []
                for raw in failures:
                    kind = raw.get("kind") if isinstance(raw, dict) else getattr(raw, "kind", None)
                    if kind is None:
                        continue
                    # checkpoint 往返后 kind 可能是字符串
                    value = kind.value if isinstance(kind, FailureKind) else str(kind)
                    if value in {k.value for k in manual_kinds}:
                        mode = InterruptMode.MANUAL
                        break
            except Exception as e:
                # 判不出来就保守当作需要人工决策，不擅自自动续跑
                logger.warning(f"任务 {task.task_id} 恢复模式判定失败，保守标 manual: {e}")
                mode = InterruptMode.MANUAL

            task_registry.set_interrupt_mode(task.task_id, mode)

    # ---------- 执行 ----------

    async def run_task(
        self,
        task: TaskRecord,
        resume: bool = False,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """执行/恢复一个任务。

        Args:
            task: 任务记录（提供 thread_id 与 input）
            resume: True 时从最新 checkpoint 续跑（``astream(None, ...)``），
                已完成节点不会重放；False 时以初始状态开始。

        Yields:
            Dict[str, Any]: 流式事件
        """
        session_id = task.session_id
        config_dict = {"configurable": {"thread_id": task.thread_id}}

        logger.info(
            f"[任务 {task.task_id}][会话 {session_id}] "
            f"{'恢复' if resume else '开始'}执行: {task.input[:80]}"
        )

        # 恢复前确认 checkpoint 存在，避免对空现场续跑
        if resume:
            state = await self.graph.aget_state(config_dict)
            if not state or not state.values:
                logger.error(f"[任务 {task.task_id}] 无可恢复的 checkpoint，任务标记 failed")
                task_registry.update_status(task.task_id, TaskStatus.FAILED)
                yield {
                    "type": "error",
                    "stage": "resume_failed",
                    "message": "该任务没有可恢复的执行现场（checkpoint 缺失）",
                }
                return
            stream_input: Any = None
            logger.info(f"[任务 {task.task_id}] 断点续跑，next={state.next}")
        else:
            stream_input = {
                "input": task.input,
                "plan": [],
                "past_steps": [],
                "response": "",
                "failures": [],
                "time_anchor": task.time_anchor.isoformat() if task.time_anchor else "",
            }

        try:
            async for event in self.graph.astream(
                input=stream_input,
                config=config_dict,
                stream_mode="updates",
            ):
                for node_name, node_output in event.items():
                    logger.info(f"节点 '{node_name}' 输出事件")

                    if node_name == NODE_PLANNER:
                        yield self._format_planner_event(node_output)
                    elif node_name == NODE_EXECUTOR:
                        yield self._format_executor_event(node_output)
                    elif node_name == NODE_REPLANNER:
                        yield self._format_replanner_event(node_output)

            # 取最终状态（AsyncSqliteSaver 必须用 aget_state）
            final_state = await self.graph.aget_state(config_dict)
            final_response = ""
            if final_state and final_state.values:
                final_response = final_state.values.get("response", "")

            task_registry.update_status(
                task.task_id,
                TaskStatus.DONE,
                result_ref=f"aiops_tasks/{task.task_id}#report",
                report=final_response or None,
            )

            yield {
                "type": "complete",
                "stage": "complete",
                "message": "任务执行完成",
                "task_id": task.task_id,
                "response": final_response,
            }

            logger.info(f"[任务 {task.task_id}] 任务执行完成")

        except Exception as e:
            # 进程级异常：是否可恢复取决于是否为中断。此处保守标 failed，
            # 由调用方（或重启扫描）根据是否需要恢复再决定转 ejected。
            logger.error(f"[任务 {task.task_id}] 任务执行失败: {e}", exc_info=True)
            task_registry.update_status(task.task_id, TaskStatus.FAILED)
            yield {
                "type": "error",
                "stage": "error",
                "message": f"任务执行出错: {str(e)}",
                "task_id": task.task_id,
            }

    async def diagnose(
        self,
        session_id: str = "default",
        user_input: str | None = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        AIOps 诊断接口（兼容旧接口）

        Args:
            session_id: 会话ID
            user_input: 自定义排障任务描述；为 None 时使用内置诊断模板

        Yields:
            Dict[str, Any]: 诊断过程的流式事件
        """
        # 使用固定的 AIOps 任务描述（调用方未自定义时）
        from textwrap import dedent
        aiops_task = default_diagnosis_input()

        task_input = user_input if user_input is not None else aiops_task

        # 登记任务后执行；旧接口不返回 task_id，但任务已在注册表可查
        record = self.create_task(
            task_input, session_id=session_id, parent_session=session_id
        )
        logger.info(f"[会话 {session_id}] 已创建任务 {record.task_id}")

        async for event in self.run_task(record, resume=False):
            # 转换事件格式以兼容旧的 API
            if event.get("type") == "complete":
                # 将 response 包装为 diagnosis 格式
                yield {
                    "type": "complete",
                    "stage": "diagnosis_complete",
                    "message": "诊断流程完成",
                    "task_id": record.task_id,
                    "diagnosis": {
                        "status": "completed",
                        "report": event.get("response", "")
                    }
                }
            else:
                yield event

    async def resume_task(
        self,
        task_id: str,
        owner: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """恢复一个被中断的任务，从最新 checkpoint 续跑。

        Args:
            task_id: 任务 ID
            owner: 调用方身份；提供时须与任务 owner 匹配（task 级越权防护，§6.1#5b）。
                本期 owner 为占位值，鉴权完整实现属 P2，但接口已预留。

        Yields:
            Dict[str, Any]: 与首次执行一致的流式事件
        """
        task = task_registry.get(task_id)
        if task is None:
            yield {"type": "error", "stage": "not_found", "message": f"任务不存在: {task_id}"}
            return

        if owner is not None and task.owner != owner:
            logger.warning(f"[任务 {task_id}] owner 不匹配，拒绝恢复")
            yield {
                "type": "error",
                "stage": "forbidden",
                "message": "无权恢复该任务",
            }
            return

        if task.is_terminal:
            yield {
                "type": "error",
                "stage": "invalid_state",
                "message": f"任务已处于终态（{task.status.value}），不可恢复",
                "task_id": task_id,
            }
            return

        claimed = task_registry.transition_ejected_to_running(task_id)
        if claimed is None:
            current = task_registry.get(task_id)
            if current is None:
                yield {"type": "error", "stage": "not_found", "message": f"任务不存在: {task_id}"}
            else:
                yield {
                    "type": "error",
                    "stage": "invalid_state",
                    "message": f"任务状态为 {current.status.value}，仅 ejected 任务可恢复",
                    "task_id": task_id,
                }
            return

        async for event in self.run_task(claimed, resume=True):
            yield event

    def cancel_task(self, task_id: str) -> Optional[TaskRecord]:
        """用户主动中止任务（即时停，而非被动等 TTL）。"""
        task = task_registry.get(task_id)
        if task is None or task.is_terminal:
            return None
        return task_registry.update_status(task_id, TaskStatus.CANCELLED)

    def _format_planner_event(self, state: Dict | None) -> Dict:
        """格式化 Planner 节点事件"""
        if not state:
            return {
                "type": "status",
                "stage": "planner",
                "message": "规划节点执行中"
            }

        plan = state.get("plan", [])

        return {
            "type": "plan",
            "stage": "plan_created",
            "message": f"执行计划已制定，共 {len(plan)} 个步骤",
            "plan": plan
        }

    def _format_executor_event(self, state: Dict | None) -> Dict:
        """格式化 Executor 节点事件"""
        if not state:
            return {
                "type": "status",
                "stage": "executor",
                "message": "执行节点运行中"
            }

        plan = state.get("plan", [])
        past_steps = state.get("past_steps", [])

        if past_steps:
            last_step, _ = past_steps[-1]
            return {
                "type": "step_complete",
                "stage": "step_executed",
                "message": f"步骤执行完成 ({len(past_steps)}/{len(past_steps) + len(plan)})",
                "current_step": last_step,
                "remaining_steps": len(plan)
            }
        else:
            return {
                "type": "status",
                "stage": "executor",
                "message": "开始执行步骤"
            }

    def _format_replanner_event(self, state: Dict | None) -> Dict:
        """格式化 Replanner 节点事件"""
        if not state:
            return {
                "type": "status",
                "stage": "replanner",
                "message": "评估节点运行中"
            }

        response = state.get("response", "")
        plan = state.get("plan", [])

        if response:
            # 已生成最终响应
            return {
                "type": "report",
                "stage": "final_report",
                "message": "最终报告已生成",
                "report": response
            }
        else:
            # 重新规划
            return {
                "type": "status",
                "stage": "replanner",
                "message": f"评估完成，{'继续执行剩余步骤' if plan else '准备生成最终响应'}",
                "remaining_steps": len(plan)
            }


# 全局单例
aiops_service = AIOpsService()
