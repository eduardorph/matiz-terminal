"""Claude plan limits (5h / weekly) through the official statusLine mechanism.

Claude Code sends the status line command a JSON payload with
`rate_limits.five_hour`, `seven_day`, etc. We install a wrapper that records
those numbers to a file and passes the input, unchanged, to the status line
the user already had (e.g. Orca's).

Nothing is installed unless the user asks (button in the widget). A backup is
kept and the change can be undone.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import session
from .i18n import N_

SETTINGS = Path(os.environ.get("MATIZ_CLAUDE_SETTINGS", Path.home() / ".claude" / "settings.json"))
LIMITS_FILE = session.STATE_DIR / "claude-limits.json"
ORIGINAL_FILE = session.CONFIG_DIR / "statusline-original.json"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = PROJECT_ROOT / "bin" / "matiz-statusline"
MARK = "matiz-statusline"

# Stored in English (claude-limits.json); translated only when displayed.
NAMES = {
    # Translators: plan limit window names (5-hour window, weekly window…)
    "five_hour": N_("5h"), "seven_day": N_("Weekly"), "seven_day_opus": N_("Weekly (Opus)"),
    "seven_day_sonnet": N_("Weekly (Sonnet)"), "seven_day_oauth_apps": N_("Weekly (apps)"),
}


def _read_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return {}


def is_enabled() -> bool:
    sl = _read_settings().get("statusLine") or {}
    return MARK in str(sl.get("command", ""))


def enable() -> None:
    settings = _read_settings()
    current = settings.get("statusLine")
    if current and MARK in str(current.get("command", "")):
        return
    if SETTINGS.exists():
        shutil.copy2(SETTINGS, SETTINGS.with_name(SETTINGS.name + ".matiz-bak"))
    session.atomic_write(ORIGINAL_FILE, json.dumps({"statusLine": current}, ensure_ascii=False, indent=2))
    new = dict(current) if isinstance(current, dict) else {"type": "command"}
    new["type"] = "command"
    # If Matiz is moved/deleted, fall back to the original status line.
    orig_cmd = (current or {}).get("command") if isinstance(current, dict) else None
    fallback = orig_cmd if orig_cmd and orig_cmd.strip() else "cat >/dev/null"
    new["command"] = (f"if [ -f '{WRAPPER}' ]; then exec '{sys.executable}' '{WRAPPER}'; "
                      f"else {fallback}\nfi")
    settings["statusLine"] = new
    session.atomic_write(SETTINGS, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")


def disable() -> None:
    settings = _read_settings()
    try:
        original = json.loads(ORIGINAL_FILE.read_text()).get("statusLine")
    except (OSError, ValueError):
        original = None
    if original:
        settings["statusLine"] = original
    else:
        settings.pop("statusLine", None)
    session.atomic_write(SETTINGS, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")


def original_command() -> str | None:
    try:
        sl = json.loads(ORIGINAL_FILE.read_text()).get("statusLine") or {}
    except (OSError, ValueError):
        return None
    cmd = sl.get("command")
    return cmd if isinstance(cmd, str) and cmd.strip() else None


_LEGACY_NAMES = {"5 h": "5h", "Semanal": "Weekly", "Semanal (Opus)": "Weekly (Opus)",
                 "Semanal (Sonnet)": "Weekly (Sonnet)", "Semanal (apps)": "Weekly (apps)"}


def read_limits() -> dict | None:
    """{"updated": epoch, "available": bool, "windows": [{"key","name","pct","resets_at"}]}"""
    try:
        data = json.loads(LIMITS_FILE.read_text())
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and "janelas" in data:  # legacy file written by older versions
        data = {
            "updated": data.get("atualizado"),
            "available": data.get("disponivel", True),
            "windows": [{"key": w.get("chave"), "name": NAMES.get(w.get("chave"), _LEGACY_NAMES.get(w.get("nome"), w.get("nome"))),
                         "pct": w.get("pct"), "resets_at": w.get("reinicia")} for w in data.get("janelas", [])],
        }
    return data


def _parse_reset(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) / (1000 if v > 1e12 else 1)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def extract(payload: dict) -> dict | None:
    rl = payload.get("rate_limits")
    avail = payload.get("rate_limits_available")
    if not isinstance(rl, dict):
        return {"updated": time.time(), "available": bool(avail), "windows": []} if avail is not None else None
    windows = []
    for key, w in rl.items():
        if not isinstance(w, dict):
            continue
        pct = w.get("used_percentage", w.get("percent", w.get("utilization")))
        if pct is None:
            continue
        windows.append({"key": key, "name": NAMES.get(key, key.replace("_", " ")),
                        "pct": float(pct), "resets_at": _parse_reset(w.get("resets_at"))})
    order = list(NAMES)
    windows.sort(key=lambda w: order.index(w["key"]) if w["key"] in order else 99)
    return {"updated": time.time(), "available": True if avail is None else bool(avail), "windows": windows}


def statusline_main() -> int:
    """Run by Claude Code on every status line update."""
    data = sys.stdin.buffer.read()
    try:
        info = extract(json.loads(data or b"{}"))
        if info is not None:
            session.atomic_write(LIMITS_FILE, json.dumps(info))
    except Exception:
        pass  # never break the status line
    orig = original_command()
    if not orig:
        return 0
    res = subprocess.run(["/bin/sh", "-c", orig], input=data, stdout=subprocess.PIPE)
    sys.stdout.buffer.write(res.stdout)
    return res.returncode


# ---------------- login (official commands) ----------------
_auth_cache: dict[str, tuple[float, dict | None]] = {}


def auth_status(tool: str) -> dict | None:
    """{"logged_in": bool, "plan": str|None}. Never returns e-mail or tokens."""
    hit = _auth_cache.get(tool)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    out = None
    try:
        if tool == "claude" and shutil.which("claude"):
            r = subprocess.run(["claude", "auth", "status", "--json"], capture_output=True, text=True, timeout=15)
            d = json.loads(r.stdout or "{}")
            plan = d.get("subscriptionType") or ("API" if d.get("authMethod") == "apiKey" else None)
            out = {"logged_in": bool(d.get("loggedIn")), "plan": plan}
        elif tool == "codex" and shutil.which("codex"):
            r = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=15)
            txt = (r.stdout + r.stderr).lower()
            out = {"logged_in": r.returncode == 0 and "not logged" not in txt,
                   "plan": "ChatGPT" if "chatgpt" in txt else ("API" if "api key" in txt else None)}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        out = None
    _auth_cache[tool] = (time.time(), out)
    return out


def forget_auth_cache():
    _auth_cache.clear()
