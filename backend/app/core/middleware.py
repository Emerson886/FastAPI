"""
Fully read!!!
==============================================================================
 中间件（Middleware）配置
==============================================================================

中间件 = 请求进入路由前 / 响应返回前端前，统一做一层处理。
本文件提供 4 个企业项目常用中间件：

    1. RequestContextMiddleware  为每个请求生成 request_id，写入日志上下文与响应头
    2. AccessLogMiddleware       记录每个请求的方法/路径/状态码/耗时（性能监控基础）
    3. CORSMiddleware            FastAPI 内置，解决前后端分离的跨域问题
    4. GZipMiddleware            FastAPI 内置，压缩响应体，节省带宽

【执行顺序很重要】
    add_middleware 是「后添加的先执行（最外层）」，请求方向：后添加 → 先添加 → 路由。
    所以我们希望 request_id 最先被创建，所以它要最后注册（最外层）。
"""

from __future__ import annotations

import time
import uuid

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.core.config import settings
from app.core.logging_config import logger
from app.core.request_context import reset_request_id, set_request_id

# 不需要记录访问日志的路径（健康检查、文档、静态资源），避免日志刷屏
SKIP_LOG_PATHS: set[str] = {
    "/health",
    "/favicon.ico",
    "/openapi.json",
    "/docs",
    "/redoc",
}


class RequestContextMiddleware(BaseHTTPMiddleware):
    """
    请求上下文中间件：为每个请求生成唯一 request_id。

    request_id 的四大用途：
        1. 写入日志：一次请求产生的所有日志都带同一个 ID，方便串联
        2. 写入响应头 X-Request-ID：前端报错时可以把 ID 报给后端排查
        3. 写入 request.state：异常处理器里可以取出来
        4. 【写入 contextvars】让 ResponseModel 能自动填充 request_id 字段
           （接口函数是同步的，拿不到 Request 对象，只能靠上下文变量传递）
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # 优先复用上游（网关/Nginx）传来的 request_id，实现全链路追踪
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        # get不到就返回不带连字符"-"的 32 位十六进制字符串。

        request.state.request_id = request_id
        # state 是一个用于在请求上下文中传递数据的对象。可以在中间件中设置值，然后在路径操作函数中读取

        # 写入 contextvars：ResponseModel 会自动读取它填进响应体，
        # 保证「响应体 request_id」== 「响应头 X-Request-ID」== 「日志 request_id」
        token = set_request_id(request_id)

        try:
            # logger.contextualize 让这个请求内所有日志自动带上 request_id
            with logger.contextualize(request_id=request_id):
                response = await call_next(request)
        finally:
            # 请求结束必须还原上下文变量，防止在连接复用/任务复用场景下串号
            reset_request_id(token)

        response.headers["X-Request-ID"] = request_id
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    """
    访问日志中间件：记录「谁、什么时候、访问了什么、结果如何、耗时多少」。

    这是排查线上问题的第一手资料，也是做接口性能分析的数据来源。
    耗时超过 settings.SQL_LOG_SLOW_MS 的请求会以 WARNING 级别记录，便于告警。
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # 跳过无需记录的路径
        if request.url.path in SKIP_LOG_PATHS:
            return await call_next(request)

        start = time.perf_counter() # 记录开始
        request_id = getattr(request.state, "request_id", "-")

        # 取客户端 IP，兼容反向代理场景（X-Forwarded-For 第一个是最初的客户端）
        forwarded = request.headers.get("X-Forwarded-For")
        client_ip = forwarded.split(",")[0].strip() if forwarded else (
            request.client.host if request.client else "unknown"
        )

        with logger.contextualize(request_id=request_id):
            try:
                response = await call_next(request)
            except Exception:
                # 未捕获异常交给全局异常处理器，这里只负责记录耗时
                cost_ms = (time.perf_counter() - start) * 1000
                logger.exception(
                    "请求异常 | {} {} | ip={} | 耗时={:.1f}ms",
                    request.method, request.url.path, client_ip, cost_ms,
                )
                raise

            cost_ms = (time.perf_counter() - start) * 1000
            log_kwargs = (
                request.method,
                request.url.path,
                response.status_code,
                client_ip,
                cost_ms,
            )
            if cost_ms >= settings.SQL_LOG_SLOW_MS:
                logger.warning("慢请求 | {} {} | 状态={} | ip={} | 耗时={:.1f}ms", *log_kwargs)
            elif response.status_code >= 500:
                logger.error("服务端错误 | {} {} | 状态={} | ip={} | 耗时={:.1f}ms", *log_kwargs)
            else:
                logger.info("请求完成 | {} {} | 状态={} | ip={} | 耗时={:.1f}ms", *log_kwargs)

            # 性能指标回写响应头，前端/浏览器调试面板可见
            response.headers["X-Process-Time-Ms"] = f"{cost_ms:.1f}"
            return response


def register_middlewares(app: FastAPI) -> None:
    """
    统一注册中间件。在 main.py 中调用。
    注册顺序：先注册的在「内层」（靠近路由）。
    """

    # ---------------------------------------------------------------
    # 1) GZip 压缩：响应体大于 1KB 才压缩，节省带宽（内层）
    # ---------------------------------------------------------------
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # ---------------------------------------------------------------
    # 2) 访问日志（次内层）
    # ---------------------------------------------------------------
    app.add_middleware(AccessLogMiddleware)

    # ---------------------------------------------------------------
    # 3) CORS 跨域
    #    前后端分离部署时，浏览器会因为「同源策略」拦截请求，
    #    必须由后端明确声明「允许哪些源访问」。
    #    allow_origins 在 .env 的 BACKEND_CORS_ORIGINS 里配置。
    #    安全提醒：allow_credentials=True 时，allow_origins 不能是 ["*"]，
    #            必须写具体域名（这是一个常见的安全漏洞）。
    # ---------------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Process-Time-Ms"],
        max_age=600,  # 预检请求缓存 10 分钟，减少 OPTIONS 请求
    )

    # ---------------------------------------------------------------
    # 4) 请求上下文（最外层，保证 request_id 最早生成、最晚销毁）
    # ---------------------------------------------------------------
    app.add_middleware(RequestContextMiddleware)
