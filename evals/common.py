"""评估框架共享常量与路径工具。"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def to_rel_source(path: str) -> str:
    """把（可能是绝对的）源路径归一为仓库相对 posix 路径；在仓库外则原样返回。

    数据集里 expected_sources 用仓库相对路径，而 Milvus 里存的是绝对 posix 路径，
    两侧比较前都必须过这里。
    """
    if not path:
        return ""
    p = Path(path)
    try:
        return p.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return p.as_posix()
