"""Conversation settings, trash and transactional deletion fences."""
from psycopg.types.json import Jsonb

from server.classes.api import ServiceError
from server.classes.runs import Message
from server.repositories.runs import RunRepository


class ThreadRepository(RunRepository):
    async def memory_status(self, thread_id, active_job=None):
        async with self.connection() as conn:
            thread = await self._thread(conn, thread_id)
            if not thread['memory_storage_enabled']:
                return {'status': 'disabled'}
            exists = await (await conn.execute("SELECT to_regclass('memory_service.jobs') AS jobs, to_regclass('memory_service.results') AS results")).fetchone()
            if exists['jobs']:
                job = await (await conn.execute('SELECT id, status FROM memory_service.jobs WHERE character_name = %s AND thread_id = %s',
                    (thread['character_id'], thread['graph_thread_id']))).fetchone()
                if job:
                    running = active_job and active_job['id'] == job['id'] and active_job['thread_id'] == thread['graph_thread_id'] and active_job['character_name'] == thread['character_id']
                    return {'status': 'running' if running else job['status'], 'job_id': job['id']}
            if exists['results']:
                result = await (await conn.execute('SELECT job_id FROM memory_service.results WHERE character_name = %s AND thread_id = %s ORDER BY job_id DESC LIMIT 1',
                    (thread['character_id'], thread['graph_thread_id']))).fetchone()
                if result:
                    return {'status': 'completed', 'job_id': result['job_id']}
            return {'status': 'idle'}

    async def _idle(self, conn, thread_id):
        active = await (await conn.execute("""
            SELECT EXISTS(SELECT 1 FROM mybot_ui.runs WHERE thread_id = %s AND status IN ('queued', 'running'))
                OR EXISTS(SELECT 1 FROM mybot_ui.speech_jobs s JOIN mybot_ui.messages m ON m.id = s.message_id
                          WHERE m.thread_id = %s AND s.status IN ('queued', 'running')) AS busy
        """, (thread_id, thread_id))).fetchone()
        if active['busy']:
            raise ServiceError('thread_busy', '请等待本会话的对话或语音任务结束。', 409)

    def _version(self, thread, version):
        if thread['version'] != version:
            raise ServiceError('thread_conflict', '会话设置已变化，请重新读取后再操作。', 409)

    async def _cancel_memory(self, conn, thread):
        exists = await (await conn.execute("SELECT to_regclass('memory_service.jobs') AS name")).fetchone()
        if exists['name']:
            await conn.execute('DELETE FROM memory_service.jobs WHERE character_name = %s AND thread_id = %s',
                               (thread['character_id'], thread['graph_thread_id']))

    async def update(self, thread_id, version, changes):
        async with self.connection() as conn:
            async with conn.transaction():
                thread = await self._thread(conn, thread_id, lock=True)
                self._version(thread, version)
                await self._idle(conn, thread_id)
                title = changes.get('title') or thread['title']
                retrieval = changes.get('memory_retrieval_enabled', thread['memory_retrieval_enabled'])
                storage = changes.get('memory_storage_enabled', thread['memory_storage_enabled'])
                changed = retrieval != thread['memory_retrieval_enabled'] or storage != thread['memory_storage_enabled']
                if changed:
                    await self._cancel_memory(conn, thread)
                return await (await conn.execute("""
                    UPDATE mybot_ui.threads SET title = %s, memory_retrieval_enabled = %s,
                        memory_storage_enabled = %s, memory_policy_version = memory_policy_version + %s,
                        version = version + 1, updated_at = now() WHERE id = %s RETURNING *
                """, (title, retrieval, storage, int(changed), thread_id))).fetchone()

    async def trash(self, thread_id, version, *, restore=False):
        async with self.connection() as conn:
            async with conn.transaction():
                thread = await self._thread(conn, thread_id, lock=True, include_deleted=True)
                self._version(thread, version)
                await self._idle(conn, thread_id)
                if not restore:
                    await self._cancel_memory(conn, thread)
                return await (await conn.execute("""
                    UPDATE mybot_ui.threads SET deleted_at = CASE WHEN %s THEN NULL ELSE now() END,
                        memory_policy_version = memory_policy_version + 1, version = version + 1,
                        updated_at = now() WHERE id = %s RETURNING *
                """, (restore, thread_id))).fetchone()

    async def sync_checkpoint(self, thread_id, version, *, dry_run, checkpoint_id, read):
        """Truncate the message tail so the newest UI message matches the checkpoint tail.

        `read` is called inside the thread row lock and returns the committed
        checkpoint items, the public state projection and memory markers. Phase 1
        never rewrites the checkpoint: unsafe memory progress refuses the sync.
        """
        async with self.connection() as conn:
            async with conn.transaction():
                thread = await self._thread(conn, thread_id, lock=True)
                self._version(thread, version)
                await self._idle(conn, thread_id)
                source = await read(conn, thread)
                if not dry_run and checkpoint_id != source['checkpoint_id']:
                    raise ServiceError('checkpoint_changed', '检查点在预览后已变化，请重新预览。', 409)
                rows = await (await conn.execute('SELECT * FROM mybot_ui.messages WHERE thread_id = %s ORDER BY sequence',
                                                (thread_id,))).fetchall()
                latest = source['messages'][-1] if source['messages'] else None
                match = None
                if latest is not None:
                    match = next((row for row in reversed(rows) if row['graph_message_id'] == latest['source_message_id']), None)
                    if match is None:
                        raise ServiceError('checkpoint_conflict', '检查点最新消息不在会话历史中，未做任何修改。', 409, details={
                            'latest_message_id': latest['source_message_id'],
                            'checkpoint_tail': [item['source_message_id'] for item in source['messages'][-5:]],
                            'ui_tail': [self._message_tail(row) for row in rows[-5:]],
                        })
                elif rows:
                    raise ServiceError('checkpoint_empty', '检查点没有可确认的正式消息，未做任何修改。', 409)
                extra = [row for row in rows if match is not None and row['sequence'] > match['sequence']]
                await self._memory_fence(conn, thread, source, rows, match, extra)
                extra_ids = [row['id'] for row in extra]
                run_ids = {row['run_id'] for row in extra if row['run_id']}
                if extra_ids:
                    found = await (await conn.execute('SELECT id FROM mybot_ui.runs WHERE thread_id = %s AND user_message_id = ANY(%s)',
                                                     (thread_id, extra_ids))).fetchall()
                    run_ids.update(row['id'] for row in found)
                preview = [Message.model_validate(row) for row in extra[:20]]
                result = {'dry_run': dry_run, 'checkpoint_id': source['checkpoint_id'],
                          'latest_message_id': latest['source_message_id'] if latest else None,
                          'latest_message_text': latest['text'] if latest else None,
                          'matched_message_id': str(match['id']) if match else None,
                          'delete_count': len(extra), 'delete_preview': preview,
                          'preview_truncated': len(extra) > len(preview), 'run_count': len(run_ids),
                          'version': thread['version'], 'state': source['state']}
                if dry_run or not extra_ids:
                    return result
                await self._delete_tail(conn, thread_id, extra_ids, run_ids)
                notice = f'已按检查点截断 {len(extra)} 条消息。'
                updated = await (await conn.execute("""
                    UPDATE mybot_ui.threads SET state = state || %s, version = version + 1, updated_at = now(),
                        history_notice = LEFT(COALESCE(history_notice || ' ', '') || %s, 1000)
                    WHERE id = %s RETURNING version
                """, (Jsonb(source['state']), notice, thread_id))).fetchone()
                result['version'] = updated['version']
        return result

    async def _memory_fence(self, conn, thread, source, rows, match, extra):
        """Phase 1 refuses any state whose durable memory progress may be rewound."""
        if not extra:
            return
        markers = source['markers']
        if markers.get('active') or markers.get('pending'):
            raise ServiceError('memory_busy', '检查点仍在处理记忆任务，暂不能截断历史。', 409)
        exists = await (await conn.execute("SELECT to_regclass('memory_service.jobs') AS name")).fetchone()
        if exists['name']:
            job = await (await conn.execute('SELECT id FROM memory_service.jobs WHERE character_name = %s AND thread_id = %s LIMIT 1',
                                            (thread['character_id'], thread['graph_thread_id']))).fetchone()
            if job:
                raise ServiceError('memory_busy', '该会话还有未完成的记忆任务，暂不能截断历史。', 409, details={'job_id': job['id']})
        surviving = {row['graph_message_id'] for row in rows if match is not None and row['sequence'] <= match['sequence']}
        for name in ('processed', 'submitted'):
            value = markers.get(name)
            if value and value not in surviving:
                raise ServiceError('memory_busy', '记忆处理进度指向将被删除的消息，暂不能截断历史。', 409, details={'marker': name})

    @staticmethod
    def _message_tail(row):
        return {'id': str(row['id']), 'graph_message_id': row['graph_message_id'], 'role': row['role'],
                'sequence': row['sequence'], 'text': row['text'][:200]}

    async def _delete_tail(self, conn, thread_id, extra_ids, run_ids):
        await conn.execute("""INSERT INTO mybot_ui.audio_cleanup(resource_id)
            SELECT COALESCE(s.resource_id, s.id) FROM mybot_ui.speech_jobs s
            WHERE s.message_id = ANY(%s) ON CONFLICT DO NOTHING""", (extra_ids,))
        await conn.execute('DELETE FROM mybot_ui.speech_jobs WHERE message_id = ANY(%s)', (extra_ids,))
        if run_ids:
            ids = list(run_ids)
            # The checkpoint can end at a user input whose reply/run is removed.
            # Preserve its identity and memory eligibility without keeping stale
            # run events or a recovery snapshot that could resurrect the reply.
            await conn.execute('UPDATE mybot_ui.threads SET recovery_run_id = NULL WHERE id = %s AND recovery_run_id = ANY(%s)',
                               (thread_id, ids))
            await conn.execute("""UPDATE mybot_ui.messages m SET run_id = NULL,
                detached_memory_storage_enabled = r.memory_storage_enabled,
                detached_memory_policy_version = r.memory_policy_version
                FROM mybot_ui.runs r WHERE m.run_id = r.id AND r.id = ANY(%s)
                    AND m.id <> ALL(%s) AND m.role = 'user'""", (ids, extra_ids))
            await conn.execute('DELETE FROM mybot_ui.run_events WHERE run_id = ANY(%s)', (ids,))
            # A remaining run can never reference a newer deleted run, but keep the FK safe.
            await conn.execute('UPDATE mybot_ui.runs SET retry_of = NULL WHERE retry_of = ANY(%s) AND id <> ALL(%s)', (ids, ids))
            await conn.execute('DELETE FROM mybot_ui.runs WHERE id = ANY(%s)', (ids,))
        await conn.execute('DELETE FROM mybot_ui.messages WHERE id = ANY(%s)', (extra_ids,))

    async def purge(self, thread_id, version):
        async with self.connection() as conn:
            async with conn.transaction():
                thread = await self._thread(conn, thread_id, lock=True, include_deleted=True)
                self._version(thread, version)
                if thread['deleted_at'] is None:
                    raise ServiceError('trash_required', '请先将会话移入回收站。', 409)
                await self._idle(conn, thread_id)
                await self._cancel_memory(conn, thread)
                await conn.execute('SET CONSTRAINTS ALL DEFERRED')
                await conn.execute("""INSERT INTO mybot_ui.audio_cleanup(resource_id)
                    SELECT COALESCE(s.resource_id, s.id) FROM mybot_ui.speech_jobs s JOIN mybot_ui.messages m ON m.id = s.message_id
                    WHERE m.thread_id = %s ON CONFLICT DO NOTHING""", (thread_id,))
                await conn.execute('UPDATE mybot_ui.threads SET recovery_run_id = NULL WHERE id = %s', (thread_id,))
                aliases = await (await conn.execute("UPDATE mybot_ui.cli_threads SET status = 'purged' WHERE thread_id = %s RETURNING source_id", (thread_id,))).fetchall()
                # Also prevent CLI resolution from recreating a deleted desktop UUID as a new source.
                await conn.execute("INSERT INTO mybot_ui.cli_threads(source_id, character_id, title, status) VALUES (%s, %s, %s, 'purged') ON CONFLICT DO NOTHING",
                                   (str(thread_id), thread['character_id'], thread['title']))
                graph_ids = [thread['graph_thread_id'], *(r['source_id'] for r in aliases)]
                # These are the checkpointer's three owned tables, scoped by exact thread IDs.
                for table in ('checkpoint_writes', 'checkpoint_blobs', 'checkpoints'):
                    exists = await (await conn.execute('SELECT to_regclass(%s) AS name', (table,))).fetchone()
                    if exists['name']:
                        from psycopg import sql
                        await conn.execute(sql.SQL('DELETE FROM {} WHERE thread_id = ANY(%s)').format(sql.Identifier(table)), (graph_ids,))
                exists = await (await conn.execute("SELECT to_regclass('memory_service.results') AS name")).fetchone()
                if exists['name']:
                    await conn.execute('DELETE FROM memory_service.results WHERE character_name = %s AND thread_id = ANY(%s)', (thread['character_id'], graph_ids))
                await conn.execute('DELETE FROM mybot_ui.speech_jobs WHERE message_id IN (SELECT id FROM mybot_ui.messages WHERE thread_id = %s)', (thread_id,))
                await conn.execute('DELETE FROM mybot_ui.run_events WHERE run_id IN (SELECT id FROM mybot_ui.runs WHERE thread_id = %s)', (thread_id,))
                await conn.execute('DELETE FROM mybot_ui.messages WHERE thread_id = %s', (thread_id,))
                await conn.execute('DELETE FROM mybot_ui.runs WHERE thread_id = %s', (thread_id,))
                await conn.execute('DELETE FROM mybot_ui.threads WHERE id = %s', (thread_id,))
