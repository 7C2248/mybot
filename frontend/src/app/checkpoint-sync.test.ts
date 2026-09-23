import { afterEach, expect, it, vi } from 'vitest';
import { WorkspaceStore, serviceStorageKey } from './store';
import { ApiError, HttpService } from '../shared/api/http';
import type { CheckpointSyncResult, RunSnapshot } from '../shared/api/contracts';
import type { Thread, Message } from '../shared/types';

const stores: WorkspaceStore[] = [];
afterEach(() => stores.splice(0).forEach(store => store.dispose()));
const keep: Message = { id: 'keep', role: 'assistant', text: 'kept', sequence: 2, createdAt: 'now' };
const drop: Message = { id: 'drop', role: 'assistant', text: 'removed', sequence: 4, createdAt: 'now' };
const preview: CheckpointSyncResult = { dry_run: true, checkpoint_id: 'checkpoint', latest_message_id: 'reply_keep', latest_message_text: 'kept', matched_message_id: 'keep', delete_count: 1, delete_preview: [], preview_truncated: false, run_count: 1, version: 1, state: {} };
function setup(run?: RunSnapshot) {
  const api = new HttpService();
  const remote: Thread = { id: 'thread', characterId: 'Alpha', title: 'review', createdAt: 'now', messages: [], draft: '', unread: false, phase: 'idle', version: 1, run };
  vi.spyOn(api, 'health').mockResolvedValue({ service: 'mybot', status: 'ok' });
  vi.spyOn(api, 'ready').mockResolvedValue({ status: 'ready', database: 'connected', capabilities: { characters: true, resources: true, memories: true, chat: true, profile_write: true, model_settings: true, speech: true } });
  vi.spyOn(api, 'characters').mockResolvedValue([]);
  vi.spyOn(api, 'threads').mockImplementation(async () => ({ items: [{ ...remote }], next_cursor: null }));
  vi.spyOn(api, 'thread').mockImplementation(async () => ({ ...remote }));
  vi.spyOn(api, 'legacyThreads').mockResolvedValue({ items: [], next_cursor: null });
  vi.spyOn(api, 'messages').mockResolvedValue({ items: [keep, drop], next_cursor: 'older' });
  vi.spyOn(api, 'state').mockResolvedValue({});
  vi.spyOn(api, 'memoryStatus').mockResolvedValue({ status: 'idle' });
  vi.spyOn(api, 'snapshot').mockImplementation(async () => {
    if (remote.run) return { ...remote.run, messages: [], last_event_sequence: 1 };
    throw new ApiError('run not found', 'run_not_found', 404);
  });
  vi.spyOn(api, 'syncCheckpoint').mockImplementation(async (_id, _version, dryRun) => {
    if (dryRun) return { ...preview, version: remote.version! };
    remote.version = 2;
    vi.mocked(api.messages).mockResolvedValue({ items: [keep], next_cursor: 'kept-older' });
    return { ...preview, dry_run: false, version: 2 };
  });
  const data = new Map<string, string>();
  const store = new WorkspaceStore(api, { getItem: key => data.get(key) ?? null, setItem: (key, value) => { data.set(key, value); } });
  stores.push(store);
  return { api, remote, store, cache: () => JSON.parse(data.get(serviceStorageKey(api.baseUrl))!) };
}

it('invalidates remote truncations while preserving drafts and resetting reading/pagination', async () => {
  const { api, remote, store } = setup(); await store.initialize();
  store.setDraft('thread', 'next draft');
  store.setReading('thread', { top: 10, following: false, count: 2, anchorId: 'drop', lastId: 'drop' });
  remote.version = 2;
  vi.mocked(api.messages).mockResolvedValue({ items: [keep], next_cursor: 'kept-older' });
  await store.refresh();
  const thread = store.getSnapshot().threads[0];
  expect(thread.messages.map(m => m.id)).toEqual(['keep']);
  expect(thread).toMatchObject({ draft: 'next draft', unread: false, historyCursor: 'kept-older', historyLoaded: true });
  expect(store.reading.get('thread')).toEqual({ top: 0, following: true, count: 0 });
});

it('clears the old run and its persisted cursor when the sync deleted every run', async () => {
  const run: RunSnapshot = { id: 'deleted-run', thread_id: 'thread', user_message_id: 'user', client_request_id: 'key', status: 'completed', phase: 'completed', warnings: [], created_at: 'now', messages: [], last_event_sequence: 1 };
  const { api, remote, store, cache } = setup(run); await store.initialize();
  store.setDraft('thread', 'next draft'); remote.run = undefined;
  vi.mocked(api.snapshot).mockClear();
  await store.applyCheckpointSync('thread', preview);
  expect(store.getSnapshot().threads[0]).toMatchObject({ phase: 'idle', draft: 'next draft', version: 2 });
  expect(store.getSnapshot().threads[0].run).toBeUndefined();
  expect(cache().threads.thread).toMatchObject({ after: 0, draft: 'next draft' });
  expect(cache().threads.thread.runId).toBeUndefined();
  expect(api.snapshot).not.toHaveBeenCalled();
  expect(api.syncCheckpoint).toHaveBeenCalledWith('thread', 1, false, 'checkpoint');
});

it('discards a pre-sync history response even if the transport completes after cancellation', async () => {
  const { api, store } = setup(); await store.initialize();
  let resolveOld!: (value: { items: Message[]; next_cursor: string }) => void;
  vi.mocked(api.messages).mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; }));
  const oldLoad = store.loadHistory('thread', true);
  const oldSignal = vi.mocked(api.messages).mock.calls.at(-1)![2];
  await store.applyCheckpointSync('thread', preview);
  expect(oldSignal?.aborted).toBe(true);
  resolveOld({ items: [keep, drop], next_cursor: 'stale' }); await oldLoad;
  expect(store.getSnapshot().threads[0].messages.map(m => m.id)).toEqual(['keep']);
  expect(store.getSnapshot().threads[0].historyCursor).toBe('kept-older');
});

it('discards stale thread/list responses arriving after a local sync', async () => {
  const { api, remote, store } = setup(); await store.initialize();
  const stale = { ...remote };
  let resolveList!: (page: { items: Thread[]; next_cursor: null }) => void;
  vi.mocked(api.threads).mockImplementationOnce(() => new Promise(resolve => { resolveList = resolve; }));
  const poll = store.loadThreads(false);
  await store.applyCheckpointSync('thread', preview);
  resolveList({ items: [stale], next_cursor: null }); await poll;
  vi.mocked(api.thread).mockResolvedValueOnce(stale);
  await store.refresh();
  expect(store.getSnapshot().threads[0].version).toBe(2);
  expect(store.getSnapshot().threads[0].messages.map(m => m.id)).toEqual(['keep']);
});

it('re-previews with the latest version after a conflict and leaves local history intact on rejection', async () => {
  const { api, remote, store } = setup(); await store.initialize();
  vi.mocked(api.syncCheckpoint).mockRejectedValueOnce(new ApiError('changed', 'thread_conflict', 409));
  await expect(store.applyCheckpointSync('thread', preview)).rejects.toMatchObject({ code: 'thread_conflict' });
  expect(store.getSnapshot().threads[0].messages.map(m => m.id)).toEqual(['keep', 'drop']);
  remote.version = 3;
  const fresh = await store.previewCheckpointSync('thread');
  expect(fresh.version).toBe(3);
  expect(api.syncCheckpoint).toHaveBeenLastCalledWith('thread', 3, true);
});
