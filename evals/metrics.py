"""RAG 评估指标 —— 纯函数，无 IO，可独立单测。"""
from __future__ import annotations

from typing import Sequence


def is_refusal(answer: str, phrases: Sequence[str]) -> bool:
    """回答是否属于"拒答"。空/纯空白回答不算拒答。

    归一化：去掉所有空白字符并转小写后再做子串匹配。
    """
    if not answer or not answer.strip():
        return False
    norm = "".join(answer.split()).lower()
    for p in phrases:
        if "".join(p.split()).lower() in norm:
            return True
    return False


def recall_at_k(
    retrieved_sources: Sequence[str],
    expected_sources: Sequence[str],
    k: int,
) -> float:
    """命中期望源数 / 期望源总数（在 top-k 截断内，按集合去重）。"""
    if not expected_sources:
        return 0.0
    topk = set(retrieved_sources[:k])
    matched = topk & set(expected_sources)
    return len(matched) / len(set(expected_sources))


def topk_hit(
    retrieved_sources: Sequence[str],
    expected_sources: Sequence[str],
    k: int,
) -> bool:
    """top-k 内是否至少命中一个期望源。"""
    if not expected_sources:
        return False
    return bool(set(retrieved_sources[:k]) & set(expected_sources))


def aggregate_retrieval(rows: Sequence[dict], k: int) -> dict:
    """聚合检索指标。rows 元素含 retrieved_sources / expected_sources。"""
    n = len(rows)
    if n == 0:
        return {"n": 0, "recall_at_k": None, "topk_hit_rate": None}
    recalls = [recall_at_k(r["retrieved_sources"], r["expected_sources"], k) for r in rows]
    hits = [topk_hit(r["retrieved_sources"], r["expected_sources"], k) for r in rows]
    return {
        "n": n,
        "recall_at_k": sum(recalls) / n,
        "topk_hit_rate": sum(hits) / n,
    }


def aggregate_refusal(rows: Sequence[dict]) -> dict:
    """聚合拒答指标。rows 元素含 category / refused。

    - refusal_accuracy：知识轴二分类准确率
      =（no_answer 被拒 + 可答未被拒）/ 全部样本
    - over_refusal_rate：可答样本中被误拒的比例（单列，防奖励"什么都拒"）
    """
    no_answer = [r for r in rows if r["category"] == "no_answer"]
    answerable = [r for r in rows if r["category"] in ("fact", "cross_doc")]
    correct = (
        sum(1 for r in no_answer if r["refused"])
        + sum(1 for r in answerable if not r["refused"])
    )
    total = len(no_answer) + len(answerable)
    over = sum(1 for r in answerable if r["refused"])
    return {
        "n": total,
        "n_no_answer": len(no_answer),
        "n_answerable": len(answerable),
        "refusal_accuracy": (correct / total) if total else None,
        "over_refusal_rate": (over / len(answerable)) if answerable else None,
    }
