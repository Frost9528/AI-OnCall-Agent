"""失败分类 + 有界重试测试（P0-2）

覆盖：分类确定性、可重试/不可重试分流、超时作为瞬时失败、重试次数上界、
失败记录序列化往返（resume 场景）、失败收尾报告渲染。
"""

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("app.services.vector_store_manager", MagicMock())

from app.agent.aiops.failure import (  # noqa: E402
    FailureKind,
    FailureRecord,
    FailureSummary,
    call_with_retry,
    classify_failure,
    is_retryable,
)


class TransientError(Exception):
    def __init__(self):
        super().__init__("Connection timeout while calling model")


class SchemaError(Exception):
    def __init__(self):
        super().__init__("pydantic validation error: field required (schema mismatch)")


class ForbiddenError(Exception):
    def __init__(self):
        super().__init__("403 forbidden: permission denied")


# ---------- 分类 ----------

@pytest.mark.parametrize(
    "exc,expected",
    [
        (asyncio.TimeoutError(), FailureKind.TRANSIENT),
        (TimeoutError(), FailureKind.TRANSIENT),
        (TransientError(), FailureKind.TRANSIENT),
        (ForbiddenError(), FailureKind.PERMISSION),
        (SchemaError(), FailureKind.SCHEMA),
        (ValueError("unrecognized"), FailureKind.UNKNOWN),
    ],
    ids=["asyncio_timeout", "timeout", "conn_timeout", "forbidden", "schema", "unknown"],
)
def test_classify_failure(exc, expected):
    assert classify_failure(exc) is expected


@pytest.mark.parametrize(
    "kind,retryable",
    [
        (FailureKind.TRANSIENT, True),
        (FailureKind.UNKNOWN, True),
        (FailureKind.SCHEMA, False),
        (FailureKind.PERMISSION, False),
        (FailureKind.EMPTY_DATA, False),
    ],
)
def test_is_retryable(kind, retryable):
    assert is_retryable(kind) is retryable


# ---------- 重试行为 ----------

def test_retries_transient_once_then_succeeds():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TransientError()
        return "OK"

    result = asyncio.run(call_with_retry(flaky, stage="t", max_retries=1))
    assert result.ok
    assert result.value == "OK"
    assert calls["n"] == 2, "应该恰好重试一次"


def test_bounded_retry_gives_up_and_records_failure():
    calls = {"n": 0}

    async def always():
        calls["n"] += 1
        raise TransientError()

    result = asyncio.run(call_with_retry(always, stage="t", max_retries=1))
    assert not result.ok
    assert calls["n"] == 2, "重试次数必须有界（1 次额外尝试）"
    assert result.failure is not None
    assert result.failure.kind is FailureKind.TRANSIENT
    assert result.failure.attempts == 2
    assert result.failure.retried is True


def test_schema_failure_is_not_retried():
    """确定性失败重试无意义，应只尝试一次，避免白白烧调用。"""
    calls = {"n": 0}

    async def always_schema():
        calls["n"] += 1
        raise SchemaError()

    result = asyncio.run(call_with_retry(always_schema, stage="t", max_retries=1))
    assert not result.ok
    assert calls["n"] == 1
    assert result.failure.kind is FailureKind.SCHEMA
    assert result.failure.retried is False


def test_permission_failure_is_not_retried():
    calls = {"n": 0}

    async def always_forbidden():
        calls["n"] += 1
        raise ForbiddenError()

    result = asyncio.run(call_with_retry(always_forbidden, stage="t", max_retries=2))
    assert calls["n"] == 1
    assert result.failure.kind is FailureKind.PERMISSION


def test_timeout_treated_as_transient_and_retried():
    calls = {"n": 0}

    async def slow():
        calls["n"] += 1
        await asyncio.sleep(0.5)
        return "late"

    result = asyncio.run(call_with_retry(slow, stage="t", max_retries=1, timeout=0.05))
    assert not result.ok
    assert calls["n"] == 2
    assert result.failure.kind is FailureKind.TRANSIENT


def test_timeout_disabled_when_non_positive():
    async def quick():
        return "fine"

    result = asyncio.run(call_with_retry(quick, stage="t", timeout=0))
    assert result.ok and result.value == "fine"


# ---------- 失败记录序列化（resume 场景）----------

def test_failure_record_roundtrip_restores_enum():
    """checkpoint 反序列化后 kind 只是 str，必须还原为枚举，否则渲染会崩。"""
    original = FailureRecord(stage="replanner_decision", kind=FailureKind.SCHEMA, reason="boom")
    restored = FailureRecord(**original.to_dict())
    assert restored.kind is FailureKind.SCHEMA
    assert restored.stage == original.stage
    assert restored.reason == original.reason


def test_failure_summary_accepts_dicts_and_none():
    rec = FailureRecord(stage="s", kind=FailureKind.TRANSIENT, reason="r")
    summary = FailureSummary([rec.to_dict(), None])
    assert len(summary.records) == 1
    assert summary.records[0].kind is FailureKind.TRANSIENT


def test_failure_summary_renders_reason_and_stage():
    rec = FailureRecord(
        stage="replanner_decision",
        kind=FailureKind.SCHEMA,
        reason="schema mismatch",
        attempts=2,
        retried=True,
    )
    md = FailureSummary([rec]).render(title="任务失败收尾说明")
    assert "任务失败收尾说明" in md
    assert "replanner_decision" in md
    assert "schema" in md
    assert "schema mismatch" in md
    assert "尝试次数" in md


def test_empty_failure_summary_renders_nothing():
    assert FailureSummary().render() == ""
    assert not FailureSummary()
