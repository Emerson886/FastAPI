"""
==============================================================================
 通用 CRUD 基类（CRUDBase）
==============================================================================

企业项目里，每个表都写一遍 get/create/update/delete 会造成大量重复代码。
本模块抽象出泛型基类，子类只需声明「模型类」和「可更新字段」，即可获得：

    - get(db, id)                 按主键查询
    - get_multi(db, skip, limit)  分页查询
    - create(db, obj_in)          新增
    - update(db, db_obj, obj_in)  更新（支持 Pydantic 模型或 dict）
    - remove(db, id)              物理删除
    - soft_remove(db, id)         软删除（仅对继承 SoftDeleteMixin 的表有效）
    - count(db, filters)          统计条数
    - exists(db, **filters)       是否存在

【设计亮点：类型安全】
    使用两个泛型参数 ModelType / CreateSchemaType / UpdateSchemaType，
    IDE 能推断出返回值是具体模型类，比如 crud.user.get() 返回 User 而不是 Any。
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.db.base import Base, SoftDeleteMixin, utcnow

# 泛型参数：模型类、创建 Schema、更新 Schema
ModelType = TypeVar("ModelType", bound=Base)
CreateSchemaType = TypeVar("CreateSchemaType", bound=BaseModel)
UpdateSchemaType = TypeVar("UpdateSchemaType", bound=BaseModel)


class CRUDBase(Generic[ModelType, CreateSchemaType, UpdateSchemaType]):
    """通用增删改查基类。"""

    def __init__(self, model: type[ModelType]) -> None:
        """
        :param model: 该 CRUD 操作的 ORM 模型类，例如 User
        """
        self.model = model
        # 判断该模型是否支持软删除（决定 remove 是物理删还是逻辑删、查询是否过滤）
        self.supports_soft_delete = issubclass(model, SoftDeleteMixin)

    # ==================================================================
    # 内部工具
    # ==================================================================
    def _base_query(self, include_deleted: bool = False) -> Select[Any]:
        """
        构造基础查询语句。

        【重要】对于支持软删除的表，默认只查 is_deleted=False 的数据。
        这就是为什么业务代码不需要到处写过滤条件。
        """
        stmt = select(self.model)
        if self.supports_soft_delete and not include_deleted:
            stmt = stmt.where(self.model.is_deleted.is_(False))  # type: ignore[attr-defined]
        return stmt

    def _apply_filters(self, stmt: Select[Any], filters: dict[str, Any] | None) -> Select[Any]:
        """
        把 {"username": "admin", "is_active": True} 这样的字典转成 WHERE 条件。

        说明：value 为 None 的键会被跳过（表示「该条件不参与过滤」），
             若确实需要查 IS NULL，请直接在业务 CRUD 里写 .where(col.is_(None))。
        """
        for field, value in (filters or {}).items():
            if value is None:
                continue
            stmt = stmt.where(getattr(self.model, field) == value)
        return stmt

    # ==================================================================
    # 查询
    # ==================================================================
    def get(
        self,
        db: Session,
        obj_id: int,
        include_deleted: bool = False,
    ) -> ModelType | None:
        """按主键查询单条记录，不存在返回 None。"""
        stmt = self._base_query(include_deleted).where(self.model.id == obj_id)  # type: ignore[attr-defined]
        return db.execute(stmt).scalar_one_or_none()

    def get_by(self, db: Session, **filters: Any) -> ModelType | None:
        """
        按任意字段查询单条记录。
        示例：crud.user.get_by(db, username="admin")
        """
        stmt = self._base_query()
        for field, value in filters.items():
            stmt = stmt.where(getattr(self.model, field) == value)
        return db.execute(stmt).scalar_one_or_none()

    def get_multi(
        self,
        db: Session,
        skip: int = 0,
        limit: int = 100,
        filters: dict[str, Any] | None = None,
        order_by: Any = None,
        include_deleted: bool = False,
    ) -> list[ModelType]:
        """分页查询列表。"""
        stmt = self._base_query(include_deleted)
        for field, value in (filters or {}).items():
            if value is None:
                continue
            stmt = stmt.where(getattr(self.model, field) == value)

        # 默认按创建时间倒序（最新的在前），符合大多数业务场景
        stmt = stmt.order_by(order_by if order_by is not None else self.model.created_at.desc())
        stmt = stmt.offset(skip).limit(limit)
        return list(db.execute(stmt).scalars().all())

    def count(
        self,
        db: Session,
        filters: dict[str, Any] | None = None,
        include_deleted: bool = False,
    ) -> int:
        """统计满足条件的记录数（用于分页）。"""
        stmt = select(func.count()).select_from(self.model)
        if self.supports_soft_delete and not include_deleted:
            stmt = stmt.where(self.model.is_deleted.is_(False))  # type: ignore[attr-defined]
        for field, value in (filters or {}).items():
            if value is None:
                continue
            stmt = stmt.where(getattr(self.model, field) == value)
        return int(db.execute(stmt).scalar_one())

    def exists(self, db: Session, **filters: Any) -> bool:
        """判断记录是否存在（比 count 更高效，存在即返回）。"""
        stmt = self._base_query().where(
            *[getattr(self.model, k) == v for k, v in filters.items()]
        ).limit(1)
        return db.execute(stmt).scalar_one_or_none() is not None

    # ==================================================================
    # 新增
    # ==================================================================
    def create(self, db: Session, obj_in: CreateSchemaType | dict[str, Any], **extra: Any) -> ModelType:
        """
        新增一条记录。

        :param obj_in: Pydantic 创建模型，或普通 dict
        :param extra:  额外要写入的字段（如 create(db, data, hashed_password=xxx)）
        """
        data = obj_in.model_dump(exclude_unset=True) if isinstance(obj_in, BaseModel) else dict(obj_in)
        data.update(extra)

        db_obj = self.model(**data)
        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)  # 取回数据库生成的自增 ID 与默认值
        return db_obj

    def create_many(self, db: Session, objs_in: list[dict[str, Any]]) -> list[ModelType]:
        """
        批量新增（性能敏感场景，如写入大量文档切片）。

        使用 bulk 方式一次性插入，比循环 create() 快几十倍。
        """
        if not objs_in:
            return []
        db_objs = [self.model(**data) for data in objs_in]
        db.add_all(db_objs)
        db.commit()
        for obj in db_objs:
            db.refresh(obj)
        return db_objs

    # ==================================================================
    # 更新
    # ==================================================================
    def update(
        self,
        db: Session,
        db_obj: ModelType,
        obj_in: UpdateSchemaType | dict[str, Any],
        exclude_unset: bool = True,
    ) -> ModelType:
        """
        更新记录。

        exclude_unset=True 的含义：Pydantic 模型里「没有显式传入」的字段不会被更新，
        这对于「部分更新」接口（PATCH）非常关键 —— 否则会把未传的字段覆盖成 None。
        """
        data = (
            obj_in.model_dump(exclude_unset=exclude_unset)
            if isinstance(obj_in, BaseModel)
            else dict(obj_in)
        )
        for field, value in data.items():
            if hasattr(db_obj, field):
                setattr(db_obj, field, value)

        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    # ==================================================================
    # 删除
    # ==================================================================
    def remove(self, db: Session, obj_id: int, hard: bool = False) -> ModelType | None:
        """
        删除记录。

        :param hard: True=物理删除（真删）；False=软删除（仅打标记，若模型支持）
        """
        db_obj = self.get(db, obj_id, include_deleted=True)
        if db_obj is None:
            return None

        if self.supports_soft_delete and not hard:
            # 软删除：只改标记，数据仍在库里
            db_obj.is_deleted = True          # type: ignore[attr-defined]
            db_obj.deleted_at = utcnow()      # type: ignore[attr-defined]
            db.add(db_obj)
        else:
            db.delete(db_obj)
        db.commit()
        return db_obj

    def remove_many(self, db: Session, ids: list[int], hard: bool = False) -> int:
        """批量删除，返回实际删除条数。"""
        count = 0
        for obj_id in ids:
            if self.remove(db, obj_id, hard=hard) is not None:
                count += 1
        return count
