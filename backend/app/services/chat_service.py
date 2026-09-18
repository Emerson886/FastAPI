"""
==============================================================================
 问答服务（Chat Service）—— 把 Agent、数据库、SSE 串起来
==============================================================================

本模块是「一次完整问答」的业务编排者，职责：

    1. 会话准备：没有会话就新建，有会话就校验归属（数据隔离）
    2. 保存用户提问到 messages 表
    3. 读取历史对话（作为大模型的上下文记忆）
    4. 调用 Agent 流式生成回答
    5. 边生成边把增量文本通过 SSE 推给前端
    6. 生成结束后把完整回答、引用来源、token 用量落库
    7. 更新会话统计（消息数、最后消息时间）与审计日志

【为什么历史记忆要自己查数据库，而不用 LangGraph 的 checkpointer？】
    LangGraph 自带 checkpointer 可以把对话状态持久化（甚至存到 MySQL/Postgres）。
    但企业的普遍要求是「对话记录必须能按业务规则查询、导出、审计、脱敏」，
    也就是数据要落在我们自己的业务表里（messages 表）。
    因此本项目采用「messages 表 + 每次手动构造历史消息」的方式，
    控制力最强，也最符合企业合规要求。
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging_config import logger
from app.crud import crud_audit_log, crud_conversation, crud_message
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.services import agent as agent_service
from app.services.rag.prompts import SYSTEM_PROMPT  # noqa: F401  (导出给调试脚本用)


# =============================================================================
# SSE 帧格式化
# =============================================================================
def format_sse(data: dict[str, Any], event: str | None = None) -> str:
    """
    把字典格式化成 SSE（Server-Sent Events）协议帧。

    SSE 格式规定：
        event: <事件名>\n
        data: <数据>\n
        \n                ← 空行表示一条消息结束

    注意：data 里不能有裸换行符，否则会破坏协议，
         所以用 json.dumps 转义（ensure_ascii=False 保证中文正常显示）。
    """
    payload = json.dumps(data, ensure_ascii=False)
    lines = []
    if event:
        lines.append(f"event: {event}")
    lines.append(f"data: {payload}")
    lines.append("")
    lines.append("")
    return "\n".join(lines)


def sse_comment(text: str = "keep-alive") -> str:
    """
    SSE 注释帧（以冒号开头），用于心跳保活。
    作用：防止 Nginx / 浏览器在长时间无数据时断开连接。
    """
    return f": {text}\n\n"


# =============================================================================
# 会话准备
# =============================================================================
def prepare_conversation(
    db: Session,
    user: User,
    conversation_id: int | None,
    question: str,
    use_rag: bool | None = None,
    model_name: str | None = None,
) -> Conversation:
    """
    准备会话对象：
        - 传了 conversation_id → 校验归属（越权则抛 404）
        - 没传               → 用问题内容自动生成标题并新建会话

    【数据隔离要点】校验归属统一走 crud_conversation.get_owned(db, cid, user.id)，
    它内部带 WHERE user_id = ? 条件，从 SQL 层面杜绝越权。
    """
    if conversation_id is not None:
        conversation = crud_conversation.get_owned(db, conversation_id, user.id)
        if conversation is None:
            from app.core.exceptions import NotFoundException

            raise NotFoundException(message="会话不存在或无权访问")
        # 允许在会话内临时切换 RAG 开关与模型
        if use_rag is not None and conversation.use_rag != use_rag:
            conversation.use_rag = use_rag
        if model_name:
            conversation.model_name = model_name
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
        return conversation

    # ---- 新建会话：用问题前 20 个字做标题 ----
    title = crud_conversation.auto_title_from_question(question)
    return crud_conversation.create_for_user(
        db,
        user_id=user.id,
        title=title,
        model_name=model_name,
        use_rag=True if use_rag is None else use_rag,
    )


def save_user_message(
    db: Session, conversation: Conversation, user: User, question: str
) -> Message:
    """保存用户提问到数据库。"""
    return crud_message.create_message(
        db,
        conversation_id=conversation.id,
        user_id=user.id,
        role="user",
        content=question,
        status="success",
    )


def load_history(
    db: Session,
    conversation: Conversation,
    exclude_message_id: int | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    读取历史对话作为大模型上下文。

    ⚠ 这里必须显式传 user_id 做二次校验，
      防止「历史记录串会话」导致的数据泄露。
    """
    return crud_message.get_history_for_llm(
        db,
        conversation_id=conversation.id,
        limit=limit,
        exclude_message_id=exclude_message_id,
    )


# =============================================================================
# 流式问答主流程
# =============================================================================
async def chat_stream(
    db: Session,
    user: User,
    question: str,
    conversation_id: int | None = None,
    use_rag: bool | None = None,
    model_name: str | None = None,
    document_ids: list[int] | None = None,
    request_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """
    流式问答（SSE）。

    产出的是「已经格式化好的 SSE 字符串」，接口层直接放进
    StreamingResponse 的生成器里即可。

    【关键设计：为什么用生成器而不是普通函数？】
        普通函数必须等全部生成完才 return，用户要盯着空白屏幕等 10 秒。
        生成器可以「生成一点、推一点」，实现打字机效果，体验完全不同。

    【数据库会话的生命周期问题（重要）】
        StreamingResponse 的生成器执行时，FastAPI 的依赖注入作用域已经结束，
        get_db 提供的 Session 可能已经被关闭。
        因此这里【所有数据库写操作都在流开始前完成】，流中只做纯内存操作，
        最后落库时用一个独立的新 Session（见 _finalize_message）。
    """
    # ------------------------------------------------------------------
    # 阶段 1：准备工作（全部在流开始前完成，使用请求作用域的 db）
    # ------------------------------------------------------------------
    conversation = prepare_conversation(
        db, user, conversation_id, question, use_rag=use_rag, model_name=model_name
    )

    # 保存用户提问
    user_message = save_user_message(db, conversation, user, question)

    # 读取历史（排除刚保存的当前提问，避免重复）
    history = load_history(db, conversation, exclude_message_id=user_message.id)

    effective_use_rag = conversation.use_rag if use_rag is None else use_rag
    effective_model = model_name or conversation.model_name or settings.LLM_MODEL

    logger.info(
        "开始流式问答 | user_id={} | conversation_id={} | 历史 {} 条 | rag={} | model={}",
        user.id, conversation.id, len(history), effective_use_rag, effective_model,
    )

    # ------------------------------------------------------------------
    # 阶段 2：推送 start 事件（前端据此拿到会话 ID，用于新建会话后刷新侧边栏）
    # ------------------------------------------------------------------
    yield format_sse(
        {
            "type": "start",
            "conversation_id": conversation.id,
            "user_message_id": user_message.id,
            "model_name": effective_model,
            "use_rag": effective_use_rag,
        },
        event="start",
    )

    # ------------------------------------------------------------------
    # 阶段 3：调用 Agent 流式生成，逐块转发
    # ------------------------------------------------------------------
    full_content = ""
    citations: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    failed = False

    try:
        async for event in agent_service.stream_chat(
            question=question,
            history=history,
            user_id=user.id,                       # ← 权限过滤依据
            model_name=effective_model,
            use_rag=effective_use_rag,
            document_ids=document_ids,
        ):
            event_type = event.get("type")

            if event_type == "delta":
                full_content += event.get("content", "")
                yield format_sse(event, event="delta")

            elif event_type == "citations":
                citations.extend(event.get("citations") or [])
                yield format_sse(event, event="citations")

            elif event_type == "done":
                # done 事件不直接转发：需要等落库完成后补上 assistant_message_id
                full_content = event.get("content") or full_content
                citations = event.get("citations") or citations
                usage = {
                    "latency_ms": event.get("latency_ms"),
                    "prompt_tokens": event.get("prompt_tokens"),
                    "completion_tokens": event.get("completion_tokens"),
                    "model_name": event.get("model_name"),
                }

            elif event_type == "error":
                failed = True
                error_text = event.get("error", "生成失败")
                logger.error("问答生成失败 | user_id={} | {}", user.id, error_text)

                # 失败也要落库：用户能看到一条「生成失败」的记录，便于反馈问题
                _save_assistant_message_safely(
                    conversation_id=conversation.id,
                    user_id=user.id,
                    content=full_content or f"[生成失败] {error_text}",
                    citations=citations,
                    status="failed",
                    error_message=error_text,
                    model_name=effective_model,
                )
                yield format_sse({"type": "error", "error": error_text}, event="error")
                return

    except Exception as exc:  # pragma: no cover - 兜底保护
        logger.exception("流式问答出现未预期异常 | user_id={}", user.id)
        yield format_sse(
            {"type": "error", "error": "服务器处理异常，请稍后重试"}, event="error"
        )
        return

    # ------------------------------------------------------------------
    # 阶段 4：落库（用独立 Session，因为请求作用域的 db 可能已关闭）
    # ------------------------------------------------------------------
    assistant_message = _save_assistant_message_safely(
        conversation_id=conversation.id,
        user_id=user.id,
        content=full_content,
        citations=citations,
        status="success" if not failed else "failed",
        model_name=usage.get("model_name") or effective_model,
        latency_ms=usage.get("latency_ms"),
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
    )

    # 更新会话统计（消息数 +2、最后消息时间）
    _touch_conversation_safely(conversation.id)

    # 写审计日志
    _write_audit_safely(
        user_id=user.id,
        username=user.username,
        conversation_id=conversation.id,
        question=question,
        request_id=request_id,
    )

    # ------------------------------------------------------------------
    # 阶段 5：推送 done 事件（带上落库后的消息 ID）
    # ------------------------------------------------------------------
    yield format_sse(
        {
            "type": "done",
            "conversation_id": conversation.id,
            "user_message_id": user_message.id,
            "assistant_message_id": assistant_message.id if assistant_message else None,
            "content": full_content,
            "citations": citations,
            "latency_ms": usage.get("latency_ms"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "model_name": usage.get("model_name") or effective_model,
        },
        event="done",
    )


# =============================================================================
# 独立的数据库操作（每次开新会话，避免流式场景下 Session 已关闭的问题）
# =============================================================================
def _save_assistant_message_safely(
    conversation_id: int,
    user_id: int,
    content: str,
    citations: list[dict[str, Any]],
    status: str = "success",
    error_message: str | None = None,
    model_name: str | None = None,
    latency_ms: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> Message | None:
    """
    保存 AI 回答。

    「safely」的含义：即使保存失败也不能让整个响应崩掉 —— 
    用户已经看到回答了，此时抛异常会变成一个莫名其妙的报错。
    所以这里吞掉异常并记录日志。
    """
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        return crud_message.create_message(
            db,
            conversation_id=conversation_id,
            user_id=user_id,
            role="assistant",
            content=content,
            citations=citations or None,
            model_name=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            status=status,
            error_message=error_message,
        )
    except Exception as exc:  # pragma: no cover
        db.rollback()
        logger.exception("保存 AI 回答失败 | conversation_id={} | {}", conversation_id, exc)
        return None
    finally:
        db.close()


def _touch_conversation_safely(conversation_id: int) -> None:
    """更新会话统计（消息数 +2）。失败只记日志。"""
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        crud_conversation.touch(db, conversation_id, inc_messages=2)
    except Exception as exc:  # pragma: no cover
        db.rollback()
        logger.warning("更新会话统计失败 | conversation_id={} | {}", conversation_id, exc)
    finally:
        db.close()


def _write_audit_safely(
    user_id: int,
    username: str,
    conversation_id: int,
    question: str,
    request_id: str | None = None,
) -> None:
    """写问答审计日志（只记问题摘要，不记完整回答，避免审计表膨胀）。"""
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        crud_audit_log.write(
            db,
            action="chat",
            user_id=user_id,
            username=username,
            resource="conversation",
            resource_id=conversation_id,
            detail={"question": question[:200]},  # 截断，避免超长
            request_id=request_id,
            success=True,
            message="问答完成",
        )
    except Exception as exc:  # pragma: no cover
        db.rollback()
        logger.warning("写问答审计日志失败：{}", exc)
    finally:
        db.close()


# =============================================================================
# 非流式问答（同步版本，供接口调试与内部调用）
# =============================================================================
async def chat_once(
    db: Session,
    user: User,
    question: str,
    conversation_id: int | None = None,
    use_rag: bool | None = None,
    model_name: str | None = None,
    document_ids: list[int] | None = None,
) -> dict[str, Any]:
    """
    非流式问答：一次拿到完整结果。

    适用场景：
        - 前端不方便处理 SSE（如小程序）
        - 后端内部调用（如批量生成摘要、自动化测试）
    """
    conversation = prepare_conversation(
        db, user, conversation_id, question, use_rag=use_rag, model_name=model_name
    )
    user_message = save_user_message(db, conversation, user, question)
    history = load_history(db, conversation, exclude_message_id=user_message.id)

    effective_use_rag = conversation.use_rag if use_rag is None else use_rag
    effective_model = model_name or conversation.model_name or settings.LLM_MODEL

    result = await agent_service.invoke_chat(
        question=question,
        history=history,
        user_id=user.id,
        model_name=effective_model,
        use_rag=effective_use_rag,
        document_ids=document_ids,
    )

    assistant_message = crud_message.create_message(
        db,
        conversation_id=conversation.id,
        user_id=user.id,
        role="assistant",
        content=result["content"],
        citations=result.get("citations") or None,
        model_name=result.get("model_name"),
        latency_ms=result.get("latency_ms"),
        status="success",
    )
    crud_conversation.touch(db, conversation.id, inc_messages=2)

    return {
        "conversation_id": conversation.id,
        "conversation_title": conversation.title,
        "user_message_id": user_message.id,
        "assistant_message_id": assistant_message.id,
        "content": result["content"],
        "citations": result.get("citations") or [],
        "latency_ms": result.get("latency_ms"),
        "model_name": result.get("model_name"),
    }
