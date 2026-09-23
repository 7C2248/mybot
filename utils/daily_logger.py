# utils/daily_logger.py
# 按日期分文件的应用日志：log/2026-09-07.log
# - get_logger(name) 获取单例 logger（仅写文件，不输出控制台）
# - log_agent_init(...) 记录 agent 初始化，且与上一次日志内容至少间隔 5 行空行

import json
import logging
import threading
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

# 日志不能依赖模型配置解析，否则无模型的只读 API 也可能无法启动。
LOG_DIR = Path(__file__).resolve().parents[1] / "data" / "log"
_INIT_GAP_LINES = 5  # 初始化日志与上一次日志内容之间的最小空行数

_FORMATTER = logging.Formatter(
    "%(asctime)s [%(levelname)s] [%(name)s]%(context)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_handlers: dict[str, "_DailyFileHandler"] = {}
_loggers: dict[str, logging.Logger] = {}
_lock = threading.RLock()
_context = ContextVar("mybot_log_context", default={})
_shared_handler = None


@contextmanager
def logging_context(**fields):
    """请求、对话和记忆任务各自携带上下文，不跨线程/异步任务串线。"""
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def log_event(logger, event, *, level=logging.INFO, **fields):
    """JSON 转义换行，完整保存正文且每个业务事件保持一条日志。"""
    logger.log(level, "%s | %s", event, json.dumps(fields, ensure_ascii=False, default=str))


def log_failure(logger, event, error, **fields):
    # 保留调用栈和错误类型；异常消息可能包含数据库连接串或请求凭据。
    log_event(logger, event, level=logging.ERROR, error_type=type(error).__name__,
              traceback="".join(f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}\n'
                                for frame in traceback.extract_tb(error.__traceback__)), **fields)


class _DailyFileHandler(logging.Handler):
    """按日期切换日志文件：log/YYYY-MM-DD.log，跨天自动切换新文件。"""

    def __init__(self, name: str, level=logging.DEBUG):
        super().__init__(level)
        self._name = name
        self._stream = None
        self._date = None

    def _open(self, date_str: str):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"{date_str}.log"
        self._stream = path.open("a", encoding="utf-8")
        self._date = date_str
        self.baseFilename = str(path)

    def _ensure_stream(self, date_str: str):
        if self._stream is None or self._date != date_str:
            if self._stream is not None:
                try:
                    self._stream.close()
                except OSError:
                    pass
            self._open(date_str)

    def emit(self, record: logging.LogRecord):
        try:
            context = _context.get()
            record.context = (" " + json.dumps(context, ensure_ascii=False, default=str)) if context else ""
            self._ensure_stream(datetime.now().strftime("%Y-%m-%d"))
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self):
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
            self._stream = None
        super().close()


def get_logger(name: str) -> logging.Logger:
    """整个后端共用一个带锁的文件句柄，跨天切换，避免线程交错写入。"""
    global _shared_handler
    with _lock:
        if name in _loggers:
            return _loggers[name]
        if _shared_handler is None:
            _shared_handler = _DailyFileHandler("backend")
            _shared_handler.setFormatter(_FORMATTER)
        logger = logging.getLogger(f"mybot.{name}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.addHandler(_shared_handler)
        _handlers[name] = _shared_handler
        _loggers[name] = logger
        return logger


def configure_backend_logging():
    """Uvicorn 异常也进入后端日志；生命周期由服务统一记录。"""
    handler = get_logger("server").handlers[0]
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.WARNING)


def _ensure_gap_lines(handler: _DailyFileHandler, gap: int) -> None:
    """保证当前日志文件末尾与下一条记录之间至少 gap 行空行。"""
    try:
        with open(handler.baseFilename, "r+b") as f:
            f.seek(0, 2)
            size = f.tell()
            read_start = max(0, size - 4096)
            f.seek(read_start)
            content = f.read()

            trailing_newlines = 0
            for b in reversed(content):
                if b == 0x0A:
                    trailing_newlines += 1
                else:
                    break

            if not content:
                need = 0  # 新文件，无需空行
            else:
                need = gap - (trailing_newlines - 1)
            if need > 0:
                f.seek(0, 2)
                f.write(b"\n" * need)
                f.flush()
    except OSError:
        pass


def log_agent_init(character_name: str, thread_id: str = None,
                   extra: dict = None) -> None:
    """记录一次 agent 初始化，与上一次日志内容之间至少间隔 5 行空行。"""
    logger = get_logger("init")
    handler = _handlers.get("init")
    if handler is not None:
        with handler.lock:
            handler._ensure_stream(datetime.now().strftime("%Y-%m-%d"))
            _ensure_gap_lines(handler, _INIT_GAP_LINES)

    info = f"Agent 初始化 | 角色={character_name}"
    if thread_id:
        info += f" | thread_id={thread_id}"
    if extra:
        info += " | " + " ".join(f"{k}={v}" for k, v in extra.items())
    logger.info("=" * 40)
    logger.info(info)
    logger.info("=" * 40)
