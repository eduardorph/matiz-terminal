import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from matiz import procinfo


class ClaudeArgv(unittest.TestCase):
    def r(self, argv):
        return procinfo.claude_resume_argv(argv, "SID")

    def test_keeps_model_and_drops_prompt(self):
        self.assertEqual(self.r(["claude", "--model", "opus", "conserta o bug"]),
                         ["claude", "--model", "opus", "--resume", "SID"])

    def test_replaces_continue_and_old_resume(self):
        self.assertEqual(self.r(["claude", "-c", "--model=opus"]), ["claude", "--model=opus", "--resume", "SID"])
        self.assertEqual(self.r(["claude", "--resume", "OLD", "--dangerously-skip-permissions"]),
                         ["claude", "--dangerously-skip-permissions", "--resume", "SID"])
        self.assertEqual(self.r(["claude", "-r"]), ["claude", "--resume", "SID"])
        self.assertEqual(self.r(["claude", "--session-id", "X", "--permission-mode", "plan"]),
                         ["claude", "--permission-mode", "plan", "--resume", "SID"])

    def test_node_install(self):
        self.assertTrue(procinfo.is_claude(["/usr/bin/node", "/x/@anthropic-ai/claude-code/cli.js"]))
        self.assertEqual(self.r(["/usr/bin/node", "/x/claude-code/cli.js", "--model", "opus"]),
                         ["claude", "--model", "opus", "--resume", "SID"])


class ResumeFor(unittest.TestCase):
    @staticmethod
    def _kill(p):
        # mata o grupo inteiro (bash + sleep filho) para não deixar órfãos
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()

    def spawn(self, *argv):
        p = subprocess.Popen(list(argv), cwd="/tmp", start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._kill, p)
        want = os.path.basename(argv[0])
        for _ in range(200):  # espera o exec terminar
            cmd = procinfo.read_cmdline(p.pid)
            if cmd and os.path.basename(cmd[0]) == want:
                break
            time.sleep(0.01)
        return p

    def test_unknown_command_is_only_suggested(self):
        p = self.spawn("sleep", "300")
        r = procinfo.resume_for(p.pid)
        self.assertEqual(r["mode"], "suggest")
        self.assertEqual(r["argv"], ["sleep", "300"])
        self.assertEqual(r["cwd"], "/tmp")

    def test_whitelisted_command_runs(self):
        p = self.spawn("sleep", "300")
        r = procinfo.resume_for(p.pid, rerun=[["sleep"]])
        self.assertEqual(r["mode"], "run")

    def test_never_rerun_wins(self):
        r = procinfo.resume_for(os.getpid(), rerun=[["python3"]])
        self.assertIsNotNone(r)
        procinfo.NEVER_RERUN.add(os.path.basename(procinfo.read_cmdline(os.getpid())[0]))
        try:
            r = procinfo.resume_for(os.getpid(), rerun=[[os.path.basename(procinfo.read_cmdline(os.getpid())[0])]])
            self.assertEqual(r["mode"], "suggest")
        finally:
            procinfo.NEVER_RERUN.discard(os.path.basename(procinfo.read_cmdline(os.getpid())[0]))

    def test_shell_is_ignored(self):
        p = self.spawn("bash", "-c", "sleep 300; true")
        self.assertIsNone(procinfo.resume_for(p.pid))

    def test_claude_session_file(self):
        with tempfile.TemporaryDirectory() as d:
            # um processo cujo argv[0] é "claude"
            p = subprocess.Popen(["bash", "-c", "exec -a claude sleep 300"], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(self._kill, p)
            for _ in range(200):
                if (procinfo.read_cmdline(p.pid) or [""])[0] == "claude":
                    break
                time.sleep(0.01)
            (Path(d) / f"{p.pid}.json").write_text(json.dumps(
                {"pid": p.pid, "sessionId": "abc-123", "cwd": "/srv/proj", "kind": "interactive"}))
            old = procinfo.CLAUDE_SESSIONS
            procinfo.CLAUDE_SESSIONS = Path(d)
            try:
                r = procinfo.resume_for(p.pid)
            finally:
                procinfo.CLAUDE_SESSIONS = old
            self.assertEqual(r["kind"], "claude")
            self.assertEqual(r["argv"][-2:], ["--resume", "abc-123"])
            self.assertEqual(r["cwd"], "/srv/proj")
            self.assertEqual(r["mode"], "run")


if __name__ == "__main__":
    unittest.main()
