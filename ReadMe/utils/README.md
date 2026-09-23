# utils（跨应用共享工具）

根目录 `utils/` 提供与业务无关的通用能力：按日期分文件的应用日志与时间格式化。Agent 专用工具在 [agent/utils/](../agent/utils/README.md)，不要与本目录混淆。

## 模块

| 名称 | 类型 | 主要功能 | 使用方 | 源码 |
| --- | --- | --- | --- | --- |
| `utils/daily_logger.py` | 基础设施 | 单例 logger、上下文、结构化事件/失败日志、按天切文件、uvicorn 接入 | Agent 全部模块、server、cli、scripts | [daily_logger.py](../../utils/daily_logger.py) |
| `utils/time.py` | 工具函数 | `get_time()` 当前时间字符串 | `agent/node/draft.py` 等 | [time.py](../../utils/time.py) |

## daily_logger

- 日志目录：`data/log/YYYY-MM-DD.log`（由 `utils/daily_logger.py` 的 `LOG_DIR` 计算，跨天自动切换）。
- 格式：`时间 [级别] [logger 名]{上下文} 消息`；上下文以 JSON 追加。

| 函数 | 签名 | 行为 |
| --- | --- | --- |
| `get_logger(name)` | `(name: str) -> logging.Logger` | 全局共享一个带锁文件句柄，避免线程交错写入；同一 name 返回同一 logger |
| `logging_context(**fields)` | 上下文管理器 | 用 `ContextVar` 为当前请求/任务附加 `run_id`/`job_id`/`thread_id`/`character` 等字段，不跨任务串线 |
| `log_event(logger, event, *, level=INFO, **fields)` | 结构化事件 | 输出“事件 \| {JSON}”，JSON 转义换行，保证每个事件一行 |
| `log_failure(logger, event, error, **fields)` | 失败日志 | 额外记录 `error_type` 与调用栈位置；不输出异常消息本身（可能含连接串/凭据） |
| `configure_backend_logging()` | 服务初始化 | 把 `uvicorn`/`uvicorn.error`/`uvicorn.access` 的 handler 指向后端文件（级别 WARNING），异常也进入后端日志 |
| `log_agent_init(character_name, thread_id=None, extra=None)` | 初始化标记 | 与上一条日志之间至少保留 5 行空行，便于阅读 |

- 类 `_DailyFileHandler`：按日期打开/切换文件，`emit` 时注入上下文；`_ensure_gap_lines` 保证初始化块前有空行。
- 设计约束：日志不依赖模型配置解析，只读 API 在没有模型时也能启动。

## time

- `get_time()`：[utils/time.py:5](../../utils/time.py#L5)。返回 `datetime.now().strftime("%Y-%m-%d %A %H:%M")`，用于 `commit_reply` 给正式回复加时间戳。

## 使用边界

- 业务日志应使用 `get_logger("模块名")` 获取 logger，并优先用 `log_event`/`log_failure` 输出结构化字段。
- 记忆变更全文、正式回复全文等敏感/长文本由调用方决定是否记录；`log_failure` 不记录异常消息。
- 日志文件与数据库事务不构成同一事务，进程在提交与写日志之间被强制终止时可能缺少最后一条日志。

## 阅读导航

- 上级：[系统总览](../README.md) · Agent 工具：[agent/utils/README.md](../agent/utils/README.md)
- 使用方：[server/README.md](../server/README.md)（后端日志一节）· [agent/memory/README.md](../agent/memory/README.md)
