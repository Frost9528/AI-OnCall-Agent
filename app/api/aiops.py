"""
AIOps 智能运维接口
"""

import json
from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse
from loguru import logger

from app.models.aiops import AIOpsRequest, TaskResumeRequest, TaskSubmitRequest
from app.services.aiops_service import aiops_service, default_diagnosis_input
from app.utils.input_validation import validate_user_input

router = APIRouter()


@router.post("/aiops")
async def diagnose_stream(request: AIOpsRequest):
    """
    AIOps 故障诊断接口（流式 SSE）

    **功能说明：**
    - 自动获取当前系统的活动告警
    - 使用 Plan-Execute-Replan 模式进行智能诊断
    - 流式返回诊断过程和结果

    **SSE 事件类型：**

    1. `status` - 状态更新
       ```json
       {
         "type": "status",
         "stage": "fetching_alerts",
         "message": "正在获取系统告警信息..."
       }
       ```

    2. `plan` - 诊断计划制定完成
       ```json
       {
         "type": "plan",
         "stage": "plan_created",
         "message": "诊断计划已制定，共 6 个步骤",
         "target_alert": {...},
         "plan": ["步骤1: ...", "步骤2: ..."]
       }
       ```

    3. `step_complete` - 步骤执行完成
       ```json
       {
         "type": "step_complete",
         "stage": "step_executed",
         "message": "步骤执行完成 (2/6)",
         "current_step": "查询系统日志",
         "result_preview": "...",
         "remaining_steps": 4
       }
       ```

    4. `report` - 最终诊断报告
       ```json
       {
         "type": "report",
         "stage": "final_report",
         "message": "最终诊断报告已生成",
         "report": "# 故障诊断报告\\n...",
         "evidence": {...}
       }
       ```

    5. `complete` - 诊断完成
       ```json
       {
         "type": "complete",
         "stage": "diagnosis_complete",
         "message": "诊断流程完成",
         "diagnosis": {...}
       }
       ```

    6. `error` - 错误信息
       ```json
       {
         "type": "error",
         "stage": "error",
         "message": "诊断过程发生错误: ..."
       }
       ```

    **使用示例：**
    ```bash
    curl -X POST "http://localhost:9900/api/aiops" \\
      -H "Content-Type: application/json" \\
      -d '{"session_id": "session-123"}' \\
      --no-buffer
    ```

    **前端使用示例：**
    ```javascript
    const eventSource = new EventSource('/api/aiops');

    eventSource.onmessage = (event) => {
      const data = JSON.parse(event.data);

      if (data.type === 'plan') {
        console.log('诊断计划:', data.plan);
      } else if (data.type === 'step_complete') {
        console.log('步骤完成:', data.current_step);
      } else if (data.type === 'report') {
        console.log('最终报告:', data.report);
      } else if (data.type === 'complete') {
        console.log('诊断完成');
        eventSource.close();
      }
    };
    ```

    Args:
        request: AIOps 诊断请求

    Returns:
        SSE 事件流
    """
    session_id = request.session_id or "default"

    # 校验放在生成器之外：不通过时直接 400，而不是在流内发 error 事件
    custom_input = validate_user_input(request.input, "任务描述") if request.input is not None else None

    logger.info(f"[会话 {session_id}] 收到 AIOps 诊断请求（流式）")

    async def event_generator():
        try:
            async for event in aiops_service.diagnose(session_id=session_id, user_input=custom_input):
                # 发送事件
                yield {
                    "event": "message",
                    "data": json.dumps(event, ensure_ascii=False)
                }

                # 如果是完成或错误事件，结束流
                if event.get("type") in ["complete", "error"]:
                    break

            logger.info(f"[会话 {session_id}] AIOps 诊断流式响应完成")

        except Exception as e:
            logger.error(f"[会话 {session_id}] AIOps 诊断流式响应异常: {e}", exc_info=True)
            yield {
                "event": "message",
                "data": json.dumps({
                    "type": "error",
                    "stage": "exception",
                    "message": f"诊断异常: {str(e)}"
                }, ensure_ascii=False)
            }

    return EventSourceResponse(event_generator())


def _sse(event: dict) -> dict:
    return {"event": "message", "data": json.dumps(event, ensure_ascii=False)}


@router.post("/aiops/tasks")
async def submit_task(request: TaskSubmitRequest):
    """提交一个排障任务，返回 task_id（供后续 resume / 查状态）。

    与 `/aiops` 的区别：本接口先登记任务再执行，因此客户端**同步拿到 task_id**，
    任务中断后可用 `POST /api/aiops/resume` 带着该 id 从断点续跑。

    返回 SSE 流，首帧为 `task_created`（含 task_id）。

    ```bash
    curl -X POST "http://localhost:9900/api/aiops/tasks" \\
      -H "Content-Type: application/json" \\
      -d '{"session_id": "session-123"}' --no-buffer
    ```
    """
    session_id = request.session_id or "default"

    # 校验先于建任务：不通过直接 400，不产生垃圾任务记录
    custom_input = (
        validate_user_input(request.input, "任务描述")
        if request.input is not None
        else None
    )
    task_input = custom_input if custom_input is not None else default_diagnosis_input()

    record = aiops_service.create_task(
        task_input,
        session_id=session_id,
        owner=request.owner or "anonymous",
        parent_session=session_id,
    )
    logger.info(f"[会话 {session_id}] 收到任务提交，task_id={record.task_id}")

    async def event_generator():
        yield _sse({
            "type": "task_created",
            "stage": "task_created",
            "message": "任务已创建",
            "task_id": record.task_id,
            "status": record.status.value,
        })
        try:
            async for event in aiops_service.run_task(record, resume=False):
                yield _sse(event)
                if event.get("type") in ("complete", "error"):
                    break
        except Exception as e:
            logger.error(f"[任务 {record.task_id}] 提交执行异常: {e}", exc_info=True)
            yield _sse({
                "type": "error",
                "stage": "exception",
                "message": f"任务执行异常: {str(e)}",
                "task_id": record.task_id,
            })

    return EventSourceResponse(event_generator())


@router.post("/aiops/resume")
async def resume_task(request: TaskResumeRequest):
    """从断点恢复一个被中断的任务。

    - 仅对**未终态**任务生效（终态 done/failed/cancelled/expired 会拒绝）；
    - 内部从最新 checkpoint 续跑，**不重放已完成步**；
    - 若提供 `owner`，须与任务 owner 匹配（task 级越权防护，完整鉴权属 P2）。

    ```bash
    curl -X POST "http://localhost:9900/api/aiops/resume" \\
      -H "Content-Type: application/json" \\
      -d '{"task_id": "t-8f3a1b2c3d4e"}' --no-buffer
    ```
    """
    logger.info(f"收到任务恢复请求: task_id={request.task_id}")

    # 先做存在性/状态检查，便于非法请求直接拿到明确 HTTP 状态码
    task = aiops_service.get_task(request.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {request.task_id}")
    if request.owner is not None and task.owner != request.owner:
        raise HTTPException(status_code=403, detail="无权恢复该任务")
    if task.is_terminal:
        raise HTTPException(
            status_code=409,
            detail=f"任务已处于终态（{task.status.value}），不可恢复",
        )

    async def event_generator():
        try:
            async for event in aiops_service.resume_task(
                request.task_id, owner=request.owner
            ):
                yield _sse(event)
                if event.get("type") in ("complete", "error"):
                    break
        except Exception as e:
            logger.error(f"[任务 {request.task_id}] 恢复异常: {e}", exc_info=True)
            yield _sse({
                "type": "error",
                "stage": "exception",
                "message": f"恢复异常: {str(e)}",
                "task_id": request.task_id,
            })

    return EventSourceResponse(event_generator())


@router.get("/aiops/tasks/resumable")
async def list_resumable_tasks():
    """待恢复任务清单（只含 ejected；终态不重复出现）。

    每条含 ``interrupt`` 字段：``auto`` 表示现场无副作用、可直接续跑；
    ``manual`` 表示含需人工决策的收尾/换路（或未来写侧工具的幂等风险），
    建议由工作台确认后再续。
    """
    tasks = aiops_service.list_resumable_tasks()
    return {
        "code": 200,
        "message": "success",
        "data": {
            "total": len(tasks),
            "auto_resumable": sum(1 for t in tasks if t.interrupt.value == "auto"),
            "tasks": [t.to_dict() for t in tasks],
        },
    }


@router.get("/aiops/tasks/{task_id}")
async def get_task(task_id: str):
    """查询单个任务的状态与结果。

    这是"任务跑完后结果去哪了"的答案：终态任务的 ``report`` 字段带最终报告正文。
    提交任务若走同步 SSE（``/aiops/tasks``），客户端断连后仍可凭 task_id 回到这里取报告。
    """
    task = aiops_service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    data = task.to_dict()
    data["has_report"] = bool(task.report)
    return {"code": 200, "message": "success", "data": data}
