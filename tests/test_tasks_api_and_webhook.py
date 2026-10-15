"""任务列表接口 + 告警 webhook 派发。"""
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault("app.services.vector_store_manager", MagicMock())

from fastapi.testclient import TestClient  # noqa: E402

from app.models.task import TaskRecord, TaskStatus, TriggerSource  # noqa: E402
from app.services.task_registry import TaskRegistry  # noqa: E402


def test_list_tasks_filters_by_status(tmp_path):
    import app.api.aiops as aiops_api
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    reg.create(TaskRecord(task_id="r1", session_id="s", thread_id="r1",
                          input="x", status=TaskStatus.RUNNING))
    reg.create(TaskRecord(task_id="d1", session_id="s", thread_id="d1",
                          input="y", status=TaskStatus.DONE, report="rep"))
    with patch.object(aiops_api, "task_registry", reg):
        from fastapi import FastAPI
        app = FastAPI(); app.include_router(aiops_api.router, prefix="/api")
        c = TestClient(app)
        r = c.get("/api/aiops/tasks?status=done")
        assert r.status_code == 200
        ids = [t["task_id"] for t in r.json()["data"]["tasks"]]
        assert ids == ["d1"], f"应只返回 done，实际 {ids}"


def test_webhook_dispatches_alert_task(monkeypatch):
    import app.api.webhook as wh
    captured = {}

    class FakeDispatcher:
        async def dispatch(self, user_input, **kw):
            captured.update(kw); captured["input"] = user_input
            return TaskRecord(task_id="t-alert", session_id="alert", thread_id="t-alert",
                              input=user_input, trigger=TriggerSource.ALERT)

    monkeypatch.setattr(wh, "dispatcher", FakeDispatcher())
    payload = {
        "status": "firing",
        "alerts": [{
            "labels": {"alertname": "HighCPU", "instance": "web-01", "severity": "critical"},
            "annotations": {"summary": "CPU 持续 90%"},
            "startsAt": "2026-10-14T14:00:00Z",
        }],
    }
    from fastapi import FastAPI
    app = FastAPI(); app.include_router(wh.router, prefix="/api")
    c = TestClient(app)
    r = c.post("/api/aiops/webhook", json=payload)
    assert r.status_code == 200
    assert captured["trigger"] is TriggerSource.ALERT
    assert captured["parent_session"] is None
    assert captured["time_anchor"] is not None, "time_anchor 应取告警 startsAt"
    assert "HighCPU" in captured["input"]


def _aiops_client(registry):
    import app.api.aiops as aiops_api
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(aiops_api.router, prefix="/api")
    return app, patch.object(aiops_api, "task_registry", registry)


def test_list_tasks_rejects_invalid_status(tmp_path):
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    app, registry_patch = _aiops_client(reg)
    with registry_patch:
        response = TestClient(app).get("/api/aiops/tasks?status=not-a-status")
    assert response.status_code == 400


def test_list_tasks_accepts_multiple_statuses(tmp_path):
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    for task_id, status in (("r1", TaskStatus.RUNNING), ("d1", TaskStatus.DONE)):
        reg.create(TaskRecord(task_id=task_id, session_id="s", thread_id=task_id,
                              input="x", status=status))
    app, registry_patch = _aiops_client(reg)
    with registry_patch:
        response = TestClient(app).get("/api/aiops/tasks?status=running,done")
    assert response.status_code == 200
    assert {task["task_id"] for task in response.json()["data"]["tasks"]} == {"r1", "d1"}


def test_list_tasks_without_status_returns_all(tmp_path):
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    for task_id, status in (("r1", TaskStatus.RUNNING), ("d1", TaskStatus.DONE)):
        reg.create(TaskRecord(task_id=task_id, session_id="s", thread_id=task_id,
                              input="x", status=status))
    app, registry_patch = _aiops_client(reg)
    with registry_patch:
        response = TestClient(app).get("/api/aiops/tasks")
    assert response.status_code == 200
    assert response.json()["data"]["total"] == 2


def test_list_tasks_rejects_non_positive_limit(tmp_path):
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    app, registry_patch = _aiops_client(reg)
    with registry_patch:
        response = TestClient(app).get("/api/aiops/tasks?limit=0")
    assert response.status_code == 422


def test_webhook_ignores_non_firing_or_empty_alerts(monkeypatch):
    import app.api.webhook as wh
    calls = []

    class FakeDispatcher:
        async def dispatch(self, user_input, **kw):
            calls.append((user_input, kw))
            raise AssertionError("ignored webhook must not dispatch")

    monkeypatch.setattr(wh, "dispatcher", FakeDispatcher())
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(wh.router, prefix="/api")
    client = TestClient(app)
    for payload in ({"status": "resolved", "alerts": [{}]}, {"status": "firing", "alerts": []}):
        response = client.post("/api/aiops/webhook", json=payload)
        assert response.status_code == 200
        assert response.json()["data"]["dispatched"] == 0
    assert calls == []


@pytest.mark.parametrize("starts_at", [12345, "not-a-timestamp"])
def test_webhook_malformed_starts_at_degrades_without_500(monkeypatch, starts_at):
    import app.api.webhook as wh
    captured = {}

    class FakeDispatcher:
        async def dispatch(self, user_input, **kw):
            captured.update(kw)
            return TaskRecord(task_id="t-malformed", session_id="alert", thread_id="t-malformed",
                              input=user_input, trigger=TriggerSource.ALERT)

    monkeypatch.setattr(wh, "dispatcher", FakeDispatcher())
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(wh.router, prefix="/api")
    response = TestClient(app).post("/api/aiops/webhook", json={
        "status": "firing",
        "alerts": [{"labels": {"alertname": "BadStart"}, "startsAt": starts_at}],
    })
    assert response.status_code == 200
    assert response.json()["data"]["task_id"] == "t-malformed"
    assert captured["time_anchor"] is None
