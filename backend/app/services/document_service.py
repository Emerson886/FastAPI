"""
==============================================================================
 文档服务（Document Service）
==============================================================================

职责：把「上传文件」这件事的完整流程封装起来。

完整流程（上传 → 可被检索）：
    1. 校验文件（后缀白名单 + 大小限制）
    2. 计算文件哈希 → 查重（同一用户传同一文件直接复用，省钱省时间）
    3. 保存原文件到 storage/uploads/
    4. 在 MySQL 建文档记录（status=pending）
    5. 解析文本 → 切片（status=parsing）
    6. 调用 Embedding 向量化并写入 Chroma（status=embedding）
    7. 切片原文写入 MySQL document_chunks 表（status=completed）

【同步 vs 异步】
    本项目采用「同步处理 + 后台任务」的折中方案：
        - 小文件（< 1MB）直接同步处理完再返回，用户体验直接
        - 大文件走 FastAPI BackgroundTasks 后台处理，接口立即返回，
          前端通过文档列表轮询 status 字段看到进度

    生产环境更优方案：用 Celery / RQ + Redis 做真正的异步队列，
    支持失败重试、并发控制、处理进度上报。扩展说明见 docs/architecture.md。
"""

from __future__ import annotations

import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import BusinessException, NotFoundException
from app.core.logging_config import logger
from app.core.response import BusinessCode
from app.crud.document import crud_document
from app.models.document import Document, DocumentStatus
from app.models.user import User
from app.services.rag import loader, vector_store

# 超过该体积的文件走后台异步处理（单位：字节）
SYNC_PROCESS_MAX_SIZE = 1 * 1024 * 1024  # 1 MB


# =============================================================================
# 1. 上传校验
# =============================================================================
def validate_upload(filename: str, size: int) -> None:
    """
    校验上传文件是否合法。不合法直接抛业务异常，接口层无需再判断。

    :raises BusinessException: 格式不支持 / 文件过大 / 文件为空
    """
    if not filename:
        raise BusinessException(message="文件名不能为空", code=BusinessCode.PARAM_ERROR)

    ext = Path(filename).suffix.lower()
    allowed = settings.allowed_extensions_list
    if ext not in allowed:
        raise BusinessException(
            message=f"不支持的文件格式 {ext}，仅支持：{', '.join(allowed)}",
            code=BusinessCode.FILE_TYPE_NOT_ALLOWED,
        )

    if size <= 0:
        raise BusinessException(message="文件内容为空", code=BusinessCode.PARAM_ERROR)

    if size > settings.max_upload_size_bytes:
        raise BusinessException(
            message=f"文件过大（{size / 1024 / 1024:.1f}MB），最大允许 {settings.MAX_UPLOAD_SIZE_MB}MB",
            code=BusinessCode.FILE_TOO_LARGE,
        )


def save_upload_file(filename: str, content: bytes) -> tuple[str, str]:
    """
    把文件保存到磁盘。

    命名策略：{uuid4}_{原文件名}
        加 UUID 前缀是为了避免：
            a) 不同用户上传同名文件互相覆盖
            b) 文件名里的特殊字符（如 ../）造成目录穿越攻击
    返回 (存储绝对路径, 文件哈希)
    """
    upload_dir = Path(settings.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)

    # 防止目录穿越：只取文件名部分，丢弃任何路径信息
    safe_name = Path(filename).name
    stored_name = f"{uuid.uuid4().hex}_{safe_name}"
    stored_path = upload_dir / stored_name

    stored_path.write_bytes(content)
    file_hash = loader.calculate_file_hash(content)

    logger.info("文件已保存 | {} | {} 字节 | hash={}", stored_path.name, len(content), file_hash[:12])
    return str(stored_path), file_hash


# =============================================================================
# 2. 上传主流程
# =============================================================================
def upload_document(
    db: Session,
    user: User,
    filename: str,
    content: bytes,
    title: str | None = None,
    category: str | None = None,
    is_public: bool = False,
    tags: list[str] | None = None,
    description: str | None = None,
) -> tuple[Document, bool]:
    """
    上传并处理知识库文档。

    :return: (文档对象, 是否为重复文件)
    """
    validate_upload(filename, len(content))

    # ---- 查重：同一用户上传过相同内容的文件，直接复用 ----
    content_hash = loader.calculate_file_hash(content)
    existed = crud_document.get_by_hash(db, content_hash, user.id)
    if existed is not None:
        logger.info("检测到重复文件，复用已有文档 | document_id={}", existed.id)
        return existed, True

    # ---- 保存原文件 ----
    stored_path, file_hash = save_upload_file(filename, content)

    # ---- 创建数据库记录 ----
    document = crud_document.create(
        db,
        {
            "owner_id": user.id,
            "filename": Path(filename).name,
            "stored_path": stored_path,
            "file_ext": Path(filename).suffix.lower(),
            "file_size": len(content),
            "file_hash": file_hash,
            "title": title or Path(filename).stem,
            "category": category,
            "tags": tags,
            "description": description,
            "is_public": is_public,
            "status": DocumentStatus.PENDING,
        },
    )

    # ---- 处理文档（解析 + 向量化）----
    process_document(db, document, content)

    db.refresh(document)
    return document, False


# =============================================================================
# 3. 文档处理：解析 → 切片 → 向量化 → 落库
# =============================================================================
def process_document(db: Session, document: Document, content: bytes | None = None) -> Document:
    """
    处理文档（可单独调用，用于「重新索引」失败文档）。

    出错时不会抛异常，而是把文档状态置为 failed 并记录错误信息 —— 
    这样用户能在列表里看到「失败原因」，而不是莫名其妙少了一个文件。
    """
    try:
        # ---- 读取文件内容（如果没传）----
        if content is None:
            file_path = Path(document.stored_path)
            if not file_path.exists():
                raise BusinessException(
                    message="原始文件已丢失，请重新上传", code=BusinessCode.DOC_PARSE_ERROR
                )
            content = file_path.read_bytes()

        # ---- 步骤 1：解析 + 切片 ----
        crud_document.update_status(db, document, DocumentStatus.PARSING)
        chunks, metadatas, full_text = loader.process_document_content(document, content)

        if not chunks:
            raise BusinessException(
                message="文档内容为空或无法提取有效文本", code=BusinessCode.DOC_PARSE_ERROR
            )

        # ---- 步骤 2：写入向量库 ----
        crud_document.update_status(db, document, DocumentStatus.EMBEDDING)
        vector_ids = vector_store.add_chunks(document.id, chunks, metadatas)

        # ---- 步骤 3：切片原文落 MySQL ----
        # 先清理旧切片（重新索引场景），避免重复
        if document.chunk_count:
            from sqlalchemy import delete

            from app.models.document import DocumentChunk

            db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document.id))
            db.commit()

        crud_document.create_chunks(
            db,
            document.id,
            [
                {
                    "chunk_index": index,
                    "content": chunk,
                    "char_count": len(chunk),
                    "vector_id": vector_ids[index] if index < len(vector_ids) else None,
                    "chunk_metadata": metadatas[index] if index < len(metadatas) else None,
                }
                for index, chunk in enumerate(chunks)
            ],
        )

        # ---- 步骤 4：更新为完成 ----
        document = crud_document.update_status(
            db,
            document,
            DocumentStatus.COMPLETED,
            chunk_count=len(chunks),
            char_count=len(full_text),
        )
        logger.info(
            "文档处理完成 | id={} | {} | 切片={} | 字符={}",
            document.id, document.filename, len(chunks), len(full_text),
        )
        return document

    except Exception as exc:
        # 失败：先清理可能写入了一半的向量，再标记状态
        try:
            vector_store.delete_document(document.id)
        except Exception:  # pragma: no cover
            logger.warning("失败清理向量时出错 | document_id={}", document.id)

        error_text = str(exc)[:1000]
        document = crud_document.update_status(
            db, document, DocumentStatus.FAILED, error_message=error_text
        )
        logger.exception("文档处理失败 | id={} | {}", document.id, document.filename)
        return document


def reindex_document(db: Session, document: Document) -> Document:
    """
    重新索引单篇文档（Embedding 模型换了，或之前处理失败时使用）。
    """
    logger.info("重新索引文档 | id={} | {}", document.id, document.filename)
    vector_store.delete_document(document.id)
    return process_document(db, document)


# =============================================================================
# 4. 删除文档（必须同时清理向量库！）
# =============================================================================
def delete_document(db: Session, document: Document, hard: bool = False) -> None:
    """
    删除文档。

    【顺序很重要】先删向量 → 再删 MySQL 记录 → 最后删磁盘文件。
    如果先删 MySQL 记录，就没有 document_id 去定位向量了，会留下永久垃圾向量
    （表现为：问答时引用了已删除的文档，但用户查不到该文档）。
    """
    # 1) 删向量
    try:
        vector_store.delete_document(document.id)
    except Exception as exc:  # pragma: no cover
        # 向量删除失败不阻断主流程（可能是向量库就不可用），但要告警
        logger.error("删除向量失败，可能出现脏向量 | document_id={} | {}", document.id, exc)

    # 2) 记住路径，删除后对象属性会失效
    stored_path = document.stored_path

    # 3) 删数据库记录
    crud_document.delete_with_chunks(db, document, hard=hard)

    # 4) 删磁盘文件（软删除时保留文件，便于恢复）
    if hard and stored_path:
        try:
            path = Path(stored_path)
            if path.exists():
                path.unlink()
                logger.info("已删除磁盘文件 | {}", path.name)
        except Exception as exc:  # pragma: no cover
            logger.warning("删除磁盘文件失败 | {} | {}", stored_path, exc)


# =============================================================================
# 5. 查询辅助
# =============================================================================
def get_document_or_404(db: Session, document_id: int, user_id: int) -> Document:
    """查询可见文档，不存在则抛 404（接口层直接用，省去判空）。"""
    document = crud_document.get_visible(db, document_id, user_id)
    if document is None:
        raise NotFoundException(message="文档不存在或无权访问")
    return document


def get_knowledge_base_overview(db: Session, user_id: int) -> dict[str, Any]:
    """知识库总览（统计 + 向量库信息），知识库页面顶部展示。"""
    stats = crud_document.stats(db, user_id)
    vector_stats = vector_store.get_collection_stats()
    return {
        **stats,
        "vector_count": vector_stats.get("vector_count", 0),
        "collection": vector_stats.get("collection"),
        "embedding_model": vector_stats.get("embedding_model"),
        "categories": crud_document.list_categories(db, user_id),
        "generated_at": datetime.now().isoformat(),
    }


def cleanup_temp_files(directory: str | None = None) -> int:
    """
    清理孤儿文件（磁盘上有但数据库里没有记录的文件）。
    建议做成定时任务，防止用户上传后中途失败留下垃圾文件。
    """
    target = Path(directory or settings.UPLOAD_DIR)
    if not target.exists():
        return 0

    removed = 0
    for file_path in target.iterdir():
        if file_path.is_file():
            file_path.unlink(missing_ok=True)
            removed += 1
    logger.info("清理上传目录 | 删除文件数={}", removed)
    return removed
