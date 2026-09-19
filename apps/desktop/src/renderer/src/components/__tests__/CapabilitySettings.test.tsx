import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { act } from 'react-dom/test-utils'
import { createRoot, type Root } from 'react-dom/client'
import { CapabilitySettings } from '../settings/CapabilitySettings'
import { useCapabilitiesStore } from '../../store/capabilitiesStore'
import { useSessionsStore } from '../../store/sessionsStore'
import { MockTransport } from '../../rpc/mockTransport'
import type { RpcCallResult, RpcTransport } from '../../rpc/transport'

const NOOP_TRANSPORT: RpcTransport = {
  call: async <T,>(): Promise<RpcCallResult<T>> => ({ ok: true, result: undefined as T }),
  on: () => () => {}
}

/** 03-w4-interfaces.md §5 / issue #19 验收: "drift 非空显示醒目警告". Pre-seeds
 * both stores directly (no RPC round-trip) since what's under test is the
 * component's own rendering of already-loaded state, not the fetch — that
 * half is capabilitiesStore.test.ts's job.
 *
 * The component DOES fire its own mount-time `loadCapability`/`loadSkills`
 * against `NOOP_TRANSPORT` (matching real behavior) — its resolution can't
 * race the synchronous assertions below and clobber the pre-seeded state:
 * every `it()` here is a plain (non-async) function, so nothing scheduled on
 * the microtask queue (the mocked `transport.call`'s Promise included) runs
 * until this whole synchronous callback returns — a plain JS run-to-
 * completion guarantee, not a timing assumption. */
describe('CapabilitySettings', () => {
  beforeEach(() => {
    useSessionsStore.setState({
      transport: NOOP_TRANSPORT,
      projects: [],
      sessions: [
        {
          id: 's1',
          project_id: 'p1',
          agent_id: 'a1',
          parent_id: null,
          is_main: true,
          mode: 'task',
          title: '主会话',
          status: 'idle'
        }
      ],
      selectedSessionId: 's1',
      loading: false,
      error: null
    })
    useCapabilitiesStore.setState({
      transport: NOOP_TRANSPORT,
      skills: [
        { name: 'research', description: '调研', tier: 'builtin', source_path: '<bundled>/research/SKILL.md', valid: true, error: null },
        { name: 'broken-skill', description: '', tier: 'user', source_path: '~/.jones/skills/broken/SKILL.md', valid: false, error: "SKILL.md 缺少开头的 YAML frontmatter（'---'）" }
      ],
      skillsError: null,
      skillsLoading: false,
      capability: {
        tools: [
          { name: 'read_file', source: 'builtin', enabled: true, actually_loaded: true },
          { name: 'kanban_create', source: 'builtin', enabled: false, hidden_reason: 'not_in_allowlist', actually_loaded: false }
        ],
        drift: ['tool "search_files" 期望装配但 worker 实际未加载']
      },
      capabilitySessionId: 's1',
      capabilityError: null,
      capabilityLoading: false
    })
  })

  let container: HTMLDivElement
  let root: Root | null = null

  afterEach(() => {
    if (root) {
      act(() => {
        root!.unmount()
      })
    }
    container?.remove()
    root = null
  })

  it('shows a prominent drift warning when capability.list reports drift (G21)', () => {
    container = document.createElement('div')
    document.body.appendChild(container)
    act(() => {
      root = createRoot(container)
      root.render(<CapabilitySettings transport={NOOP_TRANSPORT} />)
    })

    const warning = container.querySelector('.capability-settings__drift-warning')
    expect(warning).not.toBeNull()
    expect(warning?.textContent).toContain('search_files')
    expect(warning?.getAttribute('role')).toBe('alert')
  })

  it('lists tools with source/enabled/hidden_reason/actually_loaded and skills with tier/validity', () => {
    container = document.createElement('div')
    document.body.appendChild(container)
    act(() => {
      root = createRoot(container)
      root.render(<CapabilitySettings transport={NOOP_TRANSPORT} />)
    })

    expect(container.textContent).toContain('read_file')
    expect(container.textContent).toContain('kanban_create')
    expect(container.textContent).toContain('不在 Agent 白名单')
    expect(container.textContent).toContain('research')
    expect(container.textContent).toContain('broken-skill')
    expect(container.textContent).toContain('格式错误')
  })

  it('shows no drift warning when drift is empty', () => {
    useCapabilitiesStore.setState((s) => ({ capability: s.capability ? { ...s.capability, drift: [] } : s.capability }))
    container = document.createElement('div')
    document.body.appendChild(container)
    act(() => {
      root = createRoot(container)
      root.render(<CapabilitySettings transport={NOOP_TRANSPORT} />)
    })

    expect(container.querySelector('.capability-settings__drift-warning')).toBeNull()
  })

  // 评审第 2 轮回归：修复 #3（去掉 SettingsPage 的重复预取）时把 loadSkills
  // 的唯一调用点留在了 session effect 里（`if (!selectedSessionId) return`
  // 之后），sessions 为空（全新安装、或用户关掉了全部会话）时 loadSkills
  // 从未被调用过，页面假装"还没有 Skill"。这条用例故意不预置任何 session，
  // 用真实 MockTransport（而不是 NOOP_TRANSPORT）跑一次真正的 skill.list
  // 往返，断言 Skill 表真的加载出来，不是假空态；并断言「刷新」按钮此时
  // 仍可点（用户在这个场景下的唯一自救手段）。
  it('loads skill.list even when there are no sessions at all', async () => {
    useSessionsStore.setState({
      transport: NOOP_TRANSPORT,
      projects: [],
      sessions: [],
      selectedSessionId: '',
      loading: false,
      error: null
    })
    useCapabilitiesStore.setState({
      transport: null,
      skills: [],
      skillsError: null,
      skillsLoading: false,
      capability: null,
      capabilitySessionId: null,
      capabilityError: null,
      capabilityLoading: false
    })

    const transport = new MockTransport({ schedule: (fn) => fn() })
    container = document.createElement('div')
    document.body.appendChild(container)
    await act(async () => {
      root = createRoot(container)
      root.render(<CapabilitySettings transport={transport} />)
    })

    expect(container.textContent).not.toContain('还没有 Skill')
    expect(useCapabilitiesStore.getState().skills.length).toBeGreaterThan(0)

    const refreshButton = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent === '刷新'
    ) as HTMLButtonElement | undefined
    expect(refreshButton?.disabled).toBe(false)
  })
})
