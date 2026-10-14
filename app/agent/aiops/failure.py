"""LLM / 工具调用的失败分类与有界重试

设计要点（对齐 ENGINEERING-DESIGN §4.2.3 与 §4.3①）：
- **分类判定不交给 LLM**：由代码按异常类型/消息做确定性判断，避免"让模型做判断题"。
- **有界重试**：只为「瞬时」类失败重试（网络/超时/5xx/限流），schema 恒不匹配这类
  确定性失败重试无意义，直接判终态，避免白白烧调用。
- **失败可见**：重试仍失败时产出结构化失败原因，供上层生成显式失败收尾报告，
  而不是静默 `return {}`。

本模块是 P0-2（失败可见性）、P1-1（LLM 重试）、P1-2（单步超时+分类）的**统一底层**，
避免出现「中间件重试 + 自写重试」两套并存。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Sequence, TypeVar

from loguru import logger

from app.config import config

T = TypeVar("T")


class FailureKind(str, Enum):
    """失败分类（先做三类粗分类，见 §4.3①）"""

    TRANSIENT = "transient"       # 瞬时：超时 / 网络 / 5xx / 限流 → 可重试
    EMPTY_DATA = "empty_data"     # 数据为空：返回空/无匹配 → 标"无数据"，不编造，不重试
    PERMISSION = "permission"     # 权限/配置：无权限/配置缺失 → 跳过并留痕，不重试
    SCHEMA = "schema"             # 结构化输出恒不匹配 → 确定性失败，不重试
    UNKNOWN = "unknown"           # 未识别 → 保守：可重试一次


# 判定关键词：按异常消息/类型做确定性匹配（不依赖 LLM）
_TRANSIENT_HINTS = (
    "timeout", "timed out", "connection", "connect", "temporarily",
    "unavailable", "rate limit", "ratelimit", "too many requests",
    "502", "503", "504", "reset by peer", "broken pipe", "dns",
)
_PERMISSION_HINTS = (
    "permission", "unauthorized", "forbidden", "401", "403",
    "api key", "apikey", "credential", "not configured", "not found",
    "no such", "access denied",
)
_SCHEMA_HINTS = (
    "validation", "schema", "parse", "parsing", "json", "pydantic",
    "could not parse", "failed to parse", "output_pars", "structured",
)


def classify_failure(exc: BaseException) -> FailureKind:
    """把异常确定性归入某一失败类型。

    判定顺序：超时/瞬时 → 权限 → schema → 未知。
    未知按「保守可重试」处理，避免因分类不全而放弃一次本可成功的调用。
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return FailureKind.TRANSIENT

    message = f"{type(exc).__name__}: {exc}".lower()

    if any(h in message for h in _TRANSIENT_HINTS):
        return FailureKind.TRANSIENT
    if any(h in message for h in _PERMISSION_HINTS):
        return FailureKind.PERMISSION
    if any(h in message for h in _SCHEMA_HINTS):
        return FailureKind.SCHEMA
    return FailureKind.UNKNOWN


def is_retryable(kind: FailureKind) -> bool:
    """只有瞬时类与未识别类值得重试。"""
    return kind in (FailureKind.TRANSIENT, FailureKind.UNKNOWN)


@dataclass
class FailureRecord:
    """一次失败的可见留痕（供失败收尾报告与执行历史使用）

    ``kind`` 在构造时会从字符串还原为枚举——checkpoint 反序列化后它只是 str，
    而下游渲染要读 ``.kind.value``，不还原会在 resume 场景下崩。
    """

    stage: str                      # 发生阶段，如 "replanner_decision"
    kind: FailureKind
    reason: str                     # 人类可读原因
    attempts: int = 1               # 实际尝试次数（含首次）
    retried: bool = False
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, FailureKind):
            self.kind = FailureKind(self.kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "kind": self.kind.value,
            "reason": self.reason,
            "attempts": self.attempts,
            "retried": self.retried,
            "detail": self.detail,
        }


@dataclass
class AttemptResult:
    """带失败信息的结果包装。"""

    value: Any = None
    failure: FailureRecord | None = None
    ok: bool = True


async def call_with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    stage: str,
    max_retries: int | None = None,
    timeout: float | None = None,
) -> AttemptResult:
    """带超时与有界重试地调用 ``fn``。

    - ``timeout``（秒，默认取 ``config.step_timeout_seconds``）为单次尝试超时；
      超时按 TRANSIENT 处理。
    - 仅对可重试类型重试，最多 ``max_retries`` 次额外尝试（默认 ``config.llm_max_retries``）。
    - 全部失败时返回带 ``FailureRecord`` 的 ``AttemptResult``，**不抛异常**，
      由调用方决定如何生成显式失败收尾，从而保证"失败可见"。
    """
    retries = config.llm_max_retries if max_retries is None else max_retries
    per_attempt_timeout = config.step_timeout_seconds if timeout is None else timeout

    last_failure: FailureRecord | None = None
    total_attempts = 0

    for attempt in range(retries + 1):
        total_attempts = attempt + 1
        try:
            if per_attempt_timeout and per_attempt_timeout > 0:
                value = await asyncio.wait_for(fn(), timeout=per_attempt_timeout)
            else:
                value = await fn()
            if attempt > 0:
                logger.info(f"[{stage}] 第 {total_attempts} 次尝试成功（此前失败已重试）")
            return AttemptResult(value=value, ok=True)

        except Exception as exc:  # noqa: BLE001 - 需对所有异常做分类
            kind = classify_failure(exc)
            last_failure = FailureRecord(
                stage=stage,
                kind=kind,
                reason=f"{type(exc).__name__}: {exc}",
                attempts=total_attempts,
                retried=total_attempts > 1,
                detail=repr(exc),
            )
            retryable = is_retryable(kind)
            logger.warning(
                f"[{stage}] 第 {total_attempts} 次尝试失败 "
                f"(kind={kind.value}, retryable={retryable}): {last_failure.reason}"
            )
            if not retryable or attempt >= retries:
                break

    assert last_failure is not None
    logger.error(
        f"[{stage}] 失败收尾：kind={last_failure.kind.value}, "
        f"attempts={last_failure.attempts}, reason={last_failure.reason}"
    )
    return AttemptResult(value=None, failure=last_failure, ok=False)


@dataclass
class FailureSummary:
    """聚合多个失败记录，用于渲染失败收尾报告。

    构造时接受 ``FailureRecord`` 或已序列化的 dict（来自 checkpoint 中的状态），
    因此恢复运行（resume）时也能正确渲染。
    """

    records: list[FailureRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        coerced: list[FailureRecord] = []
        for item in self.records:
            if item is None:
                continue
            coerced.append(item if isinstance(item, FailureRecord) else FailureRecord(**item))
        self.records = coerced

    def add(self, record: FailureRecord | None) -> None:
        if record is None:
            return
        if not isinstance(record, FailureRecord):
            record = FailureRecord(**record)
        self.records.append(record)

    def __bool__(self) -> bool:
        return bool(self.records)

    def render(self, title: str = "任务失败收尾说明") -> str:
        """渲染为 Markdown 段落，明确列出失败原因（可读、可审计）。"""
        if not self.records:
            return ""
        lines = [f"## ⚠️ {title}", ""]
        for i, r in enumerate(self.records, 1):
            lines.append(f"{i}. **阶段**: `{r.stage}`")
            lines.append(f"   - **失败类型**: `{r.kind.value}`")
            lines.append(f"   - **尝试次数**: {r.attempts}{'（已重试）' if r.retried else '（未重试）'}")
            lines.append(f"   - **原因**: {r.reason}")
            if r.detail and r.detail != r.reason:
                lines.append(f"   - **详情**: `{r.detail}`")
            lines.append("")
        return "\n".join(lines)


# ---------- LLM 构建与调用（P1-1：统一重试入口，避免第二套机制）----------

def build_llm(temperature: float = 0.0):
    """构建 P-E-R 各节点用的 LLM。

    所有节点统一从这里取模型，便于集中调整（切模型/提供方/超时参数）。
    不传 model，交给 `llm_factory` 按 config.llm_provider 决定用哪个模型。
    """
    from app.core.llm_factory import llm_factory

    return llm_factory.create_chat_model(temperature=temperature)


async def call_llm_chain(
    chain,
    payload: dict,
    *,
    stage: str,
    timeout: float | None = None,
) -> AttemptResult:
    """带分类与有界重试地调用一个 LLM chain。

    这是 P0-2/P1-1 共用的**唯一**重试入口——不要再在别处叠加第二套重试
    （计划 §P1-1 风险项：避免"中间件重试 + 自写重试"并存）。
    """
    return await call_with_retry(
        lambda: chain.ainvoke(payload), stage=stage, timeout=timeout
    )
