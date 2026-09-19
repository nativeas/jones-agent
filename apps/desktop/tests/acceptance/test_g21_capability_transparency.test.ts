/**
 * G21 — PRD 12.1 发布门禁:
 *
 * "能力透明一致：透明页列出的工具集合与该 Turn 实际装配给模型的工具集合完全
 * 一致，含 MCP 与 Skill 来源的工具" —— 验证方式："接入 1 个 MCP、1 个 Skill
 * 后比对透明页与回放中的 tool 列表"。
 *
 * daemon 侧（capability.list 的对账算法本身）见
 * `daemon/tests/acceptance/test_g21_capability_transparency.py`。
 *
 * 复用（不重复造），桌面端「装配对账不一致」告警 UI：
 * `src/renderer/src/components/__tests__/CapabilitySettings.test.tsx`
 * （"shows a prominent drift warning when capability.list reports drift
 * (G21)"）——透明页把 daemon 返回的 drift 数组渲染成醒目警告，不是静默吞掉。
 */
import { describe, it } from 'vitest'
import { runExisting } from './_reuse'

describe('G21 acceptance (desktop capability transparency)', () => {
  // round-1 review fix (评审 #7): see test_g07_replay_ui.test.ts for why this
  // must match _reuse.ts's own 120_000ms `runExisting` timeout budget.
  it(
    'reuses CapabilitySettings.test.tsx drift-warning coverage',
    () => {
      runExisting('src/renderer/src/components/__tests__/CapabilitySettings.test.tsx')
    },
    120_000
  )
})
