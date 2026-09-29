"""API 层输入校验的接口级测试（P0-4）

用 TestClient 打通真实路由，确认空/纯空白/超长输入返回 400 且不进入 Agent。
Milvus 未启动时导入链路会硬失败（vector_store_manager 在 import 期连接），
故先 stub 掉向量存储模块——本测试不涉及检索。
"""

import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("app.services.vector_store_manager", MagicMock())

from fastapi.testclient import TestClient  # noqa: E402

from app.config import config  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)

OVER = "a" * (config.input_max_length + 1)


@pytest.mark.parametrize(
    "payload",
    [
        {"Id": "s1", "Question": ""},
        {"Id": "s1", "Question": "   \t\n "},
        {"Id": "s1", "Question": OVER},
    ],
    ids=["empty", "whitespace", "over_length"],
)
def test_chat_rejects_invalid_input(payload):
    resp = client.post("/api/chat", json=payload)
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "payload",
    [
        {"Id": "s1", "Question": ""},
        {"Id": "s1", "Question": "  "},
        {"Id": "s1", "Question": OVER},
    ],
    ids=["empty", "whitespace", "over_length"],
)
def test_chat_stream_rejects_invalid_input(payload):
    resp = client.post("/api/chat_stream", json=payload)
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "custom_input",
    ["", " \n ", OVER],
    ids=["empty", "whitespace", "over_length"],
)
def test_aiops_rejects_invalid_custom_input(custom_input):
    resp = client.post("/api/aiops", json={"session_id": "s1", "input": custom_input})
    assert resp.status_code == 400


def test_aiops_without_input_still_accepted():
    """不传 input 时沿用内置模板，不应因校验被拒。

    这里只断言"没被 400 拦住"，不断言执行结果——真实诊断需要 LLM 与 Milvus。
    """
    resp = client.post("/api/aiops", json={"session_id": "s1"})
    assert resp.status_code != 400


def test_over_length_detail_mentions_field_and_limit():
    resp = client.post("/api/chat", json={"Id": "s1", "Question": OVER})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "问题" in detail
    assert str(config.input_max_length) in detail
