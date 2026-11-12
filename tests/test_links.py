import re
import unittest

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Vte", "3.91")
from matiz import pane  # noqa: E402
from matiz.app import _layout_ids  # noqa: E402


def find(pattern, text):
    return [m.group(0) for m in re.finditer(pattern, text)]


class Links(unittest.TestCase):
    def test_urls(self):
        self.assertEqual(find(pane.URL_RE, "veja https://exemplo.com/a?b=1."), ["https://exemplo.com/a?b=1"])
        self.assertEqual(find(pane.URL_RE, "(http://x.io/p)"), ["http://x.io/p"])

    def test_paths(self):
        t = "error in src/app.py:42:7 and ./README.md, also ~/notes.txt and /etc/hosts.conf:3"
        self.assertEqual(find(pane.PATH_RE, t),
                         ["src/app.py:42:7", "./README.md", "~/notes.txt", "/etc/hosts.conf:3"])

    def test_paths_without_extension(self):
        self.assertEqual(find(pane.PATH_RE, "see /etc/hostname:1 and ./Makefile or ~/bin/deploy"),
                         ["/etc/hostname:1", "./Makefile", "~/bin/deploy"])
        self.assertEqual(find(pane.PATH_RE, "and/or this"), [])

    def test_path_ignores_numbers(self):
        self.assertEqual(find(pane.PATH_RE, "version 1.5 cost 3.14"), [])

    def test_python_traceback(self):
        t = 'File "/home/u/x.py", line 12, in main'
        self.assertEqual(find(pane.PYTB_RE, t), ['File "/home/u/x.py", line 12'])
        self.assertEqual(pane.parse_link('File "/home/u/x.py", line 12', "pytb"), ("/home/u/x.py", 12, None))

    def test_parse_line_col(self):
        self.assertEqual(pane.parse_link("src/a.py:42:7", "path"), ("src/a.py", 42, 7))
        self.assertEqual(pane.parse_link("src/a.py:42", "path"), ("src/a.py", 42, None))
        self.assertEqual(pane.parse_link("src/a.py", "path"), ("src/a.py", None, None))


class Layout(unittest.TestCase):
    def test_ids(self):
        lay = {"split": "h", "a": {"pane": {"id": "p1"}},
               "b": {"split": "v", "a": {"pane": {"id": "p2"}}, "b": {"pane": {"id": "p3"}}}}
        self.assertEqual(_layout_ids(lay), ["p1", "p2", "p3"])
        self.assertEqual(_layout_ids({"pane": {"id": "x"}}), ["x"])
        self.assertEqual(_layout_ids(None), [])


if __name__ == "__main__":
    unittest.main()
