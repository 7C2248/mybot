import { describe, expect, it } from 'vitest';
import { initialWorkspace, restoreWorkspace, WorkspaceStore, STORAGE_KEY } from './store';
import type { ChatService } from '../shared/types';

const service: ChatService = { mode: 'demo', characters: async () => [], memories: async () => [], run: async () => {} };
describe('conversation state', () => {
  it('preserves exact input, rejects double sends, and commits to the captured thread after switching', async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    let calls = 0;
    const store = new WorkspaceStore({ ...service, run: async (_thread, _request, emit) => { calls++; await gate; emit({ type: 'message.committed', message: { id: 'reply-1', role: 'assistant', text: '一\n\n二', createdAt: 'now' } }); } });
    const id = store.getSnapshot().activeThreadId;
    const originalCount = store.getSnapshot().threads[0].messages.length;
    store.setDraft(id, '  第一行\n\n  第二行  ');
    const run = store.send(id); await store.send(id);
    store.setDraft(id, '下一条草稿'); store.createThread('XiaoCe', '另一段对话');
    release(); await run;
    expect(calls).toBe(1);
    const thread = store.getSnapshot().threads.find((item) => item.id === id)!;
    expect(thread.messages).toHaveLength(originalCount + 2);
    expect(thread.messages.at(-2)?.text).toBe('  第一行\n\n  第二行  ');
    expect(thread.draft).toBe('下一条草稿'); expect(thread.unread).toBe(true);
    expect(store.getSnapshot().threads[0].messages).toHaveLength(0);
  });
  it('retries a failed run without duplicating the user message', async () => {
    let calls = 0;
    const store = new WorkspaceStore({ ...service, run: async (_thread, _request, emit) => { if (++calls === 1) throw Error('offline'); emit({ type: 'message.committed', message: { id: 'reply', role: 'assistant', text: 'ok', createdAt: 'now' } }); } });
    const id = store.getSnapshot().activeThreadId;
    store.setDraft(id, '你好'); await store.send(id); expect(store.getSnapshot().threads[0].phase).toBe('error');
    await store.send(id, true);
    expect(store.getSnapshot().threads[0].messages.filter((message) => message.text === '你好')).toHaveLength(1);
    expect(store.getSnapshot().threads[0].phase).toBe('idle');
  });
  it('does not invite generation retry after a reply was already committed', async () => {
    const store = new WorkspaceStore({ ...service, run: async (_thread, _request, emit) => { emit({ type: 'message.committed', message: { id: 'reply', role: 'assistant', text: 'ok', createdAt: 'now' } }); throw Error('secondary task failed'); } });
    store.setDraft('demo-suli', '你好'); await store.send('demo-suli');
    expect(store.getSnapshot().threads[0].phase).toBe('idle');
    expect(store.getSnapshot().threads[0].messages.at(-1)?.text).toBe('ok');
  });
  it('recovers an interrupted preview run as retryable while retaining its input', () => {
    const value = initialWorkspace(); value.threads[0].phase = 'reviewing'; value.threads[0].draft = '保留草稿';
    const restored = restoreWorkspace(JSON.stringify(value));
    expect(restored.threads[0].phase).toBe('error'); expect(restored.threads[0].draft).toBe('保留草稿');
  });
  it('never overwrites invalid saved data', () => {
    const data = new Map([[STORAGE_KEY, '{broken']]);
    const store = new WorkspaceStore(service, { getItem: (key) => data.get(key) ?? null, setItem: (key, value) => { data.set(key, value); } });
    store.setDraft('demo-suli', '临时草稿');
    expect(data.get(STORAGE_KEY)).toBe('{broken'); expect(store.getSnapshot().storageWarning).toBeTruthy();
  });
});
