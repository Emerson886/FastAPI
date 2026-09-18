"""
离线测试用的 loguru 桩实现
==============================================================================

说明：
    本机未安装 loguru 时用它顶替，保证 app 能正常导入并运行。

    这里刻意做得比"纯空实现"更完整，因为 loguru 的两个特性会被业务代码真实使用：

      1. `logger.contextualize(request_id=...)` 是当作【上下文管理器】用的：
             with logger.contextualize(request_id=rid):
                 ...
         如果桩不实现 __enter__ / __exit__，会直接抛：
             TypeError: '_L' object does not support the context manager protocol
         后果是【所有接口都返回 500】。
         注意：这是桩本身的缺陷，不是业务代码的问题 —— 但一个不完整的桩
         会把"测试环境问题"伪装成"业务代码问题"，非常浪费时间。
         所以这里把上下文管理器协议补齐。

      2. `logger.opt(depth=..., exception=...)` 与 `logger.level(name).name`
         会被 logging_config.py 里的 InterceptHandler 调用。

⚠️ 仅用于离线自检，不产生任何真实日志输出，也不做日志分级过滤。
"""

from __future__ import annotations


class _FakeLevel:
    """模拟 logger.level(name) 的返回值（InterceptHandler 会读 .name）。"""

    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Level {self.name!r}>"


class _Logger:
    """loguru logger 的最小可用替身。"""

    # ------------------------------------------------------------------
    # 上下文管理器协议（关键：缺失会导致所有接口 500）
    # ------------------------------------------------------------------
    def __enter__(self) -> "_Logger":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # 返回 False：不吞掉异常，让业务逻辑自己处理
        return False

    # ------------------------------------------------------------------
    # 日志级别方法：空操作，返回自身以支持链式调用
    # ------------------------------------------------------------------
    def debug(self, *args, **kwargs) -> "_Logger":
        return self

    def info(self, *args, **kwargs) -> "_Logger":
        return self

    def success(self, *args, **kwargs) -> "_Logger":
        return self

    def warning(self, *args, **kwargs) -> "_Logger":
        return self

    def error(self, *args, **kwargs) -> "_Logger":
        return self

    def critical(self, *args, **kwargs) -> "_Logger":
        return self

    def exception(self, *args, **kwargs) -> "_Logger":
        return self

    def log(self, *args, **kwargs) -> "_Logger":
        return self

    def trace(self, *args, **kwargs) -> "_Logger":
        return self

    # ------------------------------------------------------------------
    # 链式 API
    # ------------------------------------------------------------------
    def opt(self, *args, **kwargs) -> "_Logger":
        """logger.opt(depth=..., exception=...) → 返回自身继续链式调用。"""
        return self

    def bind(self, *args, **kwargs) -> "_Logger":
        return self

    def contextualize(self, *args, **kwargs) -> "_Logger":
        """
        返回自身；_Logger 实现了 __enter__/__exit__，
        因此 `with logger.contextualize(request_id=...)` 可正常工作。
        """
        return self

    def patch(self, *args, **kwargs) -> "_Logger":
        return self

    def level(self, name: str) -> _FakeLevel:
        """logger.level("INFO").name —— InterceptHandler 会用到。"""
        return _FakeLevel(name if isinstance(name, str) else "INFO")

    def add(self, *args, **kwargs) -> int:
        """logger.add(sink, ...) 返回 handler id。"""
        return 0

    def remove(self, *args, **kwargs) -> None:
        return None

    def complete(self, *args, **kwargs) -> None:
        return None


# 模块级 logger 单例
logger = _Logger()


def add(*args, **kwargs) -> int:  # pragma: no cover
    """模块级 logger.add 的等价物。"""
    return 0


def remove(*args, **kwargs) -> None:  # pragma: no cover
    """模块级 logger.remove 的等价物。"""
    return None
