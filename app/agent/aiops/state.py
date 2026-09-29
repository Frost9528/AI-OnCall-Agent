"""
通用 Plan-Execute-Replan 状态定义
基于 LangGraph 官方教程实现
"""

from typing import Any, Dict, List, TypedDict, Annotated
import operator


class PlanExecuteState(TypedDict):
    """Plan-Execute-Replan 状态"""

    # 用户输入（任务描述）
    input: str

    # 执行计划（步骤列表）
    plan: List[str]

    # 已执行的步骤历史
    # 使用 operator.add 实现追加式更新（而非覆盖）
    past_steps: Annotated[List[tuple], operator.add]

    # 最终响应/报告
    response: str

    # 失败留痕（P0-2）：规划等环节的失败原因，供失败收尾报告与审计使用。
    # 追加式更新，避免后续节点覆盖先前记录的失败。
    failures: Annotated[List[Dict[str, Any]], operator.add]
