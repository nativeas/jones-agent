import { describe, expect, it } from 'vitest'
import { act } from 'react-dom/test-utils'
import { createRoot, type Root } from 'react-dom/client'
import { MessageList } from '../chat/MessageList'
import type { TimelineEntry } from '../../store/chatStore'
import type { Message } from '../../domain/types'

/** jones-agent#34 regression: the daemon's real `message.content` shape is an
 * OBJECT (`{kind, text}` — `sessions/queries.py::_d()`), not a string.
 * Rendering that object as a raw React child used to throw ("Objects are not
 * valid as a React child") with no error boundary, i.e. a white screen. This
 * mounts MessageList with exactly that real shape and asserts it renders the
 * text instead of throwing — a store-level test can't catch this class of bug
 * (the crash only happens in the DOM render path), so this follows
 * DaemonStatusCard.test.tsx's precedent for when a real component mount is
 * warranted. */
describe('MessageList', () => {
  it('renders object-shaped message content (daemon real shape) without throwing', () => {
    const message: Message = {
      id: 'm1',
      session_id: 's1',
      turn_id: 't1',
      role: 'assistant',
      content: { kind: 'text', text: '你好，这是真实的 daemon 消息内容。' },
      seq: 1
    }
    const timeline: TimelineEntry[] = [{ kind: 'message', message }]

    const container = document.createElement('div')
    document.body.appendChild(container)
    let root: Root | null = null
    try {
      expect(() => {
        act(() => {
          root = createRoot(container)
          root.render(<MessageList timeline={timeline} providers={[]} models={[]} />)
        })
      }).not.toThrow()

      expect(container.textContent).toContain('你好，这是真实的 daemon 消息内容。')
    } finally {
      act(() => {
        root?.unmount()
      })
      container.remove()
    }
  })
})
