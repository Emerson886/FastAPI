"""
==============================================================================
 RAG 服务包
==============================================================================

模块划分：
    vector_store.py  Chroma 向量库操作（写入 / 检索 / 删除）
    loader.py        文件解析与文本切片
    prompts.py★      提示词（想改 AI 回答风格就改这里）
    retriever.py     LangChain Retriever 封装（供工具链复用）
"""

from app.services.rag.loader import (
    build_chunk_metadatas,
    calculate_file_hash,
    parse_file,
    process_document_content,
    split_text,
)
from app.services.rag.prompts import (
    NO_CONTEXT_PLACEHOLDER,
    RETRIEVER_TOOL_DESCRIPTION,
    SYSTEM_PROMPT,
)
from app.services.rag.vector_store import (
    add_chunks,
    delete_document,
    get_collection_stats,
    get_embeddings,
    get_vector_store,
    reset_collection,
    similarity_search_with_score,
)

__all__ = [
    "add_chunks",
    "delete_document",
    "get_collection_stats",
    "get_embeddings",
    "get_vector_store",
    "reset_collection",
    "similarity_search_with_score",
    "parse_file",
    "split_text",
    "process_document_content",
    "build_chunk_metadatas",
    "calculate_file_hash",
    "SYSTEM_PROMPT",
    "NO_CONTEXT_PLACEHOLDER",
    "RETRIEVER_TOOL_DESCRIPTION",
]
