"""
==============================================================================
 ORM 模型：会话（Conversation）
==============================================================================

对应数据库表：conversations

业务含义：用户在问答助手里开的「一个聊天窗口 / 一段对话」。
例如用户在左侧列表看到的历史记录，每一条就是一个 Conversation。

【用户隔离的关键】
    user_id 外键是数据隔离的根本：所有会话查询都必须带
    `WHERE user_id = 当前登录用户ID`，本项目通过：
        - crud.conversation.get_by_id_and_user()  强制带 user_id
        - api/deps.py 的 get_owned_conversation() 依赖统一校验归属
    双重保障，杜绝「改了 URL 里的 id 就能看别人对话」的越权漏洞。

【软删除】
    用户点「删除会话」只置 is_deleted=True，消息记录仍保留在库里，
    便于审计与误删恢复。列表查询默认过滤已删除。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, PKMixin, SoftDeleteMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.message import Message
    from app.models.user import User


class Conversation(Base, PKMixin, TimestampMixin, SoftDeleteMixin):
    """会话（对话）表。"""

    __tablename__ = "conversations"

    # ------------------------------------------------------------------
    # 归属关系
    # ------------------------------------------------------------------
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),  # 用户物理删除时级联删除会话
        nullable=False,
        index=True,
        comment="所属用户 ID（数据隔离依据）",
    )

    # ------------------------------------------------------------------
    # 会话信息
    # ------------------------------------------------------------------
    title: Mapped[str] = mapped_column(
        String(200),
        default="新对话",
        nullable=False,
        comment="会话标题（默认取用户第一句话的前 N 个字）",
    )
    summary: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="会话摘要（可由大模型生成，便于历史检索）"
    )

    # ------------------------------------------------------------------
    # 统计字段（冗余设计，避免每次列表查询都 COUNT 消息表）
    # ------------------------------------------------------------------
    message_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="消息条数（含用户与助手）"
    )
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True, comment="最后一条消息时间（用于排序）"
    )

    # 会话级配置：不同会话可以用不同模型/是否启用知识库检索
    model_name: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="该会话使用的模型名（空则用系统默认）"
    )
    use_rag: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="是否启用知识库检索（RAG）"
    )

    is_pinned: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="是否置顶"
    )

    # ------------------------------------------------------------------
    # 关系
    # ------------------------------------------------------------------
    user: Mapped["User"] = relationship("User", back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Message.id",  # 按时间正序返回，前端直接渲染
    )

    # ------------------------------------------------------------------
    # 复合索引：列表查询固定是「某用户 + 未删除 + 按最后消息时间倒序」
    # 建立这个组合索引能把列表接口从全表扫描优化成索引扫描
    # ------------------------------------------------------------------
    __table_args__ = (
        Index("idx_conv_user_deleted_lastmsg", "user_id", "is_deleted", "last_message_at"),
        {"comment": "会话表：每个用户的问答会话"},
    )
