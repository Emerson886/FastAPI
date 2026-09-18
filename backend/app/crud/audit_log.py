"""
==============================================================================
 CRUD：操作审计日志（AuditLog）
==============================================================================

审计日志的特点：只写不读改（append-only）。
    - write()           写入一条审计记录（失败也不能影响主业务流程）
    - list_logs()       管理员查询日志
    - clean_expired()   清理过期日志（按保留天数，定时任务调用）

【重要设计：审计写入必须"绝不抛异常"】
    审计只是「附带记录」，不能因为它失败导致用户登录不了、传不了文件。
    所以 write() 内部 catch 所有异常，只记 loguru 日志。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.logging_config import logger
from app.crud.base import CRUDBase
from app.models.audit_log import AuditLog


class CRUDAuditLog(CRUDBase[AuditLog, Any, Any]):
    """审计日志数据访问层。"""

    def write(
        self,
        db: Session,
        action: str,
        user_id: int | None = None,
        username: str | None = None,
        resource: str | None = None,
        resource_id: str | int | None = None,
        detail: dict[str, Any] | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        request_id: str | None = None,
        method: str | None = None,
        path: str | None = None,
        status_code: int | None = None,
        success: bool = True,
        message: str | None = None,
    ) -> AuditLog | None:
        """
        写入一条审计日志。

        用法（在接口里）：
            crud_audit.write(db, "login", user_id=user.id, username=user.username,
                             ip=request.client.host, success=True)
        """
        try:
            log = AuditLog(
                action=action,
                user_id=user_id,
                username=username,
                resource=resource,
                resource_id=str(resource_id) if resource_id is not None else None,
                detail=detail,
                ip=ip,
                # User-Agent 可能很长，截断到字段长度上限，防止插入报错
                user_agent=(user_agent or "")[:500] or None,
                request_id=request_id,
                method=method,
                path=(path or "")[:255] or None,
                status_code=status_code,
                success=success,
                message=message,
                created_at=datetime.now(),
            )
            db.add(log)
            db.commit()
            db.refresh(log)
            return log
        except Exception as exc:  # pragma: no cover
            # 审计失败必须回滚，否则会污染当前事务
            db.rollback()
            logger.warning("写入审计日志失败（已忽略，不影响主流程）：{} | action={}", exc, action)
            return None

    def list_logs(
        self,
        db: Session,
        user_id: int | None = None,
        action: str | None = None,
        success: bool | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[AuditLog], int]:
        """管理员查询审计日志（多条件过滤 + 分页）。"""
        conditions = []
        if user_id is not None:
            conditions.append(AuditLog.user_id == user_id)
        if action:
            conditions.append(AuditLog.action == action)
        if success is not None:
            conditions.append(AuditLog.success.is_(success))
        if start_time:
            conditions.append(AuditLog.created_at >= start_time)
        if end_time:
            conditions.append(AuditLog.created_at <= end_time)

        stmt = select(AuditLog).where(*conditions).order_by(AuditLog.id.desc()).offset(skip).limit(limit)
        count_stmt = select(func.count()).select_from(AuditLog).where(*conditions)
        total = int(db.execute(count_stmt).scalar_one())
        return list(db.execute(stmt).scalars().all()), total

    def clean_expired(self, db: Session, retention_days: int = 180) -> int:
        """
        物理删除超过保留期的日志（合规要求：审计日志一般保留 6 个月~3 年）。
        建议用定时任务每天凌晨执行一次。
        """
        deadline = datetime.now() - timedelta(days=retention_days)
        result = db.execute(delete(AuditLog).where(AuditLog.created_at < deadline))
        db.commit()
        deleted = int(result.rowcount or 0)
        if deleted:
            logger.info("清理过期审计日志 {} 条（保留 {} 天）", deleted, retention_days)
        return deleted


crud_audit_log = CRUDAuditLog(AuditLog)
