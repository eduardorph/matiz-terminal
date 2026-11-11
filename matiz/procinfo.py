"""Descobre o que está rodando em cada aba e como retomar depois.

Tudo aqui é leitura de /proc e de arquivos; nenhuma dependência de GTK,
para poder ser testado isoladamente.
"""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

CLAUDE_SESSIONS = Path(
    os.environ.get("MATIZ_CLAUDE_SESSIONS", Path.home() / ".claude" / "sessions")
)

# Programas que podem ser reexecutados automaticamente ao restaurar.
# Cada entrada é um prefixo de argv (comparado token a token).
DEFAULT_RERUN = [
    ["ssh"], ["mosh"],
    ["vim"], ["nvim"], ["vi"], ["nano"], ["micro"], ["hx"],
    ["htop"], ["btop"], ["top"], ["nvtop"],
    ["less"], ["man"],
    ["tail", "-f"], ["tail", "-F"], ["journalctl", "-f"],
    ["npm", "run", "dev"], ["npm", "start"], ["pnpm", "dev"], ["pnpm", "run", "dev"],
    ["yarn", "dev"], ["bun", "dev"], ["bun", "run", "dev"],
    ["docker", "compose", "logs"], ["lazygit"], ["lazydocker"],
]

# Nunca reexecutar sozinho, mesmo que o usuário adicione na config.
NEVER_RERUN = {"sudo", "su", "doas", "pkexec", "rm", "dd", "mkfs", "shutdown", "reboot"}

SHELLS = {"bash", "zsh", "fish", "sh", "dash", "nu", "elvish", "xonsh"}

# Flags do Claude Code que recebem valor e devem ser preservadas.
CLAUDE_VALUE_FLAGS = {
    "--model", "--permission-mode", "--add-dir", "--agent", "--agents",
    "--settings", "--setting-sources", "--mcp-config", "--allowedTools",
    "--allowed-tools", "--disallowedTools", "--disallowed-tools",
    "--append-system-prompt", "--system-prompt", "--fallback-model",
    "--plugin-dir", "--effort", "--output-style",
}
# Flags que escolhem/criam sessão: são descartadas e trocadas por --resume <id>.
CLAUDE_DROP_FLAGS = {"-c", "--continue", "--fork-session"}
CLAUDE_DROP_VALUE_FLAGS = {"--session-id", "--from-pr", "--teleport"}
CLAUDE_OPTIONAL_VALUE_FLAGS = {"-r", "--resume"}


def read_cmdline(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    parts = raw.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    return [p.decode(errors="replace") for p in parts] or None


def read_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def children(pid: int) -> list[int]:
    out: list[int] = []
    try:
        for tid in os.listdir(f"/proc/{pid}/task"):
            txt = Path(f"/proc/{pid}/task/{tid}/children").read_text()
            out += [int(x) for x in txt.split()]
    except OSError:
        pass
    return out


def foreground_pid(pty_fd: int, shell_pid: int) -> int | None:
    """PID do líder do grupo em primeiro plano, ou None se for a própria shell."""
    try:
        pgid = os.tcgetpgrp(pty_fd)
    except OSError:
        return None
    if pgid <= 0 or pgid == shell_pid:
        return None
    return pgid


def foreground_pid_of(shell_pid: int) -> int | None:
    """Mesmo que foreground_pid, mas sem precisar do PTY: lê o tpgid de
    /proc/<pid>/stat (grupo em primeiro plano do terminal da shell)."""
    try:
        stat = Path(f"/proc/{shell_pid}/stat").read_text()
        fields = stat[stat.rindex(")") + 2 :].split()
        tpgid = int(fields[5])
    except (OSError, ValueError, IndexError):
        return None
    if tpgid <= 0 or tpgid == shell_pid:
        return None
    return tpgid


def _basename(arg0: str) -> str:
    return os.path.basename(arg0)


def is_claude(argv: list[str]) -> bool:
    if not argv:
        return False
    if _basename(argv[0]) == "claude":
        return True
    # Instalação via npm: node .../@anthropic-ai/claude-code/cli.js
    return _basename(argv[0]).startswith("node") and any(
        "claude-code" in a or _basename(a) == "claude" for a in argv[1:2]
    )


def claude_session(pid: int) -> dict | None:
    """Lê ~/.claude/sessions/<pid>.json (ou de um filho direto)."""
    for p in [pid, *children(pid)]:
        f = CLAUDE_SESSIONS / f"{p}.json"
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if data.get("sessionId"):
            return data
    return None


def claude_resume_argv(argv: list[str], session_id: str) -> list[str]:
    """Reconstrói o comando do Claude mantendo as flags úteis e trocando a
    seleção de sessão por --resume <id>. Prompts posicionais são descartados
    para não reenviar mensagens."""
    out = [argv[0]] if _basename(argv[0]) == "claude" else ["claude"]
    args = argv[1:]
    if not _basename(argv[0]) == "claude":  # node cli.js ...
        args = argv[2:]
    i = 0
    while i < len(args):
        a = args[i]
        key = a.split("=", 1)[0]
        has_inline = "=" in a and a.startswith("--")
        if key in CLAUDE_DROP_FLAGS:
            i += 1
        elif key in CLAUDE_DROP_VALUE_FLAGS:
            i += 1 if has_inline else 2
        elif key in CLAUDE_OPTIONAL_VALUE_FLAGS:
            if not has_inline and i + 1 < len(args) and not args[i + 1].startswith("-"):
                i += 2
            else:
                i += 1
        elif key in CLAUDE_VALUE_FLAGS:
            if has_inline:
                out.append(a)
                i += 1
            else:
                out += args[i : i + 2]
                i += 2
        elif a.startswith("-"):
            out.append(a)
            i += 1
        else:
            i += 1  # posicional (prompt) — descarta
    return out + ["--resume", session_id]


def _matches(argv: list[str], prefix: list[str]) -> bool:
    if len(argv) < len(prefix):
        return False
    return _basename(argv[0]) == prefix[0] and argv[1 : len(prefix)] == prefix[1:]


def resume_for(pid: int, rerun: list[list[str]] | None = None) -> dict | None:
    """Decide como retomar o processo em primeiro plano `pid`.

    Retorna {"argv", "mode": "run"|"suggest", "kind", "label", "cwd"} ou None.
    """
    argv = read_cmdline(pid)
    if not argv:
        return None
    # Node (npm, pnpm…) reescreve o título do processo: "npm run dev" num só argumento.
    if len(argv) == 1 and " " in argv[0] and not os.path.exists(argv[0]):
        argv = argv[0].split()
    cwd = read_cwd(pid)
    name = _basename(argv[0])
    if name in SHELLS or name.lstrip("-") in SHELLS:
        return None

    if is_claude(argv):
        sess = claude_session(pid)
        if sess and sess.get("kind", "interactive") == "interactive" and "-p" not in argv and "--print" not in argv:
            return {
                "argv": claude_resume_argv(argv, sess["sessionId"]),
                "mode": "run",
                "kind": "claude",
                "label": "Claude Code",
                "cwd": sess.get("cwd") or cwd,
                "session_id": sess["sessionId"],
            }

    rules = DEFAULT_RERUN if rerun is None else rerun
    safe = name not in NEVER_RERUN and any(_matches(argv, r) for r in rules)
    return {
        "argv": argv,
        "mode": "run" if safe else "suggest",
        "kind": "generic",
        "label": name,
        "cwd": cwd,
    }


def command_line(argv: list[str]) -> str:
    return shlex.join(argv)
