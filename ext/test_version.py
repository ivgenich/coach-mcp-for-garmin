"""Tests for version.py: this project's version, one line (KEY contract D17, test K14).

    python -m unittest test_version -v      (from ext/)

Standard library only. DOCS's and LIVE's checks read the file with the regex below, without MULTILINE, and CI
reads it with ast; serverInfo.version comes from it (test_launch.py checks what the server serves).
"""

from __future__ import annotations

import ast
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import version  # noqa: E402

VERSION_FILE = os.path.join(HERE, "version.py")


def read():
    with open(VERSION_FILE, encoding="utf-8", newline="") as f:
        return f.read()


class VersionFile(unittest.TestCase):
    def test_exactly_one_line(self):
        self.assertRegex(read(), r'\AVERSION = "\d+\.\d+\.\d+"\n\Z')

    def test_the_readers_regex_finds_it(self):
        # As DOCS's T-VER and LIVE's release check read it: no MULTILINE.
        match = re.search(r'^VERSION = "(\d+\.\d+\.\d+)"$', read())
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), version.VERSION)

    def test_ast_reads_the_same_value(self):
        (node,) = ast.parse(read()).body
        self.assertIsInstance(node, ast.Assign)
        self.assertEqual([t.id for t in node.targets], ["VERSION"])
        self.assertEqual(ast.literal_eval(node.value), version.VERSION)

    def test_first_release(self):
        self.assertEqual(version.VERSION, "1.0.0")


if __name__ == "__main__":
    unittest.main()
