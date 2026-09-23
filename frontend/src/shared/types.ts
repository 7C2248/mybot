import { z } from 'zod';
import type { ConversationState, Run, SpeechJob, MemoryStatus } from './api/contracts';

export const messageSchema = z.object({
  id: z.string(), role: z.enum(['user', 'assistant']), text: z.string(), createdAt: z.string(),
  sequence: z.number().optional(), runId: z.string().optional(), timestampEstimated: z.boolean().optional(),
});
export type Message = z.infer<typeof messageSchema>;
export const threadSchema = z.object({
  id: z.string(), characterId: z.string(), title: z.string(), createdAt: z.string(),
  messages: z.array(messageSchema), draft: z.string().default(''),
  unread: z.boolean().default(false),
  phase: z.enum(['idle', 'sending', 'queued', 'preparing', 'replying', 'reviewing', 'recalling', 'using_tools', 'updating_state', 'updating_memory', 'error']).default('idle'),
  error: z.string().optional(),
});
export interface MemoryPolicy { retrieval: boolean; storage: boolean }
export type Thread = z.infer<typeof threadSchema> & {
  version?: number; memoryPolicy?: MemoryPolicy; source?: 'desktop' | 'cli' | 'legacy'; deletedAt?: string | null; historyNotice?: string | null;
  run?: Run; state?: ConversationState; historyCursor?: string | null; historyLoaded?: boolean;
  historyLoading?: boolean; syncError?: string; pending?: PendingInput; unsent?: string;
  memoryStatus?: MemoryStatus; memoryStatusError?: string;
};
export interface PendingInput { requestId: string; text: string; retryOf?: string }
export interface SpeechRequest { requestId: string; job?: SpeechJob; error?: string }
export const isBusy = (thread: Thread) => !['idle', 'error'].includes(thread.phase);
export const phaseLabels: Record<Thread['phase'], string> = { idle: '本轮已完成', error: '运行未完成', sending: '正在发送…', queued: '等待执行…', preparing: '正在准备…', replying: '正在回复…', reviewing: '整理回复…', recalling: '正在检索记忆…', using_tools: '正在使用工具…', updating_state: '正在更新状态…', updating_memory: '正在提交记忆任务…' };
export const spriteSchema = z.object({
  enabled: z.boolean().default(false), assetId: z.string().default(''),
  side: z.enum(['left', 'right']).default('right'), scale: z.number().min(50).max(150).default(100),
  mirror: z.boolean().default(false),
});
export type SpriteSettings = z.infer<typeof spriteSchema>;
export const preferencesSchema = z.object({
  theme: z.enum(['system', 'dark', 'light']).default('system'),
  accent: z.enum(['violet', 'rose']).default('violet'),
  density: z.enum(['comfortable', 'compact']).default('comfortable'),
  inspector: z.boolean().default(true),
  replyPlacement: z.enum(['bubble', 'inline']).default('bubble'),
  alwaysOnTop: z.boolean().default(true),
  compactWidth: z.number().finite().min(320).max(2400).default(660),
  historyHeight: z.number().finite().min(72).max(1600).default(280),
  sprites: z.record(z.string(), spriteSchema).default({}),
});
export type Preferences = z.infer<typeof preferencesSchema>;
export const persistedSchema = z.object({
  version: z.literal(1), threads: z.array(threadSchema), activeThreadId: z.string(),
  preferences: preferencesSchema,
  profileOverrides: z.record(z.string(), z.record(z.string(), z.string())).default({}),
});
export type PersistedWorkspace = z.infer<typeof persistedSchema>;
export const characterSchema = z.object({
  id: z.string(), name: z.string(), profiles: z.record(z.string(), z.string()),
  version: z.string().optional(),
  assets: z.array(z.object({ id: z.string(), name: z.string(), url: z.string() })),
});
export type Character = z.infer<typeof characterSchema>;
export interface Memory {
  id: string; characterId: string; memory: string; importance: number | null;
  event_date: string | null; update_time: string | null; keywords: string[];
}
export type RunEvent = { type: 'phase'; phase: 'replying' | 'reviewing' }
  | { type: 'message.committed'; message: Message };
export interface ChatService {
  readonly mode: 'demo';
  characters(): Promise<Character[]>;
  memories(characterId: string): Promise<Memory[]>;
  run(thread: Thread, requestId: string, emit: (event: RunEvent) => void): Promise<void>;
}
export interface ReadingPosition { top: number; following: boolean; count: number; anchorId?: string; offset?: number; lastId?: string }
