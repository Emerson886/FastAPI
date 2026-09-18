"""
==============================================================================
 CRUD：会话（Conversation）
==============================================================================

【本文件是「用户数据隔离」的第一道防线】
所有查询方法都强制要求传 user_id，从 SQL 层面就加上 WHERE user_id = ?
即使接口层忘记校验，也拿不到别人的数据。

方法清单：
    - get_owned            按 ID + user_id 查询（越权时返回 None）
    - list_by_user         某用户的分页会话列表（支持关键字搜索）
    - create_for_user      为用户创建新会话
    - update_title         更新标题（自动截断）
    - touch                更新「最后消息时间 + 消息数」（每轮问答后调用）
    - list_all_admin       管理员查看所有人的会话（后台审计用）
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from app.crud.base import CRUDBase
from app.models.conversation import Conversation
from app.schemas.conversation import ConversationCreate, ConversationUpdate

# 自动生成标题时保留的字数
AUTO_TITLE_MAX_LEN = 20


# =============================================================================
# 排序辅助：NULL 值排在最后（跨数据库兼容）
# =============================================================================
# 【为什么需要这个函数？这是一个真实踩过的坑】
#
#   SQLAlchemy 的 `.nullslast()` 会生成 `ORDER BY col DESC NULLS LAST`，
#   但【MySQL 不支持 NULLS LAST 语法】（PostgreSQL / Oracle / SQLite 支持）。
#   在 MySQL 上执行会直接报语法错误：
#       (1064, "You have an error in your SQL syntax ... near 'NULLS LAST'")
#   现象是接口直接 500，日志里只显示「数据库异常」。
#
# 【各数据库的正确做法】
#   MySQL ：没有 NULLS LAST 关键字，用 `ISNULL(col)` 参与排序代替
#   PG/SQLite：原生支持 `NULLS LAST`
#   其他  ：退化为普通排序
#
# 这里改用 SQLAlchemy 的 case 表达式 `CASE WHEN col IS NULL THEN 1 ELSE 0 END`，
# 它是标准 SQL，MySQL / PostgreSQL / SQLite / Oracle 全部支持，
# 无需判断方言，也不会因为换数据库而失效。
# =============================================================================
def order_nulls_last(column):
    """
    返回「降序时 NULL 排最后」的排序表达式列表。

    用法：
        stmt.order_by(*order_nulls_last(Conversation.last_message_at))

    生成 SQL：
        ORDER BY CASE WHEN last_message_at IS NULL THEN 1 ELSE 0 END ASC,
                 last_message_at DESC

    原理：先把「是否为空」升序排（非空=0 排在空=1 的前面），
         同一组内再按实际值降序。这是标准 SQL，四种主流数据库都支持。
    """
    return [case((column.is_(None), 1), else_=0).asc(), column.desc()]


# 语义更明确的别名（推荐使用这个，一眼看出是「降序 + NULL 排最后」）
order_nulls_last_desc = order_nulls_last


class CRUDConversation(CRUDBase[Conversation, ConversationCreate, ConversationUpdate]):
    """会话数据访问层。"""

    # ------------------------------------------------------------------
    # 查询（全部带 user_id 隔离）
    # ------------------------------------------------------------------
    def get_owned(self, db: Session, conversation_id: int, user_id: int) -> Conversation | None:
        """
        查询「属于该用户」的会话。

        这是所有会话相关接口的统一入口：
            conversation = crud_conversation.get_owned(db, cid, current_user.id)
            if conversation is None:
                raise NotFoundException("会话不存在或无权访问")
        """
        stmt = (
            select(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,     # ← 数据隔离的关键条件
                Conversation.is_deleted.is_(False),
            )
        )
        return db.execute(stmt).scalar_one_or_none()

    def list_by_user(
        self,
        db: Session,
        user_id: int,
        keyword: str | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[Conversation], int]:
        """
        某用户的会话列表。

        排序规则：置顶优先 → 最后消息时间倒序 → 创建时间倒序。
        置顶用 is_pinned.desc()，让 pinned=True 排前面。

        ⚠ 注意 last_message_at 用 order_nulls_last_desc() 而不是
          `.desc().nullslast()` —— 后者生成的 NULLS LAST 语法 MySQL 不支持。
        """
        conditions = [Conversation.user_id == user_id, Conversation.is_deleted.is_(False)]

        # 关键字搜索：按标题模糊匹配（历史记录检索）
        if keyword:
            conditions.append(Conversation.title.like(f"%{keyword.strip()}%"))

        stmt = select(Conversation).where(*conditions)
        count_stmt = select(func.count()).select_from(Conversation).where(*conditions)

        total = int(db.execute(count_stmt).scalar_one())
        stmt = (
            stmt.order_by(
                Conversation.is_pinned.desc(),
                # 没有消息的会话（last_message_at 为 NULL）排到最后
                *order_nulls_last_desc(Conversation.last_message_at),
                Conversation.created_at.desc(),
            )
            .offset(skip)
            .limit(limit)
        )
        return list(db.execute(stmt).scalars().all()), total

    def list_all_admin(
        self,
        db: Session,
        user_id: int | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[Conversation], int]:
        """
        管理员接口：查看全部用户的会话（合规审计场景）。
        普通用户接口【绝不】调用这个方法。
        """
        conditions = [Conversation.is_deleted.is_(False)]
        if user_id is not None:
            conditions.append(Conversation.user_id == user_id)

        stmt = select(Conversation).where(*conditions)
        count_stmt = select(func.count()).select_from(Conversation).where(*conditions)
        total = int(db.execute(count_stmt).scalar_one())
        stmt = stmt.order_by(Conversation.id.desc()).offset(skip).limit(limit)
        return list(db.execute(stmt).scalars().all()), total

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def create_for_user(
        self,
        db: Session,
        user_id: int,
        title: str | None = None,
        model_name: str | None = None,
        use_rag: bool = True,
    ) -> Conversation:
        """为新用户创建会话。"""
        conversation = Conversation(
            user_id=user_id,
            title=(title or "新对话")[:200],
            model_name=model_name,
            use_rag=use_rag,
            message_count=0,
            last_message_at=datetime.now(),
        )
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
        return conversation

    def update_title(self, db: Session, conversation: Conversation, title: str) -> Conversation:
        """更新会话标题。"""
        conversation.title = title.strip()[:200] or "新对话"
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
        return conversation

    def auto_title_from_question(self, question: str) -> str:
        """
        用用户的第一句话自动生成标题（避免每次都叫「新对话」）。

        这里用纯文本截断，成本为 0。
        如果你想让大模型生成更精准的标题，可在 services/agent 里加一个
        一次性的 LLM 调用，把结果传进 create_for_user(title=...)。
        """
        text = " ".join(question.strip().split())
        if len(text) <= AUTO_TITLE_MAX_LEN:
            return text or "新对话"
        return text[:AUTO_TITLE_MAX_LEN] + "..."

    def touch(
        self,
        db: Session,
        conversation_id: int,
        inc_messages: int = 2,
        timestamp: datetime | None = None,
    ) -> None:
        """
        每轮问答结束后刷新会话统计。

        使用 SQL 的原子自增（message_count = message_count + N），
        而不是「先读出来 +1 再写回」，避免并发下的更新丢失问题。
        """
        stmt = (
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(
                message_count=Conversation.message_count + inc_messages,
                last_message_at=timestamp or datetime.now(),
            )
        )
        db.execute(stmt)
        db.commit()

    def set_pinned(self, db: Session, conversation: Conversation, pinned: bool) -> Conversation:
        """置顶 / 取消置顶。"""
        conversation.is_pinned = pinned
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
        return conversation

    def delete_all_by_user(self, db: Session, user_id: int) -> int:
        """清空某用户的全部会话（软删除），返回受影响条数。"""
        stmt = (
            update(Conversation)
            .where(Conversation.user_id == user_id, Conversation.is_deleted.is_(False))
            .values(is_deleted=True, deleted_at=datetime.now())
        )
        result = db.execute(stmt)
        db.commit()
        return int(result.rowcount or 0)


crud_conversation = CRUDConversation(Conversation)
