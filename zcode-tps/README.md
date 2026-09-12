# zcode-tps

ZCode 模型执行速度统计插件。安装后在会话内输入 `/speed` 即可查看：

- **最近请求速度**（tok/s，decode 口径；无首 token 计时的行自动回退总时长口径并标注）
- **会话平均速度**（分子分母同集过滤，对齐 deepseek-harness 口径）
- **按模型分段**（会话中切换模型各自累计；分组键小写归一）
- 平均 TTFT、模型/工具用时、缓存命中率、子代理聚合（递归含孙会话）

数据只读自 `~/.zcode/cli/db/db.sqlite`（不写库、不联网、无 hook、无后台进程）。

完整说明（安装步骤 / 截图 / 计算口径）见仓库根目录 [README.md](../README.md)。

## 用法

```
/speed                    # 当前会话统计面板
/speed --list-sessions    # 列出最近会话
/speed --session <id>     # 指定会话
/speed --watch            # 获取终端实时刷新命令
/speed --setup-menu       # 安装稳定副本 + SwiftBar 菜单栏脚本（可选）
/speed --json             # 机器可读输出
```

终端实时刷新（粘贴到 ZCode 内置终端标签页，每 2 秒刷新，Ctrl+C 退出）：

```
python3 ~/.zcode/plugins-data/zcode-tps/tps.py --watch
```

菜单栏常驻（macOS，推荐的日常方式）：先运行 `/speed --setup-menu` 生成菜单栏脚本，再安装 SwiftBar（`brew install --cask swiftbar`），首次启动向导选择插件目录时选它输出的路径——菜单栏即出现 ⚡ 实时速度（每 3 秒刷新）。详细步骤、装好后的效果与排障，见仓库根目录 README 的「方式一」一节。

> 插件升级后若已装过稳定副本，请重跑 `/speed --setup-menu` 刷新（定位链取最新文件，多数情况自动生效）。
