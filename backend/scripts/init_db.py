"""
==============================================================================
 数据库初始化脚本
==============================================================================

用途：手动建表 + 创建初始管理员（不想自动建表时使用）。

运行方式（在 backend 目录下）：
    C:/Users/thunderfish/miniconda3/envs/langchain/python.exe scripts/init_db.py

可选参数：
    --drop    先删除所有表再重建（⚠ 会清空数据！仅开发环境使用）

【前置条件】
    1. MySQL 已启动
    2. 数据库已创建：
       CREATE DATABASE kb_assistant DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
    3. backend/.env 中的 MYSQL_* 配置正确
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 把 backend 目录加入 sys.path，让脚本能 import app 包
BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings  # noqa: E402
from app.core.logging_config import logger, setup_logging  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.init_db import init_data, init_db  # noqa: E402
from app.db.session import engine  # noqa: E402

# 必须导入模型包，模型才会注册到 Base.metadata
import app.models  # noqa: E402, F401


def show_tables() -> None:
    """打印即将创建的表清单。"""
    logger.info("共发现 {} 张表：", len(Base.metadata.tables))
    for index, table_name in enumerate(sorted(Base.metadata.tables.keys()), start=1):
        table = Base.metadata.tables[table_name]
        logger.info("  {:>2}. {:<22} ({} 个字段)", index, table_name, len(table.columns))


def drop_all() -> None:
    """删除所有表（危险操作）。"""
    confirm = input("⚠ 确认要删除所有表并清空数据吗？输入 YES 继续：")
    if confirm.strip() != "YES":
        logger.info("已取消删除操作")
        return
    Base.metadata.drop_all(bind=engine)
    logger.warning("所有表已删除")


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化企业知识库问答助手数据库")
    parser.add_argument("--drop", action="store_true", help="先删除所有表再重建（危险）")
    parser.add_argument("--no-admin", action="store_true", help="不创建初始管理员账号")
    args = parser.parse_args()

    setup_logging()

    logger.info("=" * 70)
    logger.info("  数据库初始化 | 目标：{}:{}/{}",
                settings.MYSQL_HOST, settings.MYSQL_PORT, settings.MYSQL_DB)
    logger.info("=" * 70)

    show_tables()

    if args.drop:
        drop_all()

    try:
        init_db()
        if not args.no_admin:
            init_data()
    except Exception as exc:
        logger.error("-" * 70)
        logger.error("初始化失败：{}", exc)
        logger.error("排查清单：")
        logger.error("  1. MySQL 服务是否启动？")
        logger.error("  2. 数据库「{}」是否已创建？", settings.MYSQL_DB)
        logger.error("     建库命令：CREATE DATABASE {} DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;",
                     settings.MYSQL_DB)
        logger.error("  3. backend/.env 里的 MYSQL_USER / MYSQL_PASSWORD 是否正确？")
        logger.error("  4. 当前用户是否有建表权限？")
        logger.error("-" * 70)
        return 1

    logger.info("=" * 70)
    logger.info("  ✓ 数据库初始化完成")
    logger.info("  初始管理员：{} / {}",
                settings.FIRST_ADMIN_USERNAME, settings.FIRST_ADMIN_PASSWORD)
    logger.info("  ★ 请登录后立即修改密码 ★")
    logger.info("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
