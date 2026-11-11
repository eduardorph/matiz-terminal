import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from matiz import protocol as P

ROOT = Path(__file__).resolve().parent.parent


def is_dead(pid):
    """Morto ou zumbi (já terminou, só falta o pai registrar)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    return stat[stat.rindex(")") + 2 :].split()[0] in ("Z", "X")


class DaemonTest(unittest.TestCase):
    def assertDead(self, pid, wait=2.0):
        end = time.time() + wait
        while time.time() < end and not is_dead(pid):
            time.sleep(0.05)
        self.assertTrue(is_dead(pid), f"processo {pid} ainda vivo")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, MATIZ_RUNTIME_DIR=self.tmp.name, PYTHONPATH=str(ROOT))
        self.proc = subprocess.Popen([sys.executable, "-m", "matiz.daemon"], env=self.env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.sock_path = Path(self.tmp.name) / "daemon.sock"
        for _ in range(100):
            if self.sock_path.exists():
                break
            time.sleep(0.02)

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.terminate()
        self.proc.wait(5)
        self.tmp.cleanup()

    def conn(self, sid, **kw):
        s = socket.socket(socket.AF_UNIX)
        s.connect(str(self.sock_path))
        s.sendall(P.jframe(b"H", {"proto": P.PROTO, "id": sid, "cwd": "/tmp",
                                  "argv": ["bash", "--norc", "--noprofile"],
                                  "env": ["PS1=$ ", "PATH=/usr/bin:/bin", "TERM=dumb"],
                                  "rows": 24, "cols": 80, **kw}))
        return s

    def read_until(self, s, pred, timeout=5):
        dec, out, msgs = P.Decoder(), bytearray(), []
        s.settimeout(0.2)
        end = time.time() + timeout
        while time.time() < end:
            try:
                data = s.recv(65536)
            except socket.timeout:
                continue
            if not data:
                break
            for k, p in dec.feed(data):
                msgs.append((k, p))
                if k == b"O":
                    out += p
            if pred(msgs, bytes(out)):
                return msgs, bytes(out)
        self.fail(f"tempo esgotado; recebido: {msgs!r}")

    def test_survives_disconnect_and_replays(self):
        s = self.conn("a1")
        msgs, _ = self.read_until(s, lambda m, o: any(k == b"S" for k, _ in m))
        info = json.loads(next(p for k, p in msgs if k == b"S"))
        self.assertFalse(info["attached"])
        s.sendall(P.frame(b"I", b"echo marca-$((40+2)); sleep 30\n"))
        self.read_until(s, lambda m, o: b"marca-42" in o)
        s.close()  # "fecha a janela"
        time.sleep(0.3)

        s2 = self.conn("a1")
        msgs, out = self.read_until(s2, lambda m, o: b"marca-42" in o)
        info2 = json.loads(next(p for k, p in msgs if k == b"S"))
        self.assertTrue(info2["attached"])
        self.assertEqual(info2["pid"], info["pid"])  # mesma shell, ainda viva
        os.kill(info["pid"], 0)

        s2.sendall(P.frame(b"K"))  # fecha a aba de verdade
        self.assertDead(info["pid"])
        s2.close()
        self.proc.wait(10)  # sem sessões e sem clientes → sai sozinho
        self.assertEqual(self.proc.returncode, 0)

    def test_ctrl_c_and_signals_reach_programs(self):
        s = self.conn("e1")
        self.read_until(s, lambda m, o: any(k == b"S" for k, _ in m))
        s.sendall(P.frame(b"I", b"grep -E '^Sig(Ign|Blk)' /proc/self/status\n"))
        _, out = self.read_until(s, lambda m, o: b"SigBlk" in o and b"SigIgn" in o.split(b"SigBlk")[-1] or o.count(b"SigIgn:") >= 1 and b"SigBlk:" in o)
        ign = int(out.split(b"SigIgn:")[1].split()[0], 16)
        for sig in (1, 2, 13):  # SIGHUP, SIGINT, SIGPIPE
            self.assertFalse(ign & (1 << (sig - 1)), f"sinal {sig} herdado como ignorado")
        job = self._fg_job(s, "matizjobe1")
        s.sendall(P.frame(b"I", b"\x03"))  # Ctrl+C
        self.assertDead(job)

    def test_clear_forgets_buffer(self):
        s = self.conn("f1")
        self.read_until(s, lambda m, o: any(k == b"S" for k, _ in m))
        s.sendall(P.frame(b"I", b"echo segredo-$((1+1))\n"))
        self.read_until(s, lambda m, o: b"segredo-2" in o)
        s.sendall(P.frame(b"C"))
        time.sleep(0.2)
        s.close()
        s2 = self.conn("f1")
        msgs, out = self.read_until(s2, lambda m, o: any(k == b"S" for k, _ in m))
        time.sleep(0.3)
        s2.settimeout(0.3)
        try:
            out += b"".join(p for k, p in P.Decoder().feed(s2.recv(65536)) if k == b"O")
        except socket.timeout:
            pass
        self.assertNotIn(b"segredo-2", out)

    def test_shell_exit_notifies(self):
        s = self.conn("b1")
        self.read_until(s, lambda m, o: any(k == b"S" for k, _ in m))
        s.sendall(P.frame(b"I", b"exit\n"))
        self.read_until(s, lambda m, o: any(k == b"X" for k, _ in m))

    def _fg_job(self, s, tag):
        s.sendall(P.frame(b"I", f"exec -a {tag} sleep 300 &\nfg\n".encode()))
        for _ in range(100):
            out = subprocess.run(["pgrep", "-fx", f"{tag} 300"], capture_output=True, text=True).stdout.split()
            if out:
                pid = int(out[0])
                stat = Path(f"/proc/{pid}/stat").read_text()
                f = stat[stat.rindex(")") + 2 :].split()
                if f[5] == f[2]:  # tpgid == pgrp → está em primeiro plano
                    return pid
            time.sleep(0.05)
        self.fail("job não iniciou")

    def test_close_tab_kills_foreground_job(self):
        s = self.conn("d1")
        self.read_until(s, lambda m, o: any(k == b"S" for k, _ in m))
        job = self._fg_job(s, "matizjobd1")
        s.sendall(P.frame(b"K"))
        self.assertDead(job)

    def test_terminate_does_not_send_exit(self):
        s = self.conn("c1")
        msgs, _ = self.read_until(s, lambda m, o: any(k == b"S" for k, _ in m))
        pid = json.loads(next(p for k, p in msgs if k == b"S"))["pid"]
        job = self._fg_job(s, "matizjobc1")
        self.proc.terminate()
        self.proc.wait(5)
        s.settimeout(1)
        rest = b""
        try:
            while chunk := s.recv(65536):
                rest += chunk
        except socket.timeout:
            pass
        kinds = [k for k, _ in P.Decoder().feed(rest)]
        self.assertNotIn(b"X", kinds)
        time.sleep(0.2)
        for p in (pid, job):
            self.assertDead(p)


if __name__ == "__main__":
    unittest.main()
