"""
Fully read!!!!!!
==============================================================================
 数据库初始化：建表 + 创建初始管理员
==============================================================================

被 main.py 在应用启动时调用（当 settings.DB_AUTO_CREATE=True）。

⚠ 关于「自动建表」与「数据库迁移」的取舍（企业项目必读）：
    create_all()  适合开发阶段：快速起表，不用写 SQL。
                  缺点：只能「新建表」，不能修改已存在表的结构
                        （比如给 users 表加一个字段，create_all 不会生效）。
    生产环境     建议用 Alembic 做版本化迁移：
                  1. pip install alembic
                  2. alembic init migrations
                  3. 在 env.py 里 import app.models 并指向 Base.metadata
                  4. alembic revision --autogenerate -m "add column xxx"
                  5. alembic upgrade head
                  本项目已在 backend/scripts/init_db.py 提供建表脚本，
                  需要 Alembic 时可以按上面步骤接入。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging_config import logger
from app.core.security import hash_password
from app.db.base import Base
from app.db.session import SessionLocal, engine

# 【关键】必须导入 app.models 触发所有模型类注册到 Base.metadata，
#        否则 create_all 会漏建表。
from app.models import User  # noqa: F401
import app.models  # noqa: F401


def init_db() -> None:
    """创建所有表（表已存在则跳过，不会覆盖数据）。"""
    try:
        Base.metadata.create_all(bind=engine)
        # Base.metadata
        # metadata 是一个 MetaData 对象，可以理解为表定义的注册中心。
        #
        # 每定义一个继承 Base 的模型，就会在 metadata.tables 中注册一张表。
        #
        # 它保存了所有表名、列、约束、索引等信息。
        #
        # 之前配置的 naming_convention 通常就是传给 MetaData 的，所以这里生成的约束名也会遵循该约定。
        #
        # .create_all()
        # MetaData.create_all() 是 SQLAlchemy 提供的方法。
        #
        # 它会为 metadata 中所有表生成 CREATE TABLE 语句，并在目标数据库执行。
        #
        # 默认参数 checkfirst=True，即先检查表是否存在，存在就跳过，不存在才创建。
        #
        # 它还会根据外键依赖关系，自动排序建表顺序，避免因外键引用导致创建失败。
        #
        # 它不会删除表、修改列、添加索引到已有表。只负责“从无到有”地创建。
        logger.info(
            "数据库表检查完成 | 共 {} 张表 | 目标库={}",
            len(Base.metadata.tables),
            settings.MYSQL_DB,
        )
    except Exception as exc:
        logger.error(
            "建表失败：{} | 请检查 backend/.env 里的 MYSQL_* 配置是否正确、数据库是否已创建",
            exc,
        )
        raise


def _create_admin_if_absent(db: Session) -> None:
    """如果系统里还没有任何用户，则创建一个内置管理员账号。"""
    exists = db.query(User).first() # 查询 User 表，返回第一条记录
    if exists is not None:
        logger.debug("已存在用户数据，跳过初始管理员创建")
        return

    admin = User(
        username=settings.FIRST_ADMIN_USERNAME,
        email=settings.FIRST_ADMIN_EMAIL,
        nickname="系统管理员",
        hashed_password=hash_password(settings.FIRST_ADMIN_PASSWORD),
        is_active=True,
        is_superuser=True,
    )
    db.add(admin)
    db.commit()
    logger.warning(
        "已创建初始管理员账号 | 用户名={} | 密码={} | ★ 请登录后立即修改密码 ★",
        settings.FIRST_ADMIN_USERNAME,
        settings.FIRST_ADMIN_PASSWORD,
    )


def init_data() -> None:
    """初始化基础数据（初始管理员）。"""
    db = SessionLocal()
    # SessionLocal 通常是 SQLAlchemy 中通过 sessionmaker(bind=engine) 创建的会话工厂。
    #
    # 调用 SessionLocal() 会返回一个新的 Session 对象，用于执行 ORM 查询、插入、提交等操作。
    #
    # 每个请求或每个独立任务通常都会创建自己的会话，用完关闭，避免会话长期占用连接。
    try:
        _create_admin_if_absent(db)
    finally:
        db.close()


def init_all() -> None:
    """启动时一键初始化（建表 + 初始数据）。"""
    if not settings.DB_AUTO_CREATE:
        logger.info("DB_AUTO_CREATE=False，跳过自动建表（生产环境请使用 Alembic 迁移）")
        return
    init_db()
    init_data()
