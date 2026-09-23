# 角色目录与受控资源：CharacterCatalog

## 职责与入口

本页覆盖 `server/services/characters.py` 与 `server/routes/characters.py`：启动时扫描 `Character/` 建立角色注册表（档案、图片资源、版本哈希），提供档案读取/按版本写回、受控图片资源 ID，以及供 TTS 使用的语音档案。

| 文件 | 类型 | 职责 |
| --- | --- | --- |
| [server/services/characters.py](../../../server/services/characters.py) | 服务 | `CharacterCatalog` 与 `ImageResource` |
| [server/routes/characters.py](../../../server/routes/characters.py) | 路由 | 角色列表/详情、档案写回、图片资源 |
| [server/services/files.py](../../../server/services/files.py) | 工具 | `atomic_write`（见 [http 叶子](../http/README.md) R5） |

上游：`app.py` lifespan 创建并 `await asyncio.to_thread(catalog.load)`；下游：`routes/memories`（校验角色）、`RunRuntime`/`AgentAdapter`（运行前取档案）、`SpeechService.execute`（语音档案）、`LegacyService`（角色证据校验）。

## 调用链总览

```text
构建阶段
B1 CharacterCatalog.__init__ ── 固定 root、空注册表、RLock
B2 load / _load ── 扫描目录、过滤边界、生成角色与资源注册表（B2.1 档案，B2.2 图片）

运行阶段
R1 summaries / get ── 列表与详情（内存注册表）
R2 write_profile ── 重新扫描 → 版本校验 → 边界校验 → atomic_write → 重新扫描
R3 voice_profile ── tts.md 优先，缺失回退角色档案
R4 resource ── 资源 ID 查询 + 打开前再次边界校验
辅助：_within
```

## 构建链

### B1. `CharacterCatalog.__init__`

- 定位与签名：[server/services/characters.py](../../../server/services/characters.py) 的 `def __init__(self, root: Path)`。
- 输入：`root`（`settings.character_root`，即 `<项目根>/Character`）。`self.root = root.resolve()`。
- 内部状态：`_characters: dict[str, CharacterDetail]`、`_resources: dict[str, ImageResource]`、`_profiles: dict[tuple[str,str], Path]`、`_lock = threading.RLock()`。
- 输出：空目录表实例；调用 `load` 前所有查询都会 404。

### B2. `load` 与 `_load`

- 定位与签名：`def load(self)`（`with self._lock: self._load()`）与 `def _load(self)`（无锁，调用方持锁）。

`_load` 扫描规则：
1. `root` 不存在或不是目录 → 清空并返回。
2. 遍历 `sorted(root.iterdir())`：跳过非目录、符号链接、`not self._within(directory, self.root)`；解析后要求 `character_root.parent == self.root` 且 `character_root.name == directory.name`（**junction/别名不得把其他角色的资源登记为本角色**）。
3. 档案（B2.1）：`sorted(directory.glob("profile_*.md"))`，只接受文件且 `_within(profile, character_root)`；语言 = 文件名去掉 `profile_` 前缀，`cn` 归一为 `zh`；内容 `read_bytes().decode("utf-8")`；记录 `_profiles[(目录名, 语言)] = path`。无任何档案的目录跳过。
4. 图片（B2.2）：`images/` 子目录存在且 `_within(image_root, character_root)` 时，遍历 `sorted(image_root.iterdir())`；按扩展名映射媒体类型（`.png→image/png`、`.jpg/.jpeg→image/jpeg`、`.webp→image/webp`）；文件须存在且 `_within(path, image_root.resolve())`。
   - 资源 ID：`sha256(f"{directory.name}/{path.name}".encode("utf-8")).hexdigest()`——跨重启稳定，客户端从不提交文件路径。
   - `ImageResource(path, character_root, media_type)` 存入 `_resources`；`CharacterAsset(id, name=path.name, url=f"/api/resources/{id}")`。
5. 版本：`version_data = json.dumps({"profiles": profiles, "assets": [a.id for a in assets]}, sort_keys=True, ensure_ascii=False)`；`version = sha256(version_data)`。任一档案或资源集合变化都会改变版本。
6. 构造 `CharacterDetail(id=目录名, name=目录名, languages=sorted(profiles), profiles=profiles, assets=assets, version=version)`。
7. 原子替换三张注册表（先写局部变量，最后一次性赋值）。

输出：无返回值。异常：单个目录/文件问题按跳过处理，不中断扫描；无外部服务调用。

### B3. `ImageResource`

`@dataclass(frozen=True)`：`path: Path`、`character_root: Path`、`media_type: str`。仅承载资源读取信息，不含文件内容。

## 运行链

### R1. `summaries` 与 `get`

| 方法 | 签名 | 功能 | 输出 |
| --- | --- | --- | --- |
| `summaries` | `def summaries(self)` | 把每个 `CharacterDetail` 转为 `CharacterSummary`（字段相同，仅类型收窄） | `list[CharacterSummary]`，顺序为注册表插入顺序（目录名排序） |
| `get` | `def get(self, character_id)` | `character_id` 不在注册表 → 404 `character_not_found`；否则返回 `CharacterDetail` | 档案 + 语言列表 + 资源 + 版本 |

### R2. `write_profile`

- 定位与签名：`def write_profile(self, character_id, language, text, expected_version)`。
- 调用方：`PUT /api/characters/{character_id}/profiles/{language}`（请求体 `ProfileWrite`：`expected_version` 固定 64 位、`text` ≤200000 且非空白）。

| 输入 | 类型 | 来源 | 缺省与前置条件 | 用途 |
| --- | --- | --- | --- | --- |
| `character_id` | `str` | 路径 | 必须已注册 | 定位角色 |
| `language` | `str` | 路径 | 只允许已存在的语言档案 | 定位 `profile_*.md` |
| `text` | `str` | 请求体 | 非空白、≤200000 | 完整 Markdown 原文 |
| `expected_version` | `str` | 请求体 | 64 位 | 乐观锁 |

功能（全程持 `_lock`）：
1. `self._load()`：**先重新扫描外部文件变化**，再比较版本；因此服务外编辑过档案时版本必然不符。
2. `character = self.get(character_id)`；`character.version != expected_version` → 409 `version_conflict`（“角色档案已变化，请重新读取后保存”）。
3. `path = self._profiles.get((character_id, language))`；`root = self.root / character_id`；`path is None or path.is_symlink() or not self._within(path, root)` → 404 `profile_not_found`（“该语言档案不存在或不可写”）。**只允许写已有语言档案，不接受客户端文件路径。**
4. `atomic_write(path, text)`：同目录临时文件 + fsync + `os.replace`，UTF-8、`newline=""`，保留换行、空行与缩进。
5. `self._load()` 刷新注册表（版本随之变化）；返回 `self.get(character_id)`。

输出：更新后的 `CharacterDetail`（新版本哈希）。语义：正在执行的任务持有原档案快照，下一任务读取新版本；外部新增角色或资源需重启刷新。

### R3. `voice_profile`

- 定位与签名：`def voice_profile(self, character_id)`。
- 调用方：`SpeechService.execute`（[speech 叶子](../speech/README.md) R3）。
- 功能：`get(character_id)` → `root = self.root / character_id` → `root / "tts.md"` 是文件且 `_within(path, root)` 时读取其 UTF-8 文本；否则返回 `profiles.get("zh") or next(iter(profiles.values()))`。
- 输出：语音档案文本（TTS 语气参考），与 `agent/node/tts.py` 的 `_load_voice_profile` 兜底顺序一致（权威实现见 [node/tts 叶子](../../agent/node/tts/README.md)）。

### R4. `resource`

- 定位与签名：`def resource(self, resource_id) -> ImageResource`。
- 调用方：`GET /api/resources/{resource_id}`。

功能：
1. `resource = self._resources.get(resource_id)`；`None` 或 `not resource.path.is_file()` → 404 `resource_not_found`。
2. **打开前再次核对目录边界**：`image_root = resource.character_root / "images"`；要求 `_within(resource.character_root, self.root)`、`_within(image_root, resource.character_root)`、`_within(resource.path, image_root.resolve())` 全部成立，否则 404。用于防范启动后文件/目录被替换。
3. 返回 `ImageResource`。

路由随后 `FileResponse(item.path, media_type=item.media_type, headers={"X-Content-Type-Options":"nosniff","Cache-Control":"no-cache"})`。

### R5. `_within`（静态）

- 定位与签名：`@staticmethod def _within(path: Path, root: Path) -> bool`。
- 功能：`path.resolve(strict=True).is_relative_to(root)`；`OSError/RuntimeError`（不存在、解析循环等）返回 `False`。
- 用途：扫描、写回、资源读取与语音档案的目录包含判定；是目录边界的统一实现。

## 分支与异常链

| 条件 | 处理 | 终点 |
| --- | --- | --- |
| 角色不存在 | 404 `character_not_found` | R1/R2/R3/R4 |
| 版本不符（含外部编辑） | 409 `version_conflict` | R2 |
| 语言档案不存在或不可写 | 404 `profile_not_found` | R2 |
| 写回路径是符号链接 | 拒绝（404） | R2 步骤 3 |
| 图片资源 ID 未注册 | 404 `resource_not_found` | R4 |
| 资源文件启动后被替换/越界 | 再次边界校验 → 404 | R4 步骤 2 |
| 角色目录是符号链接/junction | 扫描时跳过 | B2 步骤 2 |
| 目录无 `profile_*.md` | 不注册该角色 | B2 步骤 3 |
| 外部新增角色/资源 | 需重启服务刷新；写回会重新扫描该次 | B2/R2 |
| `Character/` 不存在 | 注册表为空，角色接口 404 | B2 步骤 1 |

## 输入输出示例

`PUT /api/characters/小满/profiles/zh`（R2）：

```json
{"expected_version": "3c1d...64hex", "text": "# 小满\n\n## 性格\n温和。"}
```

成功返回 `CharacterDetail`：`version` 变为新哈希；失败（版本过期）返回：

```json
{"error": {"code": "version_conflict", "message": "角色档案已变化，请重新读取后保存。"}}
```

`GET /api/characters`（R1，节选）：

```json
[{"id": "小满", "name": "小满", "languages": ["zh"], "version": "3c1d...64hex",
  "assets": [{"id": "8a7f...64hex", "name": "portrait.png", "url": "/api/resources/8a7f...64hex"}]}]
```

## 关联文档与验证依据

- 上级：[服务总览](../README.md)；系统总览：[README/README.md](../../README.md)
- 同级：[speech](../speech/README.md)（`voice_profile`）· [legacy](../legacy/README.md)（角色证据 `catalog.get`）· [http](../http/README.md)（路由与 `atomic_write`）· [runtime](../runtime/README.md)（运行前档案读取）
- Agent 侧：[utils/character/README.md](../../agent/utils/character/README.md)（`load_character_profile`、`character_dir`）· [node/tts/README.md](../../agent/node/tts/README.md)
- 测试依据：[tests/server/test_api.py](../../../tests/server/test_api.py)（角色/资源与写回）、[tests/server/test_postgres.py](../../../tests/server/test_postgres.py)
- 迁移自原 `server/README.md`（原文件已移除）的规则：`profile_cn.md` 对应语言 `zh`，其他 `profile_*.md` 使用文件名后缀；写入体为 `{"expected_version":"读取到的版本","text":"完整 Markdown"}`，版本冲突返回 409；先检查外部文件变化及目录边界，再原子替换文件并刷新注册表；保留换行、空行和缩进；只允许写已有语言档案，不接受客户端文件路径；图片只开放注册过的资源 ID，扫描和读取均核对目录边界。
- 验证记录（迁移自原文档，未在本次编写中重跑）：2026-09-21 记录离线 API 与真实 PostgreSQL 集成通过，覆盖角色/资源与档案写回；本轮仅静态核对源码。
