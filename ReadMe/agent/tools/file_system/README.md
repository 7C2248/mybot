# file_system 工具集

## 职责与入口

- 所属类别：`agent/tools` 目录下的异步文件工具集，一个模块提供三个 `langchain_core.tools.BaseTool` 子类；**不是图节点**。
- 源码文件：[agent/tools/file_system.py](../../../../agent/tools/file_system.py)；工具名分别为 `search_files`、`read_file`、`write_file`。
- 职责：让主 LLM 在工作区根目录内按 glob 模式检索文件路径、读取 UTF-8 文本、写入/覆盖 UTF-8 文本；所有输入路径解析后必须位于工作区根目录内，越界直接返回错误。
- 工厂函数：`create_file_tools(workspace_root=None)`，一次返回 `[FileSearchTool, FileReadTool, FileWriteTool]` 三个实例。
- 调用方式：由 `agent/builder.py` 注册的 `tools` 节点（LangGraph `ToolNode`）在收到主模型对应工具调用后异步执行各自的 `_arun`。
- 上游：draft 节点中的主模型；下游：ToolMessage 经 `tools → draft` 边回流给主模型。

## 调用链总览

```text
构建阶段
B1. create_file_tools(workspace_root=None)
      → [FileSearchTool, FileReadTool, FileWriteTool]（各自持有同一个 workspace_root）
B2. build_default_tools 无条件调用 B1 并加入默认列表
B3. create_draft_node：llm.bind_tools(tools, parallel_tool_calls=True)
      同时 builder 注册节点 "tools" = ToolNode(tools)

运行阶段（三个入口共享同一执行与回流机制）
R0. _resolve_path(workspace_root, path)：read_file / write_file 共用的包含性解析辅助函数
R1. search_files._arun(pattern, limit)   → glob 检索 + 包含性过滤 → _FileSearchResult JSON
R2. read_file._arun(path, offset, limit) → 校验路径与文件 → 分段读取 → _FileReadResult JSON
R3. write_file._arun(path, content)      → 校验路径 → 建父目录并覆盖写入 → _FileWriteResult JSON
R4. ToolNode 把返回字符串包装为 ToolMessage → add_messages 追加到 AgentState.messages
R5. 框架沿 "tools" → "draft" 边再次执行 draft，主模型读取结果
```

## 构建链

### B1. create_file_tools

- 定位与签名：`create_file_tools(workspace_root=None) -> list[BaseTool]`，同步函数，源码 [file_system.py](../../../../agent/tools/file_system.py)。
- 调用方与条件：`agent/tools/__init__.py` 的 `build_default_tools` 无条件调用；`builder.py` 未传 `workspace_root`，因此默认值为 `None`。

| 输入 | 类型 | 必填或默认值 | 来源与含义 |
| --- | --- | --- | --- |
| `workspace_root` | `Path`、路径字符串或 `None`（源码声明为 `Any`） | 默认 `None` | 工作区根目录；为 `None` 时各工具回退到模块常量 `_DEFAULT_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]`，即项目根目录 |

功能与内部调用：依次构造 `FileSearchTool(workspace_root=workspace_root)`、`FileReadTool(workspace_root=workspace_root)`、`FileWriteTool(workspace_root=workspace_root)`，顺序固定；三个实例共享同一个 `workspace_root` 值，互不引用。

输出：包含三个工具实例的列表。固定属性如下：

| 工具名 | 类 | `args_schema` | 关键行为 |
| --- | --- | --- | --- |
| `search_files` | `FileSearchTool` | `_FileSearchInput` | glob 匹配工作区内路径，返回相对根目录的 POSIX 路径列表 |
| `read_file` | `FileReadTool` | `_FileReadInput` | 读取 UTF-8 文本并按行分段返回 |
| `write_file` | `FileWriteTool` | `_FileWriteInput` | 自动创建父目录，覆盖写入 UTF-8 文本 |

三个输入模型都继承 `_StrictModel`（`ConfigDict(extra="forbid")`），未知参数会被 pydantic 拒绝。

副作用、异常与去向：构造过程无 I/O、无异常；实例进入工具列表后由 `create_draft_node` 绑定到主模型，并由 `ToolNode` 持有用于执行。`recovery_only=True` 时工具列表整体为空，本模块不会被实例化。

### B2. 注册与绑定

- `build_default_tools` 中 `tools.extend(create_file_tools(workspace_root=workspace_root))` 无条件执行，因此三个文件工具始终出现在默认工具集合里（唯一例外是 `recovery_only` 时工具列表整体为 `[]`）。详见父文档 [tools/README.md](../README.md)。
- `create_draft_node`（[draft.py](../../../../agent/node/draft.py)）执行 `llm.bind_tools(tools, parallel_tool_calls=True)`，允许模型一次返回多个并行工具调用；draft 会校验调用 ID 与工具名后再交给 `ToolNode`。
- `builder.py` 注册节点 `"tools"`：`ToolNode(tools)`（[builder.py](../../../../agent/builder.py)）。

## 运行链

### R0. _resolve_path（read_file / write_file 共用）

- 定位与签名：`_resolve_path(workspace_root, path: str) -> Path | None`，同步模块级函数，源码 [file_system.py](../../../../agent/tools/file_system.py)。
- 调用方与条件：`FileReadTool._arun` 与 `FileWriteTool._arun` 的每一步都先调用它；`FileSearchTool` 不调用，而是在遍历 glob 结果时内联执行同样的包含性判断。

| 输入参数 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `workspace_root` | `Any` | 实例属性 `self.workspace_root` | `None` 时回退 `_DEFAULT_WORKSPACE_ROOT` | 限定访问边界 |
| `path` | `str` | 模型生成的工具参数 | 必填、非空（schema 已保证） | 相对或绝对的目标路径 |

功能与内部调用：

1. `root = Path(workspace_root or _DEFAULT_WORKSPACE_ROOT).resolve()`，把根目录规范化为绝对路径（解析符号链接）。
2. `candidate = Path(path)`；若不是绝对路径，则 `candidate = root / candidate`。
3. `resolved = candidate.resolve()`，规范 `..` 与符号链接。
4. 若 `resolved == root`（指向根目录本身）或 `not resolved.is_relative_to(root)`（越界），返回 `None`；否则返回解析后的绝对路径。

输出：位于工作区内的绝对 `Path`，或表示拒绝的 `None`。异常与边界：`Path` 解析本身可能因非法路径抛异常，由调用方 `_arun` 的 `try/except` 兜底；本函数不捕获异常。

### R1. FileSearchTool._arun（工具名 search_files）

- 定位与签名：`FileSearchTool._arun(self, pattern: str, limit: int = 200) -> str`，异步方法，源码 [file_system.py](../../../../agent/tools/file_system.py)。
- 调用方与条件：`ToolNode` 对名为 `search_files` 的 tool_call 调用 `tool.ainvoke`，经 `BaseTool` 参数校验后进入 `_arun`。

| 输入参数 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `pattern` | `str` | 模型 tool_call args | 必填；`min_length=1`；支持 `*` `?` `[]` 与 `**` 递归匹配，如 `scripts/**/*.py` | glob 匹配模式，相对于工作区根目录 |
| `limit` | `int` | 同上 | 默认 200；严格整数（`strict=True`，布尔值被拒），范围 1–1000 | 命中路径数量上限，达到后提前停止 |

隐式输入：`self.workspace_root`（`None` 时回退项目根目录）；日志器 `utils.daily_logger.get_logger("tools.file_system")`。

功能与内部调用（按执行顺序）：

1. `root = Path(self.workspace_root or _DEFAULT_WORKSPACE_ROOT).resolve()`。
2. 用 `sorted(glob(str(root / pattern), recursive=True))` 获取匹配路径，排序保证输出稳定；`recursive=True` 使 `**` 生效。
3. 逐个 `Path(match).resolve()`；跳过等于 `root` 或不在 `root` 内的结果（包含性过滤，绝对 pattern、`..`、指向工作区外的符号链接都会在此被排除）。
4. 通过过滤的路径用 `relative_to(root).as_posix()` 转为相对根目录的 POSIX 风格字符串（统一 `/` 分隔符）加入 `matches`。
5. 每加入一条就检查 `len(matches) >= limit`，达到上限即 `break`；因此返回数量不超过 `limit`。
6. `logger.info` 记录 pattern 与命中数。
7. 返回 `_FileSearchResult(status="ok", matches=matches).model_dump_json()`。
8. 任意异常（非法 glob 模式、权限、路径类型错误等）被 `except Exception` 捕获，记录 warning 后返回 `_FileSearchResult(status="error", error=type(exc).__name__).model_dump_json()`。

| 输出或状态字段 | 类型 | 产生条件 | 含义与更新方式 | 消费方 |
| --- | --- | --- | --- | --- |
| 返回字符串（JSON） | `str` | 总是返回 | 序列化的 `_FileSearchResult` | ToolNode 包装为 ToolMessage |
| `status` | `"ok"` / `"error"` | 成功 / 异常 | 检索是否完成 | 主模型 |
| `matches` | `list[str]` | 仅 `ok` 有值 | 相对工作区根目录的路径列表，可能包含目录；超过 `limit` 的命中被截断 | 主模型 |
| `error` | `str \| None` | 仅 `error` 时为异常类名 | 失败类型标识 | 主模型 |

副作用：只读文件系统；写日志。异常与边界：工具内部不抛异常；无命中时返回 `status="ok"` 且 `matches=[]`；不保证匹配项是文件（glob 也会返回目录）。

### R2. FileReadTool._arun（工具名 read_file）

- 定位与签名：`FileReadTool._arun(self, path: str, offset: int = 0, limit: int = 2000) -> str`，异步方法，源码 [file_system.py](../../../../agent/tools/file_system.py)。
- 调用方与条件：`ToolNode` 对名为 `read_file` 的 tool_call 执行。

| 输入参数 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `path` | `str` | 模型 tool_call args | 必填；`min_length=1`；相对工作区根目录的文本文件路径 | 目标文件 |
| `offset` | `int` | 同上 | 默认 0；`ge=0`，非严格整数 | 起始行号（0 基），用于分段读取 |
| `limit` | `int` | 同上 | 默认 2000；严格整数（`strict=True`），范围 1–10000 | 本次最多读取的行数 |

隐式输入：`self.workspace_root`；日志器。

功能与内部调用（按执行顺序）：

1. `resolved = _resolve_path(self.workspace_root, path)`（步骤 R0）；为 `None` 时立即返回 `_FileReadResult(status="error", error="路径越出工作区根目录")`。
2. `resolved.is_file()` 为假时返回 `_FileReadResult(status="error", error="目标不存在或不是文件")`（目录、不存在路径都走此分支）。
3. `resolved.read_text(encoding="utf-8", errors="replace").splitlines()`：按 UTF-8 读取，非法字节替换为替换符而不报错；`splitlines()` 去掉行分隔符，同时按多种 Unicode 换行边界切分。
4. `chunk = lines[offset:offset + limit]`；`offset` 超出文件长度时为空列表，仍返回成功。
5. `logger.info` 记录原始 `path`、总行数、offset。
6. 返回 `_FileReadResult(status="ok", path=path, content="\n".join(chunk), total_lines=len(lines)).model_dump_json()`；`path` 是模型传入的原始路径字符串，不是解析后的绝对路径。
7. 其他异常被 `except Exception` 捕获，返回 `_FileReadResult(status="error", error=type(exc).__name__)`。

| 输出或状态字段 | 类型 | 产生条件 | 含义与更新方式 | 消费方 |
| --- | --- | --- | --- | --- |
| 返回字符串（JSON） | `str` | 总是返回 | 序列化的 `_FileReadResult` | ToolNode |
| `status` | `"ok"` / `"error"` | 成功 / 越界、非文件或异常 | 读取是否完成 | 主模型 |
| `path` | `str \| None` | 仅 `ok` 为原始输入路径 | 便于模型对应请求 | 主模型 |
| `content` | `str` | 仅 `ok` | 选中行以 `\n` 连接；不保留原始行尾符；空文件或 offset 越界为 `""` | 主模型 |
| `total_lines` | `int` | 仅 `ok` | 文件总行数，便于规划后续 offset | 主模型 |
| `error` | `str \| None` | `error` 时为固定中文提示或异常类名 | 失败原因 | 主模型 |

副作用：只读文件系统；写日志。异常与边界：工具内部不抛异常；二进制文件不会被识别（按 UTF-8 替换解码）；超大文件按 `limit` 截断，需多次分段读取。

### R3. FileWriteTool._arun（工具名 write_file）

- 定位与签名：`FileWriteTool._arun(self, path: str, content: str) -> str`，异步方法，源码 [file_system.py](../../../../agent/tools/file_system.py)。
- 调用方与条件：`ToolNode` 对名为 `write_file` 的 tool_call 执行。

| 输入参数 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `path` | `str` | 模型 tool_call args | 必填；`min_length=1`；相对工作区根目录的目标路径 | 写入目标，已存在将被覆盖 |
| `content` | `str` | 同上 | 必填；`min_length=1`，空字符串会被 schema 拒绝 | 要写入的完整文本内容 |

隐式输入：`self.workspace_root`；日志器。

功能与内部调用（按执行顺序）：

1. `resolved = _resolve_path(self.workspace_root, path)`（步骤 R0）；为 `None` 时返回 `_FileWriteResult(status="error", error="路径越出工作区根目录")`。
2. `resolved.parent.mkdir(parents=True, exist_ok=True)`：自动创建缺失的父目录。
3. `resolved.write_text(content, encoding="utf-8")`：以 UTF-8 覆盖写入；不追加换行，不保留旧内容。
4. `logger.info` 记录原始 `path` 与字符数。
5. 返回 `_FileWriteResult(status="ok", path=path, chars_written=len(content)).model_dump_json()`；`chars_written` 是 Python 字符数，不是字节数。
6. 其他异常被 `except Exception` 捕获，返回 `_FileWriteResult(status="error", error=type(exc).__name__)`。

| 输出或状态字段 | 类型 | 产生条件 | 含义与更新方式 | 消费方 |
| --- | --- | --- | --- | --- |
| 返回字符串（JSON） | `str` | 总是返回 | 序列化的 `_FileWriteResult` | ToolNode |
| `status` | `"ok"` / `"error"` | 成功 / 越界或异常 | 写入是否完成 | 主模型 |
| `path` | `str \| None` | 仅 `ok` 为原始输入路径 | 便于模型对应请求 | 主模型 |
| `chars_written` | `int` | 仅 `ok` | 写入字符数 | 主模型 |
| `error` | `str \| None` | `error` 时为固定中文提示或异常类名 | 失败原因 | 主模型 |

副作用：创建目录、覆盖写入文件；写日志。异常与边界：工具内部不抛异常；覆盖不可撤销，工具描述要求模型“写入前先用 read_file 确认目标文件现状”。

### R4. ToolNode 执行与消息回流

- `ToolNode` 逐个异步执行 tool_call：参数校验失败（如未知字段、越界 limit、空 pattern）抛出 pydantic `ValidationError`，由 ToolNode 默认处理转为 `status="error"` 的 ToolMessage，不进入 `_arun`；`_arun` 内部异常已被工具自身捕获，不会冒泡。
- ToolNode 返回 `{"messages": [ToolMessage, ...]}`，`name` 为工具名，`tool_call_id` 对应调用 ID；一次 AIMessage 的多个并行调用会生成多条 ToolMessage，由 `add_messages` reducer 追加到 `AgentState.messages`。
- 框架沿 `tools → draft` 边再次执行 draft，主模型读取全部 ToolMessage；不再发起工具调用时进入 `check`。draft 的绑定与路由细节见 [node/draft 文档](../../node/draft/README.md)。
- 服务端对非 `memory_query` 的 ToolMessage 不发布专门事件，仅在 `tools` 节点前发布 phase `"using_tools"`（[server/services/agent.py](../../../../server/services/agent.py)）。

## 分支与异常链

| 条件 | 处理函数与行为 | 输出 | 去向 |
| --- | --- | --- | --- |
| `read_file` / `write_file` 路径解析后越界或等于根目录 | `_resolve_path` 返回 `None` | `status="error"`，`error="路径越出工作区根目录"` | ToolMessage 回流，模型可改用工作区内路径 |
| `read_file` 目标不存在或是目录 | `_arun` 前置判断 | `status="error"`，`error="目标不存在或不是文件"` | 同上 |
| `search_files` 模式非法或 glob/解析异常 | `_arun` 捕获 | `status="error"`，`error` 为异常类名 | 同上 |
| `read_file` 文件非 UTF-8 | 以 `errors="replace"` 解码，不报错 | `status="ok"`，非法字节被替换 | 同上 |
| 工具参数不满足 schema（空 pattern、空 path、空 content、limit 越界、多余字段等） | `BaseTool` 校验阶段抛 `ValidationError`，ToolNode 转错误 ToolMessage | `ToolMessage(status="error")` | 同上 |
| `search_files` 命中超过 `limit` | 达到上限立即停止 | `status="ok"`，`matches` 被截断 | 模型可调小模式或分段查询 |
| `read_file` 的 `offset` 超出文件长度 | 切片为空 | `status="ok"`，`content=""`，`total_lines` 仍为真实值 | 同上 |
| `recovery_only=True` | `tools=[]`，`tools` 节点为空节点 | 无工具执行 | 不产生新的工具调用 |

## 输入输出示例

示例 1（步骤 R1，search_files）：

输入：`{"pattern": "agent/tools/*.py", "limit": 200}`

输出：

```json
{"status":"ok","matches":["agent/tools/__init__.py","agent/tools/file_system.py","agent/tools/get_weather.py","agent/tools/memory_query.py"],"error":null}
```

示例 2（步骤 R2，read_file，越界路径）：

输入：`{"path": "../secret.txt", "offset": 0, "limit": 2000}`

输出：

```json
{"status":"error","path":null,"content":"","total_lines":0,"error":"路径越出工作区根目录"}
```

示例 3（步骤 R2，分段读取）：

输入：`{"path": "README.md", "offset": 0, "limit": 2000}`

输出：

```json
{"status":"ok","path":"README.md","content":"# 项目说明\n...","total_lines":120,"error":null}
```

示例 4（步骤 R3，write_file）：

输入：`{"path": "data/tmp/note.txt", "content": "hi"}`

输出：

```json
{"status":"ok","path":"data/tmp/note.txt","chars_written":2,"error":null}
```

## 关联文档与验证依据

- 上级目录：[tools/README.md](../README.md)
- 同级工具：[memory_query](../memory_query/README.md) · [get_weather](../get_weather/README.md)
- 图内循环：[node/draft（bind_tools、draft_judge、工具结果回流）](../../node/draft/README.md)
- 源码：[file_system.py](../../../../agent/tools/file_system.py) · [tools/__init__.py](../../../../agent/tools/__init__.py) · [builder.py](../../../../agent/builder.py) · [draft.py](../../../../agent/node/draft.py)
- 验证依据：本文档基于源码静态阅读整理；当前 `tests/` 中未发现针对三个文件工具的专项测试，未执行文件系统读写验证。工具名与注册关系可由 `tests/test_module_layout.py` 的导入检查间接覆盖。
