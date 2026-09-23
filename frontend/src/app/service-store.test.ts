import { afterEach, describe, expect, it, vi } from 'vitest';
import { WorkspaceStore, STORAGE_KEY, initialWorkspace, serviceStorageKey } from './store';
import { ApiError, HttpService } from '../shared/api/http';
import type { RunSnapshot, ServiceEvent } from '../shared/api/contracts';
import type { Thread } from '../shared/types';

const active: WorkspaceStore[] = [];
afterEach(() => { active.splice(0).forEach((store) => store.dispose()); });
const message = (role: 'user' | 'assistant', id: string = role, run = 'run-1') => ({ id, role, thread_id: 'thread-1', run_id: run, source: 'run' as const, timestamp_estimated: false, sequence: role === 'user' ? 1 : 2, text: role === 'user' ? '  原文\n\n缩进  ' : '正式回复', created_at: '2026-09-21T08:00:00Z' });
function snapshot(status: RunSnapshot['status'] = 'running', id = 'run-1'): RunSnapshot {
  return { id, thread_id: 'thread-1', user_message_id: 'user', client_request_id: 'key', status, phase: 'replying', warnings: [], created_at: '2026-09-21T08:00:00Z', messages: [message('user')], last_event_sequence: 1 };
}
function setup() {
  const data = new Map<string, string>([[STORAGE_KEY, JSON.stringify(initialWorkspace())]]);
  const storage = { getItem: (key: string) => data.get(key) ?? null, setItem: (key: string, value: string) => { data.set(key, value); } };
  const api = new HttpService(); let current: RunSnapshot | undefined;
  const threads: Thread[] = [{ id: 'thread-1', characterId: 'Alpha', title: '真实会话', createdAt: 'now', messages: [], draft: '', unread: false, phase: 'idle' }];
  vi.spyOn(api, 'health').mockResolvedValue({ service: 'mybot', status: 'ok' });
  vi.spyOn(api, 'ready').mockResolvedValue({ status: 'ready', database: 'connected', capabilities: { characters: true, resources: true, memories: true, chat: true, profile_write: true, model_settings: true, speech: true } });
  vi.spyOn(api, 'characters').mockResolvedValue([{ id: 'Alpha', name: 'Alpha', profiles: { zh: '档案' }, assets: [], version: 'v1' }]);
  vi.spyOn(api, 'threads').mockImplementation(async () => ({ items: threads.map((t) => ({ ...t, run: t.id === 'thread-1' ? current : undefined })), next_cursor: null }));
  vi.spyOn(api, 'thread').mockImplementation(async (id) => ({ ...threads.find((t) => t.id === id)!, run: id === 'thread-1' ? current : undefined }));
  vi.spyOn(api, 'legacyThreads').mockResolvedValue({ items: [], next_cursor: null });
  vi.spyOn(api, 'messages').mockResolvedValue({ items: [], next_cursor: null });
  vi.spyOn(api, 'state').mockResolvedValue({});
  vi.spyOn(api, 'memoryStatus').mockResolvedValue({ status: 'idle' });
  vi.spyOn(api, 'snapshot').mockImplementation(async () => structuredClone(current!));
  vi.spyOn(api, 'events').mockImplementation(async (_id, _after, signal) => new Promise((resolve) => { if (signal.aborted) resolve(); else signal.addEventListener('abort', () => resolve(), { once: true }); }));
  const calls: string[] = [];
  vi.spyOn(api, 'submit').mockImplementation(async (_id, _text, key) => { calls.push(key); current ??= snapshot(); return { run_id: current.id, status: current.status }; });
  const make = () => { const store = new WorkspaceStore(api, storage); active.push(store); return store; };
  return { api, data, make, calls, threads, get current() { return current; }, set current(value) { current = value; } };
}
describe('service workspace', () => {
  it('distinguishes sending from accepted queueing and never blocks on background memory', async () => {
    const fixture = setup(), store = fixture.make(); await store.initialize();
    vi.mocked(fixture.api.memoryStatus).mockResolvedValue({ status: 'running', job_id: 7 });
    await store.loadMemoryStatus('thread-1');
    expect(store.getSnapshot().threads[0].phase).toBe('idle');
    let accept!: () => void;
    vi.mocked(fixture.api.submit).mockImplementation(() => new Promise((resolve) => { accept = () => {
      fixture.current = { ...snapshot('queued'), phase: 'queued' }; resolve({ run_id: 'run-1', status: 'queued' });
    }; }));
    store.setDraft('thread-1', '继续对话'); const sending = store.send('thread-1');
    expect(store.getSnapshot().threads[0].phase).toBe('sending');
    accept(); await sending;
    expect(store.getSnapshot().threads[0].phase).toBe('queued');
    expect(store.getSnapshot().threads[0].memoryStatus?.status).toBe('running');
  });
  it('background status failure does not disconnect chat or overwrite its phase', async () => {
    const fixture = setup(), store = fixture.make(); await store.initialize();
    vi.mocked(fixture.api.memoryStatus).mockRejectedValue(new Error('unavailable'));
    await store.loadMemoryStatus('thread-1');
    expect(store.getSnapshot().connection).toBe('connected');
    expect(store.getSnapshot().threads[0].phase).toBe('idle');
    expect(store.getSnapshot().threads[0].memoryStatusError).toBe('unavailable');
  });
  it('scans past unknown legacy roles to automatically import later known roles', async () => {
    const fixture = setup();
    vi.mocked(fixture.api.legacyThreads).mockImplementation(async (cursor) => cursor ? {
      items: [{ source_id: 'known', character_id: 'Alpha', thread_id: null, error_code: null, status: 'unlinked', title: 'later' }], next_cursor: null,
    } : { items: [{ source_id: 'unknown', character_id: null, thread_id: null, error_code: null, status: 'unlinked' }], next_cursor: 'page-2' });
    const imported = vi.spyOn(fixture.api, 'importLegacy').mockResolvedValue({ source_id: 'known', character_id: 'Alpha', thread_id: null, error_code: null, status: 'queued' });
    const store = fixture.make(); await store.initialize();
    expect(imported).not.toHaveBeenCalled();
    await store.refresh();
    expect(imported).toHaveBeenCalledExactlyOnceWith('known', 'Alpha', 'later');
  });
  it('keeps server history separate from demo cache and persists raw pending input before posting', async () => {
    const fixture = setup(), store = fixture.make();
    await store.initialize();
    expect(store.getSnapshot().threads.map((t) => t.id)).toEqual(['thread-1']);
    const original = fixture.data.get(STORAGE_KEY);
    vi.mocked(fixture.api.submit).mockImplementation(async (_id, text, key) => {
      const cached = JSON.parse(fixture.data.get(serviceStorageKey(fixture.api.baseUrl))!);
      expect(cached.threads['thread-1'].pending).toEqual({ requestId: key, text });
      fixture.current = { ...snapshot('completed'), messages: [message('user'), message('assistant')] };
      return { run_id: 'run-1', status: 'completed' };
    });
    store.setDraft('thread-1', '  原文\n\n缩进  '); await store.send('thread-1');
    expect(fixture.api.submit).toHaveBeenCalledWith('thread-1', '  原文\n\n缩进  ', expect.any(String), expect.any(AbortSignal));
    expect(fixture.data.get(STORAGE_KEY)).toBe(original);
    const cache = JSON.parse(fixture.data.get(serviceStorageKey(fixture.api.baseUrl))!);
    expect(cache.threads['thread-1']).not.toHaveProperty('messages');
    expect(store.getSnapshot().threads[0].messages).toHaveLength(2);
  });
  it('recovers a lost submit response with the same key after reload and keeps the next draft', async () => {
    const fixture = setup(), store = fixture.make(); await store.initialize();
    let first = true; const keys: string[] = [];
    vi.mocked(fixture.api.submit).mockImplementation(async (_id, _text, key) => {
      keys.push(key); fixture.current ??= snapshot('completed');
      if (first) { first = false; throw new ApiError('response lost'); }
      return { run_id: fixture.current.id, status: fixture.current.status };
    });
    store.setDraft('thread-1', '原始输入'); await store.send('thread-1'); store.setDraft('thread-1', '下一条草稿');
    expect(store.getSnapshot().threads[0].pending).toBeDefined();
    store.dispose(); const restored = fixture.make(); await restored.initialize();
    await vi.waitFor(() => expect(restored.getSnapshot().threads[0].pending).toBeUndefined());
    expect(new Set(keys).size).toBe(1);
    expect(restored.getSnapshot().threads[0].messages.filter((m) => m.role === 'user')).toHaveLength(1);
    expect(restored.getSnapshot().threads[0].draft).toBe('下一条草稿');
  });
  it('resumes SSE, deduplicates committed messages and never retries a committed warning run', async () => {
    const fixture = setup(), store = fixture.make(); let streams = 0;
    fixture.threads.push({ ...fixture.threads[0], id: 'thread-2' });
    vi.mocked(fixture.api.events).mockImplementation(async (id, _after, _signal, emit) => {
      if (++streams === 1) throw new Error('disconnect');
      const event: ServiceEvent = { run_id: id, sequence: 2, type: 'message.committed', payload: { message: message('assistant') }, created_at: 'now' };
      emit(event); emit(event);
      fixture.current = { ...snapshot('completed_with_warnings'), warnings: ['memory_delayed'], messages: [message('user'), message('assistant')], last_event_sequence: 3 };
    });
    await store.initialize(); store.setDraft('thread-1', 'hello'); await store.send('thread-1'); store.selectThread('thread-2');
    await vi.waitFor(() => expect(store.getSnapshot().threads.find((t) => t.id === 'thread-1')?.run?.status).toBe('completed_with_warnings'), { timeout: 4000 });
    const thread = store.getSnapshot().threads.find((t) => t.id === 'thread-1')!;
    expect(thread.messages.filter((m) => m.role === 'assistant')).toHaveLength(1); expect(thread.unread).toBe(true);
    expect(thread.phase).toBe('idle'); expect(thread.error).toContain('后续处理');
    await store.send('thread-1', true); expect(fixture.api.submit).toHaveBeenCalledTimes(1);
  });
  it('discovers the last failed run without cache and explicitly retries using its existing user message', async () => {
    const fixture = setup(); fixture.current = snapshot('failed');
    const retry = vi.spyOn(fixture.api, 'retry').mockImplementation(async (id) => {
      expect(id).toBe('run-1'); fixture.current = { ...snapshot('completed', 'run-2'), retry_of: id, messages: [message('user'), message('assistant', 'reply', 'run-2')] };
      return { run_id: 'run-2', status: 'completed' };
    });
    const store = fixture.make(); await store.initialize(); await vi.waitFor(() => expect(store.getSnapshot().threads[0].phase).toBe('error'));
    await store.send('thread-1', true);
    expect(retry).toHaveBeenCalledTimes(1); expect(fixture.api.submit).not.toHaveBeenCalled();
    expect(store.getSnapshot().threads[0].messages.filter((m) => m.role === 'user')).toHaveLength(1);
  });
  it('keeps a rejected input alongside a newer draft', async () => {
    const fixture = setup(), store = fixture.make(); await store.initialize();
    let reject!: (error: Error) => void;
    vi.mocked(fixture.api.submit).mockImplementation(() => new Promise((_resolve, fail) => { reject = fail; }));
    store.setDraft('thread-1', '被拒绝的原文'); const send = store.send('thread-1'); store.setDraft('thread-1', '正在编辑下一条');
    reject(new ApiError('busy', 'thread_busy', 409)); await send;
    expect(store.getSnapshot().threads[0].unsent).toBe('被拒绝的原文'); expect(store.getSnapshot().threads[0].draft).toBe('正在编辑下一条');
  });
});
