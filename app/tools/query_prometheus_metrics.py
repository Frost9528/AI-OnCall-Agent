"""Prometheus PromQL 指标查询工具

通过 Prometheus HTTP API `GET /api/v1/query_range` 查询一段时间窗内的指标时序，
补齐「只有 alerts、没有指标曲线」的缺口（见 ENGINEERING-DESIGN §5.3-2）。

与 `query_metrics_alerts.py` 的分工：
- 那里查 **告警**（`/api/v1/alerts`，规则产生的告警列表）；
- 这里查 **指标**（`/query_range`，PromQL 表达式的时序结果）。

设计取舍（对齐 §5.3 与本期风险项）：
- **保留原始错误返回给 LLM**：PromQL 语法由 LLM 生成时可能出错，
  把 Prometheus 的原始报错回传，LLM 才有机会自我修正；
- 错误时 JSON 兜底（`success:false`），不裸抛，避免打断图执行；
- 结果做**降采样保护**：超过上限的序列/点位会被裁剪并明确标注，
  防止一次查询把上下文撑爆。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx
from langchain_core.tools import tool
from loguru import logger

from app.config import config

# Prometheus Query Range API 路径
QUERY_RANGE_PATH = "/api/v1/query_range"

# 保护上限：返回给 LLM 的序列数与每条序列的点位数
MAX_SERIES = 10
MAX_POINTS_PER_SERIES = 60


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _query_range_api(
    query: str,
    start: float,
    end: float,
    step: str,
) -> tuple[dict[str, Any], str | None]:
    """请求 `GET {prometheus_base_url}/api/v1/query_range`。

    返回 (JSON 体, 错误信息)。成功时第二项为 None。
    """
    base_url = config.prometheus_base_url.rstrip("/")
    api_url = f"{base_url}{QUERY_RANGE_PATH}"
    params = {
        "query": query,
        "start": start,
        "end": end,
        "step": step,
    }
    logger.info(f"Querying Prometheus range: {api_url} query={query!r} step={step}")

    try:
        with httpx.Client(timeout=config.prometheus_request_timeout) as client:
            resp = client.get(api_url, params=params)
            resp.raise_for_status()
            body = resp.json()
    except httpx.HTTPStatusError as e:
        # 4xx/5xx：把 Prometheus 返回的报错体带出（PromQL 语法错误的原始信息在此）
        detail = ""
        try:
            detail = e.response.text[:500]
        except Exception:  # noqa: BLE001
            detail = ""
        return {}, f"HTTP {e.response.status_code} from Prometheus: {detail or e}"
    except httpx.HTTPError as e:
        return {}, f"failed to query Prometheus: {e}"
    except json.JSONDecodeError as e:
        return {}, f"failed to parse response: {e}"

    return body, None


def _compact_series(
    result: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """把 query_range 的返回压缩为紧凑结构，并记录被裁剪的部分。

    Prometheus 原始返回是 `[[ts, "value"], ...]`，点位可能上千；
    这里裁剪到 MAX_POINTS_PER_SERIES，并保留首尾以便看出趋势。
    """
    series_out: list[dict[str, Any]] = []
    truncated_series = 0
    truncated_points = 0

    for item in result[:MAX_SERIES]:
        metric = item.get("metric") or {}
        values = item.get("values") or []
        raw_count = len(values)

        if raw_count > MAX_POINTS_PER_SERIES:
            # 均匀采样，保留趋势形状
            stride = max(1, raw_count // MAX_POINTS_PER_SERIES)
            sampled = values[::stride][:MAX_POINTS_PER_SERIES]
            truncated_points += raw_count - len(sampled)
        else:
            sampled = values

        series_out.append({
            "metric": metric,
            # 转成 (时间戳, 值) 二元组，数值尽量转 float，失败则原样保留
            "points": [[ts, _to_number(v)] for ts, v in sampled],
            "raw_point_count": raw_count,
            "returned_point_count": len(sampled),
        })

    if len(result) > MAX_SERIES:
        truncated_series = len(result) - MAX_SERIES

    meta = {
        "series_total": len(result),
        "series_returned": len(series_out),
        "series_truncated": truncated_series,
        "points_truncated": truncated_points,
    }
    return series_out, meta


def _to_number(v: Any) -> Any:
    """Prometheus 的值是字符串，尽量转成数字便于 LLM 理解。"""
    try:
        f = float(v)
        # 整数则去掉小数点，读数更清爽
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return v


@tool
def query_prometheus_metrics(
    query: str,
    minutes: int = 30,
    step: str = "60s",
) -> str:
    """用 PromQL 查询 Prometheus 指标时序（HTTP GET /api/v1/query_range）。

    适用场景：需要看**指标随时间的曲线**而非当前告警时使用。例如排查
    「CPU/内存/磁盘/网络 最近半小时的走势」「QPS 是否下降」「错误率是否上升」
    「某服务的延迟 P99 变化」等。若只想知道「有没有告警」，用 query_prometheus_alerts。

    Args:
        query: PromQL 表达式，如 'rate(http_requests_total[5m])'、
            'avg(irate(node_cpu_seconds_total{mode="idle"}[5m])) by (instance)'。
        minutes: 查询最近多少分钟，默认 30。
        step: 查询步长（Prometheus duration 语法，如 '15s'、'1m'、'5m'），默认 '60s'。
            步长越小曲线越细但数据量越大。

    Returns:
        str: JSON 字符串。成功时含 series 列表与裁剪元信息；失败时含
            success=false 与 error（**保留 Prometheus 原始报错**，便于修正 PromQL）。
    """
    if not query or not query.strip():
        return json.dumps(
            {"success": False, "error": "query 不能为空", "message": "PromQL 表达式缺失"},
            ensure_ascii=False,
            indent=2,
        )

    end = _now_ts()
    start = end - max(1, minutes) * 60

    result, err = _query_range_api(query.strip(), start, end, step)
    if err:
        out = {
            "success": False,
            "error": err,
            "message": "Failed to query Prometheus metrics",
            "query": query,
        }
        return json.dumps(out, ensure_ascii=False, indent=2)

    status = result.get("status")
    if status != "success":
        # PromQL 语法/执行错误：原始信息必须回传给 LLM
        err_msg = result.get("error") or result.get("errorType") or "Prometheus returned non-success status"
        out = {
            "success": False,
            "error": str(err_msg),
            "message": "PromQL 执行失败（请检查表达式或指标名）",
            "query": query,
        }
        return json.dumps(out, ensure_ascii=False, indent=2)

    data = result.get("data") or {}
    raw_series = data.get("result") or []
    if not isinstance(raw_series, list):
        raw_series = []

    series, meta = _compact_series(raw_series)
    out = {
        "success": True,
        "query": query,
        "result_type": data.get("resultType", ""),
        "window_minutes": minutes,
        "step": step,
        "start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
        "end": datetime.fromtimestamp(end, timezone.utc).isoformat(),
        "series": series,
        **meta,
        "message": (
            f"已获取 {meta['series_returned']} 条序列"
            f"（共 {meta['series_total']} 条，窗口 {minutes} 分钟，步长 {step}）"
        ),
    }
    if meta["series_truncated"] or meta["points_truncated"]:
        out["truncated_notice"] = (
            f"结果已裁剪以控制体积：省略 {meta['series_truncated']} 条序列、"
            f"{meta['points_truncated']} 个数据点；如需更细数据请缩小时间窗或加大步长。"
        )

    logger.info(
        f"Prometheus metrics query completed: {meta['series_returned']}/{meta['series_total']} series"
    )
    return json.dumps(out, ensure_ascii=False, indent=2)
