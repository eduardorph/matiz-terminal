"""Translations are complete, valid and compiled (requires gettext tools)."""
import filecmp
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PO_DIR = ROOT / "po"
HAVE_TOOLS = all(shutil.which(t) for t in ("xgettext", "msgfmt"))


def msgids(pot_text: str) -> set[str]:
    ids, cur, inside = set(), [], False
    for line in pot_text.splitlines():
        if line.startswith("msgid "):
            cur, inside = [line[6:]], True
        elif inside and line.startswith('"'):
            cur.append(line)
        else:
            if inside:
                ids.add("".join(s.strip('"') for s in cur))
            inside = False
    return {i for i in ids if i}


@unittest.skipUnless(HAVE_TOOLS, "gettext tools not installed")
class Translations(unittest.TestCase):
    def pos(self):
        return sorted(PO_DIR.glob("*.po"))

    def test_template_is_up_to_date(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "matiz.pot"
            subprocess.run(["xgettext", "--language=Python", "--from-code=UTF-8", "--keyword=_",
                            "--keyword=N_", "--keyword=ngettext:1,2", "-o", str(out),
                            *map(str, sorted((ROOT / "matiz").glob("*.py")))], check=True)
            fresh = msgids(out.read_text())
        committed = msgids((PO_DIR / "matiz.pot").read_text())
        self.assertEqual(fresh - committed, set(), "run tools/update-translations (new strings)")
        self.assertEqual(committed - fresh, set(), "run tools/update-translations (removed strings)")

    def test_translations_complete_and_valid(self):
        self.assertTrue(self.pos(), "no translations in po/")
        for po in self.pos():
            r = subprocess.run(["msgfmt", "--check", "--statistics", "-o", "/dev/null", str(po)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{po.name}: {r.stderr}")
            self.assertNotIn("untranslated", r.stderr, f"{po.name}: {r.stderr.strip()}")
            self.assertNotIn("fuzzy", r.stderr, f"{po.name}: {r.stderr.strip()}")

    def test_compiled_files_match(self):
        for po in self.pos():
            mo = ROOT / "locale" / po.stem / "LC_MESSAGES" / "matiz.mo"
            self.assertTrue(mo.exists(), f"missing {mo.relative_to(ROOT)}")
            with tempfile.TemporaryDirectory() as d:
                fresh = Path(d) / "matiz.mo"
                subprocess.run(["msgfmt", "-o", str(fresh), str(po)], check=True)
                self.assertTrue(filecmp.cmp(fresh, mo, shallow=False),
                                f"{mo.relative_to(ROOT)} is stale: run tools/update-translations")


class Runtime(unittest.TestCase):
    def tearDown(self):
        from matiz import i18n
        i18n.setup()

    def test_english_fallback_and_portuguese(self):
        from matiz import i18n
        i18n.setup("en")
        self.assertEqual(i18n._("Close tab"), "Close tab")
        if (ROOT / "locale" / "pt_BR" / "LC_MESSAGES" / "matiz.mo").exists():
            i18n.setup("pt_BR")
            self.assertNotEqual(i18n._("Close tab"), "Close tab")

    def test_unknown_language_falls_back_to_english(self):
        from matiz import i18n
        i18n.setup("xx_YY")
        self.assertEqual(i18n._("Close tab"), "Close tab")


if __name__ == "__main__":
    unittest.main()


class NoShadowedGettext(unittest.TestCase):
    """A function that binds `_` locally (e.g. `for a, _ in ...`) breaks every
    `_("...")` call in that function with UnboundLocalError."""

    def test_no_function_binds_underscore_and_calls_it(self):
        import ast
        scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                  ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)

        def own_nodes(scope):
            """Nodes of this scope only (does not enter nested scopes)."""
            stack = list(ast.iter_child_nodes(scope))
            while stack:
                n = stack.pop()
                yield n
                if not isinstance(n, scopes):
                    stack.extend(ast.iter_child_nodes(n))

        problems = []
        for path in sorted((ROOT / "matiz").glob("*.py")):
            for fn in ast.walk(ast.parse(path.read_text())):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    continue
                a = fn.args
                binds = any(x.arg == "_" for x in a.args + a.kwonlyargs + a.posonlyargs
                            + [v for v in (a.vararg, a.kwarg) if v])
                calls = False
                for n in own_nodes(fn):
                    if isinstance(n, ast.Name) and n.id == "_" and isinstance(n.ctx, ast.Store):
                        binds = True
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_":
                        calls = True
                if binds and calls:
                    problems.append(f"{path.name}:{fn.lineno}")
        self.assertEqual(problems, [], "functions that shadow _() and also call it")
