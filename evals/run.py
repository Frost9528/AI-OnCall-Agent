"""评估入口：python -m evals.run

注意：app/evals 的重依赖（Milvus / DashScope embedding / ChatOpenAI / 各服务单例）在
**导入时**就会初始化。为保证 `--help` 与 `_preflight` 的“快速失败”在无 key / 无 Milvus
的环境下依然可用（不静默降级），本入口只保留模块安全的 import，把其余重 import 延迟到
真正需要它们的函数内部（_preflight / main_async）。
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from loguru import logger

from app.config import config
from evals.dataset_loader import CATEGORIES, load_dataset

DEFAULT_DATASET = "evals/datasets/rag_eval_set.jsonl"


def _preflight() -> None:
    """快速失败：Milvus 不可达或缺 key 时明确报错，不静默降级。"""
    if not config.dashscope_api_key:
        sys.exit("✗ 未配置 DASHSCOPE_API_KEY：评估需要 embedding/LLM。请在 .env 中配置后重试。")
    try:
        from app.core.milvus_client import milvus_manager

        milvus_manager.connect()
        milvus_manager.get_collection()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"✗ Milvus 不可达（{config.milvus_host}:{config.milvus_port}）：{e}\n  请先启动 Milvus 后重试。")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="RAG 离线评估（最小可行）")
    p.add_argument("--category", default="all", choices=sorted(CATEGORIES | {"all"}))
    p.add_argument("--top-k", type=int, default=config.rag_top_k)
    p.add_argument("--skip-seed", action="store_true", help="跳过语料索引（确认已入库时）")
    p.add_argument("--no-answer", action="store_true", help="只跑检索，跳过回答评估")
    p.add_argument("--dataset", default=DEFAULT_DATASET)
    p.add_argument("--out", default="evals/reports")
    return p.parse_args(argv)


async def main_async(args: argparse.Namespace) -> dict:
    # 延迟导入：只有真正要跑评估时才加载重依赖（见模块 docstring）。
    from evals import answer_eval, retrieval_eval
    from evals.corpus import ensure_corpus
    from evals.report import build_report, print_summary, write_report

    samples = load_dataset(args.dataset)
    if args.category != "all":
        samples = [s for s in samples if s.category == args.category]
    logger.info(f"载入 {len(samples)} 条样本（category={args.category}）")

    corpus = ensure_corpus(skip=args.skip_seed)
    retrieval_rows = retrieval_eval.run(samples, top_k=args.top_k)

    answer_rows: list[dict] = []
    if not args.no_answer:
        answer_rows = await answer_eval.run(samples)

    report = build_report(
        retrieval_rows=retrieval_rows,
        answer_rows=answer_rows,
        corpus=corpus,
        top_k=args.top_k,
        dataset_path=args.dataset,
    )
    print_summary(report)
    path = write_report(report, args.out)
    print(f"报告已写入: {path}")
    return report


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    _preflight()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
