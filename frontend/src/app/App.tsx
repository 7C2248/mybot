import { useCallback, useEffect, useState } from 'react';
import { flushSync } from 'react-dom';
import { BookOpen, Focus, Menu, MessageSquare, Orbit, PanelRight, PanelsTopLeft, Plus, Settings, Users, X, MoreHorizontal, FolderOpen } from 'lucide-react';
import { initializeWorkspace, useWorkspace, workspaceStore } from './store';
import { isBusy, type MemoryPolicy } from '../shared/types';
import { Avatar, Button, Dialog, Empty, IconButton } from '../shared/ui';
import { ChatPage } from '../features/chat/ChatPage';
import { CharactersPage } from '../features/characters/CharactersPage';
import { MemoryPage } from '../features/memory/MemoryPage';
import { ConversationManager, MemoryPolicyFields } from '../features/conversations/ConversationManager';
import { SettingsPage } from '../features/settings/SettingsPage';
import { CompactChat } from '../features/compact-chat/CompactChat';
import { desktop, enterCompact, leaveCompact } from '../shared/platform/window';

type Page = 'chat' | 'characters' | 'memory' | 'settings' | 'conversations';
const navigation = [{ id: 'chat', label: '对话', icon: MessageSquare }, { id: 'characters', label: '角色', icon: Users }, { id: 'memory', label: '记忆', icon: BookOpen }, { id: 'settings', label: '设置', icon: Settings }, { id: 'conversations', label: '会话管理', icon: FolderOpen }] as const;
export function App() {
  const workspace = useWorkspace();
  const { threads, activeThreadId, characters, preferences } = workspace;
  const thread = threads.find((item) => item.id === activeThreadId);
  const [page, setPage] = useState<Page>('chat'), [focus, setFocus] = useState(false), [compact, setCompact] = useState(false), [switching, setSwitching] = useState(false);
  const [sidebarOpen, showSidebar] = useState(false), [newCharacter, setNewCharacter] = useState<string | null>(null), [title, setTitle] = useState('');
  const [memoryId, setMemoryId] = useState<string>(), [platformError, setPlatformError] = useState('');
  const [managedId, setManagedId] = useState<string>();
  const [managedTab, setManagedTab] = useState<'active' | 'legacy'>('active');
  const [memoryPolicy, setMemoryPolicy] = useState<MemoryPolicy>({ retrieval: false, storage: false });
  const [creating, setCreating] = useState(false), [createError, setCreateError] = useState('');
  const [narrow, setNarrow] = useState(window.innerWidth < 1180), [drawerOpen, setDrawerOpen] = useState(false);
  const inspectorVisible = narrow ? drawerOpen : preferences.inspector;
  const reportError = useCallback((error: unknown) => setPlatformError(error instanceof Error ? error.message : String(error)), []);
  useEffect(() => { void initializeWorkspace(); }, []);
  useEffect(() => { const query = matchMedia('(max-width: 1179px)'); const update = () => { setNarrow(query.matches); setDrawerOpen(false); }; query.addEventListener('change', update); return () => query.removeEventListener('change', update); }, []);
  useEffect(() => {
    document.documentElement.dataset.theme = preferences.theme;
    document.documentElement.dataset.accent = preferences.accent;
    document.documentElement.dataset.density = preferences.density;
  }, [preferences.theme, preferences.accent, preferences.density]);
  useEffect(() => { document.documentElement.dataset.compact = String(compact); }, [compact]);
  const navigate = (next: Page) => { setPage(next); setFocus(false); showSidebar(false); if (next === 'memory') setMemoryId(undefined); };
  async function toggleCompact(value: boolean) {
    if (switching) return; setSwitching(true); setPlatformError('');
    try {
      if (value) { await enterCompact(preferences); setCompact(true); }
      else {
        // Save compact dimensions and disconnect its ResizeObserver before the
        // OS window grows, so workbench dimensions never overwrite the float.
        flushSync(() => { setCompact(false); setPage('chat'); });
        await leaveCompact();
      }
    } catch (error) { reportError(error); }
    finally { setSwitching(false); }
  }
  function startNew(id = thread?.characterId ?? characters[0]?.id ?? '') { setNewCharacter(id); setTitle(''); setCreateError(''); setMemoryPolicy({ retrieval: false, storage: false }); }
  return <>
    {(workspace.storageWarning || workspace.resourceError || platformError) && <div className="global-notice" role="alert">{workspace.storageWarning || workspace.resourceError || `窗口操作失败：${platformError}`}{platformError && <button onClick={() => setPlatformError('')}>关闭提示</button>}</div>}
    {compact && thread ? <CompactChat thread={thread} leave={() => void toggleCompact(false)} onError={reportError} /> : <div className={`app-shell ${focus ? 'focus-reading' : ''}`}>
      <header className="titlebar"><div className="brand"><Orbit size={19} />mybot</div><span className="environment-label">{desktop ? '桌面版' : '浏览器'}<i />{workspace.mode === 'demo' ? '演示模式' : workspace.connection === 'connected' ? '本机服务' : '服务连接中断'}</span></header>
      <div className="shell-body">
        {sidebarOpen && <button className="sidebar-scrim" aria-label="关闭会话导航" onClick={() => showSidebar(false)} />}
        <aside className={`sidebar ${sidebarOpen ? 'open' : ''}`} aria-label="导航与会话">
          <nav>{navigation.map(({ id, label, icon: Icon }) => <button key={id} aria-current={page === id ? 'page' : undefined} onClick={() => navigate(id)}><Icon size={17} />{label}</button>)}</nav>
          <div className="session-section"><div className="section-label">最近会话</div><Button className="new-chat" onClick={() => startNew()}><Plus size={16} />新建对话</Button><div className="sessions">{threads.map((item) => <div className="session-row" key={item.id}><button className="session" aria-pressed={activeThreadId === item.id} onClick={() => { workspaceStore.selectThread(item.id); navigate('chat'); }}><span>{item.title}{item.unread && <i className="unread-dot" aria-label="有未读回复" />}</span><small>{item.characterId}{isBusy(item) ? ' · 处理中' : workspace.mode === 'demo' ? ' · 本地预览' : ''}</small></button>{workspace.mode === 'service' && <IconButton label={`${item.title} 的会话设置`} onClick={() => { setManagedId(item.id); setManagedTab('active'); navigate('conversations'); }}><MoreHorizontal size={14} /></IconButton>}</div>)}</div>{workspace.threadsCursor && <Button disabled={workspace.threadsLoading} onClick={() => void workspaceStore.loadThreads().catch(reportError)}>更多会话</Button>}</div>
          <div className="sidebar-footer"><span className="connection-dot" />{workspace.mode === 'service' ? '正式历史保存在服务端' : '本地会话自动保存'}<small>{workspace.mode === 'service' ? workspace.baseUrl : '独立演示数据'}</small></div>
        </aside>
        <main className="main-content" key={`${workspace.mode}:${workspace.baseUrl}`}>
          {workspace.mode === 'service' && (workspace.connectionError || workspace.ready?.database !== 'connected') && <div className="service-notice" role="status">{workspace.connectionError || (workspace.loading ? '正在连接服务…' : '数据库尚未就绪，可在设置中查看连接状态。')}<Button onClick={() => void workspaceStore.refresh()}>重新连接</Button></div>}
          {workspace.mode === 'service' && (workspace.legacy.length > 0 || workspace.legacyError) && <div className="service-notice" role="status">{workspace.legacyError ? 'CLI 历史暂时无法读取，可在会话管理中重试。' : `发现 ${workspace.legacy.length} 个待关联或正在导入的 CLI 会话。`}<Button onClick={() => { setManagedId(undefined); setManagedTab('legacy'); navigate('conversations'); }}>管理 CLI 历史</Button></div>}
          <div className="toolbar"><div className="toolbar-title"><IconButton label="会话导航" onClick={() => showSidebar(!sidebarOpen)}><Menu size={18} /></IconButton><div><h1>{page === 'chat' ? thread?.title ?? '对话' : navigation.find((item) => item.id === page)?.label}</h1><p>{page === 'chat' ? `${thread?.characterId ?? '请选择角色'} · 角色对话` : { characters: '档案与资源', memory: '原文预览与详情', settings: '外观、立绘与悬浮输入', conversations: '会话设置、回收站与 CLI 历史' }[page]}</p></div></div>
            {page === 'chat' && thread && <div className="actions"><Button aria-pressed={focus} onClick={() => setFocus(!focus)}><Focus size={15} /><span>{focus ? '退出专注' : '专注阅读'}</span></Button><Button disabled={switching} onClick={() => void toggleCompact(true)}><PanelsTopLeft size={15} /><span>简化模式</span></Button><Button aria-label="切换情境面板" aria-pressed={inspectorVisible} onClick={() => { setFocus(false); if (narrow) setDrawerOpen(!drawerOpen); else workspaceStore.setPreferences({ inspector: !preferences.inspector }); }}><PanelRight size={15} /></Button></div>}
          </div>
          {page === 'chat' && (thread ? <ChatPage thread={thread} focus={focus} inspectorVisible={inspectorVisible} openMemory={(id) => { setMemoryId(id); setPage('memory'); }} /> : <Empty title="开始一段新的对话"><Button className="primary" onClick={() => startNew()}>选择角色</Button></Empty>)}
          {page === 'characters' && <CharactersPage create={startNew} />}
          {page === 'memory' && <MemoryPage key={`${thread?.characterId}-${memoryId}`} initialCharacter={thread?.characterId ?? characters[0]?.id ?? ''} initialMemory={memoryId} />}
          {page === 'conversations' && <ConversationManager key={`${managedId}-${managedTab}`} initialId={managedId} initialTab={managedTab} />}
          {page === 'settings' && <SettingsPage currentCharacter={thread?.characterId ?? ''} enterCompact={() => void toggleCompact(true)} />}
        </main>
      </div>
    </div>}
    {newCharacter !== null && <Dialog title="新建对话" close={() => { if (!creating) setNewCharacter(null); }}><form onSubmit={(event) => { event.preventDefault(); if (!newCharacter || creating) return; setCreating(true); setCreateError(''); void workspaceStore.createThread(newCharacter, title, memoryPolicy).then(() => { setNewCharacter(null); navigate('chat'); }).catch((error) => setCreateError(error instanceof Error ? error.message : '创建失败，请刷新会话列表确认。')).finally(() => setCreating(false)); }}>
      <p className="hint">{workspace.mode === 'service' ? '本会话的记忆检索与存储由下方开关控制。' : '此模式使用独立的演示会话。'}</p>{createError && <p role="alert">{createError}</p>}<label className="stacked-label">角色<select aria-label="新会话角色" value={newCharacter} disabled={creating} onChange={(event) => setNewCharacter(event.target.value)}><option value="" disabled>请选择角色</option>{characters.map((item) => <option key={item.id}>{item.id}</option>)}</select></label>
      <label className="stacked-label">会话标题 <span>可选</span><input aria-label="会话标题" autoFocus value={title} maxLength={80} placeholder="新的对话" onChange={(event) => setTitle(event.target.value)} /></label>
      {workspace.mode === 'service' && <MemoryPolicyFields value={memoryPolicy} onChange={setMemoryPolicy} disabled={creating} />}
      {newCharacter && <div className="new-chat-summary"><Avatar name={newCharacter} /><span>{newCharacter}</span></div>}
      <div className="dialog-actions"><Button disabled={creating} onClick={() => setNewCharacter(null)}><X size={14} />取消</Button><button className="button primary" type="submit" disabled={creating || !characters.some((item) => item.id === newCharacter)}>{creating ? '正在创建…' : '开始对话'}</button></div>
    </form></Dialog>}
  </>;
}
