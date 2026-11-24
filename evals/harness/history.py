"""History search in the eval server: the per-case `history` fixture (evals/cases/history.toml).

A case with history other than "off" runs the server as with HISTORY_SEARCH=on, but over memory instead of
Postgres: history_tools.HistoryService over history_store.MemoryHistoryStore (the synthetic athlete of
evals/history/athlete.py) and history_retrieval.MemoryDocIndex without the local model, so search_history
takes the keyword channel (the production path when the model is unavailable) and find_similar_weeks the
numbers channel. No database, no network, no thread started.

Fixtures:
- athlete:   all 78 weeks; index complete.
- partial:   only the newest 20 weeks (days and weeks); no backfill floor, so index.complete is false.
- two_weeks: only the newest 2 weeks (a new user: fewer than the 8 weeks find_similar_weeks needs).
- mismatch:  all 78 weeks with meta account_mismatch=yes (another Garmin account logged in).
- off:       the feature is off; the history tools are not registered (the default for every case).

How the tools join the server, for the life of the server only: launch._history_tools_wanted() answers
True, launch.allowed_tools() adds launch.HISTORY_TOOLS (both gates then let them through, as
HISTORY_SEARCH=on does), and history_tools.service_from_config() returns the fixture's service, so the
registration itself is launch's own (_register_history_tools and its adder). The history packages
(config/requirements-history.txt) are needed; without them start_server raises HistoryPackagesMissing.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import importlib.util
import os
import sys
import threading
from unittest import mock

FIXTURES = ("athlete", "partial", "two_weeks", "mismatch", "off")
PARTIAL_WEEKS = 20
NEW_USER_WEEKS = 2
# What the in-memory search needs (history_retrieval's LangChain classes and the numbers channel).
PACKAGES = ("langchain_core", "langchain_classic", "numpy")
MISSING_TEXT = ("history cases need the history packages (pip install --require-hashes -r "
                "config/requirements-history.txt)")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

_CACHE: dict = {}
_LOCK = threading.Lock()


class HistoryPackagesMissing(RuntimeError):
    """A history case started in a Python without the history packages."""


def available() -> bool:
    """True when this Python has the packages the in-memory history search needs."""
    try:
        return all(importlib.util.find_spec(name) is not None for name in PACKAGES)
    except (ImportError, ValueError):
        return False


def _athlete_module():
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    from evals.history import athlete

    return athlete


def _newest(source, weeks: int):
    """A copy of athlete `source` with only its newest `weeks` weeks (their days too) and no backfill floor."""
    import history_store as hs

    store = hs.MemoryHistoryStore()
    first = source.today - dt.timedelta(days=source.today.weekday()) - dt.timedelta(days=7 * weeks)
    fp = source.account_fp
    for row in source.store.days(fp, first, source.today):
        store.upsert_day(fp, row["date"], row["numbers"], row["final"])
    for row in source.store.weeks(fp, first, source.today):
        store.upsert_week(fp, row["start"], row["numbers"], row["final"])
    store.meta_set(hs.META_ACCOUNT, fp)
    return dataclasses.replace(source, store=store, week0=first)


def fixture(name: str):
    """(athlete, document index) for fixture `name`, built once per process (the tools only read them)."""
    if name not in FIXTURES or name == "off":
        raise ValueError(f"history fixture must be one of {', '.join(FIXTURES[:-1])}, got {name!r}")
    with _LOCK:
        if name not in _CACHE:
            import history_retrieval as hr
            import history_store as hs

            A = _athlete_module()
            if name == "partial":
                ath = _newest(A.build(), PARTIAL_WEEKS)
            elif name == "two_weeks":
                ath = _newest(A.build(), NEW_USER_WEEKS)
            else:
                ath = A.build()
                if name == "mismatch":
                    ath.store.meta_set(hs.META_MISMATCH, "yes")
            index = hr.MemoryDocIndex(None, ath.account_fp)
            index.add(A.documents(ath))
            _CACHE[name] = (ath, index)
        return _CACHE[name]


def build_service(name: str, today: dt.date, key_mode: bool):
    """history_tools.HistoryService for fixture `name` with the server's frozen `today` and mode, and the
    shipped channel settings (search_history drops the vector channel without a model: keyword search)."""
    import history_tools as ht

    ath, index = fixture(name)
    return ht.HistoryService(ath.store, lambda fp: index, today=lambda: today, key_mode=key_mode)


def install(stack, launch, name: str, today: dt.date, key_mode: bool) -> None:
    """Register the history tools over fixture `name` in the next launch.main() (patches undone by `stack`).
    `launch` is ext/launch.py, already imported. Raises HistoryPackagesMissing."""
    if not available():
        raise HistoryPackagesMissing(MISSING_TEXT)
    import history_tools as ht

    service = build_service(name, today, key_mode)
    real_allowed = launch.allowed_tools

    def allowed_tools(environ):
        return frozenset(real_allowed(environ)) | launch.HISTORY_TOOLS

    stack.enter_context(mock.patch.object(launch, "allowed_tools", allowed_tools))
    stack.enter_context(mock.patch.object(launch, "_history_tools_wanted", lambda features: True))
    stack.enter_context(mock.patch.object(ht, "service_from_config", lambda cfg: service))
