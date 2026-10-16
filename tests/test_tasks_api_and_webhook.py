"""任务列表接口 + 告警 webhook 派发。"""
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault("app.services.vector_store_manager", MagicMock())

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.config import config  # noqa: E402

from app.models.task import TaskRecord, TaskStatus, TriggerSource  # noqa: E402
from app.services.task_registry import TaskRegistry  # noqa: E402


@pytest.fixture(autouse=True)
def _task_api_token(monkeypatch):
    monkeypatch.setattr(config, "task_api_token", "test-task-token")


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
        r = c.get("/api/aiops/tasks?status=done", headers={"Authorization": "Bearer test-task-token"})
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
    monkeypatch.setattr(config, "alert_webhook_token", "test-webhook-token")
    payload = {
        "status": "firing",
        "groupKey": "group-1",
        "alerts": [{
            "fingerprint": "fp-1",
            "labels": {"alertname": "HighCPU", "instance": "web-01", "severity": "critical"},
            "annotations": {"summary": "CPU 持续 90%"},
            "startsAt": "2026-10-14T14:00:00Z",
        }],
    }
    from fastapi import FastAPI
    app = FastAPI(); app.include_router(wh.router, prefix="/api")
    c = TestClient(app)
    r = c.post("/api/aiops/webhook", json=payload,
               headers={"Authorization": "Bearer test-webhook-token"})
    assert r.status_code == 200
    assert captured["trigger"] is TriggerSource.ALERT
    assert captured["parent_session"] is None
    assert captured["time_anchor"] is not None, "time_anchor 应取告警 startsAt"
    assert captured["idempotency_key"] is not None
    assert "HighCPU" in captured["input"]



def test_webhook_retries_with_same_alert_fingerprint_return_one_task(tmp_path, monkeypatch):
    import app.api.webhook as wh
    registry = TaskRegistry(str(tmp_path / "alerts.sqlite"))

    class FakeDispatcher:
        async def dispatch(self, user_input, **kwargs):
            record = TaskRecord(
                task_id=f"t-{len(registry.list_by_status([], limit=20))}",
                session_id="alert", thread_id="alert", input=user_input,
                trigger=TriggerSource.ALERT, idempotency_key=kwargs["idempotency_key"],
            )
            return registry.create(record)

    monkeypatch.setattr(wh, "dispatcher", FakeDispatcher())
    monkeypatch.setattr(config, "alert_webhook_token", "test-webhook-token")
    app = FastAPI()
    app.include_router(wh.router, prefix="/api")
    client = TestClient(app)
    payload = {
        "status": "firing", "groupKey": "group-1",
        "alerts": [{"fingerprint": "fp-1", "startsAt": "2026-10-14T14:00:00Z"}],
    }
    headers = {"Authorization": "Bearer test-webhook-token"}
    first = client.post("/api/aiops/webhook", json=payload, headers=headers)
    second = client.post("/api/aiops/webhook", json=payload, headers=headers)
    assert first.json()["data"]["task_id"] == second.json()["data"]["task_id"]
    assert len(registry.list_by_status([], limit=20)) == 1


def _aiops_client(registry):
    import app.api.aiops as aiops_api
    app = FastAPI()
    app.include_router(aiops_api.router, prefix="/api")
    return app, patch.object(aiops_api, "task_registry", registry)


def test_list_tasks_rejects_invalid_status(tmp_path):
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    app, registry_patch = _aiops_client(reg)
    with registry_patch:
        response = TestClient(app).get(
            "/api/aiops/tasks?status=not-a-status",
            headers={"Authorization": "Bearer test-task-token"},
        )
    assert response.status_code == 400


def test_list_tasks_accepts_multiple_statuses(tmp_path):
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    for task_id, status in (("r1", TaskStatus.RUNNING), ("d1", TaskStatus.DONE)):
        reg.create(TaskRecord(task_id=task_id, session_id="s", thread_id=task_id,
                              input="x", status=status))
    app, registry_patch = _aiops_client(reg)
    with registry_patch:
        response = TestClient(app).get(
            "/api/aiops/tasks?status=running,done",
            headers={"Authorization": "Bearer test-task-token"},
        )
    assert response.status_code == 200
    assert {task["task_id"] for task in response.json()["data"]["tasks"]} == {"r1", "d1"}


def test_list_tasks_without_status_returns_all(tmp_path):
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    for task_id, status in (("r1", TaskStatus.RUNNING), ("d1", TaskStatus.DONE)):
        reg.create(TaskRecord(task_id=task_id, session_id="s", thread_id=task_id,
                              input="x", status=status))
    app, registry_patch = _aiops_client(reg)
    with registry_patch:
        response = TestClient(app).get(
            "/api/aiops/tasks",
            headers={"Authorization": "Bearer test-task-token"},
        )
    assert response.status_code == 200
    assert response.json()["data"]["total"] == 2


def test_list_tasks_rejects_non_positive_limit(tmp_path):
    reg = TaskRegistry(str(tmp_path / "t.sqlite"))
    app, registry_patch = _aiops_client(reg)
    with registry_patch:
        response = TestClient(app).get(
            "/api/aiops/tasks?limit=0",
            headers={"Authorization": "Bearer test-task-token"},
        )
    assert response.status_code == 422


@pytest.mark.parametrize("nested", [
    {"labels": None, "annotations": {"summary": "s"}},
    {"labels": {"alertname": "A"}, "annotations": None},
    {"labels": ["unexpected"], "annotations": 12},
])
def test_webhook_null_nested_fields_do_not_500(monkeypatch, nested):
    import app.api.webhook as wh

    class FakeDispatcher:
        async def dispatch(self, user_input, **kwargs):
            return TaskRecord(task_id="t-null-fields", session_id="alert", thread_id="t-null-fields",
                              input=user_input, trigger=TriggerSource.ALERT)

    monkeypatch.setattr(wh, "dispatcher", FakeDispatcher())
    monkeypatch.setattr(config, "alert_webhook_token", "test-webhook-token")
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(wh.router, prefix="/api")
    response = TestClient(app).post("/api/aiops/webhook", json={
        "status": "firing", "alerts": [nested],
    }, headers={"Authorization": "Bearer test-webhook-token"})
    assert response.status_code == 200
    assert response.json()["data"]["task_id"] == "t-null-fields"


def test_webhook_ignores_non_firing_or_empty_alerts(monkeypatch):
    import app.api.webhook as wh
    calls = []

    class FakeDispatcher:
        async def dispatch(self, user_input, **kw):
            calls.append((user_input, kw))
            raise AssertionError("ignored webhook must not dispatch")

    monkeypatch.setattr(wh, "dispatcher", FakeDispatcher())
    monkeypatch.setattr(config, "alert_webhook_token", "test-webhook-token")
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(wh.router, prefix="/api")
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-webhook-token"}
    for payload in ({"status": "resolved", "alerts": [{}]}, {"status": "firing", "alerts": []}):
        response = client.post("/api/aiops/webhook", json=payload,
                                headers={"Authorization": "Bearer test-webhook-token"})
        assert response.status_code == 200
        assert response.json()["data"]["dispatched"] == 0
    assert calls == []


def test_webhook_requires_configured_bearer_secret(monkeypatch):
    import app.api.webhook as wh
    monkeypatch.setattr(config, "alert_webhook_token", "")
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(wh.router, prefix="/api")
    response = TestClient(app).post("/api/aiops/webhook", json={"status": "firing", "alerts": [{}]})
    assert response.status_code == 503


def test_webhook_rejects_wrong_bearer_secret(monkeypatch):
    import app.api.webhook as wh
    monkeypatch.setattr(config, "alert_webhook_token", "correct")
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(wh.router, prefix="/api")
    response = TestClient(app).post(
        "/api/aiops/webhook", json={"status": "firing", "alerts": [{}]},
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401


def test_webhook_rejects_non_ascii_header_as_401_not_500(monkeypatch):
    # 非 ASCII 的 Authorization 头曾让 hmac.compare_digest 抛 TypeError → 500
    import app.api.webhook as wh
    monkeypatch.setattr(config, "alert_webhook_token", "correct")
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(wh.router, prefix="/api")
    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/aiops/webhook", json={"status": "firing", "alerts": [{}]},
        headers={b"Authorization": b"Bear\xc3\xa9r tok"},
    )
    assert response.status_code == 401


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
    monkeypatch.setattr(config, "alert_webhook_token", "test-webhook-token")
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(wh.router, prefix="/api")
    response = TestClient(app).post("/api/aiops/webhook", json={
        "status": "firing",
        "alerts": [{"labels": {"alertname": "BadStart"}, "startsAt": starts_at}],
    }, headers={"Authorization": "Bearer test-webhook-token"})
    assert response.status_code == 200
    assert response.json()["data"]["task_id"] == "t-malformed"
    assert captured["time_anchor"] is None
