"""检索评估：对可答样本跑向量检索，采集命中的源文档。"""
from __future__ import annotations

import json

from app.services.vector_search_service import vector_search_service
from evals.common import to_rel_source
from evals.dataset_loader import EvalSample


def _extract_source(metadata) -> str:
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            return ""
    if isinstance(metadata, dict):
        return to_rel_source(metadata.get("_source", ""))
    return ""


def run(samples: list[EvalSample], top_k: int) -> list[dict]:
    """对 fact/cross_doc 样本执行检索；单样本异常记入 error 并继续。"""
    rows: list[dict] = []
    for s in samples:
        if s.category == "no_answer":
            continue
        row = {
            "id": s.id,
            "category": s.category,
            "expected_sources": list(s.expected_sources),
            "retrieved_sources": [],
            "top_score": None,
            "error": None,
        }
        try:
            results = vector_search_service.search_similar_documents(s.question, top_k=top_k)
            row["retrieved_sources"] = [_extract_source(r.metadata) for r in results]
            row["top_score"] = results[0].score if results else None
        except Exception as e:  # noqa: BLE001  —— Review Focus #4：不中断整轮
            row["error"] = str(e)
        rows.append(row)
    return rows
