import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from 'react';
import { GripVertical, PanelsTopLeft, X } from 'lucide-react';
import { useWorkspace, workspaceStore } from '../../app/store';
import type { Thread } from '../../shared/types';
import { Composer } from '../chat/Composer';
import { MessageList } from '../chat/MessageList';
import { RunStatus } from '../chat/ChatPage';
import { closeDesktop, desktop, dragNative, fitCompact, onNativeFocus, resizeNative } from '../../shared/platform/window';
import { clamp, resizeRect, type Direction, type Rect } from './geometry';

const directions: Direction[] = ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'];
const labels = { n: '上边沿', s: '下边沿', e: '右边沿', w: '左边沿', ne: '右上角', nw: '左上角', se: '右下角', sw: '左下角' };
interface Layout { left: number; bottom: number; width: number; history: number }
interface Drag { id: number; direction: Direction | 'move'; x: number; y: number; start: Rect; bounds: Rect; chrome: number; handle: HTMLElement }

export function CompactChat({ thread, leave, onError }: { thread: Thread; leave: () => void; onError: (error: unknown) => void }) {
  const { preferences } = useWorkspace();
  const group = useRef<HTMLDivElement>(null), stage = useRef<HTMLDivElement>(null), composer = useRef<HTMLDivElement>(null);
  const drag = useRef<Drag | null>(null), insidePointer = useRef(false), initialized = useRef(false);
  const nativeReady = useRef(false), nativeFitTicket = useRef(0);
  const [expanded, setExpanded] = useState(false), expandedRef = useRef(expanded);
  expandedRef.current = expanded;
  const [dragging, setDragging] = useState(false), [followToken, follow] = useState(0);
  const [composerHeight, setComposerHeight] = useState(64);
  const [layout, setLayout] = useState<Layout>({ left: Math.max(14, (window.innerWidth - preferences.compactWidth) / 2), bottom: 32, width: preferences.compactWidth, history: preferences.historyHeight });
  const layoutRef = useRef(layout); layoutRef.current = layout;
  const saveSize = useCallback(() => {
    const value = layoutRef.current;
    workspaceStore.setPreferences({ compactWidth: clamp(value.width, 320, 2400), historyHeight: clamp(value.history, 72, 1600) });
  }, []);
  const endDrag = useCallback(() => {
    const previous = drag.current; if (!previous) return;
    drag.current = null; setDragging(false);
    if (previous.id >= 0 && previous.handle.hasPointerCapture(previous.id)) previous.handle.releasePointerCapture(previous.id);
    saveSize();
  }, [saveSize]);
  const syncNativeDimensions = useCallback(() => {
    if (!nativeReady.current || !stage.current || !group.current) return;
    const width = stage.current.clientWidth - 16;
    const viewport = group.current.querySelector<HTMLElement>('.message-list');
    setLayout((value) => ({ ...value, width, history: expandedRef.current && viewport ? Math.max(72, viewport.clientHeight) : value.history }));
  }, []);
  useEffect(() => {
    if (!desktop || !nativeReady.current) return;
    const timer = setTimeout(saveSize, 200);
    return () => clearTimeout(timer);
  }, [layout.width, layout.history, saveSize]);
  useEffect(() => {
    if (expanded && !document.hidden && document.hasFocus()) workspaceStore.markRead(thread.id);
  }, [expanded, thread.id, thread.unread]);
  useEffect(() => {
    function outside(event: PointerEvent) {
      if (!group.current?.contains(event.target as Node)) blur();
    }
    function blur() {
      endDrag();
      // Release text focus when leaving the window. Otherwise activating the
      // window via its drag handle can restore stale input focus and open history.
      const input = composer.current?.querySelector('textarea');
      if (document.activeElement === input) input?.blur();
      setExpanded(false);
    }
    function hidden() { if (document.hidden) blur(); }
    document.addEventListener('pointerdown', outside); window.addEventListener('blur', blur); document.addEventListener('visibilitychange', hidden);
    let disposed = false; let unlisten: (() => void) | undefined;
    if (desktop) void onNativeFocus((focused) => { if (!focused) blur(); }).then((stop) => { if (disposed) stop(); else unlisten = stop; }).catch(onError);
    return () => { disposed = true; unlisten?.(); document.removeEventListener('pointerdown', outside); window.removeEventListener('blur', blur); document.removeEventListener('visibilitychange', hidden); saveSize(); };
  }, [endDrag, onError, saveSize]);
  useLayoutEffect(() => {
    const observer = new ResizeObserver(() => setComposerHeight(composer.current?.offsetHeight ?? 64));
    if (composer.current) observer.observe(composer.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!desktop) return;
    // Programmatic focus changes preserve the input bar's bottom anchor. Native edge
    // resizing is handled by the OS and does not call this effect on every resize event.
    const history = group.current!.querySelector<HTMLElement>('.compact-history')!;
    const heading = group.current!.querySelector<HTMLElement>('.compact-heading')!;
    const status = group.current!.querySelector<HTMLElement>('.run-status')!;
    const css = getComputedStyle(history), headingCss = getComputedStyle(heading), statusCss = getComputedStyle(status);
    const surfaceCss = getComputedStyle(group.current!.querySelector<HTMLElement>('.compact-surface')!);
    const px = (value: string) => Number.parseFloat(value) || 0;
    const inputHeight = composerHeight + px(surfaceCss.borderTopWidth) + px(surfaceCss.borderBottomWidth);
    const chrome = inputHeight + px(css.paddingTop) + px(css.paddingBottom) + px(css.borderTopWidth) + px(css.borderBottomWidth) + px(css.marginBottom)
      + heading.offsetHeight + px(headingCss.marginBottom) + status.offsetHeight + px(statusCss.marginTop);
    nativeReady.current = false;
    const ticket = ++nativeFitTicket.current;
    void fitCompact(expanded ? layoutRef.current.history + chrome + 16 : inputHeight + 16, expanded ? 72 + chrome + 16 : inputHeight + 16)
      .then(() => { if (ticket === nativeFitTicket.current) { nativeReady.current = true; syncNativeDimensions(); } }).catch(onError);
  }, [expanded, composerHeight, preferences.replyPlacement, onError, syncNativeDimensions]);
  useLayoutEffect(() => {
    function contain() {
      const scene = stage.current!, frame = group.current!;
      if (desktop) {
        syncNativeDimensions();
        return;
      }
      const width = Math.min(layoutRef.current.width, scene.clientWidth - 28);
      const chrome = frame.offsetHeight - (expandedRef.current ? layoutRef.current.history : 0);
      setLayout((value) => ({ ...value, width, left: clamp(value.left, 14, scene.clientWidth - width - 14),
        history: Math.min(value.history, Math.max(72, scene.clientHeight - chrome - 28)),
        bottom: clamp(value.bottom, 14, Math.max(14, scene.clientHeight - frame.offsetHeight - 14)) }));
    }
    contain(); initialized.current = true;
    const observer = new ResizeObserver(contain); observer.observe(stage.current!);
    return () => observer.disconnect();
  }, [syncNativeDimensions]);
  useLayoutEffect(() => {
    if (desktop || !initialized.current || drag.current) return;
    const scene = stage.current!, frame = group.current!;
    const chrome = frame.offsetHeight - (expanded ? layout.history : 0);
    const history = Math.min(layout.history, Math.max(72, scene.clientHeight - chrome - 28));
    const bottom = clamp(layout.bottom, 14, Math.max(14, scene.clientHeight - chrome - (expanded ? history : 0) - 14));
    if (history !== layout.history || bottom !== layout.bottom) setLayout({ ...layout, history, bottom });
  }, [expanded, composerHeight, layout]);

  function begin(event: ReactPointerEvent<HTMLElement>, direction: Direction | 'move') {
    if (event.button !== 0) return;
    event.preventDefault(); event.stopPropagation();
    if (desktop) { void (direction === 'move' ? dragNative() : resizeNative(direction)).catch(onError); return; }
    const frame = group.current!.getBoundingClientRect(), scene = stage.current!.getBoundingClientRect();
    drag.current = { id: event.pointerId, direction, x: event.clientX, y: event.clientY, handle: event.currentTarget,
      start: { x: frame.x - scene.x, y: frame.y - scene.y, width: frame.width, height: frame.height },
      bounds: { x: 14, y: 14, width: scene.width - 28, height: scene.height - 28 }, chrome: frame.height - (expanded ? layout.history : 0) };
    event.currentTarget.setPointerCapture(event.pointerId); setDragging(true);
  }
  function move(event: ReactPointerEvent<HTMLElement>) {
    const current = drag.current; if (!current || current.id !== event.pointerId) return;
    event.preventDefault();
    const dx = event.clientX - current.x, dy = event.clientY - current.y;
    const rect = current.direction === 'move' ? { ...current.start,
      x: clamp(current.start.x + dx, 14, current.bounds.width + 14 - current.start.width),
      y: clamp(current.start.y + dy, 14, current.bounds.height + 14 - current.start.height) }
      : resizeRect(current.start, current.direction, dx, dy, current.bounds, 320, current.chrome + (expanded ? 72 : 0));
    const next = { left: rect.x, bottom: stage.current!.clientHeight - rect.y - rect.height, width: rect.width, history: expanded ? Math.max(72, rect.height - current.chrome) : layoutRef.current.history };
    layoutRef.current = next; setLayout(next);
  }
  const style: CSSProperties = desktop ? {} : { width: layout.width, left: layout.left, bottom: layout.bottom };
  const content = <section className="compact-history" aria-label="简化模式消息" hidden={!expanded}>
    <div className="compact-heading"><span>{thread.characterId} · {thread.title}</span></div>
    <MessageList thread={thread} compact height={desktop ? undefined : layout.history} visible={expanded} followToken={followToken} />
    <RunStatus thread={thread} />
  </section>;
  return <div ref={stage} className={`compact-scene ${desktop ? 'native' : 'browser'}`}>
    {!desktop && <div className="preview-context">简化模式 · 浏览器预览</div>}
    <div ref={group} className={`compact-group ${preferences.replyPlacement} ${expanded ? 'expanded' : 'collapsed'} ${dragging ? 'dragging' : ''}`} style={style}
      onFocusCapture={(event) => { if (event.target instanceof HTMLTextAreaElement) setExpanded(true); }}
      onBlurCapture={(event) => { if (!drag.current && !insidePointer.current && !event.currentTarget.contains(event.relatedTarget)) setExpanded(false); }}
      onPointerDownCapture={() => { insidePointer.current = true; requestAnimationFrame(() => { insidePointer.current = false; }); }}
      onPointerDown={(event) => { if (event.target instanceof HTMLTextAreaElement) setExpanded(true); }}
      onKeyDown={(event) => { if (event.key === 'Escape') { (document.activeElement as HTMLElement)?.blur(); setExpanded(false); } }}>
      {directions.filter((direction) => expanded || direction === 'e' || direction === 'w').map((direction) => <button key={direction} type="button" className={`resize-edge edge-${direction}`} aria-label={`拖拽${labels[direction]}调整尺寸`} data-direction={direction}
        onPointerDown={(event) => begin(event, direction)} onPointerMove={move} onPointerUp={endDrag} onPointerCancel={endDrag} onLostPointerCapture={endDrag}
        onKeyDown={(event) => {
          if (desktop || !event.key.startsWith('Arrow')) return;
          event.preventDefault();
          const horizontal = event.key === 'ArrowRight' ? 8 : event.key === 'ArrowLeft' ? -8 : 0;
          const vertical = event.key === 'ArrowDown' ? 8 : event.key === 'ArrowUp' ? -8 : 0;
          setLayout((value) => ({ ...value, width: clamp(value.width + (direction.includes('w') ? -horizontal : direction.includes('e') ? horizontal : 0), Math.min(320, stage.current!.clientWidth - 28), stage.current!.clientWidth - 28), history: clamp(value.history + (direction.includes('n') ? -vertical : direction.includes('s') ? vertical : 0), 72, 1600) }));
        }} />)}
      {preferences.replyPlacement === 'bubble' && content}
      <div className="compact-surface">
        {preferences.replyPlacement === 'inline' && content}
        <div ref={composer} className="compact-input"><Composer thread={thread} compact expanded={expanded} onSend={() => follow((value) => value + 1)}
          prefix={<button type="button" className="move-handle" aria-label="拖动输入条" onPointerDown={(event) => begin(event, 'move')} onPointerMove={move} onPointerUp={endDrag} onPointerCancel={endDrag}><GripVertical size={15} /></button>}>
          {thread.unread && !expanded && <span className="unread-label">有新回复</span>}
          <button type="button" className="return-button" aria-label="返回工作台" title="返回工作台" onClick={leave}><PanelsTopLeft size={18} /></button>
          {desktop && <button type="button" className="close-button" aria-label="退出程序" title="退出程序" onClick={() => { saveSize(); void closeDesktop().catch(onError); }}><X size={18} /></button>}
        </Composer></div>
      </div>
    </div>
  </div>;
}
