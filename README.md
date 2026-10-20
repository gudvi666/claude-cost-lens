# Claude Cost Lens

本地 Claude Code 用量统计工具，查看 Token、费用、模型、项目和会话用量。macOS 使用菜单栏，Windows 使用系统托盘，也可以单独运行网页面板。

独立社区项目，与 Anthropic 无隶属或背书关系。

## 快速开始

安装 [Node.js](https://nodejs.org/) 18+ 和 [Python](https://www.python.org/downloads/) 3.8+ 后，在终端运行：

```bash
npx claude-cost-lens
```

- **macOS**：启动菜单栏应用，点击图表图标查看用量。
- **Windows**：启动任务栏通知区域的托盘应用，图标可能收在隐藏图标菜单中。
- 默认启动时不打开浏览器，通过应用中的 **打开完整面板** 进入网页。
- 应用启动完成后可以关闭终端；退出应用请使用菜单中的退出选项。

只使用网页面板：

```bash
npx claude-cost-lens --web
```

Web 模式会打开浏览器，终端需要保持运行，按 `Ctrl-C` 停止服务。

## 系统要求

| 项目 | macOS | Windows |
| --- | --- | --- |
| 运行环境 | Node.js 18+、Python 3.8+ | Node.js 18+、Python 3.8+ |
| 原生界面 | macOS 13+ 菜单栏应用 | 系统托盘与用量窗口 |
| 首次构建 | Xcode Command Line Tools，无需完整 Xcode | Windows PowerShell 5.1、.NET Framework 4.8 |
| Web 模式 | 支持，不需要 Xcode 工具 | 支持，不需要托盘组件 |

**Linux 不支持**：npm 包限制在 macOS 和 Windows 安装，启动器也会检查平台。

Python 需要自行安装，npm 包不包含 Python。默认读取当前用户的 Claude Code 数据目录：

- macOS：`~/.claude`
- Windows：`%USERPROFILE%\.claude`

### macOS 首次启动

首次运行会构建原生应用。如果没有安装 Xcode Command Line Tools，会提示选择：

1. 使用 Apple 系统安装器安装命令行工具，完成后回到终端继续。
2. 本次仅使用 Web 面板。
3. 退出。

已构建的应用后续会直接打开，不必重新安装构建工具。非交互终端中不会自动安装工具，可以显式使用 `--web`。

### Windows 首次启动

在 PowerShell 或命令提示符运行：

```powershell
npx claude-cost-lens
```

托盘组件通过 Windows PowerShell 编译内置 C# 源码，不需要 Electron、Visual Studio 或 .NET SDK。如果系统组件缺失或组织策略阻止运行，可使用：

```powershell
npx claude-cost-lens --web
```

macOS 已做本地构建及运行验证。Windows 已通过源码编译检查和启动器模拟测试，实际 Windows 桌面上的托盘交互仍待验证。

## 安装方式

### 临时运行

```bash
npx claude-cost-lens
```

### 全局安装

```bash
npm install --global claude-cost-lens
claude-cost-lens
```

更新全局安装：

```bash
npm install --global claude-cost-lens@latest
```

### 从源码运行

```bash
git clone https://github.com/gudvi666/claude-cost-lens.git
cd claude-cost-lens
npm start
```

项目使用 Node.js 和 Python 标准库，没有需要额外安装的 npm 或 pip 依赖。源码运行不需要 npm 登录。

只启动网页，或者指定端口：

```bash
npm start -- --web
npm start -- --web --port 8787
```

## 使用方式

菜单栏和托盘中可以查看 **今天、近 7 天、近 30 天、全部** 数据，并手动刷新。应用每分钟自动刷新。

主要功能：

- macOS 菜单栏同时显示五小时、周额度剩余圆环，以及原有今日费用或 Token 消耗。
- 原生费用与单独标记的 API 估算费用。
- 输入、输出、缓存读取、缓存写入 Token，统一使用十进制 K/M/B 单位。
- 模型用量、项目统计、会话列表和活动分布。
- Advisor 调用与用量；有可用数据时计入费用和 Token，避免重复相加。
- 子代理模型在会话列表中使用灰色半角括号显示，例如 `Claude Opus 5.5, (Claude Haiku 5.5)`。
- **全部** 统计去重后的所有本地数据，包含较早和没有日期的记录。

日期范围使用 Asia/Tokyo（UTC+9）日历。每日原生费用按已保存快照的时间归属，跨日分布可能存在近似。

### macOS 订阅额度圆环

菜单栏中，左侧青色圆环表示 **五小时剩余百分比**，右侧紫色圆环表示 **周额度剩余百分比**。数字越小，圆环越短；旁边继续显示今日费用或 Token。面板内同时展示剩余比例和已用比例，额度不随今天／近 7 天／全部等统计范围切换。

额度来自 Claude 桌面端本地文件：

```text
~/Library/Application Support/Claude/plan-usage-history.json
```

每 30 秒只读刷新，手动刷新和打开面板时也会重新检查文件。按时间取最新组织快照，不混合不同组织的额度。长时间没有新记录时，仍显示最近一次有效百分比和记录日期，不推断额度归零或已经重置。只有文件缺失、不可读或数据无效时，才显示 **白色闭环和 `-`**。悬停和面板会说明当前状态。

此功能不需要额外登录、访问钥匙串或联网查询额度。快照由 Claude 桌面端维护，其保存频率决定了数据的新鲜程度。历史文件没有准确重置时间，因此不提供重置倒计时。目前额度圆环用于 macOS 菜单栏，Windows 托盘仍显示原有消耗统计。

### 常用命令

| 命令 | 作用 |
| --- | --- |
| `npx claude-cost-lens` | 启动 macOS 菜单栏或 Windows 托盘 |
| `npx claude-cost-lens --web` | 仅启动 Web 面板并打开浏览器 |
| `npx claude-cost-lens --web --port 8787` | 指定 Web 服务端口 |
| `npx claude-cost-lens --once` | 输出一次 JSON 汇总后退出 |
| `npx claude-cost-lens --help` | 查看帮助 |
| `npx claude-cost-lens --menubar` | 显式启动 macOS 菜单栏 |
| `npx claude-cost-lens --tray` | 显式启动 Windows 托盘 |

指定其他 Claude 数据目录，使用 Web 模式：

```bash
npx claude-cost-lens --web --data-root /path/to/.claude
```

Windows 示例：

```powershell
npx claude-cost-lens --web --data-root "D:\ClaudeData\.claude"
```

Python 不在默认搜索路径时，可以指定解释器。

macOS：

```bash
CLAUDE_USAGE_PYTHON=/opt/homebrew/bin/python3 npx claude-cost-lens
```

Windows PowerShell：

```powershell
$env:CLAUDE_USAGE_PYTHON = "C:\Python312\python.exe"
npx claude-cost-lens
```

## 费用和 Token 怎么统计

### Token

读取 JSONL 中请求级别的 `message.usage`，按唯一请求去重，处理流式累积计数和复制的会话历史。总 Token 包括输入、输出、缓存读取和缓存写入；Thinking 已包含在输出中，不再单独相加。Advisor 使用实际保存的调用用量参与统计。

原生 `cost-state.modelUsage` 也包含累积 Token 汇总，但它可能包含继承历史、内部调用或较旧快照。面板分别标注请求统计与原生汇总，二者可能因覆盖范围和保存时间不同而存在差异。

显示单位：`1K = 1,000`、`1M = 1,000,000`、`1B = 1,000,000,000`；小于 1,000 的数值保持整数，JSON API 保留精确计数。

### 费用

1. 有可用的 Claude 原生费用时，使用已对账的原生金额，包括可用的零值。
2. 继承的会话账本只在证据充分时扣除已计入的历史基数。
3. 没有可用原生金额时，按本地 API 价格快照估算，并明确标记估算与不完整覆盖。
4. 后续读到可用原生金额时，该会话的估算会在刷新时被替换。
5. Advisor 已包含在可用原生总额中时不重复加价；估算时按其实际模型和用量计算。

API 价格快照日期为 **2026-10-19**。估算使用快照价格，也适用于较旧日志；它不是历史账单还原，也不代表 Claude 订阅实际扣款。未知模型、缺失调用和部分额外费用会形成统计缺口。原生费用本身也属于 Claude 保存的用量估计。

常见状态：

| 状态 | 含义 |
| --- | --- |
| 已保存快照 | Claude 已保存原生费用，活跃会话的快照可能尚未更新 |
| 已扣继承 | 已确认并扣除当前会话继承的历史费用基数 |
| 待对账 | 继承关系或基数证据不足，原生金额不直接计入已对账总额 |
| 待写入 / 未记录 | 尚未找到可用原生费用，有足够用量时显示 API 估算 |

刷新会重新读取现有记录；待对账是否能解决，取决于后续是否出现足够的原生数据。工具不会主动要求 Claude 写入快照。

完整字段、去重规则与估算假设见 [费用与 Token 统计说明](docs/ACCOUNTING.md)。

## 数据与隐私

- Claude 数据目录始终按只读方式访问，不创建、修改或删除其中的文件。
- 不需要 Anthropic API Key；运行统计时不上传会话，也不联网获取价格。
- 用量统计保存在进程内存中，不生成用量数据库或向 Claude 回写数据。
- 应用自身会产生构建文件；Windows 启动时会创建并清理应用自己的临时配置和状态文件。
- 默认 Web 服务只监听 `127.0.0.1`。原生应用使用自己启动的本地后端，并在退出时清理该后端。

## 开发与验证

在源码目录运行：

```bash
python3 -m unittest discover -s tests -q
node --test tests/*.test.cjs
```

在 macOS 构建菜单栏应用：

```bash
npm run build:mac
```

默认构建 Apple Silicon 与 Intel 通用应用，输出到 `dist/`。本地应用使用临时签名，未进行 Apple 公证。需要指定架构时：

```bash
python3 scripts/build_macos.py --arch arm64
python3 scripts/build_macos.py --arch x86_64
```

打包和发布步骤见 [PUBLISHING.md](PUBLISHING.md)。

## 反馈与许可证

问题和建议请提交到 [GitHub Issues](https://github.com/gudvi666/claude-cost-lens/issues)。

本项目使用 [MIT License](LICENSE)。
