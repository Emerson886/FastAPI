"""
==============================================================================
 API v1 端点：会话与问答（chat / conversations）
==============================================================================

接口清单：
    POST   /api/v1/chat/stream              流式问答（SSE，打字机效果）★核心接口
    POST   /api/v1/chat/completions         非流式问答（一次性返回）
    GET    /api/v1/conversations            我的会话列表（分页 + 搜索）
    POST   /api/v1/conversations            新建会话
    GET    /api/v1/conversations/{id}       会话详情（含全部消息）
    PUT    /api/v1/conversations/{id}       重命名 / 置顶 / 切换 RAG
    DELETE /api/v1/conversations/{id}       删除会话（软删除）
    DELETE /api/v1/conversations             清空我的全部会话
    GET    /api/v1/conversations/{id}/messages   分页拉取某会话消息（聊天记录分页加载）
    GET    /api/v1/messages/search          搜索我的历史提问

【数据隔离】
    所有会话相关接口都使用 Depends(get_owned_conversation)，
    它内部强制校验 user_id，A 用户无法访问 B 用户的会话。
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from app.api.deps import (
    CurrentUser,
    DbSession,
    get_client_ip,
    get_owned_conversation,
    get_request_id,
)
from app.core.logging_config import logger
from app.core.response import PageResponseModel, PageResult, ResponseModel
from app.crud import crud_audit_log, crud_conversation, crud_message
from app.models.conversation import Conversation
from app.schemas.common import MessageOutSimple, PaginationQuery
from app.schemas.conversation import (
    ConversationCreate,
    ConversationDetailOut,
    ConversationOut,
    ConversationUpdate,
)
from app.schemas.message import ChatRequest, MessageOut, MessageSearchItemOut, MessageSearchOut
from app.services import chat_service

router = APIRouter(tags=["会话与问答"])


# =============================================================================
# 1. 【核心接口】流式问答（SSE）
# =============================================================================
@router.post(
    "/chat/stream",
    summary="流式问答（SSE）★核心接口",
    description="""
基于 LangChain Agent 的知识库问答，采用 SSE 流式返回，前端呈打字机效果。

**响应格式**：`text/event-stream`，每帧形如：
```
event: delta
data: {"type":"delta","content":"公司"}

```

**事件类型**：
| event | 说明 |
|-------|------|
| `start` | 流开始，返回 conversation_id（新建会话时前端用它刷新侧边栏） |
| `citations` | 检索到的知识来源列表（可能推送多次） |
| `delta` | 增量文本（前端拼接成完整回答） |
| `done` | 结束，返回完整回答、耗时、token 用量、落库后的消息 ID |
| `error` | 出错，返回友好错误提示 |

**前端调用提示**：因为要带 Authorization 头，不能用 `EventSource`，
需使用 `fetch` + `ReadableStream` 手动解析 SSE 帧（见前端 `src/api/chat.js`）。
    """,
    response_class=StreamingResponse,
)
async def chat_stream(
    payload: ChatRequest,
    db: DbSession,
    current_user: CurrentUser,
    request: Request,
    background_tasks: BackgroundTasks,
    request_id: str = Depends(get_request_id),
):
    """
    流式问答接口。

    返回 StreamingResponse，媒体类型为 text/event-stream。
    """
    generator = chat_service.chat_stream(
        db=db,
        user=current_user,
        question=payload.question,
        conversation_id=payload.conversation_id,
        use_rag=payload.use_rag,
        model_name=payload.model_name,
        document_ids=payload.document_ids,
        request_id=request_id,
    )

    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            # ---- SSE 必备响应头 ----
            "Cache-Control": "no-cache, no-transform",  # 禁止缓存，否则看不到流式效果
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # ★ Nginx 下必须：关闭缓冲，否则会攒满才返回
            "X-Request-ID": request_id,
        },
    )


# =============================================================================
# 2. 非流式问答
# =============================================================================
@router.post(
    "/chat/completions",
    response_model=ResponseModel[dict],
    summary="非流式问答",
    description="一次性返回完整回答（适合不方便处理 SSE 的客户端，或接口联调时使用）。",
)
async def chat_completions(
    payload: ChatRequest,
    db: DbSession,
    current_user: CurrentUser,
):
    result = await chat_service.chat_once(
        db=db,
        user=current_user,
        question=payload.question,
        conversation_id=payload.conversation_id,
        use_rag=payload.use_rag,
        model_name=payload.model_name,
        document_ids=payload.document_ids,
    )
    return ResponseModel.success(data=result, message="回答生成完成")


# =============================================================================
# 3. 会话管理（增删改查）
# =============================================================================
@router.get(
    "/conversations",
    response_model=PageResponseModel[ConversationOut],
    summary="我的会话列表",
    description="返回当前登录用户的会话列表（置顶优先、按最后消息时间倒序）。**只能看到自己的会话。**",
)
def list_conversations(
    db: DbSession,
    current_user: CurrentUser,
    pagination: PaginationQuery = Depends(),
):
    conversations, total = crud_conversation.list_by_user(
        db,
        user_id=current_user.id,
        keyword=pagination.keyword,
        skip=pagination.skip,
        limit=pagination.limit,
    )
    return ResponseModel.success(
        data=PageResult.create(
            items=[ConversationOut.model_validate(c) for c in conversations],
            total=total,
            page=pagination.page,
            page_size=pagination.page_size,
        )
    )


@router.post(
    "/conversations",
    response_model=ResponseModel[ConversationOut],
    status_code=status.HTTP_201_CREATED,
    summary="新建会话",
    description="创建一个空会话。若不传标题，则使用『新对话』。",
)
def create_conversation(
    payload: ConversationCreate,
    db: DbSession,
    current_user: CurrentUser,
):
    conversation = crud_conversation.create_for_user(
        db,
        user_id=current_user.id,
        title=payload.title,
        model_name=payload.model_name,
        use_rag=payload.use_rag,
    )
    return ResponseModel.success(
        data=ConversationOut.model_validate(conversation), message="会话创建成功"
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ResponseModel[ConversationDetailOut],
    summary="会话详情（含消息记录）",
    description="""
获取会话信息 + 全部消息记录，前端点开历史会话时调用。

**越权保护**：`{conversation_id}` 必须属于当前登录用户，
否则返回 404（不返回 403，避免暴露 ID 是否存在）。
    """,
)
def get_conversation_detail(
    db: DbSession,
    current_user: CurrentUser,
    conversation: Conversation = Depends(get_owned_conversation),
):
    messages = crud_message.list_by_conversation(
        db, conversation.id, user_id=current_user.id
    )
    detail = ConversationDetailOut.model_validate(
        {
            **ConversationOut.model_validate(conversation).model_dump(),
            "messages": [MessageOut.model_validate(m) for m in messages],
        }
    )
    return ResponseModel.success(data=detail)

@router.put(
    "/conversations/{conversation_id}",
    response_model=ResponseModel[ConversationOut],
    summary="修改会话",
    description="支持重命名标题、置顶、切换知识库检索开关。",
)
def update_conversation(
    payload: ConversationUpdate,
    db: DbSession,
    conversation: Conversation = Depends(get_owned_conversation),
):
    conversation = crud_conversation.update(db, conversation, payload)
    return ResponseModel.success(
        data=ConversationOut.model_validate(conversation), message="会话更新成功"
    )


@router.delete(
    "/conversations/{conversation_id}",
    response_model=ResponseModel[MessageOutSimple],
    summary="删除会话",
    description="默认软删除（数据仍在库中，可恢复）。消息记录会保留用于审计。",
)
def delete_conversation(
    db: DbSession,
    current_user: CurrentUser,
    request: Request,
    conversation: Conversation = Depends(get_owned_conversation),
    ip: str = Depends(get_client_ip),
):
    conversation_title = conversation.title
    crud_conversation.remove(db, conversation.id, hard=False)

    # 删除属于重要操作，写审计日志
    crud_audit_log.write(
        db,
        action="delete_conversation",
        user_id=current_user.id,
        username=current_user.username,
        resource="conversation",
        resource_id=conversation.id,
        detail={"title": conversation_title},
        ip=ip,
        success=True,
        message="删除会话",
    )
    return ResponseModel.success(data=MessageOutSimple(message="会话已删除"), message="会话已删除")


@router.delete(
    "/conversations",
    response_model=ResponseModel[MessageOutSimple],
    summary="清空我的全部会话",
    description="软删除当前用户的全部会话记录。",
)
def clear_conversations(
    db: DbSession,
    current_user: CurrentUser,
    ip: str = Depends(get_client_ip),
):
    count = crud_conversation.delete_all_by_user(db, current_user.id)
    crud_audit_log.write(
        db,
        action="clear_conversations",
        user_id=current_user.id,
        username=current_user.username,
        detail={"deleted_count": count},
        ip=ip,
        success=True,
        message=f"清空会话 {count} 条",
    )
    return ResponseModel.success(
        data=MessageOutSimple(message=f"已清空 {count} 个会话"), message=f"已清空 {count} 个会话"
    )


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=PageResponseModel[MessageOut],
    summary="分页拉取会话消息",
    description="用于长对话的滚动加载（默认从最新往旧翻）。",
)
def list_messages(
    db: DbSession,
    current_user: CurrentUser,
    conversation: Conversation = Depends(get_owned_conversation),
    pagination: PaginationQuery = Depends(),
):
    messages, total = crud_message.list_by_conversation_paged(
        db,
        conversation_id=conversation.id,
        user_id=current_user.id,
        skip=pagination.skip,
        limit=pagination.limit,
    )
    return ResponseModel.success(
        data=PageResult.create(
            items=[MessageOut.model_validate(m) for m in messages],
            total=total,
            page=pagination.page,
            page_size=pagination.page_size,
        )
    )


# =============================================================================
# 4. 历史提问搜索
# =============================================================================
@router.get(
    "/messages/search",
    response_model=ResponseModel[MessageSearchOut],
    summary="搜索我的历史提问",
    description="按关键字检索当前用户的历史消息，**只能搜到自己的记录**。",
)
def search_messages(
    db: DbSession,
    current_user: CurrentUser,
    keyword: str = Query(..., min_length=1, max_length=100, description="搜索关键字"),
    pagination: PaginationQuery = Depends(),
):
    messages, total = crud_message.search_by_user(
        db,
        user_id=current_user.id,
        keyword=keyword,
        skip=pagination.skip,
        limit=pagination.limit,
    )
    return ResponseModel.success(
        data=MessageSearchOut(
            keyword=keyword,
            **PageResult.create(
                items=[
                    MessageSearchItemOut(
                        id=m.id,
                        conversation_id=m.conversation_id,
                        role=m.role,
                        content=m.content,
                        created_at=m.created_at,
                    )
                    for m in messages
                ],
                total=total,
                page=pagination.page,
                page_size=pagination.page_size,
            ).model_dump(),
        )
    )
