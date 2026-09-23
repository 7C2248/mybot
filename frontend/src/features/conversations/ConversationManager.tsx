import { useEffect, useRef, useState } from 'react';
import { useWorkspace, workspaceStore } from '../../app/store';
import { ApiError, errorText } from '../../shared/api/http';
import { terminal, type CheckpointSyncResult, type LegacyThread } from '../../shared/api/contracts';
import type { MemoryPolicy, Thread } from '../../shared/types';
import { Button, Dialog, Empty } from '../../shared/ui';

export function MemoryPolicyFields({ value, onChange, disabled = false }: { value: MemoryPolicy; onChange: (value: MemoryPolicy) => void; disabled?: boolean }) {
  return <fieldset className="memory-policy" disabled={disabled}>
    <legend>本会话的长期记忆</legend>
    <label><input type="checkbox" checked={value.retrieval} onChange={(event) => onChange({ ...value, retrieval: event.target.checked })} />检索长期记忆</label>
    <label><input type="checkbox" checked={value.storage} onChange={(event) => onChange({ ...value, storage: event.target.checked })} />将本会话内容存入长期记忆</label>
    <p className="hint">关闭后仍保存聊天历史。重新开启存储只处理之后的新输入；只存不读时仅新增记忆。</p>
  </fieldset>;
}

function ConversationEditor({ initial, saved }: { initial: Thread; saved: () => void }) {
  const api = workspaceStore.service;
  const { threads } = useWorkspace();
  const [thread, setThread] = useState(initial), [title, setTitle] = useState(initial.title);
  const [policy, setPolicy] = useState(initial.memoryPolicy ?? { retrieval: false, storage: false });
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [sync, setSync] = useState<CheckpointSyncResult>(), [syncBusy, setSyncBusy] = useState(false);
  const [syncOpen, setSyncOpen] = useState(false), [syncError, setSyncError] = useState('');
  const current = threads.find((item) => item.id === thread.id) ?? thread;
  const active = !!current.pending || !!current.run && !terminal(current.run);
  async function reload() {
    if (api.mode !== 'service') return;
    setBusy(true); setError('');
    try { const value = await api.thread(thread.id); setThread(value); setTitle(value.title); setPolicy(value.memoryPolicy!); setNotice('已读取最新设置。'); }
    catch (error) { setError(errorText(error)); } finally { setBusy(false); }
  }
  async function save() {
    setBusy(true); setError(''); setNotice('');
    try {
      const value = await workspaceStore.updateConversation(thread.id, thread.version!, title, policy);
      if (value) { setThread(value); setNotice('已保存，新的输入使用此记忆策略。'); saved(); }
    } catch (error) { setError(errorText(error)); } finally { setBusy(false); }
  }
  async function previewSync() {
    setSyncOpen(true); setSync(undefined); setSyncBusy(true); setSyncError(''); setError(''); setNotice('');
    try {
      const preview = await workspaceStore.previewCheckpointSync(thread.id);
      setSync(preview);
    } catch (error) { setSyncError(errorText(error)); } finally { setSyncBusy(false); }
  }
  async function applySync() {
    if (!sync) return;
    setSyncBusy(true); setSyncError(''); setNotice('');
    try {
      const applied = await workspaceStore.applyCheckpointSync(thread.id, sync);
      setSync(undefined); setSyncOpen(false);
      // A re-preview must not authorize saving stale title/policy edits after
      // another client's settings change. Keep their original edit version.
      if (applied) setThread((old) => old.version === sync.version ? { ...old, version: applied.version, state: applied.state } : old);
      setNotice(`已按检查点截断 ${applied?.delete_count ?? 0} 条消息。`);
      saved();
    } catch (error) {
      setSync(undefined);
      setSyncError(`${errorText(error)}${error instanceof ApiError && error.status === 409 ? ' 请重新预览后再确认。' : ' 请重新预览以确认当前历史。'}`);
    } finally { setSyncBusy(false); }
  }
  return <section className="conversation-editor" aria-label="会话设置">
    <h2>{thread.characterId} · 会话设置</h2>
    <label className="stacked-label">标题<input aria-label="修改会话标题" maxLength={200} value={title} disabled={busy} onChange={(event) => setTitle(event.target.value)} /></label>
    <MemoryPolicyFields value={policy} onChange={setPolicy} disabled={busy} />
    <p className="hint">关闭存储会撤销尚未完成的记忆任务，已经写入的角色记忆保留。</p>
    <div className="actions"><Button disabled={busy || !title.trim()} onClick={() => void save()}>保存会话设置</Button><Button disabled={busy} onClick={() => void reload()}>放弃编辑并重读</Button></div>
    <section className="checkpoint-sync" aria-label="重新加载检查点">
      <h3>重新加载检查点</h3>
      <p className="hint">读取最新检查点并与当前历史比对；检查点中不存在的较新消息会被永久删除，直到最新消息与检查点一致。已经写入的长期记忆不会回滚。</p>
      <div className="actions"><Button disabled={busy || syncBusy || active} onClick={() => void previewSync()}>{syncBusy ? '正在读取…' : '预览检查点差异'}</Button></div>
      {active && <p className="hint">会话仍在执行，结束后才能重新加载。</p>}
    </section>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {syncOpen && <Dialog title="重新加载检查点" close={() => { if (!syncBusy) setSyncOpen(false); }}>
      {syncError && <p role="alert">{syncError}</p>}
      {syncBusy && !sync && <p role="status">正在读取检查点差异…</p>}
      {sync && <><p>检查点最新消息：{sync.latest_message_text || '（没有可确认的消息）'}</p>
      {sync.delete_count > 0 ? <>
        <p>将删除 <strong>{sync.delete_count}</strong> 条消息（{sync.run_count} 次运行），此操作无法撤销。</p>
        <div className="legacy-preview">{sync.delete_preview.map((message) => <article key={message.id}><strong>{message.role === 'user' ? '你' : '角色'}</strong><div className="message-text">{message.text}</div></article>)}</div>
        {sync.preview_truncated && <p className="hint">仅显示最前面的 20 条。</p>}
        <p className="hint">已经写入的角色长期记忆不会回滚；对应语音资源会排队清理。</p>
      </> : <p>当前消息历史已与检查点一致，无需删除。</p>}</>}
      <div className="dialog-actions"><Button disabled={syncBusy} onClick={() => setSyncOpen(false)}>取消</Button>
        {!sync && <Button disabled={syncBusy || active} onClick={() => void previewSync()}>重新预览</Button>}
        <Button disabled={syncBusy || active || !sync?.delete_count} onClick={() => void applySync()}>确认截断</Button></div>
    </Dialog>}
  </section>;
}

const importErrors: Record<string, string> = {
  legacy_interrupted: '旧 CLI 有未完成节点，请先核对该会话后再导入。',
  legacy_changed: '旧 CLI 历史正在变化，请停止旧 CLI 后重试。',
  legacy_empty: '没有可确认的消息可导入。', legacy_unreadable: '检查点无法读取，请检查服务版本和旧历史格式。',
};
function LegacyRow({ item, changed }: { item: LegacyThread; changed: () => void }) {
  const { characters } = useWorkspace(), api = workspaceStore.service;
  const [character, setCharacter] = useState(item.character_id ?? ''), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [preview, setPreview] = useState<{ items: { role: string; text: string }[]; notice: string }>();
  const running = item.status === 'queued' || item.status === 'running';
  async function start() {
    if (api.mode !== 'service') return;
    setBusy(true); setError('');
    try { await api.importLegacy(item.source_id, character, item.title ?? 'CLI 历史'); changed(); }
    catch (error) { setError(errorText(error)); } finally { setBusy(false); }
  }
  return <section className="legacy-row">
    <strong>{item.title ?? item.source_id}</strong>
    <div className="actions"><select aria-label={`${item.source_id} 的关联角色`} value={character} disabled={busy || running || !!item.character_id} onChange={(event) => setCharacter(event.target.value)}><option value="">请选择所属角色</option>{characters.map((value) => <option key={value.id} value={value.id}>{value.name}</option>)}</select>
      <Button disabled={busy} onClick={() => { if (api.mode !== 'service') return; setError(''); void api.legacyPreview(item.source_id).then(setPreview).catch((error) => setError(errorText(error))); }}>预览历史</Button>
      <Button disabled={busy || running || !character} onClick={() => void start()}>{running ? '正在导入…' : item.status === 'failed' ? '重试导入' : '关联并导入'}</Button></div>
    <small>{item.character_id ? '已确定角色，导入后出现在会话列表。' : '旧检查点未记录明确角色，请关联后继续对话。'}</small>
    {(error || item.error_code) && <p role="alert">{error || importErrors[item.error_code!] || '导入未完成，请重试。'}</p>}
    {preview && <Dialog title="CLI 历史预览" close={() => setPreview(undefined)}><p className="hint">{preview.notice}</p><div className="legacy-preview">{preview.items.map((message, index) => <article key={index}><strong>{message.role === 'user' ? '你' : '角色'}</strong><div className="message-text">{message.text}</div></article>)}</div></Dialog>}
  </section>;
}

export function ConversationManager({ initialId, initialTab = 'active' }: { initialId?: string; initialTab?: 'active' | 'legacy' }) {
  const workspace = useWorkspace(), api = workspaceStore.service;
  const [tab, setTab] = useState<'active' | 'trash' | 'legacy'>(initialTab);
  const [items, setItems] = useState<Thread[]>([]), [legacy, setLegacy] = useState<LegacyThread[]>([]);
  const [cursor, setCursor] = useState<string | null>(), [selected, setSelected] = useState<Thread>();
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [purging, setPurging] = useState<Thread>();
  const request = useRef(0);
  async function load(more = false) {
    if (api.mode !== 'service') return;
    const ticket = ++request.current;
    setBusy(true); setError('');
    try {
      if (tab === 'legacy') {
        const page = await api.legacyThreads(more ? cursor ?? undefined : undefined);
        if (request.current !== ticket) return;
        setLegacy((old) => more ? [...old, ...page.items] : page.items); setCursor(page.next_cursor);
      } else {
        const page = await api.threads(more ? cursor ?? undefined : undefined, undefined, tab === 'trash');
        if (request.current !== ticket) return;
        setItems((old) => more ? [...old, ...page.items] : page.items); setCursor(page.next_cursor);
      }
    } catch (error) { if (request.current === ticket) setError(errorText(error)); } finally { if (request.current === ticket) setBusy(false); }
  }
  useEffect(() => { setSelected(undefined); setCursor(null); setItems([]); setLegacy([]); void load(); return () => { request.current++; }; }, [api, tab]);
  useEffect(() => {
    if (!initialId || api.mode !== 'service') return;
    let cancelled = false;
    void api.thread(initialId).then((value) => { if (!cancelled) setSelected(value); }).catch((error) => { if (!cancelled) setError(errorText(error)); });
    return () => { cancelled = true; };
  }, [api, initialId]);
  useEffect(() => {
    if (tab !== 'legacy') return;
    const timer = setInterval(() => { if (!busy && !cursor) void load(); }, 2000);
    return () => clearInterval(timer);
  }, [tab, busy, cursor]);
  async function change(thread: Thread, action: 'delete' | 'restore' | 'purge') {
    setBusy(true); setError(''); setNotice('');
    try {
      await workspaceStore.manageThread(thread.id, thread.version!, action);
      setSelected(undefined); setPurging(undefined);
      setNotice(action === 'delete' ? '会话已移入回收站。角色长期记忆保留。' : action === 'restore' ? '会话已恢复。' : '会话已永久删除。');
      await load();
    } catch (error) { setError(errorText(error)); } finally { setBusy(false); }
  }
  if (workspace.mode !== 'service') return <Empty title="会话管理用于本机服务">请在连接设置中切换到本机服务。</Empty>;
  return <div className="page conversation-manager">
    <div className="tabs">{(['active', 'trash', 'legacy'] as const).map((value, index) => <button key={value} disabled={busy} aria-pressed={tab === value} onClick={() => { setTab(value); setNotice(''); }}>{['会话列表', '回收站', 'CLI 历史'][index]}</button>)}<Button disabled={busy} onClick={() => void load()}>刷新列表</Button></div>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {tab === 'legacy' ? <><p className="hint">已确定角色的旧 CLI 会话会自动导入。不能确定角色的会话可先预览再关联；导入不会执行旧任务或存储记忆。</p>{legacy.map((item) => <LegacyRow key={item.source_id} item={item} changed={() => { void load(); void workspaceStore.refresh(); }} />)}{!legacy.length && !busy && <p className="hint">没有待关联或正在导入的 CLI 历史。</p>}</> : <>
      {tab === 'trash' && <p className="hint">回收站中的会话可以恢复。永久删除会清理该会话的历史、运行和语音资源，角色共享的长期记忆保留。</p>}
      <div className="managed-list">{items.map((thread) => <div key={thread.id} className="managed-row"><button className="managed-title" disabled={tab === 'trash'} onClick={() => { setSelected(thread); setNotice(''); }}><strong>{thread.title}</strong><small>{thread.characterId} · {thread.source === 'legacy' ? '旧 CLI' : thread.source === 'cli' ? 'CLI' : '桌面'} · 检索{thread.memoryPolicy?.retrieval ? '开' : '关'} / 存储{thread.memoryPolicy?.storage ? '开' : '关'}</small></button><div className="actions">{tab === 'trash' ? <><Button disabled={busy} onClick={() => void change(thread, 'restore')}>恢复</Button><Button disabled={busy} onClick={() => setPurging(thread)}>永久删除</Button></> : <Button disabled={busy} aria-label={`删除会话 ${thread.title}`} onClick={() => void change(thread, 'delete')}>移入回收站</Button>}</div></div>)}</div>
      {!items.length && !busy && <p className="hint">{tab === 'trash' ? '回收站为空。' : '暂无会话。'}</p>}
      {selected && tab === 'active' && <ConversationEditor key={selected.id} initial={selected} saved={() => void load()} />}
    </>}
    {cursor && <Button disabled={busy} onClick={() => void load(true)}>加载更多</Button>}
    {purging && <Dialog title="永久删除会话" close={() => { if (!busy) setPurging(undefined); }}><p>永久删除“{purging.title}”及其聊天历史、运行记录和音频？此操作无法恢复。</p>{error && <p role="alert">{error}</p>}<div className="dialog-actions"><Button disabled={busy} onClick={() => setPurging(undefined)}>取消</Button><Button disabled={busy} onClick={() => void change(purging, 'purge')}>确认永久删除</Button></div></Dialog>}
  </div>;
}
