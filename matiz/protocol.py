"""Protocolo entre o Matiz e o matiz-daemon.

Cada mensagem: 1 byte de tipo + 4 bytes de tamanho (big-endian) + conteúdo.

Cliente → daemon
  H  JSON  {"op": "open", "id", "cwd", "argv", "env", "rows", "cols", "proto"}
  I  bytes entrada do teclado
  R  JSON  {"rows", "cols"}
  K  —     encerrar a sessão (aba fechada explicitamente)
  C  —     esquecer a saída guardada (tela limpa pelo usuário)

Daemon → cliente
  S  JSON  {"id", "pid", "attached": bool}   sessão pronta (reconectada ou criada)
  O  bytes saída do terminal
  X  JSON  {"status"}                         a shell terminou
  E  JSON  {"error"}
"""
from __future__ import annotations

import json
import os
import struct
from pathlib import Path

PROTO = 1
HEADER = struct.Struct(">cI")
MAX_FRAME = 64 * 1024 * 1024


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/matiz-{os.getuid()}"
    return Path(os.environ.get("MATIZ_RUNTIME_DIR", Path(base) / "matiz"))


def socket_path() -> Path:
    return runtime_dir() / "daemon.sock"


def frame(kind: bytes, payload: bytes = b"") -> bytes:
    return HEADER.pack(kind, len(payload)) + payload


def jframe(kind: bytes, obj) -> bytes:
    return frame(kind, json.dumps(obj).encode())


class Decoder:
    """Acumula bytes e devolve mensagens completas (tipo, conteúdo)."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes):
        self.buf += data
        out = []
        while len(self.buf) >= HEADER.size:
            kind, n = HEADER.unpack_from(self.buf)
            if n > MAX_FRAME:
                raise ValueError("mensagem grande demais")
            if len(self.buf) < HEADER.size + n:
                break
            payload = bytes(self.buf[HEADER.size : HEADER.size + n])
            del self.buf[: HEADER.size + n]
            out.append((kind, payload))
        return out
