"""
Spend guard based on the OpenRouter key itself (GET /api/v1/key).

States:
  refuse_unlimited  key has no spending limit -> do not run (protects a card
                    attached to the account), unless ALLOW_UNLIMITED_KEY=1
  stop              remaining < BUDGET_STOP_BELOW (default $3) -> no LLM calls
  economy           remaining < BUDGET_ECONOMY_BELOW (default $15) -> fewer
                    forecasts per question, cheaper research
  normal
  unknown           key info could not be read -> treated like stop
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

import requests

OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"


@dataclass
class KeyInfo:
    limit: float | None
    usage: float
    remaining: float | None


@dataclass
class BudgetDecision:
    state: str
    remaining: float | None
    reason: str

    @property
    def may_call_llm(self) -> bool:
        return self.state in ("normal", "economy")

    @property
    def red(self) -> bool:
        return not self.may_call_llm


def _env_float(env: dict[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    return float(raw) if raw not in (None, "") else default


def parse_key_payload(payload: dict[str, Any]) -> KeyInfo:
    data = payload.get("data", payload)
    limit = data.get("limit")
    usage = float(data.get("usage") or 0.0)
    remaining = data.get("limit_remaining")
    if limit is not None:
        limit = float(limit)
        remaining = float(remaining) if remaining is not None else limit - usage
    else:
        remaining = None
    return KeyInfo(limit=limit, usage=usage, remaining=remaining)


def fetch_key_info(
    api_key: str | None,
    get: Callable[..., Any] = requests.get,
) -> KeyInfo | None:
    if not api_key:
        return None
    try:
        resp = get(
            OPENROUTER_KEY_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20,
        )
        resp.raise_for_status()
        return parse_key_payload(resp.json())
    except Exception:
        # Never echo the request (it carries the key); the caller only needs
        # to know the lookup failed.
        return None


def decide(info: KeyInfo | None, env: dict[str, str] | None = None) -> BudgetDecision:
    env = dict(os.environ) if env is None else env
    economy_below = _env_float(env, "BUDGET_ECONOMY_BELOW", 15.0)
    stop_below = _env_float(env, "BUDGET_STOP_BELOW", 3.0)
    if info is None:
        return BudgetDecision(
            "unknown", None, "could not read OpenRouter key info (missing key or API error)"
        )
    if info.limit is None:
        if env.get("ALLOW_UNLIMITED_KEY") == "1":
            return BudgetDecision(
                "normal", None, "key has no limit; allowed by ALLOW_UNLIMITED_KEY=1"
            )
        return BudgetDecision(
            "refuse_unlimited",
            None,
            "OpenRouter key has NO spending limit. Set a limit on the key at "
            "openrouter.ai/settings/keys (or set ALLOW_UNLIMITED_KEY=1).",
        )
    rem = info.remaining if info.remaining is not None else 0.0
    if rem < stop_below:
        return BudgetDecision("stop", rem, f"remaining ${rem:.2f} < ${stop_below:.2f}")
    if rem < economy_below:
        return BudgetDecision(
            "economy", rem, f"remaining ${rem:.2f} < ${economy_below:.2f}"
        )
    return BudgetDecision("normal", rem, f"remaining ${rem:.2f}")
