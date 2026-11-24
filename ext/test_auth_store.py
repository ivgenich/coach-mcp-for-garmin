"""Tests for auth_store: the sign-in records in sqlite.

    python -m unittest discover -s ext -p "test_auth_store*.py" -v

Standard library only; runs on Python 3.10+. Files go to a temporary
directory (TMPDIR). Fake clocks start at time.time() and only move forward
(F22). The F4 refresh replay window lives in owner_auth, not here: see
test_owner_auth.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth_config  # noqa: E402
import auth_store  # noqa: E402
from auth_store import AuthStore, check_writable, hash_secret  # noqa: E402

DAY = 86400.0
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


class FakeClock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now

    def advance(self, seconds):
        assert seconds >= 0, "fake clocks only move forward"
        self.now += seconds


def fresh(label="x"):
    return f"{label}_{secrets.token_urlsafe(32)}"


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.path = os.path.join(self.dir, "auth.sqlite")
        self.clock = FakeClock()
        self.iterations = mock.patch.object(auth_store, "SECRET_FP_ITERATIONS", 1000)
        self.iterations.start()
        self.addCleanup(self.iterations.stop)

    def open(self, path=None):
        store = AuthStore(path or self.path, clock=self.clock)
        self.addCleanup(store.close)
        return store

    def sql(self, query, args=()):
        """Read the file through a second connection."""
        db = sqlite3.connect(self.path)
        try:
            return db.execute(query, args).fetchall()
        finally:
            db.close()

    def write_sql(self, query, args=()):
        db = sqlite3.connect(self.path, isolation_level=None)
        try:
            db.execute(query, args)
        finally:
            db.close()

    # Helpers that walk a sign-in through the public API.

    def add_client(self, store, client_id=None, max_clients=200):
        client_id = client_id or "client-" + secrets.token_hex(4)
        store.add_client(client_id, '{"client_id": "%s"}' % client_id, max_clients=max_clients, evict_idle_days=7)
        return client_id

    def add_pending(self, store, client_id, **extra):
        pending = fresh("req")
        args = dict(pending_hash=hash_secret(pending), client_id=client_id,
                    redirect_uri="https://claude.ai/api/mcp/auth_callback", redirect_uri_explicit=True,
                    state="state-1", code_challenge="c" * 43, scopes="offline_access",
                    resource="https://coach.example.com/mcp", ttl=600, max_pending=500, max_per_client=5)
        args.update(extra)
        self.assertTrue(store.add_pending(**args))
        return pending

    def sign_in(self, store, client_id):
        """A code, exchanged: returns the grant id and the token hashes."""
        pending = self.add_pending(store, client_id)
        code = fresh("ac")
        self.assertIsNotNone(store.approve_pending(hash_secret(pending), code_hash=hash_secret(code), code_ttl=300))
        grant_id = secrets.token_hex(16)
        access, refresh = hash_secret(fresh("at")), hash_secret(fresh("rt"))
        result = store.exchange_code(code_hash=hash_secret(code), client_id=client_id, grant_id=grant_id,
                                     access_hash=access, refresh_hash=refresh, access_ttl=3600, refresh_ttl=30 * DAY)
        self.assertTrue(result["ok"])
        return {"grant_id": grant_id, "access": access, "refresh": refresh, "code": code}

    def rotate(self, store, client_id, old_hash, scopes="offline_access"):
        access, refresh = hash_secret(fresh("at")), hash_secret(fresh("rt"))
        result = store.rotate_refresh(old_hash=old_hash, client_id=client_id, access_hash=access,
                                      refresh_hash=refresh, access_scopes=scopes, access_ttl=3600,
                                      refresh_ttl=30 * DAY)
        return result, access, refresh


class Files(StoreCase):
    def test_new_file_is_0600(self):
        self.open()
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_new_directories_are_0700(self):
        path = os.path.join(self.dir, "a", "b", "auth.sqlite")
        old = os.umask(0o022)
        try:
            self.open(path)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, "a")).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, "a", "b")).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_existing_directory_left_unchanged(self):
        volume = os.path.join(self.dir, "data")
        os.mkdir(volume)
        os.chmod(volume, 0o755)
        self.open(os.path.join(volume, "auth.sqlite"))
        self.assertEqual(stat.S_IMODE(os.stat(volume).st_mode), 0o755)

    def test_existing_file_loses_group_and_other_bits(self):
        self.open().close()
        os.chmod(self.path, 0o644)
        self.open()
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_rollback_journal_not_wal(self):
        store = self.open()
        self.add_client(store)
        self.assertEqual(self.sql("PRAGMA journal_mode")[0][0].lower(), "delete")
        self.assertEqual(sorted(os.listdir(self.dir)), ["auth.sqlite"])

    def test_wal_file_is_switched_back(self):
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA journal_mode=WAL")
        db.close()
        self.open()
        self.assertEqual(self.sql("PRAGMA journal_mode")[0][0].lower(), "delete")


class Schema(StoreCase):
    def test_creates_every_table_and_version(self):
        self.open()
        tables = {r[0] for r in self.sql("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"meta", "clients", "pending", "codes", "grants", "tokens"} <= tables)
        indexes = {r[0] for r in self.sql("SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertTrue({"tokens_by_grant", "tokens_by_parent", "codes_by_client", "pending_by_client",
                         "grants_by_client"} <= indexes)
        self.assertEqual(self.sql("SELECT value FROM meta WHERE key = 'schema_version'"), [("1",)])

    def test_reopen_keeps_records(self):
        store = self.open()
        client_id = self.add_client(store)
        store.close()
        self.assertIsNotNone(self.open().get_client(client_id))

    def test_newer_schema_is_refused(self):
        self.open().close()
        self.write_sql("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
        with self.assertRaises(auth_store.SchemaVersionError) as cm:
            AuthStore(self.path, clock=self.clock)
        self.assertEqual(cm.exception.version, "2")
        self.assertEqual(check_writable(self.path), [
            f"AUTH_DB: {self.path} has schema version 2; this server knows 1; "
            "deleting the file signs everyone out and starts fresh"])
        # Nothing was changed by the refusal.
        self.assertEqual(self.sql("SELECT value FROM meta WHERE key = 'schema_version'"), [("2",)])


class CheckWritable(StoreCase):
    def assertCannotOpen(self, path, exc_name):
        lines = check_writable(path)
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith(f"AUTH_DB: cannot open {path}: {exc_name}: "), lines[0])
        self.assertTrue(lines[0].endswith("; deleting the file signs everyone out and starts fresh"), lines[0])
        self.assertNotIn("\n", lines[0])

    def test_ok_creates_the_file_0600(self):
        path = os.path.join(self.dir, "new", "auth.sqlite")
        self.assertEqual(check_writable(path), [])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(check_writable(path), [])   # an existing good file too
        # The probe write was rolled back.
        db = sqlite3.connect(path)
        try:
            self.assertEqual(db.execute("SELECT key FROM meta ORDER BY key").fetchall(), [("schema_version",)])
        finally:
            db.close()

    def test_parent_is_a_file(self):
        blocker = os.path.join(self.dir, "file")
        open(blocker, "w").close()
        self.assertCannotOpen(os.path.join(blocker, "auth.sqlite"), "NotADirectoryError")

    def test_path_is_a_directory(self):
        self.assertCannotOpen(self.dir, "IsADirectoryError")

    def test_not_a_database(self):
        with open(self.path, "wb") as f:
            f.write(b"this is not a database file" * 100)
        self.assertCannotOpen(self.path, "DatabaseError")

    @unittest.skipIf(IS_ROOT, "root ignores file modes")
    def test_unwritable_directory(self):
        locked = os.path.join(self.dir, "locked")
        os.mkdir(locked, 0o500)
        self.addCleanup(os.chmod, locked, 0o700)
        self.assertCannotOpen(os.path.join(locked, "auth.sqlite"), "PermissionError")
        self.assertCannotOpen(os.path.join(locked, "sub", "auth.sqlite"), "PermissionError")

    @unittest.skipIf(IS_ROOT, "root ignores file modes")
    def test_existing_file_in_a_directory_without_room_for_the_journal(self):
        self.assertEqual(check_writable(self.path), [])
        os.chmod(self.dir, 0o500)
        self.addCleanup(os.chmod, self.dir, 0o700)
        self.assertCannotOpen(self.path, "OperationalError")

    @unittest.skipIf(IS_ROOT, "root ignores file modes")
    def test_read_only_file(self):
        self.assertEqual(check_writable(self.path), [])
        os.chmod(self.path, 0o400)
        self.assertCannotOpen(self.path, "PermissionError")


class Basics(StoreCase):
    def test_hash_secret(self):
        self.assertEqual(hash_secret("abc"), hashlib.sha256(b"abc").hexdigest())
        self.assertEqual(len(hash_secret("\udce9 lone surrogate")), 64)

    def test_close_is_idempotent_and_final(self):
        store = self.open()
        store.close()
        store.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            store.get_client("x")

    def test_counts(self):
        store = self.open()
        self.assertEqual(store.counts(), (0, 0))
        client_id = self.add_client(store)
        self.add_client(store)
        tokens = self.sign_in(store, client_id)
        self.assertEqual(store.counts(), (2, 1))
        store.revoke_grant(tokens["grant_id"], "revoked by client")
        self.assertEqual(store.counts(), (2, 0))

    def test_touch_client(self):
        store = self.open()
        client_id = self.add_client(store)
        self.clock.advance(50)
        store.touch_client(client_id)
        self.assertEqual(self.sql("SELECT last_used_at FROM clients")[0][0], self.clock.now)


class SingleUse(StoreCase):
    def test_take_pending_once(self):
        store = self.open()
        pending = self.add_pending(store, self.add_client(store))
        self.assertIsNotNone(store.get_pending(hash_secret(pending)))
        row = store.take_pending(hash_secret(pending))
        self.assertEqual(row["state"], "state-1")
        self.assertIsNone(store.take_pending(hash_secret(pending)))
        self.assertIsNone(store.get_pending(hash_secret(pending)))

    def test_approve_pending_once(self):
        store = self.open()
        pending = self.add_pending(store, self.add_client(store))
        code1, code2 = fresh("ac"), fresh("ac")
        self.assertIsNotNone(store.approve_pending(hash_secret(pending), code_hash=hash_secret(code1), code_ttl=300))
        self.assertIsNone(store.approve_pending(hash_secret(pending), code_hash=hash_secret(code2), code_ttl=300))
        self.assertIsNotNone(store.get_code(hash_secret(code1)))
        self.assertIsNone(store.get_code(hash_secret(code2)))

    def test_expired_pending_cannot_be_taken(self):
        store = self.open()
        pending = self.add_pending(store, self.add_client(store))
        self.clock.advance(601)
        self.assertIsNone(store.get_pending(hash_secret(pending)))
        self.assertIsNone(store.take_pending(hash_secret(pending)))
        self.assertIsNone(store.approve_pending(hash_secret(pending), code_hash="h", code_ttl=300))
        self.assertIsNone(store.pending_failure(hash_secret(pending), max_failures=3))

    def test_code_once_and_reuse_revokes(self):
        store = self.open()
        client_id = self.add_client(store)
        tokens = self.sign_in(store, client_id)
        again = store.exchange_code(code_hash=hash_secret(tokens["code"]), client_id=client_id,
                                    grant_id="other", access_hash="a", refresh_hash="r", access_ttl=3600,
                                    refresh_ttl=30 * DAY)
        self.assertEqual(again, {"ok": False, "reason": "used", "revoked": (tokens["grant_id"], client_id)})
        self.assertEqual(self.sql("SELECT revoked_reason FROM grants"), [("authorization code reuse",)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM grants"), [(1,)])   # no second grant
        # A third use finds it already revoked.
        third = store.exchange_code(code_hash=hash_secret(tokens["code"]), client_id=client_id, grant_id="x",
                                    access_hash="a2", refresh_hash="r2", access_ttl=3600, refresh_ttl=30 * DAY)
        self.assertEqual(third["revoked"], None)

    def test_code_expired_or_foreign(self):
        store = self.open()
        client_id, other = self.add_client(store), self.add_client(store)
        pending = self.add_pending(store, client_id)
        code = fresh("ac")
        store.approve_pending(hash_secret(pending), code_hash=hash_secret(code), code_ttl=300)
        args = dict(code_hash=hash_secret(code), grant_id="g", access_hash="a", refresh_hash="r",
                    access_ttl=3600, refresh_ttl=30 * DAY)
        self.assertEqual(store.exchange_code(client_id=other, **args)["reason"], "missing")
        self.clock.advance(301)
        self.assertEqual(store.exchange_code(client_id=client_id, **args)["reason"], "expired")
        self.assertEqual(store.exchange_code(code_hash="nope", client_id=client_id, grant_id="g", access_hash="a",
                                             refresh_hash="r", access_ttl=1, refresh_ttl=1)["reason"], "missing")

    def test_rotate_once(self):
        store = self.open()
        client_id = self.add_client(store)
        tokens = self.sign_in(store, client_id)
        first, access2, refresh2 = self.rotate(store, client_id, tokens["refresh"])
        self.assertEqual(first["grant_id"], tokens["grant_id"])
        second, _, _ = self.rotate(store, client_id, tokens["refresh"])
        self.assertIsNone(second)
        child = store.get_token(refresh2, "refresh")
        self.assertEqual(child["parent_hash"], tokens["refresh"])
        self.assertEqual(child["scopes"], "offline_access")
        self.assertIsNone(child["used_at"])
        self.assertTrue(store.refresh_unused(refresh2))
        self.assertFalse(store.refresh_unused(tokens["refresh"]))
        self.assertIsNotNone(store.get_token(access2, "access"))
        self.assertIsNone(store.get_token(access2, "refresh"))

    def test_rotate_refused_for_other_client_expired_or_revoked(self):
        store = self.open()
        client_id, other = self.add_client(store), self.add_client(store)
        tokens = self.sign_in(store, client_id)
        self.assertIsNone(self.rotate(store, other, tokens["refresh"])[0])
        self.assertIsNone(self.rotate(store, client_id, tokens["access"])[0])   # an access token is not one
        tokens2 = self.sign_in(store, client_id)
        store.revoke_grant(tokens2["grant_id"], "revoked by client")
        self.assertIsNone(self.rotate(store, client_id, tokens2["refresh"])[0])
        self.clock.advance(30 * DAY + 1)
        self.assertIsNone(self.rotate(store, client_id, tokens["refresh"])[0])

    def test_rotation_narrows_access_scopes_only(self):
        store = self.open()
        client_id = self.add_client(store)
        tokens = self.sign_in(store, client_id)
        _, access, refresh = self.rotate(store, client_id, tokens["refresh"], scopes="")
        self.assertEqual(store.get_token(access, "access")["scopes"], "")
        self.assertEqual(store.get_token(refresh, "refresh")["scopes"], "offline_access")

    def test_revoke_once(self):
        store = self.open()
        client_id = self.add_client(store)
        tokens = self.sign_in(store, client_id)
        self.assertEqual(store.revoke_grant(tokens["grant_id"], "refresh token reuse"), client_id)
        self.assertIsNone(store.revoke_grant(tokens["grant_id"], "revoked by client"))
        self.assertEqual(self.sql("SELECT revoked_reason FROM grants"), [("refresh token reuse",)])
        self.assertIsNone(store.revoke_grant("unknown", "x"))

    def test_revoke_token_grant(self):
        store = self.open()
        client_id = self.add_client(store)
        tokens = self.sign_in(store, client_id)
        self.assertEqual(store.revoke_token_grant(tokens["access"], "revoked by client"),
                         (tokens["grant_id"], client_id))
        self.assertIsNone(store.revoke_token_grant(tokens["refresh"], "revoked by client"))
        self.assertIsNone(store.revoke_token_grant("unknown", "revoked by client"))
        self.assertIsNotNone(store.get_token(tokens["access"], "access")["revoked_at"])
        self.assertFalse(store.refresh_unused(tokens["refresh"]))

    def test_failures(self):
        store = self.open()
        pending = self.add_pending(store, self.add_client(store))
        h = hash_secret(pending)
        first = store.pending_failure(h, max_failures=3)
        self.assertEqual((first[0], first[2]), (1, False))
        self.assertEqual(store.pending_failure(h, max_failures=3)[0], 2)
        failures, row, deleted = store.pending_failure(h, max_failures=3)
        self.assertEqual((failures, deleted, row["state"]), (3, True, "state-1"))
        self.assertIsNone(store.get_pending(h))
        self.assertIsNone(store.pending_failure(h, max_failures=3))


class PendingCaps(StoreCase):
    def test_per_client_cap_deletes_own_oldest(self):
        store = self.open()
        client_id, other = self.add_client(store), self.add_client(store)
        theirs = self.add_pending(store, other)
        mine = []
        for _ in range(5):
            mine.append(self.add_pending(store, client_id))
            self.clock.advance(1)
        newest = self.add_pending(store, client_id)
        self.assertIsNone(store.get_pending(hash_secret(mine[0])))       # own oldest went
        for pending in mine[1:] + [newest]:
            self.assertIsNotNone(store.get_pending(hash_secret(pending)))
        self.assertIsNotNone(store.get_pending(hash_secret(theirs)))     # never another client's
        self.assertEqual(self.sql("SELECT COUNT(*) FROM pending WHERE client_id = ?", (client_id,)), [(5,)])

    def refused(self, store, client_id, **caps):
        return not store.add_pending(pending_hash="h", client_id=client_id, redirect_uri="https://x/cb",
                                     redirect_uri_explicit=True, state=None, code_challenge="c" * 43, scopes="",
                                     resource="r", ttl=600, **caps)

    def test_global_cap_refuses_without_deleting(self):
        store = self.open()
        owner, stranger = self.add_client(store), self.add_client(store)
        owners = self.add_pending(store, owner, max_pending=4)
        for _ in range(3):
            self.add_pending(store, stranger, max_pending=4)
        before = self.sql("SELECT pending_hash FROM pending ORDER BY pending_hash")
        self.assertTrue(self.refused(store, stranger, max_pending=4, max_per_client=5))
        self.assertTrue(self.refused(store, owner, max_pending=4, max_per_client=5))
        self.assertEqual(self.sql("SELECT pending_hash FROM pending ORDER BY pending_hash"), before)
        self.assertIsNotNone(store.get_pending(hash_secret(owners)))

    def test_refusal_rolls_back_the_clients_own_trim(self):
        store = self.open()
        owner, stranger = self.add_client(store), self.add_client(store)
        for _ in range(2):
            self.add_pending(store, owner, max_pending=10)
        for _ in range(3):
            self.add_pending(store, stranger, max_pending=10)
        before = self.sql("SELECT pending_hash FROM pending ORDER BY pending_hash")
        # Trimming one of the stranger's 3 rows still leaves 4 live rows: refused, trim undone.
        self.assertTrue(self.refused(store, stranger, max_pending=4, max_per_client=3))
        self.assertEqual(self.sql("SELECT pending_hash FROM pending ORDER BY pending_hash"), before)

    def test_own_trim_makes_room_in_a_full_table(self):
        store = self.open()
        owner, stranger = self.add_client(store), self.add_client(store)
        owners = self.add_pending(store, owner, max_pending=4)
        for _ in range(3):
            self.add_pending(store, stranger, max_pending=4)
        # The stranger replaces its own oldest row; the owner's row stays.
        self.assertFalse(self.refused(store, stranger, max_pending=4, max_per_client=3))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM pending"), [(4,)])
        self.assertIsNotNone(store.get_pending(hash_secret(owners)))

    def test_expired_rows_do_not_count(self):
        store = self.open()
        client_id = self.add_client(store)
        for _ in range(4):
            self.add_pending(store, client_id, max_pending=4)
        self.clock.advance(601)
        self.add_pending(store, client_id, max_pending=4)

    def test_add_pending_touches_the_client(self):
        store = self.open()
        client_id = self.add_client(store)
        self.clock.advance(10)
        self.add_pending(store, client_id)
        self.assertEqual(self.sql("SELECT last_used_at FROM clients")[0][0], self.clock.now)

    def test_strangers_at_their_own_caps_leave_room_for_the_owner(self):
        # V1-3: with the real caps, every other stored client at its per-client
        # limit cannot fill the table, so no stranger can make the owner's
        # sign-in fail with "too many sign-ins in progress".
        store = self.open()
        caps = dict(max_pending=auth_config.MAX_PENDING, max_per_client=auth_config.MAX_PENDING_PER_CLIENT)
        strangers = [self.add_client(store) for _ in range(auth_config.MAX_CLIENTS - 1)]
        owner = self.add_client(store)
        rows = [(hash_secret(fresh("req")), client_id, self.clock.now, self.clock.now + 600)
                for client_id in strangers for _ in range(auth_config.MAX_PENDING_PER_CLIENT)]
        db = sqlite3.connect(self.path)
        try:
            with db:
                db.executemany("INSERT INTO pending (pending_hash, client_id, redirect_uri, redirect_uri_explicit, "
                               "state, code_challenge, scopes, resource, created_at, expires_at) "
                               "VALUES (?, ?, 'https://x/cb', 1, NULL, 'c', '', 'r', ?, ?)", rows)
        finally:
            db.close()
        for _ in range(auth_config.MAX_PENDING_PER_CLIENT):
            self.add_pending(store, owner, **caps)
        self.add_pending(store, owner, **caps)                    # past its own cap, its oldest makes room
        self.assertEqual(self.sql("SELECT COUNT(*) FROM pending WHERE client_id = ?", (owner,)),
                         [(auth_config.MAX_PENDING_PER_CLIENT,)])


class Eviction(StoreCase):
    def test_under_the_cap_nothing_is_evicted(self):
        store = self.open()
        self.add_client(store, "a", max_clients=2)
        self.assertEqual(store.add_client("b", "{}", max_clients=2, evict_idle_days=7), [])

    def test_tier1_least_recently_used_idle_client(self):
        store = self.open()
        idle_old = self.add_client(store, "idle-old")
        self.clock.advance(10)
        with_token = self.add_client(store, "with-token")
        self.sign_in(store, with_token)
        self.clock.advance(10)
        idle_new = self.add_client(store, "idle-new")
        self.clock.advance(10)
        evicted = store.add_client("newcomer", "{}", max_clients=3, evict_idle_days=7)
        self.assertEqual(evicted, [{"client_id": idle_old, "tier": 1, "revoked_grants": []}])
        self.assertIsNone(store.get_client(idle_old))
        for kept in (with_token, idle_new, "newcomer"):
            self.assertIsNotNone(store.get_client(kept))

    def test_in_flight_and_live_clients_are_skipped(self):
        store = self.open()
        pending_client = self.add_client(store, "pending")
        self.add_pending(store, pending_client)
        code_client = self.add_client(store, "code")
        store.approve_pending(hash_secret(self.add_pending(store, code_client)), code_hash="ch", code_ttl=300)
        token_client = self.add_client(store, "token")
        self.sign_in(store, token_client)
        revoked_client = self.add_client(store, "revoked")
        store.revoke_grant(self.sign_in(store, revoked_client)["grant_id"], "revoked by client")
        self.clock.advance(5)
        evicted = store.add_client("newcomer", "{}", max_clients=4, evict_idle_days=7)
        self.assertEqual([e["client_id"] for e in evicted], ["revoked"])
        # The evicted client's rows are gone with it.
        for table in ("pending", "codes", "grants", "tokens"):
            self.assertEqual(self.sql(f"SELECT COUNT(*) FROM {table} WHERE client_id = 'revoked'"), [(0,)])

    def test_tier2_evicts_a_week_idle_client_and_revokes(self):
        store = self.open()
        old = self.add_client(store, "old")
        self.sign_in(store, old)
        self.clock.advance(3 * DAY)
        recent = self.add_client(store, "recent")
        self.sign_in(store, recent)
        self.clock.advance(4 * DAY + 60)   # "old" is idle 7 days + 1 minute, "recent" 4 days
        evicted = store.add_client("newcomer", "{}", max_clients=2, evict_idle_days=7)
        self.assertEqual(len(evicted), 1)
        self.assertEqual((evicted[0]["client_id"], evicted[0]["tier"], len(evicted[0]["revoked_grants"])),
                         ("old", 2, 1))
        self.assertIsNone(store.get_client("old"))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM grants WHERE client_id = 'old'"), [(0,)])
        self.assertIsNotNone(store.get_client("recent"))

    def test_none_evictable_refuses_and_changes_nothing(self):
        store = self.open()
        a, b = self.add_client(store, "a"), self.add_client(store, "b")
        self.sign_in(store, a)
        self.sign_in(store, b)
        self.clock.advance(6 * DAY)    # idle, but not more than 7 days
        before = self.sql("SELECT client_id FROM clients ORDER BY client_id")
        with self.assertRaises(auth_store.ClientLimitError):
            store.add_client("newcomer", "{}", max_clients=2, evict_idle_days=7)
        self.assertEqual(self.sql("SELECT client_id FROM clients ORDER BY client_id"), before)

    def test_tier2_counts_the_newest_activity(self):
        store = self.open()
        a = self.add_client(store, "a")
        tokens = self.sign_in(store, a)
        self.clock.advance(6 * DAY)
        self.rotate(store, a, tokens["refresh"])           # grant used: activity is now
        b = self.add_client(store, "b")
        self.sign_in(store, b)
        self.clock.advance(2 * DAY)
        with self.assertRaises(auth_store.ClientLimitError):
            store.add_client("newcomer", "{}", max_clients=2, evict_idle_days=7)

    # V1-3: only what needs the owner secret (a live refresh token, an unused
    # code) keeps a client out of tier 1. A consent request costs a stranger one
    # unauthenticated /authorize, so it only decides the order.

    def test_old_clients_with_live_consent_requests_are_still_evicted(self):
        store = self.open()
        cap = auth_config.MAX_CLIENTS
        strangers = [self.add_client(store, "stranger-%03d" % n, max_clients=cap) for n in range(cap)]
        self.clock.advance(auth_config.PENDING_TTL + 1)
        for client_id in strangers:                       # each re-authorized: a live consent request
            self.add_pending(store, client_id)
        evicted = store.add_client("newcomer", "{}", max_clients=cap, evict_idle_days=7,
                                   pending_ttl=auth_config.PENDING_TTL)
        self.assertEqual(evicted, [{"client_id": "stranger-000", "tier": 1, "revoked_grants": []}])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM clients"), [(cap,)])

    def test_an_idle_owner_outlasts_every_client_without_owner_state(self):
        store = self.open()
        owner = self.add_client(store, "owner")
        tokens = self.sign_in(store, owner)               # a refresh token: only the owner secret makes one
        self.clock.advance(8 * DAY)                       # the owner has not used Claude for 8 days
        for n in range(3):                                # fresh sign-ins in flight, made without the secret
            self.add_pending(store, self.add_client(store, "stranger-%d" % n))
            self.clock.advance(1)
        for n in range(3, 6):                             # each newcomer starts a sign-in too
            evicted = store.add_client("stranger-%d" % n, "{}", max_clients=4, evict_idle_days=7)
            self.assertEqual(evicted, [{"client_id": "stranger-%d" % (n - 3), "tier": 1, "revoked_grants": []}])
            self.add_pending(store, "stranger-%d" % n)
            self.clock.advance(1)
        self.assertIsNotNone(store.get_client(owner))
        self.assertEqual(self.sql("SELECT revoked_at FROM grants WHERE grant_id = ?", (tokens["grant_id"],)),
                         [(None,)])

    def test_sign_ins_in_flight_go_last_and_the_newest_last_of_all(self):
        store = self.open()
        for name in ("a", "b", "c"):                       # three fresh sign-ins in flight, oldest first
            self.add_pending(store, self.add_client(store, name))
            self.clock.advance(10)
        self.add_pending(store, "a")                       # touching a client does not move it back
        self.assertEqual([e["client_id"] for e in store.add_client("d", "{}", max_clients=3, evict_idle_days=7)],
                         ["a"])
        self.clock.advance(10)
        # "d" has no consent request: it goes before b and c, although it is newer.
        self.assertEqual([e["client_id"] for e in store.add_client("e", "{}", max_clients=3, evict_idle_days=7)],
                         ["d"])
        self.assertEqual([e["client_id"] for e in store.add_client("f", "{}", max_clients=3, evict_idle_days=7)],
                         ["e"])
        # Only sign-ins in flight are left: the oldest goes, the newest stays.
        self.add_pending(store, "f")
        self.assertEqual([e["client_id"] for e in store.add_client("g", "{}", max_clients=3, evict_idle_days=7)],
                         ["b"])
        self.assertIsNotNone(store.get_client("c"))

    def test_a_consent_request_counts_as_in_flight_only_on_a_new_client(self):
        store = self.open()
        old = self.add_client(store, "old")
        self.clock.advance(auth_config.PENDING_TTL - 5)
        self.add_pending(store, old)                       # re-authorized: live for ten more minutes
        self.clock.advance(5)
        recent = self.add_client(store, "recent")          # no consent request, used after "old"
        self.clock.advance(5)                              # "old" is now older than PENDING_TTL
        # "old" does not count as in flight, so the least recently used goes.
        evicted = store.add_client("newcomer", "{}", max_clients=2, evict_idle_days=7,
                                   pending_ttl=auth_config.PENDING_TTL)
        self.assertEqual([e["client_id"] for e in evicted], ["old"])
        self.assertIsNotNone(store.get_client(recent))

    def test_the_owners_old_client_reconnecting_goes_last(self):
        # The owner reconnects an old client after an OWNER_SECRET change (its
        # grant revoked, so tier 1) while the table is full of a stranger's
        # fresh sign-ins in flight. Its grant row needs the owner secret, so it
        # goes after all of them; a consent request alone would protect any old
        # client (test above), so the grant row is what counts.
        store = self.open()
        owner = self.add_client(store, "owner")
        store.revoke_grant(self.sign_in(store, owner)["grant_id"], auth_store.REASON_SECRET_CHANGED)
        self.clock.advance(DAY)
        owners = self.add_pending(store, owner)            # the consent page is open now
        for n in range(3):
            self.add_pending(store, self.add_client(store, "stranger-%d" % n))
            self.clock.advance(1)
        for n in range(3, 7):                             # each newcomer starts a sign-in too
            evicted = store.add_client("stranger-%d" % n, "{}", max_clients=4, evict_idle_days=7,
                                       pending_ttl=auth_config.PENDING_TTL)
            self.assertEqual(evicted, [{"client_id": "stranger-%d" % (n - 3), "tier": 1, "revoked_grants": []}])
            self.add_pending(store, "stranger-%d" % n)
            self.clock.advance(1)
        self.assertIsNotNone(store.get_pending(hash_secret(owners)))

    def test_the_owners_reconnecting_client_still_goes_when_it_is_the_only_candidate(self):
        # Protection decides the order only: registration never fails on it.
        store = self.open()
        owner = self.add_client(store, "owner")
        store.revoke_grant(self.sign_in(store, owner)["grant_id"], auth_store.REASON_SECRET_CHANGED)
        self.add_pending(store, owner)
        other = self.add_client(store, "other")
        self.sign_in(store, other)                        # a live grant: not in tier 1
        evicted = store.add_client("newcomer", "{}", max_clients=2, evict_idle_days=7,
                                   pending_ttl=auth_config.PENDING_TTL)
        self.assertEqual([e["client_id"] for e in evicted], ["owner"])


class Purge(StoreCase):
    def test_purge_rules(self):
        store = self.open()
        client_id = self.add_client(store)
        live = self.sign_in(store, client_id)          # stays: its refresh token is valid for 30 days
        revoked = self.sign_in(store, client_id)
        store.revoke_grant(revoked["grant_id"], "revoked by client")
        pending = self.add_pending(store, client_id)
        self.clock.advance(601)
        store.purge()
        self.assertEqual(self.sql("SELECT COUNT(*) FROM pending"), [(0,)])          # past expiry
        self.assertEqual(self.sql("SELECT COUNT(*) FROM codes"), [(2,)])            # not yet 1 h past
        self.assertIsNone(store.get_pending(hash_secret(pending)))
        self.clock.advance(3600)                       # codes 1 h past expiry; access tokens just expired
        store.purge()
        self.assertEqual(self.sql("SELECT COUNT(*) FROM codes"), [(0,)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM tokens WHERE kind = 'access'"), [(2,)])
        self.clock.advance(3600)                       # access tokens 1 h past expiry
        store.purge()
        self.assertEqual(self.sql("SELECT COUNT(*) FROM tokens WHERE kind = 'access'"), [(0,)])
        self.clock.advance(30 * DAY)                   # refresh tokens expired; the revoked grant is 30 days old
        store.purge()
        self.assertEqual(self.sql("SELECT COUNT(*) FROM tokens"), [(0,)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM grants"), [(0,)])
        self.assertIsNotNone(store.get_client(client_id))   # clients are only evicted, never purged
        self.assertTrue(live)

    def test_revoked_grant_kept_for_30_days(self):
        store = self.open()
        client_id = self.add_client(store)
        tokens = self.sign_in(store, client_id)
        store.revoke_grant(tokens["grant_id"], "refresh token reuse")
        self.clock.advance(29 * DAY)
        store.purge()
        self.assertEqual(self.sql("SELECT COUNT(*) FROM grants"), [(1,)])
        self.clock.advance(DAY)
        store.purge()
        self.assertEqual(self.sql("SELECT COUNT(*) FROM grants"), [(0,)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM tokens"), [(0,)])

    def test_grant_without_tokens_goes_after_a_day(self):
        store = self.open()
        client_id = self.add_client(store)
        tokens = self.sign_in(store, client_id)
        self.write_sql("DELETE FROM tokens WHERE grant_id = ?", (tokens["grant_id"],))
        self.clock.advance(DAY - 10)
        store.purge()
        self.assertEqual(self.sql("SELECT COUNT(*) FROM grants"), [(1,)])
        self.clock.advance(10)
        store.purge()
        self.assertEqual(self.sql("SELECT COUNT(*) FROM grants"), [(0,)])

    def test_purge_runs_at_open_and_every_ten_minutes_of_writes(self):
        store = self.open()
        client_id = self.add_client(store)
        self.add_pending(store, client_id, ttl=1)
        store.close()
        self.clock.advance(2)
        store = self.open()                        # at open
        self.assertEqual(self.sql("SELECT COUNT(*) FROM pending"), [(0,)])
        self.add_pending(store, client_id, ttl=1)
        self.clock.advance(598)
        store.touch_client(client_id)              # a write 598 s after the last purge: no purge
        self.assertEqual(self.sql("SELECT COUNT(*) FROM pending"), [(1,)])
        self.clock.advance(2)
        store.touch_client(client_id)              # 600 s: purge
        self.assertEqual(self.sql("SELECT COUNT(*) FROM pending"), [(0,)])


class OwnerSecretFingerprint(StoreCase):
    def test_absent_equal_changed(self):
        secret = "TEST-ONLY-" + secrets.token_urlsafe(24)
        store = self.open()
        self.assertFalse(store.owner_secret_changed(secret))      # absent: stored
        fp = self.sql("SELECT value FROM meta WHERE key = 'owner_secret_fp'")[0][0]
        algorithm, iterations, salt, digest = fp.split("$")
        self.assertEqual((algorithm, iterations, len(salt), len(digest)), ("pbkdf2_sha256", "1000", 32, 64))
        self.assertNotIn(secret, fp)
        self.assertFalse(store.owner_secret_changed(secret))      # equal
        self.assertEqual(self.sql("SELECT value FROM meta WHERE key = 'owner_secret_fp'")[0][0], fp)

        client_id = self.add_client(store)
        live = self.sign_in(store, client_id)
        used_code = live["code"]
        pending = self.add_pending(store, client_id)
        unused_code = fresh("ac")
        store.approve_pending(hash_secret(self.add_pending(store, client_id)),
                              code_hash=hash_secret(unused_code), code_ttl=300)

        self.assertTrue(store.owner_secret_changed(secret + "x"))  # changed: revoke all
        self.assertEqual(self.sql("SELECT revoked_reason FROM grants"), [("owner secret changed",)])
        self.assertIsNone(store.get_pending(hash_secret(pending)))
        self.assertIsNone(store.get_code(hash_secret(unused_code)))
        self.assertIsNotNone(store.get_code(hash_secret(used_code)))   # kept for reuse detection
        self.assertNotEqual(self.sql("SELECT value FROM meta WHERE key = 'owner_secret_fp'")[0][0], fp)
        self.assertFalse(store.owner_secret_changed(secret + "x"))

    def test_survives_reopen_and_iteration_change(self):
        secret = "TEST-ONLY-" + secrets.token_urlsafe(24)
        self.open().owner_secret_changed(secret)
        with mock.patch.object(auth_store, "SECRET_FP_ITERATIONS", 2000):
            # The stored iteration count is used to compare.
            self.assertFalse(self.open().owner_secret_changed(secret))

    def test_damaged_fingerprint_counts_as_changed(self):
        secret = "TEST-ONLY-" + secrets.token_urlsafe(24)
        store = self.open()
        for damaged in ("garbage", "pbkdf2_sha256$x$00$00", "md5$1000$00$00", "pbkdf2_sha256$0$00$00",
                        "pbkdf2_sha256$99999999999$00$00", "pbkdf2_sha256$1000$$00"):
            with self.subTest(damaged=damaged):
                self.write_sql("INSERT OR REPLACE INTO meta (key, value) VALUES ('owner_secret_fp', ?)", (damaged,))
                self.assertTrue(store.owner_secret_changed(secret))

    def test_default_iterations(self):
        self.iterations.stop()
        try:
            self.assertEqual(auth_store.SECRET_FP_ITERATIONS, 600_000)
        finally:
            self.iterations.start()


if __name__ == "__main__":
    unittest.main()
