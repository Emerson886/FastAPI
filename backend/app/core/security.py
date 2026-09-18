"""
Fully read!!!
==============================================================================
 安全模块：密码哈希 + JWT 令牌
==============================================================================

安全设计说明（企业项目必看）：
    1. 【绝不存明文密码】数据库里只存 bcrypt 哈希值。bcrypt 自带随机盐，
       同一密码每次哈希结果都不同，无法被彩虹表反查。
    2. 【JWT 双令牌】
       - access_token ：短期（默认 1 天），每次请求带在 Authorization 头里
       - refresh_token：长期（默认 7 天），access 过期后用它换新的 access，
                        避免用户频繁重新登录
    3. JWT payload 里只放「用户ID + 用户名 + 类型 + 过期时间」，
       绝不放密码、手机号等敏感信息（JWT 只是签名，不是加密，内容可被解码查看）。
    4. token 类型用 "type" 字段区分，防止把 refresh_token 当 access_token 用。

安全提醒：生产环境务必修改 .env 中的 SECRET_KEY！
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import bcrypt
import jwt
from jwt import ExpiredSignatureError, InvalidTokenError

from app.core.config import settings
from app.core.exceptions import AuthException
from app.core.response import BusinessCode

# =============================================================================
# 1. 密码哈希（bcrypt）
# =============================================================================
# bcrypt 有一个硬限制：只处理密码前 72 字节，超长密码需先截断，否则新版本库会直接报错
_BCRYPT_MAX_BYTES = 72


def _prepare_password(password: str) -> bytes:
    """把密码转成 bcrypt 可处理的字节串（UTF-8 编码并截断到 72 字节）。"""
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """
    生成密码哈希。

    :param password: 明文密码
    :return: 形如 $2b$12$.... 的哈希字符串，长度 60，直接存 users.hashed_password
    """
    salt = bcrypt.gensalt(rounds=12)  # 生成随机盐，rounds 越大越安全但越慢，12 是业界常用平衡点
    return bcrypt.hashpw(_prepare_password(password), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    校验明文密码与数据库哈希是否匹配。

    注意：即使哈希串格式损坏也不能抛异常，直接返回 False，
         否则会变成「用户名存在就能判断出异常」的信息泄露。
    """
    try:
        # checkpw会自动从hashed_password解析出盐和成本因子
        return bcrypt.checkpw(
            _prepare_password(plain_password), hashed_password.encode("utf-8")
        )
    except (ValueError, TypeError):
        return False


# =============================================================================
# 2. JWT 令牌
# =============================================================================
TokenType = Literal["access", "refresh"]


def _create_token(
    subject: str | int,
    token_type: TokenType,
    expires_minutes: int,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """
    内部方法：生成 JWT。

    payload 标准字段说明：
        sub : subject，这里放用户 ID（JWT 规范要求是字符串）
        exp : 过期时间（UTC 时间戳），PyJWT 会自动校验
        iat : 签发时间
        jti : 令牌唯一 ID，将来做「退出登录黑名单」时用它
        type: 自定义字段，区分 access / refresh
    """
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "type": token_type,
        "iat": now,
        "exp": now + timedelta(minutes=expires_minutes),
        "jti": uuid.uuid4().hex,
    }
    if extra_claims:
        payload.update(extra_claims)

    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_access_token(user_id: int, username: str) -> str:
    """创建访问令牌，登录成功后返回给前端。"""
    return _create_token(
        subject=user_id,
        token_type="access",
        expires_minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES,
        extra_claims={"username": username},
    )


def create_refresh_token(user_id: int) -> str:
    """创建刷新令牌，用于 access_token 过期后换取新令牌。"""
    return _create_token(
        subject=user_id,
        token_type="refresh",
        expires_minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES,
    )


def decode_token(token: str, expected_type: TokenType = "access") -> dict[str, Any]:
    """
    解码并校验 JWT。

    会抛出的业务异常：
        AuthException(TOKEN_EXPIRED)  —— 令牌过期，前端应跳登录页或用 refresh 换新
        AuthException(UNAUTHORIZED)   —— 签名错误 / 被篡改 / 类型不符
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except ExpiredSignatureError:
        raise AuthException(message="登录已过期，请重新登录", code=BusinessCode.TOKEN_EXPIRED)
    except InvalidTokenError:
        raise AuthException(message="登录凭证无效，请重新登录", code=BusinessCode.UNAUTHORIZED)

    # 校验令牌类型：防止用 refresh_token 直接访问业务接口
    if payload.get("type") != expected_type:
        raise AuthException(message="登录凭证类型错误，请重新登录", code=BusinessCode.UNAUTHORIZED)

    if not payload.get("sub"):
        raise AuthException(message="登录凭证缺少用户信息", code=BusinessCode.UNAUTHORIZED)

    return payload


def get_user_id_from_token(token: str, expected_type: TokenType = "access") -> int:
    """
    从令牌中取出用户 ID（int 类型）。
    这是认证依赖（deps.py）里最常用的方法。
    """
    payload = decode_token(token, expected_type=expected_type)
    try:
        return int(payload["sub"])
    except (TypeError, ValueError):
        raise AuthException(message="登录凭证中的用户标识非法")
