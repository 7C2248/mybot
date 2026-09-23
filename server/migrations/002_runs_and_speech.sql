-- Keep 001 immutable. Recovery data is private and never returned as a DTO.
ALTER TABLE mybot_ui.runs ADD COLUMN base_state JSONB;
ALTER TABLE mybot_ui.runs ADD COLUMN model_version TEXT;
ALTER TABLE mybot_ui.runs ADD COLUMN profile_version TEXT;
ALTER TABLE mybot_ui.threads ADD COLUMN recovery_run_id UUID REFERENCES mybot_ui.runs(id);

CREATE TABLE mybot_ui.speech_jobs (
    id UUID PRIMARY KEY,
    message_id UUID NOT NULL REFERENCES mybot_ui.messages(id),
    client_request_id TEXT NOT NULL CHECK (length(client_request_id) BETWEEN 1 AND 200),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'completed', 'failed', 'interrupted')),
    resource_id UUID UNIQUE,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    UNIQUE (message_id, client_request_id)
);
CREATE UNIQUE INDEX one_active_speech_per_message ON mybot_ui.speech_jobs(message_id)
    WHERE status IN ('queued', 'running');
CREATE INDEX queued_speech ON mybot_ui.speech_jobs(created_at, id) WHERE status = 'queued';
