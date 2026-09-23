import { useEffect, useState } from 'react';
import { CloudSun, MapPin, UserRound, BookOpen, RefreshCw, Check, LoaderCircle } from 'lucide-react';
import { useWorkspace, workspaceStore } from '../../app/store';
import { isBusy, phaseLabels, type Thread } from '../../shared/types';
import { demoMemories } from '../../shared/api/demo';
import { Button } from '../../shared/ui';
import { MessageList } from './MessageList';
import { Composer } from './Composer';

export function RunStatus({ thread }: { thread: Thread }) {
  const { mode } = useWorkspace();
  const busy = isBusy(thread) || !!thread.pending;
  const memoryLabels = { pending: '后台记忆：任务已提交', running: '后台记忆：正在整理', failed: '后台记忆：整理失败，任务已保留', completed: '后台记忆：上次整理已完成', idle: '', disabled: '' };
  return <div className={`run-status ${thread.error ? 'has-error' : ''}`} role="status">
    {busy ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />}
    <span>{thread.syncError || (busy ? phaseLabels[thread.pending && !isBusy(thread) ? 'sending' : thread.phase] : thread.error || (thread.messages.length ? `本轮已完成${mode === 'demo' ? ' · 演示回复' : ''}` : '等待你的第一条消息'))}</span>
    {mode === 'service' && thread.memoryPolicy?.storage && (thread.memoryStatusError || thread.memoryStatus) && <small className="background-memory-status">{thread.memoryStatusError ? '后台记忆：状态暂不可用' : memoryLabels[thread.memoryStatus!.status]}</small>}
    {(thread.syncError || thread.pending) && mode === 'service' && <Button onClick={() => void workspaceStore.recover(thread.id)}>恢复同步</Button>}
    {thread.unsent && <details className="pending-input"><summary>有未发送的文字</summary><div className="message-text">{thread.unsent}</div><Button disabled={!!thread.draft} onClick={() => workspaceStore.restoreUnsent(thread.id)}>{thread.draft ? '请先处理输入框中的草稿' : '恢复到输入框'}</Button></details>}
    {thread.phase === 'error' && !thread.pending && (mode === 'demo' || ['failed', 'interrupted'].includes(thread.run?.status ?? '')) && <Button onClick={() => void workspaceStore.send(thread.id, true)}><RefreshCw size={13} />重试</Button>}
  </div>;
}
export function ChatPage({ thread, focus, inspectorVisible, openMemory }: { thread: Thread; focus: boolean; inspectorVisible: boolean; openMemory: (id: string) => void }) {
  const { preferences, characters, mode } = useWorkspace();
  const [tab, setTab] = useState<'state' | 'memory'>('state');
  const [followToken, follow] = useState(0);
  const sprite = preferences.sprites[thread.characterId];
  const character = characters.find((item) => item.id === thread.characterId);
  const image = sprite?.enabled && character?.assets.find((asset) => asset.id === sprite.assetId);
  useEffect(() => { workspaceStore.markRead(thread.id); }, [thread.id, thread.unread]);
  const hasExampleState = mode === 'demo' && thread.id === 'demo-suli';
  const state = thread.state;
  const display = (value: string | null | undefined) => value || '未记录';
  const rows = (value: typeof state extends undefined ? never : NonNullable<typeof state>['character_state'], hearing = false) => ([['位置', display(value?.location)], ['情绪', display(value?.mood)], ['身体', display(value?.body)], ['穿着', display(value?.clothing)], ...(hearing ? [['听觉', display(value?.hearing)]] : [])]);
  return <div className={`chat-page ${focus || !inspectorVisible ? 'without-inspector' : ''}`}>
    <div className="conversation">
      <div className="conversation-meta">{thread.messages.length ? '当前会话' : '新对话'}<span>{mode === 'demo' ? '预览模式 · 对白为演示内容' : '本机服务 · 正式消息已持久化'}</span></div>
      {mode === 'service' && <div className="conversation-policy">长期记忆：检索{thread.memoryPolicy?.retrieval ? '开启' : '关闭'} · 存储{thread.memoryPolicy?.storage ? '开启' : '关闭'}</div>}
      {thread.historyNotice && <p className="hint history-notice">{thread.historyNotice}</p>}
      <div className={`conversation-body ${image ? `with-sprite sprite-${sprite.side}` : ''}`}>
        {image && <div className="sprite-stage" aria-label={`${thread.characterId} 立绘`}><img src={image.url} alt={`${thread.characterId} · ${image.name}`} style={{ height: `${sprite.scale}%`, transform: sprite.mirror ? 'scaleX(-1)' : undefined }} /></div>}
        <MessageList key={thread.id} thread={thread} followToken={followToken} />
      </div>
      {thread.pending && <details className="pending-input"><summary>正在确认发送的输入</summary><div className="message-text">{thread.pending.text || '正在重试上一轮输入'}</div></details>}
      <RunStatus thread={thread} /><Composer thread={thread} onSend={() => follow((value) => value + 1)} />
    </div>
    {!focus && inspectorVisible && <aside className="inspector" aria-label="情境面板">
      <div className="tabs"><button aria-pressed={tab === 'state'} onClick={() => setTab('state')}>状态</button><button aria-pressed={tab === 'memory'} onClick={() => setTab('memory')}>记忆</button></div>
      {tab === 'state' ? mode === 'service' ? <>
        <StateBlock icon={<CloudSun size={16} />} title="世界" rows={[["日期", display(state?.world_state?.time?.date)], ["星期", display(state?.world_state?.time?.weekday)], ["时段", display(state?.world_state?.time?.period)], ["天气", display(state?.world_state?.weather)]]} />
        <StateBlock icon={<MapPin size={16} />} title={thread.characterId} rows={rows(state?.character_state, true)} />
        <StateBlock icon={<UserRound size={16} />} title="你" rows={rows(state?.user_state)} />
      </> : hasExampleState ? <>
        <StateBlock icon={<CloudSun size={16} />} title="世界" rows={[['日期', '2026-09-12 · 周六'], ['时段', '傍晚'], ['天气', '雨后，微凉']]} />
        <StateBlock icon={<MapPin size={16} />} title={thread.characterId} rows={[['位置', '家中 · 窗边'], ['情绪', '平静，期待出门'], ['身体', '状态良好'], ['穿着', '便服、外套'], ['听觉', '同一房间']]} />
        <StateBlock icon={<UserRound size={16} />} title="你" rows={[['位置', '家中'], ['情绪', '放松'], ['身体', '未记录'], ['穿着', '未记录']]} />
        <p className="hint">示例情境，尚未连接 Agent 状态。</p>
      </> : <p className="hint">当前会话尚未记录情境。</p> : <>
        <p className="hint">{mode === 'service' ? '本轮实际检索的记忆原文' : '检索结果展示示例。当前未执行真实记忆检索。'}</p>
        {(mode === 'service' ? state?.retrieved_memories ?? [] : hasExampleState ? demoMemories.slice(0, 2) : []).map((memory) => <button key={memory.id} className="retrieved-memory" onClick={() => openMemory(memory.id)}><span><BookOpen size={13} />记忆 #{memory.id}</span><p className="text-clamp">{memory.memory}</p></button>)}
        {mode === 'service' && !state?.retrieved_memories?.length && <p className="hint">暂无本轮检索记录。</p>}
      </>}
    </aside>}
  </div>;
}
function StateBlock({ icon, title, rows }: { icon: React.ReactNode; title: string; rows: string[][] }) {
  return <section className="state-block"><h3>{icon}{title}</h3><dl>{rows.map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl></section>;
}
