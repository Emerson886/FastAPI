"""
==============================================================================
 ORM 模型：知识库文档（Document）与 文档切片（DocumentChunk）
==============================================================================

对应数据库表：documents、document_chunks

【RAG 的工作原理与本项目的数据流】
    1. 用户上传文件（PDF/Word/TXT/MD）
    2. 后端保存原文件到 storage/uploads，并在 documents 表插入一条记录
    3. 解析文本 → 按 chunk_size 切片 → 调用 Embedding 模型转成向量
    4. 向量写入 Chroma 向量库（保留 chunk_id 作为关联键）
    5. 同时在 document_chunks 表保存「切片原文」
       —— 为什么原文要存 MySQL？
          a) 便于管理端查看/编辑切片内容
          b) 向量库重建时不用重新解析原文件
          c) 检索结果展示引用片段时可直接查库，避免依赖向量库的 metadata
    6. 用户提问 → 问题向量化 → Chroma 相似度检索 → 取回切片 → 拼进 Prompt → 大模型回答

【可见性设计】
    is_public=True  ：全员可见（企业公共知识，如员工手册、规章制度）
    is_public=False ：仅上传者可见（部门/个人私有资料）
    检索时会按 (is_public = True OR owner_id = 当前用户) 过滤，实现知识隔离。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, PKMixin, SoftDeleteMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class DocumentStatus:
    """
    文档处理状态常量（用类属性而非 Enum，避免数据库迁移麻烦）。

    状态流转：pending → parsing → embedding → completed
                              ↘ failed（任一步出错）
    前端轮询/刷新列表即可看到进度，用户体验更好。
    """

    PENDING = "pending"        # 已上传，等待处理
    PARSING = "parsing"        # 正在解析文本
    EMBEDDING = "embedding"    # 正在做向量化
    COMPLETED = "completed"    # 处理完成，可被检索
    FAILED = "failed"          # 处理失败

    ALL = (PENDING, PARSING, EMBEDDING, COMPLETED, FAILED)


class Document(Base, PKMixin, TimestampMixin, SoftDeleteMixin):
    """知识库文档表。"""

    __tablename__ = "documents"

    # ------------------------------------------------------------------
    # 归属与文件信息
    # ------------------------------------------------------------------
    owner_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="上传者用户 ID",
    )
    filename: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="原始文件名（展示用）"
    )
    stored_path: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="服务器上的存储路径"
    )
    file_ext: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="文件后缀，如 .pdf"
    )
    file_size: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False, comment="文件大小（字节）"
    )
    file_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
        comment="文件内容 SHA256（用于秒传/去重，同一文件不重复向量化）",
    )

    # ------------------------------------------------------------------
    # 业务属性
    # ------------------------------------------------------------------
    title: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="文档标题")
    category: Mapped[str | None] = mapped_column(
        String(50), nullable=True, index=True, comment="分类：如 人事制度/技术文档/财务"
    )
    tags: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="标签数组，便于筛选"
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="文档说明")

    is_public: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        index=True,
        comment="是否全员可见（True=企业公共知识，False=仅上传者可见）",
    )

    # ------------------------------------------------------------------
    # 处理状态
    # ------------------------------------------------------------------
    status: Mapped[str] = mapped_column(
        String(20),
        default=DocumentStatus.PENDING,
        nullable=False,
        index=True,
        comment="处理状态：pending/parsing/embedding/completed/failed",
    )
    chunk_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="切片数量"
    )
    char_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="解析出的字符总数"
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="处理失败的错误信息"
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="处理完成时间"
    )

    # ------------------------------------------------------------------
    # 关系
    # ------------------------------------------------------------------
    owner: Mapped["User"] = relationship("User", back_populates="documents")
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        "DocumentChunk",
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("idx_doc_status_public", "status", "is_public"),
        {"comment": "知识库文档表"},
    )

    def to_dict(self, exclude: set[str] | None = None) -> dict[str, Any]:
        data = super().to_dict(exclude=exclude)
        # 处理时间可能是 datetime，统一转字符串，方便 JSON 序列化
        if data.get("processed_at") is not None:
            data["processed_at"] = str(data["processed_at"])
        return data


class DocumentChunk(Base, PKMixin, TimestampMixin):
    """
    文档切片表：保存切片原文与向量库对应关系。

    vector_id 是 Chroma 里的文档 ID（一般用 f"doc{doc_id}_chunk{chunk_index}"），
    删除文档时要靠它去向量库删对应向量，保证「MySQL 与向量库一致」。
    """

    __tablename__ = "document_chunks"

    document_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属文档 ID",
    )
    chunk_index: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="切片序号（从 0 开始）"
    )
    content: Mapped[str] = mapped_column(
        Text, nullable=False, comment="切片原文"
    )
    char_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="切片字符数"
    )
    vector_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True, comment="对应向量库中的 ID"
    )
    chunk_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="切片元数据（页码、章节等）"
    )

    document: Mapped["Document"] = relationship("Document", back_populates="chunks")

    __table_args__ = (
        Index("idx_chunk_doc_index", "document_id", "chunk_index", unique=True),
        {"comment": "文档切片表：与向量库一一对应"},
    )
