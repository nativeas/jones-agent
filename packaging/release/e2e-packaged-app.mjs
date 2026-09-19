// e2e against the REAL packaged .app (docs/design/05-w6-interfaces.md §3.2:
// "make check + smoke 在打包产物上再跑一次，走 e2e"). Unlike
// apps/desktop/scripts/e2e.mjs — which imports the built `out/main/index.js`
// in-process, so `app.isPackaged` is always false there — this drives the
// actual `.app` binary as a separate OS process via `--remote-debugging-port`
// (a standard Chromium/Electron flag, works on a packaged app with no code
// changes needed) and CDP over a plain WebSocket, using only Node builtins
// (`fetch`, global `WebSocket`) — no puppeteer/playwright dependency added.
// Same scenario as scripts/e2e.mjs: project.list → session.create →
// session.send with no provider Key configured → assert an explicit
// provider_error card appears (PRD FR14/G08, N07/N09), now proven against
// `app.isPackaged === true` and the packaged daemon spawn path
// (findPackagedDaemonExecutable() in apps/desktop/src/main/index.ts).
//
// Usage: node e2e-packaged-app.mjs <path-to-.app>

import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const appPath = process.argv[2]
if (!appPath) {
  console.error('usage: e2e-packaged-app.mjs <path-to-.app>')
  process.exit(2)
}
const productName = path.basename(appPath, '.app')
const binary = path.join(appPath, 'Contents', 'MacOS', productName)
if (!fs.existsSync(binary)) {
  console.error(`FAIL: no executable at ${binary}`)
  process.exit(1)
}

const PORT = 9331
const jonesHome = fs.mkdtempSync(path.join(os.tmpdir(), 'jones-packaged-e2e-'))
let appProc = null
let daemonPid = null
let exited = false

function log(msg) {
  console.log(`[packaged-e2e] ${msg}`)
}

function waitForPidExit(pid, timeoutMs) {
  return new Promise((resolve) => {
    const deadline = Date.now() + timeoutMs
    const check = () => {
      try {
        process.kill(pid, 0)
      } catch {
        resolve()
        return
      }
      if (Date.now() > deadline) return resolve()
      setTimeout(check, 100)
    }
    check()
  })
}

async function cleanup(code) {
  if (exited) return
  exited = true
  try {
    if (!daemonPid) {
      const pidFile = path.join(jonesHome, 'runtime', 'daemon.pid')
      if (fs.existsSync(pidFile)) daemonPid = parseInt(fs.readFileSync(pidFile, 'utf-8').trim(), 10)
    }
    // Detached (design: daemon outlives Electron) — killing appProc below
    // does not also kill it, same as packaging/release/smoke-packaged-app.sh.
    if (daemonPid) {
      process.kill(daemonPid, 'SIGTERM')
      await waitForPidExit(daemonPid, 5000)
    }
  } catch {
    // best-effort
  }
  try {
    if (appProc) {
      appProc.kill('SIGTERM')
      await waitForPidExit(appProc.pid, 5000)
    }
  } catch {
    // best-effort
  }
  try {
    fs.rmSync(jonesHome, { recursive: true, force: true })
  } catch {
    // best-effort
  }
  process.exit(code)
}

function fail(msg) {
  console.error(`[packaged-e2e] FAIL: ${msg}`)
  void cleanup(1)
}

const timer = setTimeout(() => fail('timed out after 60s'), 60000)
timer.unref?.()

async function waitFor(predicate, { timeoutMs = 20000, intervalMs = 300 } = {}) {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    const value = await predicate().catch(() => undefined)
    if (value) return value
    if (Date.now() > deadline) return undefined
    await new Promise((r) => setTimeout(r, intervalMs))
  }
}

async function main() {
log(`launching ${binary} (JONES_HOME=${jonesHome}, CDP port ${PORT})`)
appProc = spawn(binary, [`--remote-debugging-port=${PORT}`], {
  env: { ...process.env, JONES_HOME: jonesHome },
  stdio: 'ignore'
})
appProc.on('exit', (code, signal) => {
  if (!exited) fail(`app process exited early (code=${code} signal=${signal})`)
})

const versionInfo = await waitFor(async () => {
  const res = await fetch(`http://127.0.0.1:${PORT}/json/version`)
  return res.ok ? await res.json() : undefined
})
if (!versionInfo) return fail('CDP endpoint never came up (--remote-debugging-port)')
log(`CDP up: ${versionInfo.Browser}`)

const target = await waitFor(async () => {
  const res = await fetch(`http://127.0.0.1:${PORT}/json/list`)
  const list = await res.json()
  return list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl)
})
if (!target) return fail('no page target ever appeared on the CDP endpoint')

const ws = new WebSocket(target.webSocketDebuggerUrl)
await new Promise((resolve, reject) => {
  ws.addEventListener('open', resolve, { once: true })
  ws.addEventListener('error', reject, { once: true })
})

let nextId = 1
const pending = new Map()
ws.addEventListener('message', (ev) => {
  const msg = JSON.parse(ev.data)
  if (msg.id && pending.has(msg.id)) {
    pending.get(msg.id)(msg)
    pending.delete(msg.id)
  }
})
function cdp(method, params = {}) {
  const id = nextId++
  return new Promise((resolve) => {
    pending.set(id, resolve)
    ws.send(JSON.stringify({ id, method, params }))
  })
}
async function ev(expression) {
  const res = await cdp('Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true
  })
  if (res.result?.exceptionDetails) {
    throw new Error(`evaluate failed: ${JSON.stringify(res.result.exceptionDetails)}`)
  }
  return res.result?.result?.value
}

await cdp('Runtime.enable')
await cdp('Page.enable')
await waitFor(async () => (await ev('document.readyState')) === 'complete')

const gotButton = await waitFor(() => ev(`!!document.querySelector('.project-group__new-session')`))
if (!gotButton) {
  const body = await ev('document.body.innerText').catch(() => '<unreadable>')
  return fail(`left pane never showed "+ 新建会话" — body: ${String(body).slice(0, 500)}`)
}
await ev(`document.querySelector('.project-group__new-session').click()`)

const gotInput = await waitFor(() => ev(`!!document.querySelector('.input-bar__textarea')`))
if (!gotInput) return fail('session.create never produced a selected session with an input bar')

const sessionId = await ev(`(async () => {
  const res = await window.jones.rpc.call('session.list', {})
  const s = (res.result || []).find((x) => !x.is_main)
  return s ? s.id : null
})()`)
if (!sessionId) return fail('could not find the newly created (non-main) session via session.list')
await ev(`window.jones.rpc.call('turn.messages', { session_id: ${JSON.stringify(sessionId)}, limit: 200 })`)

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
  const body = await ev('document.body.innerText').catch(() => '<unreadable>')
  return fail(`no .termination-card--error appeared — body: ${String(body).slice(0, 800)}`)
}
if (!errorCard.includes('provider_error')) {
  return fail(`error card doesn't mention provider_error — got: ${JSON.stringify(errorCard)}`)
}

clearTimeout(timer)
log('OK: packaged .app — project.list → session.create → session.send with no provider Key shows an explicit provider_error card')
await cleanup(0)
}

main().catch((err) => fail(`unhandled error: ${err instanceof Error ? (err.stack ?? err.message) : String(err)}`))
