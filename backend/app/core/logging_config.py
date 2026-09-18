"""
==============================================================================
 日志配置模块
==============================================================================

企业项目日志规范：
    - 控制台彩色输出，方便本地开发
    - 文件按天切割 + 保留 N 天 + 自动压缩（避免磁盘被日志打满）
    - 区分「全量日志」和「错误日志」两个文件，排障时直接看 error 文件
    - 每条日志带 request_id，可以按请求把一次调用的所有日志串起来

本模块使用 loguru（比标准 logging 更简单），同时把标准库 logging 的
输出也接管过来（uvicorn / sqlalchemy 的日志也会写进文件）。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from loguru import logger #全局日志对象

from app.core.config import settings


class _InterceptHandler(logging.Handler):
    """
    把 Python 标准库 logging 的日志转发给 loguru，
    这样 SQLAlchemy、uvicorn 的日志也会统一格式并写入文件。
    """

    # 重写 emit 方法，当日志记录产生时会被调用。record 是标准库的日志记录对象
    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        try:
            # 尝试从 loguru 中查找与标准库日志级别同名的级别对象，并取其名称
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            # 使用标准库的数字级别
            level = record.levelno

        # 找到真正的调用位置，保证日志里显示的文件/行号是业务代码而不是 handler
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        # frame.f_code.co_filename 是当前帧对应的代码文件名。
        #
        # logging.__file__ 是标准库 logging 模块的文件路径（通常是 .../logging/__init__.py）。
        #
        # 如果当前帧的文件名是 logging 模块的文件，说明我们还在标准库 logging 的内部。此时：
        #
        # frame = frame.f_back：向上回溯一帧（调用当前帧的帧）。
        #
        # depth += 1：因为多了一层 logging 内部的帧，Loguru 需要多跳过一层。
        #
        # 循环会一直执行，直到找到一个帧，它的文件名不是 logging 模块的文件。
        # 这个帧就是第一个不属于标准库 logging 的调用者，通常就是业务代码或第三方库中真正调用日志的地方。
        #
        # 循环结束后，depth 的值正好是需要跳过的总层数，frame 则指向业务代码的帧（虽然这里没直接用到 frame，但可以用来验证）。

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _console_format(record: dict) -> str:
    """
    自定义控制台格式：突出「时间 / 级别 / request_id / 位置」。
    request_id 由中间件通过 logger.contextualize 注入到 extra 里。
    """
    rid = record["extra"].get("request_id", "-")
    rid_short = rid[:8] if rid and rid != "-" else "-"
    return (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        f"<cyan>{rid_short: <8}</cyan> | "
        "<level>{message}</level> | "
        "<dim>{name}:{function}:{line}</dim>\n{exception}"
    )


def _add_handler_safely(sink, /, **kwargs):
    """
    安全地添加 loguru handler，自动处理「异步日志不可用」的环境。

    【为什么需要这个包装？】
        loguru 的 enqueue=True 会在底层创建一个 multiprocessing.SimpleQueue，
        而 SimpleQueue 依赖操作系统的【命名管道】。以下环境会直接拒绝创建：

            PermissionError: [WinError 5] 拒绝访问。

        - Windows 应用沙箱 / 沙箱化终端（本项目开发环境就是）
        - 部分受限容器、开启了严格 seccomp 的运行时
        - 某些企业安全策略（禁止进程间命名管道）

        一旦抛异常，整个服务会在 startup 阶段挂掉：
            ERROR: Application startup failed. Exiting.

        这属于「环境限制」而非「代码 Bug」，不应该让服务起不来。
        所以这里做自动降级：先按配置尝试 enqueue=True，
        失败则回退到同步写日志（enqueue=False），并打印一行警告说明原因。

        同步写日志的唯一代价是：写磁盘时会阻塞当前线程（日志量大时略有影响），
        功能上完全没有损失，对开发与中小规模生产都够用。
    """
    try:
        return logger.add(sink, **kwargs)
    except PermissionError as exc:
        if not kwargs.get("enqueue"):
            raise  # 本来就没开异步还失败，说明是别的问题，交给上层
        logger.remove()  # 清掉可能已部分注册的 handler
        kwargs["enqueue"] = False
        handler_id = logger.add(sink, **kwargs)
        print(
            f"[日志] 当前环境不允许使用异步日志（{exc}），已自动降级为同步写入。\n"
            f"      如需强制指定，可在 .env 中设置 LOG_ENQUEUE=false",
            file=sys.stderr,
        )
        return handler_id


def setup_logging() -> None:
    """
    初始化日志系统。在 main.py 最开始时调用一次。
    """
    # ------------------------------------------------------------------
    # 1) 准备日志目录
    # ------------------------------------------------------------------
    log_dir = Path(settings.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 2) 移除 loguru 默认 handler，避免重复输出
    # ------------------------------------------------------------------
    logger.remove()

    # ------------------------------------------------------------------
    # 3) 控制台输出（开发时看这个）
    # ------------------------------------------------------------------
    _add_handler_safely(
        sys.stdout,
        level=settings.LOG_LEVEL,
        format=_console_format,
        colorize=True,
        backtrace=not settings.is_prod(),  # 生产环境不打印过长的回溯信息
        diagnose=not settings.is_prod(),   # diagnose 会打印变量值，可能泄露敏感数据，生产必须关
        enqueue=settings.LOG_ENQUEUE,
    )

    # ------------------------------------------------------------------
    # 4) 全量日志文件（按天切割，14 天保留，自动 zip 压缩）
    # ------------------------------------------------------------------
    _add_handler_safely(
        log_dir / "app_{time:YYYY-MM-DD}.log",
        level=settings.LOG_LEVEL,
        rotation="00:00",                 # 每天 0 点切分新文件
        retention=f"{settings.LOG_RETENTION_DAYS} days", # 清理日志
        compression="zip", # 轮转之后的文件的压缩格式
        encoding="utf-8",
        enqueue=settings.LOG_ENQUEUE,

        # 当记录异常时（如 logger.exception()），
        # backtrace=True 会让 Loguru 显示完整的调用栈回溯，而不仅仅是异常发生点。
        backtrace=True,

        # diagnose=True 时，Loguru 会尝试显示异常发生时的局部变量值，对调试非常有用。
        diagnose=False,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
            "{extra[request_id]} | {name}:{function}:{line} | {message}"
        ),
        # 给 extra 一个默认值，避免没有 request_id 时格式化报错
        filter=lambda record: record["extra"].setdefault("request_id", "-") or True,
    )

    # ------------------------------------------------------------------
    # 5) 仅错误日志文件（出问题时先看这个，快速定位）
    # ------------------------------------------------------------------
    _add_handler_safely(
        log_dir / "error_{time:YYYY-MM-DD}.log",
        level="ERROR",
        rotation="00:00",
        retention=f"{settings.LOG_RETENTION_DAYS} days",
        compression="zip",
        encoding="utf-8",
        enqueue=settings.LOG_ENQUEUE,
        backtrace=True,
        diagnose=False,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
            "{extra[request_id]} | {name}:{function}:{line} | {message}"
        ),
        filter=lambda record: record["extra"].setdefault("request_id", "-") or True,
        # dict.setdefault(key, default) 的语义是：
        #
        # 如果字典中已经存在 key，则返回它对应的值，不修改字典。
        #
        # 如果字典中不存在 key，则插入 key: default，并返回 default。
    )

    # ------------------------------------------------------------------
    # 6) 接管标准库 logging（SQLAlchemy / uvicorn / httpx 等）
    # ------------------------------------------------------------------
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    # force如果为 True，basicConfig() 会强制重新配置根 logger:
    # 先移除并关闭根 logger 上所有已存在的 handler，然后再添加新的 handler。
    # 如果不加 force=True，当根 logger 已经有 handler 时，basicConfig() 会什么都不做（直接返回），导致你的配置不生效。

    # 这几个库日志太吵，单独降级
    for noisy in ("uvicorn.access", "sqlalchemy.engine.Engine", "httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # SQLAlchemy 按配置决定是否打印 SQL
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.DB_ECHO else logging.WARNING
    )

    logger.info("日志系统初始化完成 | 级别={} | 目录={}", settings.LOG_LEVEL, log_dir)


# 对外暴露统一入口
__all__ = ["logger", "setup_logging"]
