import { create } from 'zustand'
import type { RpcTransport } from '../rpc/transport'
import type { Agent, Model, Provider } from '../domain/types'

interface SettingsState {
  transport: RpcTransport | null
  providers: Provider[]
  models: Model[]
  agents: Agent[]
  loading: boolean
  error: string | null

  init(transport: RpcTransport): Promise<void>
  refresh(): Promise<void>
  setProviderKey(provider: string, key: string): Promise<void>
  deleteProviderKey(provider: string): Promise<void>
  upsertAgent(agent: Partial<Agent> & { name: string }): Promise<Agent | null>
  deleteAgent(id: string): Promise<void>
}

export const useSettingsStore = create<SettingsState>()((set, get) => ({
  transport: null,
  providers: [],
  models: [],
  agents: [],
  loading: false,
  error: null,

  async init(transport) {
    set({ transport })
    await get().refresh()
  },

  async refresh() {
    const { transport } = get()
    if (!transport) return
    set({ loading: true, error: null })
    const [providersRes, modelsRes, agentsRes] = await Promise.all([
      transport.call<Provider[]>('provider.list'),
      transport.call<Model[]>('model.list'),
      transport.call<Agent[]>('agent.list')
    ])
    if (!providersRes.ok || !modelsRes.ok || !agentsRes.ok) {
      set({
        loading: false,
        error: providersRes.message ?? modelsRes.message ?? agentsRes.message ?? '加载设置失败'
      })
      return
    }
    set({
      providers: providersRes.result ?? [],
      models: modelsRes.result ?? [],
      agents: agentsRes.result ?? [],
      loading: false
    })
  },

  async setProviderKey(provider, key) {
    const { transport } = get()
    if (!transport) return
    const res = await transport.call<Provider>('provider.set_key', { provider, key })
    if (!res.ok || !res.result) {
      set({ error: res.message ?? 'Key 保存失败' })
      return
    }
    const updated = res.result
    set((state) => ({ providers: state.providers.map((p) => (p.provider === provider ? updated : p)) }))
  },

  async deleteProviderKey(provider) {
    const { transport } = get()
    if (!transport) return
    const res = await transport.call<Provider>('provider.delete_key', { provider })
    if (!res.ok || !res.result) {
      set({ error: res.message ?? 'Key 删除失败' })
      return
    }
    const updated = res.result
    set((state) => ({ providers: state.providers.map((p) => (p.provider === provider ? updated : p)) }))
  },

  async upsertAgent(agent) {
    const { transport } = get()
    if (!transport) return null
    const res = await transport.call<Agent>('agent.upsert', agent)
    if (!res.ok || !res.result) {
      set({ error: res.message ?? 'Agent 保存失败' })
      return null
    }
    const saved = res.result
    set((state) => {
      const exists = state.agents.some((a) => a.id === saved.id)
      return { agents: exists ? state.agents.map((a) => (a.id === saved.id ? saved : a)) : [...state.agents, saved] }
    })
    return saved
  },

  async deleteAgent(id) {
    const { transport } = get()
    if (!transport) return
    const res = await transport.call('agent.delete', { id })
    if (!res.ok) {
      set({ error: res.message ?? 'Agent 删除失败' })
      return
    }
    set((state) => ({ agents: state.agents.filter((a) => a.id !== id) }))
  }
}))
