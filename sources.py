#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sources.py — 其他 AI 终端的用量日志适配器

支持：
  claude  Claude Code      ~/.claude/projects/**/*.jsonl
  codex   OpenAI Codex     ~/.codex/sessions/**/*.jsonl (+archived_sessions)
  pi      Pi 终端          ~/.pi/agent/sessions/**/*.jsonl
  grok    Grok CLI         ~/.grok/logs/unified.jsonl（标题取 sessions/*/chat_history.jsonl）
  opencode OpenCode        ~/.local/share/opencode/opencode.db（SQLite，message 表 data JSON）
  dsh     DeepSeek Harness ~/.dsh/sessions/**/session.jsonl.zstd（多帧 zstd，借 node 解码）

产出与 ailog.request_records 统一的记录：
  {time, source, model, ttft, dur, input, cacheRead, cacheCreate, output,
   cost(None=未知), sid, title, cwd, agent}
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

HOME = Path.home()
_CACHE = {}


def _iso_ms(s):
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def _parse_file(path, kind):
    """按 (size, mtime) 缓存的单文件解析。"""
    try:
        st = path.stat()
    except Exception:
        return []
    sig = (st.st_size, st.st_mtime)
    key = (str(path), kind)
    if _CACHE.get(key, (None,))[0] == sig:
        return _CACHE[key][1]
    try:
        recs = PARSERS[kind](path)
    except Exception:
        recs = []
    _CACHE[key] = (sig, recs)
    return recs


def _rec(time, source, model, usage, **kw):
    return {"time": time, "source": source, "model": model or source,
            "ttft": kw.get("ttft"), "dur": kw.get("dur"),
            "input": usage.get("input", 0), "cacheRead": usage.get("cacheRead", 0),
            "cacheCreate": usage.get("cacheCreate", 0), "output": usage.get("output", 0),
            "cost": kw.get("cost"), "sid": kw.get("sid", ""), "title": kw.get("title", ""),
            "cwd": kw.get("cwd", ""), "agent": kw.get("agent", "main")}


def _clean_title_line(t, limit=60):
    """取第一行「像人话」的文本：跳过 XML/Markdown 头/代码/日志噪声，折叠空白。"""
    for ln in (t or "").splitlines():
        ln = " ".join(ln.split())
        if not ln or ln[0] in "#<>`$-|":
            continue
        low = ln.lower()
        if "instructions" in low or "tcp socket" in low or "agents.md" in low:
            continue
        if re.match(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", ln):  # 粘贴的日志行
            continue
        return ln[:limit]
    return ""


def _first_text(content, limit=60):
    """从各家消息 content 里提取第一段用户文本做会话标题（跳过 XML 上下文块）。"""
    if isinstance(content, str):
        return _clean_title_line(content, limit)
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") in ("text", "input_text"):
                t = _clean_title_line(c.get("text", ""), limit)
                if t:
                    return t
    return ""


# ---------- Claude Code ----------

def _parse_claude(path):
    out, seen = [], set()
    title, cwd = "", ""
    with path.open(encoding="utf-8", errors="ignore") as f:
        for line in f:
            if '"usage"' not in line and '"type":"user"' not in line and '"cwd"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            cwd = cwd or d.get("cwd", "")
            t = d.get("type")
            if t == "summary" and not title:
                title = d.get("summary", "")[:60]
            elif t == "user" and not title:
                m = d.get("message", {})
                c = m.get("content")
                if isinstance(c, str) or (isinstance(c, list) and any(
                        isinstance(x, dict) and x.get("type") == "text" for x in c)):
                    title = _first_text(c)
            elif t == "assistant":
                m = d.get("message", {})
                u = m.get("usage") or {}
                if not u:
                    continue
                mid = m.get("id") or line[:64]
                if mid in seen:
                    continue
                seen.add(mid)
                ts = _iso_ms(d.get("timestamp", ""))
                if not ts:
                    continue
                out.append(_rec(ts, "claude", m.get("model", ""), {
                    "input": u.get("input_tokens", 0),
                    "cacheRead": u.get("cache_read_input_tokens", 0),
                    "cacheCreate": u.get("cache_creation_input_tokens", 0),
                    "output": u.get("output_tokens", 0),
                }, sid=d.get("sessionId", path.stem)))
    for r in out:
        r["title"], r["cwd"] = title, cwd
    return out


# ---------- OpenAI Codex ----------

def _codex_names():
    """~/.codex/session_index.jsonl → {thread_id: 会话名}（codex 自动命名/用户改名）。
    带 (size, mtime) 缓存。"""
    idx_file = HOME / ".codex" / "session_index.jsonl"
    try:
        st = idx_file.stat()
    except Exception:
        return {}
    sig = (st.st_size, st.st_mtime)
    key = ("codex", "index")
    if _CACHE.get(key, (None,))[0] == sig:
        return _CACHE[key][1]
    out = {}
    try:
        with idx_file.open(encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("id") and d.get("thread_name"):
                    out[d["id"]] = " ".join(str(d["thread_name"]).split())[:60]
    except Exception:
        pass
    _CACHE[key] = (sig, out)
    return out


def _parse_codex(path):
    out = []
    title, cwd, model, sid = "", "", "", ""
    with path.open(encoding="utf-8", errors="ignore") as f:
        for line in f:
            if "token_count" not in line and "session_meta" not in line \
                    and '"role":"user"' not in line and "turn_context" not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            p = d.get("payload", {})
            pt = p.get("type")
            if d.get("type") == "session_meta":
                sid = sid or p.get("session_id") or p.get("id") or ""
                cwd = cwd or p.get("cwd", "")
                model = model or p.get("model", "")
            elif pt == "turn_context":
                model = p.get("model") or model
                cwd = cwd or p.get("cwd", "")
            elif not title and pt == "message" and p.get("role") == "user":
                title = _first_text(p.get("content"))
            elif pt == "token_count":
                u = (p.get("info") or {}).get("last_token_usage") or {}
                ts = _iso_ms(d.get("timestamp", ""))
                if not ts or not u:
                    continue
                out.append(_rec(ts, "codex", model, {
                    "input": u.get("input_tokens", 0),
                    "cacheRead": u.get("cached_input_tokens", 0),
                    "output": u.get("output_tokens", 0) + u.get("reasoning_output_tokens", 0),
                }, sid=sid or p.get("info", {}).get("session_id", "") or path.stem))
    # 会话名优先用 codex 索引里的命名（sid 与 rollout 文件名里的 UUID 都可能命中）
    names = _codex_names()
    tail = path.stem.rsplit("-", 5)[-5:]
    uuid = "-".join(tail) if len(tail) == 5 else ""
    named = names.get(sid) or names.get(uuid) or ""
    for r in out:
        r["title"], r["cwd"] = named or title or path.stem[:20], cwd
        if model:
            r["model"] = model
    return out


# ---------- Pi ----------

def _parse_pi(path):
    out = []
    title, cwd = "", ""
    with path.open(encoding="utf-8", errors="ignore") as f:
        for line in f:
            if '"usage"' not in line and '"type":"session"' not in line \
                    and '"role":"user"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") == "session":
                cwd = cwd or d.get("cwd", "")
                continue
            if d.get("type") != "message":
                continue
            m = d.get("message", {})
            if m.get("role") == "user":
                if not title:
                    title = _first_text(m.get("content"))
                continue
            if m.get("role") != "assistant":
                continue
            u = m.get("usage") or {}
            ts = m.get("timestamp") or _iso_ms(d.get("timestamp", ""))
            if not ts or not any(u.get(k) for k in ("input", "output", "cacheRead")):
                continue
            cost = (u.get("cost") or {}).get("total")
            out.append(_rec(int(ts), "pi", m.get("model", ""), {
                "input": u.get("input", 0), "cacheRead": u.get("cacheRead", 0),
                "cacheCreate": u.get("cacheWrite", 0), "output": u.get("output", 0),
            }, cost=cost if cost else None, sid=d.get("sessionId", path.stem.split("_")[-1])))
    for r in out:
        r["title"], r["cwd"] = title, cwd
    return out


# ---------- Grok CLI ----------

def _grok_meta():
    """sid -> (首条用户消息, cwd)；cwd 由 URL 编码的上级目录名还原。"""
    out = {}
    root = HOME / ".grok" / "sessions"
    for ch in root.glob("*/*/chat_history.jsonl"):
        sid = ch.parent.name
        cwd = unquote(ch.parent.parent.name)
        if not re.match(r"^([A-Za-z]:[\\/]|/)", cwd):
            cwd = ""
        title = ""
        for line in ch.open(encoding="utf-8", errors="ignore"):
            if '"type":"user"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("text") or _first_text(d.get("content"))
            if t and not t.lstrip().startswith("<"):
                title = t[:60]
                break
        out[sid] = (title, cwd)
    return out


def _parse_grok(path):
    metas = _grok_meta()
    out = []
    with path.open(encoding="utf-8", errors="ignore") as f:
        for line in f:
            if "inference_done" not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            c = d.get("ctx", {})
            ts = _iso_ms(d.get("ts", ""))
            if not ts or "prompt_tokens" not in c:
                continue
            sid = d.get("sid", "")
            title, cwd = metas.get(sid, ("", ""))
            out.append(_rec(ts, "grok", "grok", {
                "input": c.get("prompt_tokens", 0),
                "cacheRead": c.get("cached_prompt_tokens", 0),
                "output": c.get("completion_tokens", 0) + c.get("reasoning_tokens", 0),
            }, ttft=c.get("ttft_ms"), dur=c.get("model_elapsed_ms"),
                sid=sid, title=title, cwd=cwd))
    return out


# ---------- OpenCode ----------

OPENCODE_DB = HOME / ".local" / "share" / "opencode" / "opencode.db"


def _opencode_sig(db):
    """db + wal 的 (size, mtime) 签名（WAL 模式下主文件 mtime 可能不变）。"""
    sig = []
    for f in (db, db.parent / (db.name + "-wal")):
        try:
            st = f.stat()
            sig.append((st.st_size, st.st_mtime))
        except Exception:
            sig.append(None)
    return tuple(sig)


def _parse_opencode_db():
    if not OPENCODE_DB.is_file():
        return []
    sig = _opencode_sig(OPENCODE_DB)
    key = ("opencode", "db")
    if _CACHE.get(key, (None,))[0] == sig:
        return _CACHE[key][1]
    out = []
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True)
        try:
            sessions = {r[0]: (r[1] or "", r[2] or "") for r in
                        con.execute("SELECT id, title, directory FROM session")}
            rows = con.execute(
                "SELECT session_id, data FROM message "
                "WHERE json_extract(data, '$.role') = 'assistant'")
            for sid, data in rows:
                try:
                    m = json.loads(data)
                except Exception:
                    continue
                t = m.get("tokens") or {}
                created = (m.get("time") or {}).get("created")
                if not created or not any(t.get(k) for k in ("input", "output")):
                    continue
                cache = t.get("cache") or {}
                completed = (m.get("time") or {}).get("completed")
                cost = m.get("cost")
                title, cwd = sessions.get(sid, ("", ""))
                out.append(_rec(int(created), "opencode", m.get("modelID", ""), {
                    "input": t.get("input", 0),
                    "cacheRead": cache.get("read", 0),
                    "cacheCreate": cache.get("write", 0),
                    "output": t.get("output", 0) + t.get("reasoning", 0),
                }, dur=(int(completed - created) if completed else None),
                    cost=cost if cost else None,
                    sid=sid or "", title=title, cwd=cwd))
        finally:
            con.close()
    except Exception:
        out = []
    _CACHE[key] = (sig, out)
    return out


# ---------- DeepSeek Harness (dsh) ----------

DSH_DIR = HOME / ".dsh" / "sessions"
DSH_DECODER = Path(__file__).resolve().parent / "dsh_decode.js"


def _parse_dsh(path):
    """session.jsonl.zstd（多帧拼接）→ 借 node:zlib 解码 → 逐事件提取 LLM 用量。

    每个 step = 一次模型调用：step/start 记起点，首个 assistant/chunk 记首字，
    assistant/message 记完成与 usage（inputTokens 不含 cacheReadTokens，实测
    input+cacheRead 随上下文单调递增）。
    """
    try:
        st = path.stat()
    except Exception:
        return []
    sig = (st.st_size, st.st_mtime)
    key = ("dsh", str(path))
    if _CACHE.get(key, (None,))[0] == sig:
        return _CACHE[key][1]
    out = []
    try:
        import subprocess
        # pythonw 无控制台，直接起 node（控制台程序）每次都会闪一个黑窗；
        # CREATE_NO_WINDOW 让子进程不创建窗口
        r = subprocess.run(["node", str(DSH_DECODER), str(path)],
                           capture_output=True, timeout=120,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = r.stdout.decode("utf-8", "ignore")
        sid, cwd, title = "", "", ""
        step_start, step_ttft = {}, {}
        for line in text.splitlines():
            if '"usage"' not in line and '"type"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type")
            data = d.get("data") or {}
            if t == "session":
                sid, cwd = d.get("id", ""), d.get("cwd", "")
            elif t == "session/title" and not title:
                title = data.get("title", "")[:60]
            elif t == "step/start":
                step_start[(data.get("turn"), data.get("step"))] = d.get("time")
            elif t == "assistant/chunk":
                k = (data.get("turn"), data.get("step"))
                if k not in step_ttft and k in step_start and d.get("time"):
                    step_ttft[k] = d["time"] - step_start[k]
            elif t == "assistant/message" and data.get("usage"):
                u = data["usage"]
                ts = d.get("time")
                if not ts:
                    continue
                m = (data.get("message") or {}).get("source") or {}
                k = (data.get("turn"), data.get("step"))
                start = step_start.get(k)
                out.append(_rec(int(ts), "dsh", m.get("model", ""), {
                    "input": u.get("inputTokens", 0),
                    "cacheRead": u.get("cacheReadTokens", 0),
                    "output": u.get("outputTokens", 0),
                }, ttft=step_ttft.get(k),
                    dur=(ts - start) if start else None,
                    sid=sid or path.parent.name, title=title, cwd=cwd,
                    agent="main"))
    except Exception:
        out = []
    _CACHE[key] = (sig, out)
    return out


PARSERS = {"claude": _parse_claude, "codex": _parse_codex,
           "pi": _parse_pi, "grok": _parse_grok}

SOURCES = {
    "claude": [HOME / ".claude" / "projects"],
    "codex": [HOME / ".codex" / "sessions", HOME / ".codex" / "archived_sessions"],
    "pi": [HOME / ".pi" / "agent" / "sessions"],
    "grok": [HOME / ".grok" / "logs"],
}


def external_records():
    out = []
    for source, roots in SOURCES.items():
        for root in roots:
            if not root.is_dir():
                continue
            for f in root.rglob("*.jsonl"):
                out.extend(_parse_file(f, source))
    out.extend(_parse_opencode_db())
    if DSH_DIR.is_dir() and DSH_DECODER.is_file():
        for f in DSH_DIR.rglob("*.jsonl.zstd"):
            out.extend(_parse_dsh(f))
    return out
