#!/usr/bin/env node
const { existsSync } = require("node:fs");
const { join } = require("node:path");
const { spawnSync } = require("node:child_process");
const { createInterface } = require("node:readline/promises");

async function ask(question) {
  if (!process.stdin.isTTY || !process.stdout.isTTY) return null;
  const input = createInterface({ input: process.stdin, output: process.stdout });
  try { return (await input.question(question)).trim().toLowerCase(); }
  finally { input.close(); }
}

async function startMenuBar(options = {}) {
  const run = options.run || spawnSync;
  const exists = options.exists || existsSync;
  const prompt = options.prompt || ask;
  const root = options.root || join(__dirname, "..");
  const bundle = join(root, "dist", "Claude Cost Lens.app");
  const ready = () => [
    ["xcode-select", ["-p"]],
    ["xcrun", ["--find", "swiftc"]],
    ["xcrun", ["--sdk", "macosx", "--show-sdk-path"]]
  ].every(([command, args]) => run(command, args, { stdio: "ignore" }).status === 0);

  if (!exists(bundle)) {
    while (!ready()) {
      console.log("首次启动菜单栏需要 Xcode 命令行工具，无需安装完整 Xcode。\n1) 安装命令行工具\n2) 仅使用 Web 面板\n0) 退出");
      const answer = await prompt("请选择 [1/2/0]：");
      if (answer === null) {
        console.error("当前终端无法交互。仅使用 Web 请运行：npx claude-cost-lens --web");
        return { code: 1 };
      }
      if (answer === "2" || answer === "w" || answer === "web") return { web: true };
      if (answer === "0" || answer === "q") return { code: 0 };
      if (answer !== "1") continue;
      const installer = run("xcode-select", ["--install"], { stdio: "inherit" });
      if (installer.error || installer.status !== 0) {
        console.error("未能打开系统安装程序。可以选择 Web，或安装完成后重新运行此命令。");
        continue;
      }
      const after = await prompt("请在系统窗口完成安装，然后按 Enter 重试；输入 w 使用 Web，q 退出：");
      if (after === "w" || after === "web") return { web: true };
      if (after === null || after === "q") return { code: 0 };
    }
    const python = options.findPython();
    if (!python) return { code: 1 };
    console.log("正在准备菜单栏程序，首次启动需要编译…");
    const built = run(python.command, [...python.args, join(root, "scripts", "build_macos.py")], { cwd: root, stdio: "inherit" });
    if (built.error || built.status !== 0) {
      const fallback = await prompt("菜单栏构建失败。输入 w 仅使用 Web，其他输入退出：");
      return fallback === "w" || fallback === "web" ? { web: true } : { code: 1 };
    }
  }
  const opened = run("open", [bundle], { stdio: "inherit" });
  if (!opened.error && opened.status === 0) console.log("Claude Cost Lens 已在菜单栏启动。点击图标可查看统计或打开完整网页面板。");
  return { code: opened.status ?? 1 };
}

module.exports = { startMenuBar };
if (require.main === module) {
  const { spawn } = require("node:child_process");
  const cli = spawn(process.execPath, [join(__dirname, "claude-cost-lens.cjs"), "--menubar"], { stdio: "inherit" });
  cli.on("error", error => { console.error(error.message); process.exitCode = 1; });
  cli.on("exit", code => { process.exitCode = code ?? 1; });
}
