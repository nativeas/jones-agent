import { create } from 'zustand'
import type { RpcTransport } from '../rpc/transport'
import type { CapabilityListResult, SkillEntry } from '../domain/types'

interface CapabilitiesState {
  transport: RpcTransport | null
  skills: SkillEntry[]
  skillsError: string | null
  skillsLoading: boolean
  capability: CapabilityListResult | null
  capabilitySessionId: string | null
  capabilityError: string | null
  capabilityLoading: boolean

  init(transport: RpcTransport): Promise<void>
  loadSkills(projectId?: string | null): Promise<void>
  loadCapability(sessionId: string): Promise<void>
}

/** 03-w4-interfaces.md §5「透明页 UI」的数据侧：`skill.list`（本分支新增 RPC）
 * 与 `capability.list`（H/#17，未落地——见该方法调用失败时 error 里的说明）。
 * 两个各自独立 loading/error，避免其中一个还没实现server-side 时把另一个也
 * 挡住（不能因为 capability.list 还是 method_not_found 就连 Skill 列表也显示
 * 不出来）。 */
export const useCapabilitiesStore = create<CapabilitiesState>()((set, get) => ({
  transport: null,
  skills: [],
  skillsError: null,
  skillsLoading: false,
  capability: null,
  capabilitySessionId: null,
  capabilityError: null,
  capabilityLoading: false,

  async init(transport) {
    set({ transport })
    await get().loadSkills()
  },

  async loadSkills(projectId) {
    const { transport } = get()
    if (!transport) return
    set({ skillsLoading: true, skillsError: null })
    try {
      const res = await transport.call<{ skills: SkillEntry[] }>(
        'skill.list',
        projectId ? { project_id: projectId } : {}
      )
      if (!res.ok) {
        set({ skillsLoading: false, skillsError: res.message ?? 'Skill 列表加载失败' })
        return
      }
      set({ skills: res.result?.skills ?? [], skillsLoading: false })
    } catch (err) {
      set({ skillsLoading: false, skillsError: err instanceof Error ? err.message : String(err) })
    }
  },

  async loadCapability(sessionId) {
    const { transport } = get()
    if (!transport) return
    set({ capabilityLoading: true, capabilityError: null, capabilitySessionId: sessionId })
    try {
      const res = await transport.call<CapabilityListResult>('capability.list', { session_id: sessionId })
      if (!res.ok) {
        set({
          capabilityLoading: false,
          capability: null,
          capabilityError: res.message ?? '能力清单加载失败'
        })
        return
      }
      set({ capability: res.result ?? null, capabilityLoading: false })
    } catch (err) {
      set({
        capabilityLoading: false,
        capability: null,
        capabilityError: err instanceof Error ? err.message : String(err)
      })
    }
  }
}))
