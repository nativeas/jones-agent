import { describe, expect, it } from 'vitest'
import { computeVisibleRange } from '../VirtualList'

describe('computeVisibleRange', () => {
  it('renders nothing for an empty list', () => {
    const range = computeVisibleRange(0, 600, 0, 64)
    expect(range).toEqual({ startIndex: 0, endIndex: 0, paddingTop: 0, paddingBottom: 0 })
  })

  it('renders nothing when the container has not been measured yet (height 0)', () => {
    const range = computeVisibleRange(0, 0, 1000, 64)
    expect(range).toEqual({ startIndex: 0, endIndex: 0, paddingTop: 0, paddingBottom: 0 })
  })

  it('at scrollTop 0 starts at index 0 (no negative overscan) and pads only the bottom', () => {
    const range = computeVisibleRange(0, 600, 1000, 64, 4)
    expect(range.startIndex).toBe(0)
    expect(range.paddingTop).toBe(0)
    expect(range.paddingBottom).toBeGreaterThan(0)
  })

  it('windows to a small slice in the middle of a long list, with overscan on both sides', () => {
    // 1000 rows of 64px; scrolled to row ~100 with a 600px viewport (~9.4 visible).
    const range = computeVisibleRange(100 * 64, 600, 1000, 64, 4)
    expect(range.startIndex).toBe(100 - 4)
    expect(range.endIndex).toBeGreaterThan(range.startIndex)
    expect(range.endIndex).toBeLessThan(1000)
    // Padding must reproduce the true scroll height so the scrollbar doesn't jump.
    expect(range.paddingTop).toBe(range.startIndex * 64)
    expect(range.paddingBottom).toBe((1000 - range.endIndex) * 64)
  })

  it('clamps endIndex to itemCount when scrolled to the bottom', () => {
    const range = computeVisibleRange(999 * 64, 600, 1000, 64, 4)
    expect(range.endIndex).toBe(1000)
    expect(range.paddingBottom).toBe(0)
  })
})
