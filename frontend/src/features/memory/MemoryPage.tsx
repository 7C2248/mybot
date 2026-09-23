import { useEffect, useState } from 'react';
import { ChevronRight, Search } from 'lucide-react';
import { useWorkspace, workspaceStore } from '../../app/store';
import { errorText } from '../../shared/api/http';
import type { Memory } from '../../shared/types';
import { Button, Dialog, Empty } from '../../shared/ui';

export function MemoryPage({ initialCharacter, initialMemory }: { initialCharacter: string; initialMemory?: string }) {
  const { characters, mode } = useWorkspace(), api = workspaceStore.service;
  const [character, setCharacter] = useState(initialCharacter), [query, setQuery] = useState(''), [search, setSearch] = useState('');
  const [page, setPage] = useState(0), [cursors, setCursors] = useState<(string | undefined)[]>([undefined]), [next, setNext] = useState<string | null>(null);
  const [memories, setMemories] = useState<Memory[]>([]), [selected, setSelected] = useState<string | undefined>(initialMemory), [detail, setDetail] = useState<Memory>();
  const [error, setError] = useState(''), [detailError, setDetailError] = useState(''), [loading, setLoading] = useState(false), [refresh, setRefresh] = useState(0);
  useEffect(() => { const timer = setTimeout(() => { setSearch(query); setPage(0); setCursors([undefined]); }, 250); return () => clearTimeout(timer); }, [query]);
  const cursor = cursors[page];
  useEffect(() => {
    const controller = new AbortController(); setError(''); setMemories([]); setLoading(true);
    void (async () => {
      try {
        if (!character) return;
        if (api.mode === 'service') {
          const result = await api.memories(character, search, cursor, controller.signal);
          if (!controller.signal.aborted) { setMemories(result.items); setNext(result.next_cursor); }
        } else {
          const all = (await api.memories(character)).filter((memory) => `${memory.id} ${memory.memory} ${memory.keywords.join(' ')}`.toLocaleLowerCase().includes(search.toLocaleLowerCase()));
          if (!controller.signal.aborted) { setMemories(all.slice(page * 10, (page + 1) * 10)); setNext(all.length > (page + 1) * 10 ? String(page + 1) : null); }
        }
      } catch (error) { if (!controller.signal.aborted) setError(errorText(error)); }
      finally { if (!controller.signal.aborted) setLoading(false); }
    })();
    return () => controller.abort();
  }, [api, character, search, page, cursor, refresh]);
  useEffect(() => {
    const controller = new AbortController(); setDetail(undefined); setDetailError('');
    if (selected && character) void (async () => {
      try {
        const memory = api.mode === 'service' ? await api.memory(character, selected, controller.signal) : (await api.memories(character)).find((item) => item.id === selected);
        if (!controller.signal.aborted) { setDetail(memory); if (!memory) setDetailError('该记忆不存在。'); }
      } catch (error) { if (!controller.signal.aborted) setDetailError(errorText(error)); }
    })();
    return () => controller.abort();
  }, [api, selected, character, refresh]);
  return <div className="page memory-page">
    <div className="page-intro"><p>长期记忆</p><span>{mode === 'demo' ? '演示记录' : '角色记忆库'} · 显示原文，不生成摘要</span></div>
    <div className="memory-filters"><label>角色<select aria-label="记忆所属角色" value={character} onChange={(event) => { setCharacter(event.target.value); setPage(0); setCursors([undefined]); setSelected(undefined); }}>{[...new Set([initialCharacter, ...characters.map((item) => item.id)])].filter(Boolean).map((id) => <option key={id}>{id}</option>)}</select></label>
      <label className="search-field"><Search size={16} /><input aria-label="搜索记忆原文" maxLength={1000} placeholder={mode === 'service' ? '搜索记忆原文' : '搜索原文、编号或关键词'} value={query} onChange={(event) => setQuery(event.target.value)} /></label><Button onClick={() => setRefresh((value) => value + 1)}>刷新记忆</Button></div>
    <p className="hint">{character} · 当前页 {memories.length} 条 · 新建对话继续共享该角色记忆</p>
    {loading ? <p role="status">正在读取记忆…</p> : error ? <p role="alert">{error}<Button onClick={() => setRefresh((value) => value + 1)}>重试读取</Button></p> : memories.length ? <div className="memory-list">{memories.map((memory) => <button className="memory-row" key={memory.id} onClick={() => setSelected(memory.id)}><div><h3>记忆 #{memory.id}</h3><p className="text-clamp">{memory.memory}</p><small>{memory.event_date ?? '日期未记录'} · 重要程度 {memory.importance ?? '未记录'}{memory.keywords.length ? ` · ${memory.keywords.join(' / ')}` : ''}</small></div><ChevronRight size={18} /></button>)}</div> : <Empty title={search ? '没有匹配的记忆' : '暂无记忆'}>{search ? '试试其他原文关键词。' : '该角色尚未记录长期记忆。'}</Empty>}
    {(page > 0 || next) && <div className="pagination"><Button disabled={loading || page === 0} onClick={() => setPage(page - 1)}>上一页</Button><span>第 {page + 1} 页</span><Button disabled={loading || !next} onClick={() => { setCursors([...cursors.slice(0, page + 1), next ?? undefined]); setPage(page + 1); }}>下一页</Button></div>}
    {selected && <Dialog title={`记忆 #${selected}`} close={() => setSelected(undefined)}>{detail ? <><p className="hint">{character} · 完整原文</p><div className="memory-original">{detail.memory}</div><dl className="memory-metadata"><dt>事件日期</dt><dd>{detail.event_date ?? '未记录'}</dd><dt>重要程度</dt><dd>{detail.importance ?? '未记录'}</dd><dt>关键词</dt><dd>{detail.keywords.join('、') || '未记录'}</dd><dt>更新时间</dt><dd>{detail.update_time ?? '未记录'}</dd></dl></> : <p role={detailError ? 'alert' : 'status'}>{detailError || '正在读取完整原文…'}</p>}</Dialog>}
  </div>;
}
