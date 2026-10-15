"""AIOps 任务模型（任务注册表的状态载体）

与 graph checkpoint 分工明确（见 ENGINEERING-DESIGN §4.3②）：
- checkpoint 存"这条 thread 的图执行现场"；
- 本模型存"任务是什么、属于谁、处于什么状态"，graph 不认识任务、也没有终态。

两者靠 ``thread_id`` / ``checkpoint_ref`` 关联，恢复 = 读到 ``ejected`` → 用 thread_id 续最新 checkpoint。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, List

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """任务状态机

    queued → running ──→ done(终态)
                     ├── cancel → cancelled(终态)
                     ├── 进程中断 → ejected(可恢复候选，**非**终态)
                     │     ├── resume → running → done
                     │     ├── 用户放弃 → failed(终态)
                     │     └── 停滞超 TTL → expired(终态)
                     └── 失败收尾 → failed(终态)
    """

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"
    EJECTED = "ejected"
    FAILED = "failed"
    EXPIRED = "expired"


#: 终态集合——落盘后不再出现在"待恢复清单"里（满足"不重复展示"）
TERMINAL_STATUSES = frozenset({
    TaskStatus.DONE,
    TaskStatus.CANCELLED,
    TaskStatus.FAILED,
    TaskStatus.EXPIRED,
})

#: 可恢复状态——启动扫描只取这些
RESUMABLE_STATUSES = frozenset({TaskStatus.EJECTED})


class InterruptMode(str, Enum):
    """中断任务的可恢复性判定（E5）。

    - ``auto``：现场无副作用，无决策点，可直接续跑（当前工具只读，多数属于此类）；
    - ``manual``：现场含需人工判断的收尾/换路，或未来接写侧工具后的幂等风险，
      自动续跑不安全，须显式决策后再续。
    """

    AUTO = "auto"
    MANUAL = "manual"


class TriggerSource(str, Enum):
    """任务触发源（决定结果去向与 time_anchor 来源）。"""
    CHAT = "chat"       # 对话 Agent 派发 → 结果可回填对话
    ALERT = "alert"     # 告警 webhook 自动派发 → 结果只落库
    MANUAL = "manual"   # API 手动提交


class DeliveryStatus(str, Enum):
    """投递状态机（与 task status 独立）。

    分开的原因：任务 done 了但通知可能送不到（会话被清空）。
    投递失败**不回滚**任务状态。
    """
    NONE = "none"         # 无需投递（如 alert/manual 触发，本期无通知出口）
    PENDING = "pending"   # 待投递
    SENT = "sent"         # 投递成功（终态）
    FAILED = "failed"     # 重试超限（终态）


TERMINAL_DELIVERY_STATUSES = frozenset({DeliveryStatus.SENT, DeliveryStatus.FAILED})


class TaskRecord(BaseModel):
    """一条排障任务的注册信息"""

    task_id: str = Field(..., description="任务唯一 ID")
    owner: str = Field(default="anonymous", description="任务归属（本期占位，鉴权属 P2）")
    session_id: str = Field(..., description="归属会话")
    parent_session: Optional[str] = Field(
        default=None,
        description=(
            "完成回填目标（触发本任务的主对话会话）。"
            "**当前仅落库、无消费方**：回填的前提是 supervisor 化（对话 Agent 把 "
            "run_aiops 当工具调），该结构本期未实现，故父子当前取同一值，"
            "待 supervisor 化后消费（见 ENGINEERING-DESIGN §4.0/§4.3②E3）。"
        ),
    )
    thread_id: str = Field(..., description="对应的 graph thread_id（恢复用）")
    input: str = Field(..., description="任务输入（排障目标描述）")

    status: TaskStatus = Field(default=TaskStatus.QUEUED)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    checkpoint_ref: Optional[str] = Field(
        default=None, description="checkpoint 引用（本期即 thread_id）"
    )
    plan_version: int = Field(default=1, description="计划版本（replan 会增加）")
    time_anchor: Optional[datetime] = Field(
        default=None, description="原任务时间窗；时间敏感查询恢复时沿用而非按 now"
    )
    result_ref: Optional[str] = Field(default=None, description="完成后的报告引用")
    report: Optional[str] = Field(
        default=None, description="完成后的最终报告正文（供任务查询接口返回）"
    )
    interrupt: InterruptMode = Field(
        default=InterruptMode.MANUAL,
        description="恢复安全性：auto 可直接续跑，manual 需人工决策后显式续",
    )
    trigger: TriggerSource = Field(
        default=TriggerSource.MANUAL, description="触发源：决定结果去向与 time_anchor 来源"
    )
    delivery_status: DeliveryStatus = Field(
        default=DeliveryStatus.NONE, description="投递状态（与 status 独立）"
    )
    delivery_attempts: int = Field(default=0, description="投递尝试次数（有界重试）")
    delivered_at: Optional[datetime] = Field(default=None, description="投递成功时刻")

    def touch(self) -> None:
        """更新 updated_at。"""
        self.updated_at = datetime.now(timezone.utc)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_resumable(self) -> bool:
        return self.status in RESUMABLE_STATUSES

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump(mode="json")
