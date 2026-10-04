"""文件上传接口模块"""

from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.services.layout_parser_service import (
    SUPPORTED_EXTENSIONS as LAYOUT_EXTENSIONS,
    layout_parser_service,
)
from app.services.vector_index_service import vector_index_service
from loguru import logger

router = APIRouter()

# 文件上传后存储的路径
UPLOAD_DIR = Path("./uploads")
# 纯文本链路支持的文件类型
TEXT_EXTENSIONS = ["txt", "md"]
# 允许上传的全部类型（纯文本 + 多模态版面）
ALLOWED_EXTENSIONS = TEXT_EXTENSIONS + [e.lstrip(".") for e in LAYOUT_EXTENSIONS]
# 单个文件支持最大大小
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """
    上传文件并自动创建向量索引

    Args:
        file: 上传的文件

    Returns:
        JSONResponse: 上传结果
    """
    try:
        # 1. 验证文件
        if not file.filename:
            raise HTTPException(status_code=400, detail="文件名不能为空")

        # 2. 规范化文件名（去除空格，处理 Windows 上传的文件）
        safe_filename = _sanitize_filename(file.filename)

        # 3. 验证文件扩展名
        file_extension = _get_file_extension(safe_filename)
        if file_extension not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件格式，仅支持: {', '.join(ALLOWED_EXTENSIONS)}",
            )

        # 4. 创建上传目录
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

        # 5. 保存文件
        file_path = UPLOAD_DIR / safe_filename

        # 如果文件已存在，先删除旧文件（实现覆盖更新）
        if file_path.exists():
            logger.info(f"文件已存在，将覆盖: {file_path}")
            file_path.unlink()

        # 读取并保存文件内容
        content = await file.read()

        # 验证文件大小
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f"文件大小超过限制（最大 {MAX_FILE_SIZE} 字节）")

        file_path.write_bytes(content)

        logger.info(f"文件上传成功: {file_path}")

        # 5. 建立向量索引：按类型分流
        index_info = {}
        if file_extension in TEXT_EXTENSIONS:
            try:
                logger.info(f"开始为上传文件创建向量索引: {file_path}")
                vector_index_service.index_single_file(str(file_path))
                logger.info(f"向量索引创建成功: {file_path}")
                index_info = {"indexed": True, "mode": "text"}
            except Exception as e:
                logger.error(f"向量索引创建失败: {file_path}, 错误: {e}")
                # 注意：即使索引失败，文件上传仍然成功，只是记录错误日志
                index_info = {"indexed": False, "mode": "text", "error": str(e)}
        else:
            # 多模态：走版面解析链路；解析不可用时**降级但不失败**
            index_info = _index_multimodal(file_path, file_extension)

        # 6. 返回响应
        return JSONResponse(
            status_code=200,
            content={
                "code": 200,
                "message": "success",
                "data": {
                    "filename": safe_filename,
                    "file_path": str(file_path),
                    "size": len(content),
                    **index_info,
                },
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"文件上传失败: {e}")
        raise HTTPException(status_code=500, detail=f"文件上传失败: {e}")


@router.post("/index_directory")
async def index_directory(directory_path: str = None):
    """
    索引指定目录下的所有文件

    Args:
        directory_path: 目录路径（可选，默认使用 uploads 目录）

    Returns:
        JSONResponse: 索引结果
    """
    try:
        logger.info(f"开始索引目录: {directory_path or 'uploads'}")

        # 执行索引
        result = vector_index_service.index_directory(directory_path)

        return JSONResponse(
            status_code=200,
            content={
                "code": 200,
                "message": "success" if result.success else "partial_success",
                "data": result.to_dict(),
            },
        )

    except Exception as e:
        logger.error(f"索引目录失败: {e}")
        raise HTTPException(status_code=500, detail=f"索引目录失败: {e}")


def _index_multimodal(file_path: Path, file_extension: str) -> dict:
    """对 PDF/图片走版面解析入库；不可用时降级返回，不抛异常。

    降级是**预期路径**（缺云端凭证 / 解析失败），
    上传本身仍视为成功，避免影响纯文本主链路。
    """
    parse_result = layout_parser_service.parse_document(str(file_path))

    if not parse_result:
        logger.warning(
            f"版面解析降级（{file_extension}）: {parse_result.reason}"
        )
        return {
            "indexed": False,
            "mode": "multimodal",
            "degraded": True,
            "reason": parse_result.reason,
        }

    try:
        docs = layout_parser_service.to_documents(parse_result.blocks, str(file_path))
        if not docs:
            return {
                "indexed": False,
                "mode": "multimodal",
                "degraded": True,
                "reason": "解析出的版面块为空，无内容入库",
            }

        from app.services.vector_store_manager import vector_store_manager

        vector_store_manager.delete_by_source(file_path.resolve().as_posix())
        vector_store_manager.add_documents(docs)

        logger.info(f"多模态入库成功: {file_path.name}, {len(docs)} 个块")
        return {
            "indexed": True,
            "mode": "multimodal",
            "degraded": False,
            "block_count": len(docs),
            "layout_counts": _count_layout_types(parse_result.blocks),
        }
    except Exception as e:  # noqa: BLE001 - 入库失败不影响上传成功
        logger.error(f"多模态入库失败: {file_path}, 错误: {e}")
        return {
            "indexed": False,
            "mode": "multimodal",
            "degraded": True,
            "error": str(e),
        }


def _count_layout_types(blocks) -> dict:
    """统计各类版面块数量，便于验收"四类块"是否都解析出来。"""
    counts: dict = {}
    for b in blocks:
        counts[b.layout_type] = counts.get(b.layout_type, 0) + 1
    return counts


def _get_file_extension(filename: str) -> str:
    """
    获取文件扩展名

    Args:
        filename: 文件名

    Returns:
        str: 扩展名（小写，不含点）
    """
    parts = filename.rsplit(".", 1)
    if len(parts) == 2:
        return parts[1].lower()
    return ""


def _sanitize_filename(filename: str) -> str:
    """
    规范化文件名，去除空格和特殊字符

    Args:
        filename: 原始文件名

    Returns:
        str: 规范化后的文件名
    """
    # 去除空格
    sanitized = filename.replace(" ", "_")
    # 去除其他可能导致问题的字符
    for char in ['\\', '/', ':', '*', '?', '"', '<', '>', '|']:
        sanitized = sanitized.replace(char, "_")
    return sanitized
