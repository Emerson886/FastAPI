"""
==============================================================================
 CRUD 包入口
==============================================================================

统一导出「CRUD 单例对象」，业务代码推荐这样用：

    from app.crud import crud_user, crud_conversation, crud_message, crud_document

    user = crud_user.authenticate(db, username, password)
    conv = crud_conversation.create_for_user(db, user_id=user.id)

也可单独导入：from app.crud.user import crud_user
"""

from app.crud.audit_log import crud_audit_log
from app.crud.base import CRUDBase
from app.crud.conversation import crud_conversation
from app.crud.document import crud_document
from app.crud.message import crud_message
from app.crud.user import crud_user

__all__ = [
    "CRUDBase",
    "crud_user",
    "crud_conversation",
    "crud_message",
    "crud_document",
    "crud_audit_log",
]
