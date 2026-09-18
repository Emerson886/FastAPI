"""
Fully read!!!
==============================================================================
 统一响应模型（企业项目必备）
==============================================================================

为什么需要它？
    后端接口返回格式不统一，前端就要写一堆 if/else 判断。
    企业项目通常约定：所有接口都返回同一层「信封」（envelope）：

        {
          "code": 0,                  # 业务状态码：0=成功，非0=业务错误
          "message": "success",       # 给人看的提示信息
          "data": {...},              # 真正的业务数据（可能是对象/列表/null）
          "request_id": "8f3c...",    # 链路追踪 ID，排查问题时让用户报这个
          "timestamp": 1730000000     # 服务器时间戳
        }

前端只需判断 code === 0 即为成功，错误统一走 message 展示。

本模块提供：
    - BusinessCode     : 业务码枚举（集中管理，避免魔法数字）
    - ResponseModel    : 通用响应模型（含 success / fail 两个快捷构造方法）
    - PageResult       : 分页数据结构（总条数 + 当前页数据）
    - PageResponse     : 分页响应模型
"""

from __future__ import annotations

import time
from enum import IntEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


def _current_request_id() -> str | None:
    """
    读取当前请求上下文中的 request_id。

    放在模块级（而不是内联 lambda）有两个好处：
        1. 可读性更好，也便于单元测试单独调用
        2. 延迟导入 request_context，避免 response 与 request_context 之间
           出现循环依赖（request_context 不依赖 response，方向是单向的）

    不在 HTTP 请求上下文中时（如启动脚本、后台任务）返回 None，
    这符合语义：「没有请求，就没有请求 ID」。
    """
    from app.core.request_context import get_request_id

    value = get_request_id(default="")
    return value or None


# =============================================================================
# 业务状态码
# =============================================================================
class BusinessCode(IntEnum):
    """
    业务状态码约定（与 HTTP 状态码解耦）：
        0        成功
        1xxx     通用/参数类错误
        2xxx     认证授权类错误
        3xxx     资源不存在 / 权限不足
        4xxx     业务规则类错误
        5xxx     第三方服务（大模型、向量库）错误
        9xxx     服务器内部错误
    """

    SUCCESS = 0

    # ---- 参数与通用 ----
    PARAM_ERROR = 1001          # 请求参数校验失败
    BAD_REQUEST = 1002          # 非法请求
    RATE_LIMITED = 1003         # 请求过于频繁

    # ---- 认证授权 ----
    UNAUTHORIZED = 2001         # 未登录 / token 无效
    TOKEN_EXPIRED = 2002        # token 已过期
    PASSWORD_ERROR = 2003       # 用户名或密码错误
    USER_DISABLED = 2004        # 账号被禁用
    USERNAME_EXISTS = 2005      # 用户名已存在

    # ---- 资源与权限 ----
    NOT_FOUND = 3001            # 资源不存在
    FORBIDDEN = 3002            # 无权访问（比如访问别人的会话）

    # ---- 业务规则 ----
    BUSINESS_ERROR = 4001       # 通用业务错误
    FILE_TOO_LARGE = 4002       # 文件超出大小限制
    FILE_TYPE_NOT_ALLOWED = 4003  # 文件类型不允许
    DOC_PARSE_ERROR = 4004      # 文档解析失败

    # ---- 外部服务 ----
    LLM_ERROR = 5001            # 大模型调用失败
    VECTOR_STORE_ERROR = 5002   # 向量库异常

    # ---- 服务器 ----
    INTERNAL_ERROR = 9001       # 未捕获的服务器异常


# =============================================================================
# 通用响应模型
# =============================================================================
class ResponseModel(BaseModel, Generic[T]):
    """
    统一响应体。

    用法示例：
        # 成功，带数据
        return ResponseModel.success(data=user_out, message="登录成功")

        # 成功，无数据
        return ResponseModel.success()

        # 失败
        return ResponseModel.fail(code=BusinessCode.PASSWORD_ERROR, message="用户名或密码错误")
    """

    code: int = Field(default=BusinessCode.SUCCESS, description="业务状态码，0 表示成功")
    message: str = Field(default="success", description="提示信息")
    data: T | None = Field(default=None, description="业务数据")
    # 【自动填充】request_id 由 RequestContextMiddleware 写入 contextvars，
    # 这里通过 default_factory 自动读取，业务代码无需手动传参。
    # 这样「响应体里的 request_id」与「响应头 X-Request-ID」「日志里的 request_id」
    # 三者始终一致，用户报错时直接报这一个 ID 就能定位全部日志。
    request_id: str | None = Field(
        default_factory=lambda: _current_request_id(),
        description="请求追踪 ID，便于日志排查（与响应头 X-Request-ID 一致）",
    )
    timestamp: int = Field(default_factory=lambda: int(time.time()), description="服务器时间戳（秒）")

    # ------------------------------------------------------------------
    # 快捷构造方法
    # ------------------------------------------------------------------
    # 这里的装饰器@classmethod的作用是构建工厂函数：不需要实例化即可调用

    # 返回值加引号：前向引用, 在类体内部定义方法时，类本身还没有创建完成。
    # 它变成一个字符串字面量，Python 不会立即求值，只是把它存起来。
    # 等类定义完成、ResponseModel 已经存在后，
    # 类型检查工具、Pydantic、FastAPI 等再通过 typing.get_type_hints() 去解析这个字符串。
    @classmethod
    def success(
        cls,
        data: Any = None,
        message: str = "success",
        code: BusinessCode | int = BusinessCode.SUCCESS,
        request_id: str | None = None,
    ) -> "ResponseModel[Any]":
        """
        构造成功响应。

        :param request_id: 一般不用传 —— 留空时会自动从请求上下文读取。
                           只有在非 HTTP 场景（后台任务、脚本）想手动指定时才传。
        """
        # 返回传参success函数的实例化ResponseModel
        return cls(
            code=int(code),
            message=message,
            data=data,
            request_id=request_id if request_id is not None else _current_request_id(),
        )

    @classmethod
    def fail(
        cls,
        code: BusinessCode | int = BusinessCode.BUSINESS_ERROR,
        message: str = "操作失败",
        data: Any = None,
        request_id: str | None = None,
    ) -> "ResponseModel[Any]":
        """构造失败响应。request_id 同样会自动填充。"""
        return cls(
            code=int(code),
            message=message,
            data=data,
            request_id=request_id if request_id is not None else _current_request_id(),
        )


# =============================================================================
# 分页支持
# =============================================================================
class PageResult(BaseModel, Generic[T]):
    """分页数据结构：列表接口统一返回这个形状。"""

    total: int = Field(default=0, description="总记录数")
    page: int = Field(default=1, description="当前页码，从 1 开始")
    page_size: int = Field(default=10, description="每页条数")
    pages: int = Field(default=0, description="总页数")
    items: list[T] = Field(default_factory=list, description="当前页数据列表")

    @classmethod
    def create(cls, items: list[Any], total: int, page: int, page_size: int) -> "PageResult[Any]":
        """根据查询结果计算总页数并组装分页对象。"""
        pages = (total + page_size - 1) // page_size if page_size > 0 else 0
        return cls(total=total, page=page, page_size=page_size, pages=pages, items=items)


class PageQuery(BaseModel):
    """
    分页查询参数基类：其他查询参数模型继承它即可获得统一分页能力。
    约束页码与每页条数上限，防止恶意传入 page_size=999999 打挂数据库。
    """

    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    page_size: int = Field(default=10, ge=1, le=100, description="每页条数，最大 100")

    @property
    def offset(self) -> int:
        """转换为 SQL 的 OFFSET 值。"""
        return (self.page - 1) * self.page_size


# =============================================================================
# 分页响应模型的类型别名（给接口的 response_model 使用）
# =============================================================================
# 【为什么需要它？这里踩过一个很隐蔽的坑】
#
#   以前接口写成：
#       @router.get("/conversations", response_model=ResponseModel[dict])
#       def list_conversations(...):
#           return ResponseModel.success(data=PageResult.create(...))
#
#   看起来没问题，但 FastAPI 会【严格校验响应体是否符合声明的类型】。
#   声明的是 ResponseModel[dict]，即 data 必须是 dict；
#   而实际传入的是 PageResult（一个 Pydantic 模型，不是 dict），
#   于是 FastAPI 抛出：
#       ResponseValidationError: 1 validation error for ResponseModel[dict]
#       {'type': 'dict_type', 'loc': ('response', 'data'),
#        'msg': 'Input should be a valid dictionary',
#        'input': PageResult(total=0, ...)}
#   结果接口直接 500，而日志里只有一行 "请求异常"，很难定位。
#
#   所以分页接口应该用下面这个【类型精确】的别名，
#   让 data 明确声明为 PageResult[...]，而不是笼统的 dict。
#
#   用法：
#       @router.get("/conversations", response_model=PageResponseModel[ConversationOut])
#       def list_conversations(...):
#           return ResponseModel.success(data=PageResult.create(items=[...], ...))
#
#   也可以写不含元素类型的通用版本：
#       response_model=PageResponseModel[Any]
PageResponseModel = ResponseModel[PageResult[T]]
