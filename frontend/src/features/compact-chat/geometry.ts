export type Direction = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw';
export interface Rect { x: number; y: number; width: number; height: number }
export const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value));
// All values use CSS pixels. The opposite edge stays fixed, including at the minimum size.
export function resizeRect(start: Rect, direction: Direction, dx: number, dy: number, bounds: Rect, minWidth: number, minHeight: number): Rect {
  let { x, y, width, height } = start;
  const right = x + width, bottom = y + height;
  if (direction.includes('e')) width = clamp(width + dx, Math.min(minWidth, bounds.width), bounds.x + bounds.width - x);
  if (direction.includes('w')) { width = clamp(width - dx, Math.min(minWidth, bounds.width), right - bounds.x); x = right - width; }
  if (direction.includes('s')) height = clamp(height + dy, Math.min(minHeight, bounds.height), bounds.y + bounds.height - y);
  if (direction.includes('n')) { height = clamp(height - dy, Math.min(minHeight, bounds.height), bottom - bounds.y); y = bottom - height; }
  return { x, y, width, height };
}
