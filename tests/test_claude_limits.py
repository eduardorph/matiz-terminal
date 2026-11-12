import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SAMPLE = {
    "model": {"display_name": "Opus"},
    "rate_limits_available": True,
    "rate_limits": {
        "five_hour": {"used_percentage": 42.5, "resets_at": "2026-11-12T03:00:00Z"},
        "seven_day": {"used_percentage": 71, "resets_at": "2026-11-15T12:00:00Z"},
        "seven_day_opus": None,
    },
}


class ClaudeLimits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.settings = d / "settings.json"
        self.original = {"type": "command", "command": "cat >/dev/null; echo ORIGINAL-STATUSLINE", "padding": 1}
        self.settings.write_text(json.dumps({"model": "opus", "statusLine": self.original}))
        self.env = dict(os.environ, MATIZ_CLAUDE_SETTINGS=str(self.settings),
                        MATIZ_STATE_DIR=str(d / "state"), MATIZ_CONFIG_DIR=str(d / "config"),
                        PYTHONPATH=str(ROOT))

    def tearDown(self):
        self.tmp.cleanup()

    def py(self, code, stdin=None):
        return subprocess.run([sys.executable, "-c", code], env=self.env, input=stdin,
                              capture_output=True, text=True, check=True).stdout

    def test_cycle(self):
        self.py("from matiz import claude_limits as C; C.enable()")
        s = json.loads(self.settings.read_text())
        self.assertIn("matiz-statusline", s["statusLine"]["command"])
        self.assertEqual(s["statusLine"]["padding"], 1)     # keeps other options
        self.assertEqual(s["model"], "opus")                # leaves everything else alone
        self.assertTrue(self.settings.with_name("settings.json.matiz-bak").exists())

        # Claude Code runs the status line command with the JSON on stdin
        out = subprocess.run(s["statusLine"]["command"], shell=True, env=self.env,
                             input=json.dumps(SAMPLE), capture_output=True, text=True).stdout
        self.assertEqual(out.strip(), "ORIGINAL-STATUSLINE")     # the original status line still runs

        lim = json.loads(self.py("from matiz import claude_limits as C; import json; print(json.dumps(C.read_limits()))"))
        self.assertEqual([(w["name"], w["pct"]) for w in lim["windows"]], [("5h", 42.5), ("Weekly", 71.0)])
        self.assertIsNotNone(lim["windows"][0]["resets_at"])

        self.py("from matiz import claude_limits as C; C.disable()")
        s = json.loads(self.settings.read_text())
        self.assertEqual(s["statusLine"], self.original)

    def test_enable_is_idempotent(self):
        self.py("from matiz import claude_limits as C; C.enable(); C.enable()")
        self.py("from matiz import claude_limits as C; C.disable()")
        self.assertEqual(json.loads(self.settings.read_text())["statusLine"], self.original)

    def test_reads_legacy_portuguese_file(self):
        state = Path(self.env["MATIZ_STATE_DIR"])
        state.mkdir(parents=True, exist_ok=True)
        (state / "claude-limits.json").write_text(json.dumps({
            "atualizado": 1, "disponivel": True,
            "janelas": [{"chave": "seven_day", "nome": "Semanal", "pct": 5, "reinicia": None}]}))
        lim = json.loads(self.py("from matiz import claude_limits as C; import json; print(json.dumps(C.read_limits()))"))
        self.assertEqual(lim["windows"][0]["name"], "Weekly")
        self.assertEqual(lim["updated"], 1)

    def test_api_key_login_has_no_limits(self):
        sys.path.insert(0, str(ROOT))
        from matiz import claude_limits as C
        info = C.extract({"rate_limits_available": False, "rate_limits": None})
        self.assertFalse(info["available"])
        self.assertEqual(info["windows"], [])


if __name__ == "__main__":
    unittest.main()
