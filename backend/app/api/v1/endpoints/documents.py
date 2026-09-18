"""
==============================================================================
 API v1 端点：知识库文档管理（documents）
==============================================================================

接口清单：
    POST   /api/v1/documents/upload        上传文档（自动解析 + 向量化）★核心
    GET    /api/v1/documents               我的知识库文档列表（分页 + 筛选）
    GET    /api/v1/documents/overview      知识库总览统计
    GET    /api/v1/documents/categories    分类列表（前端筛选下拉框）
    GET    /api/v1/documents/{id}          文档详情
    GET    /api/v1/documents/{id}/chunks   查看切片（调优 chunk_size 用）
    PUT    /api/v1/documents/{id}          修改文档信息（标题/分类/可见性）
    DELETE /api/v1/documents/{id}          删除文档（同步清理向量库）★
    POST   /api/v1/documents/{id}/reindex  重新索引（换 Embedding 模型后用）
    POST   /api/v1/documents/retrieve      检索测试（不经过大模型，直接看召回效果）

【权限说明】
    可见性规则：is_public=True（全员可见）或 owner_id=当前用户
    删除权限：只有上传者本人（或管理员）可以删除
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status

from app.api.deps import CurrentUser, DbSession, get_client_ip
from app.core.config import settings
from app.core.exceptions import PermissionException
from app.core.logging_config import logger
from app.core.response import PageResponseModel, PageResult, ResponseModel
from app.crud import crud_audit_log, crud_document
from app.models.document import Document
from app.schemas.common import MessageOutSimple, PaginationQuery
from app.schemas.document import (
    CategoryStatOut,
    ChunkOut,
    DocumentOut,
    DocumentStatsOut,
    DocumentUpdate,
    DocumentUploadOut,
)
from app.services import document_service
from app.services.rag import vector_store

router = APIRouter(prefix="/documents", tags=["知识库文档"])


def _check_owner_or_admin(document: Document, current_user: CurrentUser) -> None:
    """
    校验删除/修改权限：必须是上传者本人或管理员。

    为什么列表能看（公共文档），但删除要校验上传者？
        公共文档全员可见是「读」权限；删除是「写」权限，属于高危操作，
        必须收紧到上传者与管理员，否则任何员工都能删掉公司制度文件。
    """
    if document.owner_id != current_user.id and not current_user.is_superuser:
        raise PermissionException(message="只有文档上传者或管理员才能执行此操作")


# =============================================================================
# 1. 【核心】上传文档
# =============================================================================
@router.post(
    "/upload",
    response_model=ResponseModel[DocumentUploadOut],
    status_code=status.HTTP_201_CREATED,
    summary="上传知识库文档 ★核心接口",
    description=f"""
上传文档并自动完成「解析 → 切片 → 向量化」，处理完成后即可被问答检索到。

**支持格式**：{settings.ALLOWED_UPLOAD_EXTENSIONS}
**大小限制**：{settings.MAX_UPLOAD_SIZE_MB} MB

**参数说明**（使用 multipart/form-data）：
- `file`：文件本体（必填）
- `category`：分类，如「人事制度」（可选）
- `is_public`：是否全员可见，默认 false（仅自己可见）（可选）
- `title`：文档标题，默认取文件名（可选）
- `tags`：标签，多个用英文逗号分隔（可选）
- `description`：文档说明（可选）

**行为说明**：
- 上传重复文件（内容哈希相同）时不会重复向量化，直接复用已有文档，`duplicated=true`
- 解析或向量化失败时，文档状态会变成 `failed` 并附上失败原因
    """,
)
async def upload_document(
    db: DbSession,
    current_user: CurrentUser,
    request: Request,
    file: UploadFile = File(..., description="要上传的文件"),
    category: str | None = Form(default=None, description="文档分类"),
    is_public: bool = Form(default=False, description="是否全员可见"),
    title: str | None = Form(default=None, description="文档标题"),
    tags: str | None = Form(default=None, description="标签，英文逗号分隔"),
    description: str | None = Form(default=None, description="文档说明"),
    ip: str = Depends(get_client_ip),
):
    # ---- 读取文件内容 ----
    content = await file.read()

    # ---- 解析标签 ----
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None

    # ---- 调用服务层处理 ----
    document, duplicated = document_service.upload_document(
        db=db,
        user=current_user,
        filename=file.filename or "unnamed",
        content=content,
        title=title,
        category=category,
        is_public=is_public,
        tags=tag_list,
        description=description,
    )

    # ---- 审计日志 ----
    crud_audit_log.write(
        db,
        action="upload_document",
        user_id=current_user.id,
        username=current_user.username,
        resource="document",
        resource_id=document.id,
        detail={
            "filename": document.filename,
            "size": document.file_size,
            "duplicated": duplicated,
            "is_public": is_public,
        },
        ip=ip,
        method=request.method,
        path=request.url.path,
        success=document.status != "failed",
        message="上传文档" + ("（重复文件，已复用）" if duplicated else ""),
    )

    message = "文件已存在，直接复用已有文档" if duplicated else "上传并处理完成"
    if document.status == "failed":
        message = f"文件已保存，但解析失败：{document.error_message}"

    return ResponseModel.success(
        data=DocumentUploadOut(
            document=DocumentOut.model_validate(document),
            duplicated=duplicated,
            message=message,
        ),
        message=message,
    )


# =============================================================================
# 2. 文档列表与统计
# =============================================================================
@router.get(
    "",
    response_model=PageResponseModel[DocumentOut],
    summary="知识库文档列表",
    description="返回当前用户可见的文档（公共文档 + 自己上传的文档），支持关键字/分类/状态筛选。",
)
def list_documents(
    db: DbSession,
    current_user: CurrentUser,
    pagination: PaginationQuery = Depends(),
    category: str | None = Query(default=None, description="按分类筛选"),
    status_filter: str | None = Query(
        default=None, alias="status", description="按状态筛选：pending/parsing/embedding/completed/failed"
    ),
):
    documents, total = crud_document.list_visible(
        db,
        user_id=current_user.id,
        keyword=pagination.keyword,
        category=category,
        status=status_filter,
        skip=pagination.skip,
        limit=pagination.limit,
    )
    return ResponseModel.success(
        data=PageResult.create(
            items=[DocumentOut.model_validate(d) for d in documents],
            total=total,
            page=pagination.page,
            page_size=pagination.page_size,
        )
    )


@router.get(
    "/overview",
    response_model=ResponseModel[dict],
    summary="知识库总览",
    description="统计卡片数据：文档数、完成数、处理中、失败数、切片数、向量数。",
)
def knowledge_base_overview(db: DbSession, current_user: CurrentUser):
    overview = document_service.get_knowledge_base_overview(db, current_user.id)
    return ResponseModel.success(
        data={
            "stats": DocumentStatsOut(**{k: overview[k] for k in
                                         ("total", "completed", "processing", "failed", "chunks")}),
            "vector_count": overview["vector_count"],
            "collection": overview["collection"],
            "embedding_model": overview["embedding_model"],
            "categories": [CategoryStatOut(**c) for c in overview["categories"]],
        }
    )


@router.get(
    "/categories",
    response_model=ResponseModel[list[CategoryStatOut]],
    summary="文档分类统计",
)
def list_categories(db: DbSession, current_user: CurrentUser):
    categories = crud_document.list_categories(db, current_user.id)
    return ResponseModel.success(data=[CategoryStatOut(**c) for c in categories])


# =============================================================================
# 3. 检索测试（调优专用）
# =============================================================================
@router.post(
    "/retrieve",
    response_model=ResponseModel[dict],
    summary="知识库检索测试",
    description="""
**不经过大模型**，直接返回向量检索结果，用于：
- 验证文档是否已成功入库（搜不到说明向量化失败或权限不对）
- 调优 `RAG_TOP_K` / `RAG_CHUNK_SIZE` 参数，观察召回质量

⚠ 注意：本接口路径必须放在 `/{document_id}` **之前** 注册，
   否则 FastAPI 会把 "retrieve" 当成 document_id 解析而报 422。
    """,
)
def retrieve_test(
    db: DbSession,
    current_user: CurrentUser,
    query: str = Query(..., min_length=1, description="检索问题"),
    top_k: int = Query(default=5, ge=1, le=20, description="召回数量"),
    document_ids: list[int] | None = Query(default=None, description="限定文档范围"),
):
    results = vector_store.similarity_search_with_score(
        query=query,
        top_k=top_k,
        user_id=current_user.id,      # ← 权限过滤，只能检索到自己可见的文档
        document_ids=document_ids,
    )
    return ResponseModel.success(
        data={
            "query": query,
            "count": len(results),
            "results": [
                {
                    "score": round(score, 4),
                    "document_id": doc.metadata.get("document_id"),
                    "filename": doc.metadata.get("filename"),
                    "chunk_index": doc.metadata.get("chunk_index"),
                    "content": doc.page_content,
                }
                for doc, score in results
            ],
        }
    )


# =============================================================================
# 4. 文档详情 / 切片 / 修改 / 删除
# =============================================================================
@router.get(
    "/{document_id}",
    response_model=ResponseModel[DocumentOut],
    summary="文档详情",
)
def get_document(
    document_id: int,
    db: DbSession,
    current_user: CurrentUser,
):
    document = document_service.get_document_or_404(db, document_id, current_user.id)
    return ResponseModel.success(data=DocumentOut.model_validate(document))


@router.get(
    "/{document_id}/chunks",
    response_model=PageResponseModel[ChunkOut],
    summary="查看文档切片",
    description="展示文档被切成了哪些片段，用于判断 chunk_size 设置是否合理。",
)
def list_document_chunks(
    document_id: int,
    db: DbSession,
    current_user: CurrentUser,
    pagination: PaginationQuery = Depends(),
):
    document = document_service.get_document_or_404(db, document_id, current_user.id)
    chunks, total = crud_document.list_chunks(
        db, document.id, skip=pagination.skip, limit=pagination.limit
    )
    return ResponseModel.success(
        data=PageResult.create(
            items=[ChunkOut.model_validate(c) for c in chunks],
            total=total,
            page=pagination.page,
            page_size=pagination.page_size,
        )
    )


@router.put(
    "/{document_id}",
    response_model=ResponseModel[DocumentOut],
    summary="修改文档信息",
    description="""
修改标题、分类、标签、可见性。

⚠ 修改 `is_public` 后需要调用「重新索引」接口才能让向量库里的权限标记同步更新
   （因为向量检索依赖 metadata 里的 is_public 字段做过滤）。
    """,
)
def update_document(
    document_id: int,
    payload: DocumentUpdate,
    db: DbSession,
    current_user: CurrentUser,
):
    document = document_service.get_document_or_404(db, document_id, current_user.id)
    _check_owner_or_admin(document, current_user)

    document = crud_document.update(db, document, payload)
    return ResponseModel.success(
        data=DocumentOut.model_validate(document), message="文档信息更新成功"
    )


@router.delete(
    "/{document_id}",
    response_model=ResponseModel[MessageOutSimple],
    summary="删除文档",
    description="删除文档记录，**同步清理向量库中的对应向量**，避免问答时引用已删除的内容。",
)
def delete_document(
    document_id: int,
    db: DbSession,
    current_user: CurrentUser,
    hard: bool = Query(default=False, description="是否物理删除（连同切片与原文件）"),
    ip: str = Depends(get_client_ip),
):
    document = document_service.get_document_or_404(db, document_id, current_user.id)
    _check_owner_or_admin(document, current_user)

    filename = document.filename
    document_service.delete_document(db, document, hard=hard)

    crud_audit_log.write(
        db,
        action="delete_document",
        user_id=current_user.id,
        username=current_user.username,
        resource="document",
        resource_id=document_id,
        detail={"filename": filename, "hard_delete": hard},
        ip=ip,
        success=True,
        message="删除文档（含向量清理）",
    )
    return ResponseModel.success(
        data=MessageOutSimple(message=f"文档「{filename}」已删除"), message="文档删除成功"
    )


@router.post(
    "/{document_id}/reindex",
    response_model=ResponseModel[DocumentOut],
    summary="重新索引文档",
    description="""
重新解析 + 重新向量化。使用场景：
- 文档处理失败，修复后重试
- 更换了 Embedding 模型（必须先清空向量库：见 scripts/reindex.py）
- 修改了切片参数（RAG_CHUNK_SIZE）后想让新参数生效
    """,
)
def reindex_document(
    document_id: int,
    db: DbSession,
    current_user: CurrentUser,
):
    document = document_service.get_document_or_404(db, document_id, current_user.id)
    _check_owner_or_admin(document, current_user)

    document = document_service.reindex_document(db, document)
    if document.status == "failed":
        return ResponseModel.fail(
            code=4004,
            message=f"重新索引失败：{document.error_message}",
            data=DocumentOut.model_validate(document),
        )
    return ResponseModel.success(
        data=DocumentOut.model_validate(document),
        message=f"重新索引成功，共生成 {document.chunk_count} 个切片",
    )
