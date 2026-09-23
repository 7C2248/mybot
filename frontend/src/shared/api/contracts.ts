import { z } from 'zod';

export const memoryStatusDto = z.object({ status: z.enum(['disabled', 'idle', 'pending', 'running', 'failed', 'completed']), job_id: z.number().nullish() });
export type MemoryStatus = z.infer<typeof memoryStatusDto>;

const nullableText = z.string().nullable();
export const messageDto = z.object({ id: z.string(), thread_id: z.string(), run_id: z.string().nullable(), source: z.enum(['run', 'legacy']).default('run'), timestamp_estimated: z.boolean().default(false), sequence: z.number().int(), role: z.enum(['user', 'assistant']), text: z.string(), created_at: z.string() });
export const runDto = z.object({
  id: z.string(), thread_id: z.string(), user_message_id: z.string(), client_request_id: z.string(),
  status: z.enum(['queued', 'running', 'completed', 'completed_with_warnings', 'failed', 'interrupted']), phase: z.string(),
  retry_of: nullableText.optional(), error_code: nullableText.optional(), warnings: z.array(z.string()).default([]),
  model_version: nullableText.optional(), profile_version: nullableText.optional(), created_at: z.string(),
});
export type Run = z.infer<typeof runDto>;
export const terminal = (run: Run) => !['queued', 'running'].includes(run.status);
export const runSnapshotDto = runDto.extend({ messages: z.array(messageDto), last_event_sequence: z.number().int().nonnegative() });
export type RunSnapshot = z.infer<typeof runSnapshotDto>;
export const threadDto = z.object({ id: z.string(), character_id: z.string(), title: z.string(), created_at: z.string(), updated_at: z.string(), current_run: runDto.nullable().optional(), latest_run: runDto.nullable().optional(), version: z.number().int().default(1), memory_policy_version: z.number().int().default(1), memory_retrieval_enabled: z.boolean().default(false), memory_storage_enabled: z.boolean().default(false), deleted_at: nullableText.optional(), source: z.enum(['desktop', 'cli', 'legacy']).default('desktop'), history_notice: nullableText.optional() });
export const legacyDto = z.object({ source_id: z.string(), thread_id: nullableText, character_id: nullableText, status: z.enum(['unlinked', 'queued', 'running', 'completed', 'failed', 'purged']), error_code: nullableText, title: z.string().optional() });
export type LegacyThread = z.infer<typeof legacyDto>;
export const legacyPreviewDto = z.object({ items: z.array(z.object({ source_message_id: z.string(), role: z.enum(['user', 'assistant']), text: z.string(), created_at: z.string(), timestamp_estimated: z.boolean() })), notice: z.string() });
export const pageOf = <T extends z.ZodType>(schema: T) => z.object({ items: z.array(schema), next_cursor: z.string().nullable() });
export const memoryDto = z.object({ id: z.string(), memory: z.string(), importance: z.number().nullable(), event_date: nullableText, update_time: nullableText, keywords: z.array(z.string()) });
export const hitDto = z.object({ id: z.string(), memory: z.string(), event_date: nullableText.optional(), update_time: nullableText.optional() });
const participant = z.object({ location: nullableText.optional(), mood: nullableText.optional(), body: nullableText.optional(), clothing: nullableText.optional(), hearing: nullableText.optional() });
export const stateDto = z.object({
  world_state: z.object({ weather: nullableText.optional(), time: z.object({ date: nullableText.optional(), weekday: nullableText.optional(), period: nullableText.optional() }).optional() }).optional(),
  character_state: participant.optional(), user_state: participant.optional(), retrieved_memories: z.array(hitDto).optional(),
});
export type ConversationState = z.infer<typeof stateDto>;
export const eventDto = z.object({ run_id: z.string(), sequence: z.number().int().positive(), type: z.enum(['run.started', 'phase', 'message.committed', 'state.updated', 'memory.retrieved', 'run.completed', 'run.failed']), payload: z.record(z.string(), z.unknown()), created_at: z.string() });
export type ServiceEvent = z.infer<typeof eventDto>;
export const capabilitiesDto = z.object({ characters: z.boolean(), resources: z.boolean(), memories: z.boolean(), chat: z.boolean(), profile_write: z.boolean(), model_settings: z.boolean(), speech: z.boolean() });
export const readinessDto = z.object({ status: z.enum(['ready', 'not_ready']), database: z.enum(['connected', 'not_configured', 'unavailable']), capabilities: capabilitiesDto });
export type Readiness = z.infer<typeof readinessDto>;
export const settingsDto = z.object({ model_configuration: z.enum(['present', 'missing', 'invalid']), database_configured: z.boolean(), capabilities: capabilitiesDto, models: z.record(z.string(), z.object({ provider: z.string(), configured: z.boolean(), credentials_configured: z.boolean().nullable().optional(), local_model_available: z.boolean().nullable().optional() })) });
export type SettingsStatus = z.infer<typeof settingsDto>;
export const nodeModelDto = z.object({ provider: z.enum(['deepseek', 'moonshot', 'llama_cpp']), model: z.string().min(1).max(200), thinking: z.enum(['enabled', 'disabled']), reasoning_effort: z.enum(['none', 'low', 'medium', 'high', 'max']) });
export type NodeModel = z.infer<typeof nodeModelDto>;
export const modelSettingsDto = z.object({ saved_version: z.string(), active_version: nullableText, pending_changes: z.boolean(), nodes: z.record(z.string(), nodeModelDto), effective_from: z.string(), runtime_busy: z.boolean() });
export type ModelSettings = z.infer<typeof modelSettingsDto>;
export const speechDto = z.object({ id: z.string(), message_id: z.string(), status: z.enum(['queued', 'running', 'completed', 'failed', 'interrupted']), resource_id: nullableText, resource_url: nullableText, error_code: nullableText, created_at: z.string(), finished_at: nullableText });
export type SpeechJob = z.infer<typeof speechDto>;
export const checkpointSyncDto = z.object({
  dry_run: z.boolean(), checkpoint_id: nullableText, latest_message_id: nullableText, latest_message_text: nullableText,
  matched_message_id: nullableText, delete_count: z.number().int().nonnegative(), delete_preview: z.array(messageDto),
  preview_truncated: z.boolean().default(false), run_count: z.number().int().nonnegative().default(0),
  version: z.number().int(), state: stateDto.default({}),
});
export type CheckpointSyncResult = z.infer<typeof checkpointSyncDto>;
