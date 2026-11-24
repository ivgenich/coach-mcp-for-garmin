"""Tests for own_data: the leftover processes of app are stopped, then /data is given to app without following
a link. Standard library only:

    cd ext && python3 -m unittest test_own_data -v
"""

from __future__ import annotations

import os
import signal
import tempfile
import unittest
from unittest import mock

import own_data


def status(uid, state="S (sleeping)"):
    return f"Name:\tpython\nState:\t{state}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nGid:\t0\t0\t0\t0\n"


class Processes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proc = self.tmp.name

    def add(self, pid, text):
        os.makedirs(os.path.join(self.proc, str(pid)))
        with open(os.path.join(self.proc, str(pid), "status"), "w", encoding="ascii") as f:
            f.write(text)

    def test_user_pids_finds_live_processes_of_the_user_only(self):
        self.add(1, status(0))
        self.add(20, status(10001))
        self.add(21, status(10001, "Z (zombie)"))
        self.add(22, "Name:\tx\nState:\tS\nUid:\t0\t10001\t0\t0\n")   # effective uid app
        self.add(23, "garbage")
        os.makedirs(os.path.join(self.proc, "self"))
        self.assertEqual(sorted(own_data.user_pids(10001, self.proc)), [20, 22])
        self.assertEqual(own_data.user_pids(10001, os.path.join(self.proc, "missing")), [])

    def test_stop_user_kills_until_none_is_left(self):
        self.add(20, status(10001))
        killed = []

        def kill(pid, sig):
            killed.append((pid, sig))
            os.remove(os.path.join(self.proc, str(pid), "status"))

        self.assertTrue(own_data.stop_user(10001, self.proc, kill=kill, sleep=lambda s: None))
        self.assertEqual(killed, [(20, signal.SIGKILL)])

    def test_stop_user_gives_up_after_the_wait(self):
        self.add(20, status(10001))
        t = [0.0]

        def sleep(s):
            t[0] += 1.0

        self.assertFalse(own_data.stop_user(10001, self.proc, kill=lambda p, s: None, clock=lambda: t[0],
                                            sleep=sleep, wait_s=3))

    def test_main_does_not_walk_while_processes_are_left(self):
        with mock.patch.object(own_data, "stop_user", return_value=False), \
                mock.patch.object(own_data, "own_tree") as walk, \
                mock.patch.object(own_data.pwd, "getpwnam", return_value=mock.Mock(pw_uid=10001, pw_gid=10001)), \
                mock.patch("sys.stderr"):
            self.assertEqual(own_data.main([]), 1)
        walk.assert_not_called()


class Tree(unittest.TestCase):
    """own_tree with a target uid that owns nothing here, and chown calls recorded instead of made."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = os.path.join(self.tmp.name, "data")
        os.makedirs(os.path.join(self.data, "tokens"))
        for name in ("auth.sqlite", "tokens/garmin_tokens.json", "linked"):
            with open(os.path.join(self.data, name), "w", encoding="utf-8") as f:
                f.write("x")
        os.link(os.path.join(self.data, "linked"), os.path.join(self.tmp.name, "second-link"))
        self.outside = os.path.join(self.tmp.name, "outside")
        os.makedirs(self.outside)
        with open(os.path.join(self.outside, "secret"), "w", encoding="utf-8") as f:
            f.write("x")
        os.symlink("/etc/passwd", os.path.join(self.data, "symlink"))
        os.symlink(self.outside, os.path.join(self.data, "dirlink"))
        self.uid = os.getuid() + 1
        self.changed = []

    def run_tree(self, top=None, uid=None):
        def fchown(fd, uid, gid):
            self.changed.append(("fd", fd))

        def chown(name, uid, gid, *, dir_fd=None, follow_symlinks=True):
            self.assertIsNotNone(dir_fd)
            self.assertFalse(follow_symlinks)
            self.changed.append(("name", name))

        opened = []
        real_open = os.open

        def tracked_open(name, flags, *args, **kwargs):
            fd = real_open(name, flags, *args, **kwargs)
            if flags & os.O_NOFOLLOW:            # _chown_file's opens (the walk's own have no O_NOFOLLOW)
                self.assertIsNotNone(kwargs.get("dir_fd"))
                opened.append(name)
            return fd

        with mock.patch.object(own_data.os, "fchown", fchown), mock.patch.object(own_data.os, "chown", chown), \
                mock.patch.object(own_data.os, "open", tracked_open):
            result = own_data.own_tree(top or self.data, self.uid if uid is None else uid, 0)
        return result, opened

    def test_everything_not_the_users_is_changed_and_no_link_is_followed(self):
        (changed, failed), opened = self.run_tree()
        self.assertEqual(failed, 0)
        names = sorted(n for kind, n in self.changed if kind == "name")
        self.assertEqual(names, ["dirlink", "symlink"])           # the links themselves, never their targets
        self.assertEqual(sorted(opened), ["auth.sqlite", "garmin_tokens.json"])   # "linked" has 2 hard links
        fds = [n for kind, n in self.changed if kind == "fd"]
        # data/, tokens/, auth.sqlite, garmin_tokens.json; "linked" has a second hard link and is left alone
        self.assertEqual(len(fds), 4)
        self.assertEqual(changed, 6)

    def test_what_the_user_owns_already_is_left_alone(self):
        (changed, failed), _opened = self.run_tree(uid=os.getuid())
        self.assertEqual((changed, failed), (0, 0))
        self.assertEqual(self.changed, [])

    def test_a_missing_folder_or_a_link_as_the_top_does_nothing(self):
        self.assertEqual(self.run_tree(top=os.path.join(self.tmp.name, "nothing"))[0], (0, 0))
        top_link = os.path.join(self.tmp.name, "top-link")
        os.symlink(self.data, top_link)
        self.assertEqual(self.run_tree(top=top_link)[0], (0, 0))
        self.assertEqual(self.changed, [])


if __name__ == "__main__":
    unittest.main()
