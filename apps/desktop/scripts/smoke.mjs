// Real end-to-end smoke test: launches the actual built app (out/main, out/preload,
// out/renderer) as a real Electron process, waits for the window to load, then checks
// from inside the renderer that the sandboxed CJS preload (see electron.vite.config.ts)
// actually exposed `window.jones.rpc.call`. This is the one check that unit/vitest
// tests cannot give us: they run in jsdom, never through Electron's real sandboxed
// preload loader, so a preload that Electron itself refuses to load (wrong format,
// wrong path) would still show green there.
//
// Requires a real Electron binary (`node_modules/electron/dist`) — the first run
// downloads it via electron's postinstall script; that's expected and allowed.
//
// Deliberately does NOT require a running daemon: it only asserts the preload
// bridge shape, not an actual RPC round-trip (design §3 already covers daemon
// unreachability via ensureDaemonRunning()'s own retry/timeout path, exercised by
// the vitest suite).

import { app, BrowserWindow } from 'electron'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

const TIMEOUT_MS = 20000

function fail(message) {
  console.error(`[smoke] FAIL: ${message}`)
  app.exit(1)
}

const timer = setTimeout(() => fail('timed out waiting for the window to load'), TIMEOUT_MS)

app.whenReady().then(async () => {
  const win = new BrowserWindow({
    width: 800,
    height: 600,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, '../out/preload/index.cjs'),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true
    }
  })

  try {
    // Loading the real renderer bundle mounts <DaemonStatusCard>, which fires an
    // `ipcRenderer.invoke('rpc:call', ...)` on mount. This script only stands up a
    // BrowserWindow (not the real main/index.ts), so nothing has `ipcMain.handle`'d
    // that channel — Electron logs "No handler registered for 'rpc:call'" to this
    // process's stderr. That's expected and harmless here: this test only asserts
    // the preload bridge shape (window.jones.rpc.call being a function), not an
    // actual RPC round-trip.
    await win.loadFile(path.join(__dirname, '../out/renderer/index.html'))
    const bridgeType = await win.webContents.executeJavaScript(
      'typeof (window.jones && window.jones.rpc && window.jones.rpc.call)'
    )
    clearTimeout(timer)
    if (bridgeType !== 'function') {
      fail(`window.jones.rpc.call is ${bridgeType}, expected "function" — sandboxed CJS preload did not load`)
      return
    }
    console.log('[smoke] OK: window.jones.rpc.call is a function (sandboxed CJS preload loaded)')
    app.exit(0)
  } catch (err) {
    clearTimeout(timer)
    fail(err instanceof Error ? err.stack || err.message : String(err))
  }
})

app.on('window-all-closed', () => {
  // Smoke test controls its own exit via app.exit() above; don't let the default
  // window-all-closed handling race with that.
})
