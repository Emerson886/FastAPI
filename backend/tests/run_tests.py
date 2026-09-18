"""
==============================================================================
 测试运行入口（run_tests.py）
==============================================================================

【用途】
    一键运行项目的全部自检与单元测试，无需额外安装 pytest。

【运行方式】
    cd my_new_project/backend

    # 模式一：标准运行（已安装全部依赖，推荐）
    C:/Users/thunderfish/miniconda3/envs/langchain/python.exe tests/run_tests.py

    # 模式二：离线运行（尚未安装 loguru/bcrypt/PyJWT/langchain-chroma 时）
    C:/Users/thunderfish/miniconda3/envs/langchain/python.exe tests/run_tests.py --offline

    # 模式三：只跑某一组测试
    python tests/run_tests.py --only crud

【测试内容】
    1. 环境检查  —— Python 版本、依赖完整性、配置加载
    2. 架构自检  —— ORM 模型、Pydantic Schema、CRUD、FastAPI 路由与鉴权声明
    3. 单元测试  —— 用户/会话/消息/文档的 CRUD 与【用户数据隔离】安全测试

【测试数据库】
    使用 SQLite 内存库（见 conftest_helpers.py），不依赖 MySQL、不留垃圾文件，
    并已开启 SQLite 外键约束，保证级联删除行为与 MySQL 生产环境一致。
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
STUBS_DIR = TESTS_DIR / "stubs"

# ---------------------------------------------------------------------------
# --offline 模式：把桩模块目录插到 sys.path 最前，遮蔽未安装的真实包
# ---------------------------------------------------------------------------
if "--offline" in sys.argv:
    sys.path.insert(0, str(STUBS_DIR))

sys.path.insert(0, str(BACKEND_DIR))

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def banner(text: str) -> None:
    print(f"\n{BOLD}{CYAN}{'=' * 74}{RESET}")
    print(f"{BOLD}{CYAN}  {text}{RESET}")
    print(f"{BOLD}{CYAN}{'=' * 74}{RESET}")


def section(text: str) -> None:
    print(f"\n{BOLD}▌ {text}{RESET}")


# ===========================================================================
# 第一组：环境检查
# ===========================================================================
REQUIRED = [
    ("fastapi", "fastapi", True),
    ("uvicorn", "uvicorn", True),
    ("multipart", "python-multipart", True),
    ("pydantic", "pydantic", True),
    ("pydantic_settings", "pydantic-settings", True),
    ("email_validator", "email-validator", True),
    ("sqlalchemy", "SQLAlchemy", True),
    ("pymysql", "PyMySQL", True),
    ("jwt", "PyJWT", True),
    ("bcrypt", "bcrypt", True),
    ("loguru", "loguru", True),
    ("langchain", "langchain", True),
    ("langgraph", "langgraph", True),
    ("langchain_openai", "langchain-openai", True),
    ("langchain_chroma", "langchain-chroma", True),
    ("chromadb", "chromadb", True),
    ("pypdf", "pypdf", False),
    ("docx", "python-docx", False),
]


def test_environment(offline: bool) -> tuple[int, list[str]]:
    """检查依赖与配置。返回 (失败数, 提示列表)。"""
    section("第一组：环境检查")

    print(f"  Python : {sys.version.split()[0]}")
    print(f"  解释器 : {sys.executable}")
    print(f"  模式   : {'离线（使用桩模块）' if offline else '标准（使用真实依赖）'}")

    # ---- 1. 依赖 ----
    missing_required: list[str] = []
    missing_optional: list[str] = []

    for module_name, pip_name, required in REQUIRED:
        try:
            module = importlib.import_module(module_name)
            version = getattr(module, "__version__", "")
            print(f"  {GREEN}✓{RESET} {pip_name:<20} {version}")
        except ImportError:
            if required:
                missing_required.append(pip_name)
                print(f"  {RED}✗{RESET} {pip_name:<20} 未安装")
            else:
                missing_optional.append(pip_name)
                print(f"  {YELLOW}!{RESET} {pip_name:<20} 未安装（可选）")

    hints: list[str] = []
    if missing_required:
        hints.append(
            f"缺失必需依赖，请执行：python -m pip install {' '.join(missing_required)}"
        )
        hints.append("国内网络可加镜像：-i https://pypi.tuna.tsinghua.edu.cn/simple")
        hints.append("或先用桩模块跑测试：python tests/run_tests.py --offline")
    if missing_optional:
        hints.append(f"可选依赖未装（影响对应格式解析）：{' '.join(missing_optional)}")

    # ---- 2. 配置 ----
    try:
        from app.core.config import settings

        print(f"\n  {GREEN}✓{RESET} 配置加载成功 | 环境={settings.ENVIRONMENT}")
        print(f"    数据库  : {settings.MYSQL_HOST}:{settings.MYSQL_PORT}/{settings.MYSQL_DB}")
        print(f"    大模型  : {settings.LLM_MODEL} @ {settings.LLM_BASE_URL}")
        print(f"    Embedding: {settings.EMBEDDING_MODEL} @ {settings.EMBEDDING_BASE_URL}")

        placeholders = ("REPLACE_WITH", "请替换", "change-me")
        if any(p.lower() in settings.LLM_API_KEY.lower() for p in placeholders):
            print(f"  {YELLOW}!{RESET} LLM_API_KEY 尚未填写（问答功能会失败）")
            hints.append("backend/.env 中的 LLM_API_KEY / EMBEDDING_API_KEY 还是占位符")
        if "api.deepseek.com" in settings.EMBEDDING_BASE_URL:
            print(f"  {RED}✗{RESET} EMBEDDING_BASE_URL 指向 DeepSeek，但它不提供 embedding 接口")
            hints.append("请为 EMBEDDING_* 单独配置一个支持向量化的服务（详见 docs/配置修改指南.md）")
    except Exception as exc:
        print(f"  {RED}✗{RESET} 配置加载失败：{exc}")
        hints.append(f"配置加载失败：{exc}")
        return 1, hints

    return (1 if missing_required else 0), hints


# ===========================================================================
# 第二组：架构自检
# ===========================================================================
def test_architecture() -> tuple[int, list[str]]:
    """检查 ORM 模型、Schema、CRUD、路由是否组装正确。"""
    section("第二组：架构自检")

    failures = 0
    hints: list[str] = []

    # ---- 1. ORM 模型 ----
    try:
        import app.models  # noqa: F401
        from app.db.base import Base

        tables = sorted(Base.metadata.tables.keys())
        expected = {
            "users", "conversations", "messages",
            "documents", "document_chunks", "audit_logs",
        }
        missing = expected - set(tables)
        assert not missing, f"缺少表：{missing}"

        total_cols = sum(len(Base.metadata.tables[t].columns) for t in tables)
        total_idx = sum(len(Base.metadata.tables[t].indexes) for t in tables)
        print(f"  {GREEN}✓{RESET} ORM 模型 {len(tables)} 张表 | 字段 {total_cols} | 索引 {total_idx}")
        for table in tables:
            print(f"      · {table:<18} {len(Base.metadata.tables[table].columns):>2} 字段")

        from sqlalchemy.orm import configure_mappers

        configure_mappers()
        print(f"  {GREEN}✓{RESET} 关系映射配置正确（无循环/悬空引用）")
    except Exception as exc:
        failures += 1
        print(f"  {RED}✗{RESET} ORM 模型检查失败：{exc}")
        hints.append(f"ORM 模型错误：{exc}")

    # ---- 2. MySQL 方言 DDL ----
    try:
        from sqlalchemy.dialects import mysql
        from sqlalchemy.schema import CreateIndex, CreateTable

        from app.db.base import Base

        dialect = mysql.dialect()
        statements = 0
        for table in Base.metadata.sorted_tables:
            CreateTable(table).compile(dialect=dialect)
            statements += 1
            for index in table.indexes:
                CreateIndex(index).compile(dialect=dialect)
                statements += 1
        print(f"  {GREEN}✓{RESET} MySQL 方言 DDL 编译通过（{statements} 条语句）")
    except Exception as exc:
        failures += 1
        print(f"  {RED}✗{RESET} MySQL DDL 编译失败：{exc}")
        hints.append(f"DDL 编译失败：{exc}")

    # ---- 2.5 MySQL 方言【查询】编译检查 ----
    # 【为什么单独检查这一项】
    #   SQLAlchemy 有些 API（如 .nullslast()）会生成 PostgreSQL/SQLite 支持、
    #   但 MySQL 不支持的语法。上面的 DDL 检查覆盖不到查询语句，
    #   而这类问题往往要等用户登录才暴露（会话列表接口直接 500，日志只写「数据库异常」）。
    try:
        from sqlalchemy import select
        from sqlalchemy.dialects import mysql

        from app.crud.conversation import order_nulls_last_desc
        from app.models.conversation import Conversation
        from app.models.document import Document
        from app.models.message import Message
        from app.models.user import User

        dialect = mysql.dialect()
        queries = {
            "会话列表": (
                select(Conversation)
                .where(Conversation.user_id == 1, Conversation.is_deleted.is_(False))
                .order_by(
                    Conversation.is_pinned.desc(),
                    *order_nulls_last_desc(Conversation.last_message_at),
                    Conversation.created_at.desc(),
                )
            ),
            "消息列表": select(Message).where(Message.conversation_id == 1).order_by(Message.id.asc()),
            "文档列表": select(Document).where(Document.is_deleted.is_(False)).order_by(Document.id.desc()),
            "用户列表": select(User).order_by(User.id.desc()),
        }

        # MySQL 不支持的方言专属关键字
        forbidden = ("NULLS LAST", "NULLS FIRST", "ILIKE", " ON CONFLICT", "RETURNING")

        for label, stmt in queries.items():
            sql = str(stmt.compile(dialect=dialect, compile_kwargs={"literal_binds": True})).upper()
            for token in forbidden:
                assert token not in sql, f"「{label}」生成了 MySQL 不支持的语法「{token}」"

        print(f"  {GREEN}✓{RESET} MySQL 方言查询编译通过（{len(queries)} 条核心查询，无方言专属语法）")
    except Exception as exc:
        failures += 1
        print(f"  {RED}✗{RESET} MySQL 查询兼容性检查失败：{exc}")
        hints.append(f"查询语句含 MySQL 不支持的语法：{exc}")

    # ---- 3. Pydantic Schema ----
    try:
        from pydantic import ValidationError

        from app.schemas import ChatRequest, UserCreate

        UserCreate(username="tester", password="abc123456")

        # 负向校验：这些都应该被拒绝
        negatives = [
            ("短用户名", lambda: UserCreate(username="ab", password="abc123456")),
            ("无数字密码", lambda: UserCreate(username="tester", password="abcdefgh")),
            ("纯空白提问", lambda: ChatRequest(question="   ")),
        ]
        for label, fn in negatives:
            try:
                fn()
                raise AssertionError(f"{label} 竟然通过了校验")
            except ValidationError:
                pass
        print(f"  {GREEN}✓{RESET} Pydantic Schema 校验规则生效（含 {len(negatives)} 项负向用例）")
    except Exception as exc:
        failures += 1
        print(f"  {RED}✗{RESET} Schema 检查失败：{exc}")
        hints.append(f"Schema 错误：{exc}")

    # ---- 4. CRUD 层 ----
    try:
        from app.crud import crud_conversation, crud_document, crud_message, crud_user

        common = ("get", "get_by", "get_multi", "count", "exists", "create", "update", "remove")
        for crud, name in (
            (crud_user, "user"), (crud_conversation, "conversation"),
            (crud_message, "message"), (crud_document, "document"),
        ):
            missing = [m for m in common if not hasattr(crud, m)]
            assert not missing, f"{name} CRUD 缺少方法：{missing}"

        assert crud_conversation.supports_soft_delete, "会话应支持软删除"
        assert crud_document.supports_soft_delete, "文档应支持软删除"
        assert not crud_message.supports_soft_delete, "消息不应支持软删除"
        print(f"  {GREEN}✓{RESET} CRUD 层方法齐备（含软删除能力判定）")
    except Exception as exc:
        failures += 1
        print(f"  {RED}✗{RESET} CRUD 检查失败：{exc}")
        hints.append(f"CRUD 错误：{exc}")

    # ---- 5. FastAPI 路由与鉴权 ----
    try:
        from app.main import app

        spec = app.openapi()
        paths = spec.get("paths", {})
        expected_paths = {
            "/api/v1/auth/register", "/api/v1/auth/login", "/api/v1/auth/refresh",
            "/api/v1/auth/logout", "/api/v1/auth/me", "/api/v1/auth/change-password",
            "/api/v1/auth/login-logs",
            "/api/v1/chat/stream", "/api/v1/chat/completions",
            "/api/v1/conversations", "/api/v1/conversations/{conversation_id}",
            "/api/v1/conversations/{conversation_id}/messages", "/api/v1/messages/search",
            "/api/v1/documents", "/api/v1/documents/upload", "/api/v1/documents/overview",
            "/api/v1/documents/categories", "/api/v1/documents/retrieve",
            "/api/v1/documents/{document_id}", "/api/v1/documents/{document_id}/chunks",
            "/api/v1/documents/{document_id}/reindex",
            "/api/v1/health", "/api/v1/health/ready", "/api/v1/health/detail",
            "/health", "/",
        }
        missing = expected_paths - set(paths)
        assert not missing, f"缺少接口：{sorted(missing)}"
        print(f"  {GREEN}✓{RESET} FastAPI 路由完整（{len(paths)} 个路径 / {len(expected_paths)} 个预期）")

        # 中间件与异常处理器
        assert len(app.user_middleware) >= 4, "中间件数量不足"
        assert len(app.exception_handlers) >= 6, "异常处理器数量不足"
        print(f"  {GREEN}✓{RESET} 中间件 {len(app.user_middleware)} 个 | 异常处理器 {len(app.exception_handlers)} 个")

        # 鉴权声明：受保护接口必须声明 security
        protected = ["/api/v1/chat/stream", "/api/v1/conversations", "/api/v1/documents"]
        for path in protected:
            for method, operation in paths[path].items():
                assert operation.get("security"), f"{method.upper()} {path} 未声明鉴权！"
        print(f"  {GREEN}✓{RESET} 受保护接口均已声明 Bearer 鉴权（{len(protected)} 个路径）")

        # 公开接口不应要求鉴权
        for path in ["/api/v1/auth/login", "/api/v1/auth/register"]:
            assert not paths[path]["post"].get("security"), f"{path} 不应要求鉴权"
        print(f"  {GREEN}✓{RESET} 登录/注册接口正确豁免鉴权")
    except Exception as exc:
        failures += 1
        print(f"  {RED}✗{RESET} FastAPI 组装检查失败：{exc}")
        hints.append(f"FastAPI 组装错误：{exc}")

    return failures, hints


# ===========================================================================
# 第三组：单元测试
# ===========================================================================
def test_unit() -> int:
    """运行 CRUD 与数据隔离单元测试。"""
    section("第三组：单元测试（CRUD + 用户数据隔离）")
    try:
        # 直接执行测试模块（其内部会打印逐条结果并以 SystemExit 返回状态）
        import runpy

        sys.argv = [sys.argv[0]]  # 清掉参数，避免被测试模块误解析
        runpy.run_path(str(TESTS_DIR / "test_crud_and_isolation.py"), run_name="__main__")
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception as exc:
        print(f"  {RED}✗{RESET} 单元测试执行异常：{exc}")
        import traceback

        traceback.print_exc()
        return 1


# ===========================================================================
# 主流程
# ===========================================================================
def test_integration() -> int:
    """运行接口集成测试（真实 HTTP 请求，走完整中间件与响应校验链路）。"""
    section("第四组：接口集成测试（真实 HTTP 请求）")
    print(f"  {CYAN}说明：用 TestClient 发起真实请求，覆盖「响应模型校验」这类静态检查发现不了的问题{RESET}")
    try:
        import runpy

        saved_argv = sys.argv
        sys.argv = [sys.argv[0]]
        try:
            runpy.run_path(str(TESTS_DIR / "test_api_endpoints.py"), run_name="__main__")
        finally:
            sys.argv = saved_argv
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception as exc:
        print(f"  {RED}✗{RESET} 接口集成测试执行异常：{exc}")
        import traceback

        traceback.print_exc()
        return 1


# ===========================================================================
# 主流程
# ===========================================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="企业知识库问答助手 - 测试入口")
    parser.add_argument("--offline", action="store_true",
                        help="使用 tests/stubs 下的桩模块（适用于尚未安装依赖的环境）")
    parser.add_argument("--only", choices=["env", "arch", "unit", "api"], default=None,
                        help="只运行指定分组：env=环境 / arch=架构 / unit=单元 / api=接口")
    args = parser.parse_args()

    banner("企业知识库问答助手 · 测试入口")
    print(f"  后端目录: {BACKEND_DIR}")
    print(f"  测试目录: {TESTS_DIR}")

    env_failures = 0
    arch_failures = 0
    unit_failures = 0
    api_failures = 0
    all_hints: list[str] = []

    if args.only in (None, "env"):
        env_failures, hints = test_environment(args.offline)
        all_hints += hints

    # 环境检查通过（或离线模式）才继续后面的测试
    if args.only in (None, "arch", "unit", "api") and env_failures == 0:
        if args.only in (None, "arch"):
            arch_failures, hints = test_architecture()
            all_hints += hints
        if args.only in (None, "unit"):
            unit_failures = test_unit()
        if args.only in (None, "api"):
            api_failures = test_integration()

    total = env_failures + arch_failures + unit_failures + api_failures

    banner("测试结果汇总")
    print(f"  环境检查   : {'通过' if env_failures == 0 else f'{RED}失败 {env_failures}{RESET}'}")
    print(f"  架构自检   : {'通过' if arch_failures == 0 else f'{RED}失败 {arch_failures}{RESET}'}")
    print(f"  单元测试   : {'通过' if unit_failures == 0 else f'{RED}失败 {unit_failures}{RESET}'}")
    print(f"  接口集成   : {'通过' if api_failures == 0 else f'{RED}失败 {api_failures}{RESET}'}")

    if all_hints:
        print(f"\n  {YELLOW}提示：{RESET}")
        for hint in all_hints:
            print(f"    · {hint}")

    if total == 0:
        print(f"\n  {GREEN}{BOLD}全部通过 ✓{RESET}")
        print(f"\n  下一步：")
        print(f"    1. 确认 backend/.env 已填写 MySQL 与 API Key")
        print(f"    2. 启动后端：{CYAN}python app/main.py{RESET}")
        print(f"    3. 启动前端：{CYAN}cd ../frontend && npm install && npm run dev{RESET}\n")
    else:
        print(f"\n  {RED}{BOLD}存在 {total} 项失败，请根据上方提示排查{RESET}")
        print(f"  详细排查步骤见 {CYAN}docs/配置修改指南.md{RESET}\n")

    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
