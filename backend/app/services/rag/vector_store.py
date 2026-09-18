"""
==============================================================================
 向量库服务（Chroma）
==============================================================================

本模块职责：封装对 Chroma 向量数据库的所有操作，业务层不直接接触 Chroma API。

    - get_vector_store()     获取向量库实例（单例，避免重复初始化）
    - add_chunks(...)        把文档切片向量化并写入
    - delete_document(...)   删除某文档的所有向量
    - similarity_search(...) 相似度检索（带用户可见性过滤）
    - get_collection_stats() 集合统计

【为什么用 Chroma？】
    - 纯 Python 实现，pip 安装即用，无需额外部署服务
    - 数据落盘到本地目录，重启不丢
    - 与 LangChain 深度集成，一行代码拿到 VectorStore 对象
    数据量大（百万级向量）时再考虑 Milvus / Qdrant，本项目规模用 Chroma 足够。

【关于 collection 与 embedder（重要配置点）】
    Embedding 模型在 .env 里配置（EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL）。
    ⚠ 一旦写入过向量，就【不能】随意更换 Embedding 模型，
      因为不同模型的向量空间不兼容，换了之后检索结果会完全错乱。
      更换模型时必须清空向量库重新索引（见 scripts/reindex.py）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_chroma import Chroma
from langchain_core.documents import Document as LCDocument
from langchain_openai import OpenAIEmbeddings

from app.core.config import settings
from app.core.exceptions import ExternalServiceException
from app.core.logging_config import logger
from app.core.response import BusinessCode


# =============================================================================
# 1. Embedding 模型（把文字转成向量）
# =============================================================================
@lru_cache(maxsize=1)
def get_embeddings() -> OpenAIEmbeddings:
    """
    创建 Embedding 客户端（单例）。

    ★ 配置位置：backend/.env
        EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL

    使用 OpenAI 兼容协议，因此 SiliconFlow、通义、OpenAI 等都能直接接入。
    check_embedding_ctx_length=False 是为了兼容部分不实现该接口的国产服务，
    避免它们因为多传了一个参数而报 400。
    """
    logger.info(
        "初始化 Embedding 模型 | base_url={} | model={}",
        settings.EMBEDDING_BASE_URL,
        settings.EMBEDDING_MODEL,
    )
    return OpenAIEmbeddings(
        base_url=settings.EMBEDDING_BASE_URL,
        api_key=settings.EMBEDDING_API_KEY,  # type: ignore[arg-type]
        model=settings.EMBEDDING_MODEL,
        # 部分兼容服务不支持该参数，关闭以保证最大兼容性
        check_embedding_ctx_length=False,
        chunk_size=16,   # 每批向量化 16 条，避免请求体过大被服务端拒绝
        max_retries=settings.LLM_MAX_RETRIES,
        timeout=settings.LLM_TIMEOUT,
    )


# =============================================================================
# 2. 向量库实例
# =============================================================================
@lru_cache(maxsize=1)
def get_vector_store() -> Chroma:
    """
    创建 / 打开 Chroma 集合（单例）。

    数据落盘目录由 .env 的 CHROMA_PERSIST_DIR 指定，默认 backend/storage/chroma。
    目录不存在会自动创建，所以第一次运行不需要手动建目录。
    """
    persist_dir = Path(settings.CHROMA_PERSIST_DIR)
    persist_dir.mkdir(parents=True, exist_ok=True) # 父目录不存在也创建

    logger.info(
        "打开 Chroma 向量库 | 目录={} | 集合={}", persist_dir, settings.RAG_COLLECTION
    )
    return Chroma(
        collection_name=settings.RAG_COLLECTION,
        embedding_function=get_embeddings(),
        persist_directory=str(persist_dir),
        # cosine 距离：适合文本语义相似度（默认 l2 在归一化向量上等价，但显式指定更清晰）
        collection_metadata={"hnsw:space": "cosine"},
    )


# =============================================================================
# 3. 写入向量
# =============================================================================
def _build_vector_id(document_id: int, chunk_index: int) -> str:
    """
    生成向量 ID，规则：doc{document_id}_chunk{chunk_index}。
    好处：既唯一，又能从 ID 反推出属于哪篇文档，便于按文档删除。
    """
    return f"doc{document_id}_chunk{chunk_index}"


def add_chunks(
    document_id: int,
    chunks: list[str],
    metadatas: list[dict[str, Any]] | None = None,
) -> list[str]:
    """
    把文档切片向量化并写入 Chroma。

    :param document_id: 文档 ID
    :param chunks:      切片文本列表
    :param metadatas:   每个切片的元数据（至少包含 owner_id / is_public 用于权限过滤）
    :return: 写入的向量 ID 列表

    【metadata 里必须带 owner_id 与 is_public】
        检索时靠它做「只看得到自己有权限的文档」的过滤。
        这是 RAG 系统最常见的越权漏洞来源，务必保留。
    """
    if not chunks:
        return []

    store = get_vector_store()
    ids = [_build_vector_id(document_id, i) for i in range(len(chunks))]
    metadatas = metadatas or [{} for _ in chunks]

    # 补齐每个 metadata 的必备字段
    for meta in metadatas:
        meta.setdefault("document_id", document_id)

    try:
        store.add_texts(texts=chunks, metadatas=metadatas, ids=ids)
        logger.info("向量写入成功 | document_id={} | 切片数={}", document_id, len(ids))
        return ids
    except Exception as exc:
        logger.exception("向量写入失败 | document_id={}", document_id)
        raise ExternalServiceException(
            message=f"向量化失败：{exc}", code=BusinessCode.VECTOR_STORE_ERROR
        )


def delete_document(document_id: int) -> int:
    """
    删除某文档的全部向量。

    注意：Chroma 的 delete 支持按 metadata 过滤删除，
         这里用 where={"document_id": id} 精确匹配。
    调用时机：删除知识库文档时，必须先删向量再删 MySQL 记录，
             否则会出现「文档没了但问答还能引用」的脏数据。
    """
    store = get_vector_store()
    try:
        # 先统计要删多少（Chroma 的 delete 不返回删除数量）
        before = store.get(where={"document_id": document_id})
        count = len(before.get("ids", []) or [])

        if count:
            store.delete(where={"document_id": document_id})
        logger.info("向量删除完成 | document_id={} | 删除数={}", document_id, count)
        return count
    except Exception as exc:
        logger.exception("向量删除失败 | document_id={}", document_id)
        raise ExternalServiceException(
            message=f"删除向量失败：{exc}", code=BusinessCode.VECTOR_STORE_ERROR
        )


# =============================================================================
# 4. 相似度检索
# =============================================================================
def similarity_search_with_score(
    query: str,
    top_k: int | None = None,
    user_id: int | None = None,
    document_ids: list[int] | None = None,
) -> list[tuple[LCDocument, float]]:
    """
    相似度检索，返回 [(文档片段, 相似度得分), ...]。

    :param query:        用户问题
    :param top_k:        召回数量，默认取 .env 的 RAG_TOP_K
    :param user_id:      当前用户 ID，用于权限过滤（只召回 is_public 或自己的文档）
    :param document_ids: 限定检索的文档范围（可选，对应前端「只在选中文档中提问」）

    【权限过滤的实现方式】
        Chroma 的 where 支持逻辑运算，这里构造：
            $or: [ {"is_public": True}, {"owner_id": user_id} ]
        与 crud_document._visibility_condition 的逻辑完全一致，
        保证「能看到」与「能检索到」两个范围严格相等。

    ⚠ 关于得分：Chroma 返回的是【距离】，越小越相似。
      这里统一转换成「相似度得分」（越大越相关），前端展示更直观：
          score = 1 - distance
    """
    store = get_vector_store()
    k = top_k or settings.RAG_TOP_K

    # ---- 构造过滤条件 ----
    where: dict[str, Any] | None = None
    conditions: list[dict[str, Any]] = []

    if user_id is not None:
        conditions.append(
            {"$or": [{"is_public": True}, {"owner_id": int(user_id)}]}
        )
    if document_ids:
        conditions.append({"document_id": {"$in": [int(i) for i in document_ids]}})

    if len(conditions) == 1:
        where = conditions[0]
    elif len(conditions) > 1:
        where = {"$and": conditions}

    try:
        results = store.similarity_search_with_score(query, k=k, filter=where)
    except Exception as exc:
        logger.exception("向量检索失败 | query={}", query[:50])
        raise ExternalServiceException(
            message=f"知识库检索失败：{exc}", code=BusinessCode.VECTOR_STORE_ERROR
        )

    # 距离 → 相似度得分
    converted = [(doc, max(0.0, 1.0 - float(dist))) for doc, dist in results]
    logger.debug(
        "向量检索完成 | 召回={} 条 | 最高分={}",
        len(converted),
        round(converted[0][1], 3) if converted else "N/A",
    )
    return converted


def get_collection_stats() -> dict[str, Any]:
    """获取向量库统计信息（知识库页面展示、健康检查用）。"""
    try:
        store = get_vector_store()
        data = store.get()
        return {
            "collection": settings.RAG_COLLECTION,
            "persist_dir": settings.CHROMA_PERSIST_DIR,
            "vector_count": len(data.get("ids", []) or []),
            "embedding_model": settings.EMBEDDING_MODEL,
        }
    except Exception as exc:  # pragma: no cover
        logger.error("获取向量库统计失败：{}", exc)
        return {"collection": settings.RAG_COLLECTION, "vector_count": 0, "error": str(exc)}


def reset_collection() -> None:
    """
    清空整个向量集合。

    ⚠ 危险操作：更换 Embedding 模型后必须执行，
      并在清空后重新索引所有文档（scripts/reindex.py 会做这件事）。
    """
    store = get_vector_store()
    data = store.get()
    ids = data.get("ids", []) or []
    if ids:
        store.delete(ids=ids)
    logger.warning("向量集合已清空 | 删除向量数={}", len(ids))
