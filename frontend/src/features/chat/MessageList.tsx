import { useLayoutEffect, useRef, useState } from 'react';
import { ArrowDown } from 'lucide-react';
import { useWorkspace, workspaceStore } from '../../app/store';
import type { Thread } from '../../shared/types';
import { Avatar, Button } from '../../shared/ui';
import { SpeechPlayer } from './SpeechPlayer';

export function MessageList({ thread, compact = false, height, visible = true, followToken = 0 }: {
  thread: Thread; compact?: boolean; height?: number; visible?: boolean; followToken?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const { mode } = useWorkspace();
  const restoring = useRef(false);
  const viewportSize = useRef({ width: 0, height: 0 });
  const [following, setFollowing] = useState(workspaceStore.reading.get(thread.id)?.following ?? true);
  const [pending, setPending] = useState(false);
  const lastFollowToken = useRef(followToken);
  useLayoutEffect(() => {
    if (!visible || !ref.current) return;
    const view = ref.current;
    let frame = 0;
    function restore() {
      const saved = workspaceStore.reading.get(thread.id) ?? { top: 0, following: true, count: 0 };
      if (followToken !== lastFollowToken.current) { saved.following = true; lastFollowToken.current = followToken; }
      restoring.current = true;
      viewportSize.current = { width: view.clientWidth, height: view.clientHeight };
      view.scrollTop = saved.following ? view.scrollHeight : saved.top;
      const anchor = saved.anchorId && [...view.querySelectorAll<HTMLElement>('[data-message-id]')].find((element) => element.dataset.messageId === saved.anchorId);
      if (!saved.following && anchor) view.scrollTop += anchor.getBoundingClientRect().top - view.getBoundingClientRect().top - (saved.offset ?? 0);
      setFollowing(saved.following);
      setPending(!saved.following && (saved.lastId ? thread.messages.at(-1)?.id !== saved.lastId : thread.messages.length > saved.count));
      workspaceStore.setReading(thread.id, { ...saved, top: view.scrollTop, following: saved.following, count: saved.following ? thread.messages.length : saved.count, lastId: saved.following ? thread.messages.at(-1)?.id : saved.lastId });
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => { restoring.current = false; });
    }
    restore();
    // Native window and multiline input resizing change the viewport without
    // changing the height prop. Keep the same reading position in both cases.
    const observer = new ResizeObserver(restore); observer.observe(view);
    view.querySelectorAll('[data-message-id]').forEach((element) => observer.observe(element));
    return () => { observer.disconnect(); cancelAnimationFrame(frame); restoring.current = false; };
  }, [thread.id, thread.messages.length, height, visible, followToken]);
  function jump() {
    const view = ref.current!; view.scrollTop = view.scrollHeight;
    workspaceStore.setReading(thread.id, { top: view.scrollTop, following: true, count: thread.messages.length, lastId: thread.messages.at(-1)?.id });
    setFollowing(true); setPending(false);
  }
  return <div className={`message-list-wrap ${compact ? 'is-compact' : ''}`}>
    <div ref={ref} className="message-list" role="region" aria-label="对话消息，可滚动阅读" style={height ? { height } : undefined}
      onScroll={(event) => {
        if (restoring.current || !visible) return;
        const view = event.currentTarget;
        if (view.clientWidth !== viewportSize.current.width || view.clientHeight !== viewportSize.current.height) return;
        const atBottom = view.scrollHeight - view.clientHeight - view.scrollTop <= 24;
        const saved = workspaceStore.reading.get(thread.id);
        const anchor = [...view.querySelectorAll<HTMLElement>('[data-message-id]')].find((element) => element.getBoundingClientRect().bottom > view.getBoundingClientRect().top);
        workspaceStore.setReading(thread.id, { top: view.scrollTop, following: atBottom, count: atBottom ? thread.messages.length : saved?.count ?? thread.messages.length,
          anchorId: anchor?.dataset.messageId, offset: anchor ? anchor.getBoundingClientRect().top - view.getBoundingClientRect().top : 0, lastId: atBottom ? thread.messages.at(-1)?.id : saved?.lastId ?? thread.messages.at(-1)?.id });
        setFollowing(atBottom); if (atBottom) setPending(false);
      }}>
      {mode === 'service' && (thread.historyCursor || !thread.historyLoaded) && <Button disabled={thread.historyLoading} onClick={() => void workspaceStore.loadHistory(thread.id, !!thread.historyCursor)}>{thread.historyLoading ? '正在读取历史…' : thread.historyCursor ? '加载更早消息' : '读取消息历史'}</Button>}
      {!thread.messages.length && !thread.historyLoading && <p className="conversation-empty">和 {thread.characterId} 开始一段新的对话。<br /><span>{mode === 'demo' ? '此会话使用演示数据。' : thread.memoryPolicy?.retrieval ? '已开启角色长期记忆检索。' : '本会话未开启长期记忆检索。'}</span></p>}
      {thread.messages.map((message) => <article key={message.id} data-message-id={message.id} className={`message ${message.role}`} aria-label={message.role === 'user' ? '你的消息' : `${thread.characterId} 的消息`}>
        {!compact && message.role === 'assistant' && <Avatar name={thread.characterId} />}
        <div className="message-content"><div className="message-author">{message.role === 'user' ? '你' : thread.characterId}<span>{mode === 'demo' ? '演示' : message.timestampEstimated ? '旧历史 · 时间为估计值' : new Date(message.createdAt).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</span></div><div className="message-text">{message.text}</div>{mode === 'service' && message.role === 'assistant' && <SpeechPlayer messageId={message.id} />}</div>
      </article>)}
    </div>
    {!following && <Button className="jump-latest" onClick={jump}><ArrowDown size={14} />{pending ? '有新消息 · 回到最新' : '回到最新'}</Button>}
  </div>;
}
