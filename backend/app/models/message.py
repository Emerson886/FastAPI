"""
==============================================================================
 ORM 模型：消息（Message）—— 历史记录的核心
==============================================================================

对应数据库表：messages

每条消息就是对话里的一句话，role 区分是谁说的：
    user      —— 用户提问
    assistant —— AI 回答
    system    —— 系统提示词（一般不落库，落库用于记录生效的 Prompt）

【为什么要把来源引用（citations）单独存 JSON？】
    RAG 回答需要展示「答案参考了哪几个知识片段」。这些片段结构不固定
    （可能 3 条也可能 5 条），用 JSON 字段存储最合适：
        citations = [{"doc_id":1,"filename":"制度.pdf","chunk":"...","score":0.87}]
    MySQL 5.7+ 原生支持 JSON 类型，SQLAlchemy 用 JSON 类型即可自动序列化。

【审计字段】
    model_name / prompt_tokens / completion_tokens / latency_ms / status
    这些是大模型应用的「可观测性」数据：能算出成本、排查慢请求、统计失败率。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, PKMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.conversation import Conversation


class Message(Base, PKMixin, TimestampMixin):
    """消息表：保存完整的问答历史。"""

    __tablename__ = "messages"

    # ------------------------------------------------------------------
    # 归属
    # ------------------------------------------------------------------
    conversation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属会话 ID",
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属用户 ID（冗余字段：便于按用户直接查消息、也作为隔离兜底）",
    )

    # ------------------------------------------------------------------
    # 消息内容
    # ------------------------------------------------------------------
    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="角色：user=用户 / assistant=AI / system=系统",
    )
    content: Mapped[str] = mapped_column(
        Text, nullable=False, comment="消息正文"
    )
    # 前端「重新生成」功能用到：记录被重新生成的那条回答
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
        comment="父消息 ID（用于回答重新生成时的分支追溯）",
    )

    # ------------------------------------------------------------------
    # RAG 引用来源
    # ------------------------------------------------------------------
    citations: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="引用的知识片段列表：[{doc_id,filename,chunk,score}]",
    )

    # ------------------------------------------------------------------
    # 审计 / 可观测性字段
    # ------------------------------------------------------------------
    model_name: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="生成该回答使用的模型"
    )
    prompt_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="输入 token 数（算成本用）"
    )
    completion_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="输出 token 数（算成本用）"
    )
    latency_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="本次回答耗时（毫秒）"
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="success",
        nullable=False,
        comment="状态：success=成功 / failed=失败 / streaming=生成中",
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="失败原因（便于排查大模型报错）"
    )

    # ------------------------------------------------------------------
    # 关系
    # ------------------------------------------------------------------
    conversation: Mapped["Conversation"] = relationship(
        "Conversation", back_populates="messages"
    )

    # ------------------------------------------------------------------
    # 复合索引：拉取某会话的历史消息（最频繁的查询）
    # ------------------------------------------------------------------
    __table_args__ = (
        Index("idx_msg_conv_created", "conversation_id", "created_at"),
        Index("idx_msg_user_created", "user_id", "created_at"),
        {"comment": "消息表：完整问答历史记录"},
    )

    @property
    def is_user(self) -> bool:
        """是否为用户发送的消息。"""
        return self.role == "user"
