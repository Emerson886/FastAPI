"""
==============================================================================
 API 端点：健康检查（health）
==============================================================================

生产环境必备：给 K8s / Nginx / 监控系统（Prometheus、Uptime Kuma）探活用。

接口清单：
    GET /health           基础健康检查（轻量，不查外部依赖，用于高频探活）
    GET /health/ready     就绪检查（检查 MySQL 连通性，用于 K8s readinessProbe）
    GET /health/detail    详细检查（含大模型、向量库信息，人工排查用）

【为什么基础探活不查数据库？】
    高频探活（每 5 秒一次）如果每次都查数据库，会给数据库带来无谓压力。
    轻量探活只回答「进程还活着吗」，这才符合探活的本意。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Query

from app.core.config import settings
from app.core.response import ResponseModel
from app.db.session import check_database_connection, get_database_info

router = APIRouter(tags=["健康检查"])

# 记录服务启动时间，用于计算 uptime
_START_TIME = time.time()


@router.get("/health", summary="基础健康检查", description="轻量探活，不访问外部依赖。")
def health():
    return ResponseModel.success(
        data={
            "status": "ok",
            "app_name": settings.PROJECT_NAME,
            "version": settings.VERSION,
            "environment": settings.ENVIRONMENT,
            "uptime_seconds": round(time.time() - _START_TIME, 2),
        },
        message="服务运行正常",
    )


@router.get(
    "/health/ready",
    summary="就绪检查",
    description="检查数据库连通性。K8s readinessProbe 用这个接口，未就绪时不接收流量。",
)
def readiness():
    db_ok = check_database_connection()
    return ResponseModel.success(
        data={
            "status": "ready" if db_ok else "degraded",
            "database": "connected" if db_ok else "disconnected",
        },
        message="服务就绪" if db_ok else "数据库不可用",
    )


@router.get(
    "/health/detail",
    summary="详细健康检查",
    description="""
返回数据库、向量库、大模型配置的详细状态。

`deep=true` 时会真实调用一次大模型做连通性测试（会产生极小费用），默认关闭。
    """,
)
def health_detail(deep: bool = Query(default=False, description="是否深度检测大模型连通性")):
    db_info = get_database_info()

    # ---- 向量库信息（只读本地状态，不消耗 API 额度）----
    try:
        from app.services.rag.vector_store import get_collection_stats

        vector_info = get_collection_stats()
    except Exception as exc:  # pragma: no cover
        vector_info = {"error": str(exc)}

    result: dict = {
        "status": "ok",
        "app_name": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "environment": settings.ENVIRONMENT,
        "uptime_seconds": round(time.time() - _START_TIME, 2),
        "database": db_info,
        "vector_store": vector_info,
        "llm_config": {
            "base_url": settings.LLM_BASE_URL,
            "model": settings.LLM_MODEL,
            "embedding_model": settings.EMBEDDING_MODEL,
        },
    }

    # ---- 深度检测：真实请求一次大模型 ----
    if deep:
        from app.services.llm import check_llm_available

        result["llm_check"] = check_llm_available()
        if not result["llm_check"].get("available"):
            result["status"] = "degraded"

    return ResponseModel.success(data=result)
