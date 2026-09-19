import { create } from 'zustand'
import type { RpcTransport } from '../rpc/transport'

/**
 * Right 栏「回放」视图的状态 (docs/design/02-w3-interfaces.md §2, issue #12
 * FR06)。纯读——每个 action 只调用 `run.list`/`run.get`/`run.steps`/
 * `run.payload`，没有一个会改变服务端状态，这就是"回放不触发任何真实动作"
 * 在前端这一侧的体现（PRD FR06 验收口径）。
 *
 * 字段名照抄 daemon 的真实返回形状（`sessions/queries.py::_d()`），不是
 * `domain/types.ts` 里 `Step`/`TerminationCard` 那套（那是 D/#5 为活动时间线
 * 假设的形状，字段名与真实后端不完全一致——参见那个文件的模块注释）；这里
 * 直接对着 `run.list`/`run.get`/`run.steps` 的真实响应写类型，不复用它。
 */

export interface ReplayRunSummary {
  id: string
  session_id: string
  status: string
  started_at: string | null
  ended_at: string | null
  terminated_kind: string | null
  created_at: string
}

export interface ReplayRun {
  id: string
  task_id: string | null
  turn_id: string | null
  session_id: string
  status: string
  started_at: string | null
  ended_at: string | null
  terminated_kind: string | null
  terminated_reason: string | null
  terminated_step_seq: number | null
  prompt_snapshot_ref: string | null
}

export interface ReplayStep {
  id: string
  run_id: string
  seq: number
  tool: string
  args: unknown
  result_summary: string | null
  payload_ref: string | null
  duration_ms: number | null
  permission_id: string | null
  // 审批结果 (PRD 12.1 G07 / 02-w3-interfaces.md §2 round-1 review fix) — the
  // actual allow/deny outcome + who decided it, not just `permission_id` (that
  // column is only the `permission_decisions` row's primary key). NULL when
  // this Step never triggered a permission gate.
  permission_decision: 'pending' | 'allow' | 'deny' | string | null
  permission_decided_by: string | null
  status: 'running' | 'completed' | 'failed' | string
}

interface RunPayloadResponse {
  ref: string
  offset: number
  size: number
  data_base64: string
  eof: boolean
}

// Mirrors daemon `replay/store.py::CHUNK_THRESHOLD_BYTES` (02-w3-interfaces.md
// §2: "大于 1MB 走分片 offset/limit"). Round-1 review fix: `loadPromptSnapshot`/
// `loadStepPayload` used to call `run.payload` with no `offset`/`limit` at all
// — the daemon happily reads the *entire* file to EOF and returns it in one
// NDJSON frame regardless of size, so the chunking the contract requires was
// implemented server-side and never exercised by the only caller. Every fetch
// now goes through `fetchPayloadText` below, which pages with this `limit`.
const CHUNK_BYTES = 1024 * 1024

// A hard cap on how much of a payload this store will actually pull into
// renderer memory/DOM for display, independent of the file's real size —
// pagination alone (looping `offset`/`limit` until `eof`) still lets an
// arbitrarily large payload get fully materialized as one giant string and
// handed to a plain `<pre>` (not virtualized) if nothing ever stops the loop.
// 8 MiB is generous for "read a tool's output while debugging a replay" while
// keeping a pathological multi-hundred-MB `rawOutput` from hanging the
// renderer — see the PR report's "评审关注点" for why display, not just
// fetch, needed a bound here.
const MAX_DISPLAY_BYTES = 8 * CHUNK_BYTES

interface PayloadFetchResult {
  text: string
  truncated: boolean
}

async function fetchPayloadText(
  transport: RpcTransport,
  ref: string
): Promise<PayloadFetchResult | { error: string }> {
  const chunks: Uint8Array[] = []
  let total = 0
  let offset = 0
  for (;;) {
    const res = await transport.call<RunPayloadResponse>('run.payload', {
      ref,
      offset,
      limit: CHUNK_BYTES
    })
    if (!res.ok || !res.result) return { error: res.message ?? '加载失败' }
    const { data_base64, eof, size } = res.result
    // atob is available in the renderer (browser-standard global) — no Node
    // Buffer here, this runs with nodeIntegration: false (design §1).
    const binary = atob(data_base64)
    const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0))
    chunks.push(bytes)
    total += bytes.length
    offset += bytes.length
    if (eof || total >= MAX_DISPLAY_BYTES) {
      // Concatenate every chunk into one buffer *before* decoding — decoding
      // chunk-by-chunk with a fresh `TextDecoder` per chunk would corrupt any
      // multi-byte UTF-8 character that happened to straddle a chunk boundary.
      const combined = new Uint8Array(total)
      let pos = 0
      for (const chunk of chunks) {
        combined.set(chunk, pos)
        pos += chunk.length
      }
      // `run.payload` is only ever used on the JSON/text payloads this codebase
      // itself writes (replay/store.py — tool rawOutput / prompt snapshots),
      // never arbitrary binary media, so UTF-8 text decoding is the right (and
      // only) interpretation here.
      return { text: new TextDecoder('utf-8').decode(combined), truncated: !eof && total < size }
    }
  }
}

interface ReplayState {
  transport: RpcTransport | null
  sessionId: string | null
  runs: ReplayRunSummary[]
  selectedRunId: string | null
  run: ReplayRun | null
  steps: ReplayStep[]
  cursor: number
  promptSnapshot: string | null
  promptSnapshotTruncated: boolean
  payloadCache: Record<string, string>
  payloadTruncated: Record<string, boolean>
  loading: boolean
  error: string | null

  bindSession(transport: RpcTransport, sessionId: string): Promise<void>
  refreshRuns(): Promise<void>
  selectRun(runId: string): Promise<void>
  stepForward(): void
  stepBack(): void
  loadPromptSnapshot(): Promise<void>
  loadStepPayload(ref: string): Promise<void>
}

export const useReplayStore = create<ReplayState>()((set, get) => ({
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
  error: null,

  async bindSession(transport, sessionId) {
    set({
      transport,
      sessionId,
      runs: [],
      selectedRunId: null,
      run: null,
      steps: [],
      cursor: 0,
      promptSnapshot: null,
      promptSnapshotTruncated: false,
      payloadCache: {},
      payloadTruncated: {},
      error: null
    })
    await get().refreshRuns()
  },

  async refreshRuns() {
    const { transport, sessionId } = get()
    if (!transport || !sessionId) return
    set({ loading: true, error: null })
    const res = await transport.call<ReplayRunSummary[]>('run.list', { session_id: sessionId })
    if (!res.ok) {
      set({ loading: false, error: res.message ?? '加载 Run 列表失败' })
      return
    }
    set({ runs: res.result ?? [], loading: false })
  },

  async selectRun(runId) {
    const { transport } = get()
    if (!transport) return
    set({
      selectedRunId: runId,
      run: null,
      steps: [],
      cursor: 0,
      promptSnapshot: null,
      loading: true,
      error: null
    })

    const runRes = await transport.call<ReplayRun>('run.get', { run_id: runId })
    if (!runRes.ok || !runRes.result) {
      set({ loading: false, error: runRes.message ?? '加载 Run 失败' })
      return
    }

    // `run.steps` 分页 (02-w3-interfaces.md §2) — page forward until the
    // daemon returns fewer than a full page, same "small Run needs one round
    // trip" property `sessions/queries.py::list_run_steps`'s docstring names.
    const PAGE_SIZE = 200
    const steps: ReplayStep[] = []
    let afterSeq: number | undefined
    for (;;) {
      const params: Record<string, unknown> = { run_id: runId, limit: PAGE_SIZE }
      if (afterSeq !== undefined) params.after_seq = afterSeq
      const stepsRes = await transport.call<ReplayStep[]>('run.steps', params)
      if (!stepsRes.ok) {
        set({ loading: false, error: stepsRes.message ?? '加载 Step 失败' })
        return
      }
      const page = stepsRes.result ?? []
      steps.push(...page)
      if (page.length < PAGE_SIZE) break
      const last = page[page.length - 1]
      if (!last) break
      afterSeq = last.seq
    }

    set({
      run: runRes.result,
      steps,
      // Land on the last Step by default — for a terminated Run that's "what
      // happened right before it ended", the more useful starting point than
      // Step #1 (前进/后退 still walks the full timeline from there).
      cursor: steps.length > 0 ? steps.length - 1 : 0,
      loading: false
    })
  },

  stepForward() {
    set((state) => ({ cursor: Math.min(state.cursor + 1, Math.max(state.steps.length - 1, 0)) }))
  },

  stepBack() {
    set((state) => ({ cursor: Math.max(state.cursor - 1, 0) }))
  },

  async loadPromptSnapshot() {
    const { transport, run, promptSnapshot } = get()
    if (!transport || !run?.prompt_snapshot_ref || promptSnapshot !== null) return
    const result = await fetchPayloadText(transport, run.prompt_snapshot_ref)
    if ('error' in result) {
      set({ error: result.error })
      return
    }
    set({ promptSnapshot: result.text, promptSnapshotTruncated: result.truncated })
  },

  async loadStepPayload(ref) {
    const { transport, payloadCache } = get()
    if (!transport || ref in payloadCache) return
    const result = await fetchPayloadText(transport, ref)
    if ('error' in result) {
      set({ error: result.error })
      return
    }
    set((state) => ({
      payloadCache: { ...state.payloadCache, [ref]: result.text },
      payloadTruncated: { ...state.payloadTruncated, [ref]: result.truncated }
    }))
  }
}))
