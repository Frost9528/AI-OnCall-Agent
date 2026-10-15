"""
AIOps 请求和响应模型
"""

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class AIOpsRequest(BaseModel):
    """AIOps 诊断请求"""

    session_id: Optional[str] = Field(
        default="default",
        description="会话ID，用于追踪诊断历史"
    )

    input: Optional[str] = Field(
        default=None,
        description="自定义排障任务描述；不传则使用内置诊断模板"
    )

    owner: Optional[str] = Field(
        default=None,
        description="任务归属标识（本期占位，鉴权完整实现属 P2）"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "session_id": "session-123"
            }
        }


class TaskSubmitRequest(BaseModel):
    """提交排障任务请求（返回 task_id，供后续 resume）"""

    session_id: str = Field(default="default", description="会话ID")
    input: Optional[str] = Field(
        default=None, description="排障任务描述；不传则使用内置诊断模板"
    )
    owner: Optional[str] = Field(default=None, description="任务归属（本期占位）")


class TaskResumeRequest(BaseModel):
    """恢复任务请求"""

    task_id: str = Field(..., description="任务ID")
    owner: Optional[str] = Field(
        default=None, description="调用方身份；提供时须与任务 owner 匹配"
    )


class AlertmanagerPayload(BaseModel):
    """Alertmanager webhook 载荷（只取需要的字段，宽容匹配）。"""
    status: str = "firing"
    alerts: List[Dict[str, Any]] = Field(default_factory=list)


class AlertInfo(BaseModel):
    """告警信息"""
    alertname: str
    severity: str
    instance: str
    duration: str
    description: Optional[str] = None


class DiagnosisResponse(BaseModel):
    """诊断响应（非流式）"""
    
    code: int = 200
    message: str = "success"
    data: Dict[str, Any]
    
    class Config:
        json_schema_extra = {
            "example": {
                "code": 200,
                "message": "success",
                "data": {
                    "status": "completed",
                    "target_alert": {
                        "alertname": "HighCPUUsage",
                        "severity": "critical"
                    },
                    "diagnosis": {
                        "root_cause": "数据库连接池耗尽",
                        "recommendations": ["扩容数据库连接池", "优化SQL查询"]
                    }
                }
            }
        }
