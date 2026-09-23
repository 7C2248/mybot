import { useState } from 'react';
import { Check, Image, MessageSquarePlus, Pencil, Save } from 'lucide-react';
import { useWorkspace, workspaceStore } from '../../app/store';
import { spriteSchema } from '../../shared/types';
import { Avatar, Button, Empty } from '../../shared/ui';

export function CharactersPage({ create }: { create: (id: string) => void }) {
  const { characters, loading, profileOverrides, preferences, mode } = useWorkspace();
  const [selectedId, select] = useState(characters[0]?.id ?? ''), [tab, setTab] = useState('profile');
  const [language, setLanguage] = useState('zh'), [editing, setEditing] = useState(false), [draft, setDraft] = useState(''), [saved, setSaved] = useState(false);
  const [version, setVersion] = useState<string>(), [error, setError] = useState(''), [busy, setBusy] = useState(false), [latest, setLatest] = useState<string>();
  const character = characters.find((item) => item.id === selectedId) ?? characters[0];
  if (!character) return <Empty title={loading ? '正在读取角色…' : '暂无角色档案'}>将角色档案放入 Character 目录后，重新准备本地资源。</Empty>;
  const activeLanguage = character.profiles[language] !== undefined ? language : Object.keys(character.profiles)[0];
  const profile = profileOverrides[character.id]?.[activeLanguage] ?? character.profiles[activeLanguage] ?? '';
  const sprite = preferences.sprites[character.id] ?? spriteSchema.parse({});
  return <div className="page character-page">
    <div className="page-intro"><p>角色档案与素材</p><span>来自本地 Character 目录</span></div>
    <div className="character-picker">{characters.map((item) => <button className="character-option" key={item.id} aria-pressed={item.id === character.id} disabled={editing} onClick={() => { select(item.id); setSaved(false); }}><Avatar name={item.name} /><span>{item.name}<small>{item.assets.length ? `${item.assets.length} 张图片` : '暂无图片'}</small></span></button>)}</div>
    <div className="page-heading"><h2>{character.name}</h2><Button className="primary" onClick={() => create(character.id)}><MessageSquarePlus size={16} />开始对话</Button></div>
    <div className="tabs"><button aria-pressed={tab === 'profile'} onClick={() => setTab('profile')}>角色档案</button><button aria-pressed={tab === 'assets'} disabled={editing} onClick={() => setTab('assets')}>图片资源</button></div>
    {tab === 'profile' ? <>
      <div className="profile-toolbar"><label>语言<select aria-label="档案语言" value={activeLanguage} disabled={editing} onChange={(event) => { setLanguage(event.target.value); setSaved(false); }}>{Object.keys(character.profiles).map((lang) => <option key={lang} value={lang}>{lang === 'zh' ? '中文' : 'English'}</option>)}</select></label>
        {editing ? <div className="actions"><Button disabled={busy} onClick={() => { setEditing(false); setError(''); }}>取消</Button><Button disabled={busy || !draft.trim()} className="primary" onClick={() => { setBusy(true); setError(''); void workspaceStore.saveProfile(character.id, activeLanguage, draft, version).then(() => { setEditing(false); setSaved(true); }).catch((error) => setError(error instanceof Error ? error.message : '保存失败，草稿已保留。')).finally(() => setBusy(false)); }}><Save size={14} />{busy ? '正在保存…' : mode === 'service' ? '写回角色档案' : '保存本地草稿'}</Button></div>
          : <Button onClick={() => { setDraft(profile); setVersion(character.version); setLatest(undefined); setError(''); setEditing(true); setSaved(false); }}><Pencil size={14} />{mode === 'service' ? '编辑档案' : '编辑草稿'}</Button>}
      </div>
      {(saved || profileOverrides[character.id]?.[activeLanguage] !== undefined) && <p className="notice"><Check size={14} />{mode === 'service' ? '档案已写回，下一个任务使用新版本。' : '已保存本地档案草稿，原始 Markdown 文件保持原样。'}</p>}
      {error && <p role="alert">{error}{mode === 'service' && <Button disabled={busy} onClick={() => { setBusy(true); void workspaceStore.reloadCharacter(character.id).then((value) => { if (value) { setLatest(value.profiles[activeLanguage]); setVersion(value.version); setError('已读取最新版本，编辑草稿仍保留。请对照后再保存。'); } }).catch((error) => setError(error.message)).finally(() => setBusy(false)); }}>读取最新版本</Button>}</p>}
      {editing ? <textarea className="profile-editor" aria-label="角色档案草稿" value={draft} onChange={(event) => setDraft(event.target.value)} /> : <div className="profile-document">{profile}</div>}
      {editing && latest !== undefined && <details open><summary>服务器上的最新档案（供对照）</summary><div className="profile-document">{latest}</div></details>}
    </> : <div className="asset-grid">{character.assets.length ? character.assets.map((asset) => <button key={asset.id} className="asset-option" aria-pressed={sprite.assetId === asset.id} onClick={() => workspaceStore.setPreferences({ sprites: { ...preferences.sprites, [character.id]: { ...sprite, assetId: asset.id } } })}>
      <div className="asset-image"><img src={asset.url} alt={asset.name} loading="lazy" /></div><span>{sprite.assetId === asset.id ? <Check size={14} /> : <Image size={14} />}{asset.name}</span><small>{sprite.assetId === asset.id ? '已选为立绘素材' : '选择为立绘素材'}</small>
    </button>) : <Empty title="暂无图片资源">缺少图片时以姓名缩写显示头像。</Empty>}</div>}
  </div>;
}
