"""Managed conversation permissions, checked again at the memory commit boundary."""


async def memory_permitted(conn, payload, *, lock=False):
    thread_id = payload.get('thread_id', '')
    if not thread_id.startswith('ui:'):
        if lock:
            await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                               (f"memory-stream:{payload.get('character_name')}:{thread_id}",))
        exists = await (await conn.execute("SELECT to_regclass('mybot_ui.cli_threads') AS name")).fetchone()
        if not exists['name']:
            return True
        # Registering an import retires its old standalone memory stream. New CLI
        # messages use the managed ui: thread and the conversation's policy.
        alias = await (await conn.execute('SELECT 1 FROM mybot_ui.cli_threads WHERE source_id = %s', (thread_id,))).fetchone()
        return alias is None
    exists = await (await conn.execute("SELECT to_regclass('mybot_ui.threads') AS name")).fetchone()
    if not exists['name']:
        return False
    row = await (await conn.execute(
        'SELECT character_id, deleted_at, memory_storage_enabled, memory_policy_version '
        'FROM mybot_ui.threads WHERE graph_thread_id = %s' + (' FOR UPDATE' if lock else ''),
        (thread_id,))).fetchone()
    return bool(row and row['deleted_at'] is None and row['memory_storage_enabled']
                and row['character_id'] == payload.get('character_name')
                and row['memory_policy_version'] == payload.get('memory_policy_version', 1))
