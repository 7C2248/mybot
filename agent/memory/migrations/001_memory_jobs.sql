-- 队列只保留未成功处理的任务；结果表承担裁剪交接与永久幂等回执。
CREATE SCHEMA IF NOT EXISTS memory_service;

CREATE TABLE IF NOT EXISTS memory_service.jobs (
    id BIGSERIAL PRIMARY KEY,
    job_key TEXT NOT NULL UNIQUE,
    character_name TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (character_name, thread_id)
);
CREATE INDEX IF NOT EXISTS memory_jobs_ready ON memory_service.jobs (next_attempt_at, id);

CREATE TABLE IF NOT EXISTS memory_service.results (
    job_id BIGINT PRIMARY KEY,
    job_key TEXT NOT NULL UNIQUE,
    character_name TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    through_message_id TEXT NOT NULL,
    through_fingerprint TEXT NOT NULL,
    trim JSONB NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    acknowledged_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS memory_results_thread
    ON memory_service.results (character_name, thread_id, job_id);
