-- Existing conversations retain their previous policy; new conversations opt in.
ALTER TABLE mybot_ui.threads ADD COLUMN memory_retrieval_enabled BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE mybot_ui.threads ADD COLUMN memory_storage_enabled BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE mybot_ui.threads ALTER COLUMN memory_retrieval_enabled SET DEFAULT false;
ALTER TABLE mybot_ui.threads ALTER COLUMN memory_storage_enabled SET DEFAULT false;
ALTER TABLE mybot_ui.threads ADD COLUMN version BIGINT NOT NULL DEFAULT 1;
ALTER TABLE mybot_ui.threads ADD COLUMN memory_policy_version BIGINT NOT NULL DEFAULT 1;
ALTER TABLE mybot_ui.threads ADD COLUMN deleted_at TIMESTAMPTZ;
ALTER TABLE mybot_ui.threads ADD COLUMN source TEXT NOT NULL DEFAULT 'desktop' CHECK (source IN ('desktop', 'cli', 'legacy'));
ALTER TABLE mybot_ui.threads ADD COLUMN import_state JSONB;
ALTER TABLE mybot_ui.threads ADD COLUMN history_notice TEXT;
ALTER TABLE mybot_ui.runs ADD COLUMN memory_retrieval_enabled BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE mybot_ui.runs ADD COLUMN memory_storage_enabled BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE mybot_ui.runs ADD COLUMN memory_policy_version BIGINT NOT NULL DEFAULT 1;
ALTER TABLE mybot_ui.messages ALTER COLUMN run_id DROP NOT NULL;
ALTER TABLE mybot_ui.messages ADD COLUMN source TEXT NOT NULL DEFAULT 'run' CHECK (source IN ('run', 'legacy'));
ALTER TABLE mybot_ui.messages ADD COLUMN timestamp_estimated BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE mybot_ui.messages ADD CONSTRAINT message_origin CHECK (run_id IS NOT NULL OR source = 'legacy');

-- Also acts as a tombstone: a purged CLI thread must never be rediscovered.
CREATE TABLE mybot_ui.cli_threads (
    source_id TEXT PRIMARY KEY,
    thread_id UUID REFERENCES mybot_ui.threads(id) ON DELETE SET NULL,
    character_id TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed', 'purged')),
    checkpoint_id TEXT,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Files cannot participate in a PostgreSQL transaction. Persist cleanup intent.
CREATE TABLE mybot_ui.audio_cleanup (
    resource_id UUID PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX threads_active_created ON mybot_ui.threads(created_at DESC, id DESC) WHERE deleted_at IS NULL;
