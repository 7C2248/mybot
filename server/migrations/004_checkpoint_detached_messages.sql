-- A checkpoint may confirm the user input before its run's reply is durable.
-- Keep that input when deleting the run, including its original memory policy.
ALTER TABLE mybot_ui.messages ADD COLUMN detached_memory_storage_enabled BOOLEAN;
ALTER TABLE mybot_ui.messages ADD COLUMN detached_memory_policy_version BIGINT;
ALTER TABLE mybot_ui.messages DROP CONSTRAINT message_origin;
ALTER TABLE mybot_ui.messages ADD CONSTRAINT message_origin CHECK (
    run_id IS NOT NULL OR source = 'legacy' OR (
        role = 'user' AND graph_message_id IS NOT NULL
        AND detached_memory_storage_enabled IS NOT NULL
        AND detached_memory_policy_version IS NOT NULL
        AND detached_memory_policy_version > 0
    )
);
