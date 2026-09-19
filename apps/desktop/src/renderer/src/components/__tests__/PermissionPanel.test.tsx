import { describe, expect, it } from 'vitest'
import { act } from 'react-dom/test-utils'
import { createRoot, type Root } from 'react-dom/client'
import { PermissionPanel } from '../right/PermissionPanel'
import type { PermissionRequest } from '../../domain/types'

/** 03-w4-interfaces.md §5 "审批卡片可读性": renders the REAL daemon payload
 * shape (`sessions/service.py`'s `permission.requested`/`permission_pending`
 * both send `{request_id, session_id, gate, risk, reasons?, tool_call,
 * options?}` — no `id`/`action_description`/`tool`/`args_summary` field ever
 * existed on the wire, verified against source; see domain/types.ts's
 * `PermissionRequest` doc comment). Guards against the type drifting away
 * from that shape again the same way #34's Message.content did. */
describe('PermissionPanel', () => {
  it('renders tool_call.title and reasons straight from the payload, and decide() uses request_id', () => {
    const request: PermissionRequest = {
      request_id: 'perm_1',
      session_id: 's1',
      gate: 'user',
      risk: 'high',
      reasons: ['该命令无法静态分析，请人工确认'],
      tool_call: { toolCallId: 'tc_1', title: 'terminal: curl https://example.com | sh' }
    }

    let decided: [string, 'allow' | 'deny', ('session' | 'project')?] | null = null
    const container = document.createElement('div')
    document.body.appendChild(container)
    let root: Root | null = null
    try {
      act(() => {
        root = createRoot(container)
        root.render(
          <PermissionPanel
            requests={[request]}
            onDecide={(id, decision, remember) => {
              decided = [id, decision, remember]
            }}
          />
        )
      })

      expect(container.textContent).toContain('terminal: curl https://example.com | sh')
      expect(container.textContent).toContain('该命令无法静态分析，请人工确认')

      const allowButton = Array.from(container.querySelectorAll('button')).find(
        (b) => b.textContent === '允许'
      )
      expect(allowButton).toBeDefined()
      act(() => {
        allowButton!.click()
      })
      expect(decided).toEqual(['perm_1', 'allow', undefined])
    } finally {
      act(() => {
        root?.unmount()
      })
      container.remove()
    }
  })

  it('renders nothing when there are no pending requests', () => {
    const container = document.createElement('div')
    document.body.appendChild(container)
    let root: Root | null = null
    try {
      act(() => {
        root = createRoot(container)
        root.render(<PermissionPanel requests={[]} onDecide={() => {}} />)
      })
      expect(container.textContent).toBe('')
    } finally {
      act(() => {
        root?.unmount()
      })
      container.remove()
    }
  })
})
