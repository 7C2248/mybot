import { useSyncExternalStore } from 'react';
import { z } from 'zod';
import { demoService, createDemoThreads } from '../shared/api/demo';
import { HttpService, ApiError, errorText, toMessage } from '../shared/api/http';
import { hitDto, messageDto, speechDto, stateDto, terminal, type CheckpointSyncResult, type Readiness, type Run, type RunSnapshot, type ServiceEvent, type LegacyThread } from '../shared/api/contracts';
import { desktop } from '../shared/platform/window';
import { getLocalServiceStatus } from '../shared/platform/service';
import { persistedSchema, preferencesSchema, isBusy, type PersistedWorkspace, type Character, type ChatService, type Preferences, type ReadingPosition, type Thread, type Message, type SpeechRequest, type MemoryPolicy } from '../shared/types';

export const STORAGE_KEY = 'mybot.frontend.workspace.v1';
export const CONNECTION_KEY = 'mybot.frontend.connection.v1';
export const serviceStorageKey = (base: string) => `mybot.frontend.service.v1:${base}`;
export function initialWorkspace(): PersistedWorkspace {
  return { version: 1, threads: createDemoThreads(), activeThreadId: 'demo-suli', preferences: preferencesSchema.parse({}), profileOverrides: {} };
}
export function restoreWorkspace(raw: string | null): PersistedWorkspace {
  if (!raw) return initialWorkspace();
  const data = persistedSchema.parse(JSON.parse(raw));
  data.threads = data.threads.map((thread) => isBusy(thread) ? { ...thread, phase: 'error', error: '上次预览在回复完成前关闭，可以重试这条消息。' } : thread);
  if (!data.threads.some((thread) => thread.id === data.activeThreadId)) data.activeThreadId = data.threads[0]?.id ?? '';
  return data;
}
const localThreadSchema = z.object({ draft: z.string().default(''), unread: z.boolean().default(false), runId: z.string().optional(), after: z.number().nonnegative().default(0), unsent: z.string().optional(), pending: z.object({ requestId: z.string(), text: z.string(), retryOf: z.string().optional() }).optional() });
const readingSchema = z.object({ top: z.number(), following: z.boolean(), count: z.number(), anchorId: z.string().optional(), offset: z.number().optional(), lastId: z.string().optional() });
const cacheSchema = z.object({ version: z.literal(1), activeThreadId: z.string().default(''), preferences: preferencesSchema, threads: z.record(z.string(), localThreadSchema).default({}), reading: z.record(z.string(), readingSchema).default({}), speech: z.record(z.string(), z.object({ requestId: z.string(), job: speechDto.optional() })).default({}) });
export interface Workspace extends PersistedWorkspace {
  threads: Thread[];
  mode: 'demo' | 'service'; baseUrl: string; connection: 'connecting' | 'connected' | 'disconnected'; connectionError: string; ready?: Readiness;
  characters: Character[]; loading: boolean; resourceError: string; storageWarning: string; threadsCursor?: string | null; threadsLoading?: boolean;
  speech: Record<string, SpeechRequest>; legacy: LegacyThread[]; legacyError: string;
}
const emptyCache = () => cacheSchema.parse({ version: 1, preferences: {} });
const mergeMessages = (previous: Message[], incoming: Message[]) => [...new Map([...previous, ...incoming].map((message) => [message.id, message])).values()].sort((a, b) => (a.sequence ?? 0) - (b.sequence ?? 0));
function runPhase(run?: Run): Thread['phase'] {
  if (!run) return 'idle';
  if (terminal(run)) return ['failed', 'interrupted'].includes(run.status) ? 'error' : 'idle';
  return ['queued', 'preparing', 'replying', 'reviewing', 'recalling', 'using_tools', 'updating_state', 'updating_memory'].includes(run.phase) ? run.phase as Thread['phase'] : 'preparing';
}
const delay = (ms: number, signal: AbortSignal) => new Promise<void>((resolve) => {
  if (signal.aborted) return resolve();
  const finish = () => { clearTimeout(timer); signal.removeEventListener('abort', finish); resolve(); };
  const timer = setTimeout(finish, ms); signal.addEventListener('abort', finish, { once: true });
});
export class WorkspaceStore {
  private listeners = new Set<() => void>();
  private running = new Set<string>();
  private initialized = false;
  private epoch = 0;
  private lifecycle = new AbortController();
  private monitors = new Map<string, { id: string; controller: AbortController }>();
  private histories = new Map<string, AbortController>();
  private timer?: ReturnType<typeof setInterval>;
  private refreshing = false;
  private legacyScanCursor?: string;
  private blockedKeys = new Set<string>();
  private cache = emptyCache();
  readonly reading = new Map<string, ReadingPosition>();
  private value!: Workspace;
  constructor(public service: ChatService | HttpService, private storage?: Pick<Storage, 'getItem' | 'setItem'>) { this.restore(); }
  private get key() { return this.service.mode === 'service' ? serviceStorageKey(this.service.baseUrl) : STORAGE_KEY; }
  private restore() {
    let data = initialWorkspace(); let storageWarning = this.storage ? '' : '本地存储不可用，草稿和偏好只能保留到页面关闭。';
    this.cache = emptyCache(); this.reading.clear();
    this.legacyScanCursor = undefined;
    try {
      const raw = this.storage?.getItem(this.key);
      if (this.service.mode === 'demo') data = restoreWorkspace(raw ?? null);
      else {
        this.cache = raw ? cacheSchema.parse(JSON.parse(raw)) : emptyCache();
        data = { version: 1, threads: [], activeThreadId: this.cache.activeThreadId, preferences: this.cache.preferences, profileOverrides: {} };
        Object.entries(this.cache.reading).forEach(([id, value]) => this.reading.set(id, value));
      }
    } catch {
      this.blockedKeys.add(this.key);
      storageWarning = '本地缓存无法读取，原数据已保留；本次草稿和偏好暂不写入缓存。';
      if (this.service.mode === 'service') data = { version: 1, threads: [], activeThreadId: '', preferences: preferencesSchema.parse({}), profileOverrides: {} };
    }
    this.value = { ...data, mode: this.service.mode, baseUrl: this.service.mode === 'service' ? this.service.baseUrl : '', connection: 'connecting', connectionError: '', characters: [], loading: true, resourceError: '', storageWarning, speech: this.cache.speech, legacy: [], legacyError: '' };
  }
  getSnapshot = () => this.value;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => this.listeners.delete(listener); };
  private persist() {
    if (!this.storage || this.blockedKeys.has(this.key)) return;
    try {
      const { version, threads, activeThreadId, preferences, profileOverrides } = this.value;
      if (this.service.mode === 'demo') this.storage.setItem(this.key, JSON.stringify({ version, threads, activeThreadId, preferences, profileOverrides }));
      else {
        for (const thread of threads) this.cache.threads[thread.id] = { ...this.cache.threads[thread.id], draft: thread.draft, unread: thread.unread, pending: thread.pending, unsent: thread.unsent, runId: thread.run?.id, after: thread.run ? this.cache.threads[thread.id]?.after ?? 0 : 0 };
        this.cache = { ...this.cache, activeThreadId, preferences, reading: Object.fromEntries(this.reading), speech: this.value.speech };
        this.storage.setItem(this.key, JSON.stringify(this.cache));
      }
    } catch { this.value = { ...this.value, storageWarning: '本地存储不可用或已满，草稿和请求恢复标识可能无法在刷新后保留。' }; }
  }
  private publish(patch: Partial<Workspace>, persist = true) {
    this.value = { ...this.value, ...patch }; if (persist) this.persist(); this.listeners.forEach((listener) => listener());
  }
  setReading(id: string, position: ReadingPosition) { this.reading.set(id, position); this.persist(); }
  dispose() { this.epoch++; this.lifecycle.abort(); this.monitors.forEach(({ controller }) => controller.abort()); this.monitors.clear(); this.histories.forEach((controller) => controller.abort()); this.histories.clear(); this.running.clear(); clearInterval(this.timer); this.initialized = false; this.refreshing = false; }
  async switchMode(mode: 'demo' | 'service', baseUrl = 'http://127.0.0.1:8765') {
    const next = mode === 'demo' ? demoService : new HttpService(baseUrl);
    this.dispose(); this.lifecycle = new AbortController(); this.service = next; this.restore();
    try { this.storage?.setItem(CONNECTION_KEY, JSON.stringify({ mode, baseUrl: next.mode === 'service' ? next.baseUrl : baseUrl })); } catch { /* Existing storage warning remains visible. */ }
    this.publish({}, false); await this.initialize();
  }
  async initialize() {
    if (this.initialized) return; this.initialized = true;
    const epoch = this.epoch;
    if (this.service.mode === 'demo') {
      try { const characters = await this.service.characters(); if (epoch === this.epoch) this.publish({ characters, loading: false, connection: 'connected' }, false); }
      catch (error) { if (epoch === this.epoch) this.publish({ loading: false, resourceError: errorText(error), connection: 'disconnected' }, false); }
      return;
    }
    await this.refresh();
    if (epoch === this.epoch) this.timer = setInterval(() => { void this.refresh(); }, 5000);
  }
  async refresh() {
    const api = this.service; if (api.mode !== 'service' || this.refreshing) return;
    this.refreshing = true; const epoch = this.epoch; const signal = this.lifecycle.signal;
    try {
      await api.health(signal);
      const [ready, characters] = await Promise.all([api.ready(signal), api.characters(signal)]);
      if (epoch !== this.epoch) return;
      this.publish({ ready, characters, connection: 'connected', connectionError: '', resourceError: '', loading: false }, false);
      if (ready.database === 'connected') {
        await this.loadThreads(false);
        const active = this.value.threads.find((item) => item.id === this.value.activeThreadId);
        if (active) {
          try {
            const current = await api.thread(active.id);
            if (epoch !== this.epoch) return;
            this.updateThread(active.id, (old) => this.mergeRemoteThread(current, old));
            const latest = this.value.threads.find((item) => item.id === active.id)!;
            const changed = latest.run?.id !== active.run?.id || latest.run?.status !== active.run?.status;
            if (!latest.historyLoaded || changed) await this.loadHistory(active.id);
            await this.loadMemoryStatus(active.id);
          } catch (error) { if (epoch === this.epoch && error instanceof ApiError && error.status === 404) this.forgetThread(active.id); else throw error; }
        }
        try {
          const legacy = await api.legacyThreads();
          if (epoch !== this.epoch) return;
          this.publish({ legacy: legacy.items, legacyError: '' }, false);
          // Walk further pages too: unknown roles on page one must not block known roles later.
          const scanned = this.legacyScanCursor ? await api.legacyThreads(this.legacyScanCursor) : legacy;
          if (epoch !== this.epoch) return;
          this.legacyScanCursor = scanned.next_cursor ?? undefined;
          for (const item of new Map([...legacy.items, ...scanned.items].map((item) => [item.source_id, item])).values()) if (item.status === 'unlinked' && item.character_id) {
            await api.importLegacy(item.source_id, item.character_id, item.title ?? 'CLI 历史');
          }
        } catch (error) { if (epoch === this.epoch) this.publish({ legacyError: errorText(error) }, false); }
        for (const thread of this.value.threads) {
          if (thread.pending) void this.submitPending(thread.id);
          else if (thread.run && (!terminal(thread.run) || !thread.historyLoaded)) this.watch(thread.id, thread.run.id);
        }
      }
    } catch (error) {
      if (epoch === this.epoch && !signal.aborted) this.publish({ connection: 'disconnected', connectionError: errorText(error), loading: false }, false);
    } finally { if (epoch === this.epoch) this.refreshing = false; }
  }
  private updateThread(id: string, update: (thread: Thread) => Thread) { this.publish({ threads: this.value.threads.map((thread) => thread.id === id ? update(thread) : thread) }); }
  private resetHistory(thread: Thread): Thread {
    this.monitors.get(thread.id)?.controller.abort(); this.monitors.delete(thread.id);
    this.histories.get(thread.id)?.abort(); this.histories.delete(thread.id);
    this.reading.set(thread.id, { top: 0, following: true, count: 0 });
    this.cache.threads[thread.id] = { ...this.cache.threads[thread.id], draft: thread.draft, unread: false, runId: undefined, after: 0 };
    return { ...thread, messages: [], historyLoaded: false, historyLoading: false, historyCursor: null,
      unread: false, run: undefined, phase: 'idle', error: undefined, syncError: undefined, state: undefined };
  }
  private mergeRemoteThread(current: Thread, old?: Thread): Thread {
    // A poll started before a sync may arrive after its commit response.
    if (old && (current.version ?? 0) < (old.version ?? 0)) return old;
    const invalidated = old && (current.version ?? 0) > (old.version ?? 0);
    const previous = invalidated ? this.resetHistory(old) : old;
    const saved = this.cache.threads[current.id], run = current.run;
    if (previous?.run?.id !== run?.id) { this.monitors.get(current.id)?.controller.abort(); this.monitors.delete(current.id); }
    if (saved?.runId !== run?.id) this.cache.threads[current.id] = { ...saved, draft: saved?.draft ?? '', unread: saved?.unread ?? false, runId: run?.id, after: 0 };
    return { ...current, ...previous, title: current.title, version: current.version, memoryPolicy: current.memoryPolicy,
      source: current.source, historyNotice: current.historyNotice, run, phase: runPhase(run),
      error: run && previous?.run?.id === run.id ? previous.error : undefined,
      draft: previous?.draft ?? saved?.draft ?? '', unread: previous?.unread ?? saved?.unread ?? false,
      pending: previous?.pending ?? saved?.pending, unsent: previous?.unsent ?? saved?.unsent };
  }
  async loadThreads(more = true) {
    const api = this.service; if (api.mode !== 'service' || this.value.threadsLoading || (more && !this.value.threadsCursor)) return;
    const epoch = this.epoch; this.publish({ threadsLoading: true }, false);
    try {
      const page = await api.threads(more ? this.value.threadsCursor ?? undefined : undefined, this.lifecycle.signal);
      if (epoch !== this.epoch) return;
      const existing = new Map(this.value.threads.map((thread) => [thread.id, thread]));
      const items = page.items.map((thread) => this.mergeRemoteThread(thread, existing.get(thread.id)));
      const ids = new Set(items.map((item) => item.id));
      const received = new Map(items.map((item) => [item.id, item]));
      const threads = more ? [...this.value.threads.map((item) => received.get(item.id) ?? item), ...items.filter((item) => !existing.has(item.id))] : [...items, ...this.value.threads.filter((item) => !ids.has(item.id))];
      this.publish({ threads, threadsCursor: more || !this.value.threads.length ? page.next_cursor : this.value.threadsCursor,
        activeThreadId: this.value.activeThreadId || threads[0]?.id || '' });
      // Recover an older selected thread even when it falls beyond the first page.
      if (!threads.some((item) => item.id === this.value.activeThreadId) && page.next_cursor) {
        this.publish({ threadsCursor: page.next_cursor, threadsLoading: false }, false); await this.loadThreads(true);
      } else if (!threads.some((item) => item.id === this.value.activeThreadId)) this.publish({ activeThreadId: threads[0]?.id ?? '' });
      for (const item of items) if (item.run && !item.pending && (item.historyLoaded || !terminal(item.run))) {
        const changed = existing.get(item.id)?.run?.id !== item.run.id || existing.get(item.id)?.run?.status !== item.run.status;
        if (!terminal(item.run) || changed) this.watch(item.id, item.run.id);
      }
    } finally { if (epoch === this.epoch) this.publish({ threadsLoading: false }, false); }
  }
  selectThread(id: string) { this.publish({ activeThreadId: id }); this.markRead(id); if (this.service.mode === 'service') void this.loadHistory(id); }
  markRead(id: string) { if (this.value.threads.find((thread) => thread.id === id)?.unread) this.updateThread(id, (thread) => ({ ...thread, unread: false })); }
  async createThread(characterId: string, title: string, policy?: MemoryPolicy) {
    const api = this.service, epoch = this.epoch;
    const thread: Thread = api.mode === 'service' ? await api.createThread(characterId, title, policy) : { id: crypto.randomUUID(), characterId, title: title.trim() || '新的对话', createdAt: new Date().toISOString(), messages: [], draft: '', unread: false, phase: 'idle' };
    if (epoch === this.epoch) this.publish({ threads: [{ ...thread, historyLoaded: true }, ...this.value.threads], activeThreadId: thread.id });
  }
  forgetThread(id: string) {
    this.monitors.get(id)?.controller.abort(); this.monitors.delete(id); this.running.delete(id);
    this.histories.get(id)?.abort(); this.histories.delete(id);
    delete this.cache.threads[id]; this.reading.delete(id);
    const messages = new Set(this.value.threads.find((thread) => thread.id === id)?.messages.map((message) => message.id));
    const threads = this.value.threads.filter((thread) => thread.id !== id);
    this.publish({ threads, activeThreadId: this.value.activeThreadId === id ? threads[0]?.id ?? '' : this.value.activeThreadId,
      speech: Object.fromEntries(Object.entries(this.value.speech).filter(([message]) => !messages.has(message))) });
  }
  async manageThread(id: string, version: number, action: 'delete' | 'restore' | 'purge') {
    const api = this.service, epoch = this.epoch; if (api.mode !== 'service') return;
    if (action === 'delete') await api.deleteThread(id, version);
    else if (action === 'restore') await api.restoreThread(id, version);
    else await api.purgeThread(id, version);
    if (epoch !== this.epoch) return;
    if (action !== 'restore') this.forgetThread(id);
    await this.refresh();
  }
  async updateConversation(id: string, version: number, title: string, policy: MemoryPolicy) {
    const api = this.service, epoch = this.epoch; if (api.mode !== 'service') return;
    const next = await api.updateThread(id, version, title, policy);
    if (epoch === this.epoch) this.updateThread(id, (old) => ({ ...old, title: next.title, version: next.version, memoryPolicy: next.memoryPolicy }));
    return next;
  }
  async previewCheckpointSync(id: string) {
    const api = this.service, epoch = this.epoch;
    if (api.mode !== 'service') throw new Error('演示模式不支持重新加载检查点。');
    const current = await api.thread(id);
    if (epoch !== this.epoch) throw new Error('服务连接已切换，请重新预览。');
    this.updateThread(id, (old) => this.mergeRemoteThread(current, old));
    return api.syncCheckpoint(id, current.version ?? 1, true);
  }
  async applyCheckpointSync(id: string, preview: CheckpointSyncResult) {
    const api = this.service, epoch = this.epoch; if (api.mode !== 'service') return;
    const applied = await api.syncCheckpoint(id, preview.version, false, preview.checkpoint_id);
    if (epoch !== this.epoch) return applied;
    this.updateThread(id, (thread) => (thread.version ?? 0) > applied.version ? thread :
      { ...this.resetHistory(thread), version: applied.version, state: applied.state });
    try {
      const current = await api.thread(id);
      if (epoch === this.epoch) this.updateThread(id, (thread) => this.mergeRemoteThread(current, thread));
    } catch { /* The history reload below still refreshes the visible messages. */ }
    if (epoch === this.epoch) await this.loadHistory(id);
    return applied;
  }
  setDraft(id: string, draft: string) { this.updateThread(id, (thread) => ({ ...thread, draft })); }
  restoreUnsent(id: string) { this.updateThread(id, (thread) => thread.draft ? thread : { ...thread, draft: thread.unsent ?? '', unsent: undefined }); }
  setPreferences(patch: Partial<Preferences>) { this.publish({ preferences: preferencesSchema.parse({ ...this.value.preferences, ...patch }) }); }
  async saveProfile(id: string, language: string, text: string, version?: string) {
    const api = this.service, epoch = this.epoch;
    if (api.mode === 'demo') this.publish({ profileOverrides: { ...this.value.profileOverrides, [id]: { ...this.value.profileOverrides[id], [language]: text } } });
    else {
      if (!version) throw new Error('请先重新读取档案版本。');
      const character = await api.saveProfile(id, language, text, version);
      if (epoch === this.epoch) this.publish({ characters: this.value.characters.map((item) => item.id === id ? character : item) }, false);
    }
  }
  async reloadCharacter(id: string) {
    const api = this.service, epoch = this.epoch;
    if (api.mode !== 'service') return;
    const character = await api.character(id);
    if (epoch === this.epoch) this.publish({ characters: this.value.characters.map((item) => item.id === id ? character : item) }, false);
    return character;
  }
  async loadHistory(id: string, more = false) {
    const api = this.service; const thread = this.value.threads.find((item) => item.id === id);
    if (api.mode !== 'service' || !thread || thread.historyLoading || (more && !thread.historyCursor)) return;
    const epoch = this.epoch, controller = new AbortController();
    this.histories.set(id, controller);
    const signal = AbortSignal.any([controller.signal, this.lifecycle.signal]);
    const current = () => epoch === this.epoch && this.histories.get(id) === controller && !signal.aborted;
    this.updateThread(id, (item) => ({ ...item, historyLoading: true }));
    try {
      const [page, state] = await Promise.all([api.messages(id, more ? thread.historyCursor ?? undefined : undefined, signal), api.state(id, signal)]);
      if (!current()) return;
      this.updateThread(id, (item) => ({ ...item, messages: mergeMessages(item.messages, page.items), state, historyLoaded: true,
        historyCursor: more || !item.historyLoaded ? page.next_cursor : item.historyCursor, syncError: undefined }));
      const run = this.value.threads.find((item) => item.id === id)?.run;
      if (run) this.watch(id, run.id);
    } catch (error) { if (current()) this.updateThread(id, (item) => ({ ...item, syncError: errorText(error) })); }
    finally { if (current()) { this.histories.delete(id); this.updateThread(id, (item) => ({ ...item, historyLoading: false })); } }
  }
  private applySnapshot(id: string, run: RunSnapshot) {
    this.updateThread(id, (thread) => {
      const incoming = run.messages.map(toMessage);
      const unread = incoming.some((message) => message.role === 'assistant' && !thread.messages.some((known) => known.id === message.id));
      return { ...thread, run, phase: runPhase(run), messages: mergeMessages(thread.messages, incoming), unread: thread.unread || unread, syncError: undefined,
        error: run.status === 'interrupted' ? '服务执行已中断，可明确重试本轮。' : run.status === 'failed' ? '本轮未生成正式回复，可重试。' : run.status === 'completed_with_warnings' ? '回复已保存，后续处理存在异常。' : undefined };
    });
  }
  async loadMemoryStatus(id: string) {
    const api = this.service, epoch = this.epoch;
    if (api.mode !== 'service') return;
    try {
      const memoryStatus = await api.memoryStatus(id, this.lifecycle.signal);
      if (epoch === this.epoch) this.updateThread(id, (thread) => ({ ...thread, memoryStatus, memoryStatusError: undefined }));
    } catch (error) {
      if (epoch === this.epoch) this.updateThread(id, (thread) => ({ ...thread, memoryStatusError: errorText(error) }));
    }
  }
  private applyEvent(id: string, event: ServiceEvent) {
    const saved = this.cache.threads[id];
    if (saved?.runId === event.run_id && event.sequence <= saved.after) return;
    if (event.type === 'message.committed') {
      const message = toMessage(messageDto.parse(event.payload.message));
      this.updateThread(id, (thread) => ({ ...thread, unread: thread.unread || !thread.messages.some((known) => known.id === message.id), messages: mergeMessages(thread.messages, [message]) }));
    } else if (event.type === 'state.updated') {
      const state = stateDto.parse(event.payload); this.updateThread(id, (thread) => ({ ...thread, state: { ...thread.state, ...state } }));
    } else if (event.type === 'memory.retrieved') {
      const hits = z.array(hitDto).parse(event.payload.hits);
      this.updateThread(id, (thread) => ({ ...thread, state: { ...thread.state, retrieved_memories: [...new Map([...(thread.state?.retrieved_memories ?? []), ...hits].map((hit) => [hit.id, hit])).values()] } }));
    } else if (event.type === 'phase' || event.type === 'run.started') {
      this.updateThread(id, (thread) => ({ ...thread, phase: runPhase({ ...thread.run!, phase: String(event.payload.phase), status: 'running' }), syncError: undefined }));
    }
    this.cache.threads[id] = { ...this.cache.threads[id], draft: this.cache.threads[id]?.draft ?? '', unread: this.cache.threads[id]?.unread ?? false, runId: event.run_id, after: event.sequence };
    this.persist();
  }
  private watch(id: string, runId: string) {
    const api = this.service; if (api.mode !== 'service') return;
    const old = this.monitors.get(id); if (old?.id === runId) return; old?.controller.abort();
    const controller = new AbortController(), signal = AbortSignal.any([controller.signal, this.lifecycle.signal]);
    this.monitors.set(id, { id: runId, controller }); const epoch = this.epoch;
    void (async () => {
      let failures = 0;
      try {
        while (!signal.aborted && epoch === this.epoch) {
          try {
            const snapshot = await api.snapshot(runId, signal);
            const state = await api.state(id, signal);
            if (signal.aborted || epoch !== this.epoch) return;
            this.applySnapshot(id, snapshot); this.updateThread(id, (thread) => ({ ...thread, state }));
            if (terminal(snapshot)) { await this.loadMemoryStatus(id); return; }
            const saved = this.cache.threads[id]; const after = saved?.runId === runId ? Math.min(saved.after, snapshot.last_event_sequence) : 0;
            await api.events(runId, after, signal, (event) => { if (!signal.aborted && epoch === this.epoch) { this.applyEvent(id, event); failures = 0; } });
          } catch (error) {
            if (signal.aborted || epoch !== this.epoch) return;
            this.updateThread(id, (thread) => ({ ...thread, syncError: `同步暂时中断，正在恢复：${errorText(error)}` }));
            if (error instanceof ApiError && error.status === 404) return;
            failures++;
          }
          await delay(Math.min(10_000, 500 * 2 ** Math.min(failures, 5)), signal);
        }
      } finally { if (this.monitors.get(id)?.controller === controller) this.monitors.delete(id); }
    })();
  }
  async recover(id: string) {
    const thread = this.value.threads.find((item) => item.id === id); if (!thread) return;
    if (thread.pending) await this.submitPending(id);
    else { if (thread.run) this.watch(id, thread.run.id); await this.loadHistory(id); }
  }
  private async submitPending(id: string) {
    const api = this.service, thread = this.value.threads.find((item) => item.id === id);
    if (api.mode !== 'service' || !thread?.pending || this.running.has(id)) return;
    const pending = thread.pending, epoch = this.epoch; this.running.add(id);
    this.updateThread(id, (item) => ({ ...item, phase: 'sending', syncError: undefined }));
    try {
      const accepted = pending.retryOf ? await api.retry(pending.retryOf, pending.requestId, this.lifecycle.signal) : await api.submit(id, pending.text, pending.requestId, this.lifecycle.signal);
      if (epoch !== this.epoch) return;
      this.updateThread(id, (item) => ({ ...item, phase: accepted.status === 'queued' ? 'queued' : 'preparing' }));
      const snapshot = await api.snapshot(accepted.run_id, this.lifecycle.signal);
      if (epoch !== this.epoch) return;
      this.cache.threads[id] = { ...this.cache.threads[id], runId: accepted.run_id, after: 0 };
      this.updateThread(id, (item) => ({ ...item, pending: undefined }));
      this.applySnapshot(id, snapshot); this.watch(id, accepted.run_id);
    } catch (error) {
      if (epoch !== this.epoch) return;
      if (error instanceof ApiError && !error.uncertain) {
        this.updateThread(id, (item) => ({ ...item, pending: undefined, phase: runPhase(item.run), draft: pending.retryOf ? item.draft : item.draft || pending.text,
          unsent: !pending.retryOf && item.draft ? pending.text : item.unsent, syncError: errorText(error) }));
        const busyId = error.details.run_id;
        if (error.code === 'thread_busy' && typeof busyId === 'string') this.watch(id, busyId);
      } else this.updateThread(id, (item) => ({ ...item, phase: 'sending', syncError: '发送结果尚未确认，正在使用原请求标识恢复；不会重复新增输入。' }));
    } finally { if (epoch === this.epoch) this.running.delete(id); }
  }
  async send(id: string, retry = false) {
    const thread = this.value.threads.find((item) => item.id === id);
    if (!thread || this.running.has(id) || thread.pending || isBusy(thread) || (retry ? thread.phase !== 'error' : !thread.draft.trim())) return;
    if (this.service.mode === 'service') {
      if (retry && (!thread.run || !['failed', 'interrupted'].includes(thread.run.status))) return;
      if (!retry && thread.draft.length > 50000) { this.updateThread(id, (item) => ({ ...item, syncError: '输入最多 50000 个字符。' })); return; }
      this.updateThread(id, (item) => ({ ...item, draft: retry ? item.draft : '', pending: { requestId: crypto.randomUUID(), text: retry ? '' : item.draft, ...(retry ? { retryOf: item.run!.id } : {}) }, phase: 'sending', error: undefined }));
      await this.submitPending(id); return;
    }
    this.running.add(id); const epoch = this.epoch; const requestId = crypto.randomUUID();
    if (!retry) this.updateThread(id, (item) => ({ ...item, draft: '', phase: 'replying', error: undefined, messages: [...item.messages, { id: requestId, role: 'user', text: item.draft, createdAt: new Date().toISOString() }] }));
    else this.updateThread(id, (item) => ({ ...item, phase: 'replying', error: undefined }));
    let committed = false;
    try {
      await this.service.run(this.value.threads.find((item) => item.id === id)!, requestId, (event) => {
        if (epoch !== this.epoch) return;
        if (event.type === 'phase') this.updateThread(id, (item) => ({ ...item, phase: event.phase }));
        else { committed = true; this.updateThread(id, (item) => ({ ...item, unread: true, phase: 'idle', messages: item.messages.some((message) => message.id === event.message.id) ? item.messages : [...item.messages, event.message] })); }
      });
      if (!committed) throw new Error('未收到正式回复，请重试。');
      if (epoch === this.epoch) this.updateThread(id, (item) => ({ ...item, phase: 'idle' }));
    } catch (error) { if (epoch === this.epoch) this.updateThread(id, (item) => ({ ...item, phase: committed ? 'idle' : 'error', error: errorText(error) })); }
    finally { if (epoch === this.epoch) this.running.delete(id); }
  }
  saveSpeech(messageId: string, request: SpeechRequest) { this.publish({ speech: { ...this.value.speech, [messageId]: request } }); }
}
function browserStorage() { try { return window.localStorage; } catch { return undefined; } }
const storage = typeof window !== 'undefined' ? browserStorage() : undefined;
let selected: ChatService | HttpService = desktop ? new HttpService() : demoService;
let configured = false;
try {
  const raw = storage?.getItem(CONNECTION_KEY);
  if (raw) { const config = z.object({ mode: z.enum(['service', 'demo']), baseUrl: z.string() }).parse(JSON.parse(raw)); selected = config.mode === 'service' ? new HttpService(config.baseUrl) : demoService; configured = true; }
} catch { /* Invalid connection preferences use the platform default, never service data as demo. */ }
export const workspaceStore = new WorkspaceStore(selected, storage);
export async function initializeWorkspace() {
  if (desktop && !configured) {
    try { const status = await getLocalServiceStatus(); await workspaceStore.switchMode('service', status.base_url); return; } catch { /* The HTTP status and retry remain available. */ }
  }
  await workspaceStore.initialize();
}
if (typeof window !== 'undefined') window.addEventListener('beforeunload', () => workspaceStore.dispose());
export function useWorkspace() { return useSyncExternalStore(workspaceStore.subscribe, workspaceStore.getSnapshot); }
