"""
Fully read!!!
==============================================================================
 自定义异常体系 + 全局异常处理器
==============================================================================

企业项目为什么要自定义异常？
    1. 把「业务错误」（如密码错误、无权访问别人会话）和「程序 Bug」（如空指针）
       区分开：前者返回友好提示 + 明确业务码，后者返回 500 + 记录完整堆栈。
    2. 业务代码里可以直接 `raise BusinessException(...)` 中断流程，
       由全局处理器统一转成标准 JSON，不用在每个接口写 try/except。

本模块提供：
    - AppException            业务异常基类（带 code / message / http_status）
    - AuthException          认证类异常（401）
    - PermissionException    权限类异常（403）
    - NotFoundException      资源不存在（404）
    - BusinessException      通用业务错误（400）
    - ExternalServiceException 外部服务错误（502，如大模型不可用）
    - register_exception_handlers(app)  一次性注册所有处理器
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.response import BusinessCode, ResponseModel

logger = logging.getLogger(__name__)


# =============================================================================
# 1. 异常类定义
# =============================================================================
class AppException(Exception):
    """
    所有业务异常的基类。

    :param message:     给前端展示的提示信息
    :param code:        业务状态码（见 BusinessCode）
    :param http_status: 对应的 HTTP 状态码
    :param data:        附加数据，比如字段级错误详情
    """

    def __init__(
        self,
        message: str = "操作失败",
        code: BusinessCode | int = BusinessCode.BUSINESS_ERROR,
        http_status: int = status.HTTP_400_BAD_REQUEST,
        data: Any = None,
    ) -> None:
        self.message = message
        self.code = int(code)
        self.http_status = http_status
        self.data = data
        super().__init__(message)


class AuthException(AppException):
    """认证失败：未登录、token 无效、账号密码错误、账号禁用。"""

    def __init__(
        self,
        message: str = "未认证或登录已过期",
        code: BusinessCode | int = BusinessCode.UNAUTHORIZED,
    ) -> None:
        super().__init__(message=message, code=code, http_status=status.HTTP_401_UNAUTHORIZED)


class PermissionException(AppException):
    """权限不足：例如 A 用户尝试读取 B 用户的会话记录。"""

    def __init__(
        self,
        message: str = "无权访问该资源",
        code: BusinessCode | int = BusinessCode.FORBIDDEN,
    ) -> None:
        super().__init__(message=message, code=code, http_status=status.HTTP_403_FORBIDDEN)


class NotFoundException(AppException):
    """资源不存在。"""

    def __init__(
        self,
        message: str = "资源不存在",
        code: BusinessCode | int = BusinessCode.NOT_FOUND,
    ) -> None:
        super().__init__(message=message, code=code, http_status=status.HTTP_404_NOT_FOUND)


class BusinessException(AppException):
    """通用业务规则错误，如用户名已存在、文件类型不允许。"""

    def __init__(
        self,
        message: str = "业务处理失败",
        code: BusinessCode | int = BusinessCode.BUSINESS_ERROR,
    ) -> None:
        super().__init__(message=message, code=code, http_status=status.HTTP_400_BAD_REQUEST)


class ExternalServiceException(AppException):
    """外部依赖失败：大模型超时、向量库异常等。"""

    def __init__(
        self,
        message: str = "外部服务暂时不可用，请稍后重试",
        code: BusinessCode | int = BusinessCode.LLM_ERROR,
    ) -> None:
        super().__init__(message=message, code=code, http_status=status.HTTP_502_BAD_GATEWAY)


# =============================================================================
# 2. 工具：从 request.state 里取 request_id（由中间件写入）
# =============================================================================
def _get_request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


# =============================================================================
# 3. 异常处理器注册
# =============================================================================
def register_exception_handlers(app: FastAPI) -> None:
    """在 main.py 中调用，集中注册所有异常处理器。"""

    # ---------------------------------------------------------------
    # 3.1 业务异常：最常用的分支，返回结构化 JSON
    # ---------------------------------------------------------------
    @app.exception_handler(AppException)
    async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
        request_id = _get_request_id(request)
        # 4xx 属于「客户端问题」，用 warning 级别；5xx 才需要 error 级别告警
        log = logger.warning if exc.http_status < 500 else logger.error
        log(
            "业务异常 | %s %s | code=%s | %s | request_id=%s",
            request.method, request.url.path, exc.code, exc.message, request_id,
        )
        body = ResponseModel.fail(
            code=exc.code, message=exc.message, data=exc.data, request_id=request_id
        )
        return JSONResponse(status_code=exc.http_status, content=body.model_dump())

    # ---------------------------------------------------------------
    # 3.2 参数校验异常：把 Pydantic 的错误整理成人能看懂的提示
    # ---------------------------------------------------------------
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = _get_request_id(request)
        errors: list[dict[str, Any]] = []
        for err in exc.errors():
            # loc 形如 ("body", "username")，去掉第一段更直观
            loc = " -> ".join(str(x) for x in err.get("loc", [])[1:]) or "body"
            errors.append({"field": loc, "message": err.get("msg", ""), "type": err.get("type", "")})

        first = errors[0] if errors else {"field": "", "message": "参数错误"}
        message = f"参数校验失败：{first['field']} {first['message']}".strip()
        logger.warning("参数校验失败 | %s %s | %s | request_id=%s",
                       request.method, request.url.path, errors, request_id)

        body = ResponseModel.fail(
            code=BusinessCode.PARAM_ERROR,
            message=message,
            data={"errors": errors},
            request_id=request_id,
        )
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content=body.model_dump())

    # ---------------------------------------------------------------
    # 3.3 FastAPI/Starlette 内置 HTTPException（如 404 路由不存在）
    # ---------------------------------------------------------------
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        request_id = _get_request_id(request)
        # HTTP 状态码 → 业务码的粗略映射
        mapping = {
            401: BusinessCode.UNAUTHORIZED,
            403: BusinessCode.FORBIDDEN,
            404: BusinessCode.NOT_FOUND,
            429: BusinessCode.RATE_LIMITED,
        }
        code = mapping.get(exc.status_code, BusinessCode.BAD_REQUEST)
        logger.warning("HTTP异常 | %s %s | %s | %s | request_id=%s",
                       request.method, request.url.path, exc.status_code, exc.detail, request_id)
        body = ResponseModel.fail(
            code=code, message=str(exc.detail), request_id=request_id
        )
        return JSONResponse(status_code=exc.status_code, content=body.model_dump())

    # ---------------------------------------------------------------
    # 3.4 数据库完整性异常：唯一索引冲突等（如重复用户名）
    # ---------------------------------------------------------------
    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(
            request: Request, exc: IntegrityError
    ) -> JSONResponse:
        request_id = _get_request_id(request)
        logger.error("数据库完整性约束冲突 | %s | request_id=%s", str(exc.orig), request_id)
        body = ResponseModel.fail(
            code=BusinessCode.BUSINESS_ERROR,
            message="数据保存失败：存在重复或不合法的数据",
            request_id=request_id,
        )
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content=body.model_dump())

    # ---------------------------------------------------------------
    # 3.5 数据库通用异常
    # ---------------------------------------------------------------
    @app.exception_handler(SQLAlchemyError)
    async def sqlalchemy_error_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        request_id = _get_request_id(request)
        logger.exception("数据库异常 | %s | %s %s | request_id=%s", str(exc.orig), request.method, request.url.path, request_id)
        body = ResponseModel.fail(
            code=BusinessCode.INTERNAL_ERROR,
            message="数据库操作异常，请稍后重试",
            request_id=request_id,
        )
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content=body.model_dump())

    # ---------------------------------------------------------------
    # 3.6 兜底：任何未捕获异常，都要记录完整堆栈（logger.exception）
    #     并且【绝对不能】把堆栈信息返回给前端（安全要求）
    # ---------------------------------------------------------------
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = _get_request_id(request)
        logger.exception(
            "未捕获异常 | %s | %s %s | request_id=%s", exc.args, request.method, request.url.path, request_id
        )
        body = ResponseModel.fail(
            code=BusinessCode.INTERNAL_ERROR,
            message="服务器内部错误，请联系管理员并提供 request_id",
            request_id=request_id,
        )
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content=body.model_dump())
