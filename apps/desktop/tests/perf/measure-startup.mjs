// Real end-to-end startup timing (docs/design/05-w6-interfaces.md §3.1: "窗口可见
// ≤ 1.5s、可输入 ≤ 3s（Electron 启动计时，复用 smoke 的启动路径）、Electron RSS"),
// against PRD 11.1's "桌面壳可见 ≤ 1.5s" / "可开始输入 ≤ 3s" and 11.2's "Electron
// 壳（Main + Renderer）≤ 300MB".
//
// "复用 smoke 的启动路径" here means `scripts/e2e.mjs`'s pattern specifically (not
// `scripts/smoke.mjs`, which stands up its own bare BrowserWindow + stub IPC
// handler rather than the real main process) — this script imports the REAL built
// `out/main/index.js` in-process, the same way `scripts/e2e.mjs` does, so
// `createWindow()` + `ensureDaemonRunning()` + the real daemon-lifecycle retry
// sequencing all run exactly as they would for a real user, against a real daemon
// (isolated `JONES_HOME`, same "uv sync then spawn" dev-mode path `e2e.mjs` uses).
// See that file's header comment for the ESM/`app.whenReady()` ordering pitfall
// this script also has to respect.
//
// Window-visible timing: `BrowserWindow.prototype.show` is monkey-patched *before*
// `out/main/index.js` is imported, to capture the exact moment `win.show()` runs
// (main/index.ts's real code: `show: false` at construction, then
// `win.once('ready-to-show', () => win.show())`) — polling `BrowserWindow.
// getAllWindows()` after the fact (e2e.mjs's own idiom, fine at its ~200ms
// granularity for correctness-only assertions) is too coarse for a ≤1.5s budget.
//
// Input-ready timing: polls (25ms) for `.input-bar__textarea` in the real
// renderer's DOM via `executeJavaScript` — InputBar only mounts once the main
// Session is loaded and auto-selected (CenterPane.tsx / sessionsStore.ts), which
// is exactly PRD 11.1's "会话列表加载完成、输入框可用" condition.
//
// Electron RSS: `app.getAppMetrics()` summed over `type: 'Browser'` (main) and
// `type: 'Tab'` (renderer — Electron's `ProcessMetric.type` enum uses "Tab" for a
// window's renderer process, not "Renderer") — deliberately excludes GPU/Utility/
// Zygote helper processes, matching PRD 11.2's literal "Main + Renderer" wording.
//
// Writes into the SAME `docs/acceptance/v1.0/perf-<date>.json` `daemon/tests/perf`
// writes (merges into its `metrics` array, creating the file if the daemon suite
// hasn't run yet today) — 05-w6-interfaces.md §3.1's "参考机口径写进结果文件"
// describes one shared result file for both halves of G17, not two.

import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = path.resolve(__dirname, '../../../..')
const DAEMON_DIR = path.join(REPO_ROOT, 'daemon')
const REPORT_DIR = path.join(REPO_ROOT, 'docs', 'acceptance', 'v1.0')

const TIMEOUT_MS = 60000
const jonesHome = fs.mkdtempSync(path.join(os.tmpdir(), 'jones-perf-'))

// PRD 11.1 — 桌面壳可见 ≤ 1.5s / 可开始输入 ≤ 3s（守护进程已在运行的情况下）.
// This script's own `uv sync` + daemon spawn happen fully before `t0` is taken
// (see below), matching that precondition rather than folding a cold daemon
// start into the Electron-side numbers (`daemon/tests/perf/test_cold_start.py`
// already covers that separately).
const THRESHOLD_WINDOW_VISIBLE_MS = 1500
const THRESHOLD_INPUT_READY_MS = 3000
// PRD 11.2 — Electron 壳（Main + Renderer）≤ 300MB.
const THRESHOLD_ELECTRON_RSS_MB = 300

let exited = false
let daemonPid = null

function fail(message) {
  console.error(`[perf] FAIL: ${message}`)
  cleanupAndExit(1)
}

function waitForExit(pid, timeoutMs) {
  return new Promise((resolve) => {
    const deadline = Date.now() + timeoutMs
    const check = () => {
      try {
        process.kill(pid, 0)
      } catch {
        resolve()
        return
      }
      if (Date.now() > deadline) {
        resolve()
        return
      }
      setTimeout(check, 100)
    }
    check()
  })
}

async function cleanupAndExit(code) {
  if (exited) return
  exited = true
  try {
    if (!daemonPid) {
      const pidFile = path.join(jonesHome, 'runtime', 'daemon.pid')
      if (fs.existsSync(pidFile)) daemonPid = parseInt(fs.readFileSync(pidFile, 'utf-8').trim(), 10)
    }
    if (daemonPid) {
      process.kill(daemonPid, 'SIGTERM')
      await waitForExit(daemonPid, 5000)
    }
  } catch {
    // best-effort cleanup only
  }
  try {
    fs.rmSync(jonesHome, { recursive: true, force: true })
  } catch {
    // same as above
  }
  globalThis.__jonesPerfApp?.exit(code)
}

function machineInfo() {
  const cpus = os.cpus()
  return {
    platform: `${os.type()} ${os.release()} ${os.arch()}`,
    arch: os.arch(),
    chip: cpus.length > 0 ? cpus[0].model : null,
    memory_gb: Math.round((os.totalmem() / 1024 ** 3) * 10) / 10,
    is_prd_reference_machine: false,
    prd_reference_machines: ['Apple M1 / 16GB', 'Intel i5 12代 / 16GB']
  }
}

function mergePerfReport(newMetrics) {
  fs.mkdirSync(REPORT_DIR, { recursive: true })
  const dateStr = new Date().toISOString().slice(0, 10)
  const outPath = path.join(REPORT_DIR, `perf-${dateStr}.json`)
  let report
  if (fs.existsSync(outPath)) {
    report = JSON.parse(fs.readFileSync(outPath, 'utf-8'))
  } else {
    report = {
      date: dateStr,
      generated_by: 'daemon/tests/perf (pytest) + apps/desktop/tests/perf (electron)',
      machine: machineInfo(),
      idle_window_s: null,
      metrics: [],
      all_passed: true
    }
  }
  if (!report.generated_by.includes('apps/desktop/tests/perf')) {
    report.generated_by += ' + apps/desktop/tests/perf (electron)'
  }
  report.metrics.push(...newMetrics)
  report.all_passed = report.metrics.every((m) => m.passed)
  fs.writeFileSync(outPath, JSON.stringify(report, null, 2) + '\n', 'utf-8')
  console.log(`[perf] wrote ${outPath}`)
}

const timer = setTimeout(() => fail(`timed out after ${TIMEOUT_MS}ms`), TIMEOUT_MS)
timer.unref?.()

const sync = spawnSync('uv', ['sync'], {
  cwd: DAEMON_DIR,
  env: { ...process.env, UV_FROZEN: '1' },
  stdio: 'inherit'
})
if (sync.status !== 0) fail('uv sync (daemon deps) failed — see output above')

process.env.JONES_HOME = jonesHome
process.env.UV_FROZEN = '1'

const { app, BrowserWindow } = await import('electron')
globalThis.__jonesPerfApp = app

// Monkey-patch BEFORE importing the real main module, so the very first
// `win.show()` call it makes (`ready-to-show` → `win.show()`) is caught exactly
// when it happens, not discovered later by polling.
let windowVisibleAt = null
const origShow = BrowserWindow.prototype.show
BrowserWindow.prototype.show = function patchedShow(...args) {
  if (windowVisibleAt === null) windowVisibleAt = process.hrtime.bigint()
  return origShow.apply(this, args)
}

const t0 = process.hrtime.bigint()
await import(path.join(__dirname, '../../out/main/index.js'))

async function waitFor(predicate, { timeoutMs = 20000, intervalMs = 25 } = {}) {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    const value = await predicate()
    if (value) return { value, at: process.hrtime.bigint() }
    if (Date.now() > deadline) return { value: undefined, at: null }
    await new Promise((r) => setTimeout(r, intervalMs))
  }
}

function msSince(start, end) {
  return Number(end - start) / 1e6
}

app
  .whenReady()
  .then(async () => {
    const { value: win } = await waitFor(() => BrowserWindow.getAllWindows()[0], { timeoutMs: 45000 })
    if (!win) return fail('no BrowserWindow ever appeared')

    const gotShow = await waitFor(() => windowVisibleAt !== null, { timeoutMs: 15000 })
    if (!gotShow.value) return fail('window never called show() (ready-to-show never fired?)')
    const windowVisibleMs = msSince(t0, windowVisibleAt)

    await waitFor(() => win.webContents.isLoading() === false, { timeoutMs: 15000 })

    const ev = (js) => win.webContents.executeJavaScript(js)

    const gotInput = await waitFor(() => ev(`!!document.querySelector('.input-bar__textarea')`), {
      timeoutMs: 25000
    })
    if (!gotInput.value) {
      const bodyText = await ev('document.body.innerText').catch(() => '<could not read body>')
      return fail(
        `input bar never appeared (session list never loaded a main session?) — body: ${bodyText.slice(0, 500)}`
      )
    }
    const inputReadyMs = msSince(t0, gotInput.at)

    // Let things settle a beat before reading process metrics — a website/window
    // that just finished loading can have a transient memory spike that isn't
    // representative of steady state (same reasoning as the daemon-side idle
    // RSS test's settle sleep).
    await new Promise((r) => setTimeout(r, 500))
    const metricsRaw = app.getAppMetrics()
    const rssKb = metricsRaw
      .filter((m) => m.type === 'Browser' || m.type === 'Tab')
      .reduce((sum, m) => sum + (m.memory?.workingSetSize ?? 0), 0)
    const electronRssMb = rssKb / 1024

    const windowVisiblePassed = windowVisibleMs <= THRESHOLD_WINDOW_VISIBLE_MS
    const inputReadyPassed = inputReadyMs <= THRESHOLD_INPUT_READY_MS
    const rssPassed = electronRssMb <= THRESHOLD_ELECTRON_RSS_MB

    console.log(
      `[perf] window_visible_ms=${windowVisibleMs.toFixed(1)} (<= ${THRESHOLD_WINDOW_VISIBLE_MS}) ` +
        `input_ready_ms=${inputReadyMs.toFixed(1)} (<= ${THRESHOLD_INPUT_READY_MS}) ` +
        `electron_rss_mb=${electronRssMb.toFixed(1)} (<= ${THRESHOLD_ELECTRON_RSS_MB})`
    )

    mergePerfReport([
      {
        name: 'desktop_window_visible_ms',
        value: windowVisibleMs,
        unit: 'ms',
        threshold: THRESHOLD_WINDOW_VISIBLE_MS,
        threshold_source: 'PRD 11.1 桌面壳可见 ≤ 1.5s',
        passed: windowVisiblePassed,
        detail: {}
      },
      {
        name: 'desktop_input_ready_ms',
        value: inputReadyMs,
        unit: 'ms',
        threshold: THRESHOLD_INPUT_READY_MS,
        threshold_source: 'PRD 11.1 可开始输入 ≤ 3s（守护进程已在运行的情况下）',
        passed: inputReadyPassed,
        detail: {}
      },
      {
        name: 'desktop_electron_rss_mb',
        value: electronRssMb,
        unit: 'MB',
        threshold: THRESHOLD_ELECTRON_RSS_MB,
        threshold_source: 'PRD 11.2 Electron 壳（Main + Renderer）≤ 300MB',
        passed: rssPassed,
        detail: { processes: metricsRaw.map((m) => ({ type: m.type, rss_kb: m.memory?.workingSetSize })) }
      }
    ])

    clearTimeout(timer)
    if (!windowVisiblePassed || !inputReadyPassed || !rssPassed) {
      return fail('one or more perf thresholds exceeded — see [perf] line above and the JSON report')
    }
    console.log('[perf] OK: all thresholds passed')
    cleanupAndExit(0)
  })
  .catch((err) => fail(`unhandled error: ${err instanceof Error ? (err.stack ?? err.message) : String(err)}`))
