"""多模态版面解析测试（P1-4）

验收口径（用户已确认，因无云端凭证）：
① 无 key / 未启用时**不影响启动与纯文本链路**（降级而非报错）；
② 用 mock 假响应验证**四类块归一化** + metadata 扩展正确。

红线：不新增 embedding、不引 ChromaDB、复用现有 biz collection。
"""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault("app.services.vector_store_manager", MagicMock())

from langchain_core.documents import Document  # noqa: E402

from app.config import config  # noqa: E402
from app.services.layout_parser_service import (  # noqa: E402
    LAYOUT_TYPES,
    LayoutParserService,
    LayoutBlock,
)


@pytest.fixture
def svc_disabled():
    """未启用（无 key）的服务实例。"""
    with patch.object(config, "ocr_enabled", False), \
         patch.object(config, "ocr_api_key", ""), \
         patch.object(config, "ocr_api_url", ""):
        return LayoutParserService()


@pytest.fixture
def svc_enabled():
    """已配置凭证的服务实例（不会真的发请求，调用处会 mock）。"""
    with patch.object(config, "ocr_enabled", True), \
         patch.object(config, "ocr_api_key", "fake-key"), \
         patch.object(config, "ocr_api_url", "https://example.test/ocr"):
        return LayoutParserService()


# ---------- 1. 降级契约（最重要：不影响主链路） ----------

def test_disabled_returns_degraded_not_raises(svc_disabled, tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    result = svc_disabled.parse_document(str(pdf))

    assert result.degraded is True
    assert result.blocks == []
    assert "未启用" in result.reason or "配置" in result.reason


def test_missing_file_returns_degraded(svc_enabled, tmp_path):
    result = svc_enabled.parse_document(str(tmp_path / "nope.pdf"))
    assert result.degraded is True
    assert "不存在" in result.reason


def test_unsupported_extension_returns_degraded(svc_enabled, tmp_path):
    f = tmp_path / "a.docx"
    f.write_text("x")
    result = svc_enabled.parse_document(str(f))
    assert result.degraded is True
    assert "不支持" in result.reason


def test_cloud_failure_returns_degraded_not_raises(svc_enabled, tmp_path):
    """云端调用异常必须被吞掉并降级，不能打断上传。"""
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    with patch.object(svc_enabled, "_call_cloud_ocr", side_effect=RuntimeError("cloud down")):
        result = svc_enabled.parse_document(str(pdf))

    assert result.degraded is True
    assert "失败" in result.reason


def test_enabled_requires_all_three_settings(tmp_path):
    """只配 key 不配 url 时仍视为未启用。"""
    with patch.object(config, "ocr_enabled", True), \
         patch.object(config, "ocr_api_key", "k"), \
         patch.object(config, "ocr_api_url", ""):
        svc = LayoutParserService()
    assert svc.enabled is False


# ---------- 2. 四类块归一化 ----------

def test_normalize_four_layout_types():
    """文本/表格/公式/图像四类都应被识别。"""
    payload = {
        "pages": [{
            "page_index": 0,
            "blocks": [
                {"label": "text", "content": "这是一段正文", "bbox": [0, 0, 10, 10], "order": 0},
                {"label": "table", "content": "<table><tr><td>1</td></tr></table>", "bbox": [0, 20, 100, 60]},
                {"label": "formula", "content": "$$E=mc^2$$", "bbox": [0, 70, 50, 90]},
                {"label": "image", "content": "", "image_path": "imgs/fig1.png", "bbox": [0, 100, 200, 300]},
            ],
        }]
    }
    svc = LayoutParserService()
    blocks = svc._normalize_blocks(payload, source=__import__("pathlib").Path("a.pdf"))

    types = [b.layout_type for b in blocks]
    assert set(types) == set(LAYOUT_TYPES), f"应含四类块，实际 {types}"
    assert blocks[0].bbox == [0.0, 0.0, 10.0, 10.0]
    assert blocks[3].image_path == "imgs/fig1.png"


def test_normalize_unknown_label_falls_back_to_text():
    """未知标签保守归为 text，避免丢内容。"""
    b = LayoutBlock(layout_type="weird-label", content="x", page_index=0, block_order=0)
    assert b.layout_type == "text"


def test_normalize_paddle_style_payload():
    """兼容飞桨 PP-StructureV3 的 result.layoutParsingResults 形状。"""
    payload = {
        "result": {
            "layoutParsingResults": [{
                "prunedResult": {
                    "page_index": 0,
                    "layout_blocks": [
                        {"type": "table", "text": "<table></table>"},
                        {"type": "text", "text": "正文"},
                    ],
                }
            }]
        }
    }
    svc = LayoutParserService()
    blocks = svc._normalize_blocks(payload, source=__import__("pathlib").Path("a.pdf"))
    assert len(blocks) == 2
    assert {b.layout_type for b in blocks} == {"table", "text"}


def test_normalize_chinese_labels():
    payload = {"pages": [{"blocks": [
        {"label": "表格", "content": "<table/>"},
        {"label": "公式", "content": "x^2"},
        {"label": "图片", "content": ""},
    ]}]}
    svc = LayoutParserService()
    blocks = svc._normalize_blocks(payload, source=__import__("pathlib").Path("a.pdf"))
    assert [b.layout_type for b in blocks] == ["table", "formula", "image"]


def test_page_markdown_fallback_when_no_blocks():
    """该页无块但有 markdown 时降级为整页文本块（不丢内容）。"""
    payload = {"result": {"layoutParsingResults": [{
        "prunedResult": {"page_index": 0, "layout_blocks": []},
        "markdown": {"text": "# 标题\n正文内容"},
    }]}}
    svc = LayoutParserService()
    blocks = svc._normalize_blocks(payload, source=__import__("pathlib").Path("a.pdf"))
    assert len(blocks) == 1
    assert "标题" in blocks[0].content


# ---------- 3. Document 归一化 + metadata 扩展 ----------

def test_to_documents_sets_required_metadata():
    """metadata 必须含 §2.5 要求的块级键。"""
    blocks = [
        LayoutBlock(layout_type="text", content="正文", page_index=0, block_order=0,
                    bbox=[1.0, 2.0, 3.0, 4.0]),
        LayoutBlock(layout_type="table", content="<table/>", page_index=1, block_order=1),
    ]
    svc = LayoutParserService()
    docs = svc.to_documents(blocks, "uploads/a.pdf")

    assert len(docs) == 2
    md = docs[0].metadata
    for key in ("source_type", "page_index", "layout_block_id", "layout_type", "block_order"):
        assert key in md, f"缺 metadata 键: {key}"
    # 既有键保持，便于与纯文本切片共存/去重
    for key in ("_source", "_extension", "_file_name"):
        assert key in md
    assert md["layout_type"] == "text"
    assert md["page_index"] == 0
    assert json.loads(md["block_bbox"]) == [1.0, 2.0, 3.0, 4.0]
    assert docs[1].metadata["layout_type"] == "table"


def test_image_block_metadata_has_image_path():
    blocks = [LayoutBlock(layout_type="image", content="图注", page_index=0,
                          block_order=0, image_path="imgs/x.png")]
    svc = LayoutParserService()
    docs = svc.to_documents(blocks, "uploads/a.pdf")
    assert docs[0].metadata["image_path"] == "imgs/x.png"


def test_blank_content_blocks_skipped():
    blocks = [
        LayoutBlock(layout_type="image", content="", page_index=0, block_order=0),
        LayoutBlock(layout_type="text", content="有内容", page_index=0, block_order=1),
    ]
    svc = LayoutParserService()
    docs = svc.to_documents(blocks, "uploads/a.pdf")
    assert len(docs) == 1
    assert docs[0].page_content == "有内容"


def test_table_block_not_split_even_if_long():
    """表格整体入库不切分（保结构），即使超过阈值。"""
    long_table = "<table>" + "<tr><td>x</td></tr>" * 200 + "</table>"
    blocks = [LayoutBlock(layout_type="table", content=long_table,
                          page_index=0, block_order=0)]
    svc = LayoutParserService()
    docs = svc.to_documents(blocks, "uploads/a.pdf")
    assert len(docs) == 1, "表格不应被切分"
    assert docs[0].page_content == long_table


# ---------- 4. 云端调用重试 ----------

def test_cloud_ocr_retries_on_server_error_then_raises(svc_enabled, tmp_path, monkeypatch):
    """5xx 应重试；重试耗尽后抛出（由 parse_document 捕获降级）。"""
    import httpx

    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF")

    calls = {"n": 0}

    def fake_request(_path):
        calls["n"] += 1
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(svc_enabled, "_do_request", fake_request)
    monkeypatch.setattr("app.services.layout_parser_service._sleep", lambda _s: None)
    with patch.object(config, "ocr_max_retries", 2):
        with pytest.raises(httpx.ConnectError):
            svc_enabled._call_cloud_ocr(pdf)

    assert calls["n"] == 3, f"应尝试 3 次（1+2 重试），实际 {calls['n']}"


def test_cloud_ocr_does_not_retry_on_4xx(svc_enabled, tmp_path, monkeypatch):
    """4xx 是参数/凭证问题，重试无意义。"""
    import httpx

    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF")

    resp = MagicMock()
    resp.status_code = 401
    err = httpx.HTTPStatusError("401", request=MagicMock(), response=resp)

    calls = {"n": 0}

    def fake_request(_path):
        calls["n"] += 1
        raise err

    monkeypatch.setattr(svc_enabled, "_do_request", fake_request)
    monkeypatch.setattr("app.services.layout_parser_service._sleep", lambda _s: None)
    with patch.object(config, "ocr_max_retries", 2):
        with pytest.raises(httpx.HTTPStatusError):
            svc_enabled._call_cloud_ocr(pdf)

    assert calls["n"] == 1, "4xx 不应重试"


# ---------- 5. 端到端（mock 云端）：解析 -> Document ----------

def test_end_to_end_parse_to_documents(svc_enabled, tmp_path):
    """mock 云端响应，走完 parse_document -> to_documents 全链路。"""
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    payload = {"pages": [{
        "page_index": 0,
        "blocks": [
            {"label": "text", "content": "系统概览", "bbox": [0, 0, 100, 20]},
            {"label": "table", "content": "<table><tr><td>指标</td></tr></table>"},
            {"label": "formula", "content": "$$S = \\pi r^2$$"},
            {"label": "image", "content": "", "image_path": "imgs/chart.png"},
        ],
    }]}

    with patch.object(svc_enabled, "_call_cloud_ocr", return_value=payload):
        result = svc_enabled.parse_document(str(pdf))

    assert result.degraded is False
    assert result.source_type == "ocr"
    docs = svc_enabled.to_documents(result.blocks, str(pdf))
    # image 块 content 为空 -> 被跳过；其余 3 类入库
    assert len(docs) == 3
    types = {d.metadata["layout_type"] for d in docs}
    assert types == {"text", "table", "formula"}
    assert all(d.metadata["source_type"] == "ocr" for d in docs)
