"""
Fully read!!!
==============================================================================
 ORM 模型：用户（User）
==============================================================================

对应数据库表：users

安全要点：
    1. hashed_password 存 bcrypt 哈希，绝不存明文
    2. is_active 用于「禁用账号」（员工离职后停用，而非删除数据）
    3. is_superuser 区分管理员与普通员工
    4. to_dict() 里必须排除 hashed_password —— 这是最容易出的安全事故
       （接口不小心直接返回 ORM 对象，密码哈希就泄露了）
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, PKMixin, TimestampMixin, utcnow

if TYPE_CHECKING:  # 仅用于类型检查，避免运行时循环导入
    from app.models.conversation import Conversation
    from app.models.document import Document

# DeclarativeBase 内部通过元类或类创建钩子，在定义 User 时自动做很多事情：
#
# 读取 __tablename__
#
# 扫描 Mapped[...] 类型注解
#
# 处理 mapped_column()
#
# 生成 Table 对象
#
# 创建 Mapper 对象
#
# 把类注册到 registry
#
# 把表放进 metadata
class User(Base, PKMixin, TimestampMixin):
    """用户表。"""

    __tablename__ = "users"

    # ------------------------------------------------------------------
    # 账号信息
    # ------------------------------------------------------------------
    # Mapped标注为orm需要映射的属性，mapped_column设置参数及其细节
    username: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        nullable=False, # 不许为空
        index=True, # 为该列建索引
        comment="登录用户名（唯一）",
    )
    email: Mapped[str | None] = mapped_column(
        String(120),
        unique=True,
        nullable=True,
        index=True,
        comment="邮箱（唯一，可用于找回密码）",
    )
    hashed_password: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="bcrypt 密码哈希（严禁存明文）",
    )
    nickname: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="显示昵称"
    )
    avatar: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="头像 URL"
    )

    # ------------------------------------------------------------------
    # 状态与权限
    # ------------------------------------------------------------------
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="是否启用（False 表示被禁用）"
    )
    is_superuser: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="是否超级管理员"
    )

    # ------------------------------------------------------------------
    # 登录审计
    # ------------------------------------------------------------------
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="最后登录时间"
    )
    last_login_ip: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="最后登录 IP"
    )

    # ------------------------------------------------------------------
    # 关系（relationship）
    # ------------------------------------------------------------------
    # 一个用户拥有多个会话；删除用户时级联删除其会话（DB 层也配了 ondelete）
    conversations: Mapped[list["Conversation"]] = relationship(
        "Conversation", # 指定关系另一端的模型类名
        back_populates="user",
        # 建立双向关系：Conversation 类里也有一个 user 属性，通过 back_populates 与这里的 conversations 互相引用。

        cascade="all, delete-orphan",
        # 级联行为，控制当 User 对象发生操作时，Conversation 对象如何跟随。
        # delete-orphan：孤儿删除。
        # 当一个 Conversation 从 user.conversations 中移除，且不再属于任何 User 时，它会被自动删除。

        passive_deletes=True,
        # 删除 User 时，不要主动去加载并逐个删除关联的 Conversation，而是交给数据库处理。
        # 避免 SQLAlchemy 先把所有 Conversation 加载到内存再逐条删除，性能更好。
        # 前提：数据库外键必须设置了 ON DELETE CASCADE，
        # 否则删除 User 时，数据库会因为外键约束报错，或者留下孤儿记录。

        lazy="selectin",  # 避免 N+1 查询
        # 控制加载策略：当查询 User 时，如何加载它的 conversations。
        # 好处：避免 N+1 查询问题。
        #
        # 如果默认用 lazy="select"（懒加载），查 10 个用户再访问各自的 conversations，会发 1 + 10 = 11 条 SQL。
        #
        # 用 selectin，查 10 个用户时，SQLAlchemy 会自动发 2 条 SQL：一条查用户，一条用 IN 查所有会话。
        #
        # 适用场景：经常需要同时访问父对象和子集合时，比如 API 返回用户及其会话列表。
    )
    # 一个用户上传多个知识库文档
    documents: Mapped[list["Document"]] = relationship(
        "Document",
        back_populates="owner",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------
    @property
    def display_name(self) -> str:
        """展示名：优先昵称，其次用户名。"""
        return self.nickname or self.username

    def to_dict(self, exclude: set[str] | None = None) -> dict[str, Any]:
        """
        转字典时强制排除密码哈希，防止误泄露。
        即使调用方忘了传 exclude，也绝对拿不到 hashed_password。
        """
        exclude = set(exclude or set()) | {"hashed_password"}
        return super().to_dict(exclude=exclude)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} username={self.username}>"
