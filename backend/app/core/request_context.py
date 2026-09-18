"""
Fully read!!!
==============================================================================
 请求上下文（Request Context）
==============================================================================

【解决的问题】
    统一响应体里有一个 request_id 字段，用于把「用户看到的错误」和
    「后端日志」关联起来。但接口函数是同步的普通函数，拿不到 Request 对象，
    于是到处都是 ResponseModel.success(data=..., request_id=None) —— 结果
    request_id 永远是 null，这个字段形同虚设。

【解决方案：contextvars】
    Python 的 contextvars 提供「协程/线程安全」的上下文变量。
    中间件在请求开始时把 request_id 写入变量，请求结束时自动清理；
    任何代码（包括深层函数）都能在本次请求的上下文中读到它，
    不需要一层层传参数。

    关键优势：contextvars 在 asyncio 下是「按任务隔离」的，
    并发请求之间不会串号（这正是不能用全局变量的原因）。

【使用方式】
    # 中间件里写入
    from app.core.request_context import set_request_id
    set_request_id(rid)

    # 任何地方读取（响应模型会自动读，一般不用手写）
    from app.core.request_context import get_request_id
    rid = get_request_id()
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token

# 默认值 "-" 表示「不在请求上下文中」（如启动脚本、后台任务）
_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
# 创建了一个上下文变量实例，名字叫 "request_id"；
#
# 声明它存储字符串，默认值为 "-"；
#
# 赋值给模块级变量 _request_id_var，供全局在异步请求中安全地存取 request_id。

def set_request_id(request_id: str) -> Token:
    """
    写入当前上下文的 request_id。

    :return: ContextVar Token，可用于精确还原（见 reset_request_id）
    """
    return _request_id_var.set(request_id or "-")
    # set会返回一个token用于表示这个上下文变量


def get_request_id(default: str = "-") -> str:
    """
    读取当前上下文的 request_id。

    不在请求上下文中时返回 default（默认 "-"）。
    """
    value = _request_id_var.get()
    return value if value else default


def reset_request_id(token: Token | None = None) -> None:
    """
    还原 request_id。

    :param token: set_request_id 返回的 Token；传 None 则重置为默认值。
                 传 Token 可以精确还原到「设置之前」的状态，
                 对嵌套中间件/后台任务更安全。
    """
    if token is not None:
        try:
            _request_id_var.reset(token)
            # reset会根据对应的token重置上下文变量的值为set之前的值
            return
        except ValueError:
            # Token 已被使用过或来自其他上下文，退化为直接重置
            pass
    _request_id_var.set("-")


def new_request_id() -> str:
    """生成一个新的 request_id（32 位十六进制，无连字符）。"""
    return uuid.uuid4().hex
