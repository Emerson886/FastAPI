"""
==============================================================================
 Pydantic Schema：消息（Message）
==============================================================================

前端聊天界面主要用到两个：
    - ChatRequest   ：用户提问的请求体
    - MessageOut    ：消息输出（含 RAG 引用来源 citations）

流式输出（SSE）的事件结构由 ChatStreamChunk 描述，见本文件末尾。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 允许的消息角色
MessageRole = Literal["user", "assistant", "system"]
# 消息状态
MessageStatus = Literal["success", "failed", "streaming"]


class Citation(BaseModel):
    """
    RAG 引用来源。

    前端会把它们渲染成回答下方的「参考来源」卡片，
    点击可以查看原文片段，这是企业知识库问答取信于用户的关键设计。
    """

    doc_id: int | None = Field(default=None, description="文档 ID")
    filename: str | None = Field(default=None, description="文档文件名")
    title: str | None = Field(default=None, description="文档标题")
    chunk_index: int | None = Field(default=None, description="切片序号")
    content: str | None = Field(default=None, description="命中的原文片段")
    score: float | None = Field(default=None, description="相似度得分，越大越相关")


class MessageBase(BaseModel):
    """消息公共字段。"""

    role: MessageRole = Field(default="user", description="角色：user/assistant/system")
    content: str = Field(..., min_length=1, description="消息内容")


class MessageCreate(MessageBase):
    """内部使用的消息创建模型（一般由服务层调用，不直接暴露给前端）。"""

    conversation_id: int
    user_id: int
    citations: list[Citation] | None = None
    model_name: str | None = None
    status: MessageStatus = "success"


class MessageUpdate(BaseModel):
    """消息更新（目前主要用于编辑提问）。"""

    content: str | None = Field(default=None, min_length=1)


class MessageOut(BaseModel):
    """消息输出结构。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    role: str
    content: str
    citations: list[Citation] | None = None
    model_name: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    status: str = "success"
    created_at: datetime


# ============================================================================
# 问答请求
# ============================================================================
class ChatRequest(BaseModel):
    """
    提问请求体。

    前端把当前问题、目标会话、是否用知识库一起传过来。
    """

    question: str = Field(..., min_length=1, max_length=4000, description="用户提问内容")
    conversation_id: int | None = Field(
        default=None,
        description="会话 ID。为空则自动新建会话（服务端会返回新会话信息）",
    )
    use_rag: bool | None = Field(
        default=None, description="是否启用知识库检索，为空则沿用会话配置"
    )
    # 进阶功能：让用户临时指定「只在这几篇文档里检索」
    document_ids: list[int] | None = Field(
        default=None, description="限定检索的文档 ID 列表（可选）"
    )
    # 进阶功能：让用户直接指定模型（需在允许列表内）
    model_name: str | None = Field(default=None, description="指定使用的大模型")

    @field_validator("question", mode="after")
    @classmethod
    def _strip_question(cls, v: str) -> str:
        """去掉首尾空白；纯空白视为非法输入。"""
        v = v.strip()
        if not v:
            raise ValueError("提问内容不能为空")
        return v


# ============================================================================
# 流式输出（SSE）事件结构
# ============================================================================
class ChatStreamChunk(BaseModel):
    """
    SSE 流式输出的单个事件体。

    事件类型（type）：
        start      —— 流开始，携带 conversation_id / user_message_id / assistant_message_id
        citations  —— 检索到的知识来源（在正式回答前推送，前端可先显示"正在参考..."）
        delta      —— 增量文本（前端把它拼接到气泡里，形成打字机效果）
        done       —— 结束，携带完整回答、耗时、token 用量
        error      —— 出错，携带错误信息
    """

    type: Literal["start", "citations", "delta", "done", "error"]
    # 增量文本
    content: str | None = None
    # 会话与消息 ID
    conversation_id: int | None = None
    user_message_id: int | None = None
    assistant_message_id: int | None = None
    # 引用来源
    citations: list[Citation] | None = None
    # 结束时的统计信息
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    model_name: str | None = None
    # 错误信息
    error: str | None = None
    extra: dict[str, Any] | None = None


# ============================================================================
# 历史提问搜索结果
# ============================================================================
class MessageSearchItemOut(BaseModel):
    """
    搜索结果里的单条消息。

    这是 MessageOut 的精简版（只保留前端渲染搜索结果需要的字段），
    单独定义而不是复用 MessageOut，好处是：
        1. 响应体积更小（不返回 citations / token 统计等无用字段）
        2. 结果列表不需要展示引用来源，避免前端多余渲染
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    role: str
    content: str
    created_at: datetime


class MessageSearchOut(BaseModel):
    """
    历史提问搜索接口的完整返回结构。

    结构 = 分页结构（total/page/page_size/pages/items）+ 本次搜索关键字。
    带上 keyword 是为了让前端在渲染「搜索：xxx（共 N 条）」时不用自己回填。
    """

    keyword: str = Field(..., description="本次搜索的关键字")
    total: int = Field(default=0, description="匹配到的消息总数")
    page: int = Field(default=1, description="当前页码")
    page_size: int = Field(default=20, description="每页条数")
    pages: int = Field(default=0, description="总页数")
    items: list[MessageSearchItemOut] = Field(default_factory=list, description="消息列表")
