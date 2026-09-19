import { describe, expect, it } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import { RPC_V0_METHODS } from '../rpcMethods'

// docs/design/00-foundation.md §4.1's table is the contract this constant must
// mirror (02-w3-interfaces.md §2 集成收口 #3: "从一个共享常量文件导出，测试断言
// 与 00-foundation §4.1 表一致") — parsed directly from the doc, not retyped by
// hand here, so this test actually fails if the table and the whitelist drift
// apart in either direction (a new §4.1 row nobody added to the whitelist, or a
// whitelist entry that isn't backed by the contract).
const DESIGN_DOC = path.resolve(__dirname, '../../../../../docs/design/00-foundation.md')

function extractMethodNames(text: string): string[] {
  // A "method name" here is exactly `word.word[.word...]` (letters/underscores,
  // dot-separated) — matches how every RPC v0 method in the doc is spelled,
  // wherever it appears in the slice (inside backticks, joined with " / ", etc).
  return Array.from(text.matchAll(/\b[a-z][a-z_]*(?:\.[a-z_]+)+\b/g)).map((m) => m[0])
}

function section(text: string, startHeading: string, endHeading: string): string {
  const start = text.indexOf(startHeading)
  const end = text.indexOf(endHeading, start)
  if (start === -1 || end === -1) {
    throw new Error(`could not find ${startHeading}..${endHeading} in ${DESIGN_DOC}`)
  }
  return text.slice(start, end)
}

describe('RPC_V0_METHODS', () => {
  it('mirrors 00-foundation.md §4.1 exactly, plus the two documented additions', () => {
    const doc = fs.readFileSync(DESIGN_DOC, 'utf-8')
    const table41 = section(doc, '### 4.1 方法（前端 → daemon）', '### 4.2 通知')
    // §4.2's own header line is where session.subscribe/unsubscribe are declared
    // (not as a §4.1 table row) — see rpcMethods.ts's doc comment.
    const header42 = section(doc, '### 4.2 通知', '\n\n')

    const expected = new Set([
      ...extractMethodNames(table41),
      ...extractMethodNames(header42),
      // 02-w3-interfaces.md §2 (issue #12, FR06 回放) — the two methods this
      // branch added on top of the original §4.1 table.
      'run.list',
      'run.payload'
    ])

    expect(new Set(RPC_V0_METHODS)).toEqual(expected)
  })

  it('has no duplicate entries', () => {
    expect(RPC_V0_METHODS.length).toBe(new Set(RPC_V0_METHODS).size)
  })
})
