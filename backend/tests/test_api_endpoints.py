"""
==============================================================================
 接口集成测试（真实 HTTP 请求，端到端验证）
==============================================================================

【为什么需要这层测试？】
    仅做"应用能否导入"的检查是不够的。有一类 Bug 只有真正发请求才会暴露：

        fastapi.exceptions.ResponseValidationError:
            {'type': 'dict_type', 'loc': ('response', 'data'),
             'msg': 'Input should be a valid dictionary',
             'input': PageResult(total=0, ...)}

    原因是接口声明了 `response_model=ResponseModel[dict]`，
    但实际返回的是 `PageResult` 对象（Pydantic 模型，不是 dict），
    FastAPI 严格校验响应体后直接抛 500。
    这个 Bug 静态检查发现不了，必须发真实请求。

    本测试用 FastAPI 的 TestClient 走完整请求链路
    （中间件 → 依赖注入 → 路由 → 响应序列化 → 响应模型校验），
    确保每个接口的返回结构与声明的 response_model 一致。

【测试数据库】
    用 SQLite 内存库覆盖 get_db 依赖，不依赖 MySQL、不产生垃圾文件。
    并用 dependency_overrides 伪造"已登录用户"，跳过真实 JWT 校验
    （JWT 校验逻辑已由单元测试覆盖）。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
for _p in (TESTS_DIR, TESTS_DIR / "stubs"):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from app.api.deps import get_current_user, get_db  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.main import app  # noqa: E402
from conftest_helpers import create_test_conversation, create_test_engine, create_test_message, create_test_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
REGISTRY: list = []


def case(name: str):
    """登记测试用例（与单元测试保持一致的极简风格）。"""

    def decorator(fn):
        def wrapper():
            try:
                fn()
                PASSED.append(name)
                print(f"  PASS  {name}")
            except AssertionError as exc:
                FAILED.append(f"{name}: {exc}")
                print(f"  FAIL  {name}: {exc}")
            except Exception as exc:  # pragma: no cover
                import traceback

                FAILED.append(f"{name}: {type(exc).__name__}: {exc}")
                print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
                traceback.print_exc()

        wrapper.__name__ = fn.__name__
        REGISTRY.append(wrapper)
        return wrapper

    return decorator


# ===========================================================================
# 准备工作：内存数据库 + 伪造登录用户 + TestClient
# ===========================================================================
engine = create_test_engine()
Base.metadata.create_all(engine)

from sqlalchemy.orm import sessionmaker  # noqa: E402

TestSessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)

# 造一个测试用户，以及它的会话、消息、文档
_setup_db = TestSessionLocal()
TEST_USER = create_test_user(_setup_db, "apitester")
TEST_CONV = create_test_conversation(_setup_db, TEST_USER.id, title="接口测试会话")
create_test_message(_setup_db, TEST_CONV.id, TEST_USER.id, "user", "年假几天？")
create_test_message(_setup_db, TEST_CONV.id, TEST_USER.id, "assistant", "5 天【来源：手册.pdf】",
                    citations=[{"doc_id": 1, "filename": "手册.pdf", "score": 0.9}], latency_ms=500)
_setup_db.close()

_user_id = TEST_USER.id


def _override_get_db():
    """覆盖数据库依赖：让接口使用内存库。"""
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


def _override_current_user():
    """覆盖认证依赖：绕过 JWT，直接返回测试用户。"""
    db = TestSessionLocal()
    try:
        from app.models import User

        return db.get(User, _user_id)
    finally:
        db.close()


app.dependency_overrides[get_db] = _override_get_db
app.dependency_overrides[get_current_user] = _override_current_user

client = TestClient(app, raise_server_exceptions=False)


def _assert_ok(response, label: str = "") -> dict:
    """断言请求成功且符合统一响应格式，返回 data 部分。"""
    assert response.status_code in (200, 201), (
        f"{label} 状态码 {response.status_code}，响应：{response.text[:400]}"
    )
    body = response.json()
    assert "code" in body and "message" in body, f"{label} 响应缺少 code/message：{body}"
    assert body["code"] == 0, f"{label} 业务失败：code={body['code']} message={body['message']}"
    return body.get("data")


# ===========================================================================
print("\n【一】基础与健康检查")
# ===========================================================================


@case("GET / 服务信息")
def _():
    # 根路径按设计返回裸信息（方便运维直接看），不是统一响应格式
    response = client.get("/")
    assert response.status_code == 200, response.text[:200]
    data = response.json()
    assert data["name"] == "企业知识库问答助手", data
    assert data["docs"] == "/docs", data


@case("GET /health 健康检查")
def _():
    data = _assert_ok(client.get("/health"), "健康检查")
    assert data["status"] in ("ok", "degraded"), data


@case("GET /api/v1/health 版本化健康检查")
def _():
    _assert_ok(client.get("/api/v1/health"), "健康检查 v1")


@case("GET /api/v1/health/ready 就绪检查")
def _():
    _assert_ok(client.get("/api/v1/health/ready"), "就绪检查")


# ===========================================================================
print("\n【二】★ 认证与用户（含分页接口，之前 500 的高发区）")
# ===========================================================================


@case("GET /api/v1/auth/me 当前用户信息")
def _():
    data = _assert_ok(client.get("/api/v1/auth/me"), "当前用户")
    assert data["username"] == "apitester", data
    assert "hashed_password" not in data, "接口泄露了密码哈希！"


@case("PUT /api/v1/auth/me 修改资料")
def _():
    data = _assert_ok(
        client.put("/api/v1/auth/me", json={"nickname": "接口测试员"}), "修改资料"
    )
    assert data["nickname"] == "接口测试员", data


@case("★ GET /api/v1/auth/login-logs 登录记录（分页结构）")
def _():
    data = _assert_ok(client.get("/api/v1/auth/login-logs", params={"page": 1, "page_size": 10}),
                      "登录记录")
    # 必须真的是分页结构，而不是被 FastAPI 序列化失败
    for key in ("total", "page", "page_size", "pages", "items"):
        assert key in data, f"分页结构缺少字段 {key}：{data}"
    assert isinstance(data["items"], list), data


@case("POST /api/v1/auth/change-password 修改密码")
def _():
    # 先重置为测试已知的密码，再走一次修改流程（避免依赖执行顺序）
    from app.core.security import hash_password

    db = TestSessionLocal()
    try:
        user = db.get(type(TEST_USER), _user_id)
        user.hashed_password = hash_password("knownpass123")
        db.commit()
    finally:
        db.close()

    data = _assert_ok(
        client.post(
            "/api/v1/auth/change-password",
            json={"old_password": "knownpass123", "new_password": "newpass789",
                  "confirm_password": "newpass789"},
        ),
        "修改密码",
    )
    assert "message" in data, data

    # 原密码错误时应返回业务码 2003，而不是 500
    response = client.post(
        "/api/v1/auth/change-password",
        json={"old_password": "definitely_wrong", "new_password": "another123",
              "confirm_password": "another123"},
    )
    assert response.status_code == 400, f"应 400，实际 {response.status_code}"
    assert response.json()["code"] == 2003, response.json()

    # 复原密码，避免影响其他用例
    db = TestSessionLocal()
    try:
        user = db.get(type(TEST_USER), _user_id)
        user.hashed_password = hash_password("abc123456")
        db.commit()
    finally:
        db.close()


# ===========================================================================
print("\n【三】★ 会话与消息（登录后立刻调用的接口）")
# ===========================================================================


@case("★ GET /api/v1/conversations 会话列表（列表页首屏接口）")
def _():
    data = _assert_ok(client.get("/api/v1/conversations", params={"page": 1, "page_size": 50}),
                      "会话列表")
    for key in ("total", "page", "page_size", "pages", "items"):
        assert key in data, f"分页结构缺少字段 {key}：{data}"
    assert data["total"] >= 1, f"应至少有 1 个会话，实际 {data['total']}"
    item = data["items"][0]
    for key in ("id", "title", "message_count", "is_pinned", "created_at"):
        assert key in item, f"会话项缺少字段 {key}：{item}"


@case("GET /api/v1/conversations 关键字搜索")
def _():
    data = _assert_ok(client.get("/api/v1/conversations", params={"keyword": "接口测试"}),
                      "会话搜索")
    assert data["total"] == 1, data


@case("POST /api/v1/conversations 新建会话")
def _():
    data = _assert_ok(client.post("/api/v1/conversations", json={"title": "新建的会话"}), "新建会话")
    assert data["title"] == "新建的会话", data
    assert data["id"], data


@case("★ GET /api/v1/conversations/{id} 会话详情（含消息 + 引用来源）")
def _():
    data = _assert_ok(client.get(f"/api/v1/conversations/{TEST_CONV.id}"), "会话详情")
    assert data["id"] == TEST_CONV.id
    assert isinstance(data["messages"], list) and len(data["messages"]) == 2, data
    assistant = [m for m in data["messages"] if m["role"] == "assistant"][0]
    assert assistant["citations"][0]["filename"] == "手册.pdf", assistant
    assert assistant["latency_ms"] == 500, assistant


@case("★ GET /api/v1/conversations/{id}/messages 消息分页")
def _():
    data = _assert_ok(
        client.get(f"/api/v1/conversations/{TEST_CONV.id}/messages",
                   params={"page": 1, "page_size": 10}),
        "消息分页",
    )
    assert data["total"] == 2, data
    assert len(data["items"]) == 2, data


@case("PUT /api/v1/conversations/{id} 修改会话（重命名 + 置顶）")
def _():
    data = _assert_ok(
        client.put(f"/api/v1/conversations/{TEST_CONV.id}",
                   json={"title": "改过名的会话", "is_pinned": True}),
        "修改会话",
    )
    assert data["title"] == "改过名的会话" and data["is_pinned"] is True, data


@case("★ GET /api/v1/messages/search 搜索历史提问")
def _():
    data = _assert_ok(client.get("/api/v1/messages/search", params={"keyword": "年假"}),
                      "搜索历史")
    assert data["keyword"] == "年假", data
    assert data["total"] >= 1, data
    assert isinstance(data["items"], list), data
    assert data["items"][0]["content"], data


# ===========================================================================
print("\n【四】★ 知识库文档（含分页与统计）")
# ===========================================================================


@case("★ GET /api/v1/documents 文档列表（分页结构）")
def _():
    data = _assert_ok(client.get("/api/v1/documents", params={"page": 1, "page_size": 10}),
                      "文档列表")
    for key in ("total", "page", "page_size", "pages", "items"):
        assert key in data, f"分页结构缺少字段 {key}：{data}"


@case("GET /api/v1/documents/overview 知识库总览")
def _():
    data = _assert_ok(client.get("/api/v1/documents/overview"), "知识库总览")
    assert "stats" in data, data
    for key in ("total", "completed", "processing", "failed", "chunks"):
        assert key in data["stats"], f"stats 缺少 {key}：{data['stats']}"


@case("GET /api/v1/documents/categories 分类统计")
def _():
    data = _assert_ok(client.get("/api/v1/documents/categories"), "分类统计")
    assert isinstance(data, list), f"应返回数组，实际 {type(data)}"


@case("POST /api/v1/documents/retrieve 检索测试")
def _():
    # 没有文档时向量库返回空列表，接口应当正常返回而不是报错
    data = _assert_ok(
        client.post("/api/v1/documents/retrieve", params={"query": "年假", "top_k": 3}),
        "检索测试",
    )
    assert "results" in data and isinstance(data["results"], list), data


@case("GET /api/v1/documents/{id} 文档不存在时返回 404")
def _():
    response = client.get("/api/v1/documents/999999")
    assert response.status_code == 404, f"应返回 404，实际 {response.status_code}"
    body = response.json()
    assert body["code"] != 0, f"业务码应为错误码：{body}"
    assert "不存在" in body["message"] or "无权" in body["message"], body


# ===========================================================================
print("\n【五】错误处理与数据隔离")
# ===========================================================================


@case("访问不存在的会话返回 404（不泄露是否存在）")
def _():
    response = client.get("/api/v1/conversations/999999")
    assert response.status_code == 404, f"应返回 404，实际 {response.status_code}"
    assert response.json()["code"] != 0


@case("访问别人的会话返回 404（越权防护）")
def _():
    # 造一个属于另一个用户的会话
    db = TestSessionLocal()
    try:
        other = create_test_user(db, "otheruser")
        other_conv = create_test_conversation(db, other.id, title="别人的会话")
        other_conv_id = other_conv.id
    finally:
        db.close()

    response = client.get(f"/api/v1/conversations/{other_conv_id}")
    assert response.status_code == 404, (
        f"严重安全问题：能访问别人的会话！状态码 {response.status_code}"
    )
    assert response.json()["code"] != 0


@case("参数校验失败返回 422 且格式统一")
def _():
    response = client.get("/api/v1/conversations", params={"page": 0})  # page 必须 >= 1
    assert response.status_code == 422, f"应返回 422，实际 {response.status_code}"
    body = response.json()
    assert body["code"] == 1001, f"业务码应为参数错误 1001：{body}"
    assert "errors" in body["data"], body


@case("分页参数超上限被拒绝（page_size > 100）")
def _():
    response = client.get("/api/v1/conversations", params={"page_size": 99999})
    assert response.status_code == 422, f"应返回 422，实际 {response.status_code}"


@case("未登录访问受保护接口返回 401")
def _():
    # 临时移除认证覆盖，模拟未携带 token
    saved = app.dependency_overrides.pop(get_current_user, None)
    try:
        fresh_client = TestClient(app, raise_server_exceptions=False)
        response = fresh_client.get("/api/v1/conversations")
        assert response.status_code == 401, f"应返回 401，实际 {response.status_code}"
        assert response.json()["code"] == 2001, response.json()
    finally:
        if saved is not None:
            app.dependency_overrides[get_current_user] = saved


@case("响应头包含 request_id（链路追踪）")
def _():
    response = client.get("/api/v1/conversations")
    assert "X-Request-ID" in response.headers, "缺少 X-Request-ID 响应头"
    assert len(response.headers["X-Request-ID"]) == 32, response.headers["X-Request-ID"]


@case("响应体包含 request_id 与 timestamp（统一响应格式）")
def _():
    body = client.get("/api/v1/conversations").json()
    assert body.get("request_id"), "响应体缺少 request_id"
    assert body.get("timestamp"), "响应体缺少 timestamp"


# ===========================================================================
# 执行
# ===========================================================================
for _test in REGISTRY:
    _test()

app.dependency_overrides.clear()
engine.dispose()

print("\n" + "=" * 74)
print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("\n失败详情：")
    for item in FAILED:
        print(f"  - {item}")
    print("=" * 74)
    sys.exit(1)
print("接口集成测试全部通过 ✓")
print("=" * 74)
sys.exit(0)
