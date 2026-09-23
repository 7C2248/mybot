import { describe, expect, it, vi } from 'vitest';
import { HttpService, localBaseUrl } from './http';

describe('HTTP transport', () => {
  it('limits the endpoint and resource URLs to the selected loopback service', () => {
    expect(localBaseUrl('http://localhost:9876')).toBe('http://127.0.0.1:9876');
    for (const url of ['https://example.org', 'http://user:secret@localhost:8765', 'http://localhost:8765/private', 'http://localhost:8765/?query=x']) expect(() => localBaseUrl(url)).toThrow();
    expect(() => new HttpService().resource('/api/resources/../../secret')).toThrow();
  });
  it('sends raw text and the required client header and retains error codes', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ error: { code: 'thread_busy', message: 'busy', details: { run_id: 'run' } } }), { status: 409 }));
    const api = new HttpService('http://127.0.0.1:8765', fetcher);
    await expect(api.submit('thread', '  一\n\n二 ', 'stable')).rejects.toMatchObject({ code: 'thread_busy', details: { run_id: 'run' } });
    const init = fetcher.mock.calls[0][1]!;
    expect(init.headers).toMatchObject({ 'X-Mybot-Client': 'mybot-desktop' }); expect(JSON.parse(String(init.body)).text).toBe('  一\n\n二 ');
  });
  it('decodes fragmented UTF-8 SSE, ignores comments and repeated sequence numbers', async () => {
    const event = (sequence: number) => `id: ${sequence}\r\nevent: phase\r\ndata: ${JSON.stringify({ run_id: 'run', sequence, type: 'phase', payload: { phase: 'replying', text: '汉字' }, created_at: 'now' })}\r\n\r\n`;
    const bytes = new TextEncoder().encode(': heartbeat\n\n' + event(1) + event(2) + event(2) + event(3));
    const stream = new ReadableStream<Uint8Array>({ start(controller) { for (let i = 0; i < bytes.length; i += 3) controller.enqueue(bytes.slice(i, i + 3)); controller.close(); } });
    const api = new HttpService('http://127.0.0.1:8765', vi.fn<typeof fetch>().mockResolvedValue(new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } })));
    const received: number[] = []; await api.events('run', 1, new AbortController().signal, (value) => received.push(value.sequence));
    expect(received).toEqual([2, 3]);
  });
  it('sends checkpoint sync previews and commits with the previewed checkpoint id', async () => {
    const payload = { dry_run: true, checkpoint_id: 'cp-1', latest_message_id: 'reply_1', latest_message_text: '你好', matched_message_id: 'm1', delete_count: 0, delete_preview: [], preview_truncated: false, run_count: 0, version: 3, state: {} };
    const fetcher = vi.fn<typeof fetch>().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(payload), { status: 200 })));
    const api = new HttpService('http://127.0.0.1:8765', fetcher);
    const result = await api.syncCheckpoint('thread', 3, true);
    expect(result.version).toBe(3);
    expect(fetcher.mock.calls[0][0]).toBe('http://127.0.0.1:8765/api/threads/thread/checkpoint-sync');
    expect(JSON.parse(String(fetcher.mock.calls[0][1]!.body))).toEqual({ expected_version: 3, dry_run: true });
    await api.syncCheckpoint('thread', 3, false, 'cp-1');
    expect(JSON.parse(String(fetcher.mock.calls[1][1]!.body))).toEqual({ expected_version: 3, dry_run: false, checkpoint_id: 'cp-1' });
  });
});
