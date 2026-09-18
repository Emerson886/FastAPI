"""
==============================================================================
 ORM 模型：操作审计日志（AuditLog）
==============================================================================

对应数据库表：audit_logs

企业项目（尤其涉及等保、ISO27001 认证）通常要求：
    「谁、在什么时间、从哪个 IP、做了什么操作、成功还是失败」全部留痕。

本项目记录的关键操作：
    login / logout          登录登出（安全事件）
    create_conversation     新建会话
    delete_conversation     删除会话（重要：数据销毁动作必须留痕）
    upload_document         上传知识库文档
    delete_document         删除知识库文档
    chat                    发起问答（可选，量大时可关闭）

注意：审计日志只增不改不删，且【不参与业务查询】，
     所以不需要 updated_at，也不建议加软删除。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, PKMixin, utcnow


class AuditLog(Base, PKMixin):
    """操作审计日志表。"""

    __tablename__ = "audit_logs"

    # ------------------------------------------------------------------
    # 操作主体
    # ------------------------------------------------------------------
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True, comment="操作人 ID（登录失败时可能为空）"
    )
    username: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="操作人用户名（冗余：防止用户被删后查不到）"
    )

    # ------------------------------------------------------------------
    # 操作内容
    # ------------------------------------------------------------------
    action: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True, comment="操作类型，如 login/upload_document"
    )
    resource: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="操作对象类型，如 conversation/document"
    )
    resource_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="操作对象 ID"
    )
    detail: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="操作详情（额外上下文）"
    )

    # ------------------------------------------------------------------
    # 请求上下文
    # ------------------------------------------------------------------
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="客户端 IP")
    user_agent: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="浏览器 User-Agent"
    )
    request_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True, comment="请求追踪 ID，可与日志关联"
    )
    method: Mapped[str | None] = mapped_column(String(10), nullable=True, comment="HTTP 方法")
    path: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="请求路径")
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="HTTP 状态码")

    # ------------------------------------------------------------------
    # 结果
    # ------------------------------------------------------------------
    success: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="操作是否成功"
    )
    message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="附加说明或错误信息")

    # 创建时间（审计日志只需创建时间，所以不继承 TimestampMixin）
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
        index=True,
        comment="操作时间",
    )

    __table_args__ = (
        Index("idx_audit_user_action", "user_id", "action"),
        Index("idx_audit_created", "created_at"),
        {"comment": "操作审计日志表（只增不改）"},
    )
