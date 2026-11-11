"""matiz-daemon: mantém as shells vivas quando a janela do Matiz fecha.

Cada aba é uma sessão com um PTY próprio. O daemon guarda os últimos
bytes de saída de cada sessão para reexibi-los quando a janela reconecta.
Sai sozinho quando não há mais sessões nem clientes.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import selectors
import signal
import socket
import struct
import sys
import termios
import time

from . import protocol as P

BUFFER_BYTES = int(os.environ.get("MATIZ_BUFFER_BYTES", 4 * 1024 * 1024))
IDLE_EXIT_S = 5.0          # sem sessões nem clientes por este tempo → sai
CLIENT_MAX_PENDING = 32 * 1024 * 1024


class _Stop(Exception):
    pass


def _raise_stop(*_):
    raise _Stop()


def set_winsize(fd: int, rows: int, cols: int):
    rows, cols = max(1, int(rows)), max(1, int(cols))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def set_nonblock(fd: int):
    fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_NONBLOCK)


class Session:
    def __init__(self, daemon, sid, cwd, argv, env, rows, cols):
        self.daemon = daemon
        self.id = sid
        self.buf = bytearray()
        self.truncated = False
        self.clients: set[Client] = set()
        self.wq = bytearray()  # entrada pendente para o PTY
        self.size = (rows, cols)
        pid, fd = pty.fork()
        if pid == 0:  # filho
            # Sinais ignorados são herdados pelo exec: restaura o padrão, senão
            # Ctrl+C (SIGINT), fechar a aba (SIGHUP) e pipes (SIGPIPE) não
            # funcionariam nos programas da aba.
            for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM,
                        signal.SIGPIPE, signal.SIGCHLD, signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU):
                signal.signal(sig, signal.SIG_DFL)
            signal.pthread_sigmask(signal.SIG_SETMASK, [])
            try:
                os.chdir(cwd if cwd and os.path.isdir(cwd) else os.path.expanduser("~"))
            except OSError:
                pass
            envd = dict(e.split("=", 1) for e in env if "=" in e) if env else dict(os.environ)
            envd.setdefault("TERM", "xterm-256color")
            try:
                os.execvpe(argv[0], argv, envd)
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd
        set_winsize(fd, rows, cols)
        set_nonblock(fd)
        daemon.sel.register(fd, selectors.EVENT_READ, ("pty", self))

    def on_output(self):
        try:
            data = os.read(self.fd, 65536)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EINTR):
                return
            data = b""
        if not data:
            self.finish()
            return
        self.buf += data
        if len(self.buf) > BUFFER_BYTES:
            del self.buf[: len(self.buf) - BUFFER_BYTES]
            self.truncated = True
        msg = P.frame(b"O", data)
        for c in list(self.clients):
            c.send(msg)

    def write(self, data: bytes):
        self.wq += data
        self.flush()

    def flush(self):
        while self.wq:
            try:
                n = os.write(self.fd, self.wq)
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EINTR):
                    break
                self.wq.clear()
                return
            del self.wq[:n]
        ev = selectors.EVENT_READ | (selectors.EVENT_WRITE if self.wq else 0)
        try:
            self.daemon.sel.modify(self.fd, ev, ("pty", self))
        except (KeyError, ValueError):
            pass

    def replay(self) -> bytes:
        data = bytes(self.buf)
        if self.truncated:
            # começa numa linha inteira para não cortar sequências de escape
            nl = data.find(b"\n")
            data = data[nl + 1 :] if nl >= 0 else b""
        return data

    def resize(self, rows, cols):
        self.size = (rows, cols)
        try:
            set_winsize(self.fd, rows, cols)
        except OSError:
            pass

    def nudge(self):
        """Força os programas de tela cheia (Claude, vim…) a redesenharem."""
        rows, cols = self.size
        try:
            set_winsize(self.fd, rows, max(1, cols - 1))
            set_winsize(self.fd, rows, cols)
        except OSError:
            pass
        try:
            pgrp = os.tcgetpgrp(self.fd)
            os.killpg(pgrp, signal.SIGWINCH)
        except OSError:
            pass

    def kill(self):
        # SIGHUP no grupo em primeiro plano (ex.: claude, npm) e na shell,
        # como um terminal faz ao fechar. SIGKILL depois, para quem ignorar.
        groups = {self.pid}
        try:
            groups.add(os.tcgetpgrp(self.fd))
        except OSError:
            pass
        for g in groups:
            for sig in (signal.SIGHUP, signal.SIGCONT):
                try:
                    os.killpg(g, sig)
                except OSError:
                    pass
        self.finish()
        self.daemon.pending_kill[self.pid] = (groups, time.monotonic() + 3)

    def finish(self):
        if self.id not in self.daemon.sessions:
            return
        del self.daemon.sessions[self.id]
        try:
            self.daemon.sel.unregister(self.fd)
        except (KeyError, ValueError):
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        status = None
        for _ in range(20):
            try:
                pid, st = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                break
            if pid:
                status = os.waitstatus_to_exitcode(st)
                break
            time.sleep(0.01)
        else:
            self.daemon.zombies.add(self.pid)
        msg = P.jframe(b"X", {"status": status})
        for c in list(self.clients):
            c.send(msg)
            c.session = None
        self.clients.clear()


class Client:
    def __init__(self, daemon, sock):
        self.daemon = daemon
        self.sock = sock
        self.dec = P.Decoder()
        self.out = bytearray()
        self.session: Session | None = None
        sock.setblocking(False)
        daemon.sel.register(sock, selectors.EVENT_READ, ("client", self))

    def send(self, data: bytes):
        self.out += data
        if len(self.out) > CLIENT_MAX_PENDING:
            self.close()
            return
        self.flush()

    def flush(self):
        while self.out:
            try:
                n = self.sock.send(self.out)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                self.close()
                return
            del self.out[:n]
        ev = selectors.EVENT_READ | (selectors.EVENT_WRITE if self.out else 0)
        try:
            self.daemon.sel.modify(self.sock, ev, ("client", self))
        except (KeyError, ValueError):
            pass

    def on_readable(self):
        try:
            data = self.sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:
            self.close()
            return
        try:
            msgs = self.dec.feed(data)
        except ValueError:
            self.close()
            return
        for kind, payload in msgs:
            self.handle(kind, payload)

    def handle(self, kind, payload):
        s = self.session
        if kind == b"H":
            req = json.loads(payload)
            if req.get("proto") != P.PROTO:
                self.send(P.jframe(b"E", {"error": "versão de protocolo diferente"}))
                return
            sid = req["id"]
            sess = self.daemon.sessions.get(sid)
            attached = sess is not None
            if not sess:
                argv = req.get("argv") or [os.environ.get("SHELL", "/bin/bash")]
                try:
                    sess = Session(self.daemon, sid, req.get("cwd"), argv, req.get("env"),
                                   req.get("rows", 24), req.get("cols", 80))
                except OSError as e:
                    self.send(P.jframe(b"E", {"error": str(e)}))
                    return
                self.daemon.sessions[sid] = sess
            self.session = sess
            sess.clients.add(self)
            self.send(P.jframe(b"S", {"id": sid, "pid": sess.pid, "attached": attached}))
            if attached:
                self.send(P.frame(b"O", sess.replay()))
                sess.resize(req.get("rows", 24), req.get("cols", 80))
                sess.nudge()
        elif s is None:
            return
        elif kind == b"I":
            s.write(payload)
        elif kind == b"R":
            r = json.loads(payload)
            s.resize(r["rows"], r["cols"])
        elif kind == b"K":
            s.kill()
        elif kind == b"C":
            s.buf.clear()
            s.truncated = False

    def close(self):
        if self.session:
            self.session.clients.discard(self)
            self.session = None
        try:
            self.daemon.sel.unregister(self.sock)
        except (KeyError, ValueError):
            pass
        self.sock.close()
        self.daemon.clients.discard(self)


class Daemon:
    def __init__(self, path):
        self.path = path
        self.sel = selectors.DefaultSelector()
        self.sessions: dict[str, Session] = {}
        self.clients: set[Client] = set()
        self.zombies: set[int] = set()
        self.pending_kill: dict[int, tuple[set[int], float]] = {}
        self.idle_since = time.monotonic()
        self.running = True

    def serve(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(self.path))
        os.chmod(self.path, 0o600)
        srv.listen(64)
        srv.setblocking(False)
        self.sel.register(srv, selectors.EVENT_READ, ("listen", srv))
        signal.signal(signal.SIGTERM, _raise_stop)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
        try:
          try:
            while self.running:
                for key, ev in self.sel.select(timeout=1.0):
                    kind, obj = key.data
                    if kind == "listen":
                        try:
                            conn, _ = srv.accept()
                        except OSError:
                            continue
                        self.clients.add(Client(self, conn))
                    elif kind == "client":
                        if ev & selectors.EVENT_WRITE:
                            obj.flush()
                        if ev & selectors.EVENT_READ and obj in self.clients:
                            obj.on_readable()
                    elif kind == "pty":
                        if ev & selectors.EVENT_WRITE:
                            obj.flush()
                        if ev & selectors.EVENT_READ:
                            obj.on_output()
                self.reap()
                if self.sessions or self.clients:
                    self.idle_since = time.monotonic()
                elif time.monotonic() - self.idle_since > IDLE_EXIT_S and not self.pending_kill:
                    break
          except _Stop:
            pass
        finally:
            try:
                if self.path.exists() and os.stat(self.path).st_ino == os.fstat(srv.fileno()).st_ino:
                    self.path.unlink()
            except OSError:
                pass
            # Encerramento do daemon (ex.: desligar o PC): fecha os PTYs sem
            # avisar "shell saiu" aos clientes — para a janela isso é uma
            # desconexão, e o estado salvo das abas é preservado.
            for c in list(self.clients):
                c.session = None
                c.close()
            for s in list(self.sessions.values()):
                s.clients.clear()
                s.kill()
            # dá até 1 s para saírem com SIGHUP; quem sobrar leva SIGKILL
            end = time.monotonic() + 1.0
            while self.pending_kill and time.monotonic() < end:
                self.reap()
                time.sleep(0.05)
            self.reap(force=True)

    def reap(self, force=False):
        now = time.monotonic()
        for pid, (groups, deadline) in list(self.pending_kill.items()):
            alive = []
            for g in groups:
                try:
                    os.killpg(g, 0)
                    alive.append(g)
                except OSError:
                    pass
            if not alive:
                del self.pending_kill[pid]
            elif force or now > deadline:
                for g in alive:
                    try:
                        os.killpg(g, signal.SIGKILL)
                    except OSError:
                        pass
                del self.pending_kill[pid]
        for pid in list(self.zombies):
            try:
                if os.waitpid(pid, os.WNOHANG)[0]:
                    self.zombies.discard(pid)
            except ChildProcessError:
                self.zombies.discard(pid)


def main():
    path = P.socket_path()
    # Já existe um daemon respondendo? Então não faz nada.
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(str(path))
        probe.close()
        return 0
    except OSError:
        probe.close()
    lock = open(path.parent / "daemon.lock", "w") if path.parent.is_dir() else None
    if lock is None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = open(path.parent / "daemon.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # outro daemon está subindo
    Daemon(path).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
