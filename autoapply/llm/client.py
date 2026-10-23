"""Every Claude call goes through here: JSON output, retries, spend and budget (§9).

The network sits behind `Transport`, so tests replay recorded responses and
never touch the API (§18).
"""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..config import LlmCfg
from ..ledger import Ledger

log = logging.getLogger(__name__)

# $/million tokens, from Anthropic's published table as of 2026-10-06. Used only
# where profile.yaml leaves `llm.pricing` empty - VERIFY current prices there.
KNOWN_PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
# models that accept the server-side refusal fallback ("default" form)
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"}


class BudgetExceeded(Exception):
    pass


class LLMError(Exception):
    """The call failed in a way worth retrying next run (posting -> `error`)."""


class LLMUnavailable(LLMError):
    """Claude is unusable for every posting right now (key revoked, no credit, rate limit or
    outage after the SDK's own retries): stop the run, do not count it against postings."""


BILLING = ("credit", "billing", "balance", "quota", "payment")


@dataclass
class Result:
    text: str
    usage: dict
    stop_reason: str | None
    model: str
    data: Any = None


class Transport(Protocol):
    def create(self, params: dict, fallback: bool) -> Result: ...


class AnthropicTransport:
    def __init__(self, api_key: str, max_retries: int = 4, timeout: float = 120):
        import anthropic
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=max_retries, timeout=timeout)

    def ping(self, model: str) -> None:
        """Free check that the key works and the model exists (no tokens spent)."""
        self.client.models.retrieve(model)

    def create(self, params: dict, fallback: bool) -> Result:
        a = self._anthropic
        try:
            if fallback:
                msg = self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
            else:
                msg = self.client.messages.create(**params)
        except (a.BadRequestError, a.AuthenticationError, a.PermissionDeniedError, a.NotFoundError):
            raise
        except (a.RateLimitError, a.APIStatusError, a.APIConnectionError) as e:
            # the SDK already retried with backoff: Claude is unavailable for now, not this posting
            raise LLMUnavailable(f"Anthropic API: {type(e).__name__}: {str(e)[:200]}") from e
        u = msg.usage
        usage = {
            "input_tokens": u.input_tokens or 0,
            "output_tokens": u.output_tokens or 0,
            "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        }
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return Result(text=text, usage=usage, stop_reason=msg.stop_reason, model=msg.model)


@dataclass
class FakeTransport:
    """Test double: `responder(params) -> str | dict` returns the model's text."""
    responder: Callable[[dict], Any]
    calls: list = field(default_factory=list)

    def create(self, params: dict, fallback: bool) -> Result:
        self.calls.append(params)
        out = self.responder(params)
        text = out if isinstance(out, str) else json.dumps(out)
        return Result(text=text, usage={"input_tokens": 1000, "output_tokens": 100},
                      stop_reason="end_turn", model=params["model"])


class LLM:
    def __init__(self, cfg: LlmCfg, ledger: Ledger, transport: Transport):
        self.cfg = cfg
        self.ledger = ledger
        self.transport = transport
        self._lock = threading.Lock()
        self._fallback_ok = cfg.server_fallback
        self.stopped = False          # set once the budget is hit mid-run
        self._unpriced: set[str] = set()

    # ------------------------------------------------------------ pricing --
    def _known(self, model: str):
        """(input, output, PriceCfg|None) for `model`, or None. A dated id
        ("claude-sonnet-5-5-20260915") uses its base model's price."""
        base = re.sub(r"-\d{8}$", "", model or "")
        p = self.cfg.pricing.get(model) or self.cfg.pricing.get(base)
        if p is not None and p.input is not None and p.output is not None:
            return p.input, p.output, p
        known = [k for k in KNOWN_PRICES if base == k or base.startswith(k + "-")]
        if known:
            return (*KNOWN_PRICES[max(known, key=len)], p)
        return None

    def has_price(self, model: str) -> bool:
        return self._known(model) is not None

    def price(self, model: str) -> tuple[float, float, float, float]:
        """Per-million-token prices. An unknown model is charged at the most expensive
        known rate, never free, so the monthly budget always holds."""
        found = self._known(model)
        if found is None:
            inp, out = max(KNOWN_PRICES.values())
            p = self.cfg.pricing.get(model)
            if model not in self._unpriced:
                self._unpriced.add(model)
                log.warning("no price for model %s: counting it at the highest known rate "
                            "(set llm.pricing in profile.yaml to fix)", model)
        else:
            inp, out, p = found
        cr = p.cache_read if p is not None and p.cache_read is not None else inp * 0.1
        cw = p.cache_write if p is not None and p.cache_write is not None else inp * 1.25
        return inp, out, cr, cw

    def cost(self, model: str, usage: dict) -> float:
        inp, out, cr, cw = self.price(model)
        return (usage.get("input_tokens", 0) * inp + usage.get("output_tokens", 0) * out
                + usage.get("cache_read_tokens", 0) * cr + usage.get("cache_write_tokens", 0) * cw) / 1e6

    def month_spend(self) -> float:
        return self.ledger.month_spend()

    def budget_left(self) -> float:
        return self.cfg.monthly_budget_usd - self.month_spend()

    # -------------------------------------------------------------- calls --
    def _check_budget(self):
        if self.stopped or self.budget_left() <= 0:
            self.stopped = True
            raise BudgetExceeded(
                f"month-to-date spend ${self.month_spend():.2f} reached the "
                f"${self.cfg.monthly_budget_usd:.2f} budget")

    def call(self, purpose: str, model: str, system: list[dict], user: str,
             schema: dict | None = None, posting_key: str | None = None,
             max_tokens: int = 4000, effort: str | None = None) -> Result:
        """One request. With `schema`, `.data` holds the parsed JSON object.

        Malformed JSON gets exactly one retry, then LLMError (§9.1).
        """
        self._check_budget()
        params: dict[str, Any] = {
            "model": model, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        oc: dict[str, Any] = {}
        if schema is not None:
            oc["format"] = {"type": "json_schema", "schema": schema}
        if effort:
            oc["effort"] = effort
        if oc:
            params["output_config"] = oc
        last_err = None
        for attempt in range(2):
            res = self._send(purpose, params, posting_key)
            if res.stop_reason == "refusal":
                raise LLMError(f"{purpose}: model declined the request")
            if res.stop_reason == "max_tokens":
                last_err = f"{purpose}: output hit max_tokens"
                params["max_tokens"] = int(params["max_tokens"] * 2)
                continue
            if schema is None:
                return res
            try:
                res.data = json.loads(res.text)
                if not isinstance(res.data, dict):
                    raise ValueError("not an object")
                return res
            except ValueError as e:
                last_err = f"{purpose}: malformed JSON ({e})"
                log.warning("%s - retrying once", last_err)
        raise LLMError(last_err or f"{purpose}: failed")

    def _send(self, purpose: str, params: dict, posting_key: str | None) -> Result:
        model = params["model"]
        use_fallback = self._fallback_ok and model in FALLBACK_MODELS
        try:
            res = self.transport.create(params, use_fallback)
        except Exception as e:  # noqa: BLE001 - classify SDK errors without importing them here
            name = type(e).__name__
            if use_fallback and name == "BadRequestError" and "fallback" in str(e).lower():
                log.warning("server-side fallback rejected; continuing without it")
                self._fallback_ok = False
                res = self.transport.create(params, False)
            elif name in ("AuthenticationError", "PermissionDeniedError", "NotFoundError") or (
                    name == "BadRequestError" and any(w in str(e).lower() for w in BILLING)):
                raise LLMUnavailable(f"Claude is unavailable ({name}): {str(e)[:200]}") from e
            elif name == "BadRequestError":
                raise LLMError(f"{purpose}: {name}: {e}") from e
            else:
                raise
        cost = self.cost(res.model or model, res.usage)
        with self._lock:
            self.ledger.record_usage(purpose, res.model or model, res.usage, cost, posting_key)
        return res


def cached_system(stable: str, *volatile: str) -> list[dict]:
    """System blocks with a cache breakpoint after the stable prefix (resume + rules)."""
    blocks = [{"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}}]
    blocks += [{"type": "text", "text": v} for v in volatile if v]
    return blocks
