"""
Spend guard, model choice and budget pacing. Standard library only
(precheck.py imports it before any dependency is installed).

Key states (GET https://openrouter.ai/api/v1/key):
  ok          key readable and remaining >= BUDGET_STOP_BELOW
  stop        remaining < BUDGET_STOP_BELOW (default $3) -> no LLM calls
  auth_error  key missing or rejected (401/403)          -> systemic
  unknown     any other read failure                     -> no LLM calls
A key WITHOUT a spending limit is not refused (spec 12, C3): remaining is
taken as ASSUMED_BUDGET_USD - usage and the summary shows a yellow warning.

Pacing (A2 + C6): per-question budget = spendable / estimated remaining
questions. The richest tier that fits is used; below the cheapest tier the
cheapest is still used (never skip a question for budget).
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# Explicit fallback lists (B11, C2). First usable one wins.
FORECAST_CANDIDATES = ["openai/gpt-5.6-terra", "openai/gpt-5.4"]
CHEAP_CANDIDATES = ["openai/gpt-5.4-mini", "openai/gpt-5-mini"]
_EXCLUDED = re.compile(r"(-pro\b|-image\b|:batch$)")
FALLBACK_PRICES = {  # USD per token (OpenRouter 2026-10-16); only if /models is unreadable
    "openai/gpt-5.6-terra": (2e-6, 12e-6),
    "openai/gpt-5.4": (2.5e-6, 15e-6),
    "openai/gpt-5.4-mini": (0.75e-6, 4.5e-6),
}
WEB_SEARCH_COST = 0.01  # per research call (native search request)


@dataclass
class KeyInfo:
    limit: float | None
    usage: float
    remaining: float | None


@dataclass
class KeyLookup:
    info: KeyInfo | None
    http_status: int | None = None


@dataclass
class BudgetDecision:
    state: str
    remaining: float | None  # effective remaining (assumed budget when the key has no limit)
    reason: str
    warning: str = ""  # yellow note (e.g. key without a limit)

    @property
    def may_call_llm(self) -> bool:
        return self.state == "ok"

    @property
    def systemic(self) -> bool:
        """Turns the GitHub job red (B1)."""
        return self.state != "ok"


def _env_float(env: dict[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    return float(raw) if raw not in (None, "") else default


def http_get_json(url: str, headers: dict[str, str] | None = None, timeout: int = 20) -> Any:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
    api_key: str | None, get_json: Callable[..., Any] = http_get_json
) -> KeyLookup:
    if not api_key:
        return KeyLookup(None, None)
    try:
        payload = get_json(OPENROUTER_KEY_URL, {"Authorization": f"Bearer {api_key}"})
        return KeyLookup(parse_key_payload(payload), 200)
    except urllib.error.HTTPError as e:
        return KeyLookup(None, e.code)
    except Exception:
        # Never echo the request: it carries the key.
        return KeyLookup(None, None)


def decide(lookup: KeyLookup, env: dict[str, str] | None = None) -> BudgetDecision:
    env = dict(os.environ) if env is None else env
    stop_below = _env_float(env, "BUDGET_STOP_BELOW", 3.0)
    info = lookup.info
    if info is None:
        if lookup.http_status in (401, 403):
            return BudgetDecision("auth_error", None, f"OpenRouter key rejected (HTTP {lookup.http_status})")
        if lookup.http_status == 402:
            return BudgetDecision("stop", 0.0, "OpenRouter says the credits are used up (HTTP 402)")
        if not env.get("OPENROUTER_API_KEY") and lookup.http_status is None:
            return BudgetDecision("auth_error", None, "OPENROUTER_API_KEY is not set")
        status = f" (HTTP {lookup.http_status})" if lookup.http_status else ""
        return BudgetDecision("unknown", None, f"could not read OpenRouter key info{status}")
    warning = ""
    if info.limit is None:
        assumed = _env_float(env, "ASSUMED_BUDGET_USD", 100.0)
        rem = assumed - info.usage
        warning = (
            f"OpenRouter key has no spending limit; pacing assumes ${assumed:.0f} in total "
            f"(ASSUMED_BUDGET_USD) minus ${info.usage:.2f} used. Make sure auto top-up is OFF."
        )
    else:
        rem = info.remaining if info.remaining is not None else 0.0
    if rem < stop_below:
        return BudgetDecision("stop", rem, f"remaining ${rem:.2f} < ${stop_below:.2f}", warning)
    return BudgetDecision("ok", rem, f"remaining ${rem:.2f}", warning)


# ------------------------------------------------------------------- models


def fetch_models(get_json: Callable[..., Any] = http_get_json) -> list[dict[str, Any]] | None:
    try:
        return get_json(OPENROUTER_MODELS_URL, None, 30)["data"]
    except Exception:
        return None


def allowed_model(model_id: str) -> bool:
    return bool(model_id) and not _EXCLUDED.search(model_id)


def pick_model(
    models: list[dict[str, Any]] | None, candidates: list[str], preferred: str | None = None
) -> str:
    """First usable model: `preferred` (config) if listed, then the candidate list."""
    order = ([preferred] if preferred else []) + candidates
    order = [m for m in order if allowed_model(m)]
    if not models:
        return order[0]
    ids = {m["id"] for m in models}
    return next((m for m in order if m in ids), order[0])


def model_prices(
    models: list[dict[str, Any]] | None, model_ids: list[str]
) -> dict[str, tuple[float, float]]:
    by_id = {m["id"]: m for m in (models or [])}
    prices: dict[str, tuple[float, float]] = {}
    for mid in model_ids:
        if mid in by_id:
            p = by_id[mid].get("pricing", {})
            prices[mid] = (float(p.get("prompt", 0)), float(p.get("completion", 0)))
        else:
            prices[mid] = FALLBACK_PRICES.get(mid, FALLBACK_PRICES["openai/gpt-5.4"])
    return prices


# ------------------------------------------------------------------- pacing

# Token assumptions per call (input, output incl. reasoning). Rough; the job
# summary reports the real spend so these can be recalibrated.
RESEARCH_TOKENS = (10_000, 3_000)  # research always runs on the cheap model (C2)
FORECAST_TOKENS = (4_000, 4_000)
PARSE_TOKENS = (2_500, 200)  # 2 parser calls per forecast (validation samples)
SUMMARY_TOKENS = (3_000, 500)


@dataclass(frozen=True)
class Tier:
    name: str
    forecasts: int  # forecasts per question (one research report each question)


TIERS = [  # richest first; "minimum" = one research + one forecast
    Tier("full", 5),
    Tier("standard", 3),
    Tier("lean", 2),
    Tier("minimum", 1),
]
KINDS = ("main", "minibench")


def _cost(tokens: tuple[int, int], price: tuple[float, float]) -> float:
    return tokens[0] * price[0] + tokens[1] * price[1]


def tier_cost(tier: Tier, forecast_price: tuple[float, float], cheap_price: tuple[float, float]) -> float:
    return (
        _cost(RESEARCH_TOKENS, cheap_price)
        + WEB_SEARCH_COST
        + _cost(SUMMARY_TOKENS, cheap_price)
        + tier.forecasts * (_cost(FORECAST_TOKENS, forecast_price) + 2 * _cost(PARSE_TOKENS, cheap_price))
    )


def _date(env: dict[str, str], name: str, default: str) -> date:
    return date.fromisoformat(env.get(name) or default)


def questions_per_day(env: dict[str, str], kind: str) -> float:
    name = "MINIBENCH_QUESTIONS_PER_DAY" if kind == "minibench" else "MAIN_QUESTIONS_PER_DAY"
    return _env_float(env, name, 4.0)


@dataclass
class KindPace:
    tier: Tier
    per_question_budget: float
    est_cost: float
    remaining_questions: int


@dataclass
class Pacing:
    kinds: dict[str, KindPace]
    after_split: bool
    run_out: date | None


def pace(
    remaining: float,
    today: date,
    env: dict[str, str],
    open_unforecasted: dict[str, int],
    prices: dict[str, tuple[tuple[float, float], tuple[float, float]]],
) -> Pacing:
    """
    prices: kind -> (forecast model price, cheap model price).
    Before BUDGET_SPLIT_DATE (default 2026-11-01, first MiniBench round
    resolved) both tournaments share one per-question budget. After it the
    main tournament gets MAIN_BUDGET_SHARE (default 0.8) of the money and
    MiniBench always uses the cheapest tier (C6).
    """
    days_left = max(0, (_date(env, "SEASON_END", "2027-01-01") - today).days)
    n = {
        k: max(1, open_unforecasted.get(k, 0), round(days_left * questions_per_day(env, k)))
        for k in KINDS
    }
    stop_below = _env_float(env, "BUDGET_STOP_BELOW", 3.0)
    spendable = max(0.0, remaining - _env_float(env, "BUDGET_RESERVE", stop_below))
    after_split = today >= _date(env, "BUDGET_SPLIT_DATE", "2026-11-01")
    if after_split:
        share = min(1.0, max(0.0, _env_float(env, "MAIN_BUDGET_SHARE", 0.8)))
        per_q = {"main": spendable * share / n["main"], "minibench": spendable * (1 - share) / n["minibench"]}
    else:
        both = spendable / (n["main"] + n["minibench"])
        per_q = {"main": both, "minibench": both}
    kinds: dict[str, KindPace] = {}
    for k in KINDS:
        fp, cp = prices[k]
        if after_split and k == "minibench":
            tier = TIERS[-1]
        else:
            tier = next((t for t in TIERS if tier_cost(t, fp, cp) <= per_q[k]), TIERS[-1])
        kinds[k] = KindPace(tier, per_q[k], tier_cost(tier, fp, cp), n[k])
    daily = sum(kinds[k].est_cost * questions_per_day(env, k) for k in KINDS)
    left = max(0.0, remaining - stop_below)
    run_out = today + timedelta(days=int(left / daily)) if daily > 0 else None
    return Pacing(kinds, after_split, run_out)


def kind_of(tournament_name: str) -> str:
    return "minibench" if "minibench" in tournament_name.lower() else "main"


def utc_today() -> date:
    return datetime.now(timezone.utc).date()
