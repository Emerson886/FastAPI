"""
==============================================================================
 知识库重建索引脚本（reindex.py）
==============================================================================

【什么时候必须运行这个脚本？】
    1. ★ 更换了 Embedding 模型 ★（最重要）
       不同模型产生的向量位于不同向量空间，混在一起检索结果会完全错乱。
       必须清空向量库，再用新模型重新索引所有文档。
    2. 修改了 RAG_CHUNK_SIZE / RAG_CHUNK_OVERLAP 后想立即全局生效
    3. 向量库文件损坏或与 MySQL 数据不一致时
    4. 批量修复状态为 failed 的文档

运行方式（在 backend 目录下）：
    # 预览将要处理哪些文档（不实际执行）
    C:/Users/thunderfish/miniconda3/envs/langchain/python.exe scripts/reindex.py --dry-run

    # 全量重建（会先清空向量库！）
    C:/Users/thunderfish/miniconda3/envs/langchain/python.exe scripts/reindex.py --reset

    # 只重试失败的文档
    C:/Users/thunderfish/miniconda3/envs/langchain/python.exe scripts/reindex.py --only-failed

    # 只处理指定文档
    C:/Users/thunderfish/miniconda3/envs/langchain/python.exe scripts/reindex.py --doc-id 3

⚠ 全量重建会重复消耗 Embedding 额度，请确认有足够配额。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.logging_config import logger, setup_logging  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.document import Document, DocumentStatus  # noqa: E402
from app.services import document_service  # noqa: E402
from app.services.rag import vector_store  # noqa: E402


def collect_documents(db, only_failed: bool, doc_ids: list[int] | None) -> list[Document]:
    """筛选需要重新索引的文档。"""
    stmt = select(Document).where(Document.is_deleted.is_(False))
    if only_failed:
        stmt = stmt.where(Document.status == DocumentStatus.FAILED)
    if doc_ids:
        stmt = stmt.where(Document.id.in_(doc_ids))
    return list(db.execute(stmt.order_by(Document.id).scalars()).all())


def main() -> int:
    parser = argparse.ArgumentParser(description="重建知识库向量索引")
    parser.add_argument("--reset", action="store_true",
                        help="先清空整个向量集合（换 Embedding 模型时必须加）")
    parser.add_argument("--only-failed", action="store_true", help="只处理失败的文档")
    parser.add_argument("--doc-id", type=int, action="append", dest="doc_ids",
                        help="只处理指定文档 ID，可重复传入")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不实际执行")
    args = parser.parse_args()

    setup_logging()

    logger.info("=" * 74)
    logger.info("  知识库重建索引")
    logger.info("  Embedding 模型：{}", settings.EMBEDDING_MODEL)
    logger.info("  Embedding 地址：{}", settings.EMBEDDING_BASE_URL)
    logger.info("  向量库目录：  {}", settings.CHROMA_PERSIST_DIR)
    logger.info("  切片参数：    chunk_size={} overlap={}",
                settings.RAG_CHUNK_SIZE, settings.RAG_CHUNK_OVERLAP)
    logger.info("=" * 74)

    db = SessionLocal()
    try:
        documents = collect_documents(db, args.only_failed, args.doc_ids)
        if not documents:
            logger.info("没有需要处理的文档，退出。")
            return 0

        logger.info("待处理文档 {} 篇：", len(documents))
        for doc in documents:
            logger.info("  - [{}] {} | 状态={} | 切片={}",
                        doc.id, doc.filename, doc.status, doc.chunk_count)

        if args.dry_run:
            logger.info("--dry-run 模式，未执行实际操作。")
            return 0

        # ---- 清空向量库 ----
        if args.reset:
            answer = input("⚠ 确认清空整个向量集合并重建吗？输入 YES 继续：")
            if answer.strip() != "YES":
                logger.info("已取消。")
                return 1
            vector_store.reset_collection()
            logger.warning("向量集合已清空。")

        # ---- 逐个重建 ----
        started = time.time()
        success, failed = 0, 0

        for index, doc in enumerate(documents, start=1):
            logger.info("[{}/{}] 处理中：{}", index, len(documents), doc.filename)
            # 不传 --reset 时，先删掉这篇文档的旧向量，避免重复
            if not args.reset:
                try:
                    vector_store.delete_document(doc.id)
                except Exception as exc:
                    logger.warning("清理旧向量失败（继续）：{}", exc)

            result = document_service.process_document(db, doc)
            if result.status == DocumentStatus.COMPLETED:
                success += 1
                logger.info("  ✓ 完成 | 切片={}", result.chunk_count)
            else:
                failed += 1
                logger.error("  ✗ 失败 | {}", result.error_message)

        elapsed = time.time() - started
        logger.info("=" * 74)
        logger.info("  重建完成 | 成功 {} 篇 | 失败 {} 篇 | 耗时 {:.1f}s", success, failed, elapsed)
        logger.info("=" * 74)
        return 0 if failed == 0 else 1

    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
