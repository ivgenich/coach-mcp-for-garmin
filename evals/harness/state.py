"""Fake Garmin states: load a base file and its overlays, merge them, check the result.

A state file (evals/fixtures/base/<name>.json, evals/fixtures/overlays/**, evals/fixtures/variants/**) is

    {"responses": {"<garminconnect.Garmin method>": {"<first positional argument as a string, or *>": <answer>}},
     "faults": {"<method name or tool name>": "<group>"},
     "note": "<what this file is for>"}

Merge order: the base, then each overlay in order. responses[method][arg] is merged deeply: a JSON object in an
overlay is merged key by key into the one before it (a nested object too), anything else (a list, a number, a
string, null) replaces it. {"$replace": <answer>} replaces an object whole instead. faults replace by name; the
`faults` argument of load_state() comes last.

A fault on a Garmin write (evals/garmin_writes.WRITE_METHODS: upload_workout, schedule_workout,
unschedule_workout, delete_workout) takes a write group (WRITE_FAULT_GROUPS), optionally "@N": the first N such
writes succeed and every later one fails ("upload_workout": "rate_limited@1"). See fake_garmin.write_exception for
what each group raises. Standard library only.
"""

from __future__ import annotations

import copy
import json
import os

from ..garmin_writes import WRITE_METHODS

HERE = os.path.dirname(os.path.abspath(__file__))
EVALS = os.path.dirname(HERE)
FIXTURES = os.path.join(EVALS, "fixtures")
BASE_DIR = os.path.join(FIXTURES, "base")
# Where a bare overlay name is looked up, in this order.
OVERLAY_DIRS = (os.path.join(FIXTURES, "overlays"), os.path.join(FIXTURES, "variants"))

FAULT_GROUPS = ("login", "rate_limited", "no_data", "unavailable", "internal", "bad_input")
# Faults on a Garmin write: 429, a 5xx that did not land, a connect timeout (never sent), a read timeout after
# the write landed, a lost login, a bug.
WRITE_FAULT_GROUPS = ("rate_limited", "server_error", "connect_timeout", "read_timeout", "login", "internal")
REPLACE = "$replace"
FAULT_KEY = "$fault"
BYTES_KEY = "$base64"


class StateError(ValueError):
    """A state file or a fault map that cannot be used; the message names the file and the problem."""


def _resolve(name, dirs, kind):
    """The path of state file `name`: a path ending in .json (absolute, or relative to evals/fixtures), or a bare
    name looked up as <dir>/<name>.json in `dirs` in order."""
    if not isinstance(name, str) or not name.strip():
        raise StateError(f"{kind}: a state file name must be a non-empty string, got {name!r}")
    if name.endswith(".json"):
        path = name if os.path.isabs(name) else os.path.join(FIXTURES, name)
        if os.path.isfile(path):
            return path
        raise StateError(f"{kind} {name!r}: no such file {path}")
    for folder in dirs:
        path = os.path.join(folder, name + ".json")
        if os.path.isfile(path):
            return path
    where = ", ".join(os.path.relpath(d, EVALS) for d in dirs)
    raise StateError(f"{kind} {name!r}: no {name}.json in {where}")


def read_state_file(path):
    """The checked content of one state file: {"responses", "faults", "note"}."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise StateError(f"{path}: cannot read: {exc}") from exc
    label = os.path.relpath(path, EVALS) if path.startswith(EVALS) else path
    if not isinstance(data, dict):
        raise StateError(f"{label}: the top level must be a JSON object")
    unknown = set(data) - {"responses", "faults", "note", "anchor"}
    if unknown:
        raise StateError(f"{label}: unknown top-level keys {sorted(unknown)}")
    responses = data.get("responses", {})
    faults = data.get("faults", {})
    check_responses(responses, label)
    check_faults(faults, label)
    return {"responses": responses, "faults": faults, "note": data.get("note", "")}


def check_responses(responses, label):
    from . import fake_garmin  # the method table lives with the fake

    if not isinstance(responses, dict):
        raise StateError(f"{label}: responses must be an object")
    for method, answers in responses.items():
        if method not in fake_garmin.METHODS:
            raise StateError(f"{label}: responses names {method!r}, which no tool of this server calls "
                             "(see evals/harness/README.md for the method list)")
        if not isinstance(answers, dict):
            raise StateError(f"{label}: responses[{method!r}] must be an object keyed by argument")
        for arg, answer in answers.items():
            if isinstance(answer, dict) and FAULT_KEY in answer and answer[FAULT_KEY] not in FAULT_GROUPS:
                raise StateError(f"{label}: responses[{method!r}][{arg!r}] has an unknown {FAULT_KEY} "
                                 f"{answer[FAULT_KEY]!r}; one of {', '.join(FAULT_GROUPS)}")


def parse_write_fault(value):
    """(group, after) of a write fault "<group>" or "<group>@<N>"; StateError when it is neither."""
    text = value if isinstance(value, str) else ""
    group, at, after = text.partition("@")
    if group not in WRITE_FAULT_GROUPS or (at and not (after.isdigit() and after.isascii())):
        raise StateError(f"write fault {value!r}: one of {', '.join(WRITE_FAULT_GROUPS)}, optionally @N")
    return group, int(after or 0)


def check_faults(faults, label):
    if not isinstance(faults, dict):
        raise StateError(f"{label}: faults must be an object")
    for name, group in faults.items():
        if name in WRITE_METHODS:
            try:
                parse_write_fault(group)
            except StateError as exc:
                raise StateError(f"{label}: fault {name!r}: {exc}") from None
            continue
        if group not in FAULT_GROUPS:
            raise StateError(f"{label}: fault {name!r} -> {group!r} is no group; one of {', '.join(FAULT_GROUPS)}")


def deep_merge(old, new):
    """`new` laid over `old` (neither changed): objects merge key by key, {"$replace": x} gives x, else `new`."""
    if isinstance(new, dict) and set(new) == {REPLACE}:
        return copy.deepcopy(new[REPLACE])
    if isinstance(old, dict) and isinstance(new, dict):
        out = copy.deepcopy(old)
        for key, value in new.items():
            out[key] = deep_merge(old[key], value) if key in old else _strip_replace(value)
        return out
    return _strip_replace(new)


def _strip_replace(value):
    if isinstance(value, dict):
        if set(value) == {REPLACE}:
            return copy.deepcopy(value[REPLACE])
        return {k: _strip_replace(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_replace(v) for v in value]
    return value


def merge_states(states):
    """One state from file contents in order (see the module docstring)."""
    responses, faults, notes = {}, {}, []
    for state in states:
        for method, answers in state.get("responses", {}).items():
            merged = responses.setdefault(method, {})
            for arg, answer in answers.items():
                merged[arg] = deep_merge(merged[arg], answer) if arg in merged else _strip_replace(answer)
        faults.update(state.get("faults", {}))
        if state.get("note"):
            notes.append(state["note"])
    return {"responses": responses, "faults": faults, "notes": notes}


def load_state(base, overlays=(), faults=None):
    """The merged state of base file `base` (evals/fixtures/base/<base>.json), the overlays in order (a bare
    name is looked up in evals/fixtures/overlays/ then evals/fixtures/variants/; a name ending .json is a path
    relative to evals/fixtures/) and then `faults` ({method or tool name: group}).

    Returns {"responses", "faults", "notes", "files"}; raises StateError on a missing file, an unknown method,
    or an unknown fault group."""
    files = [_resolve(base, (BASE_DIR,), "base")]
    files += [_resolve(name, OVERLAY_DIRS, "overlay") for name in (overlays or ())]
    states = [read_state_file(path) for path in files]
    extra = dict(faults or {})
    check_faults(extra, "faults argument")
    states.append({"faults": extra})
    merged = merge_states(states)
    merged["files"] = [os.path.relpath(path, EVALS) for path in files]
    return merged


def empty_state():
    """A state with no answers and no faults (every method answers Garmin's "no data")."""
    return {"responses": {}, "faults": {}, "notes": [], "files": []}
