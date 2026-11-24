"""Prices per model, the cost of one API call from its usage, and the --max-usd guard.

Prices are US dollars per million tokens, Anthropic first-party API rates, taken from
the claude-api skill (cached 2026-11-03). Cache writes use the 5-minute TTL (1.25x the
input price); cache reads are the listed read price (0.1x input where the skill lists
none). Haiku 5.5's rate is the one for prompts up to 100K tokens, which every case is.
Change a price here, nowhere else.

The guard (Budget) stops a run BEFORE a call whose projected cost would take the run
over its cap. A call's cost is only known after it returns, so the projection is the
costliest call seen so far in the run, or, for the first call, an estimate from the
size of the request (about 3.5 characters per token) plus 4,000 output tokens.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# model id: (input, output, cache read, cache write 5 min), USD per million tokens
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-opus-5": (5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50),
    "claude-fable-5-2": (8.00, 40.00, 0.20, 10.00),
}

USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
FIRST_CALL_OUTPUT_TOKENS = 4000
CHARS_PER_TOKEN = 3.5


class UnknownModel(ValueError):
    """The model has no price here, so no budget can be kept for it."""


class BudgetExceeded(RuntimeError):
    """The next call would take the run over --max-usd."""


def price(model: str) -> tuple[float, float, float, float]:
    """The price row for model; UnknownModel when PRICES has none."""
    try:
        return PRICES[model]
    except KeyError:
        raise UnknownModel(f"no price for model {model!r}; add it to evals/cost.py PRICES") from None


def usage_dict(usage) -> dict[str, int]:
    """The four token counts of a usage object or dict, missing or null ones as 0."""
    if usage is None:
        return {k: 0 for k in USAGE_KEYS}
    if not isinstance(usage, dict):
        usage = {k: getattr(usage, k, 0) for k in USAGE_KEYS}
    return {k: int(usage.get(k) or 0) for k in USAGE_KEYS}


def usage_cost(model: str, usage) -> float:
    """US dollars for one call of model with this usage."""
    p_in, p_out, p_read, p_write = price(model)
    u = usage_dict(usage)
    return (u["input_tokens"] * p_in + u["output_tokens"] * p_out
            + u["cache_read_input_tokens"] * p_read + u["cache_creation_input_tokens"] * p_write) / 1_000_000


def add_usage(total: dict[str, int], usage) -> dict[str, int]:
    """total += usage, in place; returns total."""
    for k, v in usage_dict(usage).items():
        total[k] = total.get(k, 0) + v
    return total


@dataclass
class Budget:
    """Running cost of a run and its cap (max_usd None = no cap)."""

    max_usd: float | None = None
    spent: float = 0.0
    calls: int = 0
    costliest: float = 0.0

    def estimate(self, request: dict) -> float:
        """Projected cost of the call request (the kwargs of messages.create)."""
        # The worst case of this call: its whole input uncached plus every output token it may use. A tool
        # loop's calls grow, so the costliest call so far is no bound; this keeps --max-usd a hard stop.
        p_in, p_out, _, _ = price(request.get("model", ""))
        size = len(json.dumps({k: request.get(k) for k in ("tools", "system", "messages")}, default=str))
        out_tokens = request.get("max_tokens") or FIRST_CALL_OUTPUT_TOKENS
        return (size / CHARS_PER_TOKEN * p_in + out_tokens * p_out) / 1_000_000

    def check(self, request: dict) -> None:
        """BudgetExceeded when the projected call would cross the cap."""
        if self.max_usd is None:
            return
        projected = self.spent + self.estimate(request)
        if projected > self.max_usd:
            raise BudgetExceeded(
                f"stopped before the next call: ${self.spent:.4f} spent, about ${projected:.4f} after it, "
                f"cap ${self.max_usd:.2f} (--max-usd)")

    def charge(self, model: str, usage) -> float:
        """Add one finished call; returns its cost."""
        cost = usage_cost(model, usage)
        self.spent += cost
        self.calls += 1
        self.costliest = max(self.costliest, cost)
        return cost


class MeteredClient:
    """Wraps an Anthropic client: every messages.create is checked against and charged to budget.

    The runner and the judge both get this wrapper, so --max-usd covers both.
    """

    def __init__(self, client, budget: Budget):
        self._client = client
        self.budget = budget
        self.messages = _MeteredMessages(self)


class _MeteredMessages:
    def __init__(self, owner: MeteredClient):
        self._owner = owner

    def create(self, **kwargs):
        budget = self._owner.budget
        budget.check(kwargs)
        response = self._owner._client.messages.create(**kwargs)
        usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
        budget.charge(kwargs.get("model", ""), usage)
        return response
