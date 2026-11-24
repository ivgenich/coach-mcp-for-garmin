"""Give the user app what it keeps on /data. entrypoint.sh runs this as root before each server start.

    python -I /opt/garmin-ext/own_data.py

1. Stops every process still running as app (SIGKILL) and waits until none is left, so no leftover child of
   the server can swap a file under /data for a link while step 2 runs.
2. Walks /data without following a symbolic link and without leaving its file system, and gives app every
   entry that app does not own yet: the volume's root, the sign-in database, the Garmin tokens, key mode's
   token cache and the embedding model. A volume made before the server ran as app holds root's files, and
   garmin-mcp-auth over `railway ssh` or `docker compose exec` runs as root and writes root's files.

Directories are changed through the descriptor the walk opened (os.fwalk checks it is the directory it
listed); a regular file through a descriptor opened with O_NOFOLLOW, and only while it has one hard link;
a symbolic link or another entry by name with follow_symlinks=False, which never reaches the link's target.
Modes do not change. Idempotent. Standard library only; Linux (/proc). Exit status 0 when every entry that
needed it was changed, 1 otherwise (the script then logs a warning and goes on).
"""

from __future__ import annotations

import os
import pwd
import signal
import stat
import sys
import time
from typing import Callable, Iterable, List, Optional, Tuple

USER = "app"
DATA = "/data"
KILL_WAIT_S = 10.0
POLL_S = 0.1


def user_pids(uid: int, proc: str = "/proc") -> List[int]:
    """The live processes (not zombies) whose real, effective, saved or file-system uid is `uid`."""
    pids = []
    try:
        names = os.listdir(proc)
    except OSError:
        return pids
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc, name, "status"), encoding="ascii", errors="replace") as f:
                fields = dict(line.split(":", 1) for line in f if ":" in line)
        except OSError:
            continue                     # it exited meanwhile
        if fields.get("State", "").split()[:1] in (["Z"], ["X"]):
            continue                     # a zombie cannot run anything
        try:
            uids = {int(v) for v in fields.get("Uid", "").split()}
        except ValueError:
            continue
        if uid in uids:
            pids.append(int(name))
    return pids


def stop_user(uid: int, proc: str = "/proc", kill: Callable[[int, int], None] = os.kill,
              clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
              wait_s: float = KILL_WAIT_S) -> bool:
    """SIGKILL every process of `uid` until none is left. True when none is left within `wait_s`."""
    deadline = clock() + wait_s
    while True:
        pids = user_pids(uid, proc)
        if not pids:
            return True
        if clock() >= deadline:
            return False
        for pid in pids:
            try:
                kill(pid, signal.SIGKILL)
            except OSError:
                pass                     # already gone
        sleep(POLL_S)


def _chown_file(name: str, dir_fd: int, uid: int, gid: int) -> bool:
    """A regular file, through a descriptor: no link is followed and a second hard link is left alone."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_NOCTTY", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(name, flags, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid == uid:
            return False
        os.fchown(fd, uid, gid)
        return True
    finally:
        os.close(fd)


def own_tree(top: str, uid: int, gid: int) -> Tuple[int, int]:
    """Give uid:gid every entry under `top` (and `top`) not owned by uid. (changed, failed) counts."""
    changed = failed = 0
    try:
        top_st = os.lstat(top)
    except FileNotFoundError:
        return 0, 0
    if not stat.S_ISDIR(top_st.st_mode):
        return 0, 0                      # not a directory, or a link to one: never followed
    for _root, dirs, files, root_fd in os.fwalk(top, follow_symlinks=False):
        try:
            here = os.fstat(root_fd)
            if here.st_dev != top_st.st_dev:
                dirs[:] = []             # another file system mounted below: not ours to change
                continue
            if here.st_uid != uid:
                os.fchown(root_fd, uid, gid)
                changed += 1
        except OSError:
            failed += 1
        for name in list(dirs) + files:
            try:
                st = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                if stat.S_ISDIR(st.st_mode):
                    continue             # changed through its own descriptor when the walk enters it
                if st.st_uid == uid:
                    continue
                if stat.S_ISREG(st.st_mode):
                    if st.st_nlink == 1 and _chown_file(name, root_fd, uid, gid):
                        changed += 1
                    continue
                os.chown(name, uid, gid, dir_fd=root_fd, follow_symlinks=False)
                changed += 1
            except FileNotFoundError:
                continue                 # removed meanwhile
            except OSError:
                failed += 1
    return changed, failed


def main(argv: Optional[Iterable[str]] = None) -> int:
    _ = argv
    try:
        user = pwd.getpwnam(USER)
    except KeyError:
        print(f"own_data: there is no user {USER}", file=sys.stderr)
        return 1
    ok = stop_user(user.pw_uid)
    if not ok:
        print(f"own_data: processes of {USER} are still running; {DATA} was not changed", file=sys.stderr)
        return 1
    _changed, failed = own_tree(DATA, user.pw_uid, user.pw_gid)
    if failed:
        print(f"own_data: {failed} entries under {DATA} could not be given to {USER}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
