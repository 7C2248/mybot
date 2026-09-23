import { useLayoutEffect, useRef, type ReactNode } from 'react';
import { ArrowUp, LoaderCircle } from 'lucide-react';
import { useWorkspace, workspaceStore } from '../../app/store';
import { isBusy, type Thread } from '../../shared/types';
import { Avatar } from '../../shared/ui';

export function Composer({ thread, compact = false, expanded = true, children, prefix, onSend }: {
  thread: Thread; compact?: boolean; expanded?: boolean; children?: ReactNode; prefix?: ReactNode; onSend?: () => void;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const { mode, connection, ready } = useWorkspace();
  const busy = isBusy(thread) || !!thread.pending;
  const unavailable = mode === 'service' && (connection !== 'connected' || !ready?.capabilities.chat);
  useLayoutEffect(() => {
    const input = ref.current!;
    input.style.height = compact ? '36px' : '60px';
    if (!compact || expanded) input.style.height = `${Math.min(compact ? 120 : 180, input.scrollHeight)}px`;
    input.style.overflowY = !compact || expanded ? 'auto' : 'hidden';
  }, [thread.draft, compact, expanded]);
  function submit() {
    if (busy || unavailable || !thread.draft.trim()) return;
    onSend?.(); void workspaceStore.send(thread.id);
    if (!compact || expanded) ref.current?.focus();
  }
  return <form className={compact ? 'compact-composer' : 'composer'} onSubmit={(event) => { event.preventDefault(); submit(); }}>
    {prefix}{compact && <Avatar name={thread.characterId} />}
    <textarea ref={ref} rows={compact ? 1 : 2} maxLength={50000} aria-label="消息输入" placeholder={`对 ${thread.characterId} 说些什么…`} value={thread.draft}
      onChange={(event) => workspaceStore.setDraft(thread.id, event.target.value)}
      onKeyDown={(event) => {
        if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); submit(); }
      }} />
    {!compact && <span className="compose-hint">Enter 发送 · Shift+Enter 换行</span>}
    <button className="send-button" type="submit" aria-label={busy ? '正在回复' : '发送消息'} disabled={busy || unavailable || !thread.draft.trim()} title={unavailable ? '对话服务尚未就绪，草稿已保留' : undefined}>
      {busy ? <LoaderCircle className="spin" size={18} /> : <ArrowUp size={18} />}{!compact && <span>发送</span>}
    </button>{children}
  </form>;
}
