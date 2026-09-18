"""
Fully read!!!
==============================================================================
 Pydantic Schema：用户（User）
==============================================================================

Schema 是「接口进出的数据契约」，与 ORM 模型（数据库表结构）分离，好处：
    1. 接口不会因为表结构变化而被动改变（解耦）
    2. 输入校验集中在这里（长度、正则、必填），业务代码不用写 if 判断
    3. 输出可以精确控制字段（比如绝不返回密码哈希）

本项目命名规范：
    XxxBase    —— 公共字段
    XxxCreate  —— 创建时的输入（含密码等敏感字段）
    XxxUpdate  —— 更新时的输入（所有字段可选）
    XxxOut     —— 输出给前端的结构
    XxxIn      —— 非 CRUD 的请求体（如登录、改密码）
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

# ----------------------------------------------------------------------------
# 校验规则常量（集中管理，前端也照这个提示用户）
# ----------------------------------------------------------------------------
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\u4e00-\u9fa5]{3,50}$")
# re.compile将字符串形式的正则表达式编译为一个pattern对象，可以调用match，full_match等方法
# r标注原始字符串，\不会被转义；[]表示字符集，^匹配字符串开头，$表示结尾
# 允许大小写字母，数字，下划线_，常用汉字，{3,50}表示该字符集重复3-50次，也就是至少3字符，最多50字符
PASSWORD_MIN_LEN = 6
PASSWORD_MAX_LEN = 64


class UserBase(BaseModel):
    """用户公共字段。"""

    username: str = Field(..., min_length=3, max_length=50, description="用户名，3-50 位")
    email: EmailStr | None = Field(default=None, description="邮箱，可选但唯一")
    nickname: str | None = Field(default=None, max_length=50, description="昵称")
    avatar: str | None = Field(default=None, max_length=500, description="头像地址")

    @field_validator("username", mode="after")
    @classmethod
    def _validate_username(cls, v: str) -> str:
        """用户名只允许字母、数字、下划线、中文 —— 防止注入奇怪字符。"""
        v = v.strip()
        if not USERNAME_PATTERN.match(v):
            raise ValueError("用户名只能包含字母、数字、下划线或中文，长度 3-50 位")
        return v


class UserCreate(UserBase):
    """注册 / 新建用户的请求体。"""

    password: str = Field(
        ...,
        min_length=PASSWORD_MIN_LEN,
        max_length=PASSWORD_MAX_LEN,
        description=f"密码，{PASSWORD_MIN_LEN}-{PASSWORD_MAX_LEN} 位",
    )
    confirm_password: str | None = Field(default=None, description="确认密码（注册时前端传）")

    @field_validator("password", mode="after") # after表示内部校验之后执行
    @classmethod
    def _validate_password(cls, v: str) -> str:
        """
        密码强度校验：至少包含字母和数字。
        企业系统通常还要求特殊字符，可按需调整此处正则。
        """
        if not re.search(r"[A-Za-z]", v):
            raise ValueError("密码必须包含字母")
        if not re.search(r"\d", v):
            raise ValueError("密码必须包含数字")
        return v

    @model_validator(mode="after")
    def _check_confirm(self) -> "UserCreate":
        """两次密码输入必须一致（如果前端传了 confirm_password）。"""
        if self.confirm_password is not None and self.confirm_password != self.password:
            raise ValueError("两次输入的密码不一致")
        return self


class UserRegister(UserCreate):
    """注册请求体（与 UserCreate 相同，单独定义便于将来加邀请码等字段）。"""

    invite_code: str | None = Field(default=None, description="邀请码（如需邀请注册）")


class UserLogin(BaseModel):
    """登录请求体（JSON 方式登录，比 OAuth2 表单更适合前后端分离）。"""

    username: str = Field(..., description="用户名或邮箱")
    password: str = Field(..., description="密码")
    remember: bool = Field(default=False, description="记住我：True 时后端签发更长有效期的令牌")


class UserUpdate(BaseModel):
    """更新用户资料（所有字段可选，只传要改的字段）。"""

    nickname: str | None = Field(default=None, max_length=50)
    avatar: str | None = Field(default=None, max_length=500)
    email: EmailStr | None = None


class UserPasswordUpdate(BaseModel):
    """修改密码请求体。"""

    old_password: str = Field(..., min_length=1, description="原密码") # old_password 必须提供
    new_password: str = Field(
        ..., min_length=PASSWORD_MIN_LEN, max_length=PASSWORD_MAX_LEN, description="新密码"
    )
    confirm_password: str | None = Field(default=None, description="确认新密码")

    @field_validator("new_password", mode="after")
    @classmethod
    def _validate_new(cls, v: str) -> str:
        if not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
            raise ValueError("新密码必须同时包含字母和数字")
        return v

    @model_validator(mode="after")
    def _check_confirm(self) -> "UserPasswordUpdate":
        if self.confirm_password is not None and self.confirm_password != self.new_password:
            raise ValueError("两次输入的新密码不一致")
        if self.old_password == self.new_password:
            raise ValueError("新密码不能与原密码相同")
        return self


class UserOut(BaseModel):
    """
    用户信息输出结构。

    【安全要点】这里【没有】password / hashed_password 字段，
    所以即使接口不小心返回了 ORM 对象，也不会泄露密码。
    这是比「手写 exclude」更安全的做法（用结构本身保证安全）。
    """

    model_config = ConfigDict(from_attributes=True)  # 允许直接从 ORM 对象构造，Pydantic 会尝试从对象的属性中读取字段值

    id: int
    username: str
    email: str | None = None
    nickname: str | None = None
    avatar: str | None = None
    is_active: bool = True
    is_superuser: bool = False
    last_login_at: datetime | None = None
    created_at: datetime | None = None


class UserBrief(BaseModel):
    """用户简要信息（嵌在会话/文档里返回，减少数据量）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    nickname: str | None = None
    avatar: str | None = None


# ============================================================================
# 认证相关
# ============================================================================
class TokenOut(BaseModel):
    """登录成功返回的令牌信息。"""

    access_token: str = Field(..., description="访问令牌，后续请求放在 Authorization: Bearer <token>")
    refresh_token: str = Field(..., description="刷新令牌，access 过期后用它换新令牌")
    token_type: str = Field(default="bearer", description="令牌类型，固定 bearer")
    expires_in: int = Field(..., description="access_token 有效期（秒）")
    user: UserOut = Field(..., description="当前登录用户信息")


class RefreshTokenIn(BaseModel):
    """刷新令牌请求体。"""

    refresh_token: str = Field(..., description="登录时返回的 refresh_token")


class LoginLogOut(BaseModel):
    """登录日志输出（用户可在个人中心查看自己的登录记录）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    ip: str | None = None
    user_agent: str | None = None
    success: bool
    created_at: datetime
