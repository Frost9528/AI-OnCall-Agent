"""AIOps 任务注册表

职责：维护 task 级状态（graph checkpoint 不认识任务、也没有终态）。
持久化用 SQLite，与 checkpointer 同一套"单文件、零服务"的理由（见 §7）。

关键规则（ENGINEERING-DESIGN §4.3②）：
- ``ejected != failed``：进程中断只是"可恢复候选"，不是失败；
- 不能无期限滞留：靠 用户放弃(failed) / 停滞 TTL(expired) / 主动中止(cancelled) 收敛成终态；
- **重启扫描只列 ``ejected``**，已落终态的不再出现（"不重复展示"）。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from loguru import logger

from app.config import config
from app.models.task import (
    DeliveryStatus,
    InterruptMode,
    RESUMABLE_STATUSES,
    TaskRecord,
    TaskStatus,
    TriggerSource,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS aiops_tasks (
    task_id        TEXT PRIMARY KEY,
    owner          TEXT NOT NULL DEFAULT 'anonymous',
    session_id     TEXT NOT NULL,
    parent_session TEXT,
    thread_id      TEXT NOT NULL,
    input          TEXT NOT NULL,
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    checkpoint_ref TEXT,
    plan_version   INTEGER NOT NULL DEFAULT 1,
    time_anchor    TEXT,
    result_ref     TEXT,
    interrupt      TEXT NOT NULL DEFAULT 'manual',
    report         TEXT,
    trigger        TEXT NOT NULL DEFAULT 'manual',
    delivery_status TEXT NOT NULL DEFAULT 'none',
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    delivered_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_aiops_tasks_status ON aiops_tasks(status);
CREATE INDEX IF NOT EXISTS idx_aiops_tasks_session ON aiops_tasks(session_id);
"""

#: 增量迁移：老库缺列时补上（CREATE TABLE IF NOT EXISTS 不会改已存在的表）
_MIGRATIONS = (
    ("interrupt", "ALTER TABLE aiops_tasks ADD COLUMN interrupt TEXT NOT NULL DEFAULT 'manual'"),
    ("report", "ALTER TABLE aiops_tasks ADD COLUMN report TEXT"),
    ("trigger", "ALTER TABLE aiops_tasks ADD COLUMN trigger TEXT NOT NULL DEFAULT 'manual'"),
    ("delivery_status", "ALTER TABLE aiops_tasks ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'none'"),
    ("delivery_attempts", "ALTER TABLE aiops_tasks ADD COLUMN delivery_attempts INTEGER NOT NULL DEFAULT 0"),
    ("delivered_at", "ALTER TABLE aiops_tasks ADD COLUMN delivered_at TEXT"),
)


class TaskRegistry:
    """任务注册表（SQLite 持久化 + 进程内锁）

    使用同步 sqlite3：调用方均为短事务、单机单文件，无需 async 驱动。
    """

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or config.task_db_path
        self._lock = threading.Lock()
        self._ensure_parent()
        self._init_schema()
        logger.info(f"任务注册表初始化完成: {self._db_path}")

    def _ensure_parent(self) -> None:
        if self._db_path == ":memory:":
            return
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)
            self._apply_migrations(conn)

    @staticmethod
    def _apply_migrations(conn: sqlite3.Connection) -> None:
        """补齐老库缺失的列（幂等）。"""
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(aiops_tasks)")}
        for column, ddl in _MIGRATIONS:
            if column not in existing:
                conn.execute(ddl)
                logger.info(f"任务表迁移：新增列 {column}")

    # ---------- 写 ----------

    def create(self, task: TaskRecord) -> TaskRecord:
        """登记一个新任务。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO aiops_tasks (
                    task_id, owner, session_id, parent_session, thread_id, input,
                    status, created_at, updated_at, checkpoint_ref, plan_version,
                    time_anchor, result_ref, interrupt, report, trigger,
                    delivery_status, delivery_attempts, delivered_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task.task_id,
                    task.owner,
                    task.session_id,
                    task.parent_session,
                    task.thread_id,
                    task.input,
                    task.status.value,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                    task.checkpoint_ref,
                    task.plan_version,
                    task.time_anchor.isoformat() if task.time_anchor else None,
                    task.result_ref,
                    task.interrupt.value,
                    task.report,
                    task.trigger.value,
                    task.delivery_status.value,
                    task.delivery_attempts,
                    task.delivered_at.isoformat() if task.delivered_at else None,
                ),
            )
        logger.info(f"任务已登记: {task.task_id} (session={task.session_id}, status={task.status.value})")
        return task

    def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        result_ref: Optional[str] = None,
        plan_version: Optional[int] = None,
        report: Optional[str] = None,
    ) -> Optional[TaskRecord]:
        """更新任务状态；返回更新后的记录（不存在则 None）。"""
        now = datetime.now(timezone.utc).isoformat()
        sets = ["status = ?", "updated_at = ?"]
        params: list = [status.value, now]
        if result_ref is not None:
            sets.append("result_ref = ?")
            params.append(result_ref)
        if plan_version is not None:
            sets.append("plan_version = ?")
            params.append(plan_version)
        if report is not None:
            sets.append("report = ?")
            params.append(report)
        params.append(task_id)

        with self._lock, self._connect() as conn:
            cur = conn.execute(
                f"UPDATE aiops_tasks SET {', '.join(sets)} WHERE task_id = ?", params
            )
            if cur.rowcount == 0:
                logger.warning(f"更新任务状态失败，任务不存在: {task_id}")
                return None
        logger.info(f"任务状态更新: {task_id} -> {status.value}")
        return self.get(task_id)

    def mark_ejected(self, task_id: str) -> Optional[TaskRecord]:
        """把未收尾的 running 任务标为 ejected（进程/服务中断的恢复候选）。"""
        return self.update_status(task_id, TaskStatus.EJECTED)

    def set_interrupt_mode(
        self, task_id: str, mode: InterruptMode
    ) -> Optional[TaskRecord]:
        """记录任务的可恢复性判定（auto 可直接续 / manual 需人工决策）。"""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE aiops_tasks SET interrupt = ?, updated_at = ? WHERE task_id = ?",
                (mode.value, datetime.now(timezone.utc).isoformat(), task_id),
            )
            if cur.rowcount == 0:
                return None
        logger.info(f"任务 {task_id} 恢复模式: {mode.value}")
        return self.get(task_id)

    def set_delivery(
        self,
        task_id: str,
        status: DeliveryStatus,
        *,
        attempts: Optional[int] = None,
        delivered_at: Optional[datetime] = None,
    ) -> Optional[TaskRecord]:
        """更新投递状态；返回更新后记录（不存在则 None）。"""
        sets = ["delivery_status = ?", "updated_at = ?"]
        params: list = [status.value, datetime.now(timezone.utc).isoformat()]
        if attempts is not None:
            sets.append("delivery_attempts = ?"); params.append(attempts)
        if delivered_at is not None:
            sets.append("delivered_at = ?"); params.append(delivered_at.isoformat())
        params.append(task_id)
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                f"UPDATE aiops_tasks SET {', '.join(sets)} WHERE task_id = ?", params
            )
            if cur.rowcount == 0:
                return None
        return self.get(task_id)

    # ---------- 读 ----------

    def get(self, task_id: str) -> Optional[TaskRecord]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM aiops_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def claim_pending_for_session(self, session_id: str) -> List[TaskRecord]:
        """原子认领该会话的待投递记录（并发安全）。

        先取候选，再用条件 UPDATE（WHERE delivery_status = 'pending'）逐个认领：
        只有 rowcount == 1 的才算认领成功。这样两个并发会话不会重复注入同一通知
        （SQLite 单写者 + WHERE 守卫提供跨进程安全；进程内锁只作补充）。
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM aiops_tasks WHERE session_id = ? AND delivery_status = ? "
                "AND trigger = ? ORDER BY updated_at ASC",
                (session_id, DeliveryStatus.PENDING.value, TriggerSource.CHAT.value),
            ).fetchall()
            claimed: List[TaskRecord] = []
            for row in rows:
                cur = conn.execute(
                    "UPDATE aiops_tasks SET delivery_status = ?, delivered_at = ?, "
                    "delivery_attempts = delivery_attempts + 1, updated_at = ? "
                    "WHERE task_id = ? AND delivery_status = ?",
                    (DeliveryStatus.SENT.value, now, now,
                     row["task_id"], DeliveryStatus.PENDING.value),
                )
                if cur.rowcount == 1:
                    claimed.append(self._row_to_record(row))
            return claimed

    def list_by_session(self, session_id: str) -> List[TaskRecord]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM aiops_tasks WHERE session_id = ? ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_resumable(self) -> List[TaskRecord]:
        """待恢复清单：只含 ``ejected``。

        终态（done/cancelled/failed/expired）一律不入，故重启后不会重复展示。
        """
        placeholders = ",".join("?" for _ in RESUMABLE_STATUSES)
        values = [s.value for s in RESUMABLE_STATUSES]
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM aiops_tasks WHERE status IN ({placeholders}) "
                f"ORDER BY updated_at DESC",
                values,
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_pending_deliveries(self, limit: int = 20) -> List[TaskRecord]:
        """待投递任务（delivery_status=pending）。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM aiops_tasks WHERE delivery_status = ? "
                "ORDER BY updated_at ASC LIMIT ?",
                (DeliveryStatus.PENDING.value, limit),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def find_running_by_session(self, session_id: str) -> Optional[TaskRecord]:
        """查该会话是否有正在跑的任务（会话级单飞行约束，见 §4.0）。"""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM aiops_tasks WHERE session_id = ? AND status = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (session_id, TaskStatus.RUNNING.value),
            ).fetchone()
        return self._row_to_record(row) if row else None

    # ---------- 收敛 ----------

    def expire_stale(self, ttl_seconds: int) -> int:
        """把停滞超过 TTL 的 ``ejected`` 收敛为 ``expired``（硬超时）。

        比较用 ``updated_at <= cutoff``：任务创建与扫描之间可能只隔微秒，
        若用严格小于，TTL=0（"立即全部过期"）会因时间戳先后而随机漏掉，
        形成不稳定行为。

        Returns:
            int: 被收敛的任务数
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)).isoformat()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE aiops_tasks SET status = ?, updated_at = ? "
                "WHERE status = ? AND updated_at <= ?",
                (
                    TaskStatus.EXPIRED.value,
                    datetime.now(timezone.utc).isoformat(),
                    TaskStatus.EJECTED.value,
                    cutoff,
                ),
            )
            count = cur.rowcount
        if count:
            logger.info(f"已收敛 {count} 个停滞任务为 expired (TTL={ttl_seconds}s)")
        return count

    def mark_running_as_ejected(self) -> int:
        """进程启动时把遗留的 ``running`` 视为中断，标为 ``ejected``。

        进程被杀不会有机会写"我中断了"，只能靠重启时反推。
        """
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE aiops_tasks SET status = ?, updated_at = ? WHERE status = ?",
                (
                    TaskStatus.EJECTED.value,
                    datetime.now(timezone.utc).isoformat(),
                    TaskStatus.RUNNING.value,
                ),
            )
            count = cur.rowcount
        if count:
            logger.warning(f"启动扫描：{count} 个遗留 running 任务已标为 ejected（可恢复）")
        return count

    # ---------- 内部 ----------

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> TaskRecord:
        def _dt(v):
            return datetime.fromisoformat(v) if v else None

        return TaskRecord(
            task_id=row["task_id"],
            owner=row["owner"],
            session_id=row["session_id"],
            parent_session=row["parent_session"],
            thread_id=row["thread_id"],
            input=row["input"],
            status=TaskStatus(row["status"]),
            created_at=_dt(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=_dt(row["updated_at"]) or datetime.now(timezone.utc),
            checkpoint_ref=row["checkpoint_ref"],
            plan_version=row["plan_version"],
            time_anchor=_dt(row["time_anchor"]),
            result_ref=row["result_ref"],
            interrupt=InterruptMode(row["interrupt"] or InterruptMode.MANUAL.value),
            report=row["report"],
            trigger=TriggerSource(row["trigger"] or TriggerSource.MANUAL.value),
            delivery_status=DeliveryStatus(row["delivery_status"] or DeliveryStatus.NONE.value),
            delivery_attempts=row["delivery_attempts"] or 0,
            delivered_at=_dt(row["delivered_at"]),
        )


# 全局单例
task_registry = TaskRegistry()
