"""Limites do plano do Claude (5 h / semana) pelo mecanismo oficial statusLine.

O Claude Code envia ao comando da barra de status um JSON com
`rate_limits.five_hour`, `seven_day` etc. Instalamos um intermediário que
anota esses números num arquivo e repassa a entrada, sem alteração, para a
barra de status que o usuário já tinha (ex.: a do Orca).

Nada é instalado sem o usuário pedir (botão no widget). Há backup e desfazer.
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

SETTINGS = Path(os.environ.get("MATIZ_CLAUDE_SETTINGS", Path.home() / ".claude" / "settings.json"))
LIMITS_FILE = session.STATE_DIR / "claude-limits.json"
ORIGINAL_FILE = session.CONFIG_DIR / "statusline-original.json"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = PROJECT_ROOT / "bin" / "matiz-statusline"
MARK = "matiz-statusline"

NAMES = {
    "five_hour": "5 h", "seven_day": "Semanal", "seven_day_opus": "Semanal (Opus)",
    "seven_day_sonnet": "Semanal (Sonnet)", "seven_day_oauth_apps": "Semanal (apps)",
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
    # Se o Matiz for movido/apagado, cai direto na barra de status original.
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


def read_limits() -> dict | None:
    """{"atualizado": epoch, "disponivel": bool, "janelas": [{"nome","pct","reinicia"}]}"""
    try:
        return json.loads(LIMITS_FILE.read_text())
    except (OSError, ValueError):
        return None


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
        return {"atualizado": time.time(), "disponivel": bool(avail), "janelas": []} if avail is not None else None
    janelas = []
    for key, w in rl.items():
        if not isinstance(w, dict):
            continue
        pct = w.get("used_percentage", w.get("percent", w.get("utilization")))
        if pct is None:
            continue
        janelas.append({"chave": key, "nome": NAMES.get(key, key.replace("_", " ")),
                        "pct": float(pct), "reinicia": _parse_reset(w.get("resets_at"))})
    order = list(NAMES)
    janelas.sort(key=lambda j: order.index(j["chave"]) if j["chave"] in order else 99)
    return {"atualizado": time.time(), "disponivel": True if avail is None else bool(avail), "janelas": janelas}


def statusline_main() -> int:
    """Executado pelo Claude Code a cada atualização da barra de status."""
    data = sys.stdin.buffer.read()
    try:
        info = extract(json.loads(data or b"{}"))
        if info is not None:
            session.atomic_write(LIMITS_FILE, json.dumps(info))
    except Exception:
        pass  # nunca atrapalha a barra de status
    orig = original_command()
    if not orig:
        return 0
    res = subprocess.run(["/bin/sh", "-c", orig], input=data, stdout=subprocess.PIPE)
    sys.stdout.buffer.write(res.stdout)
    return res.returncode


# ---------------- login (comandos oficiais) ----------------
_auth_cache: dict[str, tuple[float, dict | None]] = {}


def auth_status(tool: str) -> dict | None:
    """{"conectado": bool, "plano": str|None}. Nunca devolve e-mail ou tokens."""
    hit = _auth_cache.get(tool)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    out = None
    try:
        if tool == "claude" and shutil.which("claude"):
            r = subprocess.run(["claude", "auth", "status", "--json"], capture_output=True, text=True, timeout=15)
            d = json.loads(r.stdout or "{}")
            plano = d.get("subscriptionType") or ("API" if d.get("authMethod") == "apiKey" else None)
            out = {"conectado": bool(d.get("loggedIn")), "plano": plano}
        elif tool == "codex" and shutil.which("codex"):
            r = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=15)
            txt = (r.stdout + r.stderr).lower()
            out = {"conectado": r.returncode == 0 and "not logged" not in txt,
                   "plano": "ChatGPT" if "chatgpt" in txt else ("API" if "api key" in txt else None)}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        out = None
    _auth_cache[tool] = (time.time(), out)
    return out


def forget_auth_cache():
    _auth_cache.clear()
