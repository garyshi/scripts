#!/usr/bin/env python3
"""Convert a Claude Code session log (.jsonl) into a readable Markdown chat.

Claude Code stores each session as a JSON-Lines file under
``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl``. Each line is one
event. This tool walks those events and renders the human/assistant turns as a
clean Markdown transcript, similar to a ChatGPT/Claude conversation export.

Usage:
    python render_claude_session.py SESSION.jsonl [-o OUTPUT]
    python render_claude_session.py SESSION.jsonl                  # writes SESSION.md
    python render_claude_session.py SESSION.jsonl --format html    # writes SESSION.html
    python render_claude_session.py SESSION.jsonl -                # print to stdout

Options:
    --format md|html   Output format (default: md).
    --no-thinking      Omit assistant "thinking" blocks.
    --no-tools         Omit tool calls and their results entirely.
    --tool-output N    Truncate each tool result to N chars (default 1500, 0 = no limit).
    --no-fold          Render thinking/tool sections expanded instead of collapsed.

Both formats include a Table of Contents. Thinking and tool sections are
collapsible: in Markdown via HTML <details> elements (works on GitHub and many
viewers), and in HTML via native <details> that work in every browser with no
JavaScript. The HTML output is self-contained (embedded CSS + JS) and uses
marked.js from a CDN to prettify prose, degrading to readable raw text offline.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import json
import sys
from pathlib import Path

import click


def _fmt_time(ts: str | None) -> str:
    """Format an ISO-8601 timestamp into a short local-ish string."""
    if not ts:
        return ""
    try:
        # Stored as e.g. "2026-06-18T12:34:56.789Z"
        dt = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts


def _blocks(content) -> list:
    """Normalize a message ``content`` field into a list of block dicts.

    Content may be a plain string (treated as a single text block) or a list of
    typed blocks (text / thinking / tool_use / tool_result / image / ...).
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict):
                out.append(b)
            elif isinstance(b, str):
                out.append({"type": "text", "text": b})
        return out
    return []


def _stringify_tool_result(content) -> str:
    """Tool results may be a string or a list of {type:text,text:...} blocks."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif b.get("type") == "image":
                    parts.append("[image]")
                else:
                    parts.append(json.dumps(b, ensure_ascii=False))
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False)


def _truncate(text: str, limit: int) -> str:
    if limit and len(text) > limit:
        return text[:limit] + f"\n… [truncated {len(text) - limit} chars]"
    return text


def _fence(text: str, lang: str = "") -> str:
    """Wrap text in a code fence that is long enough to not collide with backticks."""
    longest = 0
    run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{text}\n{fence}"


def _details(summary: str, body: str, *, fold: bool, open_: bool = False) -> str:
    """Wrap body in a collapsible <details> block, or render it inline if not folding.

    A blank line after <summary> and around the body is required so the inner
    Markdown (code fences, quotes, lists) is still parsed inside the HTML block.
    """
    if not fold:
        return f"{summary}\n\n{body}"
    attr = " open" if open_ else ""
    return f"<details{attr}>\n<summary>{summary}</summary>\n\n{body}\n\n</details>"


# USD per 1M tokens (input, output), per model ID. Source: claude-api skill
# model catalog (cached 2026-06-04). Cache multipliers applied on the input rate:
# read ~0.1x, 5-minute write 1.25x, 1-hour write 2x.
_PRICING = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-opus-4-0": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4-0": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_CACHE_READ_MULT = 0.1
_CACHE_WRITE_5M_MULT = 1.25
_CACHE_WRITE_1H_MULT = 2.0


def _price_for(model: str) -> tuple[float, float] | None:
    """Look up (input, output) $/1M for a model ID, tolerating suffixes/prefixes."""
    if not model:
        return None
    if model in _PRICING:
        return _PRICING[model]
    # e.g. "anthropic.claude-opus-4-8" (Bedrock) or "claude-opus-4-8-20260101"
    for mid, price in _PRICING.items():
        if mid in model:
            return price
    return None


def _request_cost(usage: dict, price: tuple[float, float]) -> float:
    """Estimated USD cost of one API request from its usage payload."""
    in_rate, out_rate = price[0] / 1e6, price[1] / 1e6
    cc = usage.get("cache_creation") or {}
    write_5m = cc.get("ephemeral_5m_input_tokens") or 0
    write_1h = cc.get("ephemeral_1h_input_tokens") or 0
    if not cc:  # older logs lack the breakdown; assume default 5-minute TTL
        write_5m = usage.get("cache_creation_input_tokens") or 0
    return (
        (usage.get("input_tokens") or 0) * in_rate
        + (usage.get("cache_read_input_tokens") or 0) * in_rate * _CACHE_READ_MULT
        + write_5m * in_rate * _CACHE_WRITE_5M_MULT
        + write_1h * in_rate * _CACHE_WRITE_1H_MULT
        + (usage.get("output_tokens") or 0) * out_rate
    )


def _collect_usage(events: list[dict]) -> dict | None:
    """Aggregate token usage across the session, deduped by request.

    Claude Code splits one API response across several assistant events that
    repeat the same ``usage`` payload, so summing raw events double-counts.
    Keying on ``requestId`` charges each API call exactly once.
    """
    seen: set = set()
    agg = {"requests": 0, "input": 0, "output": 0, "cache_read": 0,
           "cache_creation": 0, "web_search": 0, "web_fetch": 0, "models": set(),
           "cost": 0.0, "unpriced": set()}
    for e in events:
        if e.get("type") != "assistant":
            continue
        msg = e.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        # Synthetic messages (interrupts, injected errors) aren't real API calls.
        if msg.get("model") == "<synthetic>":
            continue
        key = e.get("requestId") or id(e)
        if key in seen:
            continue
        seen.add(key)
        agg["requests"] += 1
        agg["input"] += usage.get("input_tokens") or 0
        agg["output"] += usage.get("output_tokens") or 0
        agg["cache_read"] += usage.get("cache_read_input_tokens") or 0
        agg["cache_creation"] += usage.get("cache_creation_input_tokens") or 0
        stu = usage.get("server_tool_use") or {}
        agg["web_search"] += stu.get("web_search_requests") or 0
        agg["web_fetch"] += stu.get("web_fetch_requests") or 0
        model = msg.get("model") or ""
        if model:
            agg["models"].add(model)
        price = _price_for(model)
        if price:
            agg["cost"] += _request_cost(usage, price)
        else:
            agg["unpriced"].add(model)
    if agg["requests"] == 0:
        return None
    agg["total"] = agg["input"] + agg["output"] + agg["cache_read"] + agg["cache_creation"]
    agg["models"] = sorted(agg["models"])
    agg["unpriced"] = sorted(agg["unpriced"])
    return agg


def _usage_summary(u: dict) -> str:
    """A compact one-line token breakdown."""
    parts = [
        f"{u['total']:,} total",
        f"{u['input']:,} input",
        f"{u['output']:,} output",
        f"{u['cache_read']:,} cache read",
        f"{u['cache_creation']:,} cache write",
    ]
    extra = f" across {u['requests']:,} request{'s' if u['requests'] != 1 else ''}"
    if u["web_search"]:
        extra += f", {u['web_search']:,} web search"
    if u["web_fetch"]:
        extra += f", {u['web_fetch']:,} web fetch"
    return f"{parts[0]} — " + " · ".join(parts[1:]) + extra


def _fmt_cost(cost: float) -> str:
    return f"${cost:,.2f}" if cost >= 0.01 else f"${cost:,.4f}"


def _cost_summary(u: dict) -> str:
    """A one-line estimated-cost string, or '' if nothing could be priced."""
    cost = u.get("cost") or 0.0
    if cost <= 0 and not u.get("unpriced"):
        return ""
    note = " (approx; some models unpriced)" if u.get("unpriced") else " (approx)"
    return _fmt_cost(cost) + note


# Columns of the usage table: (header, value-from-usage-dict). Order left-to-right.
_USAGE_COLUMNS: list[tuple[str, "callable"]] = [
    ("Requests", lambda u: f"{u['requests']:,}"),
    ("Input", lambda u: f"{u['input']:,}"),
    ("Output", lambda u: f"{u['output']:,}"),
    ("Cache read", lambda u: f"{u['cache_read']:,}"),
    ("Cache write", lambda u: f"{u['cache_creation']:,}"),
    ("Total tokens", lambda u: f"{u['total']:,}"),
    ("Cost", lambda u: _fmt_cost(u.get("cost", 0.0))),
]


def _usage_rows(meta: dict) -> list[tuple[str, dict]]:
    """(scope-label, usage-dict) rows for the usage table — main/sub-agents/total
    when sub-agent transcripts exist, otherwise a single session row."""
    main = meta.get("usage")
    if not main:
        return []
    sub = meta.get("subagent_usage")
    total = meta.get("total_usage")
    if sub and total:
        n = meta.get("subagent_count", 0)
        return [("Main session", main), (f"Sub-agents ({n})", sub), ("Total", total)]
    return [("Session", main)]


def _usage_models(meta: dict) -> list[str]:
    main = meta.get("usage")
    if not main:
        return []
    return (meta.get("total_usage") or main)["models"]


def _usage_unpriced(meta: dict) -> bool:
    main = meta.get("usage")
    if not main:
        return False
    return bool((meta.get("total_usage") or main).get("unpriced"))


def _snippet(blocks: list[dict], limit: int = 70) -> str:
    """A one-line preview of a turn, for the table of contents."""
    for b in blocks:
        if b["type"] in ("text", "thinking"):
            for raw in b["text"].splitlines():
                line = raw.lstrip("#> -*`").strip()
                if line:
                    return line[:limit] + ("…" if len(line) > limit else "")
    # No prose: describe the turn by its tool activity instead of repeating the label.
    names = [b["name"] for b in blocks if b["type"] in ("tool_use", "tool_result")]
    if names:
        verb = "🔧" if blocks[0]["type"] == "tool_use" else "📤"
        uniq = list(dict.fromkeys(names))
        return f"{verb} {', '.join(uniq[:3])}" + ("…" if len(uniq) > 3 else "")
    if any(b["type"] == "image" for b in blocks):
        return "🖼️ image"
    return ""


def _load_events(path: Path) -> list[dict]:
    """Read a .jsonl transcript into a list of event dicts, skipping bad lines."""
    events = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _sum_usages(parts: list[dict | None]) -> dict | None:
    """Combine several usage dicts (from _collect_usage) into one aggregate."""
    parts = [p for p in parts if p]
    if not parts:
        return None
    nums = ["requests", "input", "output", "cache_read", "cache_creation",
            "web_search", "web_fetch", "total", "cost"]
    agg = {k: 0 for k in nums}
    models: set = set()
    unpriced: set = set()
    for p in parts:
        for k in nums:
            agg[k] += p.get(k, 0)
        models.update(p.get("models") or [])
        unpriced.update(p.get("unpriced") or [])
    agg["models"] = sorted(models)
    agg["unpriced"] = sorted(unpriced)
    return agg


def parse_session(path: Path, *, show_thinking: bool, show_tools: bool, tool_limit: int) -> tuple[dict, list[dict]]:
    """Read a session .jsonl into (metadata, turns).

    Each turn is ``{label, kind, ts, anchor, snippet, blocks}`` where blocks are
    format-neutral dicts the renderers translate to Markdown or HTML.
    """
    events = _load_events(path)

    meta: dict = {"title": None, "session_id": None, "cwd": None, "git_branch": None, "started": None}
    for e in events:
        meta["title"] = meta["title"] or e.get("aiTitle")
        meta["session_id"] = meta["session_id"] or e.get("sessionId")
        meta["cwd"] = meta["cwd"] or e.get("cwd")
        meta["git_branch"] = meta["git_branch"] or e.get("gitBranch")
        if meta["started"] is None and e.get("timestamp"):
            meta["started"] = e.get("timestamp")
    meta["title"] = meta["title"] or "Claude Code Session"
    meta["usage"] = _collect_usage(events)

    # Sub-agent transcripts live in <session-id>/subagents/agent-*.jsonl.
    sub_dir = path.with_suffix("") / "subagents"
    sub_usage = None
    sub_count = 0
    if sub_dir.is_dir():
        sub_events: list[dict] = []
        for sub_file in sorted(sub_dir.glob("agent-*.jsonl")):
            file_events = _load_events(sub_file)
            if any(e.get("type") == "assistant" for e in file_events):
                sub_count += 1
            sub_events.extend(file_events)
        sub_usage = _collect_usage(sub_events)
    meta["subagent_usage"] = sub_usage
    meta["subagent_count"] = sub_count
    meta["total_usage"] = _sum_usages([meta["usage"], sub_usage]) if sub_usage else None

    tool_names: dict[str, str] = {}  # tool_use id -> tool name, so results can be labeled
    turns: list[dict] = []

    for e in events:
        if e.get("type") not in ("user", "assistant"):
            continue
        # Sidechain entries are sub-agent transcripts; skipping keeps the main thread clean.
        if e.get("isSidechain"):
            continue
        msg = e.get("message")
        if not isinstance(msg, dict):
            continue

        role = msg.get("role") or e.get("type")
        raw_blocks = _blocks(msg.get("content"))
        is_meta = e.get("isMeta")

        blocks: list[dict] = []
        for b in raw_blocks:
            btype = b.get("type")
            if btype == "thinking":
                if not show_thinking:
                    continue
                think = (b.get("thinking") or "").strip()
                if think:
                    blocks.append({"type": "thinking", "text": think})
            elif btype == "text":
                text = (b.get("text") or "").strip()
                if text:
                    blocks.append({"type": "text", "text": text})
            elif btype == "tool_use":
                name = b.get("name", "tool")
                tool_names[b.get("id", "")] = name
                if not show_tools:
                    continue
                pretty = json.dumps(b.get("input", {}), ensure_ascii=False, indent=2)
                blocks.append({"type": "tool_use", "name": name, "input": pretty})
            elif btype == "tool_result":
                if not show_tools:
                    continue
                name = tool_names.get(b.get("tool_use_id", ""), "tool")
                out = _stringify_tool_result(b.get("content")).strip() or "(no output)"
                blocks.append({"type": "tool_result", "name": name, "output": _truncate(out, tool_limit)})
            elif btype == "image":
                blocks.append({"type": "image"})

        if not blocks:
            continue

        # tool_result-only user turns are tool outputs, not human speech.
        only_tool_results = all(b.get("type") == "tool_result" for b in raw_blocks) and bool(raw_blocks)
        if role == "user":
            kind, label = ("tool_output", "🧰 Tool Output") if only_tool_results else ("user", "👤 User")
            if is_meta and not only_tool_results:
                kind, label = "meta", "⚙️ System (meta)"
        else:
            kind, label = "assistant", "🤖 Assistant"

        idx = len(turns) + 1
        turns.append({
            "label": label,
            "kind": kind,
            "ts": _fmt_time(e.get("timestamp")),
            "anchor": f"turn-{idx}",
            "snippet": _snippet(blocks),
            "blocks": blocks,
        })

    return meta, turns


# --------------------------------------------------------------------------- #
# Markdown renderer
# --------------------------------------------------------------------------- #

def render_markdown(meta: dict, turns: list[dict], *, fold: bool) -> str:
    lines: list[str] = [f"# {meta['title']}", ""]
    info = []
    if meta["session_id"]:
        info.append(f"- **Session:** `{meta['session_id']}`")
    if meta["cwd"]:
        info.append(f"- **Directory:** `{meta['cwd']}`")
    if meta["git_branch"]:
        info.append(f"- **Git branch:** `{meta['git_branch']}`")
    if meta["started"]:
        info.append(f"- **Started:** {_fmt_time(meta['started'])}")
    models = _usage_models(meta)
    if models:
        info.append("- **Model:** " + ", ".join(f"`{m}`" for m in models))
    if info:
        lines += info + [""]

    rows = _usage_rows(meta)
    if rows:
        headers = ["Scope"] + [h for h, _ in _USAGE_COLUMNS]
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "|".join(["---"] + [" ---: "] * len(_USAGE_COLUMNS)) + "|")
        for label, u in rows:
            cells = [label] + [fn(u) for _, fn in _USAGE_COLUMNS]
            lines.append("| " + " | ".join(cells) + " |")
        if _usage_unpriced(meta):
            lines.append("")
            lines.append("> Cost is approximate; some models could not be priced.")
        else:
            lines.append("")
            lines.append("> Cost is approximate (list prices).")
        lines.append("")

    # Table of contents.
    lines += ["## Table of Contents", ""]
    for i, t in enumerate(turns, 1):
        ts = f" <sub>{t['ts']}</sub>" if t["ts"] else ""
        snip = t["snippet"].replace("[", "\\[").replace("]", "\\]")
        text = f"{t['label']} — {snip}" if snip else t["label"]
        lines.append(f"{i}. [{text}](#{t['anchor']}){ts}")
    lines += ["", "---", ""]

    for t in turns:
        # Explicit HTML anchor so ToC links are stable regardless of header slugging.
        lines.append(f'<a id="{t["anchor"]}"></a>')
        lines.append("")
        header = f"## {t['label']}"
        if t["ts"]:
            header += f"  <sub>{t['ts']}</sub>"
        lines += [header, ""]

        rendered: list[str] = []
        for b in t["blocks"]:
            if b["type"] == "text":
                rendered.append(b["text"])
            elif b["type"] == "thinking":
                rendered.append(_details("💭 Thinking", b["text"], fold=fold))
            elif b["type"] == "tool_use":
                rendered.append(_details(f"🔧 Tool call: <code>{b['name']}</code>", _fence(b["input"], "json"), fold=fold))
            elif b["type"] == "tool_result":
                rendered.append(_details(f"📤 Tool result: <code>{b['name']}</code>", _fence(b["output"]), fold=fold))
            elif b["type"] == "image":
                rendered.append("🖼️ *[image]*")
        body = "\n\n".join(rendered)
        # Meta turns are injected skill/system content — collapse them by default.
        if t["kind"] == "meta":
            body = _details(f"📄 {t['snippet'] or 'Injected content'}", body, fold=fold)
        lines.append(body)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# HTML renderer
# --------------------------------------------------------------------------- #

_HTML_STYLE = """
:root {
  --bg: #f5f6f8; --panel: #ffffff; --ink: #1f2328; --muted: #6a737d;
  --line: #e2e5ea; --accent: #6b57d2; --user: #2563eb; --assistant: #16a34a;
  --tool: #b45309; --meta: #6b7280; --code-bg: #f3f4f6;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --panel: #161b22; --ink: #e6edf3; --muted: #8b949e;
    --line: #30363d; --accent: #a78bfa; --user: #58a6ff; --assistant: #3fb950;
    --tool: #d29922; --meta: #8b949e; --code-bg: #1b2129;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.layout { display: grid; grid-template-columns: 300px 1fr; gap: 0; align-items: start; }
nav.toc {
  position: sticky; top: 0; max-height: 100vh; overflow-y: auto;
  padding: 20px 16px; border-right: 1px solid var(--line); background: var(--panel);
}
nav.toc h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); margin: 0 0 12px; }
nav.toc ol { list-style: none; margin: 0; padding: 0; counter-reset: item; }
nav.toc li { margin: 2px 0; font-size: 13px; }
nav.toc a { display: block; padding: 4px 8px; border-radius: 6px; color: var(--ink); border-left: 3px solid transparent; }
nav.toc a:hover { background: var(--bg); text-decoration: none; }
nav.toc a.active { border-left-color: var(--accent); background: var(--bg); }
nav.toc .tlabel { font-weight: 600; }
nav.toc .tsnip { color: var(--muted); }
.toc-user .tlabel { color: var(--user); }
.toc-assistant .tlabel { color: var(--assistant); }
.toc-tool_output .tlabel, .toc-meta .tlabel { color: var(--tool); }
main { padding: 28px 32px 120px; max-width: 920px; }
header.session { margin-bottom: 24px; }
header.session h1 { margin: 0 0 10px; font-size: 26px; }
header.session .meta { color: var(--muted); font-size: 13px; }
header.session .meta code { background: var(--code-bg); padding: 1px 5px; border-radius: 4px; }
header.session .usage { margin-top: 6px; color: var(--muted); font-size: 13px; }
.usage-table { border-collapse: collapse; margin: 14px 0 6px; font-size: 13px; }
.usage-table th, .usage-table td { border: 1px solid var(--line); padding: 5px 12px; }
.usage-table thead th { background: var(--code-bg); font-weight: 600; text-align: right; }
.usage-table thead th.scope, .usage-table tbody th.scope { text-align: left; font-weight: 600; }
.usage-table td { text-align: right; font-variant-numeric: tabular-nums; }
.usage-table tbody th.scope { background: color-mix(in srgb, var(--code-bg) 45%, transparent); }
.usage-table tr.total th, .usage-table tr.total td { border-top: 2px solid var(--accent); font-weight: 700; }
.usage-note { color: var(--muted); font-size: 12px; margin-bottom: 4px; }
.controls { margin: 14px 0 6px; }
.controls button {
  font: inherit; font-size: 13px; cursor: pointer; color: var(--ink); background: var(--panel);
  border: 1px solid var(--line); border-radius: 6px; padding: 5px 12px; margin-right: 8px;
}
.controls button:hover { border-color: var(--accent); color: var(--accent); }
.turn { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 16px 20px; margin: 16px 0; scroll-margin-top: 12px; }
.turn-user { border-left: 4px solid var(--user); }
.turn-assistant { border-left: 4px solid var(--assistant); }
.turn-tool_output { border-left: 4px solid var(--tool); }
.turn-meta { border-left: 4px solid var(--meta); }
.turn-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
.turn-head .role { font-weight: 700; }
.turn-head .ts { color: var(--muted); font-size: 12px; }
.prose { overflow-wrap: anywhere; }
.prose > *:first-child { margin-top: 0; }
.prose > *:last-child { margin-bottom: 0; }
.prose.raw { white-space: pre-wrap; font-family: inherit; }
.prose pre, details pre {
  background: var(--code-bg); border: 1px solid var(--line); border-radius: 8px;
  padding: 12px 14px; overflow-x: auto; font-size: 13px;
}
.prose code, details code { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace; }
.prose :not(pre) > code { background: var(--code-bg); padding: 1px 5px; border-radius: 4px; font-size: .9em; }
.prose table { border-collapse: collapse; margin: 12px 0; display: block; overflow-x: auto; max-width: 100%; }
.prose th, .prose td { border: 1px solid var(--line); padding: 6px 12px; text-align: left; }
.prose th { background: var(--code-bg); font-weight: 600; }
.prose tr:nth-child(even) td { background: color-mix(in srgb, var(--code-bg) 45%, transparent); }
details.fold { margin: 10px 0; border: 1px solid var(--line); border-radius: 8px; background: var(--bg); }
details.fold > summary {
  cursor: pointer; padding: 8px 12px; font-size: 13px; font-weight: 600; color: var(--muted);
  user-select: none; list-style: none;
}
details.fold > summary::-webkit-details-marker { display: none; }
details.fold > summary::before { content: "▸ "; }
details.fold[open] > summary::before { content: "▾ "; }
details.fold[open] > summary { border-bottom: 1px solid var(--line); }
details.fold .fold-body { padding: 10px 12px; }
details.thinking > summary { color: var(--accent); }
details.tool > summary { color: var(--tool); }
details.meta > summary { color: var(--meta); }
.image { color: var(--muted); font-style: italic; }
@media (max-width: 800px) {
  .layout { grid-template-columns: 1fr; }
  nav.toc { position: static; max-height: none; border-right: none; border-bottom: 1px solid var(--line); }
  main { padding: 20px 16px 80px; }
}
"""

_HTML_SCRIPT = """
(function () {
  function renderProse() {
    if (!window.marked) return;
    var parse = window.marked.parse || window.marked;
    document.querySelectorAll('.prose.raw').forEach(function (el) {
      try {
        el.innerHTML = parse(el.textContent);
        el.classList.remove('raw');
      } catch (e) { /* leave raw text on failure */ }
    });
  }
  // marked.js may load after this inline script; poll briefly, then give up gracefully.
  var tries = 0;
  (function wait() {
    if (window.marked) { renderProse(); }
    else if (tries++ < 50) { setTimeout(wait, 100); }
  })();

  document.querySelectorAll('[data-expand]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var open = btn.getAttribute('data-expand') === 'all';
      document.querySelectorAll('details.fold').forEach(function (d) { d.open = open; });
    });
  });

  // Highlight the ToC entry for the turn currently in view.
  var links = {};
  document.querySelectorAll('nav.toc a').forEach(function (a) {
    links[a.getAttribute('href').slice(1)] = a;
  });
  var obs = new IntersectionObserver(function (entries) {
    entries.forEach(function (en) {
      var a = links[en.target.id];
      if (a && en.isIntersecting) {
        Object.values(links).forEach(function (x) { x.classList.remove('active'); });
        a.classList.add('active');
        a.scrollIntoView({ block: 'nearest' });
      }
    });
  }, { rootMargin: '-10% 0px -80% 0px' });
  document.querySelectorAll('section.turn').forEach(function (s) { obs.observe(s); });
})();
"""

_MARKED_CDN = "https://cdn.jsdelivr.net/npm/marked@12/marked.min.js"


def _esc(text: str) -> str:
    return _html.escape(text, quote=False)


def _html_block(b: dict, *, fold: bool) -> str:
    open_attr = "" if fold else " open"
    if b["type"] == "text":
        # Raw markdown as textContent; JS (marked) upgrades it, else it shows as readable text.
        return f'<div class="prose raw">{_esc(b["text"])}</div>'
    if b["type"] == "thinking":
        return (f'<details class="fold thinking"{open_attr}><summary>💭 Thinking</summary>'
                f'<div class="fold-body"><div class="prose raw">{_esc(b["text"])}</div></div></details>')
    if b["type"] == "tool_use":
        return (f'<details class="fold tool"{open_attr}><summary>🔧 Tool call: <code>{_esc(b["name"])}</code></summary>'
                f'<div class="fold-body"><pre><code class="language-json">{_esc(b["input"])}</code></pre></div></details>')
    if b["type"] == "tool_result":
        return (f'<details class="fold tool"{open_attr}><summary>📤 Tool result: <code>{_esc(b["name"])}</code></summary>'
                f'<div class="fold-body"><pre><code>{_esc(b["output"])}</code></pre></div></details>')
    if b["type"] == "image":
        return '<div class="image">🖼️ [image]</div>'
    return ""


def render_html(meta: dict, turns: list[dict], *, fold: bool) -> str:
    title = _esc(meta["title"])

    info = []
    if meta["session_id"]:
        info.append(f'Session <code>{_esc(meta["session_id"])}</code>')
    if meta["cwd"]:
        info.append(f'Dir <code>{_esc(meta["cwd"])}</code>')
    if meta["git_branch"]:
        info.append(f'Branch <code>{_esc(meta["git_branch"])}</code>')
    if meta["started"]:
        info.append(f'Started {_esc(_fmt_time(meta["started"]))}')
    models = _usage_models(meta)
    if models:
        info.append("Model " + ", ".join(f"<code>{_esc(m)}</code>" for m in models))
    meta_html = " &nbsp;·&nbsp; ".join(info)

    rows = _usage_rows(meta)
    if rows:
        cells = "".join(f"<th>{_esc(h)}</th>" for h, _ in _USAGE_COLUMNS)
        trs = [f'<thead><tr><th class="scope">Scope</th>{cells}</tr></thead><tbody>']
        for label, u in rows:
            row_cls = ' class="total"' if label == "Total" else ""
            tds = "".join(f"<td>{_esc(fn(u))}</td>" for _, fn in _USAGE_COLUMNS)
            trs.append(f'<tr{row_cls}><th class="scope">{_esc(label)}</th>{tds}</tr>')
        trs.append("</tbody>")
        note = ("approx; some models unpriced" if _usage_unpriced(meta) else "approx, list prices")
        meta_html += (f'<table class="usage-table">{"".join(trs)}</table>'
                      f'<div class="usage-note">💵 Cost is {note}.</div>')

    toc = ['<nav class="toc"><h2>Contents</h2><ol>']
    for t in turns:
        toc.append(
            f'<li class="toc-{t["kind"]}"><a href="#{t["anchor"]}">'
            f'<span class="tlabel">{_esc(t["label"])}</span> '
            f'<span class="tsnip">{_esc(t["snippet"])}</span></a></li>'
        )
    toc.append("</ol></nav>")

    body = ['<main>']
    body.append('<header class="session">')
    body.append(f"<h1>{title}</h1>")
    if meta_html:
        body.append(f'<div class="meta">{meta_html}</div>')
    body.append('<div class="controls">'
                '<button data-expand="all">Expand all</button>'
                '<button data-expand="none">Collapse all</button></div>')
    body.append("</header>")

    for t in turns:
        body.append(f'<section class="turn turn-{t["kind"]}" id="{t["anchor"]}">')
        ts = f'<span class="ts">{_esc(t["ts"])}</span>' if t["ts"] else ""
        body.append(f'<div class="turn-head"><span class="role">{_esc(t["label"])}</span>{ts}</div>')
        body.append('<div class="turn-body">')
        if t["kind"] == "meta" and fold:
            # Meta turns are injected skill/system content — collapse them by default.
            summary = _esc(f"📄 {t['snippet'] or 'Injected content'}")
            body.append(f'<details class="fold meta"><summary>{summary}</summary><div class="fold-body">')
            for b in t["blocks"]:
                body.append(_html_block(b, fold=fold))
            body.append("</div></details>")
        else:
            for b in t["blocks"]:
                body.append(_html_block(b, fold=fold))
        body.append("</div></section>")
    body.append("</main>")

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n"
        f"<style>{_HTML_STYLE}</style>\n"
        "</head>\n<body>\n"
        '<div class="layout">\n'
        + "\n".join(toc) + "\n"
        + "\n".join(body) + "\n"
        "</div>\n"
        f'<script src="{_MARKED_CDN}"></script>\n'
        f"<script>{_HTML_SCRIPT}</script>\n"
        "</body>\n</html>\n"
    )


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("session", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-o", "--output", default=None,
              help="Output path, or '-' for stdout. Default: <session>.<ext>")
@click.option("-f", "--format", "fmt", type=click.Choice(["md", "html"]), default="md", show_default=True,
              help="Output format.")
@click.option("--thinking/--no-thinking", default=True, show_default=True,
              help="Include assistant thinking blocks.")
@click.option("--tools/--no-tools", default=True, show_default=True,
              help="Include tool calls and results.")
@click.option("--fold/--no-fold", default=True, show_default=True,
              help="Collapse thinking/tool sections (vs. expanded by default).")
@click.option("--tool-output", type=int, default=1500, metavar="N", show_default=True,
              help="Truncate each tool result to N chars (0 = no limit).")
def main(session: Path, output, fmt: str, thinking: bool, tools: bool, fold: bool, tool_output: int) -> None:
    """Convert a Claude Code session log (SESSION .jsonl) into a Markdown or HTML chat."""
    meta, turns = parse_session(
        session, show_thinking=thinking, show_tools=tools, tool_limit=tool_output,
    )
    if fmt == "html":
        doc = render_html(meta, turns, fold=fold)
    else:
        doc = render_markdown(meta, turns, fold=fold)

    if output == "-":
        sys.stdout.write(doc)
        return

    out = Path(output) if output else session.with_suffix("." + fmt)
    out.write_text(doc, encoding="utf-8")
    click.echo(f"Wrote {out} ({len(doc):,} chars, {len(turns)} turns)")


if __name__ == "__main__":
    main()
