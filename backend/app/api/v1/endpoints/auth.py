"""
==============================================================================
 API v1 端点：认证与用户（auth）
==============================================================================

接口清单：
    POST   /api/v1/auth/register          用户注册
    POST   /api/v1/auth/login             用户登录（返回 JWT 双令牌）
    POST   /api/v1/auth/refresh           刷新访问令牌
    POST   /api/v1/auth/logout            退出登录（记录审计）
    GET    /api/v1/auth/me                获取当前登录用户信息
    PUT    /api/v1/auth/me                修改个人资料
    POST   /api/v1/auth/change-password   修改密码
    GET    /api/v1/auth/login-logs        查看自己的登录记录

【安全设计说明】
    1. 登录失败统一提示「用户名或密码错误」，不透露账号是否存在
    2. 所有认证相关操作都写审计日志
    3. 注册接口可以直接开放（内部系统），也可加邀请码校验
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import (
    CurrentUser,
    DbSession,
    get_client_ip,
    get_request_id,
    get_user_agent,
)
from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.logging_config import logger
from app.core.response import BusinessCode, PageResponseModel, PageResult, ResponseModel
from app.core.security import create_access_token, create_refresh_token, get_user_id_from_token
from app.crud import crud_audit_log, crud_user
from app.models.user import User
from app.schemas.common import MessageOutSimple, PaginationQuery
from app.schemas.user import (
    LoginLogOut,
    RefreshTokenIn,
    TokenOut,
    UserCreate,
    UserLogin,
    UserOut,
    UserPasswordUpdate,
    UserUpdate,
)

router = APIRouter(prefix="/auth", tags=["认证与用户"])


def _build_token_out(user: User) -> TokenOut:
    """
    生成登录返回的令牌对象。

    同时把用户信息塞进去，前端登录后一次请求就能拿到 token + 用户资料，
    不需要再单独调 /me（减少一次网络往返）。
    """
    return TokenOut(
        access_token=create_access_token(user.id, user.username),
        refresh_token=create_refresh_token(user.id),
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserOut.model_validate(user),
        # model_validate:它把任意输入数据解析成 UserOut 实例，并执行 Pydantic 的类型校验和转换。
    )


# =============================================================================
# 1. 注册
# =============================================================================
@router.post(
    "/register",
    response_model=ResponseModel[UserOut],
    status_code=status.HTTP_201_CREATED,
    summary="用户注册",
    description="注册新用户。用户名与邮箱均需唯一。",
)
def register(
    payload: UserCreate,
    db: DbSession,
    request: Request,
    ip: str = Depends(get_client_ip),
    ua: str = Depends(get_user_agent),
    request_id: str = Depends(get_request_id),
):
    # ---- 唯一性校验（先查再插，配合数据库唯一索引双保险）----
    if crud_user.get_by_username(db, payload.username):
        raise BusinessException(
            message=f"用户名「{payload.username}」已被注册", code=BusinessCode.USERNAME_EXISTS
        )
    if payload.email and crud_user.get_by_email(db, payload.email):
        raise BusinessException(message="该邮箱已被注册", code=BusinessCode.USERNAME_EXISTS)

    # ---- 创建用户（密码在 crud.create_user 内部自动哈希）----
    user = crud_user.create_user(db, payload)
    logger.info("新用户注册成功 | id={} | username={}", user.id, user.username)

    # ---- 写审计日志 ----
    crud_audit_log.write(
        db,
        action="register",
        user_id=user.id,
        username=user.username,
        resource="user",
        resource_id=user.id,
        ip=ip,
        user_agent=ua,
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        success=True,
        message="用户注册成功",
    )
    return ResponseModel.success(data=user, message="注册成功")


# =============================================================================
# 2. 登录
# =============================================================================
@router.post(
    "/login",
    response_model=ResponseModel[TokenOut],
    summary="用户登录",
    description="使用用户名（或邮箱）+ 密码登录，成功后返回 access_token 与 refresh_token。",
)
def login(
    payload: UserLogin,
    db: DbSession,
    request: Request,
    ip: str = Depends(get_client_ip),
    ua: str = Depends(get_user_agent),
    request_id: str = Depends(get_request_id),
):
    # ---- 认证（内部做了防时序攻击处理）----
    user = crud_user.authenticate(db, payload.username, payload.password)

    if user is None:
        # 失败也要留痕：连续失败可用于识别暴力破解
        crud_audit_log.write(
            db,
            action="login",
            username=payload.username,
            resource="user",
            ip=ip,
            user_agent=ua,
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            success=False,
            message="用户名或密码错误",
        )
        raise BusinessException(
            message="用户名或密码错误", code=BusinessCode.PASSWORD_ERROR
        )

    if not user.is_active:
        crud_audit_log.write(
            db, action="login", user_id=user.id, username=user.username,
            ip=ip, success=False, message="账号已禁用",
        )
        raise BusinessException(
            message="账号已被禁用，请联系管理员", code=BusinessCode.USER_DISABLED
        )

    # ---- 记录登录信息（时间 + IP）----
    crud_user.update_login_info(db, user, ip)

    crud_audit_log.write(
        db, action="login", user_id=user.id, username=user.username,
        resource="user", resource_id=user.id, ip=ip, user_agent=ua,
        request_id=request_id, method=request.method, path=request.url.path,
        success=True, message="登录成功",
    )
    logger.info("用户登录成功 | id={} | username={} | ip={}", user.id, user.username, ip)

    return ResponseModel.success(data=_build_token_out(user), message="登录成功")


# =============================================================================
# 3. 刷新令牌
# =============================================================================
@router.post(
    "/refresh",
    response_model=ResponseModel[TokenOut],
    summary="刷新访问令牌",
    description="access_token 过期后，用 refresh_token 换取新的令牌对，避免用户重新登录。",
)
def refresh_token(payload: RefreshTokenIn, db: DbSession):
    # 校验 refresh_token（decode_token 会检查 type == refresh，防止拿 access 冒充）
    user_id = get_user_id_from_token(payload.refresh_token, expected_type="refresh")

    user = crud_user.get(db, user_id)
    if user is None or not user.is_active:
        raise BusinessException(
            message="用户不存在或已被禁用，请重新登录", code=BusinessCode.UNAUTHORIZED
        )

    # 刷新时同时下发新的 refresh_token（令牌轮换，安全性更好）
    return ResponseModel.success(data=_build_token_out(user), message="令牌刷新成功")


# =============================================================================
# 4. 退出登录
# =============================================================================
@router.post(
    "/logout",
    response_model=ResponseModel[MessageOutSimple],
    summary="退出登录",
    description="服务端记录审计日志。JWT 是无状态的，真正的令牌失效由前端清除本地存储完成。",
)
def logout(
    db: DbSession,
    current_user: CurrentUser,
    request: Request,
    ip: str = Depends(get_client_ip),
    request_id: str = Depends(get_request_id),
):
    """
    说明：JWT 方案下服务端不保存会话，所以「退出」主要是前端删掉 token。
    如果企业要求「服务端强制失效」（如管理员踢人下线），
    可以在这里把当前 token 的 jti 写入 Redis 黑名单，并在认证依赖里校验。
    本项目保持轻量，未引入 Redis；如需该能力，参考 docs/architecture.md 的扩展说明。
    """
    crud_audit_log.write(
        db, action="logout", user_id=current_user.id, username=current_user.username,
        ip=ip, request_id=request_id, method=request.method, path=request.url.path,
        success=True, message="退出登录",
    )
    return ResponseModel.success(data=MessageOutSimple(message="已退出登录"), message="已退出登录")


# =============================================================================
# 5. 当前用户信息
# =============================================================================
@router.get(
    "/me",
    response_model=ResponseModel[UserOut],
    summary="获取当前登录用户信息",
    description="前端刷新页面后调用它校验 token 是否仍有效，并获取最新用户资料。",
)
def read_me(current_user: CurrentUser):
    return ResponseModel.success(data=UserOut.model_validate(current_user))


@router.put(
    "/me",
    response_model=ResponseModel[UserOut],
    summary="修改个人资料",
    description="目前支持修改昵称、头像、邮箱。",
)
def update_me(payload: UserUpdate, db: DbSession, current_user: CurrentUser):
    # 邮箱唯一性校验（排除自己）
    if payload.email:
        existed = crud_user.get_by_email(db, payload.email)
        if existed is not None and existed.id != current_user.id:
            raise BusinessException(message="该邮箱已被其他用户使用", code=BusinessCode.USERNAME_EXISTS)

    user = crud_user.update(db, current_user, payload)
    return ResponseModel.success(data=UserOut.model_validate(user), message="资料更新成功")


# =============================================================================
# 6. 修改密码
# =============================================================================
@router.post(
    "/change-password",
    response_model=ResponseModel[MessageOutSimple],
    summary="修改密码",
    description="需要提供原密码。修改成功后建议前端引导用户重新登录。",
)
def change_password(
    payload: UserPasswordUpdate,
    db: DbSession,
    current_user: CurrentUser,
    ip: str = Depends(get_client_ip),
):
    from app.core.security import verify_password

    # 校验原密码
    if not verify_password(payload.old_password, current_user.hashed_password):
        raise BusinessException(message="原密码不正确", code=BusinessCode.PASSWORD_ERROR)

    crud_user.change_password(db, current_user, payload.new_password)
    crud_audit_log.write(
        db, action="change_password", user_id=current_user.id, username=current_user.username,
        ip=ip, success=True, message="修改密码成功",
    )
    logger.info("用户修改密码 | id={} | username={}", current_user.id, current_user.username)
    return ResponseModel.success(
        data=MessageOutSimple(message="密码修改成功"), message="密码修改成功，请重新登录"
    )


# =============================================================================
# 7. 我的登录记录
# =============================================================================
@router.get(
    "/login-logs",
    response_model=PageResponseModel[LoginLogOut],
    summary="查看我的登录记录",
    description="个人中心安全页展示最近的登录 IP、时间、设备。",
)
def my_login_logs(
    db: DbSession,
    current_user: CurrentUser,
    pagination: PaginationQuery = Depends(),
):
    logs, total = crud_audit_log.list_logs(
        db,
        user_id=current_user.id,
        action="login",
        skip=pagination.skip,
        limit=pagination.limit,
    )
    return ResponseModel.success(
        data=PageResult.create(
            items=[LoginLogOut.model_validate(log) for log in logs],
            total=total,
            page=pagination.page,
            page_size=pagination.page_size,
        )
    )
