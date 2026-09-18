"""
==============================================================================
 环境自检脚本（preflight_check.py）
==============================================================================

【用途】
    在启动服务前，一次性检查「依赖是否装齐、配置是否正确、数据库能否连通、
    大模型与向量化服务能否调通」，把问题在启动前就暴露出来，
    避免启动到一半才报错、或者用户提问后才发现 Key 没配。

【运行方式】
    cd my_new_project/backend
    C:/Users/thunderfish/miniconda3/envs/langchain/python.exe scripts/preflight_check.py

【可选参数】
    --online    真实调用大模型与 Embedding 接口做连通性测试（会产生极小费用）
    --skip-db   跳过数据库检查
"""

from __future__ import annotations

import argparse
import importlib
import sys
import traceback
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

# ---------------------------------------------------------------------------
# 输出辅助（不依赖 loguru，因为 loguru 本身可能就是缺失的依赖）
# ---------------------------------------------------------------------------
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def fail(msg: str, hint: str = "") -> None:
    print(f"  {RED}✗{RESET} {msg}")
    if hint:
        print(f"      {YELLOW}→ {hint}{RESET}")


def warn(msg: str, hint: str = "") -> None:
    print(f"  {YELLOW}!{RESET} {msg}")
    if hint:
        print(f"      {YELLOW}→ {hint}{RESET}")


def title(msg: str) -> None:
    print(f"\n{BOLD}{CYAN}{msg}{RESET}")
    print(f"{CYAN}{'─' * 72}{RESET}")


# ===========================================================================
# 第一部分：Python 依赖检查
# ===========================================================================
# 每项：(import 名, pip 包名, 用途说明, 是否必需)
REQUIRED_PACKAGES: list[tuple[str, str, str, bool]] = [
    # ---- Web 框架 ----
    ("fastapi", "fastapi", "Web 框架", True),
    ("uvicorn", "uvicorn", "ASGI 服务器", True),
    ("multipart", "python-multipart", "文件上传（UploadFile/Form）", True),
    # ---- 数据校验 ----
    ("pydantic", "pydantic", "数据校验", True),
    ("pydantic_settings", "pydantic-settings", "配置管理", True),
    ("email_validator", "email-validator", "EmailStr 校验", True),
    # ---- 数据库 ----
    ("sqlalchemy", "SQLAlchemy", "ORM", True),
    ("pymysql", "PyMySQL", "MySQL 驱动", True),
    # ---- 安全 ----
    ("jwt", "PyJWT", "JWT 令牌", True),
    ("bcrypt", "bcrypt", "密码哈希", True),
    # ---- 日志 ----
    ("loguru", "loguru", "日志", True),
    # ---- LangChain / Agent ----
    ("langchain", "langchain", "Agent 框架", True),
    ("langgraph", "langgraph", "Agent 图引擎", True),
    ("langchain_openai", "langchain-openai", "OpenAI 兼容客户端", True),
    # ---- 向量库 ----
    ("langchain_chroma", "langchain-chroma", "LangChain 的 Chroma 集成", True),
    ("chromadb", "chromadb", "向量数据库", True),
    # ---- 文档解析 ----
    ("pypdf", "pypdf", "PDF 解析", False),
    ("docx", "python-docx", "Word 解析", False),
]


def check_dependencies() -> tuple[int, int]:
    """检查依赖包是否安装齐全。返回 (缺失必需数, 缺失可选数)。"""
    title("① Python 依赖检查")

    missing_required: list[str] = []
    missing_optional: list[str] = []

    for module_name, pip_name, purpose, required in REQUIRED_PACKAGES:
        try:
            module = importlib.import_module(module_name)
            version = getattr(module, "__version__", "")
            ok(f"{pip_name:<22} {version:<12} {purpose}")
        except ImportError:
            if required:
                missing_required.append(pip_name)
                fail(f"{pip_name:<22} {'未安装':<12} {purpose}")
            else:
                missing_optional.append(pip_name)
                warn(f"{pip_name:<22} {'未安装':<12} {purpose}（可选：对应格式的文档将无法解析）")

    if missing_required:
        print(f"\n  {RED}{BOLD}缺失必需依赖，请执行：{RESET}")
        print(f"  {CYAN}python -m pip install {' '.join(missing_required)}{RESET}")
        print(f"  {CYAN}# 网络慢时可用国内镜像：{RESET}")
        print(f"  {CYAN}python -m pip install {' '.join(missing_required)} -i https://pypi.tuna.tsinghua.edu.cn/simple{RESET}")

    if missing_optional:
        print(f"\n  {YELLOW}可选依赖未安装（不影响启动，仅影响对应格式）：{RESET}")
        print(f"  {CYAN}python -m pip install {' '.join(missing_optional)}{RESET}")

    return len(missing_required), len(missing_optional)


# ===========================================================================
# 第二部分：配置检查
# ===========================================================================
def check_config() -> list[str]:
    """检查 .env 配置项是否已填写。返回问题列表。"""
    title("② 配置文件检查")

    env_file = BACKEND_DIR / ".env"
    if not env_file.exists():
        fail("backend/.env 不存在")
        print(f"      {YELLOW}→ 请复制模板：copy .env.example .env{RESET}")
        return ["缺少 .env 文件"]

    ok(f"配置文件存在：{env_file}")

    problems: list[str] = []

    try:
        from app.core.config import settings
    except Exception as exc:
        fail(f"配置加载失败：{exc}")
        return ["配置加载失败"]

    # ---- 逐项检查 ----
    rows = [
        ("MySQL 地址", f"{settings.MYSQL_HOST}:{settings.MYSQL_PORT}/{settings.MYSQL_DB}", None),
        ("MySQL 密码", "*" * len(settings.MYSQL_PASSWORD), None),
        ("大模型地址", settings.LLM_BASE_URL, None),
        ("大模型模型", settings.LLM_MODEL, None),
        ("Embedding 地址", settings.EMBEDDING_BASE_URL, None),
        ("Embedding 模型", settings.EMBEDDING_MODEL, None),
        ("向量维度", str(settings.EMBEDDING_DIMENSION), None),
    ]
    for label, value, _ in rows:
        print(f"  {CYAN}·{RESET} {label:<14}: {value}")

    # ---- 关键校验 ----
    placeholder_marks = ("REPLACE_WITH", "请替换", "your_key", "sk-xxx", "xxxx")

    if any(mark.lower() in settings.LLM_API_KEY.lower() for mark in placeholder_marks):
        fail("LLM_API_KEY 还是占位符，未填写真实 Key")
        print(f"      {YELLOW}→ 编辑 backend/.env，修改 LLM_API_KEY{RESET}")
        problems.append("LLM_API_KEY 未配置")
    else:
        ok(f"LLM_API_KEY 已配置（{settings.LLM_API_KEY[:8]}...）")

    if any(mark.lower() in settings.EMBEDDING_API_KEY.lower() for mark in placeholder_marks):
        fail("EMBEDDING_API_KEY 还是占位符，未填写真实 Key")
        print(f"      {YELLOW}→ 编辑 backend/.env，修改 EMBEDDING_API_KEY{RESET}")
        problems.append("EMBEDDING_API_KEY 未配置")
    else:
        ok(f"EMBEDDING_API_KEY 已配置（{settings.EMBEDDING_API_KEY[:8]}...）")

    if "change-me" in settings.SECRET_KEY:
        warn("SECRET_KEY 还是默认值，生产环境存在安全风险")
        print(f"      {YELLOW}→ 生成随机密钥：python -c \"import secrets;print(secrets.token_urlsafe(48))\"{RESET}")
    else:
        ok("SECRET_KEY 已自定义")

    # ---- 逻辑校验：DeepSeek 没有 embedding 接口 ----
    if "api.deepseek.com" in settings.EMBEDDING_BASE_URL:
        fail("EMBEDDING_BASE_URL 指向 DeepSeek，但 DeepSeek 官方不提供 embedding 接口！")
        print(f"      {YELLOW}→ 请改用 SiliconFlow / 通义 / OpenAI 的 embedding 服务{RESET}")
        print(f"      {YELLOW}→ 详见 docs/配置修改指南.md 第四节{RESET}")
        problems.append("Embedding 服务选择错误")

    if settings.EMBEDDING_MODEL and "bge-m3" in settings.EMBEDDING_MODEL.lower():
        if settings.EMBEDDING_DIMENSION != 1024:
            warn(f"bge-m3 的向量维度应为 1024，当前配置为 {settings.EMBEDDING_DIMENSION}")

    if not settings.SQLALCHEMY_DATABASE_URI:
        problems.append("数据库连接串生成失败")

    return problems


# ===========================================================================
# 第三部分：数据库连通性
# ===========================================================================
def check_database() -> list[str]:
    """检查 MySQL 连接与建库情况。返回问题列表。"""
    title("③ MySQL 数据库检查")

    try:
        from sqlalchemy import text

        from app.core.config import settings
        from app.db.session import engine
    except Exception as exc:
        fail(f"数据库模块加载失败：{exc}")
        return ["数据库模块加载失败"]

    # ---- 1. 能否连接 ----
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT VERSION()")).scalar()
            ok(f"连接成功 | MySQL {version}")
    except Exception as exc:
        error_text = str(exc)
        fail(f"连接失败：{error_text[:200]}")
        if "Unknown database" in error_text:
            print(f"      {YELLOW}→ 数据库「{settings.MYSQL_DB}」不存在，请执行：{RESET}")
            print(
                f"      {CYAN}CREATE DATABASE {settings.MYSQL_DB} "
                f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;{RESET}"
            )
        elif "Access denied" in error_text:
            print(f"      {YELLOW}→ 密码或用户名错误，请检查 backend/.env 的 MYSQL_USER / MYSQL_PASSWORD{RESET}")
        elif "Can't connect" in error_text or "Connection refused" in error_text:
            print(f"      {YELLOW}→ MySQL 服务未启动。Windows 下执行：net start MySQL80{RESET}")
        return ["数据库连接失败"]

    # ---- 2. 表是否已创建 ----
    try:
        import app.models  # noqa: F401
        from app.db.base import Base

        with engine.connect() as conn:
            existing = set(conn.dialect.get_table_names(conn))

        expected = set(Base.metadata.tables.keys())
        missing = expected - existing

        if missing:
            warn(f"以下表尚未创建（启动后端时会自动创建）：{', '.join(sorted(missing))}")
        else:
            ok(f"数据表齐全 | 共 {len(existing & expected)} 张：{', '.join(sorted(expected))}")

        unexpected = existing - expected
        if unexpected:
            print(f"  {CYAN}·{RESET} 库中还有其他表：{', '.join(sorted(unexpected))}")
    except Exception as exc:
        warn(f"检查数据表失败：{exc}")

    return []


# ===========================================================================
# 第四部分：向量库检查
# ===========================================================================
def check_vector_store() -> list[str]:
    """检查 Chroma 向量库能否初始化。"""
    title("④ 向量库检查")

    try:
        from app.core.config import settings
        from app.services.rag.vector_store import get_collection_stats

        stats = get_collection_stats()
        ok(f"Chroma 初始化成功 | 目录={settings.CHROMA_PERSIST_DIR}")
        print(f"  {CYAN}·{RESET} 集合名称  : {stats.get('collection')}")
        print(f"  {CYAN}·{RESET} 已有向量数: {stats.get('vector_count')}")
        print(f"  {CYAN}·{RESET} Embedding : {stats.get('embedding_model')}")
        return []
    except ImportError as exc:
        fail(f"向量库依赖缺失：{exc}")
        print(f"      {YELLOW}→ pip install langchain-chroma chromadb{RESET}")
        return ["向量库依赖缺失"]
    except Exception as exc:
        fail(f"向量库初始化失败：{str(exc)[:200]}")
        return ["向量库初始化失败"]


# ===========================================================================
# 第五部分：在线连通性测试（可选，会真实调用 API）
# ===========================================================================
def check_online() -> list[str]:
    """真实调用大模型与 Embedding 接口。"""
    title("⑤ 在线服务测试（--online，会产生极小费用）")

    problems: list[str] = []

    # ---- 大模型 ----
    try:
        from app.services.llm import check_llm_available

        result = check_llm_available()
        if result.get("available"):
            ok(f"大模型连通 | {result.get('model')} | 回复：{result.get('reply')}")
        else:
            fail(f"大模型调用失败：{str(result.get('error'))[:200]}")
            problems.append("大模型不可用")
    except Exception as exc:
        fail(f"大模型测试异常：{str(exc)[:200]}")
        problems.append("大模型异常")

    # ---- Embedding ----
    try:
        from app.services.rag.vector_store import get_embeddings

        vector = get_embeddings().embed_query("连通性测试")
        ok(f"Embedding 连通 | 返回向量维度={len(vector)}（配置值为 1024/1536 等，需与实际一致）")
    except Exception as exc:
        fail(f"Embedding 调用失败：{str(exc)[:200]}")
        problems.append("Embedding 不可用")

    return problems


# ===========================================================================
# 第六部分：应用整体导入测试
# ===========================================================================
def check_app_import() -> list[str]:
    """检查整个 FastAPI 应用能否成功导入（能导入说明没有语法/循环导入问题）。"""
    title("⑥ 应用完整性检查")

    try:
        from app.main import app

        routes = [r for r in app.routes if hasattr(r, "methods")]
        ok(f"FastAPI 应用加载成功 | 共注册 {len(routes)} 个接口")

        # 按标签分组打印接口
        from collections import defaultdict

        groups: dict[str, list[str]] = defaultdict(list)
        for route in app.routes:
            tags = getattr(route, "tags", None)
            if not tags or not hasattr(route, "methods"):
                continue
            methods = "/".join(sorted(route.methods - {"HEAD", "OPTIONS"}))
            groups[tags[0]].append(f"{methods} {route.path}")

        for tag, items in groups.items():
            print(f"\n  {BOLD}{tag}{RESET}（{len(items)} 个）")
            for item in sorted(items):
                print(f"    {CYAN}·{RESET} {item}")

        return []
    except Exception as exc:
        fail(f"应用加载失败：{exc}")
        print(f"\n{RED}{traceback.format_exc()}{RESET}")
        return ["应用加载失败"]


# ===========================================================================
# 主流程
# ===========================================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="企业知识库问答助手 - 环境自检")
    parser.add_argument("--online", action="store_true", help="真实调用大模型/Embedding 测试连通性")
    parser.add_argument("--skip-db", action="store_true", help="跳过数据库检查")
    args = parser.parse_args()

    print(f"\n{BOLD}{'=' * 72}{RESET}")
    print(f"{BOLD}  企业知识库问答助手 · 环境自检{RESET}")
    print(f"{BOLD}  Python: {sys.version.split()[0]} | 路径: {sys.executable}{RESET}")
    print(f"{BOLD}{'=' * 72}{RESET}")

    all_problems: list[str] = []

    missing_required, _ = check_dependencies()
    if missing_required:
        # 依赖缺失时后面的检查没意义（导入会失败）
        print(f"\n{RED}{BOLD}请先安装缺失的依赖，然后重新运行本脚本。{RESET}\n")
        return 1

    all_problems += check_config()
    if not args.skip_db:
        all_problems += check_database()
    all_problems += check_vector_store()
    all_problems += check_app_import()

    if args.online:
        all_problems += check_online()

    # ------------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------------
    title("自检结果汇总")
    if not all_problems:
        print(f"  {GREEN}{BOLD}✓ 全部通过！可以启动服务了：{RESET}")
        print(f"  {CYAN}python app/main.py{RESET}")
        print(f"  {CYAN}接口文档：http://127.0.0.1:8000/docs{RESET}")
        print(f"\n  {YELLOW}建议再执行一次在线测试：python scripts/preflight_check.py --online{RESET}\n")
        return 0

    print(f"  {RED}{BOLD}发现 {len(all_problems)} 个问题：{RESET}")
    for index, problem in enumerate(all_problems, start=1):
        print(f"    {index}. {problem}")
    print(f"\n  {YELLOW}详细排查步骤见 docs/配置修改指南.md 第九节{RESET}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
