import { beforeEach, describe, expect, it } from 'vitest'
import { MockTransport } from '../../rpc/mockTransport'
import { useCapabilitiesStore } from '../capabilitiesStore'
import type { RpcCallResult, RpcTransport } from '../../rpc/transport'

describe('capabilitiesStore', () => {
  beforeEach(() => {
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
  })

  it('init() loads skill.list', async () => {
    const transport = new MockTransport({ schedule: (fn) => fn() })
    await useCapabilitiesStore.getState().init(transport)

    const state = useCapabilitiesStore.getState()
    expect(state.skills.length).toBeGreaterThan(0)
    expect(state.skillsError).toBeNull()
    expect(state.skillsLoading).toBe(false)
  })

  it('loadCapability() populates tools/drift on success', async () => {
    const transport = new MockTransport({ schedule: (fn) => fn() })
    useCapabilitiesStore.setState({ transport })
    await useCapabilitiesStore.getState().loadCapability('session_main')

    const state = useCapabilitiesStore.getState()
    expect(state.capabilityError).toBeNull()
    expect(state.capability?.tools.length).toBeGreaterThan(0)
    expect(state.capability?.drift).toEqual([])
    expect(state.capabilitySessionId).toBe('session_main')
  })

  // 03-w4-interfaces.md §5: capability.list is H/#17 — not implemented server-
  // side yet on `main` (rpcMethods.ts's own comment says so). This is exactly
  // what happens against a real, current daemon: `method_not_found`, i.e. the
  // transport call rejects/returns ok:false. The store must surface that as
  // an error, not silently show an empty table that looks like "0 tools
  // assembled" (a false G21-passing signal).
  it('loadCapability() surfaces a not-implemented-yet error honestly instead of pretending an empty result', async () => {
    const transport: RpcTransport = {
      call: async <T,>(): Promise<RpcCallResult<T>> => ({
        ok: false,
        message: 'method_not_found: capability.list'
      }),
      on: () => () => {}
    }
    useCapabilitiesStore.setState({ transport })

    await useCapabilitiesStore.getState().loadCapability('session_main')

    const state = useCapabilitiesStore.getState()
    expect(state.capability).toBeNull()
    expect(state.capabilityError).toBe('method_not_found: capability.list')
    expect(state.capabilityLoading).toBe(false)
  })

  it('loadSkills() surfaces a rejected transport call as an error (not a stuck spinner)', async () => {
    const transport: RpcTransport = {
      call: async (): Promise<never> => {
        throw new Error('IPC channel closed')
      },
      on: () => () => {}
    }

    await useCapabilitiesStore.getState().init(transport)

    const state = useCapabilitiesStore.getState()
    expect(state.skillsLoading).toBe(false)
    expect(state.skillsError).toBe('IPC channel closed')
  })
})
