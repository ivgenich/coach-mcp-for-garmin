"""Load and check the eval cases in evals/cases/*.toml.

One TOML file per group (the file name is the group). Each file may start with a `[defaults]` table (mode, now,
tz, variants, trials, garmin) and then holds `[[case]]` tables, each with `[case.expect]` and `[case.garmin]`.
load_cases() returns frozen Case objects in file order and refuses anything it does not know: an unknown key, a
tool name the server does not expose in the case's mode (config/enabled-tools.txt, config/enabled-tools-key.txt),
an unknown judge rubric or error group, a duplicate id, a fixture file that does not exist or does not have the
state format of evals/harness (responses / faults / note).

Optional fields beyond the shared contract (all default to "not used"):
- Case.followups: more user turns, sent one after the other once the model has answered the previous turn.
- Case.prompt_file / report_url: the prompt is the "## Prompt" section of that file (the morning-report recipe),
  with {{REPORT_URL}} replaced by report_url. Case.prompt then holds the resolved text.
- Case.extra_tools: client-side tools the runner fakes next to the server's (ArtifactData, ToolSearch).
- Overlay names are bare (no folder, no .json), as evals.harness.load_state() takes them; an answer may be
  {"$replace": <answer>} to replace an object whole instead of the harness's key-by-key merge.
- Case.db_seed: documents the fake ArtifactData starts with. Each is {collection, doc_id} plus either `data` (the
  document as given) or `status_of` (overlay names on the case's base: the document is that state's
  get_daily_status JSON without sources/thresholds/thresholds_athlete, plus summary_en and stored_at).
- Expect.must_mention / must_not_mention items may be lists: any one of those phrases (evals.grading.checks).
- Expect.tool_output_has / tool_output_lacks: strings the tool results of the case's own state must (not) contain
  when the expected tools run without a model (a check of the fixtures, never of the answer).
- Expect.db_result, db_collections, db_unchanged, db_no_delete: the fake ArtifactData's state after the run
  (runs/latest result; the only collections written; "collection/doc_id" documents left as they were; no delete).

Standard library only.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import re
import tomllib
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES_DIR = os.path.join(REPO, "evals", "cases")
FIXTURES_DIR = os.path.join(REPO, "evals", "fixtures")
TOOLS_FILE = os.path.join(REPO, "config", "enabled-tools.txt")
KEY_TOOLS_FILE = os.path.join(REPO, "config", "enabled-tools-key.txt")

GROUPS = ("tool_choice", "faithfulness", "failure_texts", "key_mode", "safety", "efficiency", "morning_report")
# Groups whose every case must pass on every trial (a failure text ignored once is a failure).
STRICT_GROUPS = ("failure_texts", "safety")
MODES = ("full", "key")
VARIANTS = ("bare", "instr", "coach")
RUBRICS = ("no_redecide", "missing_not_estimated", "injection_flagged", "error_next_step")
# ext/tool_errors.GROUPS (test_cases checks they are equal).
FAULT_GROUPS = ("login", "rate_limited", "bad_input", "no_data", "unavailable", "internal")
EXTRA_TOOLS = ("ArtifactData", "ToolSearch")
DEFAULT_VARIANTS = ("bare", "instr")
DEFAULT_TRIALS = 3
FIXTURE_FOLDERS = ("overlays", "variants")

ID_RE = re.compile(r"^[A-Z][0-9]{2}$")
NOW_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
METHOD_RE = re.compile(r"^[a-z][a-z0-9_.]*$")
FIXTURE_RE = re.compile(r"^[a-z0-9_]+$")
PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}")

CASE_KEYS = frozenset({
    "id", "group", "title", "prompt", "mode", "now", "tz", "variants", "garmin", "smoke", "trials",
    "all_trials_must_pass", "why", "expect", "followups", "prompt_file", "report_url", "extra_tools", "db_seed",
    "max_turns"})
DEFAULT_KEYS = frozenset({"mode", "now", "tz", "variants", "trials", "garmin", "all_trials_must_pass", "prompt_file",
                          "report_url", "extra_tools", "max_turns"})
GARMIN_KEYS = frozenset({"base", "overlays", "faults"})
SEED_KEYS = frozenset({"collection", "doc_id", "data", "status_of"})
FILE_KEYS = frozenset({"defaults", "case"})
STATE_KEYS = frozenset({"responses", "faults", "note", "anchor"})


class CaseError(ValueError):
    """A case file that does not load or does not pass validation; the message names the file and the case."""


@dataclasses.dataclass(frozen=True)
class Expect:
    tools_any: tuple = ()
    tools_all: tuple = ()
    tools_none: tuple = ()
    first_tool: str | None = None
    args: dict = dataclasses.field(default_factory=dict)
    max_tool_calls: int | None = None
    no_invented_numbers: bool = False
    must_mention: tuple = ()
    must_not_mention: tuple = ()
    error_group: str | None = None
    no_call_after_error: tuple = ()
    refuse_write: bool = False
    judge: tuple = ()
    tool_output_has: tuple = ()
    tool_output_lacks: tuple = ()
    db_result: str | None = None
    db_collections: tuple = ()
    db_unchanged: tuple = ()
    db_no_delete: bool = False


EXPECT_LISTS = ("tools_any", "tools_all", "tools_none", "must_mention", "must_not_mention", "no_call_after_error",
                "judge", "tool_output_has", "tool_output_lacks", "db_collections", "db_unchanged")
EXPECT_BOOLS = ("no_invented_numbers", "refuse_write", "db_no_delete")
EXPECT_KEYS = frozenset(f.name for f in dataclasses.fields(Expect))


@dataclasses.dataclass(frozen=True)
class Case:
    id: str
    group: str
    title: str
    prompt: str
    mode: str
    now: str
    tz: str
    variants: tuple
    garmin: dict
    smoke: bool
    trials: int
    all_trials_must_pass: bool
    why: str
    expect: Expect
    followups: tuple = ()
    prompt_file: str | None = None
    report_url: str | None = None
    extra_tools: tuple = ()
    db_seed: tuple = ()
    max_turns: int | None = None  # model calls per trial; None = the runner's MAX_TURNS

    @property
    def tools(self) -> frozenset:
        """Every tool name the case may name: the server's tools in its mode plus its extra tools."""
        return tool_names(self.mode) | frozenset(self.extra_tools)


def read_tool_list(path: str) -> frozenset:
    """Names in a config/enabled-tools*.txt file: one per line, '#' starts a comment."""
    names = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            name = line.split("#", 1)[0].strip()
            if name:
                names.add(name)
    return frozenset(names)


_TOOL_CACHE: dict = {}


def tool_names(mode: str) -> frozenset:
    """The tools the server exposes in `mode` ("full" or "key")."""
    if mode not in _TOOL_CACHE:
        _TOOL_CACHE[mode] = read_tool_list(TOOLS_FILE if mode == "full" else KEY_TOOLS_FILE)
    return _TOOL_CACHE[mode]


def fixture_path(name: str, base: bool = False) -> str:
    """The file of a base set (evals/fixtures/base/<name>.json) or of an overlay name, looked up as the harness
    does: evals/fixtures/overlays/<name>.json, then variants/<name>.json (the overlays path when neither exists)."""
    if base:
        return os.path.join(FIXTURES_DIR, "base", name + ".json")
    for folder in FIXTURE_FOLDERS:
        path = os.path.join(FIXTURES_DIR, folder, name + ".json")
        if os.path.isfile(path):
            return path
    return os.path.join(FIXTURES_DIR, FIXTURE_FOLDERS[0], name + ".json")


def check_state_file(path: str) -> dict:
    """Load one state file and check its shape; raises CaseError."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        raise CaseError(f"fixture file missing: {os.path.relpath(path, REPO)}") from None
    except (OSError, ValueError) as exc:
        raise CaseError(f"fixture file unreadable: {os.path.relpath(path, REPO)}: {exc}") from None
    rel = os.path.relpath(path, REPO)
    if not isinstance(doc, dict) or set(doc) - STATE_KEYS:
        raise CaseError(f"{rel}: a state file holds only responses, faults and note")
    responses = doc.get("responses", {})
    if not isinstance(responses, dict) or not all(
            isinstance(m, str) and METHOD_RE.fullmatch(m) and isinstance(a, dict) for m, a in responses.items()):
        raise CaseError(f"{rel}: responses must be {{method: {{arg: answer}}}}")
    for method, answers in responses.items():
        if not all(isinstance(k, str) and k for k in answers):
            raise CaseError(f"{rel}: responses.{method} keys must be non-empty strings")
    _check_faults(doc.get("faults", {}), rel)
    return doc


def _check_faults(faults: Any, where: str) -> None:
    if not isinstance(faults, dict):
        raise CaseError(f"{where}: faults must be a table")
    for name, group in faults.items():
        if not (isinstance(name, str) and METHOD_RE.fullmatch(name)):
            raise CaseError(f"{where}: fault target {name!r} is not a method or tool name")
        if group not in FAULT_GROUPS:
            raise CaseError(f"{where}: unknown fault group {group!r} (known: {', '.join(FAULT_GROUPS)})")


def recipe_prompt(path: str, report_url: str) -> str:
    """The "## Prompt" section of a recipe file with {{REPORT_URL}} filled in."""
    full = path if os.path.isabs(path) else os.path.join(REPO, path)
    try:
        with open(full, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        raise CaseError(f"prompt_file missing: {path}") from None
    marker = "\n## Prompt\n"
    if marker not in text:
        raise CaseError(f"prompt_file {path} has no '## Prompt' section")
    body = text.split(marker, 1)[1]
    body = re.split(r"\n## ", body, maxsplit=1)[0].strip()
    body = body.replace("{{REPORT_URL}}", report_url)
    left = PLACEHOLDER_RE.search(body)
    if left:
        raise CaseError(f"prompt_file {path}: placeholder {left.group(0)} not filled")
    return body


def _str_list(value: Any, where: str, key: str) -> tuple:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise CaseError(f"{where}: {key} must be a list of non-empty strings")
    if len(set(value)) != len(value):
        raise CaseError(f"{where}: {key} repeats an item")
    return tuple(value)


def _phrase_list(value: Any, where: str, key: str) -> tuple:
    """must_mention / must_not_mention: each item a phrase, or a list of phrases meaning any one of them."""
    if not isinstance(value, list):
        raise CaseError(f"{where}: {key} must be a list")
    out = []
    for item in value:
        if isinstance(item, list):
            out.append(_str_list(item, where, key))
            if not item:
                raise CaseError(f"{where}: {key} has an empty list of alternatives")
        elif isinstance(item, str) and item:
            out.append(item)
        else:
            raise CaseError(f"{where}: {key} items must be phrases or lists of phrases")
    return tuple(out)


def _expect(raw: Any, where: str, tools: frozenset) -> Expect:
    if not isinstance(raw, dict):
        raise CaseError(f"{where}: [case.expect] must be a table")
    unknown = set(raw) - EXPECT_KEYS
    if unknown:
        raise CaseError(f"{where}: unknown expect key(s): {', '.join(sorted(unknown))}")
    kw: dict = {}
    for key in EXPECT_LISTS:
        if key in raw:
            if key in ("must_mention", "must_not_mention"):
                kw[key] = _phrase_list(raw[key], where, key)
            else:
                kw[key] = _str_list(raw[key], where, key)
    for key in EXPECT_BOOLS:
        if key in raw:
            if not isinstance(raw[key], bool):
                raise CaseError(f"{where}: {key} must be true or false")
            kw[key] = raw[key]
    for key in ("first_tool", "error_group", "db_result"):
        if key in raw:
            if not isinstance(raw[key], str) or not raw[key]:
                raise CaseError(f"{where}: {key} must be a non-empty string")
            kw[key] = raw[key]
    if "max_tool_calls" in raw:
        value = raw["max_tool_calls"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CaseError(f"{where}: max_tool_calls must be a whole number >= 0")
        kw["max_tool_calls"] = value
    if "args" in raw:
        args = raw["args"]
        if not isinstance(args, dict) or not all(isinstance(v, dict) and v for v in args.values()):
            raise CaseError(f"{where}: args must be {{tool: {{arg: value}}}}")
        for tool, values in args.items():
            for arg, value in values.items():
                if not isinstance(value, (str, int, float, bool)):
                    raise CaseError(f"{where}: args.{tool}.{arg} must be a string, number or boolean")
        kw["args"] = {tool: dict(values) for tool, values in args.items()}
    exp = Expect(**kw)

    named = set(exp.tools_any) | set(exp.tools_all) | set(exp.tools_none) | set(exp.args)
    if exp.first_tool:
        named.add(exp.first_tool)
    unknown = named - tools
    if unknown:
        raise CaseError(f"{where}: unknown tool name(s) for this mode: {', '.join(sorted(unknown))}")
    clash = (set(exp.tools_all) | set(exp.tools_any) | ({exp.first_tool} - {None})) & set(exp.tools_none)
    if clash:
        raise CaseError(f"{where}: tool(s) both expected and forbidden: {', '.join(sorted(clash))}")
    bad = set(exp.judge) - set(RUBRICS)
    if bad:
        raise CaseError(f"{where}: unknown judge rubric(s): {', '.join(sorted(bad))} (known: {', '.join(RUBRICS)})")
    bad = ({exp.error_group} - {None}) | set(exp.no_call_after_error)
    bad -= set(FAULT_GROUPS)
    if bad:
        raise CaseError(f"{where}: unknown error group(s): {', '.join(sorted(bad))}")
    return exp


def _garmin(raw: Any, where: str, check_files: bool) -> dict:
    if not isinstance(raw, dict):
        raise CaseError(f"{where}: [case.garmin] must be a table")
    unknown = set(raw) - GARMIN_KEYS
    if unknown:
        raise CaseError(f"{where}: unknown garmin key(s): {', '.join(sorted(unknown))}")
    base = raw.get("base")
    if not isinstance(base, str) or not re.fullmatch(r"[a-z0-9_]+", base):
        raise CaseError(f"{where}: garmin.base must name a base set (evals/fixtures/base/<name>.json)")
    overlays = raw.get("overlays", [])
    if not isinstance(overlays, list) or not all(isinstance(o, str) and FIXTURE_RE.fullmatch(o) for o in overlays):
        raise CaseError(f"{where}: garmin.overlays must be bare names (evals/fixtures/overlays|variants/<name>.json)")
    if len(set(overlays)) != len(overlays):
        raise CaseError(f"{where}: garmin.overlays repeats a name")
    faults = raw.get("faults", {})
    _check_faults(faults, where)
    if check_files:
        for name, is_base in [(base, True), *((o, False) for o in overlays)]:
            try:
                check_state_file(fixture_path(name, base=is_base))
            except CaseError as exc:
                raise CaseError(f"{where}: {exc}") from None
    return {"base": base, "overlays": list(overlays), "faults": dict(faults)}


def _seed(raw: Any, where: str, check_files: bool) -> tuple:
    if not isinstance(raw, list):
        raise CaseError(f"{where}: db_seed must be a list of tables")
    out = []
    for item in raw:
        if not isinstance(item, dict) or set(item) - SEED_KEYS:
            raise CaseError(f"{where}: db_seed items hold only collection, doc_id and data or status_of")
        if not all(isinstance(item.get(k), str) and item.get(k) for k in ("collection", "doc_id")):
            raise CaseError(f"{where}: db_seed items need collection and doc_id")
        if ("data" in item) == ("status_of" in item):
            raise CaseError(f"{where}: db_seed item needs exactly one of data or status_of")
        if "data" in item and not isinstance(item["data"], dict):
            raise CaseError(f"{where}: db_seed data must be a table")
        if "status_of" in item:
            names = item["status_of"]
            if not isinstance(names, list) or not all(isinstance(n, str) and FIXTURE_RE.fullmatch(n) for n in names):
                raise CaseError(f"{where}: db_seed status_of must be a list of overlay names")
            if check_files:
                for name in names:
                    try:
                        check_state_file(fixture_path(name))
                    except CaseError as exc:
                        raise CaseError(f"{where}: {exc}") from None
        out.append(dict(item))
    return tuple(out)


def _case(raw: Any, defaults: dict, group: str, where: str, check_files: bool) -> Case:
    if not isinstance(raw, dict):
        raise CaseError(f"{where}: every [[case]] must be a table")
    unknown = set(raw) - CASE_KEYS
    if unknown:
        raise CaseError(f"{where}: unknown key(s): {', '.join(sorted(unknown))}")
    cid = raw.get("id")
    if not isinstance(cid, str) or not ID_RE.fullmatch(cid):
        raise CaseError(f"{where}: id must look like T01 (a capital letter and two digits)")
    where = f"{where}: case {cid}"
    if raw.get("group", group) != group:
        raise CaseError(f"{where}: group {raw.get('group')!r} does not match the file ({group})")
    merged = {**defaults, **raw}
    if "garmin" in defaults and "garmin" in raw:
        merged["garmin"] = {**defaults["garmin"], **raw["garmin"]}

    for key in ("title", "why"):
        if not isinstance(merged.get(key), str) or not merged[key].strip():
            raise CaseError(f"{where}: {key} is required")
    mode = merged.get("mode", "full")
    if mode not in MODES:
        raise CaseError(f"{where}: mode must be full or key")
    now = merged.get("now")
    if not isinstance(now, str) or not NOW_RE.fullmatch(now):
        raise CaseError(f"{where}: now must look like 2026-03-18T06:30")
    try:
        dt.datetime.fromisoformat(now)
    except ValueError:
        raise CaseError(f"{where}: now is not a real date and time") from None
    tz = merged.get("tz")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise CaseError(f"{where}: tz must be an IANA time zone name") from None
    variants = merged.get("variants", list(DEFAULT_VARIANTS))
    if not isinstance(variants, list) or not variants or set(variants) - set(VARIANTS) \
            or len(set(variants)) != len(variants):
        raise CaseError(f"{where}: variants must be a non-empty list of {', '.join(VARIANTS)}")
    max_turns = merged.get("max_turns")
    if max_turns is not None and (isinstance(max_turns, bool) or not isinstance(max_turns, int)
                                  or not 1 <= max_turns <= 30):
        raise CaseError(f"{where}: max_turns must be a whole number from 1 to 30")
    trials = merged.get("trials", DEFAULT_TRIALS)
    if isinstance(trials, bool) or not isinstance(trials, int) or trials < 1:
        raise CaseError(f"{where}: trials must be a whole number >= 1")
    for key in ("smoke", "all_trials_must_pass"):
        if not isinstance(merged.get(key, False), bool):
            raise CaseError(f"{where}: {key} must be true or false")
    strict = merged.get("all_trials_must_pass", False)
    if group in STRICT_GROUPS and not strict:
        raise CaseError(f"{where}: {group} cases need all_trials_must_pass = true")

    extra = _str_list(merged.get("extra_tools", []), where, "extra_tools")
    bad = set(extra) - set(EXTRA_TOOLS)
    if bad:
        raise CaseError(f"{where}: unknown extra tool(s): {', '.join(sorted(bad))}")
    followups = _str_list(merged.get("followups", []), where, "followups")

    prompt_file, report_url = merged.get("prompt_file"), merged.get("report_url")
    if prompt_file is not None:
        if "prompt" in merged:
            raise CaseError(f"{where}: give prompt or prompt_file, not both")
        if not isinstance(report_url, str) or not report_url.startswith("https://"):
            raise CaseError(f"{where}: prompt_file needs report_url (an https link)")
        prompt = recipe_prompt(prompt_file, report_url)
    else:
        if report_url is not None:
            raise CaseError(f"{where}: report_url is only used with prompt_file")
        prompt = merged.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise CaseError(f"{where}: prompt is required")

    if "garmin" not in merged:
        raise CaseError(f"{where}: [case.garmin] with a base is required")
    garmin = _garmin(merged["garmin"], where, check_files)
    seed = _seed(merged.get("db_seed", []), where, check_files)
    if seed and "ArtifactData" not in extra:
        raise CaseError(f"{where}: db_seed needs ArtifactData in extra_tools")
    tools = tool_names(mode) | frozenset(extra)
    exp = _expect(merged.get("expect", {}), where, tools)
    for target in garmin["faults"]:
        if target in tool_names("full") and target not in tools:
            raise CaseError(f"{where}: fault on {target}, a tool this mode does not have")
    if (exp.db_result or exp.db_collections or exp.db_unchanged or exp.db_no_delete) and "ArtifactData" not in extra:
        raise CaseError(f"{where}: db_* expectations need ArtifactData in extra_tools")
    return Case(id=cid, group=group, title=merged["title"].strip(), prompt=prompt.strip(), mode=mode, now=now,
                tz=tz, variants=tuple(variants), garmin=garmin, smoke=merged.get("smoke", False), trials=trials,
                all_trials_must_pass=strict, why=" ".join(merged["why"].split()), expect=exp,
                followups=followups, prompt_file=prompt_file, report_url=report_url, extra_tools=extra,
                db_seed=seed, max_turns=max_turns)


def load_file(path: str, check_files: bool = True) -> list:
    """The cases of one group file."""
    group = os.path.splitext(os.path.basename(path))[0]
    where = os.path.relpath(path, REPO)
    if group not in GROUPS:
        raise CaseError(f"{where}: unknown group file (known: {', '.join(GROUPS)})")
    try:
        with open(path, "rb") as fh:
            doc = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise CaseError(f"{where}: not valid TOML: {exc}") from None
    unknown = set(doc) - FILE_KEYS
    if unknown:
        raise CaseError(f"{where}: unknown top-level key(s): {', '.join(sorted(unknown))}")
    defaults = doc.get("defaults", {})
    if not isinstance(defaults, dict) or set(defaults) - DEFAULT_KEYS:
        raise CaseError(f"{where}: [defaults] may hold only {', '.join(sorted(DEFAULT_KEYS))}")
    raw_cases = doc.get("case", [])
    if not isinstance(raw_cases, list) or not raw_cases:
        raise CaseError(f"{where}: no [[case]] tables")
    return [_case(raw, defaults, group, where, check_files) for raw in raw_cases]


def load_cases(path: str = "evals/cases", check_files: bool = True) -> list:
    """Every case in the *.toml files of `path` (relative paths resolve against the repo root), in file and table
    order. check_files=False skips opening fixture files (for tools that only list cases). Raises CaseError."""
    folder = path if os.path.isabs(path) else os.path.join(REPO, path)
    if not os.path.isdir(folder):
        raise CaseError(f"no cases folder: {path}")
    names = sorted(n for n in os.listdir(folder) if n.endswith(".toml"))
    if not names:
        raise CaseError(f"no case files in {path}")
    cases, seen = [], {}
    for name in names:
        for case in load_file(os.path.join(folder, name), check_files):
            if case.id in seen:
                raise CaseError(f"duplicate case id {case.id} in {seen[case.id]} and {name}")
            seen[case.id] = name
            cases.append(case)
    return cases


def smoke_ids(cases: list) -> list:
    """Ids of the smoke cases, in load order."""
    return [c.id for c in cases if c.smoke]
