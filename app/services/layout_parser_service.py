"""多模态版面解析服务（P1-4）

目标：让 PDF/图片文档解析成**结构化版面块**并入库，覆盖表格/图表/扫描件等
纯文本链路处理不了的运维文档（见 ENGINEERING-DESIGN §2.5）。

设计口径（严格对齐 §2.5 与本期红线）：
- **统一 `parse_document` 接口**：云端飞桨 OCR（本期）；本地模型留接口不实现；
- **开关式接入**：`config.ocr_enabled=False` 或无 key 时**不报错**，
  返回明确的降级结果，`/upload` 仍走纯文本链路，主链路可用性不受影响；
- **块归一化**：输出 `Document` 列表，metadata 扩块级键
  `source_type/page_index/layout_block_id/layout_type/block_bbox/block_order/image_path`；
- **低侵入入库**：复用现有 `biz` collection 与 text-embedding-v4，
  **不新增第二套 embedding、不引 ChromaDB**（红线）；
- 瞬时失败（5xx/超时）指数退避重试；空结果降级为整页文本。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from langchain_core.documents import Document
from loguru import logger

from app.config import config

# 版面块类型（对齐 §2.5：文本 / 表格 / 公式 / 图像）
LAYOUT_TYPES = ("text", "table", "formula", "image")

# 支持的扩展名
SUPPORTED_EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp")

# 结构化块整体入库不切分；长文本块超过此长度才二次切分
LONG_TEXT_THRESHOLD = 800


@dataclass
class LayoutBlock:
    """一个版面块"""

    layout_type: str
    content: str
    page_index: int
    block_order: int
    bbox: Optional[List[float]] = None
    image_path: Optional[str] = None
    block_id: str = field(default_factory=lambda: f"blk-{uuid.uuid4().hex[:12]}")

    def __post_init__(self) -> None:
        if self.layout_type not in LAYOUT_TYPES:
            # 未知类型保守归为文本，避免丢内容
            logger.debug(f"未知版面块类型 {self.layout_type!r}，按 text 处理")
            self.layout_type = "text"


@dataclass
class ParseResult:
    """解析结果（含降级信息）"""

    blocks: List[LayoutBlock] = field(default_factory=list)
    degraded: bool = False
    reason: str = ""
    source_type: str = "text"

    def __bool__(self) -> bool:
        return bool(self.blocks)


class LayoutParserService:
    """版面解析服务（云端飞桨 OCR，开关式）"""

    def __init__(self) -> None:
        self.enabled = bool(config.ocr_enabled and config.ocr_api_key and config.ocr_api_url)
        if config.ocr_enabled and not self.enabled:
            logger.warning(
                "ocr_enabled=True 但缺少 ocr_api_url / ocr_api_key，"
                "版面解析降级为不可用（纯文本链路不受影响）"
            )
        elif self.enabled:
            logger.info(f"版面解析已启用: {config.ocr_api_url}")
        else:
            logger.info("版面解析未启用（config.ocr_enabled=False），PDF/图片走降级提示")

    # ---------- 对外接口 ----------

    def parse_document(self, file_path: str) -> ParseResult:
        """解析文档为版面块。

        **不抛异常**：任何问题都以降级结果返回，保证主链路可用性。

        Args:
            file_path: 文档路径

        Returns:
            ParseResult: 版面块列表；降级时 blocks 为空并给出 reason
        """
        path = Path(file_path)
        if not path.exists():
            return ParseResult(degraded=True, reason=f"文件不存在: {file_path}")

        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            return ParseResult(
                degraded=True,
                reason=f"版面解析不支持该格式（支持: {', '.join(SUPPORTED_EXTENSIONS)}）",
            )

        if not self.enabled:
            return ParseResult(
                degraded=True,
                reason=(
                    "云端版面解析未启用（需配置 ocr_enabled / ocr_api_url / ocr_api_key），"
                    "PDF/图片暂无法入库"
                ),
            )

        try:
            payload = self._call_cloud_ocr(path)
        except Exception as e:  # noqa: BLE001 - 任何异常都降级，不打断上传
            logger.error(f"版面解析失败，降级处理: {e}")
            return ParseResult(degraded=True, reason=f"版面解析调用失败: {e}")

        blocks = self._normalize_blocks(payload, source=path)
        if not blocks:
            # 空结果降级：整页 Markdown（若有）或提示
            return ParseResult(
                degraded=True,
                reason="版面解析返回空结果",
                source_type="ocr_empty",
            )

        logger.info(f"版面解析完成: {path.name} -> {len(blocks)} 个块")
        return ParseResult(blocks=blocks, source_type="ocr")

    def to_documents(
        self,
        blocks: List[LayoutBlock],
        file_path: str,
    ) -> List[Document]:
        """把版面块归一化为 LangChain `Document`（写入现有向量体系）。

        metadata 扩块级键，并保留既有的 `_source` / `_extension` / `_file_name`
        以便与纯文本切片共存与去重（同一 collection）。
        """
        path = Path(file_path)
        normalized = path.as_posix()
        docs: List[Document] = []

        for block in blocks:
            metadata: Dict[str, Any] = {
                # 既有键（与纯文本链路一致）
                "_source": normalized,
                "_extension": path.suffix,
                "_file_name": path.name,
                # 块级键（§2.5 要求）
                "source_type": "ocr",
                "page_index": block.page_index,
                "layout_block_id": block.block_id,
                "layout_type": block.layout_type,
                "block_order": block.block_order,
            }
            # 可选键只在有值时写入，避免污染元数据
            if block.bbox:
                metadata["block_bbox"] = json.dumps(block.bbox)
            if block.image_path:
                metadata["image_path"] = block.image_path

            text = block.content or ""
            if not text.strip():
                continue

            # 长文本块二次切分；表格/公式/图像**整体入库不切**（保结构）
            if block.layout_type == "text" and len(text) > LONG_TEXT_THRESHOLD:
                docs.extend(self._split_long_text(text, metadata))
            else:
                docs.append(Document(page_content=text, metadata=metadata))

        return docs

    # ---------- 云端调用 ----------

    def _call_cloud_ocr(self, path: Path) -> Dict[str, Any]:
        """调用云端版面解析 API，带瞬时失败重试（指数退避）。

        同步实现：调用方（/upload）本身是同步链路，且这是短请求。
        """
        last_error: Optional[Exception] = None
        attempts = max(1, config.ocr_max_retries + 1)

        for attempt in range(attempts):
            try:
                return self._do_request(path)
            except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.HTTPError) as e:
                last_error = e
                status = getattr(getattr(e, "response", None), "status_code", None)
                # 4xx（除 429）多为参数/凭证问题，重试无意义
                if status is not None and 400 <= status < 500 and status != 429:
                    raise
                if attempt < attempts - 1:
                    delay = 2 ** attempt  # 1s, 2s, 4s...
                    logger.warning(
                        f"版面解析第 {attempt + 1} 次失败，{delay}s 后重试: {e}"
                    )
                    _sleep(delay)

        assert last_error is not None
        raise last_error

    def _do_request(self, path: Path) -> Dict[str, Any]:
        """实际发出 HTTP 请求。"""
        headers = {"Authorization": f"Bearer {config.ocr_api_key}"}
        with path.open("rb") as fh:
            files = {"file": (path.name, fh, "application/octet-stream")}
            with httpx.Client(timeout=config.ocr_request_timeout) as client:
                resp = client.post(config.ocr_api_url, headers=headers, files=files)
                resp.raise_for_status()
                return resp.json()

    # ---------- 归一化 ----------

    def _normalize_blocks(
        self,
        payload: Dict[str, Any],
        source: Path,
    ) -> List[LayoutBlock]:
        """把云端返回归一化为 LayoutBlock 列表。

        兼容两种常见形状：
        - {"pages": [{"page_index": 0, "blocks": [{label, content, bbox, order}]}]}
        - {"result": {"layoutParsingResults": [...]}}（飞桨 PP-StructureV3 风格）

        字段名做**宽容匹配**（label/type/layout_type、content/text/block_content），
        因为云端的字段结构需以实际响应为准（本期无凭证，未能端到端验证）。
        """
        blocks: List[LayoutBlock] = []

        # 形状 1：pages
        pages = payload.get("pages")
        if isinstance(pages, list):
            for pi, page in enumerate(pages):
                if not isinstance(page, dict):
                    continue
                page_idx = page.get("page_index", pi)
                for order, raw in enumerate(page.get("blocks") or []):
                    block = self._block_from_raw(raw, page_idx, order, source)
                    if block:
                        blocks.append(block)
            return blocks

        # 形状 2：result.layoutParsingResults
        result = payload.get("result")
        if isinstance(result, dict):
            results = result.get("layoutParsingResults")
            if isinstance(results, list):
                for pi, page in enumerate(results):
                    if not isinstance(page, dict):
                        continue
                    # 飞桨常把整页文本放在 prunedResult / markdown
                    pruned = page.get("prunedResult") or {}
                    page_idx = pruned.get("page_index", pi)
                    raw_blocks = (
                        pruned.get("layout_blocks")
                        or page.get("layout_blocks")
                        or page.get("blocks")
                        or []
                    )
                    for order, raw in enumerate(raw_blocks):
                        block = self._block_from_raw(raw, page_idx, order, source)
                        if block:
                            blocks.append(block)
                    # 若该页无块但有 markdown，降级为整页文本块
                    if not raw_blocks:
                        md = page.get("markdown") or {}
                        text = md.get("text") if isinstance(md, dict) else md
                        if isinstance(text, str) and text.strip():
                            blocks.append(LayoutBlock(
                                layout_type="text",
                                content=text,
                                page_index=page_idx,
                                block_order=0,
                            ))
            return blocks

        # 形状 3：顶层 blocks
        raw_blocks = payload.get("blocks")
        if isinstance(raw_blocks, list):
            for order, raw in enumerate(raw_blocks):
                block = self._block_from_raw(raw, 0, order, source)
                if block:
                    blocks.append(block)

        return blocks

    @staticmethod
    def _block_from_raw(
        raw: Any,
        page_idx: int,
        order: int,
        source: Path,
    ) -> Optional[LayoutBlock]:
        """单个原始块 -> LayoutBlock（字段名宽容匹配）。"""
        if isinstance(raw, str):
            return LayoutBlock(
                layout_type="text", content=raw,
                page_index=page_idx, block_order=order,
            )
        if not isinstance(raw, dict):
            return None

        label = (
            raw.get("layout_type") or raw.get("label") or raw.get("type") or "text"
        )
        content = (
            raw.get("content") or raw.get("text") or raw.get("block_content") or ""
        )
        bbox = raw.get("bbox") or raw.get("block_bbox") or raw.get("box")
        image_path = raw.get("image_path") or raw.get("img_path")

        # 归一化类型名（飞桨常见：table/图/公式；中文标签也做映射）
        label_map = {
            "text": "text", "paragraph": "text", "title": "text", "文本": "text",
            "table": "table", "表格": "table",
            "formula": "formula", "equation": "formula", "公式": "formula",
            "image": "image", "figure": "image", "图片": "image", "图": "image",
        }
        layout_type = label_map.get(str(label).lower(), "text")

        if isinstance(bbox, (list, tuple)):
            try:
                bbox = [float(v) for v in bbox]
            except (TypeError, ValueError):
                bbox = None
        else:
            bbox = None

        return LayoutBlock(
            layout_type=layout_type,
            content=str(content),
            page_index=int(page_idx) if isinstance(page_idx, int) else 0,
            block_order=int(order),
            bbox=bbox,
            image_path=str(image_path) if image_path else None,
        )

    @staticmethod
    def _split_long_text(text: str, metadata: Dict[str, Any]) -> List[Document]:
        """长文本块二次切分（复用项目既有分割器，避免另起一套）。"""
        try:
            from app.services.document_splitter_service import document_splitter_service

            docs = document_splitter_service.text_splitter.create_documents(
                texts=[text], metadatas=[dict(metadata)]
            )
            return docs
        except Exception as e:  # noqa: BLE001
            logger.warning(f"长文本块二次切分失败，整块入库: {e}")
            return [Document(page_content=text, metadata=dict(metadata))]


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


# 全局单例
layout_parser_service = LayoutParserService()
