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
  daemonProc.on("exit", (code) => {
    console.log(`[spike] daemon exited code=${code}`);
  });
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
  if (daemonProc) daemonProc.kill("SIGTERM");
  app.quit();
});
