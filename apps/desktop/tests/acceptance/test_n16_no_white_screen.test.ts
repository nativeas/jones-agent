/**
 * N16 — PRD 12.2 负面清单:
 *
 * "白屏、无响应窗口、需要强制退出才能恢复的挂死" —— 对应原则 5.5，绝对不能
 * 发生。也是 G08（"UI 出错误卡片，进程不崩、不白屏、不静默"）的渲染层那一半。
 *
 * 复用（不重复造）：
 * `src/renderer/src/components/__tests__/ErrorBoundary.test.tsx`
 * （jones-agent#34）——任意子组件渲染抛错时，`ErrorBoundary` 兜住并显示可恢复
 * 的 fallback UI，而不是让整棵树白屏；重新 `key` 切换视图后能恢复，不需要强制
 * 退出重启。
 */
import { describe, it } from 'vitest'
import { runExisting } from './_reuse'

describe('N16 acceptance (no white screen on render error)', () => {
  // round-1 review fix (评审 #7): see test_g07_replay_ui.test.ts for why this
  // must match _reuse.ts's own 120_000ms `runExisting` timeout budget.
  it(
    'reuses ErrorBoundary.test.tsx coverage',
    () => {
      runExisting('src/renderer/src/components/__tests__/ErrorBoundary.test.tsx')
    },
    120_000
  )
})
