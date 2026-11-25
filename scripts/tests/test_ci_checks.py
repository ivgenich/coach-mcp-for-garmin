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


class TestPins(unittest.TestCase):
    """pin-drift's rules for config/requirements-test.txt and verify-env's allowance for its packages."""

    def setUp(self):
        self.cc = load()
        self.tmp = tempfile.mkdtemp(prefix="ci-checks-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def pin_problems(self, test_text, pins, feature_text=None, licenses=None):
        test = self.write("test.txt", test_text)
        features = [(self.write("feature.txt", feature_text), False)] if feature_text else []
        with mock.patch.object(self.cc, "TEST_REQUIREMENTS", test), \
                mock.patch.object(self.cc, "FEATURE_REQUIREMENTS", features), \
                mock.patch.object(self.cc, "TEST_LICENSES", licenses if licenses is not None else
                                  {"cov": "Apache-2.0", "hyp": "MPL-2.0"}), \
                redirect_stdout(io.StringIO()):
            return self.cc.test_pin_problems(pins)

    def test_the_repo_file_passes(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.cc.test_pin_problems(self.cc.constraints()), [])

    def test_clean_test_pins_pass(self):
        self.assertEqual(self.pin_problems(f"cov==1.0 {HASH}\nhyp==2.0 {HASH}\n", {"alpha": "1.0"}), [])

    def test_a_package_constraints_pins_fails(self):
        problems = self.pin_problems(f"cov==1.0 {HASH}\nhyp==2.0 {HASH}\n", {"cov": "1.0"})
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("pins cov, which constraints.txt or a feature file pins too", problems[0])

    def test_a_package_a_feature_file_pins_fails(self):
        problems = self.pin_problems(f"cov==1.0 {HASH}\nhyp==2.0 {HASH}\n", {}, feature_text=f"hyp==2.0 {HASH}\n")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("pins hyp, which constraints.txt or a feature file pins too", problems[0])

    def test_an_unhashed_pin_fails(self):
        problems = self.pin_problems(f"cov==1.0\nhyp==2.0 {HASH}\n", {})
        self.assertTrue(problems)

    def test_licence_table_must_match_the_test_pins(self):
        problems = self.pin_problems(f"cov==1.0 {HASH}\ngamma==3 {HASH}\n", {})
        self.assertEqual(len(problems), 2, problems)
        self.assertIn("gamma has no reviewed licence in TEST_LICENSES", problems[0])
        self.assertIn("TEST_LICENSES lists hyp", problems[1])

    def verify_env(self, freeze_lines):
        test = self.write("test.txt", f"cov==1.0 {HASH}\n")
        ref = "r" * 40
        freeze = [f"garmin-mcp @ {self.cc.UPSTREAM_URL.format(ref=ref)}", "alpha==1.0"] + freeze_lines
        out = io.StringIO()
        with mock.patch.object(self.cc, "TEST_REQUIREMENTS", test), \
                mock.patch.object(self.cc, "constraints", lambda: {"alpha": "1.0"}), \
                mock.patch.object(self.cc, "read", lambda path, real=self.cc.read: real(path) if path == test else ""), \
                mock.patch.object(self.cc, "dockerfile_ref", lambda text: ref), \
                mock.patch.object(self.cc, "EXPECTED_CONFLICTS", []), \
                mock.patch.object(self.cc, "pip_output", lambda *a: freeze if a[0] == "freeze" else []), \
                redirect_stdout(out):
            code = self.cc.cmd_verify_env(mock.Mock(freeze=None, pip_check=None))
        return code, out.getvalue()

    def test_verify_env_accepts_a_test_package_at_its_pin(self):
        code, out = self.verify_env(["cov==1.0"])
        self.assertEqual(code, 0, out)
        self.assertIn("(1 test-only)", out)

    def test_verify_env_refuses_a_test_package_at_another_version(self):
        code, out = self.verify_env(["cov==1.1"])
        self.assertEqual(code, 1, out)
        self.assertIn("cov==1.1 is installed but not pinned by constraints.txt", out)


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

    def test_pip_audit_reads_constraints_the_feature_and_test_files_one_run_each(self):
        """pip-audit refuses to mix the unhashed constraints.txt with the hash-pinned feature and test
        files in one run ("does not contain a hash"), so each file is a run of its own."""
        cc = load()
        seen = []
        code, _ = self.audit(cc, self.stub(seen))
        self.assertEqual(code, 0)
        files = [[c[i + 1] for i, a in enumerate(c) if a == "--requirement"] for c in seen]
        self.assertEqual([[os.path.relpath(f, ROOT) for f in run] for run in files],
                         [["constraints.txt"], ["config/requirements-telemetry.txt"],
                          ["config/requirements-history.txt"], ["config/requirements-test.txt"]])

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


class InstallFull(unittest.TestCase):
    """install-full replays the Dockerfile's pip steps in order: hashed packages first, the index never."""

    REF = "0" * 40
    URL = f"https://github.com/Taxuspt/garmin_mcp/archive/{REF}.tar.gz"

    def setUp(self):
        self.cc = load()

    def dockerfile(self, run):
        return (f"ARG GARMIN_MCP_REF={self.REF}\nCOPY constraints.txt /tmp/constraints.txt\n"
                "COPY config/requirements-build.txt /tmp/requirements-build.txt\n"
                "COPY config/requirements-core.txt /tmp/requirements-core.txt\n" + run)

    def test_the_repo_dockerfile_installs_by_hash_before_the_offline_check(self):
        with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as f:
            steps = self.cc.dockerfile_installs(f.read())
        kinds = [(words[0], "--require-hashes" in words, env) for words, env in steps]
        first_c = next(i for i, (words, _env) in enumerate(steps) if "-c" in words)
        self.assertEqual(steps[first_c][1], {"PIP_NO_INDEX": "1"})
        self.assertTrue(kinds[0][1], steps[0])                       # the build backends, by hash
        self.assertIn("--no-build-isolation", steps[1][0])           # the upstream tarball
        core = next(words for words, _env in steps if any(w.endswith("requirements-core.txt") for w in words))
        self.assertIn("--no-build-isolation", core)                  # fitparse's sdist
        self.assertLess(steps.index((core, {})), first_c)
        self.assertEqual(steps[first_c - 1][0][:2], ["uninstall", "--yes"])

    def test_an_index_install_before_the_hashed_one_fails(self):
        run = ('RUN pip install --no-deps "garmin-mcp @ https://github.com/Taxuspt/garmin_mcp/archive/'
               '${GARMIN_MCP_REF}.tar.gz" \\\n    && export PIP_NO_INDEX=1 \\\n'
               '    && pip install -c /tmp/constraints.txt mcp==2.3.0 \\\n'
               '    && pip install --require-hashes --no-deps -r /tmp/requirements-core.txt\n')
        with self.assertRaisesRegex(ValueError, "hashed packages before"):
            self.cc.dockerfile_installs(self.dockerfile(run))

    def test_the_check_must_run_without_the_index(self):
        run = ('RUN pip install --require-hashes --no-deps -r /tmp/requirements-core.txt \\\n'
               '    && pip install --no-deps "garmin-mcp @ https://github.com/Taxuspt/garmin_mcp/archive/'
               '${GARMIN_MCP_REF}.tar.gz" \\\n    && pip install -c /tmp/constraints.txt mcp==2.3.0\n')
        with self.assertRaisesRegex(ValueError, "PIP_NO_INDEX=1"):
            self.cc.dockerfile_installs(self.dockerfile(run))

    def test_dry_run_prints_the_steps_in_order(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(self.cc.cmd_install_full(mock.Mock(dry_run=True)), 0)
        lines = [line for line in out.getvalue().splitlines() if "-m pip" in line]
        self.assertIn("requirements-build.txt", lines[0])
        self.assertIn("PIP_NO_INDEX=1 ", [line for line in lines if " -c " in line][0])

    def test_build_backends_must_not_overlap_constraints(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.cc.build_pin_problems(self.cc.constraints()), [])
            problems = self.cc.build_pin_problems({"setuptools": "1.0"})
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("setuptools, which constraints.txt pins too", problems[0])


if __name__ == "__main__":
    unittest.main()
