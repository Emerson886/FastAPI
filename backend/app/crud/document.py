"""
==============================================================================
 CRUD：知识库文档（Document / DocumentChunk）
==============================================================================

方法清单：
    - list_visible           当前用户可见的文档（公共 + 自己上传的）
    - get_visible            单篇文档（带可见性校验）
    - get_by_hash            按文件哈希查重（秒传）
    - update_status          更新处理状态（解析 → 向量化 → 完成）
    - create_chunks          批量写入切片
    - list_chunks            查询某文档的切片（管理端预览）
    - delete_with_chunks     删除文档并清理切片
    - list_categories        文档分类聚合（前端筛选下拉框）

【可见性规则（与 RAG 检索保持一致）】
    is_public = True             → 全员可见
    is_public = False AND owner_id = 当前用户 → 自己可见
    其他情况                     → 不可见
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.crud.base import CRUDBase
from app.models.document import Document, DocumentChunk, DocumentStatus
from app.schemas.document import DocumentCreate, DocumentUpdate


class CRUDDocument(CRUDBase[Document, DocumentCreate, DocumentUpdate]):
    """文档数据访问层。"""

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def _visibility_condition(self, user_id: int) -> Any:
        """
        构造可见性 SQL 条件。

        【关键】这个方法同时被「文档列表接口」和「RAG 检索」使用，
        保证「列表里看得到的文档」和「问答能检索到的文档」完全一致，
        避免出现「问答引用了用户看不到的文档」这种权限漏洞。
        """
        return or_(Document.is_public.is_(True), Document.owner_id == user_id)

    def list_visible(
        self,
        db: Session,
        user_id: int,
        keyword: str | None = None,
        category: str | None = None,
        status: str | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[Document], int]:
        """当前用户可见的文档列表（分页）。"""
        conditions = [Document.is_deleted.is_(False), self._visibility_condition(user_id)]

        if keyword:
            pattern = f"%{keyword.strip()}%"
            conditions.append(
                or_(Document.filename.like(pattern), Document.title.like(pattern))
            )
        if category:
            conditions.append(Document.category == category)
        if status:
            conditions.append(Document.status == status)

        stmt = (
            select(Document)
            .where(*conditions)
            .order_by(Document.id.desc())
            .offset(skip)
            .limit(limit)
        )
        count_stmt = select(func.count()).select_from(Document).where(*conditions)
        total = int(db.execute(count_stmt).scalar_one())
        return list(db.execute(stmt).scalars().all()), total

    def get_visible(self, db: Session, document_id: int, user_id: int) -> Document | None:
        """按 ID 查询可见文档，不可见时返回 None（接口层转成 403/404）。"""
        stmt = select(Document).where(
            Document.id == document_id,
            Document.is_deleted.is_(False),
            self._visibility_condition(user_id),
        )
        return db.execute(stmt).scalar_one_or_none()

    def get_by_hash(self, db: Session, file_hash: str, owner_id: int) -> Document | None:
        """
        按文件内容哈希查重。
        同一用户重复上传同一文件时直接复用，不需要重新向量化（省时省钱）。
        """
        stmt = select(Document).where(
            Document.file_hash == file_hash,
            Document.owner_id == owner_id,
            Document.is_deleted.is_(False),
        )
        return db.execute(stmt).scalars().first()

    def list_completed_for_rag(self, db: Session, user_id: int) -> list[Document]:
        """
        取出「已处理完成 + 当前用户可见」的文档，
        用于把知识库过滤条件传给向量检索。
        """
        stmt = select(Document).where(
            Document.is_deleted.is_(False),
            Document.status == DocumentStatus.COMPLETED,
            self._visibility_condition(user_id),
        )
        return list(db.execute(stmt).scalars().all())

    def list_categories(self, db: Session, user_id: int) -> list[dict[str, Any]]:
        """按分类聚合统计（前端筛选下拉框数据源）。"""
        stmt = (
            select(Document.category, func.count(Document.id))
            .where(
                Document.is_deleted.is_(False),
                self._visibility_condition(user_id),
                Document.category.is_not(None),
            )
            .group_by(Document.category)
        )
        return [{"category": row[0], "count": row[1]} for row in db.execute(stmt).all()]

    def stats(self, db: Session, user_id: int) -> dict[str, int]:
        """知识库统计（首页卡片展示）。"""
        base = [Document.is_deleted.is_(False), self._visibility_condition(user_id)]
        total = int(
            db.execute(select(func.count()).select_from(Document).where(*base)).scalar_one()
        )
        completed = int(
            db.execute(
                select(func.count())
                .select_from(Document)
                .where(*base, Document.status == DocumentStatus.COMPLETED)
            ).scalar_one()
        )
        processing = int(
            db.execute(
                select(func.count())
                .select_from(Document)
                .where(
                    *base,
                    Document.status.in_(
                        [DocumentStatus.PENDING, DocumentStatus.PARSING, DocumentStatus.EMBEDDING]
                    ),
                )
            ).scalar_one()
        )
        chunks = int(
            db.execute(
                select(func.coalesce(func.sum(Document.chunk_count), 0))
                .where(*base)
            ).scalar_one()
            or 0
        )
        return {
            "total": total,
            "completed": completed,
            "processing": processing,
            "failed": max(total - completed - processing, 0),
            "chunks": chunks,
        }

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def update_status(
        self,
        db: Session,
        document: Document,
        status: str,
        error_message: str | None = None,
        chunk_count: int | None = None,
        char_count: int | None = None,
    ) -> Document:
        """更新文档处理状态。上传后的异步处理流程每一步都会调用它。"""
        document.status = status
        if error_message is not None:
            document.error_message = error_message
        if chunk_count is not None:
            document.chunk_count = chunk_count
        if char_count is not None:
            document.char_count = char_count
        if status in (DocumentStatus.COMPLETED, DocumentStatus.FAILED):
            document.processed_at = datetime.now()

        db.add(document)
        db.commit()
        db.refresh(document)
        return document

    def create_chunks(
        self,
        db: Session,
        document_id: int,
        chunks: list[dict[str, Any]],
    ) -> list[DocumentChunk]:
        """
        批量写入切片。

        :param chunks: [{"chunk_index":0,"content":"...","char_count":500,"vector_id":"..."}]
        使用 bulk insert 提升性能（一篇 100 页 PDF 可能有几百个切片）。
        """
        if not chunks:
            return []
        objs = [DocumentChunk(document_id=document_id, **chunk) for chunk in chunks]
        db.add_all(objs)
        db.commit()
        for obj in objs:
            db.refresh(obj)
        return objs

    def list_chunks(
        self, db: Session, document_id: int, skip: int = 0, limit: int = 50
    ) -> tuple[list[DocumentChunk], int]:
        """查询文档的切片列表（管理端预览切片效果）。"""
        stmt = (
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index.asc())
            .offset(skip)
            .limit(limit)
        )
        total = int(
            db.execute(
                select(func.count())
                .select_from(DocumentChunk)
                .where(DocumentChunk.document_id == document_id)
            ).scalar_one()
        )
        return list(db.execute(stmt).scalars().all()), total

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------
    def delete_with_chunks(self, db: Session, document: Document, hard: bool = False) -> None:
        """
        删除文档。

        :param hard: True=物理删除（同时删掉切片）；False=软删除（切片保留，便于恢复）

        注意：调用方（service 层）必须在删除前先调用
             vector_store.delete_document(...) 清理向量库，
             否则会出现「文档已删但检索还能召回」的脏数据。
        """
        if hard:
            db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document.id))
            db.delete(document)
            db.commit()
        else:
            self.remove(db, document.id, hard=False)


crud_document = CRUDDocument(Document)
