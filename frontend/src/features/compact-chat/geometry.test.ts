import { describe, expect, it } from 'vitest';
import { resizeRect } from './geometry';

describe('compact window resizing', () => {
  const start = { x: 100, y: 100, width: 600, height: 400 };
  const bounds = { x: 14, y: 14, width: 972, height: 772 };
  it('keeps the bottom and right edges anchored when shrinking the upper-left corner to the minimum', () => {
    const result = resizeRect(start, 'nw', 900, 900, bounds, 320, 172);
    expect(result.width).toBe(320); expect(result.height).toBe(172);
    expect(result.x + result.width).toBe(700); expect(result.y + result.height).toBe(500);
  });
  it('keeps the entire frame inside the available work area', () => {
    expect(resizeRect(start, 'se', 2000, 2000, bounds, 320, 172)).toEqual({ x: 100, y: 100, width: 886, height: 686 });
    expect(resizeRect(start, 'nw', -2000, -2000, bounds, 320, 172)).toEqual({ x: 14, y: 14, width: 686, height: 486 });
  });
  it('allows a narrow browser preview without horizontal overflow', () => {
    const result = resizeRect({ x: 14, y: 50, width: 292, height: 200 }, 'e', -800, 0, { x: 14, y: 14, width: 292, height: 600 }, 320, 172);
    expect(result.width).toBe(292);
  });
});
