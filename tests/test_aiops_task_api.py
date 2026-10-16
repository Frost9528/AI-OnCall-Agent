"""AIOps 任务 API 测试（P0-3）

覆盖：提交返回 task_id、resume 的参数校验（404/403/409）、待恢复清单只含 ejected、
任务状态查询。不启动真实图执行（那需要 LLM/Milvus），只测 API 契约与注册表联动。
"""

import sys
import uuid
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("app.services.vector_store_manager", MagicMock())

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.models.task import TaskRecord, TaskStatus  # noqa: E402
from app.services.task_registry import task_registry  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _task_api_token(monkeypatch):
    from app.config import config
    monkeypatch.setattr(config, "task_api_token", "test-task-token")


def _auth():
    return {"Authorization": "Bearer test-task-token"}


def _make_task(task_id: str, status: TaskStatus, owner: str = "anonymous") -> TaskRecord:
    # 全局注册表在同一进程内跨用例共享，用 uuid 保证 task_id 唯一
    task_id = f"{task_id}-{uuid.uuid4().hex[:8]}"
    return task_registry.create(TaskRecord(
        task_id=task_id,
        owner=owner,
        session_id="s-api",
        thread_id=task_id,
        input="诊断告警",
        status=status,
        checkpoint_ref=task_id,
    ))


def test_task_api_rejects_requests_without_configured_token(monkeypatch):
    from app.config import config
    monkeypatch.setattr(config, "task_api_token", "")
    response = client.get("/api/aiops/tasks/t-secret")
    assert response.status_code == 503


def test_task_api_rejects_non_ascii_header_as_401_not_500():
    # 非 ASCII 的 Authorization 头曾让 hmac.compare_digest 抛 TypeError → 500
    response = client.get(
        "/api/aiops/tasks/t-whatever",
        headers={b"Authorization": b"Bear\xc3\xa9r tok"},
    )
    assert response.status_code == 401


def test_resume_unknown_task_returns_404():
    resp = client.post("/api/aiops/resume", json={"task_id": "t-does-not-exist"}, headers=_auth())
    assert resp.status_code == 404
    assert "不存在" in resp.json()["detail"]


def test_resume_owner_mismatch_returns_403():
    task = _make_task("t-api-owner", TaskStatus.EJECTED, owner="alice")
    resp = client.post(
        "/api/aiops/resume", json={"task_id": task.task_id, "owner": "bob"}, headers=_auth()
    )
    assert resp.status_code == 403


@pytest.mark.parametrize(
    "status",
    [TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.DONE,
     TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.EXPIRED],
    ids=["queued", "running", "done", "cancelled", "failed", "expired"],
)
def test_resume_non_ejected_task_returns_409(status):
    """仅 ejected 状态可恢复。"""
    task = _make_task(f"t-api-{status.value}", status)
    resp = client.post("/api/aiops/resume", json={"task_id": task.task_id}, headers=_auth())
    assert resp.status_code == 409


def test_get_task_returns_status():
    task = _make_task("t-api-get", TaskStatus.RUNNING)
    resp = client.get(f"/api/aiops/tasks/{task.task_id}", headers=_auth())
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["task_id"] == task.task_id
    assert data["status"] == "running"


def test_get_unknown_task_returns_404():
    resp = client.get("/api/aiops/tasks/t-nope", headers=_auth())
    assert resp.status_code == 404


def test_resumable_list_excludes_terminal():
    """待恢复清单只含 ejected；终态不出现（'不重复展示'）。"""
    ej = _make_task("t-api-ej", TaskStatus.EJECTED)
    done = _make_task("t-api-done", TaskStatus.DONE)

    resp = client.get("/api/aiops/tasks/resumable", headers=_auth())
    assert resp.status_code == 200
    ids = [t["task_id"] for t in resp.json()["data"]["tasks"]]
    assert ej.task_id in ids
    assert done.task_id not in ids


def test_submit_task_rejects_over_length_input():
    """校验先于建任务：超长输入直接 400，不产生垃圾任务记录。"""
    from app.config import config

    resp = client.post(
        "/api/aiops/tasks",
        json={"session_id": "s-api", "input": "a" * (config.input_max_length + 1)},
        headers=_auth(),
    )
    assert resp.status_code == 400


def test_submit_task_rejects_blank_input():
    resp = client.post("/api/aiops/tasks", json={"session_id": "s-api", "input": "   "}, headers=_auth())
    assert resp.status_code == 400
