"""
==============================================================================
 Schema 包入口
==============================================================================

统一导出所有 Pydantic 模型，业务代码可写：
    from app.schemas import UserCreate, UserOut, ChatRequest
"""

from app.schemas.common import (
    AuditLogOut,
    HealthOut,
    MessageOutSimple,
    PaginationQuery,
)
from app.schemas.conversation import (
    ConversationCreate,
    ConversationDetailOut,
    ConversationOut,
    ConversationUpdate,
)
from app.schemas.document import (
    CategoryStatOut,
    ChunkOut,
    DocumentCreate,
    DocumentOut,
    DocumentStatsOut,
    DocumentUpdate,
    DocumentUploadOut,
)
from app.schemas.message import (
    ChatRequest,
    ChatStreamChunk,
    Citation,
    MessageCreate,
    MessageOut,
    MessageUpdate,
)
from app.schemas.user import (
    LoginLogOut,
    RefreshTokenIn,
    TokenOut,
    UserBrief,
    UserCreate,
    UserLogin,
    UserOut,
    UserPasswordUpdate,
    UserRegister,
    UserUpdate,
)

__all__ = [
    # user
    "UserCreate", "UserRegister", "UserLogin", "UserUpdate", "UserPasswordUpdate",
    "UserOut", "UserBrief", "TokenOut", "RefreshTokenIn", "LoginLogOut",
    # conversation
    "ConversationCreate", "ConversationUpdate", "ConversationOut", "ConversationDetailOut",
    # message
    "MessageCreate", "MessageUpdate", "MessageOut", "Citation", "ChatRequest", "ChatStreamChunk",
    # document
    "DocumentCreate", "DocumentUpdate", "DocumentOut", "DocumentUploadOut",
    "ChunkOut", "DocumentStatsOut", "CategoryStatOut",
    # common
    "PaginationQuery", "HealthOut", "AuditLogOut", "MessageOutSimple",
]
