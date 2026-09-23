"""Read-only checkpoint discovery and restartable import into managed conversations."""
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import re
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from psycopg.types.json import Jsonb
from server.classes.api import ServiceError
from server.repositories.runs import RunRepository
from server.services.agent import freeze_state, public_state
from server.services.speech import audio_path
from utils.daily_logger import get_logger, log_event, log_failure

logger = get_logger("server.legacy")


def source_key(value):
    if not value or len(value) > 255 or value.startswith('ui:') or any(c in value for c in ('/', '\\', '\x00')):
        raise ServiceError('invalid_cli_thread', 'CLI 会话标识无效。')
    return value


def checkpoint_messages(values, stamp, ai_prefixes=('reply_',)):
    """Only known committed AI messages qualify; never infer a draft is a reply."""
    from langchain_core.messages import HumanMessage, AIMessage
    items, omitted = [], False
    for message in values.get('messages', []):
        if not isinstance(message, (HumanMessage, AIMessage)):
            continue
        if isinstance(message, AIMessage) and (message.tool_calls or not str(message.id or '').startswith(ai_prefixes)):
            omitted = omitted or not bool(message.tool_calls)
            continue
        if not message.id or not isinstance(message.content, str):
            omitted = True
            continue
        text, time, estimated = message.content, stamp, True
        prefix = re.match(r'\A<timestamp>([^<]*)</timestamp>\r?\n?', text)
        if prefix:
            text = text[prefix.end():]
            try:
                parsed = datetime.fromisoformat(prefix.group(1))
                if parsed.tzinfo is not None:
                    time, estimated = parsed, False
            except ValueError:
                pass
        items.append({'source_message_id': message.id, 'role': 'user' if isinstance(message, HumanMessage) else 'assistant',
                      'text': text, 'created_at': time, 'timestamp_estimated': estimated})
    return items, omitted


class LegacyService:
    def __init__(self, database, catalog, audio_root):
        self.database, self.catalog, self.audio_root = database, catalog, audio_root
        self.task = None
        self._queue_failed = False
        self._audio_failures = set()

    @property
    def pool(self):
        if self.database.pool is None:
            raise ServiceError('database_unavailable', '数据库尚未就绪。', 503)
        return self.database.pool

    async def start(self):
        if self.database.pool is not None:
            self.task = asyncio.create_task(self._loop())

    async def close(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def _latest(self, conn, source):
        exists = await (await conn.execute("SELECT to_regclass('checkpoints') AS name")).fetchone()
        if not exists['name']:
            return None
        return await (await conn.execute("SELECT checkpoint_id, checkpoint, metadata FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = '' ORDER BY checkpoint_id DESC LIMIT 1", (source,))).fetchone()

    async def discover(self, *, cursor=None, limit=30):
        async with self.pool.connection() as conn:
            exists = await (await conn.execute("SELECT to_regclass('checkpoints') AS name")).fetchone()
            if not exists['name']:
                return {'items': [], 'next_cursor': None}
            rows = await (await conn.execute("""
                SELECT DISTINCT ON (c.thread_id) c.thread_id AS source_id, c.checkpoint_id, c.metadata,
                    a.status, a.character_id, a.thread_id, a.error_code
                FROM checkpoints c LEFT JOIN mybot_ui.cli_threads a ON a.source_id = c.thread_id
                WHERE c.checkpoint_ns = '' AND c.thread_id NOT LIKE 'ui:%%'
                    AND (a.status IS NULL OR a.status IN ('queued', 'running', 'failed'))
                    AND (%s::text IS NULL OR c.thread_id > %s)
                ORDER BY c.thread_id, c.checkpoint_id DESC LIMIT %s
            """, (cursor, cursor, limit + 1))).fetchall()
            items = []
            for row in rows[:limit]:
                character = row['character_id']
                if not character:
                    hints = set()
                    metadata = row['metadata'] or {}
                    if metadata.get('character_name'):
                        hints.add(metadata['character_name'])
                    for table in ('jobs', 'results'):
                        if (await (await conn.execute('SELECT to_regclass(%s) AS name', (f'memory_service.{table}',))).fetchone())['name']:
                            from psycopg import sql
                            found = await (await conn.execute(sql.SQL('SELECT DISTINCT character_name FROM memory_service.{} WHERE thread_id = %s').format(sql.Identifier(table)), (row['source_id'],))).fetchall()
                            hints.update(r['character_name'] for r in found)
                    if len(hints) == 1:
                        candidate = next(iter(hints))
                        try:
                            self.catalog.get(candidate)
                            character = candidate
                        except ServiceError:
                            pass
                items.append({k: row[k] for k in ('source_id', 'thread_id', 'error_code')} | {
                    'character_id': character, 'status': row['status'] or 'unlinked',
                    'title': f"CLI · {row['source_id']}"})
            return {'items': items, 'next_cursor': rows[limit-1]['source_id'] if len(rows) > limit else None}

    async def request_import(self, source, character, title):
        source_key(source)
        self.catalog.get(character)
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f'cli-source:{source}',))
                existing = await (await conn.execute('SELECT * FROM mybot_ui.cli_threads WHERE source_id = %s FOR UPDATE', (source,))).fetchone()
                if existing:
                    if existing['status'] == 'purged':
                        raise ServiceError('thread_purged', '该 CLI 会话已永久删除，请新建会话。', 410)
                    if existing['character_id'] != character:
                        raise ServiceError('character_conflict', '该 CLI 会话已关联其他角色。', 409)
                    if existing['status'] != 'failed':
                        return self.job_dto(existing)
                latest = await self._latest(conn, source)
                if not latest:
                    raise ServiceError('legacy_not_found', '未找到该 CLI 检查点。', 404)
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f'memory-stream:{character}:{source}',))
                row = await (await conn.execute("""INSERT INTO mybot_ui.cli_threads(source_id, character_id, title, status, checkpoint_id)
                    VALUES (%s, %s, %s, 'queued', %s) ON CONFLICT (source_id) DO UPDATE SET status = 'queued',
                    error_code = NULL, checkpoint_id = EXCLUDED.checkpoint_id RETURNING *""",
                    (source, character, title, latest['checkpoint_id']))).fetchone()
                if (await (await conn.execute("SELECT to_regclass('memory_service.jobs') AS name")).fetchone())['name']:
                    await conn.execute('DELETE FROM memory_service.jobs WHERE character_name = %s AND thread_id = %s', (character, source))
                return self.job_dto(row)

    @staticmethod
    def job_dto(row):
        return {k: row[k] for k in ('source_id', 'thread_id', 'character_id', 'status', 'error_code')}

    async def status(self, source):
        async with self.pool.connection() as conn:
            row = await (await conn.execute('SELECT * FROM mybot_ui.cli_threads WHERE source_id = %s', (source,))).fetchone()
        if row is None:
            raise ServiceError('legacy_not_found', '该 CLI 会话尚未导入。', 404)
        return self.job_dto(row)

    async def resolve(self, source, character, title, retrieval=False, storage=False):
        source_key(source)
        self.catalog.get(character)
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f'cli-source:{source}',))
                try:
                    identifier = UUID(source)
                except ValueError:
                    identifier = None
                if identifier:
                    thread = await (await conn.execute('SELECT * FROM mybot_ui.threads WHERE id = %s', (identifier,))).fetchone()
                    if thread:
                        if thread['deleted_at']:
                            raise ServiceError('thread_deleted', '会话已在回收站，请先恢复。', 409)
                        return {'source_id': source, 'thread_id': thread['id'], 'character_id': thread['character_id'], 'status': 'completed', 'error_code': None}
                alias = await (await conn.execute('SELECT * FROM mybot_ui.cli_threads WHERE source_id = %s', (source,))).fetchone()
                if alias:
                    if alias['status'] == 'purged':
                        raise ServiceError('thread_purged', '会话已永久删除，请使用新的会话标识。', 410)
                    if alias['character_id'] != character:
                        raise ServiceError('character_conflict', '该 CLI 标识属于其他角色。', 409)
                    return self.job_dto(alias)
                latest = await self._latest(conn, source)
                if not latest:
                    thread = await RunRepository(self.pool, connection=conn).create_thread(character, title,
                        memory_retrieval_enabled=retrieval, memory_storage_enabled=storage, source='cli')
                    alias = await (await conn.execute("INSERT INTO mybot_ui.cli_threads(source_id, thread_id, character_id, title, status) VALUES (%s, %s, %s, %s, 'completed') RETURNING *",
                                                     (source, thread['id'], character, title))).fetchone()
                    return self.job_dto(alias)
        return await self.request_import(source, character, title)

    async def preview(self, source):
        source_key(source)
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        async with self.pool.connection() as conn:
            latest = await self._latest(conn, source)
            if not latest:
                raise ServiceError('legacy_not_found', '未找到 CLI 历史。', 404)
            alias = await (await conn.execute('SELECT status FROM mybot_ui.cli_threads WHERE source_id = %s', (source,))).fetchone()
            if alias and alias['status'] == 'purged':
                raise ServiceError('thread_purged', '会话已永久删除。', 410)
            saver = AsyncPostgresSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=None))
            value = await saver.aget_tuple({'configurable': {'thread_id': source, 'checkpoint_ns': ''}})
            items, omitted = checkpoint_messages(value.checkpoint['channel_values'], datetime.fromisoformat(value.checkpoint['ts']))
            return {'items': items[-100:], 'notice': '仅预览当前保留的消息；导入时会回溯可用检查点。' + (' 部分旧回复无法确认，未纳入预览。' if omitted else '')}

    async def _import(self, conn, job):
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        from langchain_core.messages import HumanMessage, AIMessage
        saver = AsyncPostgresSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=None))
        config = {'configurable': {'thread_id': job['source_id'], 'checkpoint_ns': '', 'checkpoint_id': job['checkpoint_id']}}
        # A read-only recovery graph determines whether the old invocation was interrupted.
        from agent.builder import build_rp_agent
        graph = await build_rp_agent(job['character_id'], checkpointer=saver, recovery_only=True)
        snapshot = await graph.aget_state(config)
        if snapshot.next:
            raise ServiceError('legacy_interrupted', '旧 CLI 会话存在未完成节点，需先核对后再导入。', 409)
        latest_values = snapshot.values
        chain, seen_checkpoints, omitted, missing = [], set(), False, False
        while config:
            identifier = config['configurable'].get('checkpoint_id')
            if identifier in seen_checkpoints:
                raise ValueError('Cyclic checkpoint ancestry')
            seen_checkpoints.add(identifier)
            value = await saver.aget_tuple(config)
            if value is None:
                missing = True
                break
            items, skipped = checkpoint_messages(value.checkpoint['channel_values'], datetime.fromisoformat(value.checkpoint['ts']))
            omitted = omitted or skipped
            chain.append(items)
            config = value.parent_config
        ordered = {}
        for items in reversed(chain):
            for item in items:
                # Keep first observation time; later snapshots may update the same stable message.
                previous = ordered.get(item['source_message_id'])
                if previous and item['timestamp_estimated']:
                    item = {**item, 'created_at': previous['created_at']}
                ordered[item['source_message_id']] = item
        if not ordered:
            raise ServiceError('legacy_empty', '没有可确认的用户输入或正式回复可导入。', 409)
        notice = '从旧 CLI 检查点恢复；检查点不是完整消息档案，可能存在无法恢复的历史。'
        if omitted:
            notice += ' 未纳入无法确认的旧回复。'
        if missing:
            notice += ' 部分父检查点已缺失。'
        thread_id = uuid4()
        history, context = [], []
        retained = {m.id for m in latest_values.get('messages', [])}
        for sequence, item in enumerate(ordered.values(), 1):
            identifier = uuid5(NAMESPACE_URL, f"mybot-cli:{job['source_id']}:{item['source_message_id']}")
            graph_id = f'legacy_{identifier}'
            history.append((identifier, thread_id, sequence, item['role'], item['text'], item['created_at'], graph_id, item['timestamp_estimated']))
            if item['source_message_id'] in retained:
                cls = HumanMessage if item['role'] == 'user' else AIMessage
                context.append(cls(id=graph_id, content=f"<timestamp>{item['created_at'].isoformat()}</timestamp>\n" + item['text']))
        state = public_state(latest_values)
        seed = freeze_state({**state, 'messages': context, 'iteration': 0, 'memory_policy_version': 1,
                             'memory_retrieval_enabled': False, 'memory_storage_enabled': False})
        async with conn.transaction():
            latest = await self._latest(conn, job['source_id'])
            if latest is None or latest['checkpoint_id'] != job['checkpoint_id']:
                raise ServiceError('legacy_changed', '导入期间 CLI 历史发生变化，请停止旧 CLI 后重试。', 409)
            await conn.execute("INSERT INTO mybot_ui.threads(id, character_id, graph_thread_id, title, source, state, import_state, history_notice) VALUES (%s, %s, %s, %s, 'legacy', %s, %s, %s)",
                               (thread_id, job['character_id'], f'ui:{thread_id}', job['title'], Jsonb(state), Jsonb(seed), notice))
            async with conn.cursor() as cursor:
                await cursor.executemany("INSERT INTO mybot_ui.messages(id, thread_id, sequence, role, text, created_at, graph_message_id, timestamp_estimated, source) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'legacy')", history)
            await conn.execute("UPDATE mybot_ui.cli_threads SET thread_id = %s, status = 'completed', error_code = NULL WHERE source_id = %s", (thread_id, job['source_id']))
        log_event(logger, "旧历史导入完成", source_id=job['source_id'], thread_id=thread_id,
                  character=job['character_id'], message_count=len(history))

    async def _cleanup_audio(self, conn):
        rows = await (await conn.execute('SELECT resource_id FROM mybot_ui.audio_cleanup LIMIT 50')).fetchall()
        for row in rows:
            try:
                path = audio_path(self.audio_root, row['resource_id'])
                await asyncio.to_thread(path.unlink, missing_ok=True)
                await conn.execute('DELETE FROM mybot_ui.audio_cleanup WHERE resource_id = %s', (row['resource_id'],))
                self._audio_failures.discard(row['resource_id'])
                log_event(logger, "音频资源清理完成", resource_id=row['resource_id'])
            except OSError as error:
                if row['resource_id'] not in self._audio_failures:
                    log_failure(logger, "音频清理失败，保留待办", error, resource_id=row['resource_id'])
                self._audio_failures.add(row['resource_id'])

    async def _loop(self):
        while True:
            try:
                async with self.pool.connection() as conn:
                    locked = (await (await conn.execute("SELECT pg_try_advisory_lock(hashtextextended('mybot-legacy-import', 0)) AS ok")).fetchone())['ok']
                    if locked:
                        try:
                            await self._cleanup_audio(conn)
                            job = await (await conn.execute("SELECT * FROM mybot_ui.cli_threads WHERE status IN ('queued', 'running') ORDER BY created_at LIMIT 1")).fetchone()
                            if job:
                                await conn.execute("UPDATE mybot_ui.cli_threads SET status = 'running' WHERE source_id = %s", (job['source_id'],))
                                log_event(logger, "旧历史导入开始", source_id=job['source_id'], character=job['character_id'])
                                try:
                                    await self._import(conn, job)
                                except Exception as error:
                                    await conn.execute("UPDATE mybot_ui.cli_threads SET status = 'failed', error_code = %s WHERE source_id = %s", (error.code if isinstance(error, ServiceError) else 'legacy_unreadable', job['source_id']))
                                    log_failure(logger, "旧历史导入失败", error, source_id=job['source_id'])
                        finally:
                            if not conn.closed:
                                await conn.execute("SELECT pg_advisory_unlock(hashtextextended('mybot-legacy-import', 0))")
                if self._queue_failed:
                    log_event(logger, "历史导入队列已恢复")
                self._queue_failed = False
            except Exception as error:
                if not self._queue_failed:
                    log_failure(logger, "历史导入队列不可用", error)
                self._queue_failed = True
            await asyncio.sleep(0.5)
