---
description: 展示当前会话的模型执行速度（最近请求/平均 tok/s、TTFT、缓存命中、按模型分段）
allowed-tools: Bash
argument-hint: "[--list-sessions] [--setup-menu] [--watch] [--json] [--menubar] [--session <id>]"
---

定位本插件的 tps.py 脚本：在以下路径中取**修改时间最新**的一个存在的文件（用 `ls -t` 比较）：

1. `~/.zcode/plugins-data/zcode-tps/tps.py`（稳定副本）
2. `~/.zcode/cli/plugins/cache/*/zcode-tps/*/scripts/tps.py`（插件安装目录，官方布局为 cache/市场名/插件名/版本/）

执行命令：

- 用户无参数时：`python3 <脚本路径> --panel`
- 用户带参数（`--list-sessions` / `--setup-menu` / `--watch` / `--json` / `--menubar` / `--line` / `--session <id>` 等）时：`python3 <脚本路径> <用户参数>`，**不要再附加 `--panel`**（脚本按参数自动选择模式）

把脚本输出**原样**展示给用户：不要改写、四舍五入或省略数值与口径标注；面板首行会显示本次统计的会话标题。若脚本报错，原样转述错误信息与修复建议，不要臆造数据。若用户传 `--watch`：不要运行 watch 循环（长驻进程会卡住回合），而是把下面这条命令原样给用户，让用户复制到 ZCode 内置终端（终端标签页）里自行运行（脚本已内置循环刷新，无需外部 watch 命令）：

```
python3 ~/.zcode/plugins-data/zcode-tps/tps.py --watch
```

并说明：该终端标签页会每 2 秒自动刷新当前会话的速度单行（当前模型/均值/TTFT/缓存命中），与聊天并排可见；`Ctrl+C` 退出；追加 `--panel` 可显示完整面板版。若用户运行后报"稳定副本缺失/No such file"（尚未跑过 `--setup-menu`），提示先执行一次 `/speed --setup-menu` 再用该命令。
若面板显示的会话不是用户想看的会话，提示可用 `/speed --list-sessions` 列出最近会话、再用 `/speed --session <id>` 指定。若两处路径都找不到脚本，说明插件未正确安装：请用户到 设置 → 插件管理 确认 zcode-tps 已安装并启用后重试。
