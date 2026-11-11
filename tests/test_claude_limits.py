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
        self.original = {"type": "command", "command": "cat >/dev/null; echo BARRA-ORIGINAL", "padding": 1}
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
        self.assertEqual(s["statusLine"]["padding"], 1)     # preserva outras opções
        self.assertEqual(s["model"], "opus")                # não mexe no resto
        self.assertTrue(self.settings.with_name("settings.json.matiz-bak").exists())

        # o Claude Code executa o comando da barra de status com o JSON na entrada
        out = subprocess.run(s["statusLine"]["command"], shell=True, env=self.env,
                             input=json.dumps(SAMPLE), capture_output=True, text=True).stdout
        self.assertEqual(out.strip(), "BARRA-ORIGINAL")     # a barra original continua

        lim = json.loads(self.py("from matiz import claude_limits as C; import json; print(json.dumps(C.read_limits()))"))
        self.assertEqual([(j["nome"], j["pct"]) for j in lim["janelas"]], [("5 h", 42.5), ("Semanal", 71.0)])
        self.assertIsNotNone(lim["janelas"][0]["reinicia"])

        self.py("from matiz import claude_limits as C; C.disable()")
        s = json.loads(self.settings.read_text())
        self.assertEqual(s["statusLine"], self.original)

    def test_enable_is_idempotent(self):
        self.py("from matiz import claude_limits as C; C.enable(); C.enable()")
        self.py("from matiz import claude_limits as C; C.disable()")
        self.assertEqual(json.loads(self.settings.read_text())["statusLine"], self.original)

    def test_api_key_login_has_no_limits(self):
        sys.path.insert(0, str(ROOT))
        from matiz import claude_limits as C
        info = C.extract({"rate_limits_available": False, "rate_limits": None})
        self.assertFalse(info["disponivel"])
        self.assertEqual(info["janelas"], [])


if __name__ == "__main__":
    unittest.main()
