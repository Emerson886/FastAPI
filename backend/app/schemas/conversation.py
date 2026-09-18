"""
==============================================================================
 Pydantic Schema：会话（Conversation）
==============================================================================

对应前端左侧「历史会话列表」。
输出结构 ConversationOut 里带了 message_count / last_message_at，
可以让前端直接展示「12 条消息 · 昨天」这样的信息，无需再发一次请求统计。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConversationBase(BaseModel):
    """会话公共字段。"""

    title: str = Field(default="新对话", max_length=200, description="会话标题")
    model_name: str | None = Field(default=None, max_length=100, description="该会话使用的模型")
    use_rag: bool = Field(default=True, description="是否启用知识库检索")


class ConversationCreate(BaseModel):
    """新建会话请求体。全部字段可选，不传就用系统默认。"""

    title: str | None = Field(default=None, max_length=200, description="会话标题，不传则用『新对话』")
    model_name: str | None = Field(default=None, max_length=100)
    use_rag: bool = Field(default=True)
    # 可选：允许「带着第一条问题一起创建会话」，省一次请求
    first_question: str | None = Field(
        default=None, description="可选：首条提问。传了则自动生成标题并直接回答"
    )


class ConversationUpdate(BaseModel):
    """更新会话（改名 / 开关 RAG / 置顶）。"""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    use_rag: bool | None = None
    is_pinned: bool | None = None

    @field_validator("title", mode="after")
    @classmethod
    def _strip_title(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("标题不能为空")
        return v


class ConversationOut(BaseModel):
    """会话输出结构。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    summary: str | None = None
    message_count: int = 0
    last_message_at: datetime | None = None
    model_name: str | None = None
    use_rag: bool = True
    is_pinned: bool = False
    created_at: datetime
    updated_at: datetime


class ConversationDetailOut(ConversationOut):
    """
    会话详情：额外带上消息列表。
    前端点开某个历史会话时，一次请求就能拿到「会话信息 + 全部消息」。
    """

    # 使用字符串前向引用避免循环导入（MessageOut 在 message.py 中定义）
    messages: list["MessageOut"] = Field(default_factory=list, description="消息列表（时间正序）")


# 解决前向引用：Pydantic v2 需要显式重建模型
from app.schemas.message import MessageOut  # noqa: E402

ConversationDetailOut.model_rebuild()
