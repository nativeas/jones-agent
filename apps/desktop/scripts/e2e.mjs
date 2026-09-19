// Real end-to-end test (02-w3-interfaces.md §2 集成收口 #4, issue #12): a real
// daemon (spawned by the REAL main/index.ts recovery path — this script never
// hand-rolls its own `uv run python -m jones_daemon`, it imports the actual
// built `out/main/index.js` and lets its own `ensureDaemonRunning()` do exactly
// what it does for a real user on a machine where the daemon isn't running yet)
// + a real Electron window, driven through project.list → session.create →
// session.send with no provider Key configured anywhere (`JONES_HOME` points at
// a brand-new empty temp directory — no `secrets/vault.enc`, so every vendor's
// `has_key` is false) — asserts the UI ends up showing an explicit error card
// naming the provider problem, not a blank/stuck screen (PRD FR14/G08, N07/N09).
//
// Deliberately does NOT set ANTHROPIC_API_KEY/etc — the whole point is
// exercising the no-Key path (`_run_turn`'s provider pre-flight check,
// sessions/service.py).
//
// `UV_FROZEN=1` (see docs/DEV.md, docs/design/01-w2-interfaces.md §2.2): the
// daemon's `worker` dependency group points at a machine-local hermes-agent
// checkout that doesn't exist on most machines/CI — this script's `uv sync`
// (below, to make sure daemon deps are actually installed before main.js tries
// to spawn `uv run python -m jones_daemon`) and the spawned daemon's own `uv
// run` both need it, or `uv` re-resolves the full lock (including `worker`)
// and fails even though nothing here ever needs that group.
//
// Structural note (cost a long debugging session, written down so it isn't
// re-discovered the hard way): every await in this file that can resolve only
// *after* Electron's 'ready' fires must happen inside a `app.whenReady().then()`
// continuation, never as a top-level `await` chained directly off this module's
// own evaluation. A top-level `await app.whenReady()` (or an `await`-chained
// polling loop reaching for `BrowserWindow` before 'ready') reproducibly starves
// 'ready' from ever firing at all under Electron 44.4.2's ESM entry handling —
// confirmed by isolating it to a bare `await app.whenReady()` with nothing else
// in the file, which hangs indefinitely, while the exact same wait expressed as
// `app.whenReady().then(cb)` (the pattern main/index.ts and scripts/smoke.mjs
// both already use) resolves in well under a second. `main.js`'s own
// `app.whenReady().then(() => { createWindow(); ... })` is exactly this safe
// pattern — this file only needs to mirror it for its *own* continuation too.

import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = path.resolve(__dirname, '../../..')
const DAEMON_DIR = path.join(REPO_ROOT, 'daemon')

const TIMEOUT_MS = 180000
const jonesHome = fs.mkdtempSync(path.join(os.tmpdir(), 'jones-e2e-'))

function fail(message) {
  console.error(`[e2e] FAIL: ${message}`)
  cleanupAndExit(1)
}

function pass(message) {
  console.log(`[e2e] OK: ${message}`)
  cleanupAndExit(0)
}

let daemonPid = null
let exited = false

/** Resolves once `pid` no longer exists, or after `timeoutMs` — never rejects
 * (a `kill(pid, 0)` probe that fails because it's already gone is the success
 * case here, not an error to propagate). */
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
  if (exited) return // fail()/pass() must be idempotent — a later awaited step
  exited = true // can still resolve after an earlier one already decided the outcome
  // The daemon is designed to outlive Electron (00-foundation.md §3) — real
  // production behavior does NOT kill it on quit, but a test must clean up
  // after itself or every CI run leaks one more orphaned daemon process
  // pinned to a now-abandoned temp JONES_HOME. `daemon.pid` is the daemon's
  // own documented PID file (paths.py::pid_file()), not something this script
  // invented a side channel for. Waiting for it to actually exit (not just
  // sending the signal) matters: `fs.rmSync` below can otherwise race a still
  // very-much-alive daemon that keeps re-creating files under `jonesHome`
  // (logs, runtime/) faster than the recursive removal completes, leaking the
  // temp directory (observed in practice — this isn't a hypothetical).
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
    // best-effort cleanup only — never let a cleanup failure mask the real
    // pass/fail result below
  }
  try {
    fs.rmSync(jonesHome, { recursive: true, force: true })
  } catch {
    // same as above
  }
  globalThis.__jonesE2eApp?.exit(code)
}

const timer = setTimeout(() => fail(`timed out after ${TIMEOUT_MS}ms`), TIMEOUT_MS)
timer.unref?.()

// Make sure the daemon's own deps are actually installed before main.js's
// ensureDaemonRunning() tries to spawn it — a fresh checkout with no
// `daemon/.venv` would otherwise fail this test over a setup step, not the
// behavior it's meant to verify.
const sync = spawnSync('uv', ['sync'], {
  cwd: DAEMON_DIR,
  env: { ...process.env, UV_FROZEN: '1' },
  stdio: 'inherit'
})
if (sync.status !== 0) fail('uv sync (daemon deps) failed — see output above')

process.env.JONES_HOME = jonesHome
process.env.UV_FROZEN = '1'

const { app, BrowserWindow } = await import('electron')
globalThis.__jonesE2eApp = app

// Importing the real built main process module runs its real startup sequence
// (createWindow() + ensureDaemonRunning() + heartbeat, all wired off main.js's
// own `app.whenReady().then(...)`) — see this file's header for why that's
// deliberate, not a shortcut.
await import(path.join(__dirname, '../out/main/index.js'))

async function waitFor(predicate, { timeoutMs = 20000, intervalMs = 200 } = {}) {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    const value = await predicate()
    if (value) return value
    if (Date.now() > deadline) return undefined
    await new Promise((r) => setTimeout(r, intervalMs))
  }
}

// Everything from here on can only resolve after Electron's 'ready' fires
// (BrowserWindow instances, webContents) — it MUST run as a `.then()`
// continuation off `app.whenReady()`, not as more top-level `await` chained
// directly onto this module's own evaluation. See the header note.
app
  .whenReady()
  .then(async () => {
    const win = await waitFor(() => BrowserWindow.getAllWindows()[0], { timeoutMs: 45000 })
    if (!win) return fail('no BrowserWindow ever appeared')

    await waitFor(() => win.webContents.isLoading() === false, { timeoutMs: 15000 })

    const ev = (js) => win.webContents.executeJavaScript(js)

    // 1. project.list → session.create: click the default Project's "+ 新建会话"
    //    button once the left pane has rendered it (LeftPane.tsx / sessionsStore.ts
    //    — both call project.list/session.list on mount).
    const gotNewSessionButton = await waitFor(
      () => ev(`!!document.querySelector('.project-group__new-session')`),
      { timeoutMs: 20000 }
    )
    if (!gotNewSessionButton) {
      const bodyText = await ev('document.body.innerText').catch(() => '<could not read body>')
      return fail(
        `left pane never showed a "+ 新建会话" button (project.list/session.list never resolved?) — body: ${bodyText.slice(0, 500)}`
      )
    }
    await ev(`document.querySelector('.project-group__new-session').click()`)

    const gotInputBar = await waitFor(
      () => ev(`!!document.querySelector('.input-bar__textarea')`),
      { timeoutMs: 20000 }
    )
    if (!gotInputBar) return fail('session.create never produced a selected session with an input bar')

    // Give the newly bound session's own initial `turn.messages`/`session.get`
    // fetches (chatStore.ts's `bindSession()`) a moment to actually settle
    // before sending — sending immediately can race a still-in-flight initial
    // `turn.messages` call so its response (fetched a moment *after* this Turn's
    // own Message row already exists) resolves to include the just-sent
    // message too, alongside the optimistic local echo `send()` already added.
    // Real users don't type+send within single-digit milliseconds of a session
    // appearing; this wait keeps this test on the same "normal use" timing
    // real usage exercises, not a synthetic edge race this issue's scope
    // doesn't own (see the PR report's "评审关注点" for the underlying frontend
    // bug this race would otherwise expose).
    await new Promise((r) => setTimeout(r, 1000))

    // 2. session.send — the daemon has no provider Key configured anywhere in
    //    this fresh JONES_HOME, so `_run_turn`'s provider pre-flight check
    //    (sessions/service.py) must terminate this Run with a provider_error
    //    reason before ever trying to spawn a worker subprocess.
    await ev(`(() => {
      const ta = document.querySelector('.input-bar__textarea')
      const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set
      setter.call(ta, 'hello')
      ta.dispatchEvent(new Event('input', { bubbles: true }))
    })()`)
    await ev(`document.querySelectorAll('.input-bar__actions button')[0].click()`)

    const errorCard = await waitFor(
      () =>
        ev(`(() => {
          const el = document.querySelector('.termination-card--error')
          return el ? el.innerText : null
        })()`),
      { timeoutMs: 30000 }
    )

    if (!errorCard) {
      const bodyText = await ev('document.body.innerText').catch(() => '<could not read body>')
      return fail(
        `no .termination-card--error ever appeared (blank/stuck screen instead of an explicit error) — body: ${bodyText.slice(0, 800)}`
      )
    }
    if (!errorCard.includes('provider_error')) {
      return fail(`error card appeared but doesn't mention provider_error — got: ${JSON.stringify(errorCard)}`)
    }

    const bodyNotBlank = await ev('document.body.innerText.length > 0')
    if (!bodyNotBlank) return fail('document.body is empty after the error card should have rendered')

    clearTimeout(timer)
    pass(
      'project.list → session.create → session.send with no provider Key shows an explicit provider_error card, not a blank screen'
    )
  })
  .catch((err) => fail(`unhandled error: ${err instanceof Error ? (err.stack ?? err.message) : String(err)}`))
