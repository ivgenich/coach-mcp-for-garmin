"""Tests for scripts/ci-checks.py's feature checks: the hashed feature files in pin-drift, the licence
table (licenses), the audit's file list and the Grafana dashboard rule in text-scan.

Standard library only; no network, no pip-audit run (its command line is checked with a stub).
"""

import email.message
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
HASH = "--hash=sha256:" + "a" * 64


def load():
    spec = importlib.util.spec_from_file_location("ci_checks", os.path.join(ROOT, "scripts", "ci-checks.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def meta(expression=None, license=None, classifiers=()):
    m = email.message.Message()
    if expression:
        m["License-Expression"] = expression
    if license:
        m["License"] = license
    for c in classifiers:
        m["Classifier"] = c
    return m


class FeatureFiles(unittest.TestCase):
    def setUp(self):
        self.cc = load()
        self.tmp = tempfile.mkdtemp(prefix="ci-checks-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def check(self, files, pins, licenses=None):
        with mock.patch.object(self.cc, "FEATURE_REQUIREMENTS", files), \
                mock.patch.object(self.cc, "LICENSES", licenses if licenses is not None else
                                  {"alpha": "MIT", "beta": "Apache-2.0"}), \
                redirect_stdout(io.StringIO()):
            return self.cc.feature_pin_problems(pins)

    def test_the_repo_files_pass(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.cc.feature_pin_problems(self.cc.constraints()), [])

    def test_hashed_pins_reads_continued_lines(self):
        path = self.write("r.txt", f"# comment\nalpha==1.0 \\\n    {HASH} \\\n    {HASH}\nBeta_x==2.0b1 {HASH}\n")
        self.assertEqual(self.cc.hashed_pins(path), ({"alpha": "1.0", "beta-x": "2.0b1"}, []))

    def test_unhashed_or_loose_lines_fail(self):
        path = self.write("r.txt", f"alpha==1.0\nbeta>=2 {HASH}\ngamma==1 --hash=md5:abc\n")
        pins, problems = self.cc.hashed_pins(path)
        self.assertEqual(pins, {})
        self.assertEqual(len(problems), 4, problems)  # three bad lines, then "no pins"
        self.assertTrue(problems[-1].endswith(": no pins"))

    def test_a_version_that_differs_from_constraints_fails(self):
        path = self.write("t.txt", f"alpha==1.0 {HASH}\nbeta==2.0 {HASH}\n")
        problems = self.check([(path, False)], {"alpha": "1.1"})
        self.assertEqual(len(problems), 1)
        self.assertIn("alpha==1.0 but constraints.txt pins 1.1", problems[0])

    def test_a_whole_file_must_be_in_constraints(self):
        path = self.write("t.txt", f"alpha==1.0 {HASH}\nbeta==2.0 {HASH}\n")
        problems = self.check([(path, True)], {"alpha": "1.0"})
        self.assertEqual(len(problems), 1)
        self.assertIn("beta==2.0, which constraints.txt does not pin", problems[0])

    def test_two_files_must_agree(self):
        one = self.write("one.txt", f"alpha==1.0 {HASH}\nbeta==2.0 {HASH}\n")
        two = self.write("two.txt", f"alpha==1.1 {HASH}\n")
        problems = self.check([(one, False), (two, False)], {})
        self.assertEqual(len(problems), 1)
        self.assertIn("two.txt pins alpha==1.1 but", problems[0])

    def test_licence_table_must_match_the_pins(self):
        path = self.write("t.txt", f"alpha==1.0 {HASH}\ngamma==3 {HASH}\n")
        problems = self.check([(path, False)], {}, {"alpha": "MIT", "beta": "MIT"})
        self.assertEqual(len(problems), 2, problems)
        self.assertIn("gamma has no reviewed licence in LICENSES", problems[0])
        self.assertIn("LICENSES lists beta, which no feature requirements file pins", problems[1])


class Licenses(unittest.TestCase):
    def setUp(self):
        self.cc = load()

    def test_ids_and_families(self):
        self.assertEqual(self.cc.license_ids("MPL-2.0 AND (Apache-2.0 OR MIT)"), {"mpl-2.0", "apache-2.0", "mit"})
        self.assertEqual(self.cc.metadata_families(meta("LGPL-3.0-only")), {"lgpl"})
        self.assertEqual(self.cc.metadata_families(meta(license="Modified BSD License")), {"bsd"})
        self.assertEqual(self.cc.metadata_families(meta(classifiers=[
            "License :: OSI Approved :: Apache Software License", "Topic :: Permit"])), {"apache"})
        self.assertEqual(self.cc.metadata_families(meta(license="GNU General Public License v3")), {"gpl"})
        self.assertEqual(self.cc.metadata_families(meta()), set())

    def run_cmd(self, licenses, installed):
        import importlib.metadata as md

        def distribution(name):
            if name not in installed:
                raise md.PackageNotFoundError(name)
            return mock.Mock(metadata=installed[name])

        out = io.StringIO()
        with mock.patch.object(self.cc, "LICENSES", licenses), \
                mock.patch.object(md, "distribution", distribution), redirect_stdout(out):
            code = self.cc.cmd_licenses(None)
        return code, out.getvalue()

    def test_the_repo_table_passes(self):
        code, out = self.run_cmd(self.cc.LICENSES, {})
        self.assertEqual(code, 0, out)
        self.assertEqual(self.cc.LICENSES["protobuf"], "BSD-3-Clause")

    def test_a_licence_outside_the_allowed_set_fails(self):
        code, out = self.run_cmd({"alpha": "GPL-3.0-only"}, {})
        self.assertEqual(code, 1)
        self.assertIn("alpha: licence gpl-3.0-only is neither allowed nor a reviewed exception", out)

    def test_installed_metadata_that_disagrees_fails(self):
        code, out = self.run_cmd({"alpha": "MIT", "beta": "Apache-2.0"},
                                 {"alpha": meta("AGPL-3.0-only"), "beta": meta(license="Apache 2.0")})
        self.assertEqual(code, 1)
        self.assertIn("alpha: the installed metadata declares agpl", out)
        self.assertNotIn("beta:", out.replace("reviewed exception", ""))


class Audit(unittest.TestCase):
    @staticmethod
    def stub(seen, codes=None, vulns=None):
        def run(cmd, check=False):
            seen.append(cmd)
            i = len(seen) - 1
            dep = {"name": f"x{i}", "version": "1", "vulns": (vulns or {}).get(i, [])}
            with open(cmd[cmd.index("--output") + 1], "w", encoding="utf-8") as f:
                json.dump({"dependencies": [dep]}, f)
            return mock.Mock(returncode=(codes or {}).get(i, 0))
        return run

    def audit(self, cc, run):
        out = io.StringIO()
        with mock.patch.object(cc.subprocess, "run", run), mock.patch.object(cc, "AUDIT_IGNORES", ()), \
                redirect_stdout(out):
            code = cc.cmd_audit(None)
        return code, out.getvalue()

    def test_pip_audit_reads_constraints_and_both_feature_files_one_run_each(self):
        """Review fix: pip-audit refuses to mix the unhashed constraints.txt with the hash-pinned feature files
        in one run ("does not contain a hash"), so each file is a run of its own."""
        cc = load()
        seen = []
        code, _ = self.audit(cc, self.stub(seen))
        self.assertEqual(code, 0)
        files = [[c[i + 1] for i, a in enumerate(c) if a == "--requirement"] for c in seen]
        self.assertEqual([[os.path.relpath(f, ROOT) for f in run] for run in files],
                         [["constraints.txt"], ["config/requirements-telemetry.txt"],
                          ["config/requirements-history.txt"]])

    def test_any_failed_run_or_finding_fails_the_step(self):
        cc = load()
        code, out = self.audit(cc, self.stub([], codes={1: 1}))
        self.assertEqual(code, 1)
        self.assertIn("requirements-telemetry.txt", out)
        vuln = {"id": "PYSEC-0000-1", "aliases": [], "fix_versions": ["2"]}
        code, out = self.audit(cc, self.stub([], codes={2: 1}, vulns={2: [vuln]}))
        self.assertEqual(code, 1)
        self.assertIn("known vulnerability: x2 1: PYSEC-0000-1", out)


class Dashboards(unittest.TestCase):
    def setUp(self):
        self.cc = load()

    def test_the_repo_dashboard_passes(self):
        path = os.path.join(ROOT, "dashboards", "grafana", "coach-mcp.json")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(self.cc.dashboard_problems("dashboards/grafana/coach-mcp.json", f.read()), [])

    def test_stack_address_and_real_uids_fail(self):
        text = json.dumps({"uid": None, "panels": [
            {"datasource": {"uid": "${DS_PROMETHEUS}"}},
            {"datasource": {"uid": "grafanacloud-prom"}},
            {"links": [{"url": "https://example.grafana.net/d/x"}]},
            {"targets": [{"datasource": {"uid": 12}}]}]}, indent=1)
        problems = self.cc.dashboard_problems("dashboards/grafana/x.json", text)
        self.assertEqual(len(problems), 3, problems)
        self.assertTrue(any("grafana.net" in p for p in problems))
        self.assertTrue(any("'grafanacloud-prom'" in p for p in problems))
        self.assertTrue(any(": 12" in p for p in problems))

    def test_broken_json_fails(self):
        self.assertIn("not valid JSON", self.cc.dashboard_problems("dashboards/grafana/x.json", "{")[0])

    def test_text_scan_applies_the_rule_to_dashboards_only(self):
        files = {"dashboards/grafana/a.json": '{"uid": "abc"}', "docs/a.json": '{"uid": "abc"}',
                 "dashboards/grafana/sub/b.json": '{"uid": "abc"}'}
        tmp = tempfile.mkdtemp(prefix="ci-checks-scan-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))
        for rel, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(tmp, rel)), exist_ok=True)
            with open(os.path.join(tmp, rel), "w", encoding="utf-8") as f:
                f.write(text)
        out = io.StringIO()
        with mock.patch.object(self.cc, "ROOT", tmp), mock.patch.object(self.cc, "git_files", lambda: sorted(files)), \
                redirect_stdout(out):
            self.assertEqual(self.cc.cmd_text_scan(None), 1)
        self.assertEqual(out.getvalue().count("a uid that is not"), 1)
        self.assertIn("dashboards/grafana/a.json", out.getvalue())


if __name__ == "__main__":
    unittest.main()
