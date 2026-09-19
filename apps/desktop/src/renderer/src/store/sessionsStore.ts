import { create } from 'zustand'
import type { RpcTransport } from '../rpc/transport'
import type { Project, Session, SessionMode } from '../domain/types'

interface SessionsState {
  transport: RpcTransport | null
  projects: Project[]
  sessions: Session[]
  selectedSessionId: string | null
  loading: boolean
  error: string | null

  init(transport: RpcTransport): Promise<void>
  refresh(): Promise<void>
  selectSession(id: string): void
  createSession(input: {
    project_id: string
    agent_id: string
    parent_id?: string
    mode?: SessionMode
    title?: string
  }): Promise<Session | null>
  createProject(path: string): Promise<Project | null>
  setMode(sessionId: string, mode: SessionMode): Promise<void>
}

/** The main session is unique and permanent (issue #5 / 00-foundation.md §5). */
export function mainSession(sessions: Session[]): Session | undefined {
  return sessions.find((s) => s.is_main)
}

export const useSessionsStore = create<SessionsState>()((set, get) => ({
  transport: null,
  projects: [],
  sessions: [],
  selectedSessionId: null,
  loading: false,
  error: null,

  async init(transport) {
    set({ transport })
    await get().refresh()
    const current = get()
    if (!current.selectedSessionId) {
      const main = mainSession(current.sessions)
      if (main) set({ selectedSessionId: main.id })
    }
  },

  async refresh() {
    const { transport } = get()
    if (!transport) return
    set({ loading: true, error: null })
    const [projectsRes, sessionsRes] = await Promise.all([
      transport.call<Project[]>('project.list'),
      transport.call<Session[]>('session.list')
    ])
    if (!projectsRes.ok || !sessionsRes.ok) {
      set({
        loading: false,
        error: projectsRes.message ?? sessionsRes.message ?? '加载会话列表失败'
      })
      return
    }
    set({ projects: projectsRes.result ?? [], sessions: sessionsRes.result ?? [], loading: false })
  },

  selectSession(id) {
    set({ selectedSessionId: id })
  },

  async createSession(input) {
    const { transport } = get()
    if (!transport) return null
    const res = await transport.call<Session>('session.create', input)
    if (!res.ok || !res.result) {
      set({ error: res.message ?? '创建会话失败' })
      return null
    }
    set((state) => ({ sessions: [...state.sessions, res.result as Session], selectedSessionId: (res.result as Session).id }))
    return res.result
  },

  async createProject(path) {
    const { transport } = get()
    if (!transport) return null
    const res = await transport.call<Project>('project.create', { path })
    if (!res.ok || !res.result) {
      set({ error: res.message ?? '创建 Project 失败' })
      return null
    }
    set((state) => ({ projects: [...state.projects, res.result as Project] }))
    return res.result
  },

  async setMode(sessionId, mode) {
    const { transport } = get()
    if (!transport) return
    const res = await transport.call<Session>('session.set_mode', { id: sessionId, mode })
    if (!res.ok || !res.result) {
      set({ error: res.message ?? '切换模式失败' })
      return
    }
    set((state) => ({
      sessions: state.sessions.map((s) => (s.id === sessionId ? (res.result as Session) : s))
    }))
  }
}))
