"""The optional features' startup pieces that launch.py runs in the app (feature_config; both features off by
default, and then nothing here runs).

launch._setup_features() calls these in its order: the call guard (build_guard), the history forget
(forget_history), and the background threads: the catch-up (start_catch_up), the token publisher for the
worker (start_token_publisher) and the cleanup of a token a full-mode history setup left behind
(start_token_cleanup). run_role() runs worker.py or enqueue.py instead of the server. Nothing here reads
launch.py's module state: launch passes the call guard, the telemetry module and the refresh hook in, so a
test that sets launch._GUARD or patches launch._start_catch_up sees the same behaviour as before the split.

The features' modules are imported only when a feature is on, so the server starts without their packages.
Log lines go to the garmin_ext.launch logger, as they did inside launch.py.
"""

from __future__ import annotations

import datetime
import importlib
import importlib.util
import logging
import sys
import threading

import garmin_guard
import status_common

log = logging.getLogger("garmin_ext.launch")


def has_module(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # a missing parent package
        return False


def optional(name):
    """Import one of the features' modules (a sibling in ext/), or None with one log line naming it."""
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001 - a feature's module never stops the server
        log.error("feature: cannot import %s (%s); the feature stays off", name, type(exc).__name__)
        return None


def history_tools_wanted(features):
    """True when the history tools are registered: HISTORY_SEARCH=on, ROLE=app, not while forgetting."""
    return features.history and features.role == "app" and not features.history_forget


def register_history_tools(app, features):
    """history_tools.register(adder, features); adder(fn, name=None, description=None, **kwargs) registers
    one tool through upstream's filter and the read-only gate, like this repo's other tools. A failure
    leaves the tools out and the server up."""
    module = optional("history_tools")
    if module is None:
        return

    def adder(fn, name=None, description=None, **kwargs):
        if name is not None:
            kwargs["name"] = name
        if description is not None:
            kwargs["description"] = description
        return app.tool(**kwargs)(fn)

    try:
        module.register(adder, features)
    except Exception as exc:  # noqa: BLE001
        log.error("history search: the tools could not be registered (%s)", type(exc).__name__)


def build_guard(features, environ):
    """The process's GarminGuard: on the Postgres shared state (garmin_guard_pg.PgSharedState, live=True: the
    live path answers from memory and writes in the background) with HISTORY_SEARCH=on, else in memory. A
    shared state that cannot be built falls back to memory with one log line."""
    zone = (environ.get("REPORT_TZ") or "").strip() or None
    state = None
    if features.history and features.database_url_set:
        module = optional("garmin_guard_pg")
        if module is not None:
            try:
                state = module.PgSharedState(features.database_url, status_common.zone(zone), live=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("call guard: the shared state could not be opened (%s); this process's copy is used",
                            type(exc).__name__)
                state = None
    # Its table is created by the history startup thread (start_catch_up), after history_store's migrations.
    return garmin_guard.GarminGuard(state, zone=zone)


def forget_history(features):
    """HISTORY_FORGET=yes: delete the stored history before anything uses the database: history_store's
    forget_all() (the schema with the days, weeks, jobs and vectors), then history_retrieval.forget() and
    history_embed.forget_caches() where those modules are in the image. Logs what each step removed (fixed
    texts), then how to finish. Never raises: a failed step is one ERROR line, and True only when every step
    worked."""
    removed, ok = [], True
    steps = (("history_store", lambda m: m.PgHistoryStore(features.database_url).forget_all()),
             ("history_retrieval", lambda m: m.forget(features)),
             ("history_embed", lambda m: m.forget_caches()))
    for module_name, step in steps:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            if module_name == "history_store":
                log.error("history forget: FAILED (the history modules are not in this image); build it with "
                          "HISTORY=1")
                return False
            continue
        try:
            removed.append(removed_text(step(module), module_name))
        except Exception as exc:  # noqa: BLE001
            ok = False
            log.error("history forget: FAILED in %s (%s); check DATABASE_URL and the Postgres service, then "
                      "redeploy with HISTORY_FORGET=yes still set", module_name, type(exc).__name__)
            if module_name == "history_store":
                return False
    log.warning("history forget: removed %s", "; ".join(r for r in removed if r) or "nothing (already empty)")
    if ok:
        log.warning("history forget: done. Now delete HISTORY_FORGET from the app and the worker; with "
                    "HISTORY_SEARCH=on the history is built again from Garmin. Postgres backups may still hold "
                    "copies: check your Railway backup settings (docs/history-search.md#forgetting)")
    return ok


def removed_text(result, default):
    """A log text for what a forget step returned: names (str, list of str, dict keys) or `default`."""
    if isinstance(result, str):
        return result[:200]
    if isinstance(result, dict):
        return ", ".join(f"{k}: {v}" for k, v in sorted(result.items()) if isinstance(v, (int, str)))[:400]
    if isinstance(result, (list, tuple)):
        return ", ".join(str(x) for x in result if isinstance(x, (str, int)))[:400]
    return default


def start_catch_up(features, guard, telemetry):
    """With history search on, the app's history startup, in a background thread so the server starts at
    once: the migrations in a fixed order (history_store's, then the shared guard state's of `guard`, then
    the job queue's), then the catch-up jobs (the days missing from the index, enqueue.catch_up(), which gets
    `telemetry`) and history_jobs.scheduled_jobs() (the sweep, a rebuild after a version change). A failure
    is one WARNING line (its type only); the daily cron run enqueues the jobs later anyway."""
    guard_state = getattr(guard, "_state", None)

    def run():
        try:
            import enqueue
            import history_store
            import jobqueue

            store = history_store.PgHistoryStore(features.database_url)
            store.migrate()
            migrate_guard = getattr(guard_state, "migrate", None)
            if migrate_guard is not None:
                migrate_guard()
            queue = jobqueue.PgJobQueue(features.database_url, application_name="coach-mcp-app")
            queue.migrate()
            today = datetime.datetime.now(status_common.zone()).date()
            out = enqueue.catch_up(queue, today, store=store, telemetry=telemetry)
            added = out.get("added") if isinstance(out, dict) else None
            log.info("history search: startup catch-up enqueued %s jobs", added if isinstance(added, int) else "the")
            try:
                import history_jobs

                for kind, key, payload in history_jobs.scheduled_jobs(store, today):
                    queue.enqueue(kind, key, payload)
            except ImportError:
                pass
        except Exception as exc:  # noqa: BLE001
            log.warning("history search: the startup catch-up could not be enqueued (%s)", type(exc).__name__)

    thread = threading.Thread(target=run, name="history-catch-up", daemon=True)
    thread.start()
    return thread


def run_role(features):
    """ROLE=worker or enqueue: run worker.main() or enqueue.main() instead of the server; its exit code."""
    name = "worker" if features.role == "worker" else "enqueue"
    try:
        module = importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        print(f"launch: ROLE={features.role} cannot start: {name}.py could not be imported "
              f"({type(exc).__name__}); build the image with HISTORY=1", file=sys.stderr)
        return 1
    setup_logging = getattr(module, "configure_logging", None)
    if callable(setup_logging):
        setup_logging()      # as when worker.py / enqueue.py run as a script: the library records hidden
    code = module.main()
    return code if isinstance(code, int) else 0


def app_garmin_client():
    """The app's logged-in garminconnect Client behind courses.garmin_client (upstream's _GarminProxy around
    the pending-login holder around garminconnect.Garmin), or None while login is pending or failed. Walks
    the wrappers through their instance dicts, so it never blocks on the login wait in __getattr__."""
    try:
        from garmin_mcp import courses
    except Exception:  # noqa: BLE001
        return None
    obj = getattr(courses, "garmin_client", None)
    for _ in range(5):
        if obj is None:
            return None
        if callable(getattr(type(obj), "_token_expires_soon", None)) and \
                callable(getattr(type(obj), "_refresh_session", None)):
            return obj
        inner = vars(obj) if hasattr(obj, "__dict__") else {}
        obj = inner.get("_client", inner.get("client"))
    return None


def start_token_publisher(features, environ, refresh):
    """Full mode with history search on: the worker cannot read this service's /data volume, so the app
    copies its Garmin token file to Postgres, sealed with HISTORY_TOKEN_KEY, whenever it changes
    (worker_garmin.TokenPublisher, every few minutes in a daemon thread, with `refresh` as its refresh hook).
    The app stays the only one that refreshes the login. A failure is one WARNING line; the worker then waits
    ("token_stale")."""
    try:
        import worker_garmin

        store = worker_garmin.PgTokenStore(features.database_url, features.history_token_key)
        publisher = worker_garmin.TokenPublisher(store, (environ.get("GARMINTOKENS") or "/data/tokens").strip(),
                                                 refresh=refresh)

        def run():
            try:
                store.migrate()
            except Exception as exc:  # noqa: BLE001
                log.warning("history token publish: the table could not be created (%s)", type(exc).__name__)
            publisher.start()

        threading.Thread(target=run, name="history-token-setup", daemon=True).start()
        return publisher
    except Exception as exc:  # noqa: BLE001
        log.warning("history token publish: could not start (%s)", type(exc).__name__)
        return None


TOKEN_CLEANUP_HISTORY_OFF = "history search is off"
TOKEN_CLEANUP_KEY_MODE = "history search runs in key mode"
TOKEN_CLEANUP_NO_DRIVER = ("history search is off: a Garmin token stored by an earlier full-mode history setup "
                           "could not be checked or deleted, because this image has no database driver (psycopg); "
                           "build it with HISTORY=1 once, or delete the history.garmin_token table by hand")


def start_token_cleanup(features, reason=TOKEN_CLEANUP_HISTORY_OFF, *, has_module=has_module):
    """The app started with HISTORY_SEARCH off (or on in key mode) and DATABASE_URL set: delete the sealed
    Garmin token a full-mode history setup left in Postgres (worker_garmin.delete_stored_token, which never
    raises), in a daemon thread so the server starts at once. One fixed WARNING line when a row was deleted
    ("<reason>: the stored Garmin token was deleted from the database"). An image without the database driver
    (`has_module("psycopg")` false) logs one fixed WARNING line (TOKEN_CLEANUP_NO_DRIVER); a build whose
    worker_garmin has no delete_stored_token skips it without a word."""
    if not has_module("psycopg"):
        log.warning("%s", TOKEN_CLEANUP_NO_DRIVER)
        return None
    try:
        import worker_garmin
    except Exception:  # noqa: BLE001 - not in this image
        return None
    delete = getattr(worker_garmin, "delete_stored_token", None)
    if not callable(delete):
        return None
    dsn = features.database_url

    def run():
        try:
            if delete(dsn):
                log.warning("%s: the stored Garmin token was deleted from the database", reason)
        except Exception:  # noqa: BLE001 - it never raises; nothing may stop the server
            pass

    thread = threading.Thread(target=run, name="history-token-cleanup", daemon=True)
    thread.start()
    return thread
