-- UI persistence is independent of LangGraph checkpoints and character memory tables.
-- These tables prepare phase B; phase A only serves catalog and memory reads.
CREATE TABLE mybot_ui.threads (
    id UUID PRIMARY KEY,
    character_id TEXT NOT NULL,
    graph_thread_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    state JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK (graph_thread_id = 'ui:' || id::text),
    CHECK (jsonb_typeof(state) = 'object')
);
CREATE INDEX threads_updated ON mybot_ui.threads (updated_at DESC, id DESC);

CREATE TABLE mybot_ui.runs (
    id UUID PRIMARY KEY,
    thread_id UUID NOT NULL REFERENCES mybot_ui.threads(id),
    user_message_id UUID NOT NULL,
    client_request_id TEXT NOT NULL CHECK (length(client_request_id) BETWEEN 1 AND 200),
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'completed', 'completed_with_warnings', 'failed', 'interrupted')),
    phase TEXT NOT NULL DEFAULT 'queued',
    retry_of UUID,
    error_code TEXT,
    warnings JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(warnings) = 'array'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    UNIQUE (id, thread_id),
    UNIQUE (thread_id, client_request_id),
    FOREIGN KEY (retry_of, thread_id) REFERENCES mybot_ui.runs (id, thread_id)
);
CREATE UNIQUE INDEX one_active_run_per_thread ON mybot_ui.runs (thread_id)
    WHERE status IN ('queued', 'running');
CREATE INDEX queued_runs ON mybot_ui.runs (created_at, id) WHERE status = 'queued';

CREATE TABLE mybot_ui.messages (
    id UUID PRIMARY KEY,
    thread_id UUID NOT NULL REFERENCES mybot_ui.threads(id),
    run_id UUID NOT NULL,
    sequence BIGINT NOT NULL CHECK (sequence > 0),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    graph_message_id TEXT,
    UNIQUE (id, thread_id),
    UNIQUE (thread_id, sequence),
    UNIQUE (thread_id, graph_message_id),
    UNIQUE (run_id, role),
    FOREIGN KEY (run_id, thread_id) REFERENCES mybot_ui.runs (id, thread_id)
        DEFERRABLE INITIALLY DEFERRED
);
ALTER TABLE mybot_ui.runs ADD CONSTRAINT runs_user_message_fk
    FOREIGN KEY (user_message_id, thread_id) REFERENCES mybot_ui.messages (id, thread_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE mybot_ui.run_events (
    run_id UUID NOT NULL REFERENCES mybot_ui.runs(id),
    sequence BIGINT NOT NULL CHECK (sequence > 0),
    type TEXT NOT NULL CHECK (type IN (
        'run.started', 'phase', 'message.committed', 'state.updated',
        'memory.retrieved', 'run.completed', 'run.failed'
    )),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, sequence)
);
