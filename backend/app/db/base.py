"""
Fully read!!!!
==============================================================================
 SQLAlchemy 声明式基类与公共 Mixin
==============================================================================

本模块定义：
    Base        —— 所有 ORM 模型的基类
    TimestampMixin —— 统一的创建时间/更新时间字段（所有表都需要）
    SoftDeleteMixin —— 软删除（企业项目常要求「删除可恢复」）

【命名规范 convention 说明】
    通过 metadata 指定索引/约束的命名规则，好处是：
    - 数据库里索引名可读（idx_users_username 而不是 ix_8321）
    - Alembic 自动生成迁移脚本时不会因为命名不稳定而反复 diff

【时区说明】
    统一使用 datetime.now(timezone.utc) 存 UTC 时间，前端负责转成本地时区展示。
    不要用 datetime.now()（本地时间），服务器换时区会导致数据错乱。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import BigInteger, DateTime, Integer, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# =============================================================================
# 1. 元数据：统一索引/约束命名规范
# =============================================================================
NAMING_CONVENTION: dict[str, str] = {
    "ix": "idx_%(column_0_label)s",          # 普通索引
    "uq": "uq_%(table_name)s_%(column_0_name)s",  # 唯一约束
    "ck": "ck_%(table_name)s_%(constraint_name)s",  # 检查约束
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",  # 外键
    "pk": "pk_%(table_name)s",               # 主键
}


def utcnow() -> datetime:
    """
    获取当前 UTC 时间（带时区信息）。

    所有模型的时间字段默认值都用它，保证时间统一、可比较。
    """
    return datetime.now(timezone.utc)


# =============================================================================
# 2. 声明式基类，DeclarativeBase 是 SQLAlchemy 2.0 提供的一个特殊基类，专门用于声明式映射
# =============================================================================

class Base(DeclarativeBase):
    """
    所有 ORM 模型的基类。

    SQLAlchemy 2.0 采用 Mapped[...] + mapped_column() 的类型注解风格，
    好处是 IDE 能自动补全、类型检查器能发现字段拼写错误。
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # naming_convention 是命名规则，用于给约束、索引自动生成名字

    # 所有模型都提供 to_dict()，方便日志打印与调试
    def to_dict(self, exclude: set[str] | None = None) -> dict[str, Any]:
        """把 ORM 对象转成字典（注意：敏感字段应手动 exclude）。"""
        exclude = exclude or set()
        result: dict[str, Any] = {}
        for column in self.__table__.columns:  # type: ignore[attr-defined]
            if column.name in exclude:
                continue
            value = getattr(self, column.name)
            result[column.name] = value.isoformat() if isinstance(value, datetime) else value
            # 调用 value.isoformat() 转成 ISO 8601 格式字符串、
            # datetime 对象不能直接被 JSON 序列化，转成字符串后就可以
        return result

    def __repr__(self) -> str:  # pragma: no cover
        pk = getattr(self, "id", None)
        return f"<{self.__class__.__name__} id={pk}>"
        # self.__class__.__name__：获取当前对象的类名，例如 "User"
    # 这样：
    #
    # 在列表里打印多个对象时，会显示 [<User id=1>, <User id=2>]，一目了然。
    #
    # 日志中记录对象时，也能快速识别。
    #
    # 调试时不用再去查内存地址。


# =============================================================================
# 3. 时间戳 Mixin （通用字段，需继承）
# =============================================================================
# 如果某个模型继承了Mixin类，则添加Mixin父类字段到对应模型映射类（如User）当中
class TimestampMixin:
    """
    创建时间 + 更新时间，让每张表都具备审计能力。

    created_at：插入时自动填充（默认值），之后不再变化
    updated_at：插入时填充，每次 UPDATE 自动刷新（onupdate）
    """

    # created_at: Mapped[datetime]：
    # SQLAlchemy 2.0 的类型注解风格，声明该属性映射为 datetime 类型。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),

        # datetime.utcnow() 返回的是不带时区信息的 naive datetime，
        # 而 DateTime(timezone=True) 期望带时区的 datetime
        default=utcnow, # 返回带 UTC 时区的 aware datetime，与 DateTime(timezone=True) 匹配。
        nullable=False,
        comment="创建时间（UTC）",
        index=True,  # 常按时间倒序查询，加索引
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
        comment="最后更新时间（UTC）",
    )
    # 1. default 与 server_default 的区别
    # default：Python 端生成，插入时由 SQLAlchemy 调用。
    #
    # server_default：数据库端默认值，如 server_default=func.now()。
    #
    # 用 default 的好处是跨数据库一致，且能拿到 Python 的 UTC 时间。
    #
    # 但如果绕过 ORM 直接写 SQL，default 不会生效。

    # 2.onupdate 只在 ORM 更新时生效
    # 用 session.query(User).update(...) 或直接 SQL 更新时，onupdate 可能不触发。
    #
    # 如果需要数据库层面强制，
    # 需用触发器或 server_onupdate（但 SQLAlchemy 对 server_onupdate 支持有限）。


# =============================================================================
# 4. 自增主键 Mixin（统一用 BIGINT，避免数据量增长后 INT 溢出）
# =============================================================================
class PKMixin:
    """
    统一主键定义，所有业务表都继承它。

    【跨数据库兼容说明】
        这里用 with_variant 做了一个「方言变体」：
          - MySQL / PostgreSQL：BIGINT（大整数，避免数据量增长后溢出）
          - SQLite：INTEGER
        为什么要这么做？
            SQLite 只有「INTEGER PRIMARY KEY」才支持自增，
            声明成 BIGINT PRIMARY KEY 时 SQLite 不会自动生成 ID，
            插入数据会报 NOT NULL constraint failed: xxx.id。
        加上这个变体后，就能用 SQLite 内存库跑单元测试与离线验证脚本，
        而生产环境仍然使用 BIGINT。这是 SQLAlchemy 官方推荐的跨方言写法。
    """

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
        comment="主键 ID",
    )


# =============================================================================
# 5. 软删除 Mixin
# =============================================================================
class SoftDeleteMixin:
    """
    软删除：不真正 DELETE 数据，只把 is_deleted 置为 True。

    企业项目为什么这样做？
        - 数据可恢复（误删后能找回）
        - 保留审计痕迹（谁删了什么）
    代价：所有查询都要加 .filter(Model.is_deleted.is_(False))，
         本项目已在 CRUDBase 中统一处理，业务代码无需关心。
    """

    is_deleted: Mapped[bool] = mapped_column(
        default=False,
        nullable=False,
        comment="是否已删除（软删除标记）",
        index=True,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="删除时间",
    )
