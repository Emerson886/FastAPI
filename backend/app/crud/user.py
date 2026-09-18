"""
==============================================================================
 CRUD：用户（User）
==============================================================================

在通用 CRUDBase 基础上，补充用户特有的业务查询：
    - get_by_username        登录时按用户名查
    - get_by_email           邮箱查重
    - authenticate           校验用户名 + 密码（登录核心逻辑）
    - create_user            创建用户（自动哈希密码）
    - change_password        修改密码
    - update_login_info      记录最后登录时间与 IP（登录审计）
    - deactivate / activate  启用 / 禁用账号
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.security import hash_password, verify_password
from app.crud.base import CRUDBase
from app.models.user import User
from app.schemas.user import UserCreate, UserUpdate


class CRUDUser(CRUDBase[User, UserCreate, UserUpdate]):
    """用户数据访问层。"""

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_by_username(self, db: Session, username: str) -> User | None:
        """
        按用户名查询。使用 func.lower 做大小写不敏感匹配
        （MySQL 默认 collation 通常不区分大小写，这里显式处理避免换库后行为不一致）。
        """
        stmt = select(User).where(func.lower(User.username) == username.strip().lower())
        return db.execute(stmt).scalar_one_or_none()

    def get_by_email(self, db: Session, email: str) -> User | None:
        """按邮箱查询（用于注册查重与找回密码）。"""
        stmt = select(User).where(func.lower(User.email) == email.strip().lower())
        return db.execute(stmt).scalar_one_or_none()

    def get_by_username_or_email(self, db: Session, account: str) -> User | None:
        """
        登录时允许「用户名 或 邮箱」登录。
        企业系统常见体验优化。
        """
        account = account.strip()
        stmt = select(User).where(
            or_(
                func.lower(User.username) == account.lower(),
                func.lower(User.email) == account.lower(),
            )
        )
        return db.execute(stmt).scalar_one_or_none()

    def search(
        self,
        db: Session,
        keyword: str | None = None,
        is_active: bool | None = None,
        skip: int = 0,
        limit: int = 10,
    ) -> tuple[list[User], int]:
        """
        管理员用户列表：支持关键字模糊搜索 + 状态过滤 + 分页。
        返回 (数据列表, 总条数)，配合 PageResult 使用。
        """
        stmt = select(User)
        count_stmt = select(func.count()).select_from(User)

        if keyword:
            pattern = f"%{keyword.strip()}%"
            condition = or_(
                User.username.like(pattern),
                User.nickname.like(pattern),
                User.email.like(pattern),
            )
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)
        if is_active is not None:
            stmt = stmt.where(User.is_active.is_(is_active))
            count_stmt = count_stmt.where(User.is_active.is_(is_active))

        total = int(db.execute(count_stmt).scalar_one())
        stmt = stmt.order_by(User.id.desc()).offset(skip).limit(limit)
        return list(db.execute(stmt).scalars().all()), total

    # ------------------------------------------------------------------
    # 认证
    # ------------------------------------------------------------------
    def authenticate(self, db: Session, account: str, password: str) -> User | None:
        """
        登录认证核心逻辑。

        安全细节（很重要，避免账号枚举攻击）：
            1) 用户不存在时，也执行一次假的 bcrypt 校验，
               让「用户不存在」与「密码错误」的响应耗时接近，
               攻击者无法通过响应时间判断用户名是否存在。
            2) 对外统一返回「用户名或密码错误」，不区分具体原因。
        """
        user = self.get_by_username_or_email(db, account)
        if user is None:
            # 假的哈希比对，消耗与真实校验相近的时间
            verify_password(password, "$2b$12$" + "x" * 53)
            return None

        if not verify_password(password, user.hashed_password):
            return None
        return user

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def create_user(self, db: Session, obj_in: UserCreate, is_superuser: bool = False) -> User:
        """
        创建用户。

        【重要】UserCreate 里有两个「不能直接写进数据库」的字段：
            - password        ：明文密码，必须转成 hashed_password 再入库
            - confirm_password：仅用于前后端校验「两次密码一致」，不是数据库字段
        如果直接把 model_dump() 的结果丢给 SQLAlchemy，会报
            TypeError: 'confirm_password' is an invalid keyword argument for User
        所以这里【显式列出】允许入库的字段白名单，而不是用 exclude 排除黑名单 ——
        白名单更安全：将来 Schema 新增字段时不会被误写入数据库。
        """
        data = {
            "username": obj_in.username,
            "email": obj_in.email or None,   # 空串统一存 NULL，避免唯一索引冲突
            "nickname": obj_in.nickname or obj_in.username,
            "avatar": obj_in.avatar,
            "hashed_password": hash_password(obj_in.password),
            "is_superuser": is_superuser,
        }

        return super().create(db, data)

    def change_password(self, db: Session, user: User, new_password: str) -> User:
        """修改密码：只更新哈希值。"""
        user.hashed_password = hash_password(new_password)
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    def update_login_info(self, db: Session, user: User, ip: str | None) -> None:
        """
        记录最后登录时间与 IP。
        容错处理：即使这步失败也不能影响登录流程，所以吞掉异常只记日志。
        """
        from app.core.logging_config import logger

        try:
            user.last_login_at = datetime.now()
            user.last_login_ip = ip
            db.add(user)
            db.commit()
        except Exception as exc:  # pragma: no cover
            db.rollback()
            logger.warning("更新登录信息失败：{}", exc)

    def activate(self, db: Session, user: User, active: bool = True) -> User:
        """启用 / 禁用账号（员工离职处理方式，不删数据）。"""
        user.is_active = active
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    def to_safe_dict(self, user: User) -> dict[str, Any]:
        """返回脱敏字典（无密码哈希），用于写审计日志。"""
        return user.to_dict()


# 全局单例：其他模块 `from app.crud.user import crud_user` 直接使用
crud_user = CRUDUser(User)
