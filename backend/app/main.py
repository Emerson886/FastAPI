"""
Fully read!!!
==============================================================================
 FastAPI 应用入口（main.py）
==============================================================================

启动方式（在 backend 目录下执行）：
    # 方式一：uvicorn 命令（推荐开发用，支持热重载）
    C:/Users/thunderfish/miniconda3/envs/langchain/python.exe -m uvicorn app.main:app --reload --port 8000

    # 方式二：直接运行本文件（python app/main.py）
    C:/Users/thunderfish/miniconda3/envs/langchain/python.exe app/main.py

【★ 运行前必做：配置 backend/.env ★】
    参考 backend/.env.example，至少要改：
        MYSQL_PASSWORD   —— 你的 MySQL 密码
        MYSQL_DB         —— 数据库名（需先建库）
        LLM_API_KEY      —— 大模型 API Key
        EMBEDDING_API_KEY—— Embedding 服务 Key

访问地址：
    接口文档（Swagger）  http://127.0.0.1:8000/docs
    接口文档（ReDoc）    http://127.0.0.1:8000/redoc
    健康检查            http://127.0.0.1:8000/health

【应用启动流程（lifespan）】
    1. 初始化日志系统
    2. 打印配置摘要（敏感信息已打码）
    3. 检查数据库连接
    4. 自动建表 + 创建初始管理员（当 DB_AUTO_CREATE=True）
    5. 预热 Embedding / 向量库（可选，减少首次请求延迟）
    ↓ 应用运行中
    6. 关闭时释放数据库连接池
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

# ----------------------------------------------------------------------------
# 把 backend 目录加入 sys.path，保证 `python app/main.py` 也能正确导入 app 包
# ----------------------------------------------------------------------------
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from app.api.v1.router import api_router  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.exceptions import register_exception_handlers  # noqa: E402
from app.core.logging_config import logger, setup_logging  # noqa: E402
from app.core.middleware import register_middlewares  # noqa: E402
from app.core.response import ResponseModel  # noqa: E402
from app.db.session import check_database_connection, engine  # noqa: E402


# =============================================================================
# 应用生命周期管理（FastAPI 官方推荐的 lifespan 写法，替代旧的 on_event）
# =============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    yield 之前 = 应用启动时执行
    yield 之后 = 应用关闭时执行
    """
    # ------------------------------------------------------------------
    # 启动阶段
    # ------------------------------------------------------------------
    setup_logging()  # 1. 初始化日志（必须最先执行，后面的日志才有格式）

    logger.info("=" * 78)
    logger.info("  {} v{} 正在启动...", settings.PROJECT_NAME, settings.VERSION)
    logger.info("=" * 78)

    # 2. 打印配置摘要（敏感字段已打码，防止日志泄露密钥）
    for key, value in settings.masked_summary().items():
        logger.info("  配置 | {:<12} : {}", key, value)
        # {} 是占位符，按顺序对应后面的 key 和 value。
        #
        # {:<12} 专门用于第一个参数 key，意思是：
        #
        # <：左对齐
        #
        # 12：最小宽度为 12 个字符
        #
        # 不足 12 个字符时，右边用空格补齐；超过 12 个字符时，原样输出，不会截断。

    # 3. 检查数据库连接（提前失败，比运行中才发现连不上数据库友好得多）
    if check_database_connection():
        logger.info("  ✓ MySQL 连接成功 | {}:{}/{}",
                    settings.MYSQL_HOST, settings.MYSQL_PORT, settings.MYSQL_DB)
    else:
        logger.error(
            "  ✗ MySQL 连接失败！请检查 backend/.env 中的 MYSQL_* 配置，"
            "并确认数据库已创建（CREATE DATABASE {} DEFAULT CHARACTER SET utf8mb4;）",
            settings.MYSQL_DB,
        )

    # 4. 建表 + 初始数据
    if settings.DB_AUTO_CREATE:
        try:
            from app.db.init_db import init_all

            init_all()
        except Exception as exc:
            logger.error("初始化数据库失败：{}", exc)
            logger.error("服务仍会启动，但涉及数据库的接口将不可用。请修正 .env 后重启。")

    # 5. 预热 Embedding 与向量库（首次请求能快 1-2 秒）
    #    注意：这里不做真实的 Embedding 调用（会消耗额度），只初始化对象
    try:
        from app.services.rag.vector_store import get_vector_store

        get_vector_store()
        logger.info("  ✓ 向量库就绪 | 目录={}", settings.CHROMA_PERSIST_DIR)
    except Exception as exc:
        logger.warning("向量库初始化失败（知识库功能将不可用）：{}", exc)

    logger.info("=" * 78)
    logger.info("  服务已启动 | 接口文档: http://127.0.0.1:{}/docs", settings.PORT)
    logger.info("  健康检查:   http://127.0.0.1:{}/health", settings.PORT)
    logger.info("=" * 78)

    yield  # ------------------- 应用运行中 -------------------

    # ------------------------------------------------------------------
    # 关闭阶段：释放资源
    # ------------------------------------------------------------------
    logger.info("正在关闭 {} ...", settings.PROJECT_NAME)
    engine.dispose()  # 关闭数据库连接池，防止连接泄漏
    logger.info("数据库连接池已释放，服务已安全退出")


# =============================================================================
# 创建 FastAPI 应用实例
# =============================================================================
app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description=f"""
## {settings.PROJECT_NAME} · 后端 API

基于 **FastAPI + SQLAlchemy + LangChain** 的企业知识库问答助手后端服务。

### 核心能力
- 🔐 **用户认证**：JWT 双令牌（access + refresh），bcrypt 密码哈希
- 💬 **智能问答**：LangChain Agent + ReAct 推理，支持多轮对话记忆
- 📚 **知识库**：文档上传 → 解析 → 切片 → 向量化 → 语义检索（RAG）
- 🔒 **数据隔离**：每个用户只能访问自己的会话记录，越权返回 404
- 📊 **可观测**：request_id 链路追踪、审计日志、token 用量统计

### 认证方式
1. 调用 `POST /api/v1/auth/login` 获取 `access_token`
2. 后续请求在请求头携带：`Authorization: Bearer <access_token>`
3. token 过期（默认 1 天）时，用 `POST /api/v1/auth/refresh` 换取新令牌

### 统一响应格式
```json
{{
  "code": 0,
  "message": "success",
  "data": {{}},
  "request_id": "8f3c...",
  "timestamp": 1730000000
}}
```
`code = 0` 表示成功，非 0 为业务错误码（详见 `app/core/response.py` 的 BusinessCode）。
    """,
    lifespan=lifespan,
    docs_url="/docs",       # Swagger UI 地址
    redoc_url="/redoc",     # ReDoc 地址
    openapi_url="/openapi.json",
    # Swagger 文档里按业务模块分组展示时使用的标签说明
    # （标签名与各 endpoint 的 tags 参数一致；这里的 description 只影响文档展示）
    openapi_tags=[
        {"name": "认证与用户", "description": "注册、登录、令牌刷新、个人资料与密码管理"},
        {"name": "会话与问答", "description": "**核心模块**：流式问答（SSE）、会话历史记录管理"},
        {"name": "知识库文档", "description": "文档上传、解析、向量化、检索测试与权限管理"},
        {"name": "健康检查", "description": "服务探活与依赖状态检查，供运维监控使用"},
        {"name": "根路径", "description": "服务基本信息"},
    ],
    # 仅开发环境开启交互式文档；生产环境建议关闭或加访问限制
    debug=settings.DEBUG,
)

# =============================================================================
# 注册中间件（顺序很重要，见 middleware.py 注释）
# =============================================================================
register_middlewares(app)

# =============================================================================
# 注册全局异常处理器（统一错误响应格式）
# =============================================================================
register_exception_handlers(app)

# =============================================================================
# 注册路由
# =============================================================================
# 业务接口统一挂在 /api/v1 下
app.include_router(api_router, prefix=settings.API_V1_PREFIX)


# =============================================================================
# 根路径与健康检查（不带 /api/v1 前缀，方便运维直接访问）
# =============================================================================
@app.get("/", tags=["根路径"], summary="服务信息")
def root():
    """访问根路径时返回服务基本信息与文档地址。"""
    return {
        "name": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "environment": settings.ENVIRONMENT,
        "docs": "/docs",
        "api_prefix": settings.API_V1_PREFIX,
        "health": "/health",
    }


@app.get("/health", tags=["健康检查"], summary="健康检查（供运维监控使用）")
def health_check():
    """最常用的探活接口，不访问数据库，响应极快。"""
    db_ok = check_database_connection()
    return JSONResponse(
        status_code=200 if db_ok else 503,
        content=ResponseModel.success(
            data={
                "status": "ok" if db_ok else "degraded",
                "app_name": settings.PROJECT_NAME,
                "version": settings.VERSION,
                "database": "connected" if db_ok else "disconnected",
            }
        ).model_dump(),
    )


# =============================================================================
# 本地直接运行入口
# =============================================================================
if __name__ == "__main__":
    import uvicorn

    # 注意：reload=True 时不能传 app 对象（必须传 import 字符串），
    #      因为热重载需要重新导入模块。
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_config=None,   # 使用我们自己的 loguru 配置，禁用 uvicorn 默认日志
        access_log=False,  # 访问日志由 AccessLogMiddleware 统一记录
    )
