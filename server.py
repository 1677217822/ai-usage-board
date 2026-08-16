#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py — AI 用量看板（零依赖，纯 stdlib）

启动：pythonw server.py   （已运行则直接复用并打开浏览器）
打开：http://127.0.0.1:8765
"""
import json
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import tokenscope as K  # noqa: E402

HOST, PORT = "127.0.0.1", 8765
HTML_FILE = BASE / "panel.html"

# 各来源默认单价（每百万 token）。kimi = 官方价目（元）：
# https://www.kimi.com/zh-cn/help/kimi-api/api-pricing
# claude = 用户 Any Router 实际路由价（美元，2026-08-10 截图：提示$5/缓存$0.5/创建$6.25/补全$25）
# 其他来源默认不估算；可在 tokenscope.config.json 的 "prices" 按来源补费率
PRICES_DEFAULT = {
    "kimi": {"input": 20.0, "cacheRead": 2.0, "cacheCreate": 20.0, "output": 100.0, "cur": "¥"},
    "claude": {"input": 5.0, "cacheRead": 0.5, "cacheCreate": 6.25, "output": 25.0, "cur": "$"},
    # pi 日志自带成本（USD）优先；缺失时按 kimi 官方价估算（这里 pi 跑的是 k3-256k，走 Kimi 计费）
    "pi": {"input": 20.0, "cacheRead": 2.0, "cacheCreate": 20.0, "output": 100.0, "cur": "¥"},
    # dsh（DeepSeek Harness）实测走 kimi-coding provider（模型 k3），按 Kimi 官方价估算
    "dsh": {"input": 20.0, "cacheRead": 2.0, "cacheCreate": 20.0, "output": 100.0, "cur": "¥"},
}

# 按模型定价（每百万 token），优先于来源价——同一来源可能路由到多家模型，
# 用来源统一价会算错（例如 claude/dsh 里跑的 deepseek 远便宜于 kimi）。
# deepseek = 官方平峰价（元，2026-08：flash 1/0.02/2，pro 3/0.025/6，峰时另算）：
# https://api-docs.deepseek.com/quick_start/pricing
MODEL_PRICES_DEFAULT = {
    "k3": {"input": 20.0, "cacheRead": 2.0, "cacheCreate": 20.0, "output": 100.0, "cur": "¥"},
    "k3-256k": {"input": 20.0, "cacheRead": 2.0, "cacheCreate": 20.0, "output": 100.0, "cur": "¥"},
    "kimi-k3": {"input": 20.0, "cacheRead": 2.0, "cacheCreate": 20.0, "output": 100.0, "cur": "¥"},
    "deepseek-v4-flash": {"input": 1.0, "cacheRead": 0.02, "cacheCreate": 1.0, "output": 2.0, "cur": "¥"},
    "deepseek-v4-pro": {"input": 3.0, "cacheRead": 0.025, "cacheCreate": 3.0, "output": 6.0, "cur": "¥"},
    "deepseek-v4-flash-free": {"input": 0.0, "cacheRead": 0.0, "cacheCreate": 0.0, "output": 0.0, "cur": "¥"},
    # grok-4.5 = xAI 官方价（美元：<200k 上下文档 $2/$0.3/$6）
    "grok-4.5": {"input": 2.0, "cacheRead": 0.3, "cacheCreate": 2.0, "output": 6.0, "cur": "$"},
}


def prices():
    """{"src": 来源价, "model": 模型价}，均可被 tokenscope.config.json 覆盖
    （"prices" 覆盖来源价，"modelPrices" 覆盖模型价）。"""
    p = {"src": {k: dict(v) for k, v in PRICES_DEFAULT.items()},
         "model": {k: dict(v) for k, v in MODEL_PRICES_DEFAULT.items()}}
    for src, v in (K.config().get("prices") or {}).items():
        if isinstance(v, dict):
            p["src"].setdefault(src, {}).update(v)
    for m, v in (K.config().get("modelPrices") or {}).items():
        if isinstance(v, dict):
            p["model"].setdefault(m, {}).update(v)
    return p


def _price_for(r, p):
    """该记录适用的费率：模型价优先，来源价兜底。"""
    sp = p["model"].get(norm_model(r.get("model", "")))
    return sp or p["src"].get(r.get("source", "kimi"))


def rec_cost(r, p):
    """单条请求的估算成本。日志自带成本（pi/opencode）优先；无费率返回 None。"""
    if r.get("cost") is not None:
        return r["cost"]
    sp = _price_for(r, p)
    if not sp or "input" not in sp:
        return None
    return (r["input"] * sp.get("input", 0) + r["cacheRead"] * sp.get("cacheRead", 0)
            + r["cacheCreate"] * sp.get("cacheCreate", 0) + r["output"] * sp.get("output", 0)) / 1e6


def rec_cur(r, p):
    """该记录成本的币种符号。"""
    if r.get("cost") is not None:
        return "$"
    return (_price_for(r, p) or {}).get("cur", "¥")


def day_start(ms_offset_days=0):
    now = time.time() + ms_offset_days * 86400
    d = datetime.fromtimestamp(now)
    return int(datetime(d.year, d.month, d.day).timestamp() * 1000)


def range_bounds(q):
    """解析时间范围：优先 start/end（毫秒），否则 range 预设。返回 (start_ms, end_ms)。"""
    now_ms = int(time.time() * 1000)
    if q.get("start", [""])[0]:
        start = int(q["start"][0])
        end_raw = q.get("end", [""])[0]
        end = int(end_raw) if end_raw else now_ms  # end 为空 = 跟随当前时刻
        return start, min(end, now_ms)
    rng = q.get("range", ["today"])[0]
    if rng == "all":
        return 0, now_ms
    if rng == "today":
        return day_start(), now_ms
    days = {"1d": 1, "7d": 7, "14d": 14, "30d": 30}.get(rng, 1)
    return now_ms - days * 86400000, now_ms


def norm_model(m):
    """归一化模型名：kimi 记录带 kimi-code/ 前缀，其他来源是裸名，统一成裸名。"""
    return (m or "").replace("kimi-code/", "")


def filtered(recs, q):
    lo, hi = range_bounds(q)
    model = q.get("model", [""])[0]
    sid = q.get("session", [""])[0]
    source = q.get("source", [""])[0]
    out = []
    for r in recs:
        if not (lo <= r["time"] <= hi):
            continue
        if source and r.get("source", "kimi") != source:
            continue
        if model and norm_model(r["model"]) != model:
            continue
        if sid and r["sid"] != sid:
            continue
        out.append(r)
    return out


def _sum_window(recs, p):
    """一组记录的聚合：token 四类 + 请求数 + 命中率 + 分币种成本。"""
    agg = {"input": 0, "output": 0, "cacheRead": 0, "cacheCreate": 0}
    costs = {}
    for r in recs:
        for k in agg:
            agg[k] += r[k]
        c = rec_cost(r, p)
        if c is not None:
            cur = rec_cur(r, p)
            costs[cur] = costs.get(cur, 0.0) + c
    total_in = agg["input"] + agg["cacheRead"] + agg["cacheCreate"]
    single = len(costs) == 1
    return {
        "totalTokens": total_in + agg["output"],
        "requests": len(recs),
        "hitRate": (agg["cacheRead"] / total_in) if total_in else 0,
        # 单币种时给数值+符号；多币种各给各的；无价目为 None
        "cost": round(next(iter(costs.values())), 4) if single else None,
        "costCur": next(iter(costs)) if single else None,
        "costs": {k: round(v, 4) for k, v in costs.items()},
        "input": agg["input"], "output": agg["output"],
        "cacheRead": agg["cacheRead"], "cacheCreate": agg["cacheCreate"],
    }


def api_overview(q):
    recs = filtered(K.request_records(), q)
    p = prices()
    out = _sum_window(recs, p)
    # 环比：同长度紧邻的前一周期（「全部」和自定义起止不提供环比）
    if not q.get("start", [""])[0] and q.get("range", ["today"])[0] != "all":
        lo, hi = range_bounds(q)
        dur = hi - lo
        prev_q = {"start": [str(lo - dur)], "end": [str(lo)],
                  "model": q.get("model", [""]), "session": q.get("session", [""]),
                  "source": q.get("source", [""])}
        prev = _sum_window(filtered(K.request_records(), prev_q), p)
        out["prev"] = {"totalTokens": prev["totalTokens"], "costs": prev["costs"],
                       "requests": prev["requests"]}
    out["highlights"] = _highlights(recs, p)
    # 统计速览：活跃天数 / 最忙的一天 / 峰值小时
    days, hours = {}, [0] * 24
    for r in recs:
        d = datetime.fromtimestamp(r["time"] / 1000)
        t = r["input"] + r["cacheRead"] + r["cacheCreate"] + r["output"]
        days[d.strftime("%m-%d")] = days.get(d.strftime("%m-%d"), 0) + t
        hours[d.hour] += t
    busiest = max(days.items(), key=lambda kv: kv[1]) if days else (None, 0)
    out["stats"] = {
        "activeDays": len(days),
        "busiestDay": busiest[0], "busiestTokens": busiest[1],
        "peakHour": hours.index(max(hours)) if any(hours) else None,
    }
    return out


def _highlights(recs, p):
    """趣味极值单：最贵 / token 最大 / 首字最快 / 耗时最长。"""
    best = {}
    for r in recs:
        tot = r["input"] + r["cacheRead"] + r["cacheCreate"] + r["output"]
        c = rec_cost(r, p)
        if c is not None and ("cost" not in best or c > best["cost"][0]):
            best["cost"] = (c, r)
        if tot and ("tokens" not in best or tot > best["tokens"][0]):
            best["tokens"] = (tot, r)
        if r.get("ttft") and ("ttft" not in best or r["ttft"] < best["ttft"][0]):
            best["ttft"] = (r["ttft"], r)
        if r.get("dur") and ("dur" not in best or r["dur"] > best["dur"][0]):
            best["dur"] = (r["dur"], r)

    def pack(key):
        if key not in best:
            return None
        val, r = best[key]
        return {"value": val, "title": r["title"], "sid": r["sid"],
                "model": norm_model(r["model"]), "source": r.get("source", "kimi"),
                "time": r["time"], "costCur": rec_cur(r, p) if key == "cost" else None}

    return {k: pack(k) for k in ("cost", "tokens", "ttft", "dur")}


def api_heatmap(q):
    """星期×小时 使用强度矩阵（周一为首行）：token 数 + 请求数。"""
    recs = filtered(K.request_records(), q)
    tok = [[0] * 24 for _ in range(7)]
    cnt = [[0] * 24 for _ in range(7)]
    for r in recs:
        d = datetime.fromtimestamp(r["time"] / 1000)
        t = r["input"] + r["cacheRead"] + r["cacheCreate"] + r["output"]
        tok[d.weekday()][d.hour] += t
        cnt[d.weekday()][d.hour] += 1
    return {"tok": tok, "cnt": cnt}


def api_sessions(q):
    """按会话聚合：请求数、总 token、命中率、分币种成本。按成本降序（烧钱大户）。"""
    recs = filtered(K.request_records(), q)
    p = prices()
    agg = {}
    for r in recs:
        a = agg.setdefault(r["sid"], {
            "sid": r["sid"], "title": r["title"] or r["sid"][:13],
            "source": r.get("source", "kimi"), "requests": 0,
            "input": 0, "cacheRead": 0, "cacheCreate": 0, "output": 0,
            "models": {}, "costs": {}})
        a["requests"] += 1
        for k in ("input", "cacheRead", "cacheCreate", "output"):
            a[k] += r[k]
        m = norm_model(r["model"])
        a["models"][m] = a["models"].get(m, 0) + 1
        c = rec_cost(r, p)
        if c is not None:
            cur = rec_cur(r, p)
            a["costs"][cur] = a["costs"].get(cur, 0.0) + c
    rows = []
    for a in agg.values():
        total_in = a["input"] + a["cacheRead"] + a["cacheCreate"]
        rows.append({
            "sid": a["sid"], "title": a["title"], "source": a["source"],
            "model": max(a["models"], key=a["models"].get) if a["models"] else "?",
            "requests": a["requests"],
            "totalTokens": total_in + a["output"],
            "hitRate": a["cacheRead"] / total_in if total_in else 0,
            "costs": {k: round(v, 4) for k, v in a["costs"].items()},
            # 混合币种不能直接比大小，仅用于排序（同币种场景下是对的）
            "_cost": sum(a["costs"].values()) if a["costs"] else -1,
        })
    rows.sort(key=lambda r: (-r["_cost"], -r["totalTokens"]))
    for r in rows:
        del r["_cost"]
    return {"rows": rows[:50]}


def api_trend(q):
    recs = filtered(K.request_records(), q)
    start, end = range_bounds(q)
    # 「全部」从最早一条记录开始，而不是 epoch
    if q.get("range", ["today"])[0] == "all" and not q.get("start", [""])[0]:
        start = min((r["time"] for r in recs), default=day_start())
    dur = max(end - start, 1)
    # 自动选桶：目标约 48 个桶，取整到好看的时间粒度
    target = dur / 48
    bucket_ms = int(next((b for b in (5*60e3, 15*60e3, 30*60e3, 3600e3, 3*3600e3,
                                      6*3600e3, 12*3600e3, 86400e3) if b >= target), 86400e3))
    start = start - (start % bucket_ms)
    n = int((end - start) // bucket_ms) + 1
    # 桶数超限时加大粒度，保证所有数据都落进桶内
    while n > 200:
        bucket_ms *= 2
        start = start - (start % bucket_ms)
        n = int((end - start) // bucket_ms) + 1
    if bucket_ms >= 86400e3:
        fmt = lambda t: datetime.fromtimestamp(t / 1000).strftime("%m-%d")
    elif dur > 86400e3:
        fmt = lambda t: datetime.fromtimestamp(t / 1000).strftime("%m-%d %H:%M")
    else:
        fmt = lambda t: datetime.fromtimestamp(t / 1000).strftime("%H:%M")
    series = {k: [0] * n for k in ("input", "output", "cacheRead", "cacheCreate", "requests", "cost")}
    labels = [fmt(start + i * bucket_ms) for i in range(n)]
    p = prices()
    curs = set()
    by_source = {}  # 来源 -> 每桶总 token（堆叠视图用）
    for r in recs:
        i = (r["time"] - start) // bucket_ms
        if 0 <= i < n:
            for k in ("input", "output", "cacheRead", "cacheCreate"):
                series[k][i] += r[k]
            series["requests"][i] += 1
            src = r.get("source", "kimi")
            by_source.setdefault(src, [0] * n)[i] += (
                r["input"] + r["cacheRead"] + r["cacheCreate"] + r["output"])
            c = rec_cost(r, p)
            if c is not None:
                curs.add(rec_cur(r, p))
                series["cost"][i] += c
    # 只有全部成本同一币种时成本线才有意义，否则前端不画
    cost_cur = curs.pop() if len(curs) == 1 else None
    if not cost_cur:
        series["cost"] = [0] * n
    series["cost"] = [round(c, 4) for c in series["cost"]]
    return {"labels": labels, "series": series, "costCur": cost_cur, "bySource": by_source}


def api_models(q):
    """按模型聚合（当前筛选范围内）：请求数、token 分项、命中率、成本（分币种）、
    平均首字/耗时。供模型分布环图和模型效率卡使用。"""
    recs = filtered(K.request_records(), q)
    p = prices()
    agg = {}
    for r in recs:
        m = norm_model(r["model"]) or "?"
        if m in ("<synthetic>", "?"):
            continue
        a = agg.setdefault(m, {"requests": 0, "input": 0, "cacheRead": 0, "cacheCreate": 0,
                               "output": 0, "costs": {}, "ttft": [], "dur": [],
                               "sources": set()})
        a["requests"] += 1
        for k in ("input", "cacheRead", "cacheCreate", "output"):
            a[k] += r[k]
        a["sources"].add(r.get("source", "kimi"))
        src = r.get("source", "kimi")
        t = r["input"] + r["cacheRead"] + r["cacheCreate"] + r["output"]
        a.setdefault("bySrc", {})
        a["bySrc"][src] = a["bySrc"].get(src, 0) + t
        if r.get("ttft"):
            a["ttft"].append(r["ttft"])
        if r.get("dur"):
            a["dur"].append(r["dur"])
        c = rec_cost(r, p)
        if c is not None:
            cur = rec_cur(r, p)
            a["costs"][cur] = a["costs"].get(cur, 0) + c
    rows = []
    for m, a in agg.items():
        base = a["input"] + a["cacheRead"] + a["cacheCreate"]
        rows.append({
            "model": m, "requests": a["requests"],
            "total": a["input"] + a["cacheRead"] + a["cacheCreate"] + a["output"],
            "output": a["output"],
            "hitRate": (a["cacheRead"] / base) if base else None,
            "costs": {k: round(v, 4) for k, v in a["costs"].items()},
            "avgTtft": int(sum(a["ttft"]) / len(a["ttft"])) if a["ttft"] else None,
            "avgDur": int(sum(a["dur"]) / len(a["dur"])) if a["dur"] else None,
            # 输出速度 = 总输出 / 总生成耗时（tok/s）
            "speed": round(a["output"] / (sum(a["dur"]) / 1000), 1) if a["dur"] and sum(a["dur"]) else None,
            "sources": sorted(a["sources"]),
            "bySrc": a.get("bySrc", {}),  # 来源 → token，桑基图用
        })
    rows.sort(key=lambda x: -x["total"])
    return {"rows": rows}


def _sort_recs(recs, q, p):
    """排序：time(默认) 之外支持 cost/dur/ttft/total/hit；dir=asc|desc。"""
    sort = q.get("sort", ["time"])[0]
    rev = q.get("dir", ["desc"])[0] != "asc"
    if sort == "time":
        return recs if rev else list(reversed(recs))  # recs 默认已按时间倒序
    keyf = {
        "cost": lambda r: (lambda c: c if c is not None else -1)(rec_cost(r, p)),
        "dur": lambda r: r["dur"] if r["dur"] is not None else -1,
        "ttft": lambda r: r["ttft"] if r["ttft"] is not None else -1,
        "total": lambda r: r["input"] + r["cacheRead"] + r["cacheCreate"] + r["output"],
        "hit": lambda r: (r["cacheRead"] / (r["input"] + r["cacheRead"])
                          if (r["input"] + r["cacheRead"]) else -1),
    }.get(sort)
    return sorted(recs, key=keyf, reverse=rev) if keyf else recs


def requests_csv(q):
    """当前筛选下全部记录导成 CSV（带 BOM，Excel 直接打开不乱码）。"""
    recs = _sort_recs(filtered(K.request_records(), q), q, prices())
    p = prices()
    lines = ["时间,会话,来源,模型,首字ms,耗时ms,提示,缓存命中,缓存创建,补全,总token,命中率%,费用,结束"]
    for r in recs:
        c = rec_cost(r, p)
        base = r["input"] + r["cacheRead"]
        total = base + r["cacheCreate"] + r["output"]
        hit = f"{r['cacheRead'] / base * 100:.1f}" if base else ""
        title = (r["title"] or r["sid"][:13]).replace(",", "，").replace("\n", " ")
        cost = f"{rec_cur(r, p)}{c:.4f}" if c is not None else ""
        lines.append(",".join([
            datetime.fromtimestamp(r["time"] / 1000).strftime("%Y-%m-%d %H:%M:%S"),
            title, r.get("source", "kimi"), norm_model(r["model"]),
            str(r["ttft"] or ""), str(r["dur"] or ""),
            str(r["input"]), str(r["cacheRead"]), str(r["cacheCreate"]), str(r["output"]),
            str(total), hit, cost, r.get("finish", "")]))
    return "﻿" + "\n".join(lines)


def api_requests(q):
    p = prices()
    recs = _sort_recs(filtered(K.request_records(), q), q, p)
    # all=1：不分页（气泡图等可视化用），封顶 5000 条，附带 epoch 毫秒 ts
    all_mode = q.get("all", [""])[0] == "1"
    page = max(1, int(q.get("page", ["1"])[0]))
    per = 50
    window = recs[:5000] if all_mode else recs[(page - 1) * per: page * per]
    rows = []
    for r in window:
        c = rec_cost(r, p)
        rows.append({
            "time": datetime.fromtimestamp(r["time"] / 1000).strftime("%m-%d %H:%M:%S"),
            "ts": r["time"],
            "sid": r["sid"], "title": r["title"] or r["sid"][:13],
            "source": r.get("source", "kimi"),
            "model": r["model"], "ttft": r["ttft"], "dur": r["dur"],
            "input": r["input"], "cacheRead": r["cacheRead"],
            "cacheCreate": r["cacheCreate"], "output": r["output"],
            "cost": round(c, 6) if c is not None else None,
            "costCur": rec_cur(r, p) if c is not None else None,
            "finish": r.get("finish", ""), "agent": r.get("agent", "main"),
        })
    return {"total": len(recs), "page": page, "per": per, "rows": rows}


ALL_SOURCES = ["kimi", "claude", "codex", "pi", "grok", "opencode", "dsh"]


def api_meta():
    """来源列表 + 按来源分组的模型/会话（前端三级联动用）。
    模型名归一化去重；<synthetic> 等占位伪模型不进筛选项。"""
    recs = K.request_records()
    models_by_src, sessions_by_src = {}, {}
    for r in recs:
        src = r.get("source", "kimi")
        m = norm_model(r["model"])
        if m and m not in ("<synthetic>", "?"):
            models_by_src.setdefault(src, set()).add(m)
        sessions_by_src.setdefault(src, {}).setdefault(r["sid"], r["title"] or r["sid"][:13])
    sources = sorted(set(ALL_SOURCES) | set(models_by_src) | set(sessions_by_src))
    return {"sources": sources,
            "models": {k: sorted(v) for k, v in models_by_src.items()},
            "sessions": {s: [{"id": a, "title": b} for a, b in v.items()]
                         for s, v in sessions_by_src.items()}}


class Handler(BaseHTTPRequestHandler):
    def _send(self, data, code=200, ctype="application/json; charset=utf-8"):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                self._send(HTML_FILE.read_bytes(), ctype="text/html; charset=utf-8")
            elif u.path == "/api/overview":
                self._send(api_overview(q))
            elif u.path == "/api/trend":
                self._send(api_trend(q))
            elif u.path == "/api/requests":
                if q.get("format", [""])[0] == "csv":
                    self._send(requests_csv(q).encode("utf-8"),
                               ctype="text/csv; charset=utf-8")
                else:
                    self._send(api_requests(q))
            elif u.path == "/api/sessions":
                self._send(api_sessions(q))
            elif u.path == "/api/models":
                self._send(api_models(q))
            elif u.path == "/api/heatmap":
                self._send(api_heatmap(q))
            elif u.path == "/api/meta":
                self._send(api_meta())
            else:
                self._send({"error": "not found"}, 404)
        except Exception as e:
            self._send({"error": f"{type(e).__name__}: {e}"}, 500)


def main():
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        # 已在运行：直接打开浏览器复用
        webbrowser.open(f"http://{HOST}:{PORT}")
        return
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    print(f"面板运行中: http://{HOST}:{PORT}  (Ctrl+C 停止)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
