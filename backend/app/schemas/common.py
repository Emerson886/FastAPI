"""
==============================================================================
 Pydantic Schema：通用（分页请求 / 健康检查 / 审计日志）
==============================================================================
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PaginationQuery(BaseModel):
    """
    通用分页查询参数。

    用法（在接口里作为 Depends 注入）：
        def list_users(pagination: PaginationQuery = Depends()):
            ...
    """

    page: int = Field(default=1, ge=1, le=10000, description="页码，从 1 开始")
    page_size: int = Field(default=20, ge=1, le=100, description="每页条数，最大 100")
    keyword: str | None = Field(default=None, description="关键字搜索")

    @property
    def skip(self) -> int:
        """SQL OFFSET。"""
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        """SQL LIMIT。"""
        return self.page_size


class HealthOut(BaseModel):
    """健康检查返回体（给运维监控系统用）。"""

    status: str = Field(description="状态：ok / degraded / error")
    app_name: str
    version: str
    environment: str
    database: dict[str, Any] = Field(default_factory=dict, description="数据库连接信息")
    uptime_seconds: float = Field(default=0, description="服务已运行秒数")
    timestamp: datetime = Field(default_factory=datetime.now)


class AuditLogOut(BaseModel):
    """审计日志输出（仅管理员可见）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int | None = None
    username: str | None = None
    action: str
    resource: str | None = None
    resource_id: str | None = None
    detail: dict[str, Any] | None = None
    ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None
    method: str | None = None
    path: str | None = None
    status_code: int | None = None
    success: bool = True
    message: str | None = None
    created_at: datetime


class MessageOutSimple(BaseModel):
    """通用简单消息返回（如删除成功）。"""

    message: str = "操作成功"
    detail: str | None = None
