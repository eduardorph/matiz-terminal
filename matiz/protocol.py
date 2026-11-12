"""Protocol between Matiz and matiz-daemon.

Each message: 1 type byte + 4 length bytes (big-endian) + payload.

Client → daemon
  H  JSON  {"op": "open", "id", "cwd", "argv", "env", "rows", "cols", "proto"}
  I  bytes keyboard input
  R  JSON  {"rows", "cols"}
  K  —     end the session (tab/pane explicitly closed)
  C  —     forget the buffered output (screen cleared by the user)

Daemon → client
  S  JSON  {"id", "pid", "attached": bool}   session ready (reattached or created)
  O  bytes terminal output
  X  JSON  {"status"}                         the shell exited
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
    """Accumulates bytes and returns complete messages (type, payload)."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes):
        self.buf += data
        out = []
        while len(self.buf) >= HEADER.size:
            kind, n = HEADER.unpack_from(self.buf)
            if n > MAX_FRAME:
                raise ValueError("message too large")
            if len(self.buf) < HEADER.size + n:
                break
            payload = bytes(self.buf[HEADER.size : HEADER.size + n])
            del self.buf[: HEADER.size + n]
            out.append((kind, payload))
        return out
