from uuid import uuid4

from server.classes.api import ServiceError
from server.repositories.runs import RunRepository


class SpeechRepository(RunRepository):
    async def enqueue(self, message_id, request_id):
        async with self.connection() as conn:
            async with conn.transaction():
                message = await (await conn.execute("SELECT * FROM mybot_ui.messages WHERE id = %s", (message_id,))).fetchone()
                if not message or message["role"] != "assistant":
                    raise ServiceError("message_not_found", "只能为已提交的角色回复生成语音。", 404)
                await self._thread(conn, message['thread_id'], lock=True)
                await conn.execute('SELECT id FROM mybot_ui.messages WHERE id = %s FOR UPDATE', (message_id,))
                if len(message["text"]) > 10000:
                    raise ServiceError("speech_text_too_long", "回复超过本次语音合成长度上限。", 413)
                existing = await (await conn.execute("SELECT * FROM mybot_ui.speech_jobs WHERE message_id = %s AND client_request_id = %s",
                                                    (message_id, request_id))).fetchone()
                if existing:
                    return self.dto(existing)
                active = await (await conn.execute("SELECT * FROM mybot_ui.speech_jobs WHERE message_id = %s AND status IN ('queued', 'running')", (message_id,))).fetchone()
                if active:
                    return self.dto(active)
                row = await (await conn.execute("INSERT INTO mybot_ui.speech_jobs(id, message_id, client_request_id) VALUES (%s, %s, %s) RETURNING *",
                                               (uuid4(), message_id, request_id))).fetchone()
                return self.dto(row)

    @staticmethod
    def dto(row):
        return dict(row, resource_url=f"/api/audio/resources/{row['resource_id']}" if row.get("resource_id") else None)

    async def get(self, job_id):
        async with self.connection() as conn:
            row = await (await conn.execute("SELECT s.* FROM mybot_ui.speech_jobs s JOIN mybot_ui.messages m ON m.id = s.message_id JOIN mybot_ui.threads t ON t.id = m.thread_id WHERE s.id = %s AND t.deleted_at IS NULL", (job_id,))).fetchone()
        if row is None:
            raise ServiceError("speech_not_found", "语音任务不存在。", 404)
        return self.dto(row)

    async def resource(self, resource_id):
        async with self.connection() as conn:
            row = await (await conn.execute("SELECT s.* FROM mybot_ui.speech_jobs s JOIN mybot_ui.messages m ON m.id = s.message_id JOIN mybot_ui.threads t ON t.id = m.thread_id WHERE s.resource_id = %s AND s.status = 'completed' AND t.deleted_at IS NULL", (resource_id,))).fetchone()
        if row is None:
            raise ServiceError("resource_not_found", "音频资源不存在。", 404)
        return row

    async def claim(self):
        async with self.connection() as conn:
            async with conn.transaction():
                row = await (await conn.execute("SELECT * FROM mybot_ui.speech_jobs WHERE status = 'queued' ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED")).fetchone()
                if row is None:
                    return None
                await conn.execute("UPDATE mybot_ui.speech_jobs SET status = 'running' WHERE id = %s", (row["id"],))
                return await (await conn.execute("""
                    SELECT s.*, m.text, t.character_id FROM mybot_ui.speech_jobs s
                    JOIN mybot_ui.messages m ON m.id = s.message_id JOIN mybot_ui.threads t ON t.id = m.thread_id
                    WHERE s.id = %s
                """, (row["id"],))).fetchone()

    async def finish(self, job_id, *, error=None):
        async with self.connection() as conn:
            await conn.execute("UPDATE mybot_ui.speech_jobs SET status = %s, error_code = %s, resource_id = %s, finished_at = now() WHERE id = %s AND status = 'running'",
                               ("failed" if error else "completed", error, None if error else job_id, job_id))

    async def recover(self):
        async with self.connection() as conn:
            await conn.execute("UPDATE mybot_ui.speech_jobs SET status = 'interrupted', error_code = 'service_interrupted', finished_at = now() WHERE status = 'running'")
