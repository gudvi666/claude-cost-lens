# 发布指南

GitHub 源码仓库和 npm 包分别发布。推送 GitHub 不会自动发布 npm。

## 检查与打包

在项目目录运行：

```bash
python3 -m unittest discover -s tests -q
node --test tests/*.test.cjs
python3 scripts/pack_release.py
```

打包脚本只复制 `package.json` 的 `files` 白名单，检查压缩包内容，并生成 `release/claude-cost-lens-<版本>.tgz`。

准备发布新版本时，先修改版本号，再执行 `python3 scripts/pack_release.py --dry-run` 演练发布。它不会上传，但 npm 仍会拒绝已经发布过的版本号。

发布前检查包内容，确保不包含 Claude 会话、凭据、截图、审计数据或本地构建产物。不要将 npm 令牌写入仓库。

## npm 发布

后续版本先修改 `package.json` 的版本号。已发布的同名同版本不能覆盖。

登录 npm：

```bash
npm login --auth-type=web --registry=https://registry.npmjs.org/
npm whoami --registry=https://registry.npmjs.org/
```

### 暂存后在网页确认

需要 npm 11.15.0+、Node.js 22.14.0+，且账号启用双重认证。将下面的版本替换成实际版本：

```bash
npm stage publish ./release/claude-cost-lens-<版本>.tgz --access public --registry=https://registry.npmjs.org/
```

登录 npm 网站，在 **Staged Packages** 中核对内容，点击 **Approve** 并完成双重认证。首次暂存新包会创建公开的 `0.0.0-stage` 占位版本；正式版本完成审核和批准后才可用。`Validating` 表示 npm 自动审核尚未完成。

参考：[npm 暂存发布文档](https://docs.npmjs.com/staged-publishing/)。

### 直接发布

```bash
npm publish ./release/claude-cost-lens-<版本>.tgz --access public --registry=https://registry.npmjs.org/
```

根据提示完成认证。发布后验证版本和启动器：

```bash
npm view claude-cost-lens version --registry=https://registry.npmjs.org/
npx --yes claude-cost-lens@<版本> --help
```

## Windows 桌面验证

Windows 源码编译和启动器模拟测试通过后，仍需要在实际 Windows 上确认：

1. 默认命令启动一个托盘图标，终端返回且不自动打开浏览器；重复启动复用现有实例。
2. Today / 7-day / 30-day / all-local-data 范围、费用、K/M/B Token、刷新与 Web 面板入口可用。
3. 用量窗口关闭或失去焦点后隐藏，菜单退出能清理自己的 Python 后端。
4. 强制终止托盘后也能清理所属后端，其他 Claude 和面板进程不受影响。
5. `--web` 模式可以独立运行。
