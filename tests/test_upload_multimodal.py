"""文件上传接口的多模态分流测试（P1-4）

验收口径①：无 OCR 配置时 PDF/图片上传**降级但成功**，纯文本链路不受影响。
另覆盖：解析成功时四类块入库、不支持类型被拒。
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault("app.services.vector_store_manager", MagicMock())

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services.layout_parser_service import LayoutBlock, ParseResult  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


# ---------- 1. 降级：不影响主链路 ----------

def test_pdf_upload_degrades_but_succeeds_without_ocr():
    """未配置 OCR 时，PDF 上传仍返回 200，只是不建索引并给出原因。"""
    with patch("app.api.file.layout_parser_service") as svc:
        svc.parse_document.return_value = ParseResult(
            degraded=True, reason="云端版面解析未启用"
        )
        resp = client.post(
            "/api/upload",
            files={"file": ("r.pdf", b"%PDF-1.4", "application/pdf")},
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["degraded"] is True
    assert data["mode"] == "multimodal"
    assert data["indexed"] is False
    assert data["reason"]


def test_text_upload_still_uses_text_pipeline():
    """纯文本链路不受多模态改动影响。"""
    with patch("app.api.file.vector_index_service") as vis:
        resp = client.post(
            "/api/upload",
            files={"file": ("n.txt", b"hello", "text/plain")},
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["mode"] == "text"
    assert data["indexed"] is True
    assert vis.index_single_file.called


def test_markdown_upload_uses_text_pipeline():
    with patch("app.api.file.vector_index_service"):
        resp = client.post(
            "/api/upload",
            files={"file": ("d.md", b"# t", "text/markdown")},
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["mode"] == "text"


def test_unsupported_extension_rejected():
    resp = client.post(
        "/api/upload",
        files={"file": ("a.docx", b"x", "application/msword")},
    )
    assert resp.status_code == 400


# ---------- 2. 启用时四类块入库 ----------

def test_multimodal_success_reports_layout_counts():
    blocks = [
        LayoutBlock(layout_type="text", content="正文", page_index=0, block_order=0),
        LayoutBlock(layout_type="table", content="<table/>", page_index=0, block_order=1),
        LayoutBlock(layout_type="formula", content="$$x$$", page_index=0, block_order=2),
        LayoutBlock(layout_type="image", content="", page_index=0, block_order=3),
    ]
    from langchain_core.documents import Document

    with patch("app.api.file.layout_parser_service") as svc, \
         patch("app.services.vector_store_manager.vector_store_manager") as vsm:
        svc.parse_document.return_value = ParseResult(blocks=blocks, source_type="ocr")
        # image 块无文本内容 -> 不入库
        svc.to_documents.return_value = [
            Document(page_content=b.content, metadata={"layout_type": b.layout_type})
            for b in blocks if b.content
        ]
        resp = client.post(
            "/api/upload",
            files={"file": ("c.png", b"\x89PNG", "image/png")},
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["indexed"] is True
    assert data["degraded"] is False
    assert data["block_count"] == 3, "image 无内容，不入库"
    # layout_counts 统计解析出的块（含 image），与入库数不同
    assert data["layout_counts"] == {"text": 1, "table": 1, "formula": 1, "image": 1}


def test_multimodal_parse_empty_returns_degraded():
    with patch("app.api.file.layout_parser_service") as svc:
        svc.parse_document.return_value = ParseResult(
            degraded=True, reason="版面解析返回空结果"
        )
        resp = client.post(
            "/api/upload",
            files={"file": ("e.pdf", b"%PDF", "application/pdf")},
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["degraded"] is True


def test_multimodal_indexing_error_does_not_fail_upload():
    """入库抛异常也不得让上传失败（文件已保存，索引是增强）。"""
    from langchain_core.documents import Document

    with patch("app.api.file.layout_parser_service") as svc:
        svc.parse_document.return_value = ParseResult(
            blocks=[LayoutBlock(layout_type="text", content="x", page_index=0, block_order=0)],
            source_type="ocr",
        )
        svc.to_documents.return_value = [Document(page_content="x", metadata={})]
        with patch(
            "app.services.vector_store_manager.vector_store_manager.add_documents",
            side_effect=RuntimeError("milvus down"),
        ):
            resp = client.post(
                "/api/upload",
                files={"file": ("f.pdf", b"%PDF", "application/pdf")},
            )

    assert resp.status_code == 200
    assert resp.json()["data"]["indexed"] is False
