"""回答评估：走对话 Agent 现有应答路径，判定是否拒答。"""
from __future__ import annotations

from loguru import logger

from app.services.rag_agent_service import rag_agent_service
from evals.dataset_loader import EvalSample
from evals.metrics import is_refusal

DEFAULT_REFUSAL_PHRASES: tuple[str, ...] = (
    "无知识库信息",
    "知识库中没有",
    "知识库中未收录",
    "知识库未收录",
    "知识库未涵盖",
    "未收录",
    "无法从知识库",
    "没有找到相关",
    "未找到相关",
    "没有相关知识库",
)


async def run(samples: list[EvalSample]) -> list[dict]:
    """逐样本调用 Agent；每样本独立 session_id 防串话。"""
    rows: list[dict] = []
    for s in samples:
        answer, error = "", None
        try:
            answer = await rag_agent_service.query(s.question, session_id=f"eval-{s.id}")
        except Exception as e:  # noqa: BLE001 —— Review Focus #4
            error = str(e)
            logger.warning(f"[eval] 样本 {s.id} 回答失败: {e}")
        rows.append(
            {
                "id": s.id,
                "category": s.category,
                "answer": answer,
                "refused": is_refusal(answer, DEFAULT_REFUSAL_PHRASES),
                "error": error,
            }
        )
    return rows
