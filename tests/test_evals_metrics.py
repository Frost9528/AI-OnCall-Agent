"""evals.metrics 纯函数单测（无网络、无 Milvus）。"""
from evals.metrics import (
    aggregate_refusal,
    aggregate_retrieval,
    is_refusal,
    recall_at_k,
    topk_hit,
)

PHRASES = ["无知识库信息", "知识库中没有", "没有找到相关"]


def test_is_refusal_positive():
    assert is_refusal("很抱歉，知识库中没有该问题的信息。", PHRASES) is True


def test_is_refusal_negative():
    assert is_refusal("CPU 使用率过高时应先查看 top 进程。", PHRASES) is False


def test_is_refusal_empty_answer_is_not_refusal():
    # Review Focus #2：空回答不算拒答
    assert is_refusal("", PHRASES) is False
    assert is_refusal("   \n  ", PHRASES) is False


def test_is_refusal_normalizes_whitespace_and_case():
    # Review Focus #5：去空白 + 小写后仍能匹配
    assert is_refusal("没有  找到相关\n信息", PHRASES) is True
    assert is_refusal("NO  KNOWLEDGE", ["no knowledge"]) is True


def test_recall_at_k_basic():
    assert recall_at_k(["a", "b", "c"], ["a"], 3) == 1.0
    assert recall_at_k(["x", "y", "z"], ["a"], 3) == 0.0


def test_recall_at_k_cross_doc_partial():
    # 2 个期望源命中 1 个 → 0.5
    assert recall_at_k(["a", "x", "y"], ["a", "b"], 3) == 0.5


def test_recall_at_k_dedupes_duplicate_retrieved():
    # Review Focus #3：重复检索结果不得使 Recall 超过 1.0
    assert recall_at_k(["a", "a", "a"], ["a"], 3) == 1.0


def test_recall_at_k_respects_k_cutoff():
    # 命中项在第 3 位，k=2 时应看不到
    assert recall_at_k(["x", "y", "a"], ["a"], 2) == 0.0
    assert recall_at_k(["x", "y", "a"], ["a"], 3) == 1.0


def test_recall_at_k_empty_expected():
    assert recall_at_k(["a"], [], 3) == 0.0


def test_topk_hit_true_false():
    assert topk_hit(["x", "a"], ["a"], 3) is True
    assert topk_hit(["x", "y"], ["a"], 3) is False
    assert topk_hit([], ["a"], 3) is False


def test_aggregate_retrieval_empty_rows_returns_none_metrics():
    # Review Focus #1：无样本时返回 None，而非除零崩溃
    out = aggregate_retrieval([], k=3)
    assert out == {"n": 0, "recall_at_k": None, "topk_hit_rate": None}


def test_aggregate_retrieval_averages():
    rows = [
        {"retrieved_sources": ["a"], "expected_sources": ["a"]},
        {"retrieved_sources": ["x"], "expected_sources": ["a"]},
    ]
    out = aggregate_retrieval(rows, k=3)
    assert out["n"] == 2
    assert out["recall_at_k"] == 0.5
    assert out["topk_hit_rate"] == 0.5


def test_aggregate_refusal_counts_and_over_refusal():
    rows = [
        {"category": "no_answer", "refused": True},   # 正确拒答
        {"category": "no_answer", "refused": False},  # 漏拒答
        {"category": "fact", "refused": False},       # 正确作答
        {"category": "fact", "refused": True},        # 误拒答
    ]
    out = aggregate_refusal(rows)
    assert out["n"] == 4
    assert out["n_no_answer"] == 2
    assert out["n_answerable"] == 2
    assert out["refusal_accuracy"] == 0.5      # 2/4 正确
    assert out["over_refusal_rate"] == 0.5     # 1/2 可答样本被误拒


def test_aggregate_refusal_no_answerable_returns_none():
    out = aggregate_refusal([{"category": "no_answer", "refused": True}])
    assert out["over_refusal_rate"] is None
    assert out["refusal_accuracy"] == 1.0


# --- dataset_loader / common ---
import json
from pathlib import Path

import pytest

from evals.common import REPO_ROOT, to_rel_source
from evals.dataset_loader import load_dataset


def test_to_rel_source_absolute_to_repo_relative():
    abs_path = (REPO_ROOT / "aiops-docs" / "cpu_high_usage.md").resolve().as_posix()
    assert to_rel_source(abs_path) == "aiops-docs/cpu_high_usage.md"


def test_to_rel_source_passthrough_when_outside_repo():
    assert to_rel_source("/some/other/place.txt") == "/some/other/place.txt"


def _write(tmp_path, rows):
    p = tmp_path / "ds.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    return str(p)


def _valid_row(**over):
    row = {
        "id": "fact-x-001",
        "category": "fact",
        "question": "Q?",
        "expected_sources": ["aiops-docs/cpu_high_usage.md"],
        "reference_answer": "A",
        "notes": "",
    }
    row.update(over)
    return row


def test_load_dataset_valid(tmp_path):
    samples = load_dataset(_write(tmp_path, [_valid_row()]))
    assert len(samples) == 1
    assert samples[0].category == "fact"
    assert samples[0].expected_sources == ["aiops-docs/cpu_high_usage.md"]


def test_load_dataset_rejects_bad_category(tmp_path):
    with pytest.raises(ValueError, match="category"):
        load_dataset(_write(tmp_path, [_valid_row(category="bogus")]))


def test_load_dataset_rejects_duplicate_id(tmp_path):
    with pytest.raises(ValueError, match="重复"):
        load_dataset(_write(tmp_path, [_valid_row(), _valid_row()]))


def test_load_dataset_no_answer_must_have_empty_expected(tmp_path):
    with pytest.raises(ValueError, match="expected_sources"):
        load_dataset(_write(tmp_path, [_valid_row(category="no_answer")]))


def test_committed_dataset_loads_and_meets_quota():
    path = REPO_ROOT / "evals" / "datasets" / "rag_eval_set.jsonl"
    samples = load_dataset(str(path))
    assert len(samples) >= 30
    cats = {s.category for s in samples}
    assert {"fact", "cross_doc", "no_answer"} <= cats
