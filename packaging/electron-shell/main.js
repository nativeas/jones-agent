// 最小 Electron 壳：验证 extraResources 里的 daemon 可执行文件路径正确、能被 spawn。
// 不是真实 apps/desktop 的实现，只用于打包 spike。
const { app, BrowserWindow } = require("electron");
const { spawn } = require("node:child_process");
const path = require("node:path");
const fs = require("node:fs");

function resolveDaemonPath() {
  // 打包后：Contents/Resources/daemon/run.sh（python-build-standalone 方案，见
  // packaging/standalone/build.sh；electron-builder.yml 里 extraResources 按 ${arch} 选对应产物）。
  // 开发模式：直接指向 ../standalone/dist/<当前架构>/run.sh。
  const packaged = path.join(process.resourcesPath, "daemon", "run.sh");
  if (fs.existsSync(packaged)) return packaged;
  const devArch = process.arch === "arm64" ? "arm64" : "x64";
  return path.join(__dirname, "..", "standalone", "dist", devArch, "run.sh");
}

let daemonProc = null;

function spawnDaemon() {
  const exe = resolveDaemonPath();
  daemonProc = spawn(exe, [], {
    env: { ...process.env, JONES_SPIKE_HOME: path.join(app.getPath("userData"), "spike-runtime") },
    stdio: "inherit",
  });
  // 没有 'error' 监听时，spawn 目标不存在会在 Electron 主进程里变成未捕获异常
  // （已实测：node 22 对不存在的可执行文件抛 Unhandled 'error' event，进程崩溃）。
  // 这正是本 spike 要验证的失败模式之一（daemon 路径在打包后失效），必须捕获成
  // 可见的错误而不是让 app 直接崩掉、把「daemon 路径错了」误报成「app 崩了」。
  daemonProc.on("error", (err) => {
    console.error(`[spike] failed to spawn daemon at ${exe}: ${err.message}`);
    daemonProc = null;
  });
  daemonProc.on("exit", (code, signal) => {
    console.log(`[spike] daemon exited code=${code} signal=${signal}`);
    daemonProc = null;
  });
  // run.sh 用 exec 拉起 python（见 packaging/standalone/build.sh），exec 不 fork，
  // 所以这个 pid 就是 daemon 进程本身的 pid——验证时应该拿它去和 pong 响应里的
  // pid 字段比对，确认连上的是这一次刚 spawn 的实例，而不是上一轮遗留的旧 daemon
  // （见 docs/spikes/02-packaging.md §2 与「如何验证」一节）。
  console.log(`[spike] spawned daemon pid=${daemonProc.pid} exe=${exe}`);
}

function stopDaemon() {
  if (daemonProc) {
    daemonProc.kill("SIGTERM");
    daemonProc = null;
  }
}

function createWindow() {
  const win = new BrowserWindow({ width: 480, height: 320, title: "Jones packaging spike" });
  win.loadURL(
    "data:text/html,<body style='font-family:-apple-system;padding:2em'><h2>Jones packaging spike</h2><p>daemon spawned, check console for pong test.</p></body>"
  );
}

app.whenReady().then(() => {
  spawnDaemon();
  createWindow();
});

app.on("window-all-closed", () => {
  stopDaemon();
  app.quit();
});

// Cmd+Q / app.quit() 走 will-quit，不会先发 window-all-closed（Electron 文档明确的行为）。
// 只挂 window-all-closed 会让 Cmd+Q 退出时 daemon 变成孤儿进程——spawn 没有 detached，
// 但 Node 不会在父进程退出时自动杀子进程。这里两条路径都挂，stopDaemon 本身是幂等的。
app.on("will-quit", () => {
  stopDaemon();
});
