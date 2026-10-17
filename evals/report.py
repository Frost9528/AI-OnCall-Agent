"""评估报告：控制台摘要 + JSON/Markdown 落盘。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.config import config
from evals.corpus import CorpusReport
from evals.metrics import aggregate_refusal, aggregate_retrieval


def build_report(
    *,
    retrieval_rows: list[dict],
    answer_rows: list[dict],
    corpus: CorpusReport,
    top_k: int,
    dataset_path: str,
) -> dict:
    retrieval = aggregate_retrieval(retrieval_rows, k=top_k)
    refusal = aggregate_refusal(answer_rows)
    errors = [
        {"id": r["id"], "error": r["error"]}
        for r in (retrieval_rows + answer_rows)
        if r.get("error")
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "rag_top_k": config.rag_top_k,
            "chunk_max_size": config.chunk_max_size,
            "chunk_overlap": config.chunk_overlap,
            "embedding_model": config.dashscope_embedding_model,
            "collection": "biz",
        },
        "dataset_path": dataset_path,
        "top_k": top_k,
        "corpus": {
            "indexed": corpus.indexed,
            "failed": corpus.failed,
            "source_inventory": corpus.source_inventory,
        },
        "retrieval": retrieval,
        "refusal": refusal,
        "retrieval_rows": retrieval_rows,
        "answer_rows": answer_rows,
        "errors": errors,
    }


def render_markdown(report: dict) -> str:
    r, rf = report["retrieval"], report["refusal"]
    lines = [
        f"# RAG 离线评估报告 {report['generated_at']}",
        "",
        f"- 数据集: `{report['dataset_path']}`（top_k={report['top_k']}）",
        f"- 配置快照: `{json.dumps(report['config'], ensure_ascii=False)}`",
        f"- 语料源清单: {report['corpus']['source_inventory']}",
        "",
        "## 检索指标",
        f"- 可答样本数: {r['n']}",
        f"- Recall@K: {r['recall_at_k']}",
        f"- TopK 命中率: {r['topk_hit_rate']}",
        "",
        "## 拒答指标",
        f"- 样本数: {rf['n']}（no_answer={rf['n_no_answer']}, 可答={rf['n_answerable']}）",
        f"- 拒答准确率: {rf['refusal_accuracy']}",
        f"- 误拒答率: {rf['over_refusal_rate']}",
        "",
        f"## 异常样本 ({len(report['errors'])})",
    ]
    lines += [f"- {e['id']}: {e['error']}" for e in report["errors"]] or ["- 无"]
    return "\n".join(lines) + "\n"


def write_report(report: dict, out_dir: str = "evals/reports") -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = out / f"{ts}.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / f"{ts}.md").write_text(render_markdown(report), encoding="utf-8")
    return json_path


def print_summary(report: dict) -> None:
    r, rf = report["retrieval"], report["refusal"]
    print("=" * 60)
    print("RAG 离线评估摘要")
    print(f"  检索: n={r['n']}  Recall@K={r['recall_at_k']}  hit={r['topk_hit_rate']}")
    print(
        f"  拒答: n={rf['n']}  准确率={rf['refusal_accuracy']}  "
        f"误拒答率={rf['over_refusal_rate']}"
    )
    print(f"  异常样本: {len(report['errors'])}")
    print("=" * 60)
