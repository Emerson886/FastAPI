"""
==============================================================================
 数据库会话管理（SQLAlchemy 2.0 + MySQL/PyMySQL）
==============================================================================

本模块职责：
    1. 创建 Engine（连接引擎）—— 全进程唯一，内部维护连接池
    2. 创建 SessionLocal（会话工厂）—— 每次请求创建一个独立 Session
    3. 提供 get_db() 依赖 —— FastAPI 通过 Depends(get_db) 注入会话，
       请求结束自动 close，出现异常自动 rollback，杜绝连接泄露
    4. 提供 transaction() 上下文管理器 —— 脚本里手动提交/回滚用

【为什么要「每个请求一个 Session」？】
    Session 是 SQLAlchemy 的工作单元（Unit of Work），内部缓存了本次操作的对象状态。
    多请求共用一个 Session 会导致数据串味、事务边界混乱。企业项目标准做法就是
    「一请求一会话，用完即关，连接归还池中」。
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.logging_config import logger

# =============================================================================
# 1. 创建数据库引擎
# =============================================================================
# 参数说明（企业项目常用配置）：
#   pool_pre_ping=True  每次从池中取连接前先 ping 一下，自动剔除已失效连接。
#                       这是解决 MySQL "server has gone away" 的关键参数。
#   pool_size           常驻连接数
#   max_overflow        峰值时额外允许创建的连接数（总上限 = size + overflow）
#   pool_recycle        连接存活秒数，超过就重建。MySQL 默认 8 小时断开空闲连接，
#                       所以这里设置为 3600 秒（1 小时）远小于 8 小时，避免踩坑。
#   pool_timeout        连接池耗尽时，等待可用连接的超时时间（秒）
#   echo                是否打印 SQL（由 .env 的 DB_ECHO 控制）
#   future=True         使用 SQLAlchemy 2.0 风格 API
engine = create_engine(
    settings.SQLALCHEMY_DATABASE_URI,
    pool_pre_ping=True,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_recycle=settings.DB_POOL_RECYCLE,
    pool_timeout=30,
    echo=settings.DB_ECHO,
    future=True,
    # 让返回的行支持中文列名等场景（PyMySQL 默认即可，这里显式声明）
    connect_args={"charset": settings.MYSQL_CHARSET},
)

# =============================================================================
# 1.5 SQLite 兼容处理（仅在使用 SQLite 时生效，不影响 MySQL）
# =============================================================================
# 说明：
#   MySQL 建表时我们通过 ForeignKey(..., ondelete="CASCADE") 声明了级联删除，
#   由数据库自己保证「删用户 → 连带删会话/消息」。
#   但 SQLite 默认【不强制】外键约束（PRAGMA foreign_keys 默认关闭），
#   这会导致用 SQLite 跑单元测试时，级联删除静默失效，测试通过但生产出错。
#   所以这里在每次建立 SQLite 连接时显式打开外键约束。
#
#   本项目生产环境使用 MySQL，这段代码只在 SQLite（测试/离线验证）时起作用。
if engine.dialect.name == "sqlite":
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:  # pragma: no cover
        """让 SQLite 强制外键约束，保证级联删除行为与 MySQL 一致。"""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    logger.debug("检测到 SQLite 数据库，已启用外键约束（用于测试环境）")


# =============================================================================
# 2. 会话工厂
# =============================================================================
#   autocommit=False  关闭自动提交，事务由我们显式控制（企业的正确姿势）
#   autoflush=False   查询前不自动 flush，行为更可预测，减少意外 SQL
#   expire_on_commit=False  提交后对象不过期，避免「提交后访问属性又触发一次查询」
#                           这一点对返回 ORM 对象给 Pydantic 序列化非常重要！
SessionLocal = sessionmaker(
    bind=engine,
    class_=Session,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    future=True,
)


# =============================================================================
# 3. FastAPI 依赖：获取数据库会话
# =============================================================================
def get_db() -> Generator[Session, Any, None]:
    """
    FastAPI 依赖注入函数。用法：

        @router.get("/users")
        def list_users(db: Session = Depends(get_db)):
            ...

    生命周期：
        yield 之前  → 创建会话（进入接口前）
        yield 之后  → 无论接口正常返回还是抛异常，都会执行 finally 关闭会话
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        # 接口抛异常时回滚未提交的事务，防止脏数据写入
        db.rollback()
        raise
    finally:
        db.close()  # 连接归还连接池（不是真正断开 TCP）


# =============================================================================
# 4. 事务上下文管理器（供脚本 / 后台任务使用）
# =============================================================================
@contextmanager
def transaction() -> Generator[Session, Any, None]:
    """
    手动事务块，适合写初始化脚本、定时任务：

        with transaction() as db:
            db.add(user)
        # 退出时自动 commit；出现异常自动 rollback

    【重要】不要在 API 接口中使用它 —— 接口用 Depends(get_db)，
           否则会出现两个 Session 互相看不到对方数据的问题。
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("事务执行失败，已回滚")
        raise
    finally:
        db.close()


# =============================================================================
# 5. 数据库可用性检测（健康检查接口用）
# =============================================================================
def check_database_connection() -> bool:
    """
    执行 SELECT 1 检测数据库是否可用。
    健康检查接口 /health 会调用它，运维监控依赖这个结果。
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1")) # 必须传对象，不能是纯字符串
        return True
    except Exception as exc:  # pragma: no cover
        logger.error("数据库连接检测失败：{}", exc)
        return False


def get_database_info() -> dict[str, Any]:
    """返回数据库版本等基础信息（健康检查接口展示用）。"""
    info: dict[str, Any] = {"connected": False}
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT VERSION()")).scalar()
            info.update({"connected": True, "version": version, "database": settings.MYSQL_DB})
    except Exception as exc:  # pragma: no cover
        info["error"] = str(exc)
    return info
