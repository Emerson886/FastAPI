"""
==============================================================================
 模型包入口
==============================================================================

作用：
    1. 集中导出所有 ORM 模型，业务代码可写 `from app.models import User, Conversation`
    2. 【关键】保证所有模型类都被 import 过一次。
       SQLAlchemy 的 Base.metadata.create_all() 只会创建「已经被导入过的模型」对应的表。
       如果某个模型没被导入，它的表就不会被创建 —— 这是新手最常见的坑。

在 app/db/init_db.py 中调用 create_all 前，会先 import app.models。
"""

from app.db.base import Base
from app.models.audit_log import AuditLog
from app.models.conversation import Conversation
from app.models.document import Document, DocumentChunk, DocumentStatus
from app.models.message import Message
from app.models.user import User

__all__ = [
    "Base",
    "User",
    "Conversation",
    "Message",
    "Document",
    "DocumentChunk",
    "DocumentStatus",
    "AuditLog",
]
