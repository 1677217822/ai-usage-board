#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tokenscope.py — AI 使用日志核心库：读取本地各 AI 终端的会话日志，产出统一请求明细。

数据来源：
  kimi    Kimi Code    ~/.kimi-code/sessions/wd_*/session_*/agents/*/wire.jsonl
  其他来源（claude/codex/pi/grok）见 sources.py

产出记录：{time(ms), model, ttft, dur, input, cacheRead, cacheCreate, output,
           finish, sid, title, cwd, agent, source, cost(None=未知)}
"""
import json
from pathlib import Path

HOME = Path.home()
BASE = Path(__file__).resolve().parent
SESSIONS_ROOT = HOME / ".kimi-code" / "sessions"
CONFIG_FILE = BASE / "tokenscope.config.json"


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def config():
    """面板配置（目前只有 prices 费率覆盖）。文件不存在时返回空。"""
    return load_json(CONFIG_FILE, {})


_REQ_CACHE = {}


def _parse_wire_requests(wire_path):
    """解析单个 wire.jsonl，产出每请求明细（step.end 含首字延迟/耗时/token）。
    带 (size, mtime) 缓存。"""
    try:
        st = wire_path.stat()
    except Exception:
        return []
    key = str(wire_path)
    sig = (st.st_size, st.st_mtime)
    if _REQ_CACHE.get(key, (None,))[0] == sig:
        return _REQ_CACHE[key][1]
    recs = []
    cur_model = None
    try:
        with wire_path.open(encoding="utf-8", errors="ignore") as f:
            for line in f:
                if '"type":"llm.request"' in line:
                    try:
                        d = json.loads(line)
                        cur_model = d.get("modelAlias") or d.get("model") or cur_model
                    except Exception:
                        pass
                elif '"step.end"' in line:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    ev = d.get("event", d)
                    u = ev.get("usage") or {}
                    t = d.get("time") or ev.get("time")
                    if not t:
                        continue
                    recs.append({
                        "time": t,
                        "model": cur_model or "?",
                        "ttft": ev.get("llmFirstTokenLatencyMs"),
                        "dur": ev.get("llmStreamDurationMs"),
                        "input": u.get("inputOther", 0),
                        "cacheRead": u.get("inputCacheRead", 0),
                        "cacheCreate": u.get("inputCacheCreation", 0),
                        "output": u.get("output", 0),
                        "finish": ev.get("finishReason", ""),
                    })
    except Exception:
        pass
    _REQ_CACHE[key] = (sig, recs)
    return recs


def request_records():
    """全部来源的每请求明细，按时间倒序。每条带 sid/title/cwd/agent/source。"""
    out = []
    for state_path in SESSIONS_ROOT.glob("wd_*/session_*/state.json"):
        st = load_json(state_path, {})
        sid = st.get("id", state_path.parent.name)
        for wire in state_path.parent.glob("agents/*/wire.jsonl"):
            for rec in _parse_wire_requests(wire):
                r = dict(rec)
                r.update({"sid": sid, "title": st.get("title") or "",
                          "cwd": st.get("cwd", ""), "agent": wire.parent.name,
                          "source": "kimi"})
                out.append(r)
    try:
        import sources
        out.extend(sources.external_records())
    except Exception:
        pass
    out.sort(key=lambda r: -r["time"])
    return out
