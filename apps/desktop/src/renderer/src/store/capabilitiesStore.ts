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
 * 不出来）。
 *
 * 评审第 1 轮修复：`init()` 以前会顺带触发一次无项目上下文的 `loadSkills()`
 * ——因为它是从 SettingsPage 挂载时无条件调用的（不管当前在哪个子 tab），
 * 这个扫描就白跑了，用户点进「能力透明」时 CapabilitySettings 自己的
 * mount effect 又会带着正确的 project_id 再扫一次。`init()` 现在只设置
 * transport，真正的加载留给唯一需要它的调用方（CapabilitySettings 挂载时，
 * 即用户点开这个 tab 时）触发——daemon 侧扫描本身也在这条分支里改成
 * `asyncio.to_thread` 了，这里的重复只是"多做一次没必要的 IPC 往返"，不是
 * 阻塞问题，但没必要留着。 */
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
