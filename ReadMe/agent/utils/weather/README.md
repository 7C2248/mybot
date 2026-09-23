# weather — 和风天气实时天气客户端

## 职责与入口

- 所属类别：`agent/utils/` 支撑模块（外部服务客户端），**不是图节点**，无图注册名、无路由。
- 源码：[`agent/utils/weather.py`](../../../../agent/utils/weather.py)（模块 docstring：和风天气实时天气客户端：Ed25519 JWT 认证、结果缓存与失败降级；仅在配置齐全时请求，任何失败都返回 `None`）。
- 职责：按 `config/config.py` 中的 `QWEATHER_*` 配置请求和风天气实时天气接口，生成并缓存 Ed25519 JWT，缓存天气文本，失败时静默降级为 `None`。
- 公开入口：`fetch_current_weather`（`__all__` 只导出该函数），异步函数。
- 内部函数：`_configured`、`_request_weather`、`_token`、`_private_key`、`_api_host`、`_cache_ttl`、`_text`；模块级缓存 `_token_cache`、`_weather_cache`。
- 调用方式：由 `world_state_update` 节点在每轮运行阶段 `await` 调用。

## 调用链总览

```text
R1 fetch_current_weather()  (async)
  ├─ 命中 _weather_cache（value 非 None 且未过期）→ 直接返回缓存
  ├─ R1.1 _configured()                                  ← 配置不齐直接返回 None
  │     ├─ _api_host() / _private_key() 等辅助
  ├─ asyncio.to_thread(R1.2 _request_weather)            ← 在线程中执行同步 HTTP
  │     ├─ R1.3 _token()                                 ← JWT 生成/缓存
  │     └─ requests.get(...)
  └─ 成功 → 写 _weather_cache(value, now + R1.4 _cache_ttl()) → 返回文本
      失败 → logger.warning → 返回 None

调用方
  agent/node/world_state.py update_world_state  → R1；None 时保留旧天气
```

## 构建链

无工厂；模块导入时只创建两个缓存字典，不读取环境变量、不发起网络请求。`fetch_current_weather` 首次调用时才检查配置并按需导入 `jwt` / `requests`。

## 运行链

### R1. `fetch_current_weather`

- 定位与签名：`async def fetch_current_weather() -> str | None`，[`agent/utils/weather.py:122`](../../../../agent/utils/weather.py)，异步函数。
- 调用方与条件：`agent/node/world_state.py` 的 `update_world_state` 每轮无条件 `await`（[`agent/node/world_state.py:15`](../../../../agent/node/world_state.py)）；返回 `None` 时节点保留旧天气。

输入参数或字段：无参数。隐式输入（全部来自 `config/config.py` 读取的环境变量）：

| 隐式输入 | 类型 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- |
| `QWEATHER_API_HOST` | `str` | 默认 `""` | API 主机；无 `://` 时自动补 `https://` |
| `QWEATHER_LATITUDE` / `QWEATHER_LONGITUDE` | `str` | 默认 `""` | 经纬度，参与 URL 路径 |
| `QWEATHER_KEY_ID` | `str` | 默认 `""` | JWT header 的 `kid` |
| `QWEATHER_PROJECT_ID` | `str` | 默认 `""` | JWT `sub` |
| `QWEATHER_DEVELOPER_ID` | `str` | 默认 `""` | JWT `iss` |
| `QWEATHER_PRIVATE_KEY_PATH` / `QWEATHER_PRIVATE_KEY` | `str` | 默认 `""` | Ed25519 私钥（优先路径，路径可为相对 `PROJECT_ROOT` 的相对路径） |
| `QWEATHER_LANG` | `str` | 默认 `"zh"` | 请求参数 `lang`，不参与 `_configured` 校验 |
| `QWEATHER_CACHE_TTL` | `str` | 默认 `"1200"` | 天气缓存秒数；非法或非正数时用 `_DEFAULT_CACHE_TTL` |
| `PROJECT_ROOT` | `Path` | 固定 | 相对私钥路径的基准目录 |

模块常量：`_DEFAULT_CACHE_TTL = 1200.0`、`_REQUEST_TIMEOUT = 10.0`、`_JWT_LIFETIME = 900`、`_JWT_REFRESH_MARGIN = 60`。

功能与内部调用：

1. `now = time.monotonic()`（天气缓存用单调时钟）；若 `_weather_cache["value"] is not None and now < expires_at`，直接返回缓存文本。
2. 调 R1.1 `_configured()`；为假直接返回 `None`（不请求、不写缓存）。
3. `weather = await asyncio.to_thread(_request_weather)`——同步 `requests` 调用放到线程池，避免阻塞事件循环。
4. 捕获 `Exception`：`logger.warning("实时天气请求失败: ...")` 后返回 `None`。
5. `weather is None`（防御分支）→ 返回 `None`。
6. 成功：`_weather_cache.update(value=weather, expires_at=now + _cache_ttl())`，`logger.info`，返回文本。

输出：天气文本，形如 `"少云 32°C"`（有数值温度时四舍五入并带 `°C`），无温度数值时仅天气现象文本；未配置、请求失败或异常时返回 `None`。

| 输出 | 类型 | 产生条件 | 含义与消费方 |
| --- | --- | --- | --- |
| 天气文本 | `str` | 配置齐全且请求成功 | `update_world_state` 写入 `world_state["weather"]` |
| `None` | `None` | 未配置 / 请求异常 / 响应缺天气现象 | 节点保留旧 `weather` 字段 |

副作用：HTTP GET 请求；更新进程内天气缓存；写日志。不写数据库、不改文件。

异常与边界：

- 未配置（缺任一必填项或私钥不可读）→ 返回 `None`，不抛异常；
- HTTP 非 2xx（`raise_for_status`）、超时（10 秒）、JSON 解析失败、响应缺 `condition.text`（抛 `ValueError`）→ 均被捕获，返回 `None`；
- 缓存命中时即使后续配置被改坏也直接返回缓存值；
- 缓存以 `value is not None` 为有效标志，失败不写缓存，因此下一轮会重试。

后续去向：返回 `update_world_state`；非 `None` 且与旧天气不同时更新状态并记录日志。

### R1.1. `_configured`

- 定位与签名：`_configured() -> bool`，[`agent/utils/weather.py:68`](../../../../agent/utils/weather.py)，同步内部函数。
- 调用方与条件：R1 在缓存未命中后调用。

功能与内部调用：

1. `all((_api_host(), _text(QWEATHER_LATITUDE), _text(QWEATHER_LONGITUDE), _text(QWEATHER_KEY_ID), _text(QWEATHER_PROJECT_ID), _text(QWEATHER_DEVELOPER_ID)))`——主机、经纬度、Key ID、Project ID、Developer ID 全部非空是前置条件；
2. 尝试 `bool(_private_key())`：私钥路径读取抛 `OSError` 时记录 `logger.warning("天气私钥不可读: ...")` 并返回 `False`；
3. 全部通过返回 `True`。

输出：`bool`。副作用：可能读取私钥文件；私钥不可读时写日志。异常与边界：`_private_key` 的 `OSError` 被捕获；其他异常（如 `UnicodeDecodeError`）不捕获。后续去向：返回 R1。

### R1.2. `_request_weather`

- 定位与签名：`_request_weather() -> str | None`，[`agent/utils/weather.py:100`](../../../../agent/utils/weather.py)，同步内部函数（在 `asyncio.to_thread` 中执行）。
- 调用方与条件：仅 R1 在线程池中调用。

功能与内部调用：

1. 延迟导入 `requests`；
2. `requests.get(f"{_api_host()}/weather/v1/current/{lat}/{lon}", headers={"Authorization": f"Bearer {_token()}"}, params={"lang": _text(QWEATHER_LANG) or "zh"}, timeout=10.0)`；
3. `response.raise_for_status()`；
4. `payload = response.json()`；取 `payload["condition"]["text"]`，为空抛 `ValueError(f"响应缺少天气现象: {payload}")`；
5. 取 `payload["temperature"]["value"]`，若是 `int`/`float` 且不是 `bool`，返回 `f"{text} {round(temperature)}°C"`；否则返回纯现象文本。

输出：天气文本；异常路径向上抛给 R1 捕获。副作用：网络请求。异常与边界：非 2xx、超时、连接错误、JSON 结构不符均抛出，由 R1 统一降级为 `None`。后续去向：返回 R1。

### R1.3. `_token`

- 定位与签名：`_token() -> str`，[`agent/utils/weather.py:80`](../../../../agent/utils/weather.py)，同步内部函数。
- 调用方与条件：仅 R1.2 构造请求头时调用。

功能与内部调用：

1. `now = time.time()`；若 `_token_cache["value"]` 非空且 `now < expires_at` 直接返回缓存 token；
2. 延迟导入 `jwt`；
3. `issued_at = int(now) - 30`（回拨 30 秒容忍时钟偏差）；
4. `jwt.encode({"iss": developer_id, "sub": project_id, "iat": issued_at, "exp": issued_at + 900}, _private_key(), algorithm="EdDSA", headers={"kid": key_id})`；
5. `_token_cache.update(value=token, expires_at=now + 900 - 60)`——提前 60 秒过期以便自动刷新；
6. 返回 token。

输出：JWT 字符串。副作用：读取私钥；更新模块级 `_token_cache`。异常与边界：私钥读取失败或签名失败向上抛出，由 R1 捕获。后续去向：拼进 `Authorization: Bearer <token>`。

### R1.4. 辅助函数

| 函数 | 签名 | 行为 | 边界 |
| --- | --- | --- | --- |
| `_text` | `_text(value) -> str`，[weather.py:39](../../../../agent/utils/weather.py) | 字符串则 `strip()`，否则 `""` | 非字符串一律空串 |
| `_cache_ttl` | `_cache_ttl() -> float`，[weather.py:43](../../../../agent/utils/weather.py) | `float(QWEATHER_CACHE_TTL)`；`TypeError`/`ValueError` 或 `<= 0` 时返回 `1200.0` | 非法配置不报错 |
| `_api_host` | `_api_host() -> str`，[weather.py:51](../../../../agent/utils/weather.py) | 去尾部 `/`；非空且不含 `://` 时补 `https://` | 空配置返回 `""` |
| `_private_key` | `_private_key() -> str`，[weather.py:58](../../../../agent/utils/weather.py) | 有 `QWEATHER_PRIVATE_KEY_PATH` 时按路径读取（相对路径基于 `PROJECT_ROOT`）；否则取 `QWEATHER_PRIVATE_KEY` 并把字面 `\n` 还原为换行 | 路径读取失败抛 `OSError`，由 `_configured`/R1 捕获 |

## 分支与异常链

| 条件 | 行为 | 终点 |
| --- | --- | --- |
| 天气缓存未过期且值非 `None` | 直接返回缓存 | R1 返回，无网络请求 |
| 缺任一必填配置 / 私钥不可读 | R1.1 返回 `False` | R1 返回 `None` |
| HTTP 错误、超时、响应缺 `condition.text` | R1 捕获异常并 `logger.warning` | R1 返回 `None`，不写缓存 |
| 响应有天气现象、无有效温度 | 返回现象文本 | R1 写入缓存 |
| 请求成功 | 更新 `_weather_cache`，TTL 为 `QWEATHER_CACHE_TTL` 或 1200 秒 | 下一轮直接命中缓存 |
| JWT 缓存未过期 | R1.3 不重新签名 | 复用 token |
| JWT 到期前 60 秒内 | R1.3 重新签名并更新缓存 | 新 token |

## 输入输出示例

与 `tests/test_weather.py:31-47` 的断言一致：

```text
环境: QWEATHER_API_HOST=https://example.qweatherapi.com、经纬度=39.92/116.41、
      Key/Project/Developer 均已配置，_token 返回 "token"
响应: {"condition": {"text": "少云"}, "temperature": {"value": 31.71}}
请求: GET https://example.qweatherapi.com/weather/v1/current/39.92/116.41
      headers: {"Authorization": "Bearer token"}
输出: "少云 32°C"
再次调用: 命中缓存，requests.get 只被调用一次
```

失败示例：

```text
环境: QWEATHER_API_HOST=""
输出: None（_request_weather 未被调用）

环境: 配置齐全，但 _request_weather 抛 RuntimeError("boom")
输出: None（不向上抛出）
```

JWT 形状（`tests/test_weather.py:54-76`）：

```text
header: {"kid": "kid"}            algorithm=EdDSA
claims: {"iss": developer_id, "sub": project_id, "iat": <now-30>, "exp": iat+900}
```

## 关联文档与验证依据

- 上级：[`../README.md`](../README.md)（agent/utils 总览）
- 调用方文档：[`../../node/world_state/README.md`](../../node/world_state/README.md)
- 相关模块：[`../state/README.md`](../state/README.md)（`prepare_world_state` 只从状态读取天气）
- 配置依据：[`config/config.py:19-28`](../../../../config/config.py)
- 已有测试覆盖（本次未执行）：`tests/test_weather.py`（未配置跳过请求、天气文本与缓存、请求失败降级、EdDSA 签名与 `kid`）
- 验证情况：本页为静态阅读源码所得；接口路径与字段名同时与源码及模块 docstring 引用的和风天气官方文档链接一致，未在本次文档编写中实际执行测试。
