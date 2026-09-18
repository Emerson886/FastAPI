"""
==============================================================================
 CRUD：消息（Message）
==============================================================================

方法清单：
    - create_message        写入一条消息（用户提问或 AI 回答）
    - list_by_conversation  拉取某会话的完整历史（前端渲染用）
    - get_history_for_llm   转成 LangChain 需要的格式，作为对话上下文
    - get_owned             按 ID + user_id 查（越权保护）
    - delete_by_conversation 清空某会话的消息
    - search_by_user        全文检索某用户的历史提问（可选功能）

【为什么要限制历史条数？】
    大模型有上下文窗口上限，把几百轮对话全部塞进去会：
        a) 超出 token 限制直接报错
        b) 成本飙升（输入 token 按量计费）
    所以 get_history_for_llm 默认只取最近 N 条（可配置）。
    进阶做法是「摘要记忆」：把更早的对话压缩成一段摘要。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.crud.base import CRUDBase
from app.models.message import Message
from app.schemas.message import MessageCreate, MessageUpdate

# 默认送入大模型的最近消息条数（不含当前提问）
DEFAULT_HISTORY_LIMIT = 20


class CRUDMessage(CRUDBase[Message, MessageCreate, MessageUpdate]):
    """消息数据访问层。"""

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_owned(self, db: Session, message_id: int, user_id: int) -> Message | None:
        """按 ID + 用户 ID 查询，防止越权读取别人的消息。"""
        stmt = select(Message).where(Message.id == message_id, Message.user_id == user_id)
        return db.execute(stmt).scalar_one_or_none()

    def list_by_conversation(
        self,
        db: Session,
        conversation_id: int,
        user_id: int | None = None,
        limit: int | None = None,
        ascending: bool = True,
    ) -> list[Message]:
        """
        拉取会话的消息列表。

        :param user_id: 传入则额外做归属校验（推荐始终传）
        :param limit:   传入则只取最近 limit 条（注意：取最近 N 条后再正序排列）
        :param ascending: True=时间正序（前端聊天界面）；False=倒序（日志查看）
        """
        conditions = [Message.conversation_id == conversation_id]
        if user_id is not None:
            conditions.append(Message.user_id == user_id)

        stmt = select(Message).where(*conditions)

        if limit:
            # 先按 id 倒序取最近 N 条，再在 Python 里翻转成正序
            stmt = stmt.order_by(Message.id.desc()).limit(limit)
            rows = list(db.execute(stmt).scalars().all())
            return list(reversed(rows)) if ascending else rows

        stmt = stmt.order_by(Message.id.asc() if ascending else Message.id.desc())
        return list(db.execute(stmt).scalars().all())

    def get_history_for_llm(
        self,
        db: Session,
        conversation_id: int,
        limit: int = DEFAULT_HISTORY_LIMIT,
        exclude_message_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        把历史消息转成 LangChain / OpenAI 对话格式：

            [
              {"role": "user",      "content": "公司年假多少天？"},
              {"role": "assistant", "content": "根据《员工手册》第 3 章..."},
              ...
            ]

        :param exclude_message_id: 排除某条消息（比如刚存进去的当前提问，
                                   避免重复送入模型）
        """
        conditions = [
            Message.conversation_id == conversation_id,
            Message.role.in_(["user", "assistant"]),  # system 提示词单独构造
            Message.status == "success",              # 失败的回答不进上下文
        ]
        if exclude_message_id is not None:
            conditions.append(Message.id != exclude_message_id)

        stmt = (
            select(Message)
            .where(*conditions)
            .order_by(Message.id.desc())
            .limit(limit)
        )
        rows = list(db.execute(stmt).scalars().all())
        rows.reverse()  # 转回时间正序

        return [{"role": m.role, "content": m.content} for m in rows]

    def count_by_conversation(self, db: Session, conversation_id: int) -> int:
        """统计会话中的消息条数。"""
        stmt = select(func.count()).select_from(Message).where(
            Message.conversation_id == conversation_id
        )
        return int(db.execute(stmt).scalar_one())

    def list_by_conversation_paged(
        self,
        db: Session,
        conversation_id: int,
        user_id: int | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[Message], int]:
        """
        分页拉取会话消息（长对话滚动加载）。

        返回 (消息列表, 总条数)。
        注意排序为 ID 正序（老消息在前），符合聊天记录从上往下的阅读顺序。
        """
        conditions = [Message.conversation_id == conversation_id]
        if user_id is not None:
            conditions.append(Message.user_id == user_id)

        stmt = (
            select(Message)
            .where(*conditions)
            .order_by(Message.id.asc())
            .offset(skip)
            .limit(limit)
        )
        count_stmt = select(func.count()).select_from(Message).where(*conditions)
        total = int(db.execute(count_stmt).scalar_one())
        return list(db.execute(stmt).scalars().all()), total

    def search_by_user(
        self,
        db: Session,
        user_id: int,
        keyword: str,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[Message], int]:
        """
        搜索当前用户的历史提问（「我记得之前问过...」场景）。

        注意：这里用 LIKE 做简单搜索。数据量大时可以：
            a) 建立 MySQL 全文索引（FULLTEXT）并用 MATCH AGAINST
            b) 把消息同步到 Elasticsearch
        """
        pattern = f"%{keyword.strip()}%"
        conditions = [Message.user_id == user_id, Message.content.like(pattern)]

        stmt = select(Message).where(*conditions).order_by(Message.id.desc()).offset(skip).limit(limit)
        count_stmt = select(func.count()).select_from(Message).where(*conditions)
        total = int(db.execute(count_stmt).scalar_one())
        return list(db.execute(stmt).scalars().all()), total

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def create_message(
        self,
        db: Session,
        conversation_id: int,
        user_id: int,
        role: str,
        content: str,
        citations: list[dict[str, Any]] | None = None,
        model_name: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        latency_ms: int | None = None,
        status: str = "success",
        error_message: str | None = None,
        parent_id: int | None = None,
    ) -> Message:
        """
        写入一条消息。

        这个方法把所有可空字段都显式列出来，调用方按需传参，
        避免用 **kwargs 造成字段拼写错误却静默失败。
        """
        message = Message(
            conversation_id=conversation_id,
            user_id=user_id,
            role=role,
            content=content,
            citations=citations,
            model_name=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            status=status,
            error_message=error_message,
            parent_id=parent_id,
        )
        db.add(message)
        db.commit()
        db.refresh(message)
        return message

    def update_assistant_reply(
        self,
        db: Session,
        message: Message,
        content: str,
        citations: list[dict[str, Any]] | None = None,
        latency_ms: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        status: str = "success",
        error_message: str | None = None,
    ) -> Message:
        """
        流式输出场景使用：先插入一条 status='streaming' 的空回答占位，
        边生成边推送给前端，全部生成完再调用本方法落库最终内容。

        这样做的好处：即使中途用户关闭页面，也能看到一条「生成中断」的记录。
        """
        message.content = content
        if citations is not None:
            message.citations = citations
        if latency_ms is not None:
            message.latency_ms = latency_ms
        if prompt_tokens is not None:
            message.prompt_tokens = prompt_tokens
        if completion_tokens is not None:
            message.completion_tokens = completion_tokens
        message.status = status
        if error_message is not None:
            message.error_message = error_message

        db.add(message)
        db.commit()
        db.refresh(message)
        return message

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------
    def delete_by_conversation(self, db: Session, conversation_id: int) -> int:
        """
        物理删除某会话的全部消息（当会话被硬删除时调用）。
        消息表不做软删除：删除会话后消息没有独立存在意义，留在库里反而占空间。
        """
        stmt = delete(Message).where(Message.conversation_id == conversation_id)
        result = db.execute(stmt)
        db.commit()
        return int(result.rowcount or 0)


crud_message = CRUDMessage(Message)
