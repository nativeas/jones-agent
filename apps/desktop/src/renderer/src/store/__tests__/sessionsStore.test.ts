import { beforeEach, describe, expect, it } from 'vitest'
import { MockTransport } from '../../rpc/mockTransport'
import { useSessionsStore, mainSession } from '../sessionsStore'
import { buildProjectGroups } from '../../domain/tree'

describe('sessionsStore', () => {
  beforeEach(() => {
    useSessionsStore.setState({
      transport: null,
      projects: [],
      sessions: [],
      selectedSessionId: null,
      loading: false,
      error: null
    })
  })

  it('init() loads projects/sessions and selects the main session by default', async () => {
    const transport = new MockTransport({ schedule: (fn) => fn() })
    await useSessionsStore.getState().init(transport)

    const state = useSessionsStore.getState()
    expect(state.projects.length).toBeGreaterThan(0)
    const main = mainSession(state.sessions)
    expect(main).toBeDefined()
    expect(state.selectedSessionId).toBe(main!.id)
  })

  it('exposes exactly one main session, and it is the only one flagged is_main', async () => {
    const transport = new MockTransport({ schedule: (fn) => fn() })
    await useSessionsStore.getState().init(transport)
    const mains = useSessionsStore.getState().sessions.filter((s) => s.is_main)
    expect(mains).toHaveLength(1)
  })

  it('createSession() adds a child session and selects it', async () => {
    const transport = new MockTransport({ schedule: (fn) => fn() })
    await useSessionsStore.getState().init(transport)
    const main = mainSession(useSessionsStore.getState().sessions)!

    const created = await useSessionsStore.getState().createSession({
      project_id: main.project_id,
      agent_id: main.agent_id,
      parent_id: main.id,
      title: '子任务'
    })

    expect(created).not.toBeNull()
    expect(created!.parent_id).toBe(main.id)
    expect(useSessionsStore.getState().selectedSessionId).toBe(created!.id)

    const groups = buildProjectGroups(useSessionsStore.getState().projects, useSessionsStore.getState().sessions)
    const projectGroup = groups.find((g) => g.project.id === main.project_id)!
    const mainNode = projectGroup.roots.find((r) => r.session.id === main.id)!
    expect(mainNode.children.map((c) => c.session.id)).toContain(created!.id)
  })

  it('surfaces a transport failure as an explicit error instead of throwing', async () => {
    const failing = {
      call: async () => ({ ok: false as const, message: 'boom' }),
      on: () => () => {}
    }
    await useSessionsStore.getState().init(failing)
    expect(useSessionsStore.getState().error).toBe('boom')
  })
})

describe('buildProjectGroups (pure tree building)', () => {
  it('pins the main session first among roots regardless of list order', () => {
    const projects = [{ id: 'p1', path: '/x', name: 'X' }]
    const sessions = [
      { id: 's2', project_id: 'p1', agent_id: 'a', parent_id: null, is_main: false, mode: 'task' as const, title: 'B', status: 'idle' as const },
      { id: 's1', project_id: 'p1', agent_id: 'a', parent_id: null, is_main: true, mode: 'task' as const, title: '主会话', status: 'idle' as const }
    ]
    const [group] = buildProjectGroups(projects, sessions)
    expect(group!.roots[0]!.session.id).toBe('s1')
  })

  it('nests children under their parent, arbitrarily deep', () => {
    const projects = [{ id: 'p1', path: '/x', name: 'X' }]
    const sessions = [
      { id: 'main', project_id: 'p1', agent_id: 'a', parent_id: null, is_main: true, mode: 'task' as const, title: '主会话', status: 'idle' as const },
      { id: 'child', project_id: 'p1', agent_id: 'a', parent_id: 'main', is_main: false, mode: 'task' as const, title: '子', status: 'idle' as const },
      { id: 'grand', project_id: 'p1', agent_id: 'a', parent_id: 'child', is_main: false, mode: 'task' as const, title: '孙', status: 'idle' as const }
    ]
    const [group] = buildProjectGroups(projects, sessions)
    const main = group!.roots[0]!
    expect(main.children[0]!.session.id).toBe('child')
    expect(main.children[0]!.children[0]!.session.id).toBe('grand')
  })
})
