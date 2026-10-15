"""工具模块 - 供 Agent 调用的各种工具"""

from app.tools.knowledge_tool import retrieve_knowledge
from app.tools.query_metrics_alerts import query_prometheus_alerts
from app.tools.query_prometheus_metrics import query_prometheus_metrics
from app.tools.time_tool import get_current_time
from app.tools.run_aiops import run_aiops
from app.tools.get_task_report import get_task_report
from app.tools.list_my_tasks import list_my_tasks

# 仅对话 Agent 应绑定的工具：run_aiops 会在排障执行体内部再派发排障，造成无限递归，
# 故 P-E-R 三节点必须显式排除这一组（见 planner/executor/replanner 的过滤）。
_AGENT_ONLY_TOOLS = {"run_aiops", "get_task_report", "list_my_tasks"}

# 默认本地工具集：凡绑定「知识库 + 时间」的 Agent 应使用此元组，与 Prometheus 告警/指标查询一并注册
DEFAULT_LOCAL_AGENT_TOOLS = (
    retrieve_knowledge,
    get_current_time,
    query_prometheus_alerts,
    query_prometheus_metrics,
    run_aiops,          # 仅对话 Agent 应绑；AIOps 执行体内需排除
    get_task_report,
    list_my_tasks,
)

__all__ = [
    "DEFAULT_LOCAL_AGENT_TOOLS",
    "_AGENT_ONLY_TOOLS",
    "retrieve_knowledge",
    "get_current_time",
    "query_prometheus_alerts",
    "query_prometheus_metrics",
    "run_aiops",
    "get_task_report",
    "list_my_tasks",
]
