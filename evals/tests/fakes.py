"""A scripted fake Anthropic client for offline eval tests: no network, no key.

    client = ScriptedClient([tool_use(("get_daily_status", {"date": ""})), answer("Green: train as planned.")])
    runner.run_trial(client, case, "instr", 1, MODEL)

Each step of a script is a response (a Messages API response as a dict) or a callable that takes the request
(the kwargs of messages.create, as plain JSON data) and returns one, so a step can read the tool results the
model would have seen (last_tool_results()). Steps are used in order; past the end, the last step repeats.
`routes` answers requests for other models (the judge) with their own scripts. With sdk_objects=True every
response is turned into an anthropic.types.Message, the type the real SDK returns, so the runner's
model_dump path is exercised too. With strict=True (the default) the request's arguments are checked against
the installed SDK's messages.create signature (check_kwargs), so an argument the real client would reject
fails here too. Every request is kept in `requests` (deep copies).
"""

from __future__ import annotations

import copy
import itertools
import json

MODEL = "claude-sonnet-5-5"  # the CLI default
JUDGE_MODEL = "claude-haiku-5-5"  # the CLI default judge
_ids = itertools.count(1)


def usage(i=100, o=50, r=0, w=0) -> dict:
    """A usage dict with the four counts the runner records."""
    return {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": r, "cache_creation_input_tokens": w}


def _response(content, stop, model, use):
    return {"id": f"msg_fake_{next(_ids):04d}", "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": stop, "stop_sequence": None, "usage": use or usage()}


def tool_use(*calls, model=MODEL, thinking=True, text=None, use=None) -> dict:
    """An assistant turn that calls tools: calls are (name, input) pairs, all in one turn (parallel calls).
    A thinking block (with a signature) leads, as adaptive thinking gives; `text` adds a text block."""
    content = [{"type": "thinking", "thinking": "", "signature": "sig-fake"}] if thinking else []
    if text:
        content.append({"type": "text", "text": text})
    for name, args in calls:
        content.append({"type": "tool_use", "id": f"toolu_fake_{next(_ids):04d}", "name": name, "input": args})
    return _response(content, "tool_use", model, use)


def answer(text, model=MODEL, stop="end_turn", use=None) -> dict:
    """A final assistant turn with one text block."""
    return _response([{"type": "text", "text": text}], stop, model, use)


def answer_text(response) -> str:
    """The text blocks of a response dict, joined."""
    return "".join(b.get("text", "") for b in response.get("content") or [] if b.get("type") == "text")


def last_tool_results(request) -> list[dict]:
    """The tool_result blocks of the last user message of a request (empty when it holds none)."""
    msgs = request.get("messages") or []
    if not msgs or msgs[-1].get("role") != "user" or not isinstance(msgs[-1].get("content"), list):
        return []
    return [b for b in msgs[-1]["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]


def result_text(block) -> str:
    """The text of a tool_result block (string content or text blocks)."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict))


class ScriptedClient:
    """messages.create answers from a script (see the module docstring)."""

    def __init__(self, script, *, model=MODEL, routes=None, sdk_objects=False, strict=True):
        self.strict = strict
        self.scripts = {model: list(script)}
        for m, s in (routes or {}).items():
            self.scripts[m] = list(s)
        self.used = {m: 0 for m in self.scripts}
        self.requests: list[dict] = []
        self.sdk_objects = sdk_objects
        self.messages = self

    def create(self, **kwargs):
        if self.strict:
            check_kwargs(kwargs)
        request = json.loads(json.dumps(kwargs, default=str))
        self.requests.append(request)
        model = request.get("model")
        if model not in self.scripts:
            raise AssertionError(f"ScriptedClient: no script for model {model!r}")
        script = self.scripts[model]
        if not script:
            raise AssertionError(f"ScriptedClient: empty script for model {model!r}")
        step = script[min(self.used[model], len(script) - 1)]
        self.used[model] += 1
        response = step(request) if callable(step) else step
        response = copy.deepcopy(response)
        if self.sdk_objects:
            from anthropic.types import Message

            return Message.model_validate(response)
        return response

    def requests_for(self, model) -> list[dict]:
        return [r for r in self.requests if r.get("model") == model]


def check_kwargs(kwargs) -> None:
    """TypeError for a messages.create argument the installed SDK does not take (as the real client raises),
    and for a missing required one. Skipped when the anthropic package is not installed."""
    try:
        import inspect

        from anthropic.resources.messages import Messages
    except ImportError:
        return
    params = inspect.signature(Messages.create).parameters
    unknown = sorted(set(kwargs) - set(params))
    if unknown:
        raise TypeError(f"messages.create() got unexpected keyword argument(s): {', '.join(unknown)}")
    lacking = [k for k in ("model", "max_tokens", "messages") if k not in kwargs]
    if lacking:
        raise TypeError(f"messages.create() missing required argument(s): {', '.join(lacking)}")


def first_prompt(request) -> str:
    """The text of the first user message of a request."""
    msgs = request.get("messages") or []
    if not msgs:
        return ""
    content = msgs[0].get("content")
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


class RoutedClient:
    """A stateless scripted client for whole runs (many cases and trials through one client, as the CLI does).

    The script of a request is the one whose key is the first user prompt of the conversation (exact match);
    its step is the number of assistant messages already in the request, so every trial replays its script
    from the start. Requests for another model (the judge) go to `judge` (a step or a callable), when given.
    """

    def __init__(self, scripts: dict, *, model=MODEL, judge=None, sdk_objects=False, strict=True):
        self.strict = strict
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.model = model
        self.judge = judge
        self.sdk_objects = sdk_objects
        self.requests: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        if self.strict:
            check_kwargs(kwargs)
        request = json.loads(json.dumps(kwargs, default=str))
        self.requests.append(request)
        if request.get("model") != self.model:
            if self.judge is None:
                raise AssertionError(f"RoutedClient: unexpected request for model {request.get('model')!r}")
            response = self.judge(request) if callable(self.judge) else self.judge
        else:
            prompt = first_prompt(request)
            if prompt not in self.scripts:
                raise AssertionError(f"RoutedClient: no script for prompt {prompt!r}")
            script = self.scripts[prompt]
            done = sum(1 for m in request.get("messages") or [] if m.get("role") == "assistant")
            step = script[min(done, len(script) - 1)]
            response = step(request) if callable(step) else step
        response = copy.deepcopy(response)
        if self.sdk_objects:
            from anthropic.types import Message

            return Message.model_validate(response)
        return response


def judge_ids(request) -> list[str]:
    """The rubric item ids a judge request asks for (from its structured-output schema), or []."""
    try:
        schema = request["output_config"]["format"]["schema"]
        return list(schema["properties"]["items"]["items"]["properties"]["id"]["enum"])
    except (KeyError, TypeError):
        return []


def judge_answer(verdicts: dict | None = None, model=JUDGE_MODEL):
    """A judge step in the structured-output shape of evals.grading.judge: {"items": [{"id", "verdict":
    "yes"|"no", "reason"}]}. Every item the request asks for is answered: from `verdicts` ({item id:
    True/False}) where given, else "yes"."""
    verdicts = dict(verdicts or {})

    def step(request):
        ids = judge_ids(request) or list(verdicts)
        items = [{"id": i, "verdict": "yes" if verdicts.get(i, True) else "no", "reason": "scripted"} for i in ids]
        return answer(json.dumps({"items": items}), model=model)
    return step
