"""评估数据集加载与校验。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

CATEGORIES = {"fact", "cross_doc", "no_answer"}


@dataclass
class EvalSample:
    id: str
    category: str
    question: str
    expected_sources: list[str] = field(default_factory=list)
    reference_answer: str = ""
    notes: str = ""


def load_dataset(path: str | Path) -> list[EvalSample]:
    """读取 JSONL 并校验；任一行非法则抛 ValueError（含行号）。"""
    p = Path(path)
    samples: list[EvalSample] = []
    seen_ids: set[str] = set()

    with p.open(encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError(f"第 {lineno} 行不是合法 JSON: {e}") from e

            for key in ("id", "category", "question"):
                if not obj.get(key):
                    raise ValueError(f"第 {lineno} 行缺少字段: {key}")

            cat = obj["category"]
            if cat not in CATEGORIES:
                raise ValueError(
                    f"第 {lineno} 行 category 非法: {cat!r}（允许 {sorted(CATEGORIES)}）"
                )
            if obj["id"] in seen_ids:
                raise ValueError(f"第 {lineno} 行 id 重复: {obj['id']}")
            seen_ids.add(obj["id"])

            expected = obj.get("expected_sources") or []
            if cat == "no_answer" and expected:
                raise ValueError(
                    f"第 {lineno} 行 no_answer 的 expected_sources 必须为空"
                )
            if cat in ("fact", "cross_doc") and not expected:
                raise ValueError(
                    f"第 {lineno} 行 {cat} 的 expected_sources 不能为空"
                )

            samples.append(
                EvalSample(
                    id=obj["id"],
                    category=cat,
                    question=obj["question"],
                    expected_sources=list(expected),
                    reference_answer=obj.get("reference_answer", ""),
                    notes=obj.get("notes", ""),
                )
            )
    return samples
