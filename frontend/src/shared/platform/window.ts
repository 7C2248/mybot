import { invoke, isTauri } from '@tauri-apps/api/core';
import { getCurrentWindow, currentMonitor, PhysicalPosition, PhysicalSize, LogicalSize } from '@tauri-apps/api/window';
import type { Direction } from '../../features/compact-chat/geometry';
import type { Preferences } from '../types';

export const desktop = isTauri();
let previous: { position: PhysicalPosition; size: PhysicalSize; maximized: boolean } | undefined;
let compactAnchor: { x: number; bottom: number } | undefined;
let queue = Promise.resolve();
function sequential(operation: () => Promise<void>) {
  const next = queue.then(operation);
  queue = next.catch(() => {});
  return next;
}
export function enterCompact(preferences: Preferences) {
  if (!desktop) return Promise.resolve();
  return sequential(async () => {
    const window = getCurrentWindow();
    previous = { position: await window.outerPosition(), size: await window.innerSize(), maximized: await window.isMaximized() };
    if (previous.maximized) await window.unmaximize();
    await window.setMinSize(new LogicalSize(336, 80));
    await window.setDecorations(false);
    await window.setShadow(false);
    await window.setAlwaysOnTop(preferences.alwaysOnTop);
    const monitor = await currentMonitor(), scale = await window.scaleFactor();
    const area = monitor?.workArea;
    const width = Math.round(Math.min((preferences.compactWidth + 16) * scale, area?.size.width ?? Infinity));
    const height = Math.round(84 * scale);
    await window.setSize(new PhysicalSize(width, height));
    if (compactAnchor) await window.setPosition(new PhysicalPosition(compactAnchor.x, compactAnchor.bottom - height));
    else if (area) await window.setPosition(new PhysicalPosition(Math.round(area.position.x + (area.size.width - width) / 2), Math.round(area.position.y + area.size.height - height - 36 * scale)));
    await window.setFocus();
  });
}
export function fitCompact(height: number, minimumHeight: number) {
  if (!desktop) return Promise.resolve();
  return sequential(async () => {
    const window = getCurrentWindow();
    // Capture the bottom anchor before changing constraints: Windows may resize
    // the window immediately when its minimum height increases.
    const size = await window.innerSize(), position = await window.outerPosition();
    const scale = await window.scaleFactor(), monitor = await currentMonitor();
    const area = monitor?.workArea;
    // Physical sizes/positions are integer pixels in Tauri. Fractional CSS
    // borders and display scaling otherwise produce invalid IPC arguments.
    const nextHeight = Math.round(Math.min(height * scale, area?.size.height ?? Infinity));
    const width = Math.round(Math.min(size.width, area?.size.width ?? Infinity));
    let x = position.x, y = position.y + size.height - nextHeight;
    if (area) {
      x = Math.max(area.position.x, Math.min(x, area.position.x + area.size.width - width));
      y = Math.max(area.position.y, Math.min(y, area.position.y + area.size.height - nextHeight));
    }
    await invoke('set_compact_bounds', { x: Math.round(x), y: Math.round(y), width, height: nextHeight, minimumHeight });
  });
}
export function closeDesktop() {
  return sequential(() => getCurrentWindow().close());
}
export function leaveCompact() {
  if (!desktop) return Promise.resolve();
  return sequential(async () => {
    const window = getCurrentWindow();
    const position = await window.outerPosition(), size = await window.innerSize();
    compactAnchor = { x: position.x, bottom: position.y + size.height };
    await window.setAlwaysOnTop(false); await window.setDecorations(true); await window.setShadow(true);
    await window.setMinSize(new LogicalSize(360, 480));
    if (previous) { await window.setSize(previous.size); await window.setPosition(previous.position); if (previous.maximized) await window.maximize(); }
    await window.setFocus();
  });
}
const nativeDirections = { n: 'North', s: 'South', e: 'East', w: 'West', ne: 'NorthEast', nw: 'NorthWest', se: 'SouthEast', sw: 'SouthWest' } as const;
export function resizeNative(direction: Direction) { return getCurrentWindow().startResizeDragging(nativeDirections[direction]); }
export function dragNative() { return getCurrentWindow().startDragging(); }
export function onNativeFocus(callback: (focused: boolean) => void) {
  return getCurrentWindow().onFocusChanged(({ payload }) => callback(payload));
}
