"""Collects usage of command-line AI tools from the data they themselves
write to disk. Read-only; nothing leaves the machine.

  Claude Code  ~/.claude/projects/**/*.jsonl   (tokens per message)
  OpenCode     ~/.local/share/opencode/opencode.db  (tokens + cost)
  Codex        ~/.codex/sessions/**/*.jsonl    (tokens + % of plan limit)
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .i18n import N_, _, fmt_decimal

HOME = Path.home()
CLAUDE_PROJECTS = Path(os.environ.get("MATIZ_CLAUDE_PROJECTS", HOME / ".claude" / "projects"))
CLAUDE_SESSIONS = Path(os.environ.get("MATIZ_CLAUDE_SESSIONS", HOME / ".claude" / "sessions"))
OPENCODE_DB = Path(os.environ.get("MATIZ_OPENCODE_DB", HOME / ".local/share/opencode/opencode.db"))
CODEX_SESSIONS = Path(os.environ.get("MATIZ_CODEX_SESSIONS", HOME / ".codex" / "sessions"))

WINDOW_7D = 7 * 24 * 3600


@dataclass
class Rec:
    ts: float          # epoch (s)
    model: str
    inp: int = 0
    out: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: float | None = None

    @property
    def total(self) -> int:
        return self.inp + self.out + self.cache_read + self.cache_write


@dataclass
class Agg:
    messages: int = 0
    inp: int = 0
    out: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: float | None = None

    @property
    def total(self) -> int:
        return self.inp + self.out + self.cache_read + self.cache_write

    def add(self, r: Rec):
        self.messages += 1
        self.inp += r.inp
        self.out += r.out
        self.cache_read += r.cache_read
        self.cache_write += r.cache_write
        if r.cost is not None:
            self.cost = (self.cost or 0.0) + r.cost


@dataclass
class ToolUsage:
    id: str
    name: str
    installed: bool
    has_data: bool = False
    windows: dict[str, Agg] = field(default_factory=dict)   # "5h", "today", "7d"
    models: list[tuple[str, int]] = field(default_factory=list)  # last 7 days
    hourly: list[int] = field(default_factory=lambda: [0] * 24)  # last 24 h (tokens)
    active: int = 0
    busy: int = 0
    limits: list[dict] = field(default_factory=list)  # [{"name","pct","resets_at"}]
    note: str | None = None
    last_used: float | None = None
    auth: dict | None = None            # {"logged_in", "plan"} via the official CLI
    limits_enabled: bool = False        # Claude: statusLine wrapper installed
    limits_updated: float | None = None


def _iso(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _procs_named(name: str) -> int:
    n = 0
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            argv0 = Path(f"/proc/{d}/cmdline").read_bytes().split(b"\0")[0]
        except OSError:
            continue
        if os.path.basename(argv0.decode(errors="replace")) == name:
            n += 1
    return n


class _JsonlTail:
    """Reads .jsonl files incrementally (only what was appended)."""

    def __init__(self):
        self.pos: dict[Path, int] = {}
        self.records: dict[Path, list[Rec]] = {}

    def scan(self, root: Path, since: float, parse_line) -> list[Rec]:
        seen = set()
        if root.is_dir():
            for f in root.rglob("*.jsonl"):
                try:
                    st = f.stat()
                except OSError:
                    continue
                if st.st_mtime < since:
                    continue
                seen.add(f)
                start = self.pos.get(f, 0)
                if st.st_size < start:  # file was rewritten
                    start, self.records[f] = 0, []
                if st.st_size == start:
                    continue
                recs = self.records.setdefault(f, [])
                ctx: dict = {}
                with open(f, "rb") as fh:
                    fh.seek(start)
                    data = fh.read()
                end = data.rfind(b"\n") + 1  # complete lines only
                for raw in data[:end].splitlines():
                    r = parse_line(raw, ctx, f)
                    if r is not None:
                        recs.append(r)
                self.pos[f] = start + end
        for f in list(self.records):
            if f not in seen:
                del self.records[f]
                self.pos.pop(f, None)
        out = []
        for recs in self.records.values():
            recs[:] = [r for r in recs if r.ts >= since]
            out += recs
        return out


# ---------------- Claude Code ----------------
class ClaudeSource:
    id, name = "claude", "Claude Code"

    def __init__(self):
        self.tail = _JsonlTail()
        self.seen_ids: set[str] = set()

    def _parse(self, raw: bytes, _ctx, _f):
        if b'"usage"' not in raw or b'"assistant"' not in raw:
            return None
        try:
            d = json.loads(raw)
        except ValueError:
            return None
        if d.get("type") != "assistant":
            return None
        m = d.get("message") or {}
        u = m.get("usage") or {}
        model = m.get("model") or "?"
        if not u or model == "<synthetic>":
            return None
        # Claude Code writes one line per content block with the same usage: dedupe
        key = f'{m.get("id")}:{d.get("requestId")}'
        if key in self.seen_ids:
            return None
        self.seen_ids.add(key)
        ts = _iso(d.get("timestamp", ""))
        if ts is None:
            return None
        return Rec(ts, model, int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                   int(u.get("cache_read_input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0))

    def collect(self, now: float) -> ToolUsage:
        t = ToolUsage(self.id, self.name, installed=bool(shutil.which("claude")) or CLAUDE_PROJECTS.is_dir())
        t.records = self.tail.scan(CLAUDE_PROJECTS, now - WINDOW_7D, self._parse)
        if len(self.seen_ids) > 500_000:
            self.seen_ids.clear()
        try:
            for f in CLAUDE_SESSIONS.glob("*.json"):
                try:
                    d = json.loads(f.read_text())
                except (OSError, ValueError):
                    continue
                if d.get("kind", "interactive") == "interactive" and d.get("pid") and _alive(int(d["pid"])):
                    t.active += 1
                    t.busy += d.get("status") == "busy"
        except OSError:
            pass
        from . import claude_limits as CL
        t.auth = CL.auth_status("claude")
        t.limits_enabled = CL.is_enabled()
        lim = CL.read_limits() if t.limits_enabled else None
        if lim:
            t.limits_updated = lim.get("updated")
            for w in lim.get("windows", []):
                t.limits.append({"name": w["name"], "pct": w["pct"], "resets_at": w.get("resets_at")})
            if not lim.get("available", True):
                t.note = _("Plan limits do not apply to this login (API key, Bedrock or Vertex).")
        return t


# ---------------- OpenCode ----------------
class OpenCodeSource:
    id, name = "opencode", "OpenCode"

    def collect(self, now: float) -> ToolUsage:
        t = ToolUsage(self.id, self.name, installed=bool(shutil.which("opencode")) or OPENCODE_DB.exists())
        t.records = []
        if OPENCODE_DB.exists():
            try:
                con = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True, timeout=2)
                rows = con.execute(
                    "select time_created, data from message where time_created >= ?",
                    (int((now - WINDOW_7D) * 1000),)).fetchall()
                last = con.execute("select max(time_created) from message").fetchone()[0]
                t.last_used = last / 1000 if last else None
                con.close()
            except sqlite3.Error as e:
                t.note = _("Could not read the database: {error}").format(error=e)
                rows = []
            for ms, data in rows:
                try:
                    d = json.loads(data)
                except ValueError:
                    continue
                tok = d.get("tokens")
                if d.get("role") != "assistant" or not tok:
                    continue
                cache = tok.get("cache") or {}
                cost = d.get("cost")
                t.records.append(Rec(
                    ms / 1000, d.get("modelID") or "?",
                    int(tok.get("input") or 0), int(tok.get("output") or 0) + int(tok.get("reasoning") or 0),
                    int(cache.get("read") or 0), int(cache.get("write") or 0),
                    float(cost) if isinstance(cost, (int, float)) else None))
        t.active = _procs_named("opencode")
        return t


# ---------------- Codex ----------------
class CodexSource:
    id, name = "codex", "Codex"

    def __init__(self):
        self.tail = _JsonlTail()
        self.last_limits: tuple[float, dict] | None = None

    def _parse(self, raw: bytes, ctx, _f):
        if b"token_count" not in raw and b"turn_context" not in raw:
            return None
        try:
            d = json.loads(raw)
        except ValueError:
            return None
        p = d.get("payload") or {}
        if d.get("type") == "turn_context" or p.get("type") == "turn_context":
            ctx["model"] = p.get("model") or ctx.get("model")
            return None
        if p.get("type") != "token_count":
            return None
        ts = _iso(d.get("timestamp", "")) or time.time()
        rl = p.get("rate_limits")
        if rl and (self.last_limits is None or ts >= self.last_limits[0]):
            self.last_limits = (ts, rl)
        u = (p.get("info") or {}).get("last_token_usage")
        if not u:
            return None
        cached = int(u.get("cached_input_tokens") or 0)
        return Rec(ts, ctx.get("model") or "?", int(u.get("input_tokens") or 0) - cached,
                   int(u.get("output_tokens") or 0) + int(u.get("reasoning_output_tokens") or 0), cached, 0)

    def collect(self, now: float) -> ToolUsage:
        t = ToolUsage(self.id, self.name, installed=bool(shutil.which("codex")))
        t.records = self.tail.scan(CODEX_SESSIONS, now - WINDOW_7D, self._parse)
        if self.last_limits:
            ts, rl = self.last_limits
            # Names are stored in English and translated when displayed (usage_ui).
            for key, name in (("primary", N_("Short window")), ("secondary", N_("Weekly"))):
                w = rl.get(key)
                if not isinstance(w, dict) or w.get("used_percent") is None:
                    continue
                mins = w.get("window_minutes")
                if mins:
                    name = f"{int(mins) // 60}h" if mins < 1440 else ("Weekly" if mins >= 10080 else f"{int(mins) // 1440}d")
                reset = w.get("resets_at")
                if reset is None and w.get("resets_in_seconds") is not None:
                    reset = ts + float(w["resets_in_seconds"])
                t.limits.append({"name": name, "pct": float(w["used_percent"]), "resets_at": reset})
        t.active = _procs_named("codex")
        if t.installed:
            from . import claude_limits as CL
            t.auth = CL.auth_status("codex")
        if self.last_limits:
            t.limits_updated = self.last_limits[0]
        return t


SOURCES = None


def collect(now: float | None = None) -> list[ToolUsage]:
    """Collects everything. Repeated calls are incremental (fast)."""
    global SOURCES
    if SOURCES is None:
        SOURCES = [ClaudeSource(), OpenCodeSource(), CodexSource()]
    now = now or time.time()
    midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    out = []
    for src in SOURCES:
        try:
            t = src.collect(now)
        except Exception as e:  # one broken source does not take down the others
            t = ToolUsage(src.id, src.name, installed=True, note=_("Read error: {error}").format(error=e))
            t.records = []
        recs = t.records
        del t.records
        t.has_data = bool(recs)
        if recs:
            t.last_used = max(r.ts for r in recs)
        t.windows = {"5h": Agg(), "today": Agg(), "7d": Agg()}
        models: dict[str, int] = {}
        for r in recs:
            t.windows["7d"].add(r)
            if r.ts >= midnight:
                t.windows["today"].add(r)
            if r.ts >= now - 5 * 3600:
                t.windows["5h"].add(r)
            models[r.model] = models.get(r.model, 0) + r.total
            age_h = int((now - r.ts) // 3600)
            if 0 <= age_h < 24:
                t.hourly[23 - age_h] += r.total
        t.models = sorted(models.items(), key=lambda kv: -kv[1])
        if t.installed or t.has_data:
            out.append(t)
    return out


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        # Translators: billions of tokens, e.g. "1.9B"; {n} is already formatted
        return _("{n}B").format(n=fmt_decimal(n / 1e9))
    if n >= 1_000_000:
        # Translators: millions of tokens, e.g. "3.4M"; {n} is already formatted
        return _("{n}M").format(n=fmt_decimal(n / 1e6))
    if n >= 1_000:
        # Translators: thousands of tokens, e.g. "1.2k"; {n} is already formatted
        return _("{n}k").format(n=fmt_decimal(n / 1e3))
    return str(n)
