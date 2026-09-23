import { useEffect, useState } from 'react';
import { useWorkspace, workspaceStore } from '../../app/store';
import { errorText } from '../../shared/api/http';
import type { ModelSettings, NodeModel, SettingsStatus } from '../../shared/api/contracts';
import { Button, Field } from '../../shared/ui';

const labels: Record<string, string> = { main: '角色回复', participant_state: '角色与用户状态', memory_query: '记忆查询', memory_summary: '记忆整理', chunking: '记忆分块', tts: '语音指令' };
export function ServiceSettingsPanel() {
  const workspace = useWorkspace(), api = workspaceStore.service;
  const [mode, setMode] = useState(workspace.mode), [address, setAddress] = useState(workspace.baseUrl || 'http://127.0.0.1:8765');
  const [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [settings, setSettings] = useState<SettingsStatus>(), [models, setModels] = useState<ModelSettings>();
  const [nodes, setNodes] = useState<Record<string, NodeModel>>({}), [notice, setNotice] = useState('');
  const dirty = !!models && JSON.stringify(nodes) !== JSON.stringify(models.nodes);
  async function readModels() {
    if (api.mode !== 'service') return;
    setError(''); setBusy(true);
    try { const [status, value] = await Promise.all([api.settings(), api.models()]); if (workspaceStore.service === api) { setSettings(status); setModels(value); setNodes(value.nodes); } }
    catch (error) { if (workspaceStore.service === api) setError(errorText(error)); }
    finally { setBusy(false); }
  }
  useEffect(() => { void readModels(); }, [api]);
  async function save() {
    if (api.mode !== 'service' || !models) return;
    setBusy(true); setError(''); setNotice('');
    try { const value = await api.saveModels(models.saved_version, nodes); setModels(value); setNodes(value.nodes); setNotice('已保存到本机。点击“应用已保存设置”后供下一任务使用。'); }
    catch (error) { setError(errorText(error)); } finally { setBusy(false); }
  }
  async function apply() {
    if (api.mode !== 'service' || !models) return;
    setBusy(true); setError(''); setNotice('');
    try { const value = await api.applyModels(models.saved_version); setModels(value); setNotice('已应用，下一个任务使用新设置。'); await workspaceStore.refresh(); }
    catch (error) { setError(errorText(error)); } finally { setBusy(false); }
  }
  return <>
    <Field label="数据来源" hint="演示会话和服务会话独立保存，切换不会上传演示消息。"><select aria-label="数据来源" value={mode} disabled={busy} onChange={(event) => setMode(event.target.value as typeof mode)}><option value="service">本机服务</option><option value="demo">演示模式</option></select></Field>
    {mode === 'service' && <Field label="服务地址"><input aria-label="服务地址" value={address} disabled={busy} onChange={(event) => setAddress(event.target.value)} /></Field>}
    <Button disabled={busy} onClick={() => { setBusy(true); setError(''); void workspaceStore.switchMode(mode, address).catch((error) => setError(errorText(error))).finally(() => setBusy(false)); }}>{mode === 'service' ? '连接本机服务' : '切换到演示'}</Button>
    {workspace.mode === 'service' && <>
      <Field label="连接状态">{workspace.connection === 'connected' ? '已连接' : workspace.connection === 'connecting' ? '正在连接' : '连接失败'}<span className="hint"> {workspace.connectionError}</span></Field>
      <Field label="数据库">{workspace.ready?.database === 'connected' ? '已连接' : workspace.ready?.database === 'not_configured' ? '未配置' : '不可用'}</Field>
      <Field label="对话与语音">{workspace.ready?.capabilities.chat ? '运行服务已就绪' : '运行服务尚未就绪'}<p className="hint">真实任务会验证模型与语音资源的可用性。</p></Field>
      <div className="page-heading"><h2>模型设置</h2><Button disabled={busy} onClick={() => void readModels()}>{dirty ? '放弃编辑并重读' : '重新读取'}</Button></div>
      {models && <>
        <p className="hint">{models.pending_changes ? '磁盘设置尚未应用' : '已保存设置正在生效'} · {models.effective_from === 'next_run' ? '从下一任务生效' : ''}</p>
        <div className="model-grid">{Object.entries(nodes).filter(([name]) => labels[name]).map(([name, node]) => <fieldset key={name} disabled={busy} className="model-node"><legend>{labels[name]}</legend>
          <label>服务商<select aria-label={`${labels[name]}服务商`} value={node.provider} onChange={(event) => setNodes({ ...nodes, [name]: { ...node, provider: event.target.value as NodeModel['provider'] } })}><option value="deepseek">DeepSeek</option><option value="moonshot">Moonshot</option><option value="llama_cpp">本地模型</option></select></label>
          <label>模型<input aria-label={`${labels[name]}模型`} value={node.model} maxLength={200} onChange={(event) => setNodes({ ...nodes, [name]: { ...node, model: event.target.value } })} /></label>
          <label>思考模式<select aria-label={`${labels[name]}思考模式`} value={node.thinking} onChange={(event) => setNodes({ ...nodes, [name]: { ...node, thinking: event.target.value as NodeModel['thinking'] } })}><option value="enabled">开启</option><option value="disabled">关闭</option></select></label>
          <label>思考强度<select aria-label={`${labels[name]}思考强度`} value={node.reasoning_effort} onChange={(event) => setNodes({ ...nodes, [name]: { ...node, reasoning_effort: event.target.value as NodeModel['reasoning_effort'] } })}>{['none', 'low', 'medium', 'high', 'max'].map((value, i) => <option key={value} value={value}>{['无', '低', '中', '高', '最高'][i]}</option>)}</select></label>
          {settings?.models[name] && <small>{settings.models[name].credentials_configured === false ? '尚未配置凭据' : settings.models[name].local_model_available === false ? '本地模型文件缺失' : '凭据和路径在本机配置'}</small>}
        </fieldset>)}</div>
        <div className="actions"><Button disabled={busy || !dirty || Object.values(nodes).some((node) => !node.model.trim())} onClick={() => void save()}>保存模型设置</Button><Button disabled={busy || dirty || !models.pending_changes} onClick={() => void apply()}>应用已保存设置</Button></div>
      </>}
    </>}
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
  </>;
}
