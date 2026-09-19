/**
 * G07 — PRD 12.1 发布门禁:
 *
 * "Run 回放完整：...回放可逐 Step 前进 / 后退；回放不触发任何真实动作"（FR06）。
 *
 * daemon 侧（回放数据本身、审批结果落库）见
 * `daemon/tests/acceptance/test_g07_run_replay_complete.py`。
 *
 * 复用（不重复造），桌面端回放 store：
 * `src/renderer/src/store/__tests__/replayStore.test.ts`——加载某 Session 的
 * Run 列表、逐页取 `run.steps`、`stepForward`/`stepBack` 移动游标且不越界。
 * `stepForward`/`stepBack` 只移动一个本地游标索引、只读 `run.get`/`run.payload`
 * ——回放数据流里没有任何写类 RPC 调用，天然满足"回放不触发任何真实动作"。
 */
import { describe, it } from 'vitest'
import { runExisting } from './_reuse'

describe('G07 acceptance (desktop replay UI)', () => {
  // round-1 review fix (评审 #7): `runExisting` blocks synchronously for up to
  // its own 120_000ms budget (see _reuse.ts), but vitest's default per-test
  // timeout is 5000ms and does NOT wait for a synchronous block to finish —
  // it throws "Test timed out in 5000ms" out from under it. Pass the same
  // 120s budget here so the two actually agree.
  it(
    'reuses replayStore.test.ts coverage',
    () => {
      runExisting('src/renderer/src/store/__tests__/replayStore.test.ts')
    },
    120_000
  )
})
