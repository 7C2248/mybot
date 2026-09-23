# get_weather 工具（未注册占位实现）

## 职责与入口

- 所属类别：`agent/tools` 目录下的工具定义，但**当前未进入图**：不是图节点，也不在默认工具集合中。
- 源码文件：[agent/tools/get_weather.py](../../../../agent/tools/get_weather.py)；工具名 `get_weather`。
- 模块类型：`langchain_core.tools.tool` 装饰器生成的同步 `StructuredTool`，函数体为固定返回值的占位实现（stub）。
- 实际状态：`agent/tools/__init__.py` 顶部导入了 `get_weather`，但默认注册表中的 `#tools.append(get_weather)` 被注释（[__init__.py](../../../../agent/tools/__init__.py)），因此 `build_default_tools` 不会返回它，`create_draft_node` 也不会把它绑定给主模型；`__all__` 中同样未导出。
- 调用方：当前没有调用方。图内不存在任何路径会执行本工具；服务端与上下文逻辑也没有针对它的分支。
- 上游/下游：无（若未来取消注释并加入工具列表，才会获得与其它工具相同的 `draft → tools → draft` 调用与回流路径）。

## 调用链总览

```text
构建阶段
B1. （无工厂函数；模块导入时由 @tool 装饰器直接生成 StructuredTool 实例）
B2. build_default_tools 中的注册语句被注释 → 不加入默认工具列表
      → create_draft_node 不绑定、ToolNode 不持有 → 运行阶段不可达

若未来被注册（当前未发生，仅说明框架行为）
R1. 主模型返回 name="get_weather" 的 tool_call
R2. draft_judge 返回 "tools" → ToolNode 执行
R3. StructuredTool 的同步函数由 BaseTool 默认 _arun 放入线程池执行
R4. 固定字符串成为 ToolMessage content，经 tools → draft 回流
```

## 构建链

### B1. 模块级 `@tool` 定义

- 定位与签名：`@tool def get_weather(location, date)`，同步函数，源码 [get_weather.py](../../../../agent/tools/get_weather.py)。
- 调用方与条件：无。装饰器在模块被导入时执行，`from .get_weather import get_weather`（[__init__.py](../../../../agent/tools/__init__.py)）使包属性 `agent.tools.get_weather` 指向该工具对象。
- 参数：`location`、`date` 两个形参都没有类型标注，`@tool` 据此推断出的 args schema 为：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `location` | 无类型约束（schema 中为 `Any`） | 必填 | 位置参数，函数体未使用 |
| `date` | 无类型约束（schema 中为 `Any`） | 必填 | 位置参数，函数体未使用 |

- 工具描述：函数 docstring `"Get weather of a location, the user should supply the location and date."`，是模型可见的唯一说明。
- 输出：一个名为 `get_weather` 的 `StructuredTool` 实例（同步，无 `_arun` 覆写）。
- 副作用、异常与去向：装饰阶段无副作用；实例留在模块中，因注册语句被注释而不进入任何工具列表。

### B2. 注册条件（实际不生效）

- `build_default_tools` 中相关行为：

| 语句 | 实际效果 |
| --- | --- |
| `#tools.append(get_weather)` | 被注释，永远不执行；`get_weather` 不在返回列表中 |
| `if memory_store is not None: tools.append(create_memory_query_tool(...))` | 与 `get_weather` 无关 |
| `tools.extend(create_file_tools(...))` | 与 `get_weather` 无关 |

- 结论：当前默认工具集合只有 `memory_query`（条件）与 `search_files` / `read_file` / `write_file`；`get_weather` 属于保留源码，未接入调用链。父目录清单见 [tools/README.md](../README.md)。

## 运行链

当前无运行链。以下仅记录若将来把该工具加入工具列表后，框架会如何执行它，**不代表当前已有路径**：

### R1. ToolNode 调用（假设已注册）

- 调用方：`ToolNode` 对 `name == "get_weather"` 的 tool_call 执行 `tool.ainvoke(args)`；参数先经推断出的 schema 校验（缺 `location` 或 `date` 会抛 `ValidationError`，由 ToolNode 默认处理转为 `status="error"` 的 ToolMessage）。
- 隐式输入：无闭包依赖、无配置、无数据库或模型调用。
- 功能：`StructuredTool` 未覆写 `_run`/`_arun`，`ainvoke` 走 `BaseTool._arun` 默认实现，把同步 `_run` 放入线程池执行；函数体无条件返回固定字符串，完全忽略 `location`、`date`。
- 输出：字符串 `"Rain 7~13°C"`（不随输入变化），成为 ToolMessage 的 `content`。
- 副作用：无。
- 异常与边界：函数体不会抛异常；无重试、无超时、无降级逻辑。
- 后续去向：与其它工具相同，`add_messages` 追加到 `AgentState.messages`，经 `tools → draft` 边回流；服务端在 `tools` 节点前发布 phase `"using_tools"`（[server/services/agent.py](../../../../server/services/agent.py)）。

## 分支与异常链

| 条件 | 处理函数与行为 | 输出 | 去向 |
| --- | --- | --- | --- |
| 默认构建（当前实际情况） | `build_default_tools` 不加入该工具；模型无法生成 `get_weather` 调用 | 无 | 不进入图 |
| 未来手动加入工具列表且参数缺失 | schema 校验失败，ToolNode 转错误 ToolMessage | `ToolMessage(status="error")` | draft 再次生成 |
| 未来手动加入且调用成功 | 固定字符串返回 | `"Rain 7~13°C"` | 同上 |

## 输入输出示例

以下示例仅用于说明该占位实现的契约，当前图不会产生这类调用：

输入（tool_call args）：

```json
{"location": "上海", "date": "2026-09-23"}
```

输出（ToolMessage content）：

```text
Rain 7~13°C
```

## 关联文档与验证依据

- 上级目录：[tools/README.md](../README.md)
- 同级工具：[memory_query](../memory_query/README.md) · [file_system](../file_system/README.md)
- 源码：[get_weather.py](../../../../agent/tools/get_weather.py) · [tools/__init__.py](../../../../agent/tools/__init__.py) · [builder.py](../../../../agent/builder.py)
- 验证依据：本文档基于源码静态阅读整理；`tests/` 中未发现针对 `get_weather` 的测试，也未在本次文档编写中执行该工具。注册状态可由 `agent/tools/__init__.py` 中被注释的 `#tools.append(get_weather)` 直接核对。
