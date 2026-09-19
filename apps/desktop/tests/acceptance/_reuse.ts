/**
 * Shared helper for `apps/desktop/tests/acceptance/` (Issue #24,
 * docs/design/05-w6-interfaces.md §2): "已有测试若已覆盖某条，写一个薄的
 * acceptance 用例引用或复用它，不重复造。"
 *
 * Mirrors `daemon/tests/acceptance/_reuse.py`'s approach: each acceptance
 * test here re-runs a curated list of existing `src/**\/__tests__/*.test.tsx?`
 * files as a real subprocess `vitest run` invocation, under its own PRD-cited,
 * G/N-numbered test name, instead of re-deriving the same render/assertion
 * logic against another component's private fixtures.
 */
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const DESKTOP_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..')

/** Run the given vitest test files (paths relative to `apps/desktop/`) as a
 * subprocess and throw with vitest's own output if any fail, error, or fail
 * to collect. */
export function runExisting(...files: string[]): void {
  try {
    execFileSync(
      'pnpm',
      ['exec', 'vitest', 'run', '--reporter=dot', ...files],
      { cwd: DESKTOP_ROOT, stdio: 'pipe', encoding: 'utf-8', timeout: 120_000 }
    )
  } catch (err) {
    const e = err as { stdout?: string; stderr?: string; message?: string }
    throw new Error(
      `reused acceptance coverage did not pass: ${files.join(', ')}\n` +
        `--- stdout ---\n${e.stdout ?? ''}\n--- stderr ---\n${e.stderr ?? e.message ?? ''}`
    )
  }
}
