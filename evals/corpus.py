"""语料幂等入库 + collection source 清单。

Milvus 数据持久化在宿主 ./volumes/，容器重启不丢；app 启动不自动索引。
本模块复用 vector_index_service.index_single_file（内部 delete_by_source 再写，天然幂等）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from app.core.milvus_client import milvus_manager
from app.services.vector_index_service import vector_index_service
from evals.common import REPO_ROOT, to_rel_source


@dataclass
class CorpusReport:
    indexed: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    source_inventory: list[str] = field(default_factory=list)


def get_source_inventory() -> list[str]:
    """返回 collection 中出现的所有 _source（仓库相对路径，去重排序）。"""
    collection = milvus_manager.get_collection()
    rows = collection.query(expr='id != ""', output_fields=["metadata"])
    sources: set[str] = set()
    for row in rows:
        meta = row.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        if isinstance(meta, dict):
            src = meta.get("_source")
            if src:
                sources.add(to_rel_source(src))
    return sorted(sources)


def ensure_corpus(docs_dir: str = "aiops-docs", *, skip: bool = False) -> CorpusReport:
    """幂等确保目录下 *.md 已入库；返回本次结果与最新 source 清单。"""
    report = CorpusReport()

    if not skip:
        dir_path = (REPO_ROOT / docs_dir)
        files = sorted(list(dir_path.glob("*.md")) + list(dir_path.glob("*.txt")))
        for f in files:
            try:
                vector_index_service.index_single_file(str(f))
                report.indexed.append(to_rel_source(str(f)))
            except Exception as e:  # noqa: BLE001
                logger.error(f"索引失败: {f}: {e}")
                report.failed[to_rel_source(str(f))] = str(e)

    report.source_inventory = get_source_inventory()
    return report
