const fs = require("node:fs");
const { join } = require("node:path");
const { tmpdir } = require("node:os");
const { spawn, spawnSync } = require("node:child_process");

async function startWindowsTray(options = {}) {
  const io = options.fs || fs;
  const run = options.run || spawnSync;
  const launch = options.spawn || spawn;
  const wait = options.wait || (ms => new Promise(resolve => setTimeout(resolve, ms)));
  const root = options.root || join(__dirname, "..");
  const python = options.findPython();
  if (!python) return { code: 1 };
  // Resolve py.exe to the actual interpreter so the job owns Python directly.
  const interpreter = run(python.command, [...python.args, "-c", "import sys; print(sys.executable)"], { encoding: "utf8", env: { ...process.env, PYTHONIOENCODING: "utf-8" }, stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
  const pythonPath = interpreter.stdout?.trim();
  if (interpreter.error || interpreter.status !== 0 || !pythonPath) {
    console.error("无法确定 Python 解释器路径，请检查 Python 安装。");
    return { code: 1 };
  }
  const powershell = options.powershell || join(process.env.SystemRoot || "C:\\Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
  const probe = run(powershell, ["-NoProfile", "-NonInteractive", "-Command", "if ($PSVersionTable.PSVersion.Major -lt 5) { exit 1 }"], { stdio: "ignore", windowsHide: true });
  if (probe.error || probe.status !== 0) {
    console.error("Windows 托盘需要 Windows PowerShell 5.1 和 .NET Framework。仅使用网页请运行：npx claude-cost-lens --web");
    return { code: 1 };
  }
  const runtime = io.mkdtempSync(join(options.tempRoot || tmpdir(), "claude-cost-lens-"));
  const statusPath = join(runtime, "status.json");
  const configPath = join(runtime, "config.json");
  let child;
  let childError;
  let exited = false;
  try {
    io.writeFileSync(configPath, JSON.stringify({
      PythonCommand: pythonPath, PythonArgs: [],
      AppPath: join(root, "app.py"), StatusPath: statusPath
    }), { encoding: "utf8", mode: 0o600 });
    child = launch(powershell, ["-NoLogo", "-NoProfile", "-NonInteractive", "-STA", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", join(root, "windows", "tray.ps1"), "-ConfigPath", configPath], {
      cwd: root, detached: true, windowsHide: true, stdio: "ignore"
    });
    child.on("error", error => { childError = error; });
    child.on("exit", () => { exited = true; });
    for (let attempt = 0; attempt < 300; attempt++) {
      let status;
      try { status = JSON.parse(io.readFileSync(statusPath, "utf8").replace(/^\uFEFF/, "")); } catch { /* not ready or write in progress */ }
      if (status?.state === "ready" || status?.state === "already_running") {
        child.unref();
        console.log(status.state === "already_running" ? "Claude Cost Lens 已在系统托盘运行。" : "Claude Cost Lens 已启动。点击右下角托盘图标查看用量或打开完整面板。");
        return { code: 0 };
      }
      if (status?.state === "error") throw new Error(status.message || "托盘启动失败");
      if (childError) throw childError;
      if (exited) throw new Error("Windows 托盘进程在准备完成前退出");
      await wait(200);
    }
    throw new Error("Windows 托盘启动超时");
  } catch (error) {
    if (child && !exited) child.kill();
    console.error(`${error.message}。仅使用网页请运行：npx claude-cost-lens --web`);
    return { code: 1 };
  } finally {
    try { io.rmSync(runtime, { recursive: true, force: true }); } catch { /* app-owned temporary files only */ }
  }
}

module.exports = { startWindowsTray };
