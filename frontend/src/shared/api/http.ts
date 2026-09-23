import { z } from 'zod';
import { memoryStatusDto } from './contracts';
import { characterSchema, type Character, type Message, type Memory, type MemoryPolicy, type Thread } from '../types';
import { eventDto, legacyDto, legacyPreviewDto, memoryDto, messageDto, modelSettingsDto, checkpointSyncDto, pageOf, readinessDto, runSnapshotDto, settingsDto, speechDto, stateDto, threadDto, type NodeModel, type ServiceEvent } from './contracts';

export class ApiError extends Error {
  constructor(message: string, readonly code = 'connection_failed', readonly status = 0, readonly details: Record<string, unknown> = {}) { super(message); }
  get uncertain() { return this.status === 0 || this.status >= 500; }
}
export const errorText = (error: unknown) => error instanceof Error ? error.message : '请求失败，请重试。';
export function localBaseUrl(input: string) {
  const url = new URL(input);
  if (url.protocol !== 'http:' || !['localhost', '127.0.0.1'].includes(url.hostname) || url.username || url.password || url.search || url.hash || url.pathname !== '/') throw new Error('服务地址必须是本机 HTTP 地址，例如 http://127.0.0.1:8765。');
  url.hostname = '127.0.0.1';
  return url.origin;
}
export function toMessage(message: z.infer<typeof messageDto>): Message {
  return { id: message.id, role: message.role, text: message.text, createdAt: message.created_at, sequence: message.sequence, runId: message.run_id ?? undefined, timestampEstimated: message.timestamp_estimated };
}
export function toThread(thread: z.infer<typeof threadDto>): Thread {
  const run = thread.current_run ?? thread.latest_run ?? undefined;
  return { id: thread.id, characterId: thread.character_id, title: thread.title, createdAt: thread.created_at, messages: [], draft: '', unread: false, phase: 'idle', run, version: thread.version, memoryPolicy: { retrieval: thread.memory_retrieval_enabled, storage: thread.memory_storage_enabled }, source: thread.source, deletedAt: thread.deleted_at, historyNotice: thread.history_notice };
}
export class HttpService {
  readonly mode = 'service' as const;
  readonly baseUrl: string;
  constructor(baseUrl = 'http://127.0.0.1:8765', private fetcher: typeof fetch = (...args) => fetch(...args)) { this.baseUrl = localBaseUrl(baseUrl); }
  resource(path: string) {
    if (!/^\/api\/(?:resources|audio\/resources)\/[a-zA-Z0-9-]+$/.test(path)) throw new ApiError('服务返回的资源地址无效。', 'invalid_response');
    return this.baseUrl + path;
  }
  private async check(response: Response) {
    if (response.ok) return;
    const payload = await response.json().catch(() => null);
    const parsed = z.object({ error: z.object({ code: z.string(), message: z.string(), details: z.record(z.string(), z.unknown()).nullish() }) }).safeParse(payload);
    throw parsed.success ? new ApiError(parsed.data.error.message, parsed.data.error.code, response.status, parsed.data.error.details ?? {}) : new ApiError(`服务请求失败（${response.status}）。`, 'http_error', response.status);
  }
  private async json<T extends z.ZodType>(path: string, schema: T, method = 'GET', body?: unknown, signal?: AbortSignal, allowUnavailable = false): Promise<z.infer<T>> {
    const timeout = AbortSignal.timeout(15_000);
    try {
      const response = await this.fetcher(this.baseUrl + '/api' + path, { method,
        headers: { 'X-Mybot-Client': 'mybot-desktop', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        body: body === undefined ? undefined : JSON.stringify(body), signal: signal ? AbortSignal.any([signal, timeout]) : timeout });
      if (!(allowUnavailable && response.status === 503)) await this.check(response);
      const result = schema.safeParse(await response.json());
      if (!result.success) throw new ApiError('服务响应格式不兼容，请检查服务版本。', 'invalid_response');
      return result.data;
    } catch (error) {
      if (signal?.aborted || error instanceof ApiError) throw error;
      throw new ApiError('无法连接本机服务或请求超时，请检查服务状态后重试。');
    }
  }
  async health(signal?: AbortSignal) { return this.json('/health', z.object({ service: z.literal('mybot'), status: z.literal('ok') }), 'GET', undefined, signal); }
  ready(signal?: AbortSignal) { return this.json('/ready', readinessDto, 'GET', undefined, signal, true); }
  settings() { return this.json('/settings', settingsDto); }
  async characters(signal?: AbortSignal): Promise<Character[]> {
    const list = await this.json('/characters', z.array(z.object({ id: z.string() })), 'GET', undefined, signal);
    return Promise.all(list.map(({ id }) => this.character(id, signal)));
  }
  async character(id: string, signal?: AbortSignal) {
    const value = await this.json(`/characters/${encodeURIComponent(id)}`, characterSchema, 'GET', undefined, signal);
    return { ...value, assets: value.assets.map((asset) => ({ ...asset, url: this.resource(asset.url) })) };
  }
  async saveProfile(id: string, language: string, text: string, version: string) {
    await this.json(`/characters/${encodeURIComponent(id)}/profiles/${encodeURIComponent(language)}`, characterSchema, 'PUT', { expected_version: version, text });
    return this.character(id);
  }
  async threads(cursor?: string, signal?: AbortSignal, deleted = false) {
    const result = await this.json('/threads?' + new URLSearchParams({ deleted: String(deleted), ...(cursor ? { cursor } : {}) }), pageOf(threadDto), 'GET', undefined, signal);
    return { items: result.items.map(toThread), next_cursor: result.next_cursor };
  }
  async createThread(characterId: string, title: string, policy: MemoryPolicy = { retrieval: false, storage: false }) { return toThread(await this.json('/threads', threadDto, 'POST', { character_id: characterId, title: title.trim() || '新的对话', memory_retrieval_enabled: policy.retrieval, memory_storage_enabled: policy.storage })); }
  async thread(id: string) { return toThread(await this.json(`/threads/${id}`, threadDto)); }
  async updateThread(id: string, version: number, title: string, policy: MemoryPolicy) {
    return toThread(await this.json(`/threads/${id}`, threadDto, 'PATCH', { expected_version: version, title, memory_retrieval_enabled: policy.retrieval, memory_storage_enabled: policy.storage }));
  }
  async deleteThread(id: string, version: number) { return toThread(await this.json(`/threads/${id}`, threadDto, 'DELETE', { expected_version: version })); }
  async restoreThread(id: string, version: number) { return toThread(await this.json(`/threads/${id}/restore`, threadDto, 'POST', { expected_version: version })); }
  purgeThread(id: string, version: number) { return this.json(`/threads/${id}/purge`, z.object({ deleted: z.boolean() }), 'DELETE', { expected_version: version }); }
  legacyThreads(cursor?: string) { return this.json('/legacy/threads' + (cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''), pageOf(legacyDto)); }
  importLegacy(source: string, character: string, title: string) { return this.json(`/legacy/threads/${encodeURIComponent(source)}/import`, legacyDto, 'POST', { character_id: character, title }); }
  legacyPreview(source: string) { return this.json(`/legacy/threads/${encodeURIComponent(source)}/messages`, legacyPreviewDto); }
  async messages(id: string, before?: string, signal?: AbortSignal) {
    const result = await this.json(`/threads/${id}/messages` + (before ? `?before=${encodeURIComponent(before)}` : ''), pageOf(messageDto), 'GET', undefined, signal);
    return { items: result.items.map(toMessage), next_cursor: result.next_cursor };
  }
  state(id: string, signal?: AbortSignal) { return this.json(`/threads/${id}/state`, stateDto, 'GET', undefined, signal); }
  memoryStatus(id: string, signal?: AbortSignal) { return this.json(`/threads/${id}/memory-status`, memoryStatusDto, 'GET', undefined, signal); }
  submit(id: string, text: string, key: string, signal?: AbortSignal) { return this.json(`/threads/${id}/runs`, z.object({ run_id: z.string(), status: z.string() }), 'POST', { text, client_request_id: key }, signal); }
  retry(id: string, key: string, signal?: AbortSignal) { return this.json(`/runs/${id}/retry`, z.object({ run_id: z.string(), status: z.string() }), 'POST', { client_request_id: key }, signal); }
  snapshot(id: string, signal?: AbortSignal) { return this.json(`/runs/${id}`, runSnapshotDto, 'GET', undefined, signal); }
  async memories(character: string, query = '', cursor?: string, signal?: AbortSignal): Promise<{ items: Memory[]; next_cursor: string | null }> {
    const params = new URLSearchParams({ query }); if (cursor) params.set('cursor', cursor);
    const result = await this.json(`/characters/${encodeURIComponent(character)}/memories?${params}`, pageOf(memoryDto), 'GET', undefined, signal);
    return { ...result, items: result.items.map((item) => ({ ...item, characterId: character })) };
  }
  async memory(character: string, id: string, signal?: AbortSignal): Promise<Memory> { return { ...await this.json(`/characters/${encodeURIComponent(character)}/memories/${encodeURIComponent(id)}`, memoryDto, 'GET', undefined, signal), characterId: character }; }
  models() { return this.json('/settings/models', modelSettingsDto); }
  saveModels(version: string, nodes: Record<string, NodeModel>) { return this.json('/settings/models', modelSettingsDto, 'PUT', { expected_version: version, nodes }); }
  applyModels(version: string) { return this.json('/settings/models/apply', modelSettingsDto, 'POST', { expected_version: version }); }
  speech(messageId: string, key: string) { return this.json(`/messages/${messageId}/speech`, speechDto, 'POST', { client_request_id: key }); }
  speechJob(id: string, signal?: AbortSignal) { return this.json(`/speech/${id}`, speechDto, 'GET', undefined, signal); }
  syncCheckpoint(id: string, expectedVersion: number, dryRun: boolean, checkpointId?: string | null) {
    return this.json(`/threads/${id}/checkpoint-sync`, checkpointSyncDto, 'POST',
      { expected_version: expectedVersion, dry_run: dryRun, ...(checkpointId === undefined ? {} : { checkpoint_id: checkpointId }) });
  }
  async events(id: string, after: number, signal: AbortSignal, emit: (event: ServiceEvent) => void) {
    const controller = new AbortController();
    const combined = AbortSignal.any([signal, controller.signal]);
    let timer = setTimeout(() => controller.abort(), 30_000);
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    try {
      const response = await this.fetcher(`${this.baseUrl}/api/runs/${id}/events?after=${after}`, { signal: combined, headers: { Accept: 'text/event-stream' } });
      await this.check(response);
      if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) throw new ApiError('事件连接格式无效。', 'invalid_response');
      reader = response.body.getReader();
      const decoder = new TextDecoder(); let buffer = '';
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        clearTimeout(timer); timer = setTimeout(() => controller.abort(), 30_000);
        buffer += decoder.decode(value, { stream: true });
        let split;
        while ((split = /\r?\n\r?\n/.exec(buffer))) {
          const frame = buffer.slice(0, split.index); buffer = buffer.slice(split.index + split[0].length);
          const data = frame.split(/\r?\n/).filter((line) => line.startsWith('data:')).map((line) => line.slice(5).replace(/^ /, '')).join('\n');
          if (data) {
            const event = eventDto.parse(JSON.parse(data));
            if (event.run_id !== id) throw new ApiError('事件所属运行不一致。', 'invalid_response');
            if (event.sequence > after) { emit(event); after = event.sequence; }
          }
        }
        if (buffer.length > 2_000_000) throw new ApiError('事件数据过大。', 'invalid_response');
      }
    } finally { clearTimeout(timer); controller.abort(); await reader?.cancel().catch(() => {}); reader?.releaseLock(); }
  }
}
