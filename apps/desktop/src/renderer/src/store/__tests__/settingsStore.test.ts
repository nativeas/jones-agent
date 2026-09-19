import { beforeEach, describe, expect, it } from 'vitest'
import { MockTransport } from '../../rpc/mockTransport'
import { useSettingsStore } from '../settingsStore'
import { KNOWN_PROVIDERS } from '../../domain/types'

describe('settingsStore', () => {
  beforeEach(() => {
    useSettingsStore.setState({
      transport: null,
      providers: [],
      models: [],
      agents: [],
      loading: false,
      error: null
    })
  })

  it('init() loads providers, models and agents', async () => {
    const transport = new MockTransport({ schedule: (fn) => fn() })
    await useSettingsStore.getState().init(transport)

    const state = useSettingsStore.getState()
    expect(state.providers.map((p) => p.provider).sort()).toEqual([...KNOWN_PROVIDERS].sort())
    expect(state.models.length).toBeGreaterThan(0)
    expect(state.agents.length).toBeGreaterThan(0)
    expect(state.loading).toBe(false)
  })

  it('setProviderKey() stores only the key hint, never the full key (PRD FR04)', async () => {
    const transport = new MockTransport({ schedule: (fn) => fn() })
    await useSettingsStore.getState().init(transport)

    await useSettingsStore.getState().setProviderKey('anthropic', 'sk-ant-verysecret1234')
    const provider = useSettingsStore.getState().providers.find((p) => p.provider === 'anthropic')
    expect(provider?.has_key).toBe(true)
    expect(provider?.key_hint).toBe('1234')
  })

  it('deleteProviderKey() clears has_key and the hint', async () => {
    const transport = new MockTransport({ schedule: (fn) => fn() })
    await useSettingsStore.getState().init(transport)
    await useSettingsStore.getState().setProviderKey('openai', 'sk-openai-abcd')

    await useSettingsStore.getState().deleteProviderKey('openai')
    const provider = useSettingsStore.getState().providers.find((p) => p.provider === 'openai')
    expect(provider?.has_key).toBe(false)
    expect(provider?.key_hint).toBeNull()
  })

  it('upsertAgent() creates a new agent and appends it', async () => {
    const transport = new MockTransport({ schedule: (fn) => fn() })
    await useSettingsStore.getState().init(transport)
    const before = useSettingsStore.getState().agents.length

    const created = await useSettingsStore.getState().upsertAgent({
      name: '调研 Agent',
      persona: '严谨',
      tone: '中性',
      principles: '先查证',
      tool_allowlist: ['web.search'],
      skills: [],
      model_pref: { provider: 'anthropic', model: 'claude-sonnet-4-5' }
    })

    expect(created).not.toBeNull()
    expect(created!.model_pref).toEqual({ provider: 'anthropic', model: 'claude-sonnet-4-5' })
    expect(useSettingsStore.getState().agents).toHaveLength(before + 1)
  })

  it('upsertAgent() with an existing id updates in place rather than duplicating', async () => {
    const transport = new MockTransport({ schedule: (fn) => fn() })
    await useSettingsStore.getState().init(transport)
    const existing = useSettingsStore.getState().agents[0]!
    const before = useSettingsStore.getState().agents.length

    await useSettingsStore.getState().upsertAgent({ id: existing.id, name: '改名后' })

    const state = useSettingsStore.getState()
    expect(state.agents).toHaveLength(before)
    expect(state.agents.find((a) => a.id === existing.id)?.name).toBe('改名后')
  })

  it('deleteAgent() removes it from the list', async () => {
    const transport = new MockTransport({ schedule: (fn) => fn() })
    await useSettingsStore.getState().init(transport)
    const created = await useSettingsStore.getState().upsertAgent({ name: '临时 Agent' })

    await useSettingsStore.getState().deleteAgent(created!.id)
    expect(useSettingsStore.getState().agents.some((a) => a.id === created!.id)).toBe(false)
  })

  it('surfaces a transport failure as an explicit error instead of throwing', async () => {
    const failing = {
      call: async () => ({ ok: false as const, message: 'boom' }),
      on: () => () => {}
    }
    await useSettingsStore.getState().init(failing)
    expect(useSettingsStore.getState().error).toBe('boom')
  })

  it('surfaces a rejected transport.call (not just ok:false) as an explicit error, and clears `loading`', async () => {
    const rejecting = {
      call: async () => {
        throw new Error('ipc channel closed')
      },
      on: () => () => {}
    }
    await useSettingsStore.getState().init(rejecting)
    const state = useSettingsStore.getState()
    expect(state.loading).toBe(false)
    expect(state.error).toBe('ipc channel closed')
  })
})
