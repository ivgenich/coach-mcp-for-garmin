#!/usr/bin/env python3
"""Release gate for maintainers: checks this repository before the tag v<version>.

    python3 scripts/release-check.py --version 1.0.0 [--denylist FILE] [--expect-tag] [--since COMMIT]
                                     [--repo PATH]

Read-only: it never tags, commits or pushes, and every git call uses
`git --no-optional-locks`. The HEAD tree is read from `git archive HEAD` into a
temporary folder that is removed afterwards. Checks R1-R10: the version in
ext/version.py (read with ast, never imported), the CHANGELOG heading with the
UTC date of HEAD's commit, a clean tree on main, the tag state, GitHub noreply
emails in every commit, +0000 offsets in the commits since the previous release
tag (before the first tag: since BASELINE, or --since COMMIT) and on the
tag itself, tracked-file hygiene, gitleaks on
the HEAD tree and the history, a private denylist (kept outside the
repository; its entries are never printed) and script modes.
Exit 0 only on RESULT: PASS. Standard library plus the git and gitleaks
executables; any python3 3.10 or newer runs it.
"""

import argparse
import ast
import fnmatch
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

NOREPLY = "@users.noreply.github.com"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
REQUIRED_FILES = (".gitignore", "LICENSE", "README.md", "CHANGELOG.md", "SECURITY.md")
FORBIDDEN_NAMES = ("*.sqlite*", "*.token", "*.secret", "garmin_tokens.json", "*garmin_tokens.base64*",
                   "*garminconnect_base64*", "*.pyc", ".DS_Store")
FORBIDDEN_DIRS = ("tokens", "garmin-key", "__pycache__")
# The commits up to this one were made before the +0000 rule and keep their
# local offsets (rewriting published history is not worth it). Before the first
# release tag, R6 checks offsets only in the commits after it; once a tag
# exists, only the commits after the newest tag. A repository without this
# commit (a fork with new history, the tests) is checked in full.
BASELINE = "7b9c03e68a509715a04cc4ddc49b3d0c9179ff74"


class GitError(Exception):
    pass


class Repo:
    def __init__(self, path):
        self.path = path

    def git(self, *args, env_extra=None, check=True, binary=False):
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        if env_extra:
            env.update(env_extra)
        try:
            proc = subprocess.run(["git", "--no-optional-locks", "-C", self.path] + list(args),
                                  capture_output=True, env=env, timeout=300, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise GitError(type(e).__name__) from None
        if check and proc.returncode != 0:
            raise GitError(f"git {args[0]} exit {proc.returncode}")
        if binary:
            return proc.returncode, proc.stdout
        return proc.returncode, proc.stdout.decode("utf-8", "replace")

    def out(self, *args, **kw):
        return self.git(*args, **kw)[1]


def forbidden(path):
    base = path.rsplit("/", 1)[-1]
    if base != ".env.example" and (base == ".env" or base.startswith(".env.") or base.endswith(".env")):
        return True
    if any(fnmatch.fnmatchcase(base, p) for p in FORBIDDEN_NAMES):
        return True
    return any(seg in FORBIDDEN_DIRS for seg in path.split("/")[:-1])


def check_version_file(path, version):
    """(ok, detail)."""
    try:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename="ext/version.py")
    except (OSError, SyntaxError, ValueError) as e:
        return False, f"ext/version.py is not readable ({type(e).__name__})"
    values = []
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id == "VERSION" for t in targets):
            values.append(node.value)
    if len(values) != 1:
        return False, f"ext/version.py has {len(values)} top-level VERSION assignments (expected 1)"
    v = values[0]
    if not (isinstance(v, ast.Constant) and isinstance(v.value, str)):
        return False, "ext/version.py VERSION is not a string"
    if not VERSION_RE.match(v.value):
        return False, "ext/version.py VERSION is not in the form N.N.N"
    if v.value != version:
        return False, f"ext/version.py VERSION is {v.value}, not {version}"
    return True, f"ext/version.py VERSION is {version}"


def check_changelog(text, version, day):
    lines = [x.rstrip() for x in text.splitlines()]
    try:
        start = lines.index("## [Unreleased]")
    except ValueError:
        return False, 'CHANGELOG.md has no line "## [Unreleased]"'
    heading = next((x for x in lines[start + 1:] if x.startswith("## [")), None)
    want = f"## [{version}] - {day}"
    if heading is None:
        return False, f'CHANGELOG.md has no release heading after "## [Unreleased]" (expected "{want}")'
    if "[YANKED]" in heading.upper():
        return False, "the first release heading is marked [YANKED]"
    if heading != want:
        m = re.match(r"^## \[([^\]]*)\] - (\d{4}-\d\d-\d\d)$", heading)
        if m and m.group(1) == version:
            return False, (f"the {version} heading has the date {m.group(2)}, not {day} (the UTC date of HEAD's "
                           "commit)")
        return False, f'the first release heading after "## [Unreleased]" is not "{want}"'
    return True, f'"## [Unreleased]" and "{want}" (the UTC date of HEAD\'s commit)'


def read_tree(repo, dest):
    """Extract HEAD's tree into dest; returns the list of file paths."""
    _, data = repo.git("archive", "--format=tar", "HEAD", binary=True)
    names = []
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        members = [m for m in tar.getmembers() if m.isfile() or m.isdir()]
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, members=members, filter="data")
        else:  # pragma: no cover - Python without the extraction filters
            tar.extractall(dest, members=[m for m in members if not m.name.startswith(("/", "..")) and
                                          ".." not in m.name.split("/")])
        names = [m.name for m in members if m.isfile()]
    return names


def run_gitleaks(exe, args, report_dir, tree_dir=None):
    """(ok, findings as 'RuleID path' strings, error text)."""
    report = os.path.join(report_dir, f"gitleaks-{len(os.listdir(report_dir))}.json")
    cmd = [exe] + args + ["--no-banner", "--redact", "--exit-code", "1", "--report-format", "json",
                          "--report-path", report]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=600, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, [], type(e).__name__
    findings = []
    try:
        with open(report, encoding="utf-8") as f:
            data = json.load(f)
        for item in data if isinstance(data, list) else []:
            rule = str(item.get("RuleID", "?"))
            path = str(item.get("File", "?"))
            if tree_dir and path.startswith(tree_dir):
                path = path[len(tree_dir):].lstrip("/")
            rule = rule if re.match(r"^[A-Za-z0-9_.-]{1,64}$", rule) else "?"
            path = path if re.match(r"^[A-Za-z0-9_./ -]{1,200}$", path) else "?"
            findings.append(f"{rule} in {path}")
    except (OSError, ValueError, AttributeError):
        pass
    if proc.returncode == 0:
        return True, [], ""
    return False, findings, f"exit {proc.returncode}"


def offset_scope(repo, tag, since):
    """(revision range, words for the output) of the commits whose offsets R6 checks."""
    if since is None:
        code, prev = repo.git("describe", "--tags", "--abbrev=0", "--match", "v*", "--exclude", tag, "HEAD",
                              check=False)
        if code == 0 and prev.strip():
            prev = prev.strip()
            return f"refs/tags/{prev}..HEAD", f"since {prev}" if re.match(r"^[A-Za-z0-9_.-]{1,64}$", prev) else \
                "since the previous tag"
        since = BASELINE
    code, base = repo.git("rev-parse", "-q", "--verify", f"{since}^{{commit}}", check=False)
    if code == 0:
        base = base.strip()
        if repo.git("merge-base", "--is-ancestor", base, "HEAD", check=False)[0] == 0:
            return f"{base}..HEAD", f"after commit {base[:7]}"
    return "HEAD", "of HEAD's history"


def main(argv=None):
    p = argparse.ArgumentParser(prog="release-check.py", description="Read-only release gate (R1-R10).")
    p.add_argument("--version", required=True, help="the release version, e.g. 1.0.0")
    p.add_argument("--denylist", metavar="FILE", help="personal details to look for, one per line (kept outside "
                   "the repository; never printed)")
    p.add_argument("--expect-tag", action="store_true", help="v<version> must exist, be annotated and point at HEAD")
    p.add_argument("--since", metavar="COMMIT", help="check date offsets only in the commits after this one (default: "
                   "the newest release tag, else the built-in baseline)")
    p.add_argument("--repo", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   help="the repository (default: this one)")
    args = p.parse_args(argv)
    version = args.version
    if not VERSION_RE.match(version):
        print("release-check: --version must look like 1.0.0")
        return 2
    repo_path = os.path.realpath(args.repo)
    if not os.path.isdir(repo_path):
        print("release-check: --repo is not a folder")
        return 2
    if args.denylist:
        deny_path = os.path.realpath(args.denylist)
        if os.path.commonpath([deny_path, repo_path]) == repo_path:
            print("release-check: keep the denylist outside the repository")
            return 2
        try:
            with open(deny_path, encoding="utf-8") as f:
                entries = [x.strip() for x in f.read().splitlines() if x.strip()]
        except OSError:
            print("release-check: the denylist is not readable")
            return 2
    repo = Repo(repo_path)
    try:
        repo.out("rev-parse", "--verify", "HEAD^{commit}")
    except GitError:
        print("release-check: not a git repository with a commit")
        return 2
    if args.since is not None:
        code = 1 if args.since.startswith("-") else \
            repo.git("rev-parse", "-q", "--verify", f"{args.since}^{{commit}}", check=False)[0]
        if code != 0 or repo.git("merge-base", "--is-ancestor", args.since, "HEAD", check=False)[0] != 0:
            print("release-check: --since is not a commit in HEAD's history")
            return 2

    results = []  # (status, line)

    def add(status, rid, text):
        results.append(status)
        print(f"{status} {rid} {text}", flush=True)

    tag = f"v{version}"
    head = repo.out("rev-parse", "HEAD").strip()
    tmp = tempfile.mkdtemp(prefix="release-check-")
    try:
        tree_dir = os.path.join(tmp, "tree")
        report_dir = os.path.join(tmp, "reports")
        os.mkdir(tree_dir)
        os.mkdir(report_dir)
        try:
            tree_files = read_tree(repo, tree_dir)
        except (GitError, tarfile.TarError, OSError) as e:
            print(f"release-check: cannot read the HEAD tree ({type(e).__name__})")
            return 2

        # R1 version
        ok, detail = check_version_file(os.path.join(tree_dir, "ext", "version.py"), version)
        add("PASS" if ok else "FAIL", "R1 version:", detail)

        # R2 changelog
        day = repo.out("log", "-1", "--format=%cd", "--date=format-local:%Y-%m-%d", "HEAD",
                       env_extra={"TZ": "UTC"}).strip()
        try:
            with open(os.path.join(tree_dir, "CHANGELOG.md"), encoding="utf-8") as f:
                ok, detail = check_changelog(f.read(), version, day)
        except OSError:
            ok, detail = False, "CHANGELOG.md is not in the HEAD tree"
        add("PASS" if ok else "FAIL", "R2 changelog:", detail)

        # R3 branch and tree
        code, branch = repo.git("symbolic-ref", "--short", "-q", "HEAD", check=False)
        branch = branch.strip() if code == 0 else ""
        dirty = [x for x in repo.out("status", "--porcelain").splitlines() if x.strip()]
        problems = []
        if branch != "main":
            problems.append("not on branch main" if branch else "HEAD is detached")
        if dirty:
            problems.append(f"{len(dirty)} uncommitted or untracked paths")
        if problems:
            add("FAIL", "R3 git:", "; ".join(problems) + f" (HEAD {head[:7]})")
        else:
            add("PASS", "R3 git:", f"on branch main, working tree clean, HEAD {head[:7]}")

        # R4 tag
        code, _ = repo.git("rev-parse", "-q", "--verify", f"refs/tags/{tag}", check=False)
        exists = code == 0
        if args.expect_tag:
            if not exists:
                add("FAIL", "R4 tag:", f"{tag} does not exist")
            else:
                kind = repo.out("cat-file", "-t", f"refs/tags/{tag}").strip()
                target = repo.out("rev-parse", f"refs/tags/{tag}^{{commit}}").strip()
                if kind != "tag":
                    add("FAIL", "R4 tag:", f"{tag} is a lightweight tag; make it annotated (git tag -a)")
                elif target != head:
                    add("FAIL", "R4 tag:", f"{tag} does not point at HEAD")
                else:
                    add("PASS", "R4 tag:", f"{tag} is annotated and points at HEAD")
        elif exists:
            add("FAIL", "R4 tag:", f"{tag} already exists")
        else:
            add("PASS", "R4 tag:", f"{tag} does not exist yet")

        # R5 remotes
        remotes = [x for x in repo.out("remote").splitlines() if x.strip()]
        print(f"INFO R5 remotes: {len(remotes)} (this script never pushes)")

        # R6 identity and offsets
        problems = []
        code, local_email = repo.git("config", "--local", "user.email", check=False)
        if code != 0 or not local_email.strip().lower().endswith(NOREPLY):
            problems.append("repo-local user.email is not a GitHub noreply address")
        # Local branches and tags only: remote-tracking branches hold Dependabot's commits, which
        # GitHub signs with its own committer address and which are never released as they are.
        rows = [x for x in repo.out("log", "--branches", "--tags", "--format=%ae%x00%ce").splitlines() if x]
        bad_mail = sum(1 for r in rows if not all(e.strip().lower().endswith(NOREPLY) for e in r.split("\0")))
        span, scope = offset_scope(repo, tag, args.since)
        offsets = [x for x in repo.out("log", "--format=%ad %cd", "--date=format:%z", span, "--").splitlines() if x]
        bad_zone = sum(1 for r in offsets if any(o != "+0000" for o in r.split()))
        if bad_mail:
            problems.append(f"{bad_mail} of {len(rows)} commits carry an email that is not a GitHub noreply address")
        if bad_zone:
            problems.append(f"{bad_zone} of {len(offsets)} commits {scope} carry a local time zone offset; commit "
                            "with TZ=UTC")
        if args.expect_tag and exists:
            tagger = repo.out("for-each-ref", f"refs/tags/{tag}",
                              "--format=%(taggeremail)%00%(taggerdate:format:%z)").strip()
            mail, _, zone = tagger.partition("\0")
            if not mail.strip().strip("<>").lower().endswith(NOREPLY):
                problems.append("the tag's tagger email is not a GitHub noreply address")
            if zone.strip() != "+0000":
                problems.append("the tag carries a local time zone offset; tag with TZ=UTC")
        if problems:
            add("FAIL", "R6 identity:", "; ".join(problems))
        else:
            extra = "; the tag's tagger too" if args.expect_tag and exists else ""
            add("PASS", "R6 identity:", f"repo-local user.email is a GitHub noreply address; every author and "
                f"committer email in {len(rows)} commits is one too; every date offset in the {len(offsets)} commits "
                f"{scope} is +0000{extra}")

        # R7 tracked files
        tracked = [x for x in repo.out("ls-files", "-z").split("\0") if x]
        missing = [f for f in REQUIRED_FILES if f not in tracked]
        bad = sorted(f for f in tracked if forbidden(f))
        if missing or bad:
            parts = []
            if missing:
                parts.append("missing " + ", ".join(missing))
            if bad:
                shown = [f if re.match(r"^[A-Za-z0-9_./ -]{1,200}$", f) else "(a path with unusual characters)"
                         for f in bad]
                parts.append(f"{len(bad)} of the forbidden kinds: " + ", ".join(shown[:20]))
            add("FAIL", "R7 tracked files:", "; ".join(parts))
        else:
            add("PASS", "R7 tracked files:", ", ".join(REQUIRED_FILES) + " present; nothing of the forbidden kinds")

        # R8 gitleaks
        exe = shutil.which("gitleaks")
        if exe is None:
            add("FAIL", "R8 secrets:", "gitleaks is not installed (brew install gitleaks)")
        else:
            try:
                ver = subprocess.run([exe, "version"], capture_output=True, text=True, timeout=30,
                                     stdin=subprocess.DEVNULL).stdout.strip().splitlines()
                ver = ver[0] if ver and re.match(r"^v?[0-9][0-9A-Za-z.+-]{0,30}$", ver[0]) else "(unknown version)"
            except (OSError, subprocess.TimeoutExpired):
                ver = "(unknown version)"
            ok1, f1, e1 = run_gitleaks(exe, ["dir", tree_dir], report_dir, tree_dir=tree_dir + os.sep)
            ok2, f2, e2 = run_gitleaks(exe, ["git", repo_path, "--log-opts=--all"], report_dir)
            if ok1 and ok2:
                add("PASS", "R8 secrets:", f"gitleaks {ver.lstrip('v')} found nothing in the HEAD tree or the history")
            else:
                parts = []
                if not ok1:
                    parts.append("HEAD tree: " + (", ".join(f1[:20]) or e1))
                if not ok2:
                    parts.append("history: " + (", ".join(f2[:20]) or e2))
                add("FAIL", "R8 secrets:", "gitleaks found something (" + "; ".join(parts) + ")")

        # R9 denylist
        if not args.denylist:
            add("SKIP", "R9 personal details:", "no --denylist given (run it before tagging)")
        else:
            places = []
            for f in sorted(tree_files):
                try:
                    with open(os.path.join(tree_dir, f), "rb") as fh:
                        places.append((f, fh.read().decode("utf-8", "replace").lower(), f.lower()))
                except OSError:
                    continue
            meta = repo.out("log", "--all", "--format=%an%x00%ae%x00%cn%x00%ce%x00%B%x01").lower()
            tags = repo.out("for-each-ref", "refs/tags",
                            "--format=%(refname:short)%00%(taggername)%00%(taggeremail)%00%(contents)").lower()
            history = repo.out("log", "-p", "--all", "--format=%an%x00%ae%x00%cn%x00%ce%x00%B").lower()
            hits = []
            for i, entry in enumerate(entries, 1):
                e = entry.lower()
                where = None
                for name, text, lname in places:
                    if e in lname:
                        where = "a file name (HEAD tree)"
                        break
                    if e in text:
                        where = (name if re.match(r"^[A-Za-z0-9_./ -]{1,200}$", name) and e not in lname
                                 else "a file") + " (HEAD tree)"
                        break
                if where is None and e in meta:
                    where = "commit metadata"
                if where is None and e in tags:
                    where = "tag metadata"
                if where is None and e in history:
                    where = "history"
                if where is not None:
                    hits.append(f"entry #{i} found in {where}")
            if hits:
                add("FAIL", "R9 personal details:", "; ".join(hits))
            else:
                add("PASS", "R9 personal details:", f"{len(entries)} denylist entries, 0 hits in the HEAD tree, "
                    "commit metadata and history")

        # R10 modes
        staged = repo.out("ls-files", "-s", "--", "scripts")
        scripts, not_exec = 0, []
        for line in staged.splitlines():
            meta_part, _, path = line.partition("\t")
            if path.count("/") != 1 or not path.startswith("scripts/"):
                continue
            mode = meta_part.split(" ")[0]
            if path.endswith(".sh"):
                needs = True
            elif path.endswith(".py"):
                try:
                    with open(os.path.join(tree_dir, path), "rb") as fh:
                        needs = fh.read(2) == b"#!"
                except OSError:
                    _, blob = repo.git("show", f":{path}", binary=True, check=False)
                    needs = blob[:2] == b"#!"
            else:
                needs = False
            if needs:
                scripts += 1
                if mode != "100755":
                    not_exec.append(path)
        if not_exec:
            add("FAIL", "R10 modes:", f"{len(not_exec)} scripts are not executable in the index: "
                + ", ".join(not_exec) + " (git update-index --chmod=+x)")
        else:
            add("PASS", "R10 modes:", f"{scripts} scripts are executable in the index")
    except GitError as e:
        print(f"release-check: a git command failed ({e})")
        return 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if "FAIL" in results:
        print("RESULT: FAIL")
        return 1
    if "SKIP" in results:
        print("RESULT: NOT READY")
        return 1
    if args.expect_tag:
        print(f"RESULT: PASS ({tag} is annotated on HEAD; ready to push the tag)")
    else:
        print(f"RESULT: PASS (ready to tag {tag})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
