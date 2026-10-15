"""任务列表接口 + 告警 webhook 派发。"""
import sys
from unittest.mock import MagicMock, patch

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
