"""State persistence (tabs, colors, commands, screen history) and configuration."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

STATE_DIR = Path(
    os.environ.get(
        "MATIZ_STATE_DIR",
        Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "matiz",
    )
)
CONFIG_DIR = Path(
    os.environ.get(
        "MATIZ_CONFIG_DIR",
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "matiz",
    )
)
STATE_FILE = STATE_DIR / "session.json"
SCROLLBACK_DIR = STATE_DIR / "scrollback"


def atomic_write(path: Path, data: str) -> None:
    """Atomic write (tmp + fsync + rename): survives a power loss."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text())
        if isinstance(data, dict) and isinstance(data.get("tabs"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "tabs": []}


def dump_state(state: dict) -> str:
    return json.dumps(state, ensure_ascii=False, indent=2)


def save_state_text(text: str) -> None:
    atomic_write(STATE_FILE, text)


def scrollback_path(tab_id: str) -> Path:
    return SCROLLBACK_DIR / f"{tab_id}.txt"


def save_scrollback(tab_id: str, text: str) -> None:
    atomic_write(scrollback_path(tab_id), text)


def load_scrollback(tab_id: str) -> str | None:
    try:
        return scrollback_path(tab_id).read_text(encoding="utf-8")
    except OSError:
        return None


def prune_scrollback(keep_ids: set[str]) -> None:
    try:
        for f in SCROLLBACK_DIR.glob("*.txt"):
            if f.stem not in keep_ids:
                f.unlink(missing_ok=True)
    except OSError:
        pass


# Legacy (Portuguese) config keys still accepted, mapped to the English ones.
LEGACY_KEYS = {
    "reexecutar_extra": "rerun_extra",
    "linhas_historico_salvas": "saved_scrollback_lines",
    "linhas_rolagem": "scrollback_lines",
    "fonte": "font",
    "notificar_apos_segundos": "notify_after_seconds",
    "notificacoes": "notifications",
    "persistir_processos": "persist_processes",
    "widget_uso": "usage_widget",
}


def load_config() -> dict:
    """~/.config/matiz/config.json (optional).

    {
      "rerun_extra": [["python", "manage.py", "runserver"]],
      "saved_scrollback_lines": 3000,
      "font": "Monospace 11"
    }

    Legacy Portuguese keys are mapped to their English names when the English
    key is absent.
    """
    try:
        data = json.loads((CONFIG_DIR / "config.json").read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    for old, new in LEGACY_KEYS.items():
        if old in data:
            value = data.pop(old)
            data.setdefault(new, value)
    return data
