"""Conexão com o matiz-daemon (inicia o daemon se não estiver rodando)."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import protocol as P

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)


def _try_connect() -> socket.socket | None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(P.socket_path()))
        return s
    except OSError:
        s.close()
        return None


def start_daemon():
    rd = P.runtime_dir()
    rd.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = dict(os.environ)
    env["PYTHONPATH"] = PROJECT_ROOT + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    log = open(rd / "daemon.log", "ab")
    # setsid -f: o daemon fica independente da janela (sobrevive ao fechá-la)
    subprocess.run(
        ["setsid", "-f", sys.executable, "-m", "matiz.daemon"],
        cwd="/", env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, check=False,
    )
    log.close()


def connect(timeout: float = 4.0) -> socket.socket:
    s = _try_connect()
    if s:
        return s
    start_daemon()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = _try_connect()
        if s:
            return s
        time.sleep(0.05)
    raise OSError("não foi possível conectar ao matiz-daemon")
