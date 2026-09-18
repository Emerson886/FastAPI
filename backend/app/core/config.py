"""
==============================================================================
 全局配置模块（企业项目后端配置中心）
==============================================================================

【★ 需要你手动修改的地方，全部集中在 backend/.env 文件中 ★】

本文件负责把 .env 里的配置项读取成 Python 对象（settings），
全项目任何地方通过 `from app.core.config import settings` 使用。

设计要点（企业项目常用套路）：
1. 使用 pydantic-settings 做「类型安全」的配置校验：
   例如 DATABASE_URL 忘了填，服务启动时立刻报错，而不是运行到一半才崩。
2. 所有配置支持「环境变量覆盖」：环境变量 > .env 文件 > 代码默认值。
   这样同一份代码可以在 开发 / 测试 / 生产 环境用不同的 .env 部署。
3. 提供 derived properties（如 SQLALCHEMY_DATABASE_URI），避免在业务代码里
   拼接字符串，便于统一维护。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ----------------------------------------------------------------------------
# 路径计算：项目根目录（backend/）
# 说明：本文件位于 backend/app/core/config.py，向上三级即 backend/
#       BASE_DIR 用于定位 .env、storage 目录、日志目录等，保证「在任意工作目录启动都正确」。
# ----------------------------------------------------------------------------
BASE_DIR: Path = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """应用配置对象。字段名对应 .env 中的大写键名（大小写不敏感）。"""

    # ------------------------------------------------------------------
    # 1) 应用基础信息
    # ------------------------------------------------------------------
    PROJECT_NAME: str = Field(default="企业知识库问答助手", description="项目名称，展示在接口文档标题")
    VERSION: str = Field(default="1.0.0", description="版本号")
    API_V1_PREFIX: str = Field(default="/api/v1", description="v1 版本接口前缀")
    ENVIRONMENT: Literal["dev", "test", "prod"] = Field(default="dev", description="运行环境")
    DEBUG: bool = Field(default=True, description="调试模式：True 时自动建表、打印 SQL、开启热重载")

    # ------------------------------------------------------------------
    # 2) 服务器
    # ------------------------------------------------------------------
    HOST: str = Field(default="0.0.0.0")
    PORT: int = Field(default=8000)

    # ------------------------------------------------------------------
    # 3) ★★★ MySQL 数据库连接（需要你修改！） ★★★
    # ------------------------------------------------------------------
    # 拆成字段的好处：密码里有特殊字符（如 @ # /）时可以通过 quote_plus 自动转义
    MYSQL_HOST: str = Field(default="127.0.0.1", description="MySQL 主机")
    MYSQL_PORT: int = Field(default=3306, description="MySQL 端口")
    MYSQL_USER: str = Field(default="root", description="MySQL 用户名")
    MYSQL_PASSWORD: str = Field(default="root", description="root")
    MYSQL_DB: str = Field(default="kb_assistant", description="数据库名")
    MYSQL_CHARSET: str = Field(default="utf8mb4", description="字符集，中文/emoji 必须 utf8mb4")

    # 连接池配置（SQLAlchemy + PyMySQL 常用企业参数）
    DB_POOL_SIZE: int = Field(default=10, description="连接池常驻连接数")
    DB_MAX_OVERFLOW: int = Field(default=20, description="连接池用尽后允许临时创建的连接数")
    DB_POOL_RECYCLE: int = Field(default=3600, description="连接回收秒数，避免 MySQL 8 小时空闲断连")
    DB_ECHO: bool = Field(default=False, description="是否打印 SQL 语句（排查问题时设 True）")
    DB_AUTO_CREATE: bool = Field(default=True, description="启动时自动建表（生产建议用 Alembic 迁移）")

    # ------------------------------------------------------------------
    # 4) ★★★ 大模型配置（需要你修改！） ★★★
    # ------------------------------------------------------------------
    # 采用 OpenAI 兼容协议：DeepSeek / 通义 / Kimi / 智谱 / 本地 vLLM 都支持，
    # 换服务商只需要改下面三个值，代码零改动。
    LLM_BASE_URL: str = Field(
        default="https://api.deepseek.com/v1",
        description="★ 大模型服务地址（Keep the /v1 suffix）",
    )
    LLM_API_KEY: str = Field(default="sk-your_key", description="★ 大模型 API Key")
    LLM_MODEL: str = Field(default="deepseek-v4-flash", description="★ 对话模型名")
    LLM_TEMPERATURE: float = Field(default=0.3, ge=0.0, le=2.0, description="温度：知识库问答建议 0~0.4")
    LLM_MAX_TOKENS: int = Field(default=2048, description="单次回答最大 token 数")
    LLM_TIMEOUT: int = Field(default=120, description="大模型请求超时时间（秒）")
    LLM_MAX_RETRIES: int = Field(default=2, description="大模型请求失败重试次数")
    LLM_STREAMING: bool = Field(default=True, description="是否流式输出（打字机效果）")

    # ------------------------------------------------------------------
    # 5) ★★★ 向量库 / Embedding（需要你修改！） ★★★
    # ------------------------------------------------------------------
    # Embedding 也走 OpenAI 兼容接口，可以和对话模型不同厂商。
    EMBEDDING_BASE_URL: str = Field(
        default="https://api.siliconflow.cn/v1", description="★ Embedding 服务地址"
    )
    EMBEDDING_API_KEY: str = Field(default="sk-your_key", description="★ Embedding API Key")
    EMBEDDING_MODEL: str = Field(
        default="BAAI/bge-m3",
        description="★ Embedding 模型名",
    )
    EMBEDDING_DIMENSION: int = Field(default=1024, description="向量维度，需与模型一致")

    CHROMA_PERSIST_DIR: str = Field(
        default=str(BASE_DIR / "storage" / "chroma"), description="Chroma 向量库落盘目录"
    )
    RAG_COLLECTION: str = Field(default="enterprise_kb", description="Chroma 集合名")
    RAG_TOP_K: int = Field(default=4, description="检索返回的片段数量")
    RAG_CHUNK_SIZE: int = Field(default=600, description="文档切片长度（字符）")
    RAG_CHUNK_OVERLAP: int = Field(default=100, description="切片重叠长度，保持语义连贯")

    # ------------------------------------------------------------------
    # 6) ★★★ JWT 认证与安全（生产环境必须修改 SECRET_KEY） ★★★
    # ------------------------------------------------------------------
    SECRET_KEY: str = Field(
        default="random-key-from-your-RNG",
        description="★ JWT 签名密钥",
    )
    JWT_ALGORITHM: str = Field(default="HS256", description="JWT 签名算法")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=60 * 24, description="访问令牌有效期（分钟）")
    REFRESH_TOKEN_EXPIRE_MINUTES: int = Field(default=60 * 24 * 7, description="刷新令牌有效期（分钟）")

    # ------------------------------------------------------------------
    # 7) CORS 跨域
    # ------------------------------------------------------------------
    # 前端 Vite 默认跑在 5173 端口。多个地址用英文逗号分隔。
    BACKEND_CORS_ORIGINS: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        description="允许跨域访问的前端地址列表，逗号分隔",
    )

    # ------------------------------------------------------------------
    # 8) 文件上传
    # ------------------------------------------------------------------
    UPLOAD_DIR: str = Field(default=str(BASE_DIR / "storage" / "uploads"), description="上传文件保存目录")
    MAX_UPLOAD_SIZE_MB: int = Field(default=20, description="单文件最大体积（MB）")
    ALLOWED_UPLOAD_EXTENSIONS: str = Field(
        default=".txt,.md,.pdf,.docx,.csv", description="允许上传的文件后缀，逗号分隔"
    )

    # ------------------------------------------------------------------
    # 9) 日志
    # ------------------------------------------------------------------
    LOG_DIR: str = Field(default=str(BASE_DIR / "logs"), description="日志文件目录")
    LOG_LEVEL: str = Field(default="INFO", description="日志级别 DEBUG/INFO/WARNING/ERROR")
    LOG_RETENTION_DAYS: int = Field(default=14, description="日志保留天数")
    LOG_ENQUEUE: bool = Field(
        default=True,
        description=(
            "是否启用异步日志（loguru enqueue）。"
            "True=日志写入独立进程，性能更好、不会阻塞请求；"
            "False=同步写日志。"
            "⚠ 在受限环境（Windows 沙箱、部分容器、禁止命名管道的安全策略）下，"
            "loguru 的异步队列底层依赖 multiprocessing.SimpleQueue（命名管道），"
            "会抛 PermissionError: [WinError 5]，此时必须设为 False"
        ),
    )
    SQL_LOG_SLOW_MS: int = Field(default=500, description="超过该毫秒数记慢 SQL 日志")

    # ------------------------------------------------------------------
    # 10) 初始管理员账号（首次启动时自动创建，可用 scripts/init_db.py 重置）
    # ------------------------------------------------------------------
    FIRST_ADMIN_USERNAME: str = Field(default="admin", description="内置管理员用户名")
    FIRST_ADMIN_PASSWORD: str = Field(default="admin123456", description="★ 内置管理员密码，登录后请立即修改")
    FIRST_ADMIN_EMAIL: str = Field(default="admin@example.com")

    # ------------------------------------------------------------------
    # pydantic-settings 元配置
    # ------------------------------------------------------------------
    model_config = SettingsConfigDict(
        # ★ 你主要修改的文件就是它：backend/.env
        env_file=os.path.join(BASE_DIR, ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # .env 里有多余键名不报错，方便你加自定义变量
    )

    # ==================================================================
    # 校验器：把常见配置错误在启动阶段就拦住
    # ==================================================================
    @field_validator("BACKEND_CORS_ORIGINS", mode="after")
    @classmethod
    def _check_cors(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("BACKEND_CORS_ORIGINS 不能为空，至少配置前端地址")
        return v

    @field_validator("SECRET_KEY", mode="after")
    @classmethod
    def _warn_secret(cls, v: str) -> str:
        # 不直接抛错，避免影响你本地快速启动；只在生产环境严格校验
        return v

    @field_validator("LOG_DIR", "UPLOAD_DIR", "CHROMA_PERSIST_DIR", mode="after")
    @classmethod
    def _resolve_storage_path(cls, v: str, info) -> str:
        """
        规范化「存储类目录」配置，避免日志/文件跑到奇怪位置。

        【背景：这里踩过一个真实的坑】
            .env 里如果写成：
                LOG_DIR=                              # 留空则使用 backend/logs
            因为 dotenv 解析器【不会去掉行尾注释】，
            实际读到的值是字符串 "                              # 留空则使用 backend/logs"。
            结果是：日志被写进了一个名字很长的怪目录；
            而相对路径又会拼到「当前工作目录」下，
            最终日志出现在 backend/app/backend/logs 这种莫名其妙的位置。

        本方法做两件事：
            1. 空值 或 以 # 开头的值 → 回退到代码里声明的默认绝对路径
            2. 相对路径 → 以项目根目录（backend/）为基准转成绝对路径，
               这样无论用 python app/main.py 还是从项目根目录启动，
               日志与上传文件都落在同一个位置（可预期、可排查）
        """
        # 该字段在代码中声明的默认值（是绝对路径）
        default_value = str(cls.model_fields[info.field_name].default)

        value = (v or "").strip()

        # 2) 截断行尾注释：形如 "backend/logs   # 说明文字"
        if " #" in value:
            value = value.split(" #", 1)[0].strip()

        # 1) 空值 或 以 # 开头（说明 .env 里把注释当成了值）
        if not value or value.startswith("#"):
            return default_value

        # 3) 相对路径 → 以 backend/ 为基准转绝对路径
        path = Path(value)
        if not path.is_absolute():
            path = BASE_DIR / path
        return str(path)

    # ==================================================================
    # 派生属性（derived properties）
    # ==================================================================
    @property
    def SQLALCHEMY_DATABASE_URI(self) -> str:
        """
        组装 SQLAlchemy 数据库连接串。
        注意：密码用 quote_plus 转义，避免 password 含 @ : / 等字符时解析失败。
        """
        from urllib.parse import quote_plus

        pwd = quote_plus(self.MYSQL_PASSWORD)
        # quote_plus(self.MYSQL_PASSWORD) 的作用是：
        #
        # 对密码进行 URL 百分号编码；
        #
        # 把 @、:、/、?、# 等特殊字符转成 %XX；
        #
        # 把空格转成 +；
        #
        # 确保密码可以安全地嵌入数据库连接 URL 中，不会破坏 URL 结构。

        return (
            f"mysql+pymysql://{self.MYSQL_USER}:{pwd}@{self.MYSQL_HOST}:{self.MYSQL_PORT}"
            f"/{self.MYSQL_DB}?charset={self.MYSQL_CHARSET}"
        )

    @property
    def cors_origins_list(self) -> list[str]:
        """把逗号分隔的字符串转成列表，供 CORSMiddleware 使用。"""
        return [o.strip() for o in self.BACKEND_CORS_ORIGINS.split(",") if o.strip()]

    @property
    def allowed_extensions_list(self) -> list[str]:
        return [e.strip().lower() for e in self.ALLOWED_UPLOAD_EXTENSIONS.split(",") if e.strip()]

    @property
    def max_upload_size_bytes(self) -> int:
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024

    def is_prod(self) -> bool:
        return self.ENVIRONMENT == "prod"

    def masked_summary(self) -> dict[str, Any]:
        """
        打印配置摘要时使用：把 API Key / 密码打码，避免日志泄露敏感信息。
        企业项目里这条非常重要（日志脱敏要求）。
        """
        def mask(value: str, keep: int = 6) -> str:
            if not value:
                return ""
            return value[:keep] + "*" * max(len(value) - keep, 0)

        return {
            "环境": self.ENVIRONMENT,
            "数据库地址": f"{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DB}",
            "数据库用户": self.MYSQL_USER,
            "数据库密码": mask(self.MYSQL_PASSWORD, 2),
            "大模型服务": self.LLM_BASE_URL,
            "大模型名称": self.LLM_MODEL,
            "大模型Key": mask(self.LLM_API_KEY, 8),
            "向量库目录": self.CHROMA_PERSIST_DIR,
            "Embedding模型": self.EMBEDDING_MODEL,
            "JWT密钥": mask(self.SECRET_KEY, 4),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    单例读取配置：lru_cache 保证进程内只解析一次 .env。
    单元测试里如果改了环境变量，可调用 get_settings.cache_clear() 重新加载。
    """
    return Settings()


# 全局配置实例：项目内直接 `from app.core.config import settings`
settings = get_settings()
