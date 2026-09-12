#!/usr/bin/env python3
"""zcode-tps — ZCode 模型执行速度统计（本地只读，不写库、不联网）.

数据源（按优先级）:
  1. ~/.zcode/cli/db/db.sqlite 的 model_usage / tool_usage / session 表
     （官方持久化；请求完成时一次性落库，无 UPDATE）
  2. 降级源: ~/.zcode/cli/rollout/model-io-<session>.jsonl（无 TTFT、未含子代理聚合）

口径（对齐 deepseek-harness turn-metrics.ts；方案文档 zcode-tps-plugin-design.md v0.3）:
  - 单请求 TPS = output_tokens / (completed_at - first_token_at)；
    无 first_token_at 的行（实测约 35%）回退 output_tokens / duration_ms（分母含 TTFT，偏低），输出标注
  - 平均 TPS 分子分母同集过滤（仅 first_token_at 非空的行双计，防系统性偏高）
  - 聚合作用域 = 会话子树（递归含孙会话）；query_source ∈ {main_turn, subagent}（排除 compact/session_title 等）
  - 模型分组键 = lower(model_id)（库内 GLM-5.3 / glm-5.3 大小写并存）
  - 会话定位: 最近一条完成请求所在会话沿 parent_id 上溯到根；--session 钉住时以该 id 为子树根
"""

import argparse
import glob
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
DB_PATH = Path(os.environ.get("ZCODE_TPS_DB", HOME / ".zcode/cli/db/db.sqlite"))
ROLLOUT_DIR = Path(os.environ.get("ZCODE_TPS_ROLLOUT", HOME / ".zcode/cli/rollout"))
STABLE_DIR = HOME / ".zcode/plugins-data/zcode-tps"
SWIFTBAR_DIR = Path(os.environ.get("SWIFTBAR_PLUGIN_DIR", HOME / "Library/Application Support/SwiftBar"))
QUERY_SOURCES = ("main_turn", "subagent")
REQUIRED_DB_COLS = {
    "session_id", "model_id", "query_source", "status", "started_at",
    "first_token_at", "completed_at", "duration_ms", "output_tokens",
    "input_tokens", "cache_read_input_tokens", "time_to_first_token_ms",
}

# ---------------------------------------------------------------- db 基础


def connect_db():
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=3)
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("PRAGMA query_only=ON")  # 双保险：连接已 mode=ro
    return conn


def db_usable(conn):
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='model_usage'"
        ).fetchone()
        if not row:
            return False
        cols = {r[1] for r in conn.execute("PRAGMA table_info(model_usage)")}
        return REQUIRED_DB_COLS.issubset(cols)
    except sqlite3.Error:
        return False


def find_root(conn, sid):
    """沿 parent_id 上溯到根会话（最多 20 层防环）。"""
    cur = sid
    for _ in range(20):
        row = conn.execute("SELECT parent_id FROM session WHERE id=?", (cur,)).fetchone()
        if not row or row[0] is None:
            return cur
        cur = row[0]
    return cur


def tree_ids(conn, sid):
    """sid 自身 + 全部后代会话（递归 CTE，覆盖孙会话）。"""
    rows = conn.execute(
        "WITH RECURSIVE tree(id) AS ("
        "  SELECT ? UNION ALL"
        "  SELECT s.id FROM session s JOIN tree t ON s.parent_id = t.id"
        ") SELECT id FROM tree",
        (sid,),
    ).fetchall()
    return [r[0] for r in rows]


def in_clause(ids):
    """(sql 片段, 参数列表)——按 900 一组 OR 拼接，规避变量数上限。"""
    parts, params = [], []
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        parts.append("(" + ",".join("?" * len(chunk)) + ")")
        params.extend(chunk)
    return "(" + " OR ".join(f"session_id IN {p}" for p in parts) + ")", params


def session_row(conn, sid):
    row = conn.execute(
        "SELECT id, title, parent_id FROM session WHERE id=?", (sid,)
    ).fetchone()
    if not row:
        return {"id": sid, "title": "(未知会话)", "parent_id": None}
    return {"id": row[0], "title": row[1] or "(无标题)", "parent_id": row[2]}


def clean_title(t, n=44):
    t = " ".join(str(t).split())
    return t if len(t) <= n else t[: n - 1] + "…"

# ---------------------------------------------------------------- db 统计


def pick_default_session(conn):
    """定位规则 v2：最近一条 main_turn/subagent 请求所在会话 → 上溯根会话。

    只看 main_turn/subagent（排除其他窗口的 session_title/compact 辅助请求
    抢占定位）；cancelled 也算活动痕迹（用户刚在该会话取消过）。
    """
    row = conn.execute(
        "SELECT session_id FROM model_usage"
        " WHERE status IN ('completed','cancelled')"
        "   AND query_source IN ('main_turn','subagent')"
        " ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return find_root(conn, row[0])


def load_stats_db(conn, sid):
    ids = tree_ids(conn, sid)
    where, params = in_clause(ids)
    qfilter = f"{where} AND query_source IN (?,?) AND status='completed'"
    fparams = params + list(QUERY_SOURCES)

    per_model = conn.execute(
        f"""
        SELECT lower(model_id) AS mkey,
               max(model_id)   AS mdisplay,
               count(*)                          AS reqs,
               sum(CASE WHEN first_token_at IS NOT NULL THEN output_tokens ELSE 0 END) AS f_out,
               sum(CASE WHEN first_token_at IS NOT NULL THEN completed_at - first_token_at ELSE 0 END) AS f_ms,
               sum(CASE WHEN first_token_at IS NULL THEN 1 ELSE 0 END) AS no_ftt,
               sum(output_tokens)  AS out_tok,
               sum(duration_ms)    AS dur_ms,
               sum(input_tokens)   AS in_tok,
               sum(cache_read_input_tokens) AS cache_read,
               sum(reasoning_tokens) AS reason_tok,
               count(time_to_first_token_ms) AS ttft_n,
               avg(time_to_first_token_ms)    AS ttft_avg,
               max(started_at)     AS last_started
        FROM model_usage WHERE {qfilter}
        GROUP BY lower(model_id) ORDER BY last_started DESC
        """,
        fparams,
    ).fetchall()

    last = conn.execute(
        f"""
        SELECT model_id, output_tokens, first_token_at, completed_at, started_at,
               duration_ms, query_source
        FROM model_usage WHERE {qfilter}
        ORDER BY completed_at DESC LIMIT 1
        """,
        fparams,
    ).fetchone()

    tool_row = conn.execute(
        f"SELECT coalesce(sum(duration_ms),0) FROM tool_usage"
        f" WHERE {where} AND status='completed'",
        params,
    ).fetchone()
    tool_ms = tool_row[0] if tool_row else 0

    sub_rows = conn.execute(
        f"""
        SELECT agent, count(*), sum(output_tokens),
               sum(CASE WHEN first_token_at IS NOT NULL THEN output_tokens ELSE 0 END),
               sum(CASE WHEN first_token_at IS NOT NULL THEN completed_at - first_token_at ELSE 0 END)
        FROM model_usage WHERE {qfilter} AND query_source='subagent'
        GROUP BY agent ORDER BY 2 DESC LIMIT 5
        """,
        fparams,
    ).fetchall()

    models = []
    tot_f_out = tot_f_ms = tot_out = tot_reqs = tot_in = tot_cr = 0
    for (mkey, mdisp, reqs, f_out, f_ms, no_ftt, out_tok, dur_ms, in_tok, cr,
         reason_tok, ttft_n, ttft_avg, last_started) in per_model:
        # 展示名取该组内行数最多的写法（库内 GLM-5.3/glm-5.3 大小写并存）
        disp_row = conn.execute(
            f"SELECT model_id FROM model_usage WHERE {qfilter}"
            " AND lower(model_id)=? GROUP BY model_id ORDER BY count(*) DESC LIMIT 1",
            fparams + [mkey],
        ).fetchone()
        mdisp = disp_row[0] if disp_row else mdisp
        models.append({
            "model": mdisp, "model_key": mkey, "requests": reqs,
            "avg_tps": (f_out / (f_ms / 1000)) if f_ms else None,
            "fallback_share": (no_ftt / reqs) if reqs else 0,
            "output_tokens": out_tok, "model_time_ms": dur_ms,
            "input_tokens": in_tok, "cache_read": cr, "reasoning_tokens": reason_tok,
            "avg_ttft_ms": ttft_avg, "ttft_samples": ttft_n,
            "last_started": last_started,
        })
        tot_f_out += f_out or 0
        tot_f_ms += f_ms or 0
        tot_out += out_tok or 0
        tot_reqs += reqs or 0
        tot_in += in_tok or 0
        tot_cr += cr or 0

    now_ms = int(time.time() * 1000)
    last_req = None
    if last:
        m_id, out_tok, ftt, comp, started, dur_ms, qsrc = last
        if ftt is not None and comp is not None and comp > ftt:
            tps, decode_s, fb = out_tok / ((comp - ftt) / 1000), (comp - ftt) / 1000, False
        else:
            tps, decode_s, fb = (out_tok / (dur_ms / 1000) if dur_ms else None), None, True
        last_req = {
            "model": m_id, "output_tokens": out_tok, "tps": tps,
            "decode_s": decode_s, "duration_ms": dur_ms,
            "completed_at": iso(comp), "age_s": max(0, (now_ms - comp) / 1000) if comp else None,
            "fallback": fb, "query_source": qsrc,
        }

    subagents = []
    for (agent, n, out_tok, f_out, f_ms) in sub_rows:
        subagents.append({
            "agent": (agent or "?").replace("zcode-", ""),
            "requests": n, "output_tokens": out_tok or 0,
            "avg_tps": (f_out / (f_ms / 1000)) if f_ms else None,
        })

    return {
        "generated_at": iso(now_ms),
        "source": "db",
        "session": {
            "id": sid,
            "title": clean_title(session_row(conn, sid)["title"]),
            "sub_sessions": max(0, len(ids) - 1),
        },
        "last_request": last_req,
        "current_model": last_req["model"] if last_req else (models[0]["model"] if models else None),
        "session_avg": {
            "tps": (tot_f_out / (tot_f_ms / 1000)) if tot_f_ms else None,
            "requests": tot_reqs, "output_tokens": tot_out,
        },
        "models": models,
        "tool_time_ms": tool_ms,
        "cache": {
            "input_tokens": tot_in, "cache_read": tot_cr,
            "hit_pct": (100.0 * tot_cr / tot_in) if tot_in else None,
        },
        "subagents": subagents,
    }

# ---------------------------------------------------------------- 降级源（rollout）


def _parse_iso_ms(s):
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def load_stats_rollout(sid=None):
    if sid:
        files = [ROLLOUT_DIR / f"model-io-{sid}.jsonl"]
    else:
        files = sorted(glob.glob(str(ROLLOUT_DIR / "model-io-*.jsonl")), key=os.path.getmtime)
    files = [f for f in files if os.path.exists(f)]
    if not files:
        return None
    path = files[-1]
    per, last, last_comp = {}, None, 0
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "model_io":
            continue
        if d.get("querySource") not in QUERY_SOURCES:
            continue
        usage = (d.get("response") or {}).get("usage") or {}
        out_tok = usage.get("outputTokens")
        dur = d.get("durationMs")
        comp = _parse_iso_ms(d.get("completedAt")) or 0
        model = ((d.get("model") or {}).get("modelId")) or "?"
        if not isinstance(out_tok, (int, float)) or not dur:
            continue
        m = per.setdefault(model, {"reqs": 0, "out": 0, "dur": 0,
                                   "in": 0, "cr": 0, "last": 0})
        m["reqs"] += 1
        m["out"] += out_tok
        m["dur"] += dur
        m["in"] += usage.get("inputTokens") or 0
        m["cr"] += usage.get("cacheReadTokens") or 0
        m["last"] = max(m["last"], comp)
        if comp >= last_comp:
            last_comp, last = comp, (model, out_tok, dur)
    if not per:
        return None
    models = [{
        "model": k, "model_key": k.lower(), "requests": v["reqs"],
        "avg_tps": (v["out"] / (v["dur"] / 1000)) if v["dur"] else None,
        "fallback_share": 1.0, "output_tokens": v["out"], "model_time_ms": v["dur"],
        "input_tokens": v["in"], "cache_read": v["cr"], "reasoning_tokens": 0,
        "avg_ttft_ms": None, "ttft_samples": 0, "last_started": v["last"] or None,
    } for k, v in sorted(per.items(), key=lambda kv: kv[1]["last"], reverse=True)]
    sid = sid or Path(path).stem[len("model-io-"):]
    tot_out = sum(v["out"] for v in per.values())
    tot_ms = sum(v["dur"] for v in per.values())
    now_ms = int(time.time() * 1000)
    return {
        "generated_at": iso(now_ms),
        "source": "rollout",
        "session": {"id": sid, "title": "(降级源: 会话标题不可用)", "sub_sessions": 0},
        "last_request": ({
            "model": last[0], "output_tokens": last[1],
            "tps": last[1] / (last[2] / 1000) if last[2] else None,
            "decode_s": None, "duration_ms": last[2],
            "completed_at": iso(last_comp), "age_s": max(0, (now_ms - last_comp) / 1000),
            "fallback": True, "query_source": None,
        } if last else None),
        "current_model": (last[0] if last else models[0]["model"]),
        "session_avg": {"tps": (tot_out / (tot_ms / 1000)) if tot_ms else None,
                        "requests": sum(v["reqs"] for v in per.values()),
                        "output_tokens": tot_out},
        "models": models, "tool_time_ms": None,
        "cache": {"input_tokens": sum(v["in"] for v in per.values()),
                  "cache_read": sum(v["cr"] for v in per.values()), "hit_pct": None},
        "subagents": [],
    }

# ---------------------------------------------------------------- 渲染


def iso(ms):
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S")


def f_tps(v):
    return f"{v:.1f}" if isinstance(v, (int, float)) else "–"


def f_tok(n):
    if not isinstance(n, (int, float)):
        return "–"
    for unit, div in (("M", 1e6), ("k", 1e3)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return str(int(n))


def f_dur(ms):
    if not isinstance(ms, (int, float)) or ms < 0:
        return "–"
    s = ms / 1000
    if s < 90:
        return f"{s:.1f}s"
    if s < 5400:
        return f"{int(s // 60)}m{int(s % 60):02d}s"
    return f"{s / 3600:.1f}h"


def f_age(s):
    if not isinstance(s, (int, float)):
        return "–"
    if s < 90:
        return f"{int(s)}s前"
    if s < 7200:
        return f"{int(s // 60)}min前"
    return f"{s / 3600:.1f}h前"


def f_pct(v):
    return f"{v:.1f}%" if isinstance(v, (int, float)) else "–"


def render_line(st):
    """紧凑单行输出（供 ZCode 内置终端 `watch --line` 实时刷新用）。"""
    lr = st["last_request"]
    sa = st["session_avg"]
    if lr and lr["tps"] is not None:
        head = f"⚡ {f_tps(lr['tps'])} tok/s"
    elif sa["tps"] is not None:
        head = f"⚡ 均值 {f_tps(sa['tps'])} tok/s"
    else:
        head = "⚡ 无数据"
    cal = "*" if lr and lr["fallback"] else ""
    parts = [
        f"{head}{cal} · {f_age(lr['age_s']) if lr else '–'}",
        f"{st['current_model'] or '–'}",
        f"均值 {f_tps(sa['tps'])} ({sa['requests']} req)",
        f"TTFT {avg_ttft_s(st)} · 缓存 {f_pct(st['cache']['hit_pct'])}",
        f"「{st['session']['title']}」",
    ]
    return "  |  ".join(p for p in parts if p)


def avg_ttft_s(st):
    ms = [m["avg_ttft_ms"] for m in st["models"] if m["avg_ttft_ms"] is not None]
    return f"{(sum(ms) / len(ms)) / 1000:.1f}s" if ms else "–"


def render_panel(st):
    L = []
    src = "db" if st["source"] == "db" else "rollout 降级(仅部分近期窗口,无TTFT,未含子代理聚合)"
    L.append(f"⚡ ZCode 速度 — 会话「{st['session']['title']}」 (数据源: {src})")
    lr = st["last_request"]
    if not lr and not st["models"]:
        L.append("  (本会话暂无已完成的模型请求)")
        return "\n".join(L)
    L.append(f"  当前模型      {st['current_model'] or '–'}"
             + (" (子代理最近请求所用)" if lr and lr.get("query_source") == "subagent" else ""))
    if lr:
        cal = "回退口径(含TTFT,偏低)" if lr["fallback"] else "decode 口径"
        base = (f"{lr['decode_s'] * 1000:.0f}ms decode" if lr["decode_s"]
                else f"{f_dur(lr['duration_ms'])} 总时长")
        L.append(f"  最近请求速度  {f_tps(lr['tps'])} tok/s"
                 f" · {f_tok(lr['output_tokens'])} tok / {base}"
                 f" · {f_age(lr['age_s'])} [{cal}]")
    L.append(f"  生成中速度    不可用(请求完成时才落库) — 生成中沿用最近请求值")
    sa = st["session_avg"]
    L.append(f"  会话平均      {f_tps(sa['tps'])} tok/s · {sa['requests']} 请求 · {f_tok(sa['output_tokens'])} tok (全部模型, 同集过滤)")
    if st["models"]:
        L.append("  模型分段")
        for m in st["models"][:6]:
            ttft = (f"TTFT {m['avg_ttft_ms'] / 1000:.1f}s (样本{m['ttft_samples']}/{m['requests']})"
                    if m["avg_ttft_ms"] is not None else "TTFT –")
            L.append(f"    {m['model']:<28} {f_tps(m['avg_tps']):>6} tok/s · {m['requests']:>4} 请求"
                     f" · {f_tok(m['output_tokens']):>7} tok · {ttft}"
                     + (f" · 回退{m['fallback_share'] * 100:.0f}%" if m["fallback_share"] >= 0.05 else ""))
    extra = []
    if st["tool_time_ms"]:
        extra.append(f"工具用时 {f_dur(st['tool_time_ms'])}")
    mt = sum(m["model_time_ms"] or 0 for m in st["models"])
    if mt:
        extra.append(f"模型用时 {f_dur(mt)}")
    if st["session"]["sub_sessions"]:
        extra.append(f"子会话×{st['session']['sub_sessions']}")
    if extra:
        L.append("  " + "   ".join(extra))
    c = st["cache"]
    if c["hit_pct"] is not None:
        L.append(f"  缓存命中      {f_pct(c['hit_pct'])} (输入 {f_tok(c['input_tokens'])}, 命中 {f_tok(c['cache_read'])})")
    if st["subagents"]:
        L.append("  子代理        " + "; ".join(
            f"{s['agent']} ×{s['requests']}: {f_tps(s['avg_tps'])} tok/s" for s in st["subagents"]))
    L.append("  注            /speed 触发时最近请求=发起本次统计的请求(自指); 会话定位=最近完成请求所在根会话(--session 可指定)")
    return "\n".join(L)


def render_menubar(st):
    """SwiftBar 格式：首行=菜单栏标题，其后为下拉项（避免正文出现裸 | ）。"""
    lr = st["last_request"]
    if lr and lr["tps"] is not None:
        title = f"⚡ {f_tps(lr['tps'])} tok/s"
    elif st["session_avg"]["tps"] is not None:
        title = f"⚡ {f_tps(st['session_avg']['tps'])} tok/s 均值"
    else:
        title = "⚡ zcode-tps 无数据"
    L = [title, "---"]
    if lr:
        cal = "回退口径" if lr["fallback"] else "decode口径"
        L.append(f"最近请求: {f_tok(lr['output_tokens'])} tok · {f_tps(lr['tps'])} tok/s · {f_age(lr['age_s'])} ({cal})")
    L.append(f"当前模型: {st['current_model'] or '–'}")
    sa = st["session_avg"]
    L.append(f"会话平均: {f_tps(sa['tps'])} tok/s · {sa['requests']} 请求")
    L.append("---")
    for m in st["models"][:6]:
        ttft = f" · TTFT {m['avg_ttft_ms'] / 1000:.1f}s" if m["avg_ttft_ms"] is not None else ""
        L.append(f"{m['model']}: {f_tps(m['avg_tps'])} tok/s · {m['requests']} 请求{ttft}")
    c = st["cache"]
    if c["hit_pct"] is not None:
        L.append(f"缓存命中 {f_pct(c['hit_pct'])} · 工具用时 {f_dur(st['tool_time_ms'] or 0)}")
    L.append("---")
    L.append(f"会话: {st['session']['title']}")
    L.append("数据只读自 ~/.zcode/cli/db (请求完成时落库)")
    L.append("刷新 | refresh=true")
    return "\n".join(L)


def list_sessions(conn, n=10):
    roots = conn.execute(
        "SELECT id, title, time_updated FROM session"
        " WHERE parent_id IS NULL ORDER BY time_updated DESC LIMIT ?", (n * 2,)
    ).fetchall()
    out = []
    for sid, title, upd in roots:
        ids = tree_ids(conn, sid)
        where, params = in_clause(ids)
        row = conn.execute(
            f"SELECT max(started_at), count(*) FROM model_usage WHERE {where}", params
        ).fetchone()
        if not row or not row[0]:
            continue
        out.append((row[0], sid, clean_title(title), row[1], len(ids)))
    out.sort(reverse=True)
    L = [f"{'最近请求活动':<14} {'请求':>5} {'子会话':>4}  会话"]
    for last_ms, sid, title, reqs, sub in out[:n]:
        ts = datetime.fromtimestamp(last_ms / 1000).strftime("%m-%d %H:%M")
        L.append(f"{ts:<14} {reqs:>5} {sub:>4}  {title}  [{sid[:24]}…]")
    return "\n".join(L)

# ---------------------------------------------------------------- setup-menu / snapshot


def setup_menu():
    STABLE_DIR.mkdir(parents=True, exist_ok=True)
    src = Path(__file__).resolve()
    dst = STABLE_DIR / "tps.py"
    if src != dst:
        shutil.copy2(src, dst)
    script = "\n".join([
        "#!/bin/bash",
        "# zcode-tps menubar (由 /speed --setup-menu 生成; 重新生成: /speed --setup-menu)",
        f'TPS="{dst}"',
        'if [ ! -f "$TPS" ]; then',
        '  echo "zcode-tps 稳定副本缺失"',
        '  echo ---',
        '  echo "请在 ZCode 会话中运行: /speed --setup-menu"',
        '  exit 0',
        'fi',
        'exec python3 "$TPS" --menubar "$@"',
        "",
    ])
    # 总是写入 SwiftBar 默认插件目录（不存在则创建）——"先 setup、后装 SwiftBar"
    # 的新用户在首启向导里选该目录即可直接点亮；SWIFTBAR_PLUGIN_DIR 环境变量可覆盖。
    target_dir = SWIFTBAR_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    sh = target_dir / "zcode-tps.3s.sh"
    sh.write_text(script)
    sh.chmod(0o755)
    swiftbar_installed = Path("/Applications/SwiftBar.app").exists()
    print(f"✓ 稳定副本: {dst}")
    print(f"✓ SwiftBar 脚本: {sh} (文件名 .3s = 每 3 秒刷新)")
    if swiftbar_installed:
        print("✓ 已检测到 SwiftBar。若菜单栏未显示 ⚡ 速度，执行后重启 SwiftBar：")
        print(f'  defaults write com.ameba.SwiftBar PluginDirectory "{target_dir}"')
    else:
        print("○ 未检测到 SwiftBar。安装并配置两步即可点亮菜单栏：")
        print("  1) brew install --cask swiftbar   （或从 https://swiftbar.app 下载）")
        print(f"  2) 首次启动向导选择插件目录时，选择: {target_dir}")
    print("  提示: 插件升级后请重跑 --setup-menu 刷新稳定副本")
    print("  备选（无 SwiftBar）: 在 ZCode 内置终端运行  python3", dst, "--watch")


def snapshot(out_path, stats):
    try:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False)
        os.replace(tmp, p)  # 原子替换，读方不会读到半截文件
    except OSError:
        pass  # hook 场景必须静默且 exit 0

# ---------------------------------------------------------------- main


def get_stats(args):
    conn = connect_db()
    if conn is not None and db_usable(conn):
        sid = args.session or pick_default_session(conn)
        if sid is None:
            return None, conn
        return load_stats_db(conn, sid), conn
    st = load_stats_rollout(args.session)
    return st, conn


def main(argv=None):
    ap = argparse.ArgumentParser(prog="zcode-tps", description="ZCode 模型执行速度统计（只读）")
    # 模式互斥（--panel 除外）：--panel 是默认模式，与其他模式同现时让位给其他模式
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="机器可读 JSON")
    mode.add_argument("--menubar", action="store_true", help="SwiftBar 菜单栏输出")
    mode.add_argument("--line", action="store_true", help="紧凑单行（配合终端 watch 实时刷新）")
    mode.add_argument("--list-sessions", action="store_true", help="列出最近会话")
    mode.add_argument("--setup-menu", action="store_true", help="安装稳定副本+SwiftBar 菜单栏脚本")
    mode.add_argument("--snapshot", action="store_true", help="静默写 JSON 快照（始终 exit 0）")
    ap.add_argument("--panel", action="store_true", help="人读面板（默认；与其他模式同现时忽略）")
    ap.add_argument("--watch", nargs="?", const=2.0, type=float, metavar="SEC",
                    help="循环实时刷新（默认 2 秒一帧，无需外部 watch 命令；配 --panel 显示整版）")
    ap.add_argument("--session", help="指定会话 id（默认: 最近完成请求所在根会话）")
    ap.add_argument("--out", help="--snapshot 的输出文件路径")
    args = ap.parse_args(argv)

    if args.setup_menu:
        setup_menu()
        return 0
    if args.list_sessions:
        conn = connect_db()
        if conn is None or not db_usable(conn):
            print("list-sessions 需要 db.sqlite（降级源不支持）", file=sys.stderr)
            return 1
        print(list_sessions(conn))
        return 0

    st, _conn = get_stats(args)
    if args.snapshot:
        if st is not None and args.out:
            snapshot(args.out, st)
        return 0  # hook 场景始终成功
    if st is None:
        print("未找到任何模型请求数据（db 无该表且 rollout 目录无 model-io 文件）", file=sys.stderr)
        return 1
    if args.watch is not None:
        interval = max(0.5, args.watch)
        try:
            while True:
                st, _c = get_stats(args)
                sys.stdout.write("\033[2J\033[H")  # 清屏+光标归位，无闪烁依赖
                if st is None:
                    print("未找到模型请求数据（等待中…）")
                elif args.panel:
                    print(render_panel(st))
                else:
                    print(render_line(st))
                print(f"\n(每 {interval:g}s 刷新 · Ctrl+C 退出)")
                sys.stdout.flush()
                time.sleep(interval)
        except KeyboardInterrupt:
            return 0
    if args.json:
        print(json.dumps(st, ensure_ascii=False, indent=1))
    elif args.menubar:
        print(render_menubar(st))
    elif args.line:
        print(render_line(st))
    else:
        print(render_panel(st))
    return 0


if __name__ == "__main__":
    sys.exit(main())
