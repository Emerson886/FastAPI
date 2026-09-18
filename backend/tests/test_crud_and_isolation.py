"""
==============================================================================
 单元测试：用户与认证
==============================================================================

运行方式（在 backend 目录下）：
    C:/Users/thunderfish/miniconda3/envs/langchain/python.exe tests/test_crud_and_isolation.py

也兼容 pytest：
    python -m pytest tests/ -v
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 【重要】必须先导入 conftest_helpers：
#   它内部会处理 sys.path（含 KB_STUB_DIR 桩模块目录），
#   否则在缺少第三方包的环境下，下面 import app.* 会直接失败。
from tests.conftest_helpers import (  # noqa: E402
    build_test_session,
    create_test_conversation,
    create_test_message,
    create_test_user,
)

from app.crud import crud_conversation, crud_document, crud_message, crud_user  # noqa: E402
from app.schemas.user import UserCreate  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
REGISTRY: list = []  # 收集所有被 @case 装饰的测试函数


def case(name: str):
    """
    极简测试装饰器（不依赖 pytest，任何环境都能直接跑）。

    做两件事：
      1. 把被装饰的函数登记到 REGISTRY，文件末尾统一执行
      2. 提供 __test__ = False，避免 pytest 把内部包装函数当成测试重复收集
    """

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
        wrapper.__doc__ = fn.__doc__
        REGISTRY.append(wrapper)
        return wrapper

    return decorator


# ============================================================================
print("\n【一】用户 CRUD 与认证")
# ============================================================================


@case("创建用户：密码被哈希，明文不入库")
def _():
    with build_test_session() as db:
        payload = UserCreate(username="alice", password="abc123456", nickname="爱丽丝", email="a@x.com")
        user = crud_user.create_user(db, payload)

        assert user.id is not None, "未生成主键"
        assert user.username == "alice"
        assert user.hashed_password != "abc123456", "密码竟然明文入库了！"
        assert len(user.hashed_password) > 20
        assert user.is_active is True
        assert user.is_superuser is False


@case("create_user：邮箱为空串时存 NULL（避免唯一索引冲突）")
def _():
    with build_test_session() as db:
        u1 = crud_user.create_user(db, UserCreate(username="user_01", password="abc123456", email=None))
        u2 = crud_user.create_user(db, UserCreate(username="user_02", password="abc123456", email=None))
        assert u1.email is None and u2.email is None, "空邮箱应存为 NULL"
        assert u1.id != u2.id


@case("create_user：confirm_password 不会被写入数据库（回归测试）")
def _():
    from app.models import User

    with build_test_session() as db:
        # confirm_password 是 Schema 里的校验字段，不是 User 表字段。
        # 历史 Bug：把它一起传给了 SQLAlchemy，导致 TypeError。
        payload = UserCreate(
            username="withconfirm", password="abc123456", confirm_password="abc123456"
        )
        user = crud_user.create_user(db, payload)

        assert user.id is not None
        assert not hasattr(User, "confirm_password"), "User 表不应存在 confirm_password 字段"
        assert user.email is None


@case("to_dict 不泄露密码哈希")
def _():
    with build_test_session() as db:
        user = create_test_user(db, "bob")
        data = user.to_dict()
        assert "hashed_password" not in data, "to_dict 泄露了密码哈希！"
        assert "username" in data


@case("按用户名 / 邮箱查询（大小写不敏感）")
def _():
    with build_test_session() as db:
        crud_user.create_user(
            db, UserCreate(username="Charlie", password="abc123456", email="Charlie@X.com")
        )
        assert crud_user.get_by_username(db, "charlie") is not None
        assert crud_user.get_by_username(db, "CHARLIE") is not None
        assert crud_user.get_by_email(db, "charlie@x.com") is not None
        assert crud_user.get_by_username_or_email(db, "charlie@x.com") is not None


@case("authenticate：正确密码通过")
def _():
    with build_test_session() as db:
        crud_user.create_user(db, UserCreate(username="dave", password="abc123456"))
        user = crud_user.authenticate(db, "dave", "abc123456")
        assert user is not None, "正确密码应当认证成功"


@case("authenticate：错误密码返回 None")
def _():
    with build_test_session() as db:
        crud_user.create_user(db, UserCreate(username="dave", password="abc123456"))
        assert crud_user.authenticate(db, "dave", "wrong_password") is None
        assert crud_user.authenticate(db, "nonexistent", "abc123456") is None


@case("修改密码后旧密码失效")
def _():
    with build_test_session() as db:
        crud_user.create_user(db, UserCreate(username="eve", password="abc123456"))
        user = crud_user.authenticate(db, "eve", "abc123456")
        crud_user.change_password(db, user, "newpass789")

        assert crud_user.authenticate(db, "eve", "abc123456") is None, "旧密码仍可用！"
        assert crud_user.authenticate(db, "eve", "newpass789") is not None


@case("启用 / 禁用账号")
def _():
    with build_test_session() as db:
        user = create_test_user(db, "frank")
        assert user.is_active is True
        crud_user.activate(db, user, active=False)
        assert user.is_active is False
        crud_user.activate(db, user, active=True)
        assert user.is_active is True


@case("更新登录信息（时间 + IP）")
def _():
    with build_test_session() as db:
        user = create_test_user(db, "grace")
        assert user.last_login_at is None
        crud_user.update_login_info(db, user, "192.168.1.100")
        assert user.last_login_at is not None
        assert user.last_login_ip == "192.168.1.100"


@case("用户列表搜索与分页")
def _():
    with build_test_session() as db:
        for i in range(15):
            crud_user.create_user(db, UserCreate(username=f"user{i:02d}", password="abc123456"))
        users, total = crud_user.search(db, keyword="user0", skip=0, limit=10)
        assert total == 10, f"关键字 user0 应匹配 10 个，实际 {total}"
        assert len(users) == 10

        all_users, all_total = crud_user.search(db, skip=0, limit=10)
        assert all_total == 15
        assert len(all_users) == 10


@case("用户名唯一约束生效")
def _():
    from sqlalchemy.exc import IntegrityError

    with build_test_session() as db:
        create_test_user(db, "duplicate")
        try:
            create_test_user(db, "duplicate")
        except IntegrityError:
            db.rollback()
            return
        raise AssertionError("重复用户名竟然插入成功了")


# ============================================================================
print("\n【二】★ 用户数据隔离（企业项目最关键的安全测试）")
# ============================================================================


@case("★ alice 无法读取 bob 的会话（get_owned 返回 None）")
def _():
    with build_test_session() as db:
        alice = create_test_user(db, "alice")
        bob = create_test_user(db, "bob")
        bob_conv = create_test_conversation(db, bob.id, title="bob 的私密会话")

        # bob 自己能读到
        assert crud_conversation.get_owned(db, bob_conv.id, bob.id) is not None

        # alice 拿 bob 的会话 ID 去查 → 必须返回 None（这就是越权防护）
        assert crud_conversation.get_owned(db, bob_conv.id, alice.id) is None, \
            "严重漏洞：alice 读到了 bob 的会话！"


@case("★ 会话列表只返回自己的（list_by_user）")
def _():
    with build_test_session() as db:
        alice = create_test_user(db, "alice")
        bob = create_test_user(db, "bob")

        for i in range(3):
            create_test_conversation(db, alice.id, title=f"alice 会话 {i}")
        for i in range(5):
            create_test_conversation(db, bob.id, title=f"bob 会话 {i}")

        alice_convs, alice_total = crud_conversation.list_by_user(db, alice.id)
        bob_convs, bob_total = crud_conversation.list_by_user(db, bob.id)

        assert alice_total == 3, f"alice 应只有 3 个会话，实际 {alice_total}"
        assert bob_total == 5
        assert all(c.user_id == alice.id for c in alice_convs), "列表里混入了别人的会话！"


@case("★ 消息列表按用户过滤（list_by_conversation + user_id）")
def _():
    with build_test_session() as db:
        alice = create_test_user(db, "alice")
        bob = create_test_user(db, "bob")
        conv = create_test_conversation(db, alice.id)
        create_test_message(db, conv.id, alice.id, content="alice 的消息")

        # alice 能查到
        assert len(crud_message.list_by_conversation(db, conv.id, user_id=alice.id)) == 1
        # bob 查同一个会话（带自己的 user_id）→ 查不到
        assert len(crud_message.list_by_conversation(db, conv.id, user_id=bob.id)) == 0


@case("★ 删除会话后列表不再返回（软删除）")
def _():
    with build_test_session() as db:
        alice = create_test_user(db, "alice")
        conv = create_test_conversation(db, alice.id)

        crud_conversation.remove(db, conv.id, hard=False)

        assert crud_conversation.get_owned(db, conv.id, alice.id) is None, "软删除的会话仍可访问"
        _, total = crud_conversation.list_by_user(db, alice.id)
        assert total == 0
        # 数据仍在库里（可恢复、可审计）
        assert crud_conversation.get(db, conv.id, include_deleted=True) is not None


@case("★ 删除用户时级联清除其会话与消息")
def _():
    with build_test_session() as db:
        alice = create_test_user(db, "alice")
        conv = create_test_conversation(db, alice.id)
        create_test_message(db, conv.id, alice.id, content="会被级联删除")

        from app.models import Conversation, Message

        db.delete(alice)
        db.commit()

        assert db.query(Conversation).count() == 0, "用户的会话未被级联删除"
        assert db.query(Message).count() == 0, "会话的消息未被级联删除"


# ============================================================================
print("\n【三】会话业务逻辑")
# ============================================================================


@case("自动生成标题：短问题保持原文")
def _():
    title = crud_conversation.auto_title_from_question("年假几天")
    assert title == "年假几天", f"实际得到 {title!r}"


@case("自动生成标题：长问题截断加省略号")
def _():
    question = "公司的年假制度是怎么规定的？我想了解详细的请假流程和审批要求"
    title = crud_conversation.auto_title_from_question(question)
    assert len(title) <= 23, f"标题过长：{len(title)}"
    assert title.endswith("...")
    assert question.startswith(title[:-3])


@case("自动生成标题：压缩多余空白")
def _():
    title = crud_conversation.auto_title_from_question("年假   几天   ")
    assert "  " not in title, f"空白未压缩：{title!r}"


@case("touch：消息数原子自增 + 刷新最后消息时间")
def _():
    with build_test_session() as db:
        user = create_test_user(db, "alice")
        conv = create_test_conversation(db, user.id)
        assert conv.message_count == 0

        crud_conversation.touch(db, conv.id, inc_messages=2)
        db.refresh(conv)
        assert conv.message_count == 2, f"实际 {conv.message_count}"

        crud_conversation.touch(db, conv.id, inc_messages=2)
        db.refresh(conv)
        assert conv.message_count == 4
        assert conv.last_message_at is not None


@case("置顶的会话排在列表最前")
def _():
    with build_test_session() as db:
        user = create_test_user(db, "alice")
        c1 = create_test_conversation(db, user.id, title="普通会话")
        c2 = create_test_conversation(db, user.id, title="要置顶的")
        crud_conversation.set_pinned(db, c2, True)

        convs, _ = crud_conversation.list_by_user(db, user.id)
        assert convs[0].id == c2.id, f"置顶会话未排最前，实际顺序：{[c.title for c in convs]}"


@case("会话标题搜索")
def _():
    with build_test_session() as db:
        user = create_test_user(db, "alice")
        create_test_conversation(db, user.id, title="年假制度咨询")
        create_test_conversation(db, user.id, title="报销流程咨询")
        create_test_conversation(db, user.id, title="技术架构讨论")

        convs, total = crud_conversation.list_by_user(db, user.id, keyword="咨询")
        assert total == 2, f"实际匹配 {total}"


@case("★ 会话列表排序 SQL 必须兼容 MySQL（禁止 NULLS LAST）")
def _():
    """
    回归测试：曾经用了 SQLAlchemy 的 .nullslast()，它会生成
        ORDER BY last_message_at DESC NULLS LAST
    而 MySQL 不支持这个语法，导致「登录后一进首页就 500」：
        (1064, "You have an error in your SQL syntax ... near 'NULLS LAST'")

    这里直接编译 MySQL 方言的 SQL 并断言不含 NULLS LAST/FIRST，
    保证以后无论谁改了排序逻辑，都不会再把 MySQL 弄挂。
    """
    from sqlalchemy import select
    from sqlalchemy.dialects import mysql, sqlite

    from app.crud.conversation import order_nulls_last_desc
    from app.models.conversation import Conversation

    stmt = (
        select(Conversation)
        .where(Conversation.user_id == 1, Conversation.is_deleted.is_(False))
        .order_by(
            Conversation.is_pinned.desc(),
            *order_nulls_last_desc(Conversation.last_message_at),
            Conversation.created_at.desc(),
        )
        .offset(0)
        .limit(50)
    )

    for dialect_name, dialect in (("MySQL", mysql.dialect()), ("SQLite", sqlite.dialect())):
        sql = str(stmt.compile(dialect=dialect, compile_kwargs={"literal_binds": True})).upper()
        assert "NULLS LAST" not in sql, f"{dialect_name} 方言仍生成了 NULLS LAST 语法！"
        assert "NULLS FIRST" not in sql, f"{dialect_name} 方言仍生成了 NULLS FIRST 语法！"
        # 防御性断言：确保排序条件确实还在（防止函数被改成返回空列表而"假通过"）
        assert "ORDER BY" in sql, "排序条件丢失"
        assert "CASE WHEN" in sql, "未使用跨数据库兼容的 CASE WHEN 写法"


@case("★ 会话排序：置顶优先 → 时间倒序 → 无消息的排最后")
def _():
    from datetime import datetime, timedelta

    with build_test_session() as db:
        user = create_test_user(db, "alice")
        now = datetime.now()

        # 刻意按"乱序"插入，验证排序是数据库做的而不是插入顺序
        create_test_conversation(db, user.id, title="还没说过话")  # last_message_at = None
        create_test_conversation(
            db, user.id, title="较早的", last_message_at=now - timedelta(hours=2)
        )
        create_test_conversation(db, user.id, title="最近的", last_message_at=now)
        create_test_conversation(
            db, user.id, title="置顶的", last_message_at=now - timedelta(days=3), is_pinned=True
        )

        convs, _ = crud_conversation.list_by_user(db, user.id)
        order = [c.title for c in convs]

        assert order[0] == "置顶的", f"置顶会话未排最前，实际顺序：{order}"
        assert order[1] == "最近的", f"最近会话应排第二，实际顺序：{order}"
        assert order[2] == "较早的", f"较早会话应排第三，实际顺序：{order}"
        assert order[3] == "还没说过话", f"无消息的会话应排最后，实际顺序：{order}"


@case("清空我的全部会话（不影响他人）")
def _():
    with build_test_session() as db:
        alice = create_test_user(db, "alice")
        bob = create_test_user(db, "bob")
        for _ in range(3):
            create_test_conversation(db, alice.id)
        create_test_conversation(db, bob.id)

        deleted = crud_conversation.delete_all_by_user(db, alice.id)
        assert deleted == 3, f"实际删除 {deleted}"

        _, alice_total = crud_conversation.list_by_user(db, alice.id)
        _, bob_total = crud_conversation.list_by_user(db, bob.id)
        assert alice_total == 0
        assert bob_total == 1, "清空操作影响到了其他用户！"


# ============================================================================
print("\n【四】消息与历史记录")
# ============================================================================


@case("写入消息并保留 RAG 引用与统计字段")
def _():
    with build_test_session() as db:
        user = create_test_user(db, "alice")
        conv = create_test_conversation(db, user.id)

        msg = crud_message.create_message(
            db,
            conversation_id=conv.id,
            user_id=user.id,
            role="assistant",
            content="年假 5 天【来源：员工手册.pdf】",
            citations=[{"doc_id": 1, "filename": "员工手册.pdf", "chunk_index": 3, "score": 0.87}],
            model_name="deepseek-chat",
            prompt_tokens=812,
            completion_tokens=156,
            latency_ms=3421,
        )
        assert msg.id is not None
        assert msg.citations[0]["filename"] == "员工手册.pdf"
        assert msg.citations[0]["score"] == 0.87
        assert msg.prompt_tokens == 812
        assert msg.latency_ms == 3421
        assert msg.status == "success"


@case("get_history_for_llm 转成 LangChain 消息格式且按时间正序")
def _():
    with build_test_session() as db:
        user = create_test_user(db, "alice")
        conv = create_test_conversation(db, user.id)

        crud_message.create_message(db, conv.id, user.id, "user", "第一个问题")
        crud_message.create_message(db, conv.id, user.id, "assistant", "第一个回答")
        crud_message.create_message(db, conv.id, user.id, "user", "第二个问题")
        crud_message.create_message(db, conv.id, user.id, "assistant", "第二个回答")

        history = crud_message.get_history_for_llm(db, conv.id)

        assert len(history) == 4
        assert [h["role"] for h in history] == ["user", "assistant", "user", "assistant"]
        assert history[0]["content"] == "第一个问题", "历史顺序错误（应为时间正序）"
        assert history[-1]["content"] == "第二个回答"


@case("get_history_for_llm 排除指定消息（避免当前提问重复）")
def _():
    with build_test_session() as db:
        user = create_test_user(db, "alice")
        conv = create_test_conversation(db, user.id)
        crud_message.create_message(db, conv.id, user.id, "user", "历史问题")
        current = crud_message.create_message(db, conv.id, user.id, "user", "当前提问")

        history = crud_message.get_history_for_llm(db, conv.id, exclude_message_id=current.id)
        assert len(history) == 1
        assert history[0]["content"] == "历史问题"


@case("get_history_for_llm 跳过失败的回答（不污染上下文）")
def _():
    with build_test_session() as db:
        user = create_test_user(db, "alice")
        conv = create_test_conversation(db, user.id)
        crud_message.create_message(db, conv.id, user.id, "user", "问题")
        crud_message.create_message(db, conv.id, user.id, "assistant", "失败的回答",
                                    status="failed")

        history = crud_message.get_history_for_llm(db, conv.id)
        assert len(history) == 1, f"失败的回答进入了上下文：{history}"
        assert history[0]["role"] == "user"


@case("get_history_for_llm 限制条数（防止超出上下文窗口）")
def _():
    with build_test_session() as db:
        user = create_test_user(db, "alice")
        conv = create_test_conversation(db, user.id)
        for i in range(30):
            crud_message.create_message(db, conv.id, user.id, "user", f"问题 {i}")
            crud_message.create_message(db, conv.id, user.id, "assistant", f"回答 {i}")

        history = crud_message.get_history_for_llm(db, conv.id, limit=10)
        assert len(history) == 10, f"实际 {len(history)}"
        # 应保留最近的对话
        assert history[-1]["content"] == "回答 29", f"最后一条是 {history[-1]['content']}"


@case("分页拉取消息（list_by_conversation_paged）")
def _():
    with build_test_session() as db:
        user = create_test_user(db, "alice")
        conv = create_test_conversation(db, user.id)
        for i in range(25):
            crud_message.create_message(db, conv.id, user.id, "user", f"消息 {i}")

        page1, total = crud_message.list_by_conversation_paged(db, conv.id, user.id, skip=0, limit=10)
        page3, _ = crud_message.list_by_conversation_paged(db, conv.id, user.id, skip=20, limit=10)

        assert total == 25
        assert len(page1) == 10
        assert len(page3) == 5
        assert page1[0].content == "消息 0", "分页顺序错误"


@case("搜索我的历史提问（只能搜到自己的）")
def _():
    with build_test_session() as db:
        alice = create_test_user(db, "alice")
        bob = create_test_user(db, "bob")
        ac = create_test_conversation(db, alice.id)
        bc = create_test_conversation(db, bob.id)

        crud_message.create_message(db, ac.id, alice.id, "user", "年假制度是怎么规定的")
        crud_message.create_message(db, bc.id, bob.id, "user", "年假制度是怎么规定的")

        results, total = crud_message.search_by_user(db, alice.id, "年假")
        assert total == 1, f"alice 应只搜到自己的 1 条，实际 {total}"
        assert all(m.user_id == alice.id for m in results), "搜到了别人的消息！"


# ============================================================================
print("\n【五】知识库文档与权限可见性")
# ============================================================================


@case("创建文档记录")
def _():
    with build_test_session() as db:
        user = create_test_user(db, "alice")
        doc = crud_document.create(
            db,
            {
                "owner_id": user.id,
                "filename": "员工手册.pdf",
                "stored_path": "/tmp/x.pdf",
                "file_ext": ".pdf",
                "file_size": 1024,
                "file_hash": "abc123",
                "title": "员工手册",
                "category": "人事制度",
                "is_public": True,
            },
        )
        assert doc.id is not None
        assert doc.status == "pending"
        assert doc.chunk_count == 0


@case("★ 公共文档：所有用户可见")
def _():
    with build_test_session() as db:
        alice = create_test_user(db, "alice")
        bob = create_test_user(db, "bob")
        doc = crud_document.create(db, {
            "owner_id": alice.id, "filename": "公司制度.pdf", "stored_path": "/tmp/a.pdf",
            "file_ext": ".pdf", "file_size": 100, "is_public": True,
        })

        assert crud_document.get_visible(db, doc.id, alice.id) is not None
        assert crud_document.get_visible(db, doc.id, bob.id) is not None, "公共文档 bob 应当可见"

        _, alice_total = crud_document.list_visible(db, alice.id)
        _, bob_total = crud_document.list_visible(db, bob.id)
        assert alice_total == 1 and bob_total == 1


@case("★ 私有文档：非上传者完全不可见、不可检索")
def _():
    with build_test_session() as db:
        alice = create_test_user(db, "alice")
        bob = create_test_user(db, "bob")
        doc = crud_document.create(db, {
            "owner_id": alice.id, "filename": "alice私有.pdf", "stored_path": "/tmp/p.pdf",
            "file_ext": ".pdf", "file_size": 100, "is_public": False,
        })

        assert crud_document.get_visible(db, doc.id, alice.id) is not None
        assert crud_document.get_visible(db, doc.id, bob.id) is None, \
            "严重漏洞：bob 能看到 alice 的私有文档！"

        _, bob_total = crud_document.list_visible(db, bob.id)
        assert bob_total == 0, f"bob 的可见列表里出现了别人的私有文档（{bob_total} 条）"


@case("★ RAG 检索范围与列表可见范围一致（is_public OR owner_id）")
def _():
    from app.models import Document, DocumentStatus

    with build_test_session() as db:
        alice = create_test_user(db, "alice")
        bob = create_test_user(db, "bob")

        crud_document.create(db, {
            "owner_id": alice.id, "filename": "公共.pdf", "stored_path": "/1",
            "file_ext": ".pdf", "file_size": 1, "is_public": True,
            "status": DocumentStatus.COMPLETED,
        })
        crud_document.create(db, {
            "owner_id": alice.id, "filename": "alice私有.pdf", "stored_path": "/2",
            "file_ext": ".pdf", "file_size": 1, "is_public": False,
            "status": DocumentStatus.COMPLETED,
        })
        crud_document.create(db, {
            "owner_id": bob.id, "filename": "bob私有.pdf", "stored_path": "/3",
            "file_ext": ".pdf", "file_size": 1, "is_public": False,
            "status": DocumentStatus.COMPLETED,
        })

        # 模拟 RAG 检索前的文档范围过滤（见 crud_document._visibility_condition 注释）
        visible_for_bob = crud_document.list_completed_for_rag(db, bob.id)
        names = sorted(d.filename for d in visible_for_bob)
        # 注意：排序按 Unicode 码点，ASCII（b）排在中文（公）之前
        assert names == ["bob私有.pdf", "公共.pdf"], f"bob 的可检索范围错误：{names}"

        # ★ 核心断言：RAG 可检索范围 必须与 列表可见范围 完全一致
        list_items, list_total = crud_document.list_visible(db, bob.id, limit=100)
        list_names = sorted(d.filename for d in list_items)
        assert list_names == names, (
            f"可见范围与可检索范围不一致！列表={list_names} 检索={names}"
            "（这会导致 AI 引用用户看不到的文档）"
        )

        # 处理中的文档不应进入可检索范围
        crud_document.create(db, {
            "owner_id": bob.id, "filename": "处理中.pdf", "stored_path": "/4",
            "file_ext": ".pdf", "file_size": 1, "is_public": True,
            "status": DocumentStatus.PARSING,
        })
        names2 = sorted(d.filename for d in crud_document.list_completed_for_rag(db, bob.id))
        assert "处理中.pdf" not in names2, "未处理完的文档进入了可检索范围"


@case("文件哈希去重（同一用户传同一文件不重复向量化）")
def _():
    with build_test_session() as db:
        alice = create_test_user(db, "alice")
        bob = create_test_user(db, "bob")
        payload = {
            "filename": "same.pdf", "stored_path": "/s", "file_ext": ".pdf",
            "file_size": 10, "file_hash": "hash_same",
        }
        crud_document.create(db, {**payload, "owner_id": alice.id})

        assert crud_document.get_by_hash(db, "hash_same", alice.id) is not None
        # 不同用户上传相同文件不算重复（各自的权限范围独立）
        assert crud_document.get_by_hash(db, "hash_same", bob.id) is None


@case("文档统计（stats）")
def _():
    from app.models import DocumentStatus

    with build_test_session() as db:
        user = create_test_user(db, "alice")
        base = {"owner_id": user.id, "stored_path": "/x", "file_ext": ".pdf",
                "file_size": 1, "is_public": True}
        crud_document.create(db, {**base, "filename": "a.pdf",
                                  "status": DocumentStatus.COMPLETED, "chunk_count": 10})
        crud_document.create(db, {**base, "filename": "b.pdf",
                                  "status": DocumentStatus.COMPLETED, "chunk_count": 5})
        crud_document.create(db, {**base, "filename": "c.pdf", "status": DocumentStatus.PARSING})
        crud_document.create(db, {**base, "filename": "d.pdf", "status": DocumentStatus.FAILED})

        stats = crud_document.stats(db, user.id)
        assert stats["total"] == 4, stats
        assert stats["completed"] == 2, stats
        assert stats["processing"] == 1, stats
        assert stats["failed"] == 1, stats
        assert stats["chunks"] == 15, stats


@case("文档切片批量写入与查询")
def _():
    with build_test_session() as db:
        user = create_test_user(db, "alice")
        doc = crud_document.create(db, {
            "owner_id": user.id, "filename": "d.pdf", "stored_path": "/d",
            "file_ext": ".pdf", "file_size": 1,
        })
        crud_document.create_chunks(db, doc.id, [
            {"chunk_index": 0, "content": "第一段内容", "char_count": 5, "vector_id": f"doc{doc.id}_chunk0"},
            {"chunk_index": 1, "content": "第二段内容", "char_count": 5, "vector_id": f"doc{doc.id}_chunk1"},
        ])
        chunks, total = crud_document.list_chunks(db, doc.id)
        assert total == 2
        assert chunks[0].content == "第一段内容"
        assert chunks[1].vector_id.endswith("chunk1")


# ============================================================================
# 执行所有登记的测试用例
# ============================================================================
for _test in REGISTRY:
    _test()

# ============================================================================
# 汇总
# ============================================================================
print("\n" + "=" * 74)
print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("\n失败详情：")
    for item in FAILED:
        print(f"  - {item}")
    print("=" * 74)
    sys.exit(1)
print("全部测试通过 ✓")
print("=" * 74)
sys.exit(0)
