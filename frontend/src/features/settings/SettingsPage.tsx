import { useEffect, useState } from 'react';
import { Image, Monitor, PanelsTopLeft, Plug } from 'lucide-react';
import { useWorkspace, workspaceStore } from '../../app/store';
import { spriteSchema, type Preferences, type SpriteSettings } from '../../shared/types';
import { Button, Field } from '../../shared/ui';
import { desktop } from '../../shared/platform/window';
import { getLocalServiceStatus, startLocalService, type LocalServiceStatus } from '../../shared/platform/service';
import { ServiceSettingsPanel } from './ServiceSettings';

const tabs = [{ id: 'appearance', label: '外观', icon: Monitor }, { id: 'sprites', label: '立绘显示', icon: Image }, { id: 'compact', label: '简化模式', icon: PanelsTopLeft }, { id: 'services', label: '连接与语音', icon: Plug }];
export function SettingsPage({ currentCharacter, enterCompact }: { currentCharacter: string; enterCompact: () => void }) {
  const { preferences, characters } = useWorkspace();
  const [tab, setTab] = useState('appearance'), [selected, select] = useState(currentCharacter);
  const [service, setService] = useState<LocalServiceStatus>();
  useEffect(() => {
    if (!desktop) return;
    let active = true;
    const refresh = () => getLocalServiceStatus().then((status) => { if (active) setService(status); }).catch(() => {
      if (active) setService({ status: 'failed', base_url: '', managed: false, error: '无法读取本机服务状态。' });
    });
    void refresh();
    const timer = window.setInterval(refresh, 2000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);
  const character = characters.find((item) => item.id === selected) ?? characters[0];
  const sprite = preferences.sprites[character?.id ?? ''] ?? spriteSchema.parse({});
  const asset = character?.assets.find((item) => item.id === sprite.assetId);
  function update(patch: Partial<Preferences>) { workspaceStore.setPreferences(patch); }
  function updateSprite(patch: Partial<SpriteSettings>) { if (character) update({ sprites: { ...preferences.sprites, [character.id]: { ...sprite, ...patch } } }); }
  return <div className="page settings-page">
    <div className="page-intro"><p>偏好设置</p><span>自动保存在当前设备</span></div>
    <div className="tabs settings-tabs">{tabs.map(({ id, label, icon: Icon }) => <button key={id} aria-pressed={tab === id} onClick={() => setTab(id)}><Icon size={15} />{label}</button>)}</div>
    {tab === 'appearance' && <>
      <Field label="主题"><select aria-label="主题" value={preferences.theme} onChange={(event) => update({ theme: event.target.value as Preferences['theme'] })}><option value="system">跟随系统</option><option value="dark">深蓝灰</option><option value="light">浅色</option></select></Field>
      <Field label="强调色"><div className="color-choices">{(['violet', 'rose'] as const).map((accent) => <button key={accent} className={`color-choice ${accent}`} aria-pressed={preferences.accent === accent} onClick={() => update({ accent })}><span />{accent === 'violet' ? '柔紫' : '玫瑰'}</button>)}</div></Field>
      <Field label="阅读密度"><select aria-label="阅读密度" value={preferences.density} onChange={(event) => update({ density: event.target.value as Preferences['density'] })}><option value="comfortable">舒适</option><option value="compact">紧凑</option></select></Field>
      <Field label="情境面板"><label className="checkbox-label"><input type="checkbox" checked={preferences.inspector} onChange={(event) => update({ inspector: event.target.checked })} />默认展开世界、角色与记忆信息</label></Field>
    </>}
    {tab === 'sprites' && <>
      <Field label="角色"><select aria-label="立绘所属角色" value={character?.id ?? ''} onChange={(event) => select(event.target.value)}>{characters.map((item) => <option key={item.id}>{item.id}</option>)}</select></Field>
      <Field label="工作台立绘" hint="简化模式保持单个输入横条，不显示立绘。"><label className="checkbox-label"><input type="checkbox" checked={sprite.enabled} disabled={!character?.assets.length} onChange={(event) => updateSprite({ enabled: event.target.checked })} />在对话工作台显示</label></Field>
      <Field label="图片素材" hint={character?.assets.length ? '选择明确的图片素材后显示。' : '此角色没有图片资源。'}><select aria-label="立绘图片" value={sprite.assetId} disabled={!character?.assets.length} onChange={(event) => updateSprite({ assetId: event.target.value })}><option value="">未选择</option>{character?.assets.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>
      <Field label="位置"><select aria-label="立绘位置" value={sprite.side} onChange={(event) => updateSprite({ side: event.target.value as SpriteSettings['side'] })}><option value="right">靠右 · 底部对齐</option><option value="left">靠左 · 底部对齐</option></select></Field>
      <Field label="缩放"><label className="range-label"><output>{sprite.scale}%</output><input type="range" aria-label="立绘缩放" min="50" max="150" step="5" value={sprite.scale} onChange={(event) => updateSprite({ scale: Number(event.target.value) })} /></label></Field>
      <Field label="镜像"><label className="checkbox-label"><input type="checkbox" checked={sprite.mirror} onChange={(event) => updateSprite({ mirror: event.target.checked })} />水平翻转</label></Field>
      {asset && <div className="sprite-preview"><img src={asset.url} alt="当前立绘预览" style={{ height: `${sprite.scale * 2.2}px`, transform: sprite.mirror ? 'scaleX(-1)' : undefined }} /><p>{asset.name}</p></div>}
    </>}
    {tab === 'compact' && <>
      <Field label="消息气泡位置"><select aria-label="简化模式回复位置" value={preferences.replyPlacement} onChange={(event) => update({ replyPlacement: event.target.value as Preferences['replyPlacement'] })}><option value="bubble">输入条上方</option><option value="inline">输入条内上沿</option></select></Field>
      <Field label="窗口尺寸" hint="拖拽左右边沿调宽，上下边沿调高，四角同时调整。消息区最低保留约一行。"><output>宽 {Math.round(preferences.compactWidth)}px · 消息区高 {Math.round(preferences.historyHeight)}px</output></Field>
      <Field label="窗口置顶" hint={desktop ? '进入简化模式时生效。' : '此偏好用于桌面版；浏览器预览无法置顶到其他应用上方。'}><label className="checkbox-label"><input type="checkbox" checked={preferences.alwaysOnTop} onChange={(event) => update({ alwaysOnTop: event.target.checked })} />显示在其他窗口上方</label></Field>
      <Field label="失焦时" hint="后台回复仅显示未读提示；再次聚焦时恢复消息和阅读位置。">隐藏消息与历史，保留输入草稿</Field>
      <Field label="输入格式">Enter 发送 · Shift+Enter 换行<br /><span className="hint">保留换行、空行和缩进</span></Field>
      <Button className="primary section-action" disabled={!currentCharacter} onClick={enterCompact}>进入简化模式</Button>
    </>}
    {tab === 'services' && <>
      {desktop && <Field label="本机 API" hint={service?.error ?? (service?.managed ? '随桌面程序启动，退出时清理本次启动的服务。' : '检测到已有服务时直接复用。')}>
        {service?.status === 'connected' ? `已连接 · ${service.base_url}` : service?.status === 'failed' ? '连接失败' : '正在启动…'}
        {service?.status === 'failed' && <Button onClick={() => { void startLocalService().then(getLocalServiceStatus).then(setService).catch(() => setService({ ...service, error: '重试启动失败。' })); }}>重试启动服务</Button>}
      </Field>}
      <ServiceSettingsPanel />
      <Field label="语音播放">服务模式中可为正式角色回复生成语音，并在消息下方播放。</Field>
    </>}
  </div>;
}
