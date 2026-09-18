"""
==============================================================================
 API v1 路由汇总
==============================================================================

把所有端点模块的路由注册到统一的路由器上，main.py 只需 include 一次。

新增模块的步骤：
    1. 在 app/api/v1/endpoints/ 下新建 xxx.py，定义 router = APIRouter(...)
    2. 在这里 import 并 api_router.include_router(xxx.router)
    3. 完成！接口自动出现在 /docs 文档里
"""

from fastapi import APIRouter

from app.api.v1.endpoints import auth, chat, documents, health

# 统一挂载到 /api/v1 前缀下（前缀在 main.py 里通过 include_router(prefix=...) 指定）
api_router = APIRouter()

# 认证与用户：/api/v1/auth/*
api_router.include_router(auth.router)

# 会话与问答：/api/v1/chat/*、/api/v1/conversations/*、/api/v1/messages/*
api_router.include_router(chat.router)

# 知识库文档：/api/v1/documents/*
api_router.include_router(documents.router)

# 健康检查：/api/v1/health/*
api_router.include_router(health.router)

__all__ = ["api_router"]
