"""checkpointer 工厂：SQLite 持久化，缺依赖时降级内存态

保持「能力开关式接入」风格（见 config.py 与红线 §0.4）：
- 正常：``AsyncSqliteSaver``（跨进程可恢复，P0-3 断点续跑的基础）；
- 缺 ``langgraph-checkpoint-sqlite`` / ``aiosqlite`` 或显式关闭：退回 ``MemorySaver``（进程内）；
- **任何情况都不能让服务起不来**。

注意：P-E-R 与对话 Agent 都走 ``astream``，因此**必须用 Async 版**。
同步的 ``SqliteSaver`` 在 async 方法上会直接抛 NotImplementedError。
"""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from langgraph.checkpoint.memory import MemorySaver
from loguru import logger

from app.config import config


def _ensure_parent(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)


def sqlite_available() -> bool:
    """探测 SQLite checkpoint 依赖是否齐备。"""
    if not config.checkpoint_enabled:
        return False
    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # noqa: F401
    except ImportError as e:
        logger.warning(f"未安装 langgraph-checkpoint-sqlite / aiosqlite，降级内存态: {e}")
        return False
    return True


@asynccontextmanager
async def create_async_checkpointer() -> AsyncIterator[Any]:
    """异步 checkpointer（供 FastAPI lifespan 用）。

    用法::

        async with create_async_checkpointer() as cp:
            graph = builder.compile(checkpointer=cp)

    依赖缺失或关闭时返回 ``MemorySaver``（进程内，重启丢），不影响启动。
    """
    if not sqlite_available():
        logger.warning(f"使用 MemorySaver（进程内，重启丢失）: checkpoint_enabled={config.checkpoint_enabled}")
        yield MemorySaver()
        return

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    db_path = config.checkpoint_db_path
    _ensure_parent(db_path)
    logger.info(f"使用 AsyncSqliteSaver 持久化 checkpoint: {db_path}")

    async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
        yield saver


@contextmanager
def create_sync_checkpointer() -> Iterator[Any]:
    """同步 checkpointer（供脚本/测试用）。

    同样做依赖降级；同步场景下若只有 Async 版可用，则退回 MemorySaver——
    脚本场景不需要持久化，不应因此失败。
    """
    if not config.checkpoint_enabled:
        yield MemorySaver()
        return
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver

        db_path = config.checkpoint_db_path
        _ensure_parent(db_path)
        with SqliteSaver.from_conn_string(db_path) as saver:
            yield saver
    except ImportError:
        yield MemorySaver()
