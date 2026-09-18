"""
==============================================================================
 FastAPI 依赖注入（Dependencies）
==============================================================================

「依赖注入」是本项目权限体系的核心。所有需要登录的接口都这样写：

    @router.get("/conversations")
    def list_conversations(
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_user),   # ← 自动完成认证
    ):
        # 到这里 current_user 一定存在且 is_active=True
        return crud_conversation.list_by_user(db, current_user.id)

本模块提供的依赖：
    get_db                  数据库会话（定义在 db/session.py，这里再导出方便使用）
    get_current_user        解析 JWT → 查出当前登录用户（401 拦截）
    get_current_active_user 在 get_current_user 基础上再校验账号是否被禁用
    get_current_superuser   仅允许管理员访问的接口使用
    get_owned_conversation  校验会话归属（【用户数据隔离的关键】）
    get_owned_document      校验文档可见性
    get_client_ip           获取真实客户端 IP（兼容 Nginx 反代）
    get_user_agent          获取 User-Agent（写审计日志用）
    get_request_id          获取请求追踪 ID
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.exceptions import AuthException, NotFoundException, PermissionException
from app.core.response import BusinessCode
from app.core.security import get_user_id_from_token
from app.crud.conversation import crud_conversation
from app.crud.document import crud_document
from app.crud.user import crud_user
from app.db.session import get_db
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.user import User

# =============================================================================
# 1. 数据库会话（session.py 里已定义，这里重新导出，让依赖集中在一个文件里）
# =============================================================================
# 用法：db: Session = Depends(get_db)
__all__ = [
    "get_db",
    "get_current_user",
    "get_current_active_user",
    "get_current_superuser",
    "get_owned_conversation",
    "get_owned_document",
    "get_client_ip",
    "get_user_agent",
    "get_request_id",
    "DbSession",
    "CurrentUser",
    "AdminUser",
]

# 类型别名：让接口签名更简洁
#     def api(db: DbSession, user: CurrentUser): ...
DbSession = Annotated[Session, Depends(get_db)]

# =============================================================================
# 2. HTTP Bearer 认证方案
# =============================================================================
# auto_error=False 的含义：请求头没有 Authorization 时不要立刻抛 403，
# 而是把 None 交给我们的代码处理，这样能返回统一的业务错误格式和中文提示。
bearer_scheme = HTTPBearer(auto_error=False, description="在请求头携带 Authorization: Bearer <token>")


def _extract_token(
    credentials: HTTPAuthorizationCredentials | None,
) -> str:
    """从 Authorization 头中取出 token 字符串。"""
    if credentials is None or not credentials.credentials:
        raise AuthException(message="请先登录后再操作", code=BusinessCode.UNAUTHORIZED)
    return credentials.credentials


# =============================================================================
# 3. 当前登录用户
# =============================================================================
def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    """
    认证依赖：解析 JWT 并返回用户对象。

    完整流程：
        1. 从 Authorization 头取出 token
        2. 校验签名与有效期（decode_token 内部处理，过期抛 TOKEN_EXPIRED）
        3. 从 payload 里拿 user_id，查数据库
        4. 校验用户存在 → 否则 401
        5. 校验 is_active → 否则 401（账号被禁用）

    任何一步失败都会抛 AuthException，由全局异常处理器转成统一格式：
        HTTP 401 + {"code": 2001, "message": "请先登录后再操作", ...}
    前端拦截器看到 401 会自动跳转到登录页。
    """
    token = _extract_token(credentials)
    user_id = get_user_id_from_token(token, expected_type="access")

    user = crud_user.get(db, user_id)
    if user is None:
        raise AuthException(message="用户不存在或已被删除，请重新登录")

    if not user.is_active:
        raise AuthException(
            message="账号已被禁用，请联系管理员", code=BusinessCode.USER_DISABLED
        )

    return user


# 类型别名：def api(user: CurrentUser) 等价于 Depends(get_current_user)
CurrentUser = Annotated[User, Depends(get_current_user)]


def get_current_active_user(current_user: CurrentUser) -> User:
    """
    当前有效用户。
    语义上与 get_current_user 相同（is_active 已在上面校验），
    保留这个依赖是为了兼容习惯 FastAPI 官方教程写法的同学。
    """
    return current_user


def get_current_superuser(current_user: CurrentUser) -> User:
    """
    管理员依赖：用于「用户管理」「审计日志」「全量会话查看」等后台接口。
    """
    if not current_user.is_superuser:
        raise PermissionException(message="该操作需要管理员权限")
    return current_user


# 类型别名：仅管理员可访问
AdminUser = Annotated[User, Depends(get_current_superuser)]


# =============================================================================
# 4. 【数据隔离】资源归属校验依赖
# =============================================================================
def get_owned_conversation(
    conversation_id: int,
    db: DbSession,
    current_user: CurrentUser,
) -> Conversation:
    """
    校验「路径参数里的 conversation_id 属于当前登录用户」，返回会话对象。

    用法（路径写成 /conversations/{conversation_id}）：
        @router.get("/conversations/{conversation_id}")
        def get_conversation(conversation: Conversation = Depends(get_owned_conversation)):
            return ResponseModel.success(data=conversation)

    这样写的好处：
        - 所有会话接口自动获得「归属校验」，不会因为某个人忘了写 WHERE 而越权
        - 业务函数里直接拿到会话对象，不用再查一次数据库

    安全说明：不属于自己时返回 404 而非 403。
             这样攻击者无法通过「404 还是 403」判断某个 ID 是否存在，
             避免会话 ID 被枚举探测。
    """
    conversation = crud_conversation.get_owned(db, conversation_id, current_user.id)
    if conversation is None:
        raise NotFoundException(message="会话不存在或无权访问")
    return conversation


def get_owned_document(
    document_id: int,
    db: DbSession,
    current_user: CurrentUser,
) -> Document:
    """
    校验文档对当前用户可见（公共文档 or 自己上传的），返回文档对象。
    """
    document = crud_document.get_visible(db, document_id, current_user.id)
    if document is None:
        raise NotFoundException(message="文档不存在或无权访问")
    return document


# =============================================================================
# 5. 请求上下文辅助依赖
# =============================================================================
def get_client_ip(
    request: Request,
    x_forwarded_for: Annotated[str | None, Header(alias="X-Forwarded-For")] = None,
    x_real_ip: Annotated[str | None, Header(alias="X-Real-IP")] = None,
) -> str:
    """
    获取真实客户端 IP。

    为什么不能直接用 request.client.host？
        部署到 Nginx / 负载均衡后面时，request.client.host 拿到的是代理的 IP。
        此时需要读 X-Forwarded-For（取第一个，那是最初的客户端）或 X-Real-IP。

    安全提醒：这两个头可以被伪造。若用于安全审计，
             必须确保只有可信代理能写入这些头（Nginx 配置里 set 而非透传）。
    """
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    if x_real_ip:
        return x_real_ip.strip()
    return request.client.host if request.client else "unknown"


def get_user_agent(
    user_agent: Annotated[str | None, Header(alias="User-Agent")] = None,
) -> str:
    """获取 User-Agent（写审计日志用）。"""
    return user_agent or "unknown"


def get_request_id(request: Request) -> str:
    """获取中间件生成的 request_id。"""
    return getattr(request.state, "request_id", "-")
