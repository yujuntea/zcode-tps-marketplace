# ⚡ zcode-tps — Model Execution Speed Stats for ZCode

[中文说明](README.md)

See your model's real-time execution speed right inside [ZCode](https://zcode.z.ai): **current request tok/s, per-session average, per-model breakdown, TTFT (time-to-first-token), and cache hit rate** — with multi-session isolation and subagent aggregation.

![/speed stats panel](docs/panel.png)

## Why this plugin

ZCode doesn't show how fast the model is generating — you just wait. Tools like DSH (deepseek-harness) have speed displays; ZCode doesn't. This plugin turns the per-request metrics ZCode **already records locally** (output tokens, first-token timestamp, completion timestamp, cache counters) into clear speed statistics:

| You want to know | The plugin shows |
|---|---|
| How fast is the model right now? | Latest request tok/s (decode window) |
| How fast is this session overall? | Session average tok/s (same-set filtered, no inflation) |
| Switched models mid-session? | **Per-model breakdown**, independently accumulated |
| Is the model responsive? | Average TTFT (time to first token) |
| How much does cache save? | Cache hit rate |
| How about subagents? | Recursive aggregation of the session's subagent tree |

Data is **read-only** from ZCode's local database (`~/.zcode/cli/db/db.sqlite`). No writes, no network, no background processes.

## Install the plugin (3 steps, ~1 minute)

> Prerequisite: open any workspace in ZCode (the plugin management UI requires a workspace).

1. **Add the marketplace**: ZCode → Settings → Plugins → **Create / Add Plugin Marketplace** → paste this repository URL:

   ```
   https://github.com/yujuntea/zcode-tps-marketplace
   ```

   (Or clone the repo locally and add the local directory.)

2. **Install**: under the "Personal" section, find **ZCode Tps** and click **Install** (enabled by default).

3. **Use**: in any session, type `/speed` to see the stats panel.

After installing, set up the **SwiftBar menu bar speed** below — it's the most convenient way to use the plugin day to day.

## ⭐ Three ways to watch the speed live

### Way 1: macOS menu bar, always visible (SwiftBar — recommended)

Once set up, a live `⚡ tok/s` figure sits in your **menu bar**, visible from any app:

![Menu bar speed](docs/menubar.png)

**What you get:**

- The menu bar shows a figure like `⚡ 497.0 tok/s`, **auto-refreshed every 3 seconds** — it ticks whenever the model completes a request
- **Click it for a dropdown** with details: latest request (tokens / speed / age), current model, session average, per-model speed & TTFT, cache hit rate, tool time, current session title, plus a one-click Refresh
- On multi-display setups the icon **follows the screen you are working on** — zero configuration

**One-time setup (~3 minutes):**

1. **Generate the menu-bar script**: in a ZCode session, type:

   ```
   /speed --setup-menu
   ```

   This copies the stats script to a stable path (`~/.zcode/plugins-data/zcode-tps/tps.py`) and creates the menu-bar script in a dedicated plugin folder (`~/.zcode/plugins-data/zcode-tps/swiftbar/zcode-tps.3s.sh`).

2. **Install SwiftBar** (an open-source macOS menu bar tool; the menu-bar display is built on it):

   ```
   brew install --cask swiftbar
   ```

   No Homebrew? Download the dmg from [swiftbar.app](https://swiftbar.app) and drag it into Applications.

3. **Pick the plugin folder**: on first launch SwiftBar asks you to choose a plugin folder — select the dedicated folder printed in step 1 (by default `~/.zcode/plugins-data/zcode-tps/swiftbar`).

4. **Done**: the ⚡ speed appears in the menu bar immediately. If not, in order:
   - The menu bar shows a plain `SwiftBar` placeholder instead of a speed: SwiftBar occasionally skips importing files that already exist — nudge it with `touch ~/.zcode/plugins-data/zcode-tps/swiftbar/zcode-tps.3s.sh`, or restart SwiftBar once
   - Still nothing (the first-launch wizard has a known quirk in some SwiftBar builds): run this once and restart SwiftBar:

     ```
     defaults write com.ameba.SwiftBar PluginDirectory "$HOME/.zcode/plugins-data/zcode-tps/swiftbar"
     ```

> Note: keep using the dedicated folder above as the plugin folder. Do **not** pick `~/Library/Application Support/SwiftBar` (SwiftBar's own data root) — its internal `Plugins` sync folder gets traversed recursively, which loads the same script twice as two menu items.
>
> After upgrading the plugin, re-run `/speed --setup-menu` once to refresh the script.

### Way 2: Live refresh in the ZCode built-in terminal (no extra apps)

> Prerequisite: run `/speed --setup-menu` once in a session first (copies the script to a stable path, one-time).

Paste this into a **ZCode built-in terminal tab** — the speed line refreshes every 2 seconds next to your chat:

![Terminal live refresh](docs/watch-line.png)

```
python3 ~/.zcode/plugins-data/zcode-tps/tps.py --watch
```

`Ctrl+C` to stop; add `--panel` for the full-panel view. Or type `/speed --watch` and the plugin hands you this command.

### Way 3: On-demand `/speed` (zero setup)

```
/speed                    # stats panel for the current session (default)
/speed --list-sessions    # list recent sessions to pick from
/speed --session <id>     # inspect a specific session
/speed --json             # machine-readable JSON output
```

## Example output

```
⚡ ZCode 速度 — 会话「调研zcode自定义插件开发方案」 (数据源: db)
  当前模型      GLM-5.3-Flash
  最近请求速度  47.8 tok/s · 1.1k tok / 22.7s decode · 0s前 [decode 口径]
  生成中速度    不可用(请求完成时才落库) — 生成中沿用最近请求值
  会话平均      60.9 tok/s · 445 请求 · 334.6k tok (全部模型, 同集过滤)
  模型分段
    GLM-5.3-Flash        47.1 tok/s · 247 请求 · 172.1k tok · TTFT 6.4s (样本160/247) · 回退35%
    k3-256k              49.8 tok/s ·  65 请求 ·  85.2k tok · TTFT 13.8s (样本58/65) · 回退11%
    GLM-5.3-HighSpeed   436.2 tok/s · 133 请求 ·  77.3k tok · TTFT 3.6s (样本96/133) · 回退28%
  工具用时 50m02s   模型用时 2.3h   子会话×6
  缓存命中      97.5% (输入 102.3M, 命中 99.8M)
  子代理        reviewer ×65: 49.8 tok/s; vision-reader ×1: 60.3 tok/s
  注            /speed 触发时最近请求=发起本次统计的请求(自指); 会话定位=最近完成请求所在根会话(--session 可指定)
```

(Labels are in Chinese; the numbers are universal — tok/s, request counts, TTFT seconds, cache %.)

## Measurement semantics (why the numbers are trustworthy)

- **Per-request TPS** = `output_tokens / (completion − first_token)`, matching the decode-window semantics of [deepseek-harness](https://github.com/deepseek-ai/deepseek-harness). ~35% of requests lack a first-token timestamp in ZCode; those rows **fall back** to `output_tokens / total_duration` (denominator includes TTFT, so slightly lower) and are labeled with the fallback share.
- **Session average** applies **same-set filtering** — only rows having both token count and decode duration count toward numerator AND denominator — avoiding a ~10% systematic overestimate.
- Model grouping keys are lower-cased (the database stores `GLM-5.3` / `glm-5.3` variants).
- Session location = the root session of the most recently completed request (recursive `parent_id` walk); subagent sessions aggregate as a tree.
- Only `main_turn` / `subagent` requests count — compaction and title-generation requests are excluded.

## Privacy & safety

- Every query is **read-only** against your local ZCode database (`mode=ro` + `query_only` as a double lock).
- No network, no DB writes, no hooks, no background processes, zero third-party dependencies (pure Python standard library).

## Known limitations

- ZCode persists request rows **on completion** (nothing observable during streaming), so live refresh is per-request granularity; while generating, the last speed is shown.
- ~35% of requests lack first-token timing; the fallback figure runs low and is labeled.
- The database is an internal ZCode implementation; if a ZCode upgrade breaks stats, update the plugin — the script probes the schema and degrades gracefully.

## How it works

```
ZCode (zcode-cli) ──writes──▶ ~/.zcode/cli/db/db.sqlite
                                 │ model_usage / tool_usage / session
                                 ▼
                    tps.py (read-only SQL: recursive session tree,
                            same-set filtering, model normalization)
                        │                   │                │
                        ▼                   ▼                ▼
                  /speed panel         terminal --watch   SwiftBar menu bar
```

## License

[MIT](LICENSE)
