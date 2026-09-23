import { useEffect, useState } from 'react';
import { Volume2 } from 'lucide-react';
import { useWorkspace, workspaceStore } from '../../app/store';
import { errorText } from '../../shared/api/http';
import { Button } from '../../shared/ui';

export function SpeechPlayer({ messageId }: { messageId: string }) {
  const { speech, ready } = useWorkspace();
  const request = speech[messageId], api = workspaceStore.service;
  const [playError, setPlayError] = useState('');
  const [reconnect, setReconnect] = useState(0);
  useEffect(() => {
    if (api.mode !== 'service' || !request) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const current = () => !controller.signal.aborted && workspaceStore.service === api && workspaceStore.getSnapshot().speech[messageId]?.requestId === request.requestId;
    void (async () => {
      try {
        let job = request.job ? await api.speechJob(request.job.id, controller.signal) : await api.speech(messageId, request.requestId);
        while (current()) {
          workspaceStore.saveSpeech(messageId, { requestId: request.requestId, job });
          if (!['queued', 'running'].includes(job.status)) return;
          await new Promise<void>((resolve) => { timer = setTimeout(resolve, 1500); controller.signal.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true }); });
          if (!current()) return;
          job = await api.speechJob(job.id, controller.signal);
        }
      } catch (error) { if (current()) workspaceStore.saveSpeech(messageId, { ...workspaceStore.getSnapshot().speech[messageId], error: errorText(error) }); }
    })();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [api, messageId, request?.requestId, reconnect]);
  if (api.mode !== 'service') return null;
  const failed = request?.job && ['failed', 'interrupted'].includes(request.job.status);
  const busy = !!request && !request.error && (!request.job || ['queued', 'running'].includes(request.job.status));
  let source: string | undefined;
  try { if (request?.job?.resource_url) source = api.resource(request.job.resource_url); } catch { /* Invalid resource stays unavailable. */ }
  function start() {
    setPlayError('');
    // A lost response reuses its original request key; a known failed synthesis gets a new one.
    if (request?.error && !failed && !playError) {
      workspaceStore.saveSpeech(messageId, { ...request, error: undefined });
      setReconnect((value) => value + 1);
    } else workspaceStore.saveSpeech(messageId, { requestId: crypto.randomUUID() });
  }
  return <div className="speech-player">
    {source && !playError ? <audio controls preload="none" src={source} aria-label="角色回复语音" onError={() => setPlayError('音频资源读取失败，可重新生成。')} /> : <Button disabled={busy || !ready?.capabilities.speech} onClick={start}><Volume2 size={13} />{busy ? '正在生成语音…' : request?.error ? '恢复语音任务' : failed || playError ? '重试语音' : '生成语音'}</Button>}
    {(request?.error || failed || playError) && <small role="status">{playError || request?.error || '语音生成未完成，可单独重试。'}</small>}
  </div>;
}
