"""Prometheus PromQL 指标查询工具测试（P1-3）

覆盖：正常时序返回、裁剪保护、PromQL 语法错误（保留原始报错）、
HTTP 错误兜底、空 query 拒绝、时间窗与步长传递。
不打真实 Prometheus：mock httpx.Client。
"""

import json
import sys
from unittest.mock import MagicMock, patch

sys.modules.setdefault("app.services.vector_store_manager", MagicMock())

import importlib  # noqa: E402

from app.tools.query_prometheus_metrics import (  # noqa: E402
    MAX_POINTS_PER_SERIES,
    MAX_SERIES,
    query_prometheus_metrics,
)

mod = importlib.import_module("app.tools.query_prometheus_metrics")


def _call(**kwargs):
    """调用 @tool 包装后的函数（直接调用其 func 更直观）。"""
    fn = getattr(query_prometheus_metrics, "func", query_prometheus_metrics)
    return fn(**kwargs)


def _fake_response(payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.text = json.dumps(payload)
    if status_code >= 400:
        import httpx

        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


def _patch_httpx(response):
    """替换 httpx.Client，使其 with 块返回一个 get() 返回 response 的对象。"""
    client = MagicMock()
    client.get.return_value = response
    ctx = MagicMock()
    ctx.__enter__.return_value = client
    ctx.__exit__.return_value = False
    return patch.object(mod.httpx, "Client", return_value=ctx), client


# ---------- 1. 正常返回 ----------

def test_successful_query_returns_series():
    payload = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"instance": "host1", "__name__": "cpu_usage"},
                    "values": [[1700000000, "0.42"], [1700000060, "0.55"]],
                }
            ],
        },
    }
    patcher, client = _patch_httpx(_fake_response(payload))
    with patcher:
        out = json.loads(_call(query="cpu_usage", minutes=10, step="60s"))

    assert out["success"] is True
    assert len(out["series"]) == 1
    assert out["series"][0]["metric"]["instance"] == "host1"
    # 值从字符串转成了数字
    assert out["series"][0]["points"][0][1] == 0.42
    assert out["series_total"] == 1
    assert out["series_truncated"] == 0


def test_window_and_step_are_passed_through():
    payload = {"status": "success", "data": {"resultType": "matrix", "result": []}}
    patcher, client = _patch_httpx(_fake_response(payload))
    with patcher:
        _call(query="up", minutes=15, step="30s")

    _, kwargs = client.get.call_args
    params = kwargs["params"]
    assert params["query"] == "up"
    assert params["step"] == "30s"
    # 时间窗应为 15 分钟
    assert abs((params["end"] - params["start"]) - 15 * 60) < 2


def test_integer_values_are_rendered_without_decimal():
    payload = {
        "status": "success",
        "data": {"resultType": "matrix", "result": [
            {"metric": {}, "values": [[1700000000, "5.0"]]},
        ]},
    }
    patcher, _ = _patch_httpx(_fake_response(payload))
    with patcher:
        out = json.loads(_call(query="x"))
    assert out["series"][0]["points"][0][1] == 5
    assert isinstance(out["series"][0]["points"][0][1], int)


# ---------- 2. 裁剪保护 ----------

def test_series_truncated_to_limit():
    payload = {
        "status": "success",
        "data": {"resultType": "matrix", "result": [
            {"metric": {"i": str(i)}, "values": [[1700000000, "1"]]}
            for i in range(MAX_SERIES + 5)
        ]},
    }
    patcher, _ = _patch_httpx(_fake_response(payload))
    with patcher:
        out = json.loads(_call(query="many"))

    assert out["series_returned"] == MAX_SERIES
    assert out["series_truncated"] == 5
    assert "truncated_notice" in out


def test_points_truncated_to_limit():
    n = MAX_POINTS_PER_SERIES * 3
    payload = {
        "status": "success",
        "data": {"resultType": "matrix", "result": [
            {"metric": {}, "values": [[1700000000 + i, str(i)] for i in range(n)]},
        ]},
    }
    patcher, _ = _patch_httpx(_fake_response(payload))
    with patcher:
        out = json.loads(_call(query="dense"))

    assert out["series"][0]["raw_point_count"] == n
    assert out["series"][0]["returned_point_count"] <= MAX_POINTS_PER_SERIES
    assert out["points_truncated"] > 0
    assert "truncated_notice" in out


def test_no_truncation_notice_when_small():
    payload = {
        "status": "success",
        "data": {"resultType": "matrix", "result": [
            {"metric": {}, "values": [[1700000000, "1"]]},
        ]},
    }
    patcher, _ = _patch_httpx(_fake_response(payload))
    with patcher:
        out = json.loads(_call(query="small"))
    assert "truncated_notice" not in out


# ---------- 3. 错误处理（保留原始报错给 LLM） ----------

def test_promql_syntax_error_returns_raw_message():
    """PromQL 语法错误必须把 Prometheus 原始报错回传，LLM 才能自我修正。"""
    payload = {
        "status": "error",
        "errorType": "bad_data",
        "error": 'parse error: unexpected identifier "rat"',
    }
    patcher, _ = _patch_httpx(_fake_response(payload))
    with patcher:
        out = json.loads(_call(query="rat(http_requests_total[5m])"))

    assert out["success"] is False
    assert "parse error" in out["error"]
    assert out["query"] == "rat(http_requests_total[5m])"


def test_http_error_is_wrapped_not_raised():
    import httpx

    resp = MagicMock()
    resp.status_code = 500
    resp.text = "internal error"
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500", request=MagicMock(), response=resp
    )
    patcher, _ = _patch_httpx(resp)
    with patcher:
        out = json.loads(_call(query="up"))

    assert out["success"] is False
    assert "500" in out["error"]


def test_connection_error_is_wrapped():
    import httpx

    client = MagicMock()
    client.get.side_effect = httpx.ConnectError("connection refused")
    ctx = MagicMock()
    ctx.__enter__.return_value = client
    ctx.__exit__.return_value = False

    with patch.object(mod.httpx, "Client", return_value=ctx):
        out = json.loads(_call(query="up"))

    assert out["success"] is False
    assert "connection refused" in out["error"] or "failed to query" in out["error"]


def test_empty_query_rejected_without_network():
    """空 query 不应发出网络请求。"""
    client = MagicMock()
    ctx = MagicMock()
    ctx.__enter__.return_value = client
    ctx.__exit__.return_value = False
    with patch.object(mod.httpx, "Client", return_value=ctx):
        out = json.loads(_call(query="   "))
    assert out["success"] is False
    assert client.get.called is False


# ---------- 4. 注册进默认工具集 ----------

def test_tool_registered_in_default_set():
    from app.tools import DEFAULT_LOCAL_AGENT_TOOLS

    names = [getattr(t, "name", "") for t in DEFAULT_LOCAL_AGENT_TOOLS]
    assert "query_prometheus_metrics" in names
    assert "query_prometheus_alerts" in names


def test_tool_has_description_for_llm():
    fn = getattr(query_prometheus_metrics, "func", None)
    desc = getattr(query_prometheus_metrics, "description", "") or (fn.__doc__ or "")
    assert "PromQL" in desc
    assert "query_range" in desc
