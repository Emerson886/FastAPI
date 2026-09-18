"""
==============================================================================
 测试工具：内存数据库测试环境
==============================================================================

【为什么需要它？】
    跑单元测试时不应该依赖真实的 MySQL（慢、需要配置、数据会污染）。
    本模块提供一个「即用即弃」的 SQLite 测试数据库：

        from tests.conftest_helpers import build_test_session

        with build_test_session() as db:
            db.add(User(username="alice", hashed_password="x"))
            db.commit()
            assert db.query(User).count() == 1

【关键点：SQLite 外键】
    SQLite 默认不强制外键约束，级联删除会静默失效。
    本模块在建连接时执行 PRAGMA foreign_keys=ON，
    让测试环境的行为与 MySQL 生产环境保持一致。
    （app/db/session.py 里也做了同样的处理）

【适用范围】
    只验证「ORM 模型 / CRUD 逻辑」，不涉及 MySQL 特有语法
    （如 JSON 类型在 SQLite 里是可用的，但 FULLTEXT 索引不可用）。
    MySQL 方言的 DDL 正确性请用 scripts/preflight_check.py 验证。
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# ---------------------------------------------------------------------------
# 路径准备：必须【在 import app 之前】完成
# ---------------------------------------------------------------------------
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 支持注入「桩模块目录」（离线环境缺少某些第三方包时使用）：
#   set KB_STUB_DIR=C:\path\to\stubs
# 这里把桩目录放在 sys.path【最前】，保证优先于真实包被导入。
_stub_dir = os.environ.get("KB_STUB_DIR")
if _stub_dir and Path(_stub_dir).is_dir() and _stub_dir not in sys.path:
    sys.path.insert(0, _stub_dir)

from app.db.base import Base  # noqa: E402

# 必须导入模型包，模型才会注册到 Base.metadata
import app.models  # noqa: E402, F401


def create_test_engine(echo: bool = False) -> Engine:
    """
    创建内存 SQLite 引擎，并开启外键约束。

    使用内存数据库（sqlite:///:memory:）的好处是测试之间完全隔离，
    且不会在磁盘留下任何文件。
    """
    engine = create_engine(
        "sqlite:///:memory:",
        echo=echo,
        # 内存数据库在多连接场景下需要 StaticPool 才能共享同一个库
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record) -> None:  # pragma: no cover
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


@contextlib.contextmanager
def build_test_session(echo: bool = False) -> Generator[Session, None, None]:
    """
    上下文管理器：自动建表 + 提供 Session + 结束时自动销毁。

    用法：
        with build_test_session() as db:
            ...测试代码...
    """
    engine = create_test_engine(echo=echo)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def create_test_user(db: Session, username: str = "tester", **kwargs):
    """
    快捷创建一个测试用户（密码哈希是假的，不用于真实登录校验）。
    """
    from app.models import User

    defaults = {
        "username": username,
        "email": f"{username}@test.local",
        "hashed_password": "$2b$12$test_hash_placeholder_value_for_unit_test_only",
        "nickname": username,
    }
    defaults.update(kwargs)
    user = User(**defaults)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def create_test_conversation(db: Session, user_id: int, title: str = "测试会话", **kwargs):
    """快捷创建一个测试会话。"""
    from app.models import Conversation

    conversation = Conversation(user_id=user_id, title=title, **kwargs)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def create_test_message(
    db: Session, conversation_id: int, user_id: int, role: str = "user", content: str = "测试消息", **kwargs
):
    """快捷创建一条测试消息。"""
    from app.models import Message

    message = Message(
        conversation_id=conversation_id, user_id=user_id, role=role, content=content, **kwargs
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message
