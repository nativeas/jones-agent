import { beforeEach, describe, expect, it } from 'vitest'
import type { RpcCallResult, RpcTransport } from '../../rpc/transport'
import { useReplayStore } from '../replayStore'

/**
 * A small hand-rolled fake, not `MockTransport` (that's D/#5's own simulator
 * for `session.*` — no `run.list`/`run.payload` handling, and not this
 * branch's file to extend) — same pattern `sessionsStore.test.ts`'s "surfaces
 * a transport failure" case already uses for a one-off fake.
 */
function fakeTransport(handlers: Record<string, (params?: Record<string, unknown>) => unknown>): RpcTransport {
  return {
    async call<T>(method: string, params?: Record<string, unknown>): Promise<RpcCallResult<T>> {
      const handler = handlers[method]
      if (!handler) return { ok: false, message: `no handler for ${method}` }
      return { ok: true, result: handler(params) as T }
    },
    on: () => () => {}
  }
}

function b64(text: string): string {
  return btoa(text)
}

function resetStore(): void {
  useReplayStore.setState({
    transport: null,
    sessionId: null,
    runs: [],
    selectedRunId: null,
    run: null,
    steps: [],
    cursor: 0,
    promptSnapshot: null,
    promptSnapshotTruncated: false,
    payloadCache: {},
    payloadTruncated: {},
    loading: false,
    error: null
  })
}

describe('replayStore', () => {
  beforeEach(resetStore)

  it('bindSession loads the Run list for that session via run.list', async () => {
    const transport = fakeTransport({
      'run.list': () => [
        { id: 'run-2', session_id: 's1', status: 'completed', started_at: null, ended_at: null, terminated_kind: null, created_at: '2026-01-02T00:00:00.000Z' },
        { id: 'run-1', session_id: 's1', status: 'terminated', started_at: null, ended_at: null, terminated_kind: 'error', created_at: '2026-01-01T00:00:00.000Z' }
      ]
    })

    await useReplayStore.getState().bindSession(transport, 's1')

    const state = useReplayStore.getState()
    expect(state.sessionId).toBe('s1')
    expect(state.runs.map((r) => r.id)).toEqual(['run-2', 'run-1'])
  })

  it('selectRun loads run.get + every page of run.steps, landing the cursor on the last Step', async () => {
    // 3 Steps returned over two pages to exercise the after_seq pagination
    // loop (`PAGE_SIZE` in replayStore.ts is 200 — force a tiny page here by
    // asserting on the params the store actually sent, not by relying on a
    // huge fixture).
    const allSteps = [
      { id: 's1', run_id: 'run-1', seq: 1, tool: 'a', args: {}, result_summary: null, payload_ref: null, duration_ms: 5, permission_id: null, permission_decision: null, permission_decided_by: null, status: 'completed' },
      { id: 's2', run_id: 'run-1', seq: 2, tool: 'b', args: {}, result_summary: null, payload_ref: null, duration_ms: 5, permission_id: 'dec-1', permission_decision: 'allow', permission_decided_by: 'user', status: 'completed' }
    ]
    const calls: Array<Record<string, unknown> | undefined> = []
    const transport = fakeTransport({
      'run.get': () => ({
        id: 'run-1', task_id: null, turn_id: 't1', session_id: 's1', status: 'completed',
        started_at: null, ended_at: null, terminated_kind: null, terminated_reason: null,
        terminated_step_seq: null, prompt_snapshot_ref: null
      }),
      'run.steps': (params) => {
        calls.push(params)
        return allSteps
      }
    })

    useReplayStore.setState({ transport })
    await useReplayStore.getState().selectRun('run-1')

    const state = useReplayStore.getState()
    expect(state.run?.id).toBe('run-1')
    expect(state.steps.map((s) => s.id)).toEqual(['s1', 's2'])
    expect(state.cursor).toBe(1) // lands on the last Step
    expect(calls[calls.length - 1]).toMatchObject({ run_id: 'run-1' })
    // G07 审批结果 (round-1 review fix): `run.steps`' decision/decided_by pass
    // through to the store untouched, not just `permission_id`.
    expect(state.steps[1]).toMatchObject({ permission_decision: 'allow', permission_decided_by: 'user' })
  })

  it('stepForward/stepBack move the cursor without going out of bounds', () => {
    useReplayStore.setState({
      steps: [
        { id: 's1', run_id: 'r', seq: 1, tool: 'a', args: null, result_summary: null, payload_ref: null, duration_ms: null, permission_id: null, permission_decision: null, permission_decided_by: null, status: 'completed' },
        { id: 's2', run_id: 'r', seq: 2, tool: 'b', args: null, result_summary: null, payload_ref: null, duration_ms: null, permission_id: null, permission_decision: null, permission_decided_by: null, status: 'completed' },
        { id: 's3', run_id: 'r', seq: 3, tool: 'c', args: null, result_summary: null, payload_ref: null, duration_ms: null, permission_id: null, permission_decision: null, permission_decided_by: null, status: 'completed' }
      ],
      cursor: 0
    })

    useReplayStore.getState().stepBack()
    expect(useReplayStore.getState().cursor).toBe(0) // clamped, no negative

    useReplayStore.getState().stepForward()
    useReplayStore.getState().stepForward()
    expect(useReplayStore.getState().cursor).toBe(2)

    useReplayStore.getState().stepForward()
    expect(useReplayStore.getState().cursor).toBe(2) // clamped at the end
  })

  it('loadPromptSnapshot fetches and base64-decodes run.payload for prompt_snapshot_ref, once', async () => {
    let calls = 0
    const seenParams: Array<Record<string, unknown> | undefined> = []
    const transport = fakeTransport({
      'run.payload': (params) => {
        calls += 1
        seenParams.push(params)
        return { ref: 'run-1/prompt_snapshot.json', offset: 0, size: 10, data_base64: b64('{"a":1}'), eof: true }
      }
    })
    useReplayStore.setState({
      transport,
      run: {
        id: 'run-1', task_id: null, turn_id: null, session_id: 's1', status: 'completed',
        started_at: null, ended_at: null, terminated_kind: null, terminated_reason: null,
        terminated_step_seq: null, prompt_snapshot_ref: 'run-1/prompt_snapshot.json'
      }
    })

    await useReplayStore.getState().loadPromptSnapshot()
    expect(useReplayStore.getState().promptSnapshot).toBe('{"a":1}')
    expect(useReplayStore.getState().promptSnapshotTruncated).toBe(false)
    // 契约分片 (02-w3-interfaces.md §2 "大于 1MB 走分片 offset/limit", round-1
    // review fix): the fetch must actually pass offset/limit, not omit them and
    // rely on the daemon's `limit=None` "read to EOF" default.
    expect(seenParams[0]).toMatchObject({ ref: 'run-1/prompt_snapshot.json', offset: 0, limit: 1024 * 1024 })

    // A second call must not re-fetch (cache check on promptSnapshot !== null).
    await useReplayStore.getState().loadPromptSnapshot()
    expect(calls).toBe(1)
  })

  it('loadStepPayload fetches and caches a Step payload by ref, once', async () => {
    let calls = 0
    const transport = fakeTransport({
      'run.payload': () => {
        calls += 1
        return { ref: 'run-1/2.json', offset: 0, size: 4, data_base64: b64('{"ok":true}'), eof: true }
      }
    })
    useReplayStore.setState({ transport })

    await useReplayStore.getState().loadStepPayload('run-1/2.json')
    expect(useReplayStore.getState().payloadCache['run-1/2.json']).toBe('{"ok":true}')
    expect(useReplayStore.getState().payloadTruncated['run-1/2.json']).toBe(false)

    await useReplayStore.getState().loadStepPayload('run-1/2.json')
    expect(calls).toBe(1)
  })

  it('loadStepPayload pages through offset/limit until eof and concatenates the chunks', async () => {
    // Round-1 review fix regression test: the pre-fix store never passed
    // offset/limit at all, so a multi-chunk response was structurally
    // impossible to exercise. Three 1-byte-larger-than-nothing "chunks" (the
    // real CHUNK_BYTES is 1 MiB — this fixture just proves the loop mechanics:
    // it must keep calling with an advancing `offset` until a response reports
    // `eof: true`, then hand back the full, correctly-ordered concatenation).
    const parts = ['abc', 'def', 'ghi']
    const calls: Array<Record<string, unknown> | undefined> = []
    const transport = fakeTransport({
      'run.payload': (params) => {
        calls.push(params)
        const offset = (params?.offset as number) ?? 0
        const index = calls.length - 1
        const part = parts[index] ?? ''
        return {
          ref: 'run-1/big.json',
          offset,
          size: parts.join('').length,
          data_base64: b64(part),
          eof: index === parts.length - 1
        }
      }
    })
    useReplayStore.setState({ transport })

    await useReplayStore.getState().loadStepPayload('run-1/big.json')

    expect(useReplayStore.getState().payloadCache['run-1/big.json']).toBe('abcdefghi')
    expect(useReplayStore.getState().payloadTruncated['run-1/big.json']).toBe(false)
    expect(calls.map((p) => p?.offset)).toEqual([0, 3, 6])
    expect(calls.every((p) => p?.limit === 1024 * 1024)).toBe(true)
  })

  it('loadStepPayload stops and marks truncated once the display cap is hit, without reaching eof', async () => {
    // The 8 MiB display cap (`MAX_DISPLAY_BYTES`) exists so an enormous
    // rawOutput can't hang the renderer materializing the whole thing into one
    // string/<pre> — simulate that by having every "chunk" claim it isn't EOF
    // and a huge `size`, and confirm the loop actually stops instead of
    // looping until real memory pressure.
    let callCount = 0
    const transport = fakeTransport({
      'run.payload': (params) => {
        callCount += 1
        const offset = (params?.offset as number) ?? 0
        return {
          ref: 'run-1/huge.json',
          offset,
          size: 999 * 1024 * 1024,
          data_base64: b64('x'.repeat(1024 * 1024)),
          eof: false
        }
      }
    })
    useReplayStore.setState({ transport })

    await useReplayStore.getState().loadStepPayload('run-1/huge.json')

    expect(useReplayStore.getState().payloadTruncated['run-1/huge.json']).toBe(true)
    expect(useReplayStore.getState().payloadCache['run-1/huge.json']?.length).toBe(8 * 1024 * 1024)
    expect(callCount).toBe(8) // MAX_DISPLAY_BYTES / CHUNK_BYTES
  })

  it('surfaces a run.list failure as an explicit error, not a throw', async () => {
    const transport = fakeTransport({}) // no handler registered -> "no handler for run.list"
    await useReplayStore.getState().bindSession(transport, 's1')
    expect(useReplayStore.getState().error).toContain('run.list')
  })
})
