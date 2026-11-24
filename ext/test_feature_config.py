"""Tests for feature_config.py: the tracing and history-search settings and their --check-config lines.

    python -m unittest discover -s ext -p "test_feature_config*.py" -v

Standard library only. Every secret here is a fake made at run time.
"""

from __future__ import annotations

import base64
import secrets
import unittest

import feature_config as fc

FULL = {"GARMIN_AUTH": "full"}
KEY = {"GARMIN_AUTH": "key"}


def fake_key():
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def db_url(password=None):
    password = password or "TEST-ONLY-" + secrets.token_urlsafe(12)
    return f"postgresql://postgres:{password}@postgres.railway.internal:5432/railway"


def history_env(**changes):
    env = {**FULL, "HISTORY_SEARCH": "on", "DATABASE_URL": db_url(), "HISTORY_TOKEN_KEY": fake_key()}
    env.update(changes)
    return {k: v for k, v in env.items() if v is not None}


def tracing_env(**changes):
    env = {"TRACING": "on", "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example.com/otlp"}
    env.update(changes)
    return {k: v for k, v in env.items() if v is not None}


class Defaults(unittest.TestCase):
    def test_nothing_set_is_both_off_and_valid(self):
        self.assertEqual(fc.problems({}), [])
        cfg = fc.load({})
        self.assertEqual(cfg, fc.FeatureConfig())
        self.assertFalse(cfg.tracing or cfg.history or cfg.history_forget)
        self.assertEqual(cfg.role, "app")
        self.assertEqual(cfg.summary(), "")
        self.assertFalse(cfg.any_on)

    def test_off_spelled_out_and_case_and_spaces(self):
        env = {"TRACING": " OFF ", "HISTORY_SEARCH": "Off", "ROLE": " App "}
        self.assertEqual(fc.problems(env), [])
        self.assertEqual(fc.load(env).role, "app")

    def test_a_bad_switch_value_is_one_line_and_counts_as_off(self):
        for name, line in (("TRACING", fc.ERR_T1), ("HISTORY_SEARCH", fc.ERR_H1)):
            for value in ("yes", "1", "true", "onn"):
                with self.subTest(name=name, value=value):
                    self.assertEqual(fc.problems({name: value}), [line])
                    cfg = fc.load({name: value})
                    self.assertFalse(cfg.tracing or cfg.history)

    def test_off_ignores_the_features_other_settings(self):
        env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "ftp://x", "OTEL_EXPORTER_OTLP_HEADERS": "\x01",
               "OTEL_SERVICE_NAME": "x", "WORKER_CONCURRENCY": "99", "HISTORY_TOKEN_KEY": "short",
               "DATABASE_URL": "mysql://x"}
        self.assertEqual(fc.problems(env), [])
        cfg = fc.load(env)
        self.assertFalse(cfg.tracing)
        self.assertIn(fc.NOTE_TRACING_OFF_SETTINGS, cfg.notes)

    def test_features_off_ignores_values_the_old_server_ignored(self):
        """Review fix: with both features off, settings the server before them never read refuse nothing."""
        for env in ({"OTEL_SDK_DISABLED": "maybe"}, {"ROLE": "web"}, {"ROLE": "scheduler", "TRACING": "off"},
                    {"HISTORY_FORGET": "no"}, {"HISTORY_FORGET": "true", "HISTORY_SEARCH": "off"},
                    {"OTEL_SDK_DISABLED": "1", "ROLE": "x", "HISTORY_FORGET": "0"},
                    {"OTEL_EXPORTER_OTLP_TRACES_HEADERS": "a=b", "OTEL_EXPORTER_OTLP_TIMEOUT": "5"}):
            with self.subTest(env=env):
                self.assertEqual(fc.problems(env), [])
                cfg = fc.load(env)
                self.assertEqual((cfg.role, cfg.history_forget, cfg.tracing), ("app", False, False))
        # ROLE=worker or enqueue still needs history on; the checks come back with each feature on.
        self.assertEqual(fc.problems({"ROLE": "worker"}), [fc.ERR_H5.format(role="worker")])
        self.assertEqual(fc.problems(tracing_env(OTEL_SDK_DISABLED="maybe")), [fc.ERR_T6])
        self.assertEqual(fc.problems(history_env(ROLE="web")), [fc.ERR_H4])
        self.assertEqual(fc.problems(history_env(HISTORY_FORGET="no")), [fc.ERR_H6])


class Tracing(unittest.TestCase):
    def test_on_with_an_https_endpoint(self):
        cfg = fc.load(tracing_env())
        self.assertEqual(fc.problems(tracing_env()), [])
        self.assertTrue(cfg.tracing)
        self.assertEqual(cfg.otlp_endpoint, "https://otlp.example.com/otlp")
        self.assertEqual(cfg.summary(), "tracing on")

    def test_endpoint_required(self):
        self.assertEqual(fc.problems(tracing_env(OTEL_EXPORTER_OTLP_ENDPOINT=None)), [fc.ERR_T2])
        self.assertEqual(fc.problems(tracing_env(OTEL_EXPORTER_OTLP_ENDPOINT=" ")), [fc.ERR_T2])

    def test_endpoint_shape(self):
        for bad in ("http://otlp.example.com", "otlp.example.com", "https://", "https://user:pw@h.example.com",
                    "https://h.example.com/?x=1", "https://h.example.com/#x", "https://h.example.com:0",
                    "https://h .example.com", "ftp://h.example.com"):
            with self.subTest(bad=bad):
                self.assertEqual(fc.problems(tracing_env(OTEL_EXPORTER_OTLP_ENDPOINT=bad)), [fc.ERR_T3])
        for good in ("http://127.0.0.1:4318", "http://localhost:4318", "http://[::1]:4318",
                     "https://otlp-gateway-prod-eu-west-2.grafana.net/otlp"):
            with self.subTest(good=good):
                self.assertEqual(fc.problems(tracing_env(OTEL_EXPORTER_OTLP_ENDPOINT=good)), [])

    def test_loopback_http_is_refused_on_railway(self):
        env = tracing_env(OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:4318", RAILWAY_SERVICE_ID="abc")
        self.assertEqual(fc.problems(env), [fc.ERR_T3])

    def test_headers_shape_and_never_echoed(self):
        secret = "TEST-ONLY-" + secrets.token_urlsafe(24)
        self.assertEqual(fc.problems(tracing_env(OTEL_EXPORTER_OTLP_HEADERS=f"Authorization=Basic%20{secret}")), [])
        for bad in (secret, f"Authorization={secret}\x01", "=x", "a=", "bad name=x", "a" * 2049 + "=x"):
            with self.subTest(bad=bad[:20]):
                lines = fc.problems(tracing_env(OTEL_EXPORTER_OTLP_HEADERS=bad))
                self.assertEqual(lines, [fc.ERR_T4])
                self.assertNotIn(secret, "".join(lines))

    def test_the_check_and_the_exporter_read_headers_the_same_way(self):
        """Review fix S3: "X_Scope_OrgID" passed the check but telemetry.parse_headers() returned {} for it, so
        every header was dropped and the export went unauthenticated. One rule now (parse_header_pairs): every
        HTTP token name passes both; anything else is refused by the check (ERR_T4), never dropped."""
        example = "Authorization=Basic%20abc,X_Scope_OrgID=tenant1"
        self.assertEqual(fc.problems(tracing_env(OTEL_EXPORTER_OTLP_HEADERS=example)), [])
        self.assertEqual(fc.parse_header_pairs(example), {"Authorization": "Basic abc", "X_Scope_OrgID": "tenant1"})
        for good in ("x-scope-orgid=1", "X.Custom~Name!=v", "a=b,,c=d"):
            with self.subTest(good=good):
                self.assertEqual(fc.problems(tracing_env(OTEL_EXPORTER_OTLP_HEADERS=good)), [])
                self.assertTrue(fc.parse_header_pairs(good))
        for bad in ("a b=c", "a(b)=c", "a:b=c", "a%20b=c", "a=b%0Ac", "a=b%7F", "n" * 65 + "=v", ","):
            with self.subTest(bad=bad):
                self.assertEqual(fc.problems(tracing_env(OTEL_EXPORTER_OTLP_HEADERS=bad)), [fc.ERR_T4])
                self.assertFalse(fc.parse_header_pairs(bad))

    def test_refused_otel_variables(self):
        env = tracing_env(OTEL_SERVICE_NAME="x", OTEL_RESOURCE_ATTRIBUTES="a=b", OTEL_PYTHON_LOG_CORRELATION="true")
        lines = fc.problems(env)
        self.assertEqual(lines, [fc.ERR_T5.format(name="OTEL_RESOURCE_ATTRIBUTES"),
                                 fc.ERR_T5.format(name="OTEL_SERVICE_NAME"),
                                 fc.ERR_T5.format(name="OTEL_PYTHON_LOG_CORRELATION")])
        # Off: not refused (nothing reads them).
        self.assertEqual(fc.problems({"OTEL_SERVICE_NAME": "x"}), [])

    def test_every_otlp_exporter_variable_but_endpoint_and_headers_is_refused(self):
        names = ("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
                 "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_EXPORTER_OTLP_TIMEOUT",
                 "OTEL_EXPORTER_OTLP_CERTIFICATE")
        env = tracing_env(OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic%20abc", **{n: "x" for n in names})
        self.assertEqual(fc.problems(env), [fc.ERR_T5.format(name=n) for n in sorted(names)])
        self.assertEqual(fc.problems(tracing_env(OTEL_EXPORTER_OTLP_HEADERS="a=b")), [])
        lines = fc.problems(tracing_env(**{"OTEL_EXPORTER_OTLP_\x1b": "x"}))
        self.assertEqual(lines, [fc.ERR_T5.format(name="OTEL_EXPORTER_OTLP_*")])

    def test_a_refused_name_that_is_not_printable_is_not_shown(self):
        lines = fc.problems(tracing_env(**{"OTEL_PYTHON_\x1b[31m": "x"}))
        self.assertEqual(lines, [fc.ERR_T5.format(name="OTEL_PYTHON_*")])

    def test_sdk_disabled_is_a_hard_off(self):
        cfg = fc.load(tracing_env(OTEL_SDK_DISABLED="true"))
        self.assertFalse(cfg.tracing)
        self.assertIsNone(cfg.otlp_endpoint)
        self.assertIn(fc.NOTE_SDK_DISABLED, cfg.notes)
        self.assertEqual(fc.problems(tracing_env(OTEL_SDK_DISABLED="maybe")), [fc.ERR_T6])
        self.assertTrue(fc.load(tracing_env(OTEL_SDK_DISABLED="false")).tracing)


class History(unittest.TestCase):
    def test_on_with_a_database(self):
        env = history_env()
        self.assertEqual(fc.problems(env), [])
        cfg = fc.load(env)
        self.assertTrue(cfg.history and cfg.database_url_set and cfg.history_token_key_set)
        self.assertEqual(cfg.database_url, env["DATABASE_URL"])
        self.assertEqual(len(cfg.history_token_key), 32)
        self.assertEqual(cfg.summary(), "history search on")

    def test_secrets_never_in_repr(self):
        env = history_env(**tracing_env(OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic%20" + "Q" * 40))
        cfg = fc.load(env)
        text = repr(cfg)
        for value in (env["DATABASE_URL"], env["HISTORY_TOKEN_KEY"], "Q" * 40):
            self.assertNotIn(value, text)

    def test_database_required(self):
        self.assertEqual(fc.problems(history_env(DATABASE_URL=None)), [fc.ERR_H2])

    def test_database_url_shape_never_echoed(self):
        password = "TEST-ONLY-" + secrets.token_urlsafe(12)
        for bad in (f"mysql://u:{password}@h/db", f"postgresql://u:{password}@/db", f"postgresql://u:{password}@h",
                    f"postgresql://u:{password}@h:99999/db", f"postgresql://u:{password} @h/db", password):
            with self.subTest(bad=bad.replace(password, "<pw>")):
                lines = fc.problems(history_env(DATABASE_URL=bad))
                self.assertEqual(lines, [fc.ERR_H3])
                self.assertNotIn(password, "".join(lines))
                self.assertFalse(fc.load(history_env(DATABASE_URL=bad)).database_url_set)
        self.assertEqual(fc.problems(history_env(DATABASE_URL="postgres://u:p@h/db")), [])

    def test_roles(self):
        self.assertEqual(fc.load(history_env(ROLE="worker")).role, "worker")
        self.assertEqual(fc.load(history_env(ROLE=" ENQUEUE ")).role, "enqueue")
        self.assertEqual(fc.problems(history_env(ROLE="scheduler")), [fc.ERR_H4])
        self.assertEqual(fc.problems({"ROLE": "worker"}), [fc.ERR_H5.format(role="worker")])
        self.assertEqual(fc.problems({"ROLE": "enqueue"}), [fc.ERR_H5.format(role="enqueue")])

    def test_token_key_needed_in_full_mode_only(self):
        self.assertEqual(fc.problems(history_env(HISTORY_TOKEN_KEY=None)), [fc.ERR_H8])
        self.assertEqual(fc.problems(history_env(HISTORY_TOKEN_KEY=None, ROLE="worker")), [fc.ERR_H8])
        self.assertEqual(fc.problems(history_env(HISTORY_TOKEN_KEY=None, ROLE="enqueue")), [])
        self.assertEqual(fc.problems(history_env(HISTORY_TOKEN_KEY=None, **KEY)), [])

    def test_token_key_shape(self):
        raw = secrets.token_bytes(32)
        for good in (base64.urlsafe_b64encode(raw).decode(), base64.b64encode(raw).decode(),
                     base64.urlsafe_b64encode(raw).decode().rstrip("=")):
            with self.subTest(good=good[:4]):
                self.assertEqual(fc.problems(history_env(HISTORY_TOKEN_KEY=good)), [])
                self.assertEqual(fc.load(history_env(HISTORY_TOKEN_KEY=good)).history_token_key, raw)
        for bad in ("short", base64.b64encode(secrets.token_bytes(16)).decode(), "!" * 44):
            with self.subTest(bad=bad[:4]):
                lines = fc.problems(history_env(HISTORY_TOKEN_KEY=bad))
                self.assertEqual(lines, [fc.ERR_H7])
                self.assertNotIn(bad, "".join(lines))

    def test_a_weak_token_key_is_refused_without_showing_it(self):
        for raw in (bytes(32), b"a" * 32, bytes(range(8)) * 4, bytes(range(15)) + bytes(17)):
            bad = base64.b64encode(raw).decode()
            with self.subTest(distinct=len(set(raw))):
                lines = fc.problems(history_env(HISTORY_TOKEN_KEY=bad))
                self.assertEqual(lines, [fc.ERR_H12])
                self.assertNotIn(bad, "".join(lines))
                self.assertIsNone(fc.load(history_env(HISTORY_TOKEN_KEY=bad)).history_token_key)
        good = bytes(range(16)) + bytes(16)      # 16 different byte values: the lowest accepted
        self.assertEqual(fc.problems(history_env(HISTORY_TOKEN_KEY=base64.b64encode(good).decode())), [])

    def test_typed_counting_and_repeated_keys_are_refused(self):
        """Review fix S3: a typed phrase, the bytes 0..31 and a repeated block passed the 16-distinct-bytes test.
        The rule (weak_key) is the one worker_garmin.parse_token_key uses."""
        block = bytes.fromhex("9f3c51e2a87d06b4c2e95f1a3d7b08c6")
        for raw in (b"0123456789abcdefghijklmnopqrstuv", b"my-secret-password-is-hunter2!!!", bytes(range(32)),
                    block * 2):
            bad = base64.b64encode(raw).decode()
            with self.subTest(raw=raw[:4]):
                self.assertTrue(fc.weak_key(raw))
                lines = fc.problems(history_env(HISTORY_TOKEN_KEY=bad))
                self.assertEqual(lines, [fc.ERR_H12])
                self.assertNotIn(bad, "".join(lines))
                self.assertIsNone(fc.load(history_env(HISTORY_TOKEN_KEY=bad)).history_token_key)
        for _ in range(50):
            raw = secrets.token_bytes(32)
            self.assertFalse(fc.weak_key(raw))
            self.assertEqual(fc.load(history_env(HISTORY_TOKEN_KEY=base64.b64encode(raw).decode())).history_token_key,
                             raw)

    def test_forget(self):
        self.assertTrue(fc.load(history_env(HISTORY_FORGET="yes")).history_forget)
        self.assertTrue(fc.load(history_env(HISTORY_FORGET=" YES ")).history_forget)
        self.assertEqual(fc.problems(history_env(HISTORY_FORGET="true")), [fc.ERR_H6])
        # With history off it still forgets, if there is a database.
        env = {"HISTORY_FORGET": "yes", "DATABASE_URL": db_url()}
        self.assertEqual(fc.problems(env), [])
        cfg = fc.load(env)
        self.assertTrue(cfg.history_forget and cfg.database_url_set and not cfg.history)
        self.assertIn(fc.NOTE_HISTORY_OFF_DATABASE, cfg.notes)
        self.assertEqual(fc.problems({"HISTORY_FORGET": "yes"}), [fc.ERR_H10])
        self.assertEqual(cfg.summary(), "HISTORY_FORGET=yes")

    def test_worker_concurrency(self):
        self.assertEqual(fc.load(history_env()).worker_concurrency, 1)
        self.assertEqual(fc.load(history_env(WORKER_CONCURRENCY="4")).worker_concurrency, 4)
        for bad in ("0", "5", "-1", "two", "1.5"):
            with self.subTest(bad=bad):
                self.assertEqual(fc.problems(history_env(WORKER_CONCURRENCY=bad)), [fc.ERR_H9])

    def test_every_line_in_order(self):
        env = {"TRACING": "on", "HISTORY_SEARCH": "on", "ROLE": "x", "HISTORY_FORGET": "no", **FULL}
        self.assertEqual(fc.problems(env), [fc.ERR_T2, fc.ERR_H4, fc.ERR_H6, fc.ERR_H2, fc.ERR_H8])


class Secrets(unittest.TestCase):
    def test_secret_values_cover_each_form(self):
        password = "TEST-ONLY-pw%2F" + secrets.token_urlsafe(8)
        token = "glc_" + secrets.token_urlsafe(30)
        basic = base64.b64encode(f"123456:{token}".encode()).decode()
        key = fake_key()
        env = history_env(DATABASE_URL=db_url(password), HISTORY_TOKEN_KEY=key,
                          **tracing_env(OTEL_EXPORTER_OTLP_HEADERS=f"Authorization=Basic%20{basic}"))
        values = fc.secret_values(fc.load(env))
        for needed in (env["DATABASE_URL"], password, password.replace("%2F", "/"), basic, f"123456:{token}", token,
                       f"Basic%20{basic}", f"Basic {basic}"):
            with self.subTest(needed=needed[:12]):
                self.assertIn(needed, values)
        self.assertTrue(any(v.rstrip("=") == key.rstrip("=") for v in values))

    def test_nothing_set_has_no_secret(self):
        self.assertEqual(fc.secret_values(fc.load({})), [])

    def test_take_secrets(self):
        env = {"DATABASE_URL": "x", "OTEL_EXPORTER_OTLP_HEADERS": "y", "HISTORY_TOKEN_KEY": "z", "KEEP": "1"}
        fc.take_secrets(env)
        self.assertEqual(env, {"KEEP": "1"})


class NeverRaises(unittest.TestCase):
    def test_odd_mappings(self):
        class Broken(dict):
            def get(self, *a, **k):
                raise RuntimeError("boom")

        self.assertEqual(fc.load(Broken()), fc.FeatureConfig())
        self.assertEqual(fc.problems(Broken()), ["the feature settings could not be checked: RuntimeError"])


if __name__ == "__main__":
    unittest.main()
