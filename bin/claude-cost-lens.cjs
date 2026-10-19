#!/usr/bin/env node

if (!["darwin", "win32"].includes(process.platform)) {
  console.error("Claude Cost Lens supports macOS and Windows only. Linux is not supported.");
  process.exit(1);
}

const { existsSync } = require("node:fs");
const { dirname, join } = require("node:path");
const { spawn, spawnSync } = require("node:child_process");

const packageRoot = join(dirname(__filename), "..");
const appPath = join(packageRoot, "app.py");

function findPython() {
  const candidates = process.env.CLAUDE_USAGE_PYTHON
    ? [{ command: process.env.CLAUDE_USAGE_PYTHON, args: [] }]
    : process.platform === "win32"
      ? [
          { command: "py", args: ["-3"] },
          { command: "python", args: [] },
          { command: "python3", args: [] }
        ]
      : [
          { command: "python3", args: [] },
          { command: "python", args: [] }
        ];

  for (const candidate of candidates) {
    const result = spawnSync(candidate.command, [...candidate.args, "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)"], { stdio: "ignore" });
    if (!result.error && result.status === 0) return candidate;
  }
  return null;
}

if (!existsSync(appPath)) {
  console.error(`Claude Cost Lens: bundled app.py was not found at ${appPath}`);
  process.exit(1);
}

function requirePython() {
  const python = findPython();
  if (!python) console.error("Claude Cost Lens requires Python 3.8 or newer. Install Python and run this command again.");
  return python;
}

async function main() {
  let args = process.argv.slice(2);
  const menu = args.includes("--menubar");
  const web = args.includes("--web");
  const tray = args.includes("--tray");
  if (tray && (web || menu || args.length !== 1)) {
    console.error("Use --tray alone, or --web with dashboard options.");
    process.exitCode = 1; return;
  }
  if (tray && process.platform !== "win32") {
    console.error("The system tray app requires Windows.");
    process.exitCode = 1; return;
  }
  if (menu && (web || args.length !== 1)) {
    console.error("Use --menubar alone, or --web with dashboard options.");
    process.exitCode = 1;
    return;
  }
  if (menu && process.platform !== "darwin") {
    console.error("The menu bar app requires macOS. Use claude-cost-lens --web.");
    process.exitCode = 1;
    return;
  }
  if (process.platform === "win32" && (tray || args.length === 0)) {
    const result = await require("./windows-tray.cjs").startWindowsTray({ findPython: requirePython });
    process.exitCode = result.code; return;
  }
  if (process.platform === "darwin" && (menu || args.length === 0)) {
    const result = await require("./menubar.cjs").startMenuBar({ findPython: requirePython });
    if (!result.web) { process.exitCode = result.code; return; }
    args = ["--open"];
  } else {
    args = args.filter(arg => arg !== "--web");
    if ((web || args.length === 0) && !args.includes("--once") && !args.includes("--open")) args.push("--open");
  }
  if (args.includes("--help") || args.includes("-h")) {
    console.log("Claude Cost Lens\n  (no arguments)  Start the macOS menu bar app or Windows system tray.\n  --web           Start only the Web dashboard and open a browser.\n  --tray          Explicitly start the Windows system tray.\n  --menubar       Explicitly start the macOS menu bar app.\n  --once          Print the usage summary without starting an interface.\n");
  }
  const python = requirePython();
  if (!python) { process.exitCode = 1; return; }
  const child = spawn(python.command, [...python.args, appPath, ...args], {
    cwd: packageRoot,
    stdio: "inherit"
  });
  for (const signal of ["SIGINT", "SIGTERM"]) {
    process.once(signal, () => child.kill(signal));
  }
  child.on("error", error => {
    console.error(`Unable to start Python: ${error.message}`);
    process.exitCode = 1;
  });
  child.on("exit", (code, signal) => {
    if (signal) {
      process.removeAllListeners(signal);
      process.kill(process.pid, signal);
    } else { process.exitCode = code ?? 1; }
  });
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
