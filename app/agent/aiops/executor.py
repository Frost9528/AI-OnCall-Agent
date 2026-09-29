"""
Executor 节点：执行单个步骤
基于 LangGraph 官方教程实现

可靠性（P1-1 / P1-2）：
- 单步 LLM/工具调用带**超时**（默认 60s，config 可配）；
- 失败按**确定性分类**分流：瞬时(超时/5xx)→重试；数据为空→标"无数据"不编造；
  权限/配置→跳过并留痕；schema 恒不匹配→不重试；
- 失败原因写入 ``failures`` 留痕（P0-2 的结构），供最终报告如实呈现。

分类判定在**工具边界**由代码完成，不交给 LLM（§4.3①）。
"""

from typing import Any, Dict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import ToolNode
from loguru import logger

from app.config import config
from app.tools import DEFAULT_LOCAL_AGENT_TOOLS
from app.agent.mcp_client import get_mcp_client_with_retry
from .state import PlanExecuteState
from .failure import (
    FailureKind,
    FailureRecord,
    build_llm,
    call_with_retry,
    classify_failure,
)

# 判定"数据为空"的信号：工具/模型明确表示没有数据，而非报错
_EMPTY_DATA_HINTS = (
    "无数据", "没有找到", "未找到", "no data", "not found", "no results",
    "is empty", "empty result", "没有相关", "暂无",
)

_EXECUTOR_SYSTEM_PROMPT = """你是一个能力强大的助手，负责执行具体的任务步骤。

你可以使用各种工具来完成任务。对于每个步骤：
1. 理解步骤的目标
2. 选择合适的工具，如果已经指定了工具，则使用指定的工具
3. 调用工具获取信息
4. 返回执行结果

注意：
- 如果工具调用失败，请说明失败原因
- 不要编造数据，只返回实际获取的信息
- 执行结果要清晰、准确
- 专注于当前步骤，不要考虑其他任务"""


def _looks_empty(result: str) -> bool:
    """判断结果是否属于"数据为空"（确定性判定，不调 LLM）。"""
    if not result or not result.strip():
        return True
    lowered = result.lower()
    return any(h.lower() in lowered for h in _EMPTY_DATA_HINTS)


async def executor(state: PlanExecuteState) -> Dict[str, Any]:
    """
    执行节点：执行计划中的下一个步骤

    使用 LangGraph 的 ToolNode 自动处理工具调用
    """
    logger.info("=== Executor：执行步骤 ===")

    plan = state.get("plan", [])

    # 如果计划为空，不执行
    if not plan:
        logger.info("计划为空，跳过执行")
        return {}

    # 取出第一个步骤
    task = plan[0]
    logger.info(f"当前任务: {task}")

    failures: list[FailureRecord] = []
    timeout = config.step_timeout_seconds

    try:
        # 获取本地工具
        local_tools = list(DEFAULT_LOCAL_AGENT_TOOLS)

        # 获取 MCP 工具
        mcp_client = await get_mcp_client_with_retry()
        mcp_tools = await mcp_client.get_tools()
        logger.info(f"可用工具数量: 本地 {len(local_tools)} + MCP {len(mcp_tools)}")

        all_tools = local_tools + mcp_tools

        llm = build_llm()
        llm_with_tools = llm.bind_tools(all_tools)
        tool_node = ToolNode(all_tools)

        messages = [
            SystemMessage(content=_EXECUTOR_SYSTEM_PROMPT),
            HumanMessage(content=f"请执行以下任务: {task}"),
        ]

        # 第一步：LLM 决定是否调用工具（带超时 + 有界重试）
        llm_attempt = await call_with_retry(
            lambda: llm_with_tools.ainvoke(messages),
            stage="executor_llm_decide",
            timeout=timeout,
        )
        if not llm_attempt.ok:
            reason = llm_attempt.failure.reason if llm_attempt.failure else "未知错误"
            kind = llm_attempt.failure.kind if llm_attempt.failure else FailureKind.UNKNOWN
            logger.error(f"步骤 '{task}' 的 LLM 调用失败（{kind.value}）: {reason}")
            return {
                "plan": plan[1:],
                "past_steps": [(task, f"执行失败（{kind.value}）: {reason}")],
                "failures": [llm_attempt.failure] if llm_attempt.failure else [],
            }

        llm_response = llm_attempt.value
        logger.info(f"LLM 响应类型: {type(llm_response)}")

        # 第二步：如果有工具调用，执行工具（带超时 + 重试）
        if hasattr(llm_response, "tool_calls") and llm_response.tool_calls:
            logger.info(f"检测到 {len(llm_response.tool_calls)} 个工具调用")

            messages.append(llm_response)
            tool_attempt = await call_with_retry(
                lambda: tool_node.ainvoke({"messages": messages}),
                stage="executor_tool",
                timeout=timeout,
            )

            if not tool_attempt.ok:
                failure = tool_attempt.failure
                kind = failure.kind if failure else FailureKind.UNKNOWN
                reason = failure.reason if failure else "未知错误"

                # 分类分流：权限/配置 → 跳过并留痕（不是重试能解决的）
                if kind is FailureKind.PERMISSION:
                    logger.warning(f"步骤 '{task}' 工具调用遇到权限/配置问题，跳过该步: {reason}")
                    note = f"已跳过（权限/配置问题）: {reason}"
                else:
                    logger.error(f"步骤 '{task}' 工具调用失败（{kind.value}）: {reason}")
                    note = f"执行失败（{kind.value}）: {reason}"

                return {
                    "plan": plan[1:],
                    "past_steps": [(task, note)],
                    "failures": [failure] if failure else [],
                }

            tool_messages = tool_attempt.value
            messages.extend(tool_messages["messages"])

            # 第三步：把工具结果交给 LLM 生成最终答案（带超时 + 重试）
            final_attempt = await call_with_retry(
                lambda: llm_with_tools.ainvoke(messages),
                stage="executor_llm_finalize",
                timeout=timeout,
            )
            if final_attempt.ok:
                fr = final_attempt.value
                result = fr.content if hasattr(fr, "content") else str(fr)
            else:
                # 汇总失败：直接把工具原始结果作为该步产出，并留痕（不丢已获取的数据）
                failure = final_attempt.failure
                logger.warning(
                    f"步骤 '{task}' 结果汇总失败，回退为工具原始结果: "
                    f"{failure.reason if failure else '未知'}"
                )
                raw = "\n".join(
                    str(getattr(m, "content", m))
                    for m in tool_messages["messages"]
                    if type(m).__name__ == "ToolMessage"
                )
                result = raw or "（工具未返回内容）"
                if failure:
                    failures.append(failure)
        else:
            # 没有工具调用，直接使用 LLM 的输出
            logger.info("LLM 未调用工具，直接返回结果")
            result = llm_response.content if hasattr(llm_response, "content") else str(llm_response)

        # 数据为空：明确标注，避免后续环节把"无数据"编造成结论
        if _looks_empty(result):
            logger.warning(f"步骤 '{task}' 未获取到数据，标注为无数据")
            note = (FailureRecord(
                stage="executor",
                kind=FailureKind.EMPTY_DATA,
                reason=f"步骤 '{task}' 未返回数据",
            ))
            failures.append(note)
            result = f"[无数据] {result}" if result and result.strip() else "[无数据] 该步骤未返回任何数据"

        logger.info(f"步骤执行完成，结果长度: {len(result)}")

        return {
            "plan": plan[1:],
            "past_steps": [(task, result)],
            "failures": failures,
        }

    except Exception as e:
        # 兜底：未预期异常也按分类记录，失败可见、写入执行历史
        kind = classify_failure(e)
        logger.error(f"执行步骤 '{task}' 异常（{kind.value}）: {e}", exc_info=True)
        record = FailureRecord(
            stage="executor",
            kind=kind,
            reason=f"{type(e).__name__}: {e}",
            detail=repr(e),
        )
        return {
            "plan": plan[1:],
            "past_steps": [(task, f"执行失败（{kind.value}）: {record.reason}")],
            "failures": [record],
        }
