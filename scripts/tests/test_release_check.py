"""Tests for scripts/release-check.py on temp git repositories.

A fake gitleaks is put first on PATH unless a test needs the real one. git
runs with the user's and the system's git config ignored. Fake data only.
"""

import os
import re
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import livefakes  # noqa: E402

ALL_IDS = ["R1", "R2", "R3", "R4", "R6", "R7", "R8", "R9", "R10"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = livefakes.temp_dir(self)
        self.bin = os.path.join(self.tmp, "bin")
        livefakes.write_fake_gitleaks(self.bin, 0)
        self.repo = livefakes.good_repo(os.path.join(self.tmp, "repo"))
        self.deny = os.path.join(self.tmp, "denylist.txt")
        self.write_deny(["Jane Example", "jane@example.org"])

    def write_deny(self, entries):
        with open(self.deny, "w") as f:
            f.write("\n".join(entries) + "\n\n")

    def env(self, path=None):
        env = livefakes.git_env()
        env["PATH"] = (path if path is not None else self.bin) + os.pathsep + os.environ.get("PATH", "")
        return env

    def rc(self, *extra, deny=True, env=None, version="1.0.0"):
        args = ["--version", version, "--repo", self.repo.path] + (["--denylist", self.deny] if deny else [])
        before = self.repo.state()
        code, out = livefakes.run_script(livefakes.RELEASE_CHECK, args + list(extra), env=env or self.env(),
                                         timeout=120)
        self.assertEqual(self.repo.state(), before, "release-check changed the repository")
        return code, out

    def status(self, out, rid):
        m = re.search(rf"^(PASS|FAIL|SKIP|INFO) {rid} ", out, re.M)
        return m.group(1) if m else None

    def assertOnlyFails(self, out, *ids):
        for rid in ALL_IDS:
            want = "FAIL" if rid in ids else "PASS"
            self.assertEqual(self.status(out, rid), want, f"{rid}\n{out}")
        self.assertIn("RESULT: FAIL", out)


class GoodRepoTest(Base):
    def test_pass_then_tag(self):
        code, out = self.rc()
        self.assertEqual(code, 0, out)
        for rid in ALL_IDS:
            self.assertEqual(self.status(out, rid), "PASS", out)
        self.assertIn('PASS R1 version: ext/version.py VERSION is 1.0.0', out)
        self.assertIn(f'PASS R2 changelog: "## [Unreleased]" and "## [1.0.0] - {self.repo.head_utc_day()}" '
                      "(the UTC date of HEAD's commit)", out)
        self.assertRegex(out, r"PASS R3 git: on branch main, working tree clean, HEAD [0-9a-f]{7}\n")
        self.assertIn("PASS R4 tag: v1.0.0 does not exist yet", out)
        self.assertIn("INFO R5 remotes: 0 (this script never pushes)", out)
        self.assertIn("PASS R6 identity: repo-local user.email is a GitHub noreply address; every author and "
                      "committer email in 2 commits is one too; every date offset is +0000", out)
        self.assertIn("PASS R7 tracked files: .gitignore, LICENSE, README.md, CHANGELOG.md, SECURITY.md present; "
                      "nothing of the forbidden kinds", out)
        self.assertIn("PASS R8 secrets: gitleaks 8.30.1 found nothing in the HEAD tree or the history", out)
        self.assertIn("PASS R9 personal details: 2 denylist entries, 0 hits in the HEAD tree, commit metadata and "
                      "history", out)
        self.assertIn("PASS R10 modes: 2 scripts are executable in the index", out)
        self.assertTrue(out.rstrip().endswith("RESULT: PASS (ready to tag v1.0.0)"), out)
        code, out = self.rc("--expect-tag")
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL R4 tag: v1.0.0 does not exist", out)
        self.repo.git("tag", "-a", "v1.0.0", "-m", "coach-mcp-for-garmin 1.0.0")
        code, out = self.rc()
        self.assertIn("FAIL R4 tag: v1.0.0 already exists", out)
        code, out = self.rc("--expect-tag")
        self.assertEqual(code, 0, out)
        self.assertIn("PASS R4 tag: v1.0.0 is annotated and points at HEAD", out)
        self.assertIn("RESULT: PASS (v1.0.0 is annotated on HEAD; ready to push the tag)", out)

    def test_lightweight_tag(self):
        self.repo.git("tag", "v1.0.0")
        code, out = self.rc("--expect-tag")
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL R4 tag: v1.0.0 is a lightweight tag", out)

    def test_tag_on_other_commit(self):
        self.repo.git("tag", "-a", "v1.0.0", "-m", "x", "HEAD~1")
        code, out = self.rc("--expect-tag")
        self.assertIn("FAIL R4 tag: v1.0.0 does not point at HEAD", out)

    def test_no_denylist_not_ready(self):
        code, out = self.rc(deny=False)
        self.assertEqual(code, 1, out)
        self.assertIn("SKIP R9 personal details: no --denylist given (run it before tagging)", out)
        self.assertIn("RESULT: NOT READY", out)

    def test_bad_version_arg(self):
        code, out = self.rc(version="1.0")
        self.assertEqual(code, 2, out)


class FailureTest(Base):
    def test_version_mismatch(self):
        code, out = self.rc(version="1.0.1")
        self.assertIn("FAIL R1 version: ext/version.py VERSION is 1.0.0, not 1.0.1", out)
        self.assertEqual(self.status(out, "R2"), "FAIL")

    def test_version_file_forms(self):
        cases = [('"""Version."""\nVERSION = "1.0.0"\n', "PASS"),
                 ('raise SystemExit("imported")\nVERSION = "1.0.0"\n', "PASS"),
                 ('VERSION = "1.0.0"\nVERSION = "1.0.0"\n', "FAIL"),
                 ("VERSION = 100\n", "FAIL"),
                 ('VERSION = "1.0"\n', "FAIL"),
                 ("X = 1\n", "FAIL")]
        for text, want in cases:
            with self.subTest(text=text):
                self.repo.write("ext/version.py", text)
                self.repo.commit("v")
                self.repo.sync_changelog()
                code, out = self.rc()
                self.assertEqual(self.status(out, "R1"), want, out)
                self.assertNotIn("imported", out)

    def test_changelog(self):
        day = self.repo.head_utc_day()
        cases = [(f"# Changelog\n\n## [1.0.0] - {day}\n", 'no line "## [Unreleased]"'),
                 ("## [Unreleased]\n\n## [1.0.0] - 2000-01-02\n", "has the date 2000-01-02, not"),
                 (f"## [Unreleased]\n\n## [1.0.0] - {day} [YANKED]\n", "[YANKED]"),
                 (f"## [Unreleased]\n\n## [0.9.0] - {day}\n## [1.0.0] - {day}\n", "is not"),
                 ("## [Unreleased]\n", "no release heading")]
        for text, needle in cases:
            with self.subTest(needle=needle):
                self.repo.write("CHANGELOG.md", text)
                self.repo.commit("c")
                text = text.replace(day, self.repo.head_utc_day())
                self.repo.write("CHANGELOG.md", text)
                self.repo.git("commit", "-q", "-a", "--amend", "--no-edit", "--allow-empty")
                code, out = self.rc()
                self.assertEqual(self.status(out, "R2"), "FAIL", out)
                self.assertIn(needle, out)

    def test_dirty_tree_and_branch(self):
        self.repo.write("new.txt", "x\n")
        code, out = self.rc()
        self.assertIn("FAIL R3 git: 1 uncommitted or untracked paths", out)
        self.repo.remove("new.txt")
        self.repo.git("checkout", "-q", "-b", "other")
        code, out = self.rc()
        self.assertIn("FAIL R3 git: not on branch main", out)

    def test_non_noreply_email(self):
        self.repo.write("a.txt", "a\n")
        self.repo.commit("a", env={"GIT_AUTHOR_EMAIL": "person@example.net"})
        code, out = self.rc()
        self.assertIn("FAIL R6 identity: 1 of 3 commits carry an email that is not a GitHub noreply address", out)
        self.assertNotIn("person@example.net", out)
        self.repo.git("config", "--local", "user.email", "person@example.net")
        code, out = self.rc()
        self.assertIn("repo-local user.email is not a GitHub noreply address", out)
        self.assertNotIn("person@example.net", out)

    def test_forbidden_files(self):
        for rel in (".env", "x.sqlite", "__pycache__/a.pyc", "tunnel.env", "prod.env", "garmin_tokens.base64",
                    "garmin-key/token.json", ".env.local", "deep/tokens/x.json", "a.token", ".DS_Store"):
            with self.subTest(rel=rel):
                repo = livefakes.good_repo(os.path.join(self.tmp, "r-" + str(abs(hash(rel)))))
                repo.write(rel, "fake\n")
                repo.git("add", "-f", rel)
                repo.git("commit", "-q", "-m", "add")
                repo.sync_changelog()
                self.repo = repo
                code, out = self.rc()
                self.assertEqual(self.status(out, "R7"), "FAIL", out)
                self.assertIn(rel, out)

    def test_env_example_and_missing_required(self):
        code, out = self.rc()
        self.assertEqual(self.status(out, "R7"), "PASS")  # .env.example is tracked in good_repo
        self.repo.git("rm", "-q", "SECURITY.md")
        self.repo.commit("rm")
        self.repo.sync_changelog()
        code, out = self.rc()
        self.assertIn("FAIL R7 tracked files: missing SECURITY.md", out)

    def test_script_mode(self):
        self.repo.write("scripts/new.sh", "#!/bin/sh\n", 0o644)
        self.repo.commit("s")
        self.repo.sync_changelog()
        code, out = self.rc()
        self.assertIn("FAIL R10 modes: 1 scripts are not executable in the index: scripts/new.sh", out)
        self.assertOnlyFails(out, "R10")


class DateTest(Base):
    def test_utc_day_not_local_day(self):
        when = "2026-11-18T05:00:00+14:00"
        self.repo.write("CHANGELOG.md", "## [Unreleased]\n\n## [1.0.0] - 2026-11-17\n")
        self.repo.commit("late", env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})
        code, out = self.rc()
        self.assertEqual(self.status(out, "R2"), "PASS", out)
        self.assertIn("FAIL R6 identity: 1 of 3 commits carry a local time zone offset; commit with TZ=UTC", out)
        self.assertNotIn("+1400", out)
        self.repo.write("CHANGELOG.md", "## [Unreleased]\n\n## [1.0.0] - 2026-11-18\n")
        self.repo.git("commit", "-q", "-a", "--amend", "--no-edit",
                      env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})
        code, out = self.rc()
        self.assertEqual(self.status(out, "R2"), "FAIL", out)

    def test_local_zone_commit_and_tag(self):
        self.repo.write("a.txt", "a\n")
        self.repo.commit("tokyo", env={"TZ": "Asia/Tokyo"})
        self.repo.sync_changelog()
        code, out = self.rc()
        self.assertIn("FAIL R6 identity: 1 of 4 commits carry a local time zone offset; commit with TZ=UTC", out)
        self.assertNotIn("+0900", out)

    def test_tag_zone_and_tagger(self):
        self.repo.git("tag", "-a", "v1.0.0", "-m", "x", env={"TZ": "Asia/Tokyo"})
        code, out = self.rc("--expect-tag")
        self.assertIn("the tag carries a local time zone offset; tag with TZ=UTC", out)
        self.assertEqual(code, 1)
        self.repo.git("tag", "-d", "v1.0.0")
        self.repo.git("tag", "-a", "v1.0.0", "-m", "x", env={"GIT_COMMITTER_EMAIL": "tagger@example.net"})
        code, out = self.rc("--expect-tag")
        self.assertIn("the tag's tagger email is not a GitHub noreply address", out)
        self.assertNotIn("tagger@example.net", out)


class DenylistTest(Base):
    def test_hits(self):
        secret_name = "Zq" + "Example Person"
        self.write_deny(["other entry", secret_name.upper()])
        # in the HEAD tree
        self.repo.write("README.md", f"# test\nby {secret_name}\n")
        self.repo.commit("readme")
        self.repo.sync_changelog()
        code, out = self.rc()
        self.assertIn("FAIL R9 personal details: entry #2 found in README.md (HEAD tree)", out)
        self.assertNotIn(secret_name.lower(), out.lower())
        # only in history
        self.repo.write("README.md", "# test\n")
        self.repo.commit("clean")
        self.repo.sync_changelog()
        code, out = self.rc()
        self.assertIn("FAIL R9 personal details: entry #2 found in history", out)
        self.assertNotIn(secret_name.lower(), out.lower())

    def test_commit_message_and_file_name(self):
        name = "Zq" + "Hidden Name"
        self.write_deny([name])
        self.repo.write("x.txt", "x\n")
        self.repo.commit(f"thanks {name}")
        self.repo.sync_changelog()
        code, out = self.rc()
        self.assertIn("FAIL R9 personal details: entry #1 found in commit metadata", out)
        self.assertNotIn(name.lower(), out.lower())
        self.write_deny(["zqfile"])
        self.repo.write("docs/zqfile.md", "x\n")
        self.repo.commit("f")
        self.repo.sync_changelog()
        code, out = self.rc()
        self.assertIn("entry #1 found in a file name (HEAD tree)", out)
        self.assertNotIn("zqfile", out.lower())

    def test_denylist_inside_repo(self):
        inside = os.path.join(self.repo.path, "deny.txt")
        with open(inside, "w") as f:
            f.write("x\n")
        code, out = livefakes.run_script(livefakes.RELEASE_CHECK, ["--version", "1.0.0", "--repo", self.repo.path,
                                                                   "--denylist", inside], env=self.env())
        self.assertEqual(code, 2, out)
        self.assertIn("release-check: keep the denylist outside the repository", out)

    def test_noreply_address_no_hit(self):
        self.write_deny(["Jane Example", "someone-else"])
        code, out = self.rc()
        self.assertEqual(self.status(out, "R9"), "PASS", out)


class GitleaksTest(Base):
    def test_missing(self):
        empty = os.path.join(self.tmp, "empty")
        os.mkdir(empty)
        git_dir = os.path.dirname(shutil.which("git"))
        code, out = self.rc(env=self.env(path=empty + os.pathsep + git_dir) | {"PATH": empty + os.pathsep +
                                                                               git_dir})
        self.assertIn("FAIL R8 secrets: gitleaks is not installed (brew install gitleaks)", out)
        self.assertOnlyFails(out, "R8")

    def test_finding(self):
        planted = "AKIA" + "Z" * 16
        livefakes.write_fake_gitleaks(self.bin, 1, {"RuleID": "aws-access-token", "File": "x.txt",
                                                    "Secret": planted, "Match": planted})
        code, out = self.rc()
        self.assertIn("FAIL R8 secrets: gitleaks found something (", out)
        self.assertIn("aws-access-token in x.txt", out)
        self.assertNotIn(planted, out)
        self.assertEqual(code, 1)

    def test_real_gitleaks(self):
        exe = shutil.which("gitleaks")
        if exe is None or exe.startswith(self.tmp):
            if livefakes.require_upstream():
                self.fail("needs gitleaks on PATH (REQUIRE_UPSTREAM=1)")
            self.skipTest("needs gitleaks on PATH")
        code, out = self.rc(env=self.env(path=os.path.dirname(exe)))
        self.assertEqual(self.status(out, "R8"), "PASS", out)
        self.assertEqual(code, 0, out)


class StaticTest(unittest.TestCase):
    def test_python310_and_usage(self):
        import ast
        with open(livefakes.RELEASE_CHECK, encoding="utf-8") as f:
            text = f.read()
        ast.parse(text, feature_version=(3, 10))
        self.assertIn("python3 scripts/release-check.py --version 1.0.0 [--denylist FILE] [--expect-tag] "
                      "[--repo PATH]", text)
        self.assertNotRegex(text, r"\b(push|commit|tag -a)\b.*check=True")


if __name__ == "__main__":
    unittest.main()
