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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# Explicit fallback lists (B11, C2). First usable one wins.
FORECAST_CANDIDATES = ["openai/gpt-6.1-sol", "openai/gpt-5.6-terra", "openai/gpt-5.4"]
CHEAP_CANDIDATES = ["openai/gpt-5.4-mini", "openai/gpt-5-mini"]
_EXCLUDED = re.compile(r"(-pro\b|-image\b|:batch$|codex)")
FALLBACK_PRICES = {  # USD per token (OpenRouter 2026-10-17); only if /models is unreadable
    "openai/gpt-6.1-sol": (2e-6, 10e-6),
    "openai/gpt-6-sol": (2e-6, 10e-6),
    "openai/gpt-6-astra": (10e-6, 50e-6),
    "openai/gpt-6-luna": (0.10e-6, 0.50e-6),
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
    provider: str = "openrouter"
    note: str = ""  # yellow note carried into the decision (e.g. Anthropic spend state missing)
    free: dict[str, tuple[int, int]] | None = None  # v0.3.0 OpenAI: group -> (free tokens used today, allowance)


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
    # v0.3.0: the OpenAI budget is a few dollars, so it has its own (lower) stop line
    stop_below = openai_stop_below(env) if lookup.provider == "openai" else _env_float(env, "BUDGET_STOP_BELOW", 3.0)
    info = lookup.info
    label = provider_label(lookup.provider)
    key_name = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(lookup.provider, "OPENROUTER_API_KEY")
    if info is None:
        if lookup.http_status in (401, 403):
            return BudgetDecision("auth_error", None, f"{label} key rejected (HTTP {lookup.http_status})")
        if lookup.http_status == 402:
            return BudgetDecision("stop", 0.0, f"{label} says the credits are used up (HTTP 402)")
        if not env.get(key_name) and lookup.http_status is None:
            return BudgetDecision("auth_error", None, f"{key_name} is not set")
        status = f" (HTTP {lookup.http_status})" if lookup.http_status else ""
        return BudgetDecision("unknown", None, f"could not read {label} key info{status}")
    warning = lookup.note
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


def tier_cost(tier: Tier, forecast_price: tuple[float, float], cheap_price: tuple[float, float],
              provider: str = "openrouter") -> float:
    if provider == "anthropic":  # v0.2.0: Haiku research with server web search, Opus forecasts
        return (
            _cost(A_RESEARCH_TOKENS, cheap_price)
            + A_SEARCHES_PER_RESEARCH * ANTHROPIC_WEB_SEARCH_COST
            + _cost(A_SUMMARY_TOKENS, cheap_price)
            + tier.forecasts * (_cost(A_FORECAST_TOKENS, forecast_price) + 2 * _cost(A_PARSE_TOKENS, cheap_price))
        )
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
    horizon_end: date | None = None,
    provider: str = "openrouter",
) -> Pacing:
    """
    prices: kind -> (forecast model price, cheap model price).
    Before BUDGET_SPLIT_DATE (default 2026-11-01, first MiniBench round
    resolved) both tournaments share one per-question budget. After it the
    main tournament gets MAIN_BUDGET_SHARE (default 0.8) of the money and
    MiniBench always uses the cheapest tier (C6).
    """
    # v0.2.0: on Anthropic the money is per credit period, so pace until the next reset, not season end
    end = horizon_end or _date(env, "SEASON_END", "2027-01-01")
    days_left = max(0, (end - today).days)
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
            tier = next((t for t in TIERS if tier_cost(t, fp, cp, provider) <= per_q[k]), TIERS[-1])
        kinds[k] = KindPace(tier, per_q[k], tier_cost(tier, fp, cp, provider), n[k])
    daily = sum(kinds[k].est_cost * questions_per_day(env, k) for k in KINDS)
    left = max(0.0, remaining - stop_below)
    run_out = today + timedelta(days=int(left / daily)) if daily > 0 else None
    return Pacing(kinds, after_split, run_out)


def kind_of(tournament_name: str) -> str:
    return "minibench" if "minibench" in tournament_name.lower() else "main"


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


# ---------------------------------------------------------------- providers
# v0.2.0: the bot runs on the Anthropic API when there is no OpenRouter key.
# The Anthropic API exposes no credit balance, so spend is tracked locally
# (state.py) from each response's usage and paced per credit period.

PROVIDERS = ("anthropic", "openai", "openrouter")


def provider_error(env: dict[str, str]) -> str:
    raw = (env.get("LLM_PROVIDER") or "").strip().lower()
    if raw and raw not in PROVIDERS:
        return f'LLM_PROVIDER="{env.get("LLM_PROVIDER")}" is not allowed: use openrouter, anthropic or openai, or delete it'
    return ""


def select_provider(env: dict[str, str] | None = None) -> str:
    """LLM_PROVIDER if set; else OpenRouter when its key is present (unchanged behaviour), else Anthropic
    when its key is present, else OpenAI (v0.3.0) when its key is present, else openrouter (whose guard then
    reports the missing key)."""
    env = dict(os.environ) if env is None else env
    raw = (env.get("LLM_PROVIDER") or "").strip().lower()
    if raw in PROVIDERS:
        return raw
    if raw:
        raise ValueError(provider_error(env))
    if env.get("OPENROUTER_API_KEY"):
        return "openrouter"
    if env.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if env.get("OPENAI_API_KEY"):
        return "openai"
    return "openrouter"


def provider_label(provider: str) -> str:
    return {"anthropic": "Anthropic", "openai": "OpenAI"}.get(provider, "OpenRouter")


ANTHROPIC_FORECAST_MODEL = "claude-opus-5-5"
ANTHROPIC_RESEARCH_MODEL = "claude-haiku-5-5"
ANTHROPIC_PRICES_PER_MTOK = {  # USD per million tokens (input, output); Anthropic first-party, skill 2026-10-06
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),  # possible server-side fallback target
    "claude-opus-4-8": (5.0, 25.0),  # possible server-side fallback target
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.0, 50.0),
}
UNKNOWN_MODEL_PER_MTOK = (10.0, 50.0)  # unlisted model without a price variable: assume the dearest tier
# Prompts over the threshold are billed on a higher rate card (Haiku 5.5: > 100K input tokens).
LONG_CONTEXT_PER_MTOK = {"claude-haiku-5-5": (100_000, (0.50, 2.50))}
CACHE_WRITE_MULT = 1.25  # ASSUMPTION: 5-minute cache write = 1.25x input (the bot sets no cache_control)
CACHE_READ_MULT = 0.10  # ASSUMPTION: cache read = 0.1x input (Opus 5.5 is 0.05x; 0.1x errs high)
ANTHROPIC_WEB_SEARCH_COST = 0.01  # $10 per 1,000 searches (web search tool docs, checked 2026-10-23)

# Token assumptions per call on Anthropic (input, output incl. thinking). Unmeasured; recalibrate
# from the Summary's real spend after the first runs.
A_RESEARCH_TOKENS = (30_000, 3_000)  # search results come back as input tokens
A_SEARCHES_PER_RESEARCH = 3  # max_uses is 5
A_FORECAST_TOKENS = (4_000, 6_000)  # effort medium; thinking is billed as output
A_PARSE_TOKENS = (2_500, 800)
A_SUMMARY_TOKENS = (3_000, 800)


def _per_mtok(raw: str | None) -> tuple[float, float] | None:
    """'4/20' -> (4.0, 20.0); anything unreadable -> None."""
    try:
        a, b = str(raw).replace(",", "/").split("/")
        return float(a), float(b)
    except (ValueError, AttributeError):
        return None


def anthropic_models(env: dict[str, str]) -> tuple[str, str]:
    """-> (final forecast model, research/parse/summary model)."""
    return (
        (env.get("ANTHROPIC_FORECAST_MODEL") or ANTHROPIC_FORECAST_MODEL).strip(),
        (env.get("ANTHROPIC_RESEARCH_MODEL") or ANTHROPIC_RESEARCH_MODEL).strip(),
    )


def anthropic_price(model: str, env: dict[str, str], input_tokens: int = 0) -> tuple[float, float]:
    """USD per token (input, output). ANTHROPIC_FORECAST_PRICE / ANTHROPIC_RESEARCH_PRICE ('in/out' per
    million tokens) override the table for the configured models."""
    forecast_id, research_id = anthropic_models(env)
    override = None
    if model == forecast_id:
        override = _per_mtok(env.get("ANTHROPIC_FORECAST_PRICE"))
    elif model == research_id:
        override = _per_mtok(env.get("ANTHROPIC_RESEARCH_PRICE"))
    per = override or ANTHROPIC_PRICES_PER_MTOK.get(model, UNKNOWN_MODEL_PER_MTOK)
    long = LONG_CONTEXT_PER_MTOK.get(model)
    if override is None and long and input_tokens > long[0]:
        per = long[1]
    return per[0] / 1e6, per[1] / 1e6


def web_search_cost(env: dict[str, str]) -> float:
    return _env_float(env, "ANTHROPIC_WEB_SEARCH_COST", ANTHROPIC_WEB_SEARCH_COST)


def _iteration_cost(it: dict[str, Any], model: str, env: dict[str, str]) -> float:
    inp = int(it.get("input_tokens") or 0)
    cw = int(it.get("cache_creation_input_tokens") or 0)
    cr = int(it.get("cache_read_input_tokens") or 0)
    out = int(it.get("output_tokens") or 0)
    pin, pout = anthropic_price(model, env, inp + cw + cr)
    return inp * pin + cw * pin * CACHE_WRITE_MULT + cr * pin * CACHE_READ_MULT + out * pout


def anthropic_usage_cost(usage: dict[str, Any], model: str, env: dict[str, str]) -> float:
    """USD for one Messages API response. `usage.iterations` (present with server-side fallbacks) is the
    per-attempt source of truth, each attempt at its own model's price; else the top-level usage."""
    iterations = [it for it in (usage.get("iterations") or []) if isinstance(it, dict)]
    parts = iterations or [usage]
    total = sum(_iteration_cost(it, it.get("model") or model, env) for it in parts)
    searches = int((usage.get("server_tool_use") or {}).get("web_search_requests") or 0)
    return total + searches * web_search_cost(env)


def credit_reset_day(env: dict[str, str]) -> int:
    """ANTHROPIC_CREDIT_RESET_DAY, default 13; not a number -> 13; clamped to 1..28 (every month has it)."""
    try:
        day = int(str(env.get("ANTHROPIC_CREDIT_RESET_DAY", "")).strip())
    except ValueError:
        return 13
    return min(28, max(1, day))


def credit_period(today: date, reset_day: int) -> tuple[date, date]:
    """-> (start of the current credit period, next reset date)."""
    if today.day >= reset_day:
        start = today.replace(day=reset_day)
    else:
        prev_month = today.replace(day=1) - timedelta(days=1)
        start = prev_month.replace(day=reset_day)
    nxt = (start.replace(day=1) + timedelta(days=32)).replace(day=reset_day)
    return start, nxt


def anthropic_budget(env: dict[str, str]) -> float:
    return _env_float(env, "ANTHROPIC_BUDGET_USD", 90.0)


def anthropic_lookup(env: dict[str, str], spent: float, state_found: bool) -> KeyLookup:
    """Stands in for the OpenRouter /key read: limit = budget per period, usage = locally tracked spend."""
    if not env.get("ANTHROPIC_API_KEY"):
        return KeyLookup(None, None, "anthropic")
    total = anthropic_budget(env)
    note = "" if state_found else (
        "No saved spend record (.state/state.json) was found, so this credit period's spend starts "
        "from $0 here; the real spend may be higher. Check usage in the Claude Console."
    )
    return KeyLookup(KeyInfo(limit=total, usage=spent, remaining=total - spent), 200, "anthropic", note)


def anthropic_lookup_from_store(env: dict[str, str], store: Any, today: date | None = None) -> KeyLookup:
    """`store` = state.PendingComments (spend ledger). None (no state at all) counts as missing."""
    start, _ = credit_period(today or utc_today(), credit_reset_day(env))
    if store is None:
        return anthropic_lookup(env, 0.0, False)
    return anthropic_lookup(env, store.period_spent(start.isoformat()), bool(store.found))


def lookup_for(env: dict[str, str], store: Any = None,
               get_json: Callable[..., Any] = http_get_json, today: date | None = None) -> KeyLookup:
    """Budget lookup for whichever provider this run uses."""
    if select_provider(env) == "anthropic":
        return anthropic_lookup_from_store(env, store, today)
    if select_provider(env) == "openai":
        return openai_lookup_from_store(env, store, today)
    return fetch_key_info(env.get("OPENROUTER_API_KEY"), get_json)


def pacing_horizon(env: dict[str, str], today: date) -> date | None:
    """Anthropic: pace until the next credit reset. OpenAI: month end. OpenRouter: None (= SEASON_END)."""
    if select_provider(env) == "openai":
        return month_end(today)
    if select_provider(env) != "anthropic":
        return None
    return credit_period(today, credit_reset_day(env))[1]


def provider_prices(env: dict[str, str], models: list[dict[str, Any]] | None = None
                    ) -> tuple[dict[str, str], str, dict[str, tuple[float, float]]]:
    """-> (forecast model per kind, cheap model, per-token prices) for the selected provider."""
    if select_provider(env) == "anthropic":
        fid, cheap = anthropic_models(env)
        ids = {k: fid for k in KINDS}
        return ids, cheap, {m: anthropic_price(m, env) for m in {fid, cheap}}
    if select_provider(env) == "openai":  # v0.3.0: "cheap" = the parse/summary model; prices without the cached rate
        fid, _research, parse = openai_models(env)
        ids = {k: fid for k in KINDS}
        return ids, parse, {m: (openai_price(m, env)[0], openai_price(m, env)[2]) for m in {fid, parse}}
    cheap = pick_model(models, CHEAP_CANDIDATES)
    ids = {
        "main": pick_model(models, FORECAST_CANDIDATES, env.get("MAIN_FORECAST_MODEL")),
        "minibench": pick_model(models, FORECAST_CANDIDATES, env.get("MINIBENCH_FORECAST_MODEL")),
    }
    return ids, cheap, model_prices(models, sorted({*ids.values(), cheap}))


# ------------------------------------------------------------------- OpenAI (v0.3.0)
# Direct OpenAI API, built around the data-sharing free daily tokens: a "big" and a
# "mini" model group, each with its own daily token allowance. Forecast and parse
# calls (no tools) normally stay inside the allowance; the research call uses the
# web_search tool and is assumed billed in full (tool use is excluded from the
# free tokens). Everything not free is booked in USD per calendar month.

FREE_BIG_MODELS = {"gpt-5.4", "gpt-5.2", "gpt-5.1", "gpt-5", "gpt-4.1", "gpt-4o", "o1", "o3"}
FREE_MINI_MODELS = {"gpt-5.4-mini", "gpt-5.4-nano", "gpt-5-mini", "gpt-5-nano", "gpt-4.1-mini",
                    "gpt-4.1-nano", "gpt-4o-mini", "o3-mini", "o4-mini"}
OPENAI_FORECAST_MODEL = "gpt-5.4"
OPENAI_RESEARCH_MODEL = "gpt-5.4-mini"
OPENAI_PARSE_MODEL = "gpt-5.4-mini"
OPENAI_PRICES_PER_MTOK = {  # (input, cached input, output) USD per 1M tokens; developers.openai.com 2026-10-24
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.4-nano": (0.20, 0.02, 1.25),
    "gpt-5-mini": (0.25, 0.025, 2.00),
}
OPENAI_UNKNOWN_PER_MTOK = (5.0, 0.5, 30.0)  # unlisted model without a price variable: assume dear
OPENAI_WEB_SEARCH_COST = 0.01  # $10 per 1,000 web_search calls (pricing page 2026-10-24)
DEFAULT_FREE_DAILY = {"big": 250_000, "mini": 2_500_000}
_SNAPSHOT = re.compile(r"-\d{4}-\d{2}-\d{2}$")

# Token assumptions per call on OpenAI (input, output incl. reasoning). Unmeasured.
O_FORECAST_TOKENS = (6_000, 5_000)  # gpt-5.4, reasoning effort medium
O_PARSE_TOKENS = (2_500, 600)  # 2 parser calls per forecast
O_RESEARCH_TOKENS = (15_000, 2_500)  # search content comes back as billed input tokens
O_SEARCHES_PER_RESEARCH = 2.5  # max_tool_calls default 3
DEFER_IF_CLOSES_AFTER_H = 30


def openai_models(env: dict[str, str]) -> tuple[str, str, str]:
    """-> (final forecast model, research model, parse/summary model)."""
    return (
        (env.get("OPENAI_FORECAST_MODEL") or OPENAI_FORECAST_MODEL).strip(),
        (env.get("OPENAI_RESEARCH_MODEL") or OPENAI_RESEARCH_MODEL).strip(),
        (env.get("OPENAI_PARSE_MODEL") or OPENAI_PARSE_MODEL).strip(),
    )


def free_group(model: str) -> str | None:
    """'big' | 'mini' | None (not in the free lists). Dated snapshots count as their base model."""
    base = _SNAPSHOT.sub("", (model or "").strip().lower())
    if base in FREE_BIG_MODELS:
        return "big"
    if base in FREE_MINI_MODELS:
        return "mini"
    return None


def free_daily_limits(env: dict[str, str]) -> dict[str, int]:
    return {"big": int(_env_float(env, "OPENAI_FREE_DAILY_BIG", DEFAULT_FREE_DAILY["big"])),
            "mini": int(_env_float(env, "OPENAI_FREE_DAILY_MINI", DEFAULT_FREE_DAILY["mini"]))}


def free_margin(env: dict[str, str]) -> float:
    """Share of each daily allowance kept unused by pacing (other traffic, estimate error). Default 0.1."""
    return min(0.9, max(0.0, _env_float(env, "OPENAI_FREE_MARGIN", 0.1)))


def openai_price(model: str, env: dict[str, str]) -> tuple[float, float, float]:
    """USD per token (input, cached input, output). OPENAI_{FORECAST,RESEARCH,PARSE}_PRICE ('in/out' per
    million tokens; cached = 10% of input) override the table for the configured models."""
    names = dict(zip(openai_models(env), ("FORECAST", "RESEARCH", "PARSE")))
    override = _per_mtok(env.get(f"OPENAI_{names[model]}_PRICE")) if model in names else None
    if override:
        per = (override[0], override[0] * 0.1, override[1])
    else:
        per = OPENAI_PRICES_PER_MTOK.get(_SNAPSHOT.sub("", model), OPENAI_UNKNOWN_PER_MTOK)
    return per[0] / 1e6, per[1] / 1e6, per[2] / 1e6


def openai_token_cost(usage: dict[str, Any], model: str, env: dict[str, str]) -> float:
    """Standard-rate USD for one Responses API usage object (cached input at the cached rate)."""
    pin, pcached, pout = openai_price(model, env)
    inp = int(usage.get("input_tokens") or 0)
    cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    return (inp - cached) * pin + cached * pcached + out * pout


def usage_tokens(usage: dict[str, Any]) -> int:
    return int(usage.get("total_tokens") or (int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)))


def tool_tokens_free(env: dict[str, str]) -> bool:
    """ASSUMPTION (default off): a request that used a tool gets no free tokens at all."""
    return (env.get("OPENAI_TOOL_TOKENS_FREE") or "").strip() == "1"


def openai_call_charge(usage: dict[str, Any], model: str, env: dict[str, str], used_today: dict[str, int],
                       web_searches: int = 0, used_tool: bool = False) -> tuple[str | None, int, float]:
    """-> (free group the tokens count against or None, tokens to add to it, USD to book).
    A request that would take the group past its daily allowance is charged in full (help-article example)."""
    tokens = usage_tokens(usage)
    searches = web_searches * _env_float(env, "OPENAI_WEB_SEARCH_COST", OPENAI_WEB_SEARCH_COST)
    group = free_group(model)
    if group is None or (used_tool and not tool_tokens_free(env)):
        return None, 0, openai_token_cost(usage, model, env) + searches
    limit = free_daily_limits(env)[group]
    over = int(used_today.get(group, 0)) + tokens > limit
    return group, tokens, (openai_token_cost(usage, model, env) if over else 0.0) + searches


def month_key(today: date) -> str:
    return today.strftime("%Y-%m")


def month_end(today: date) -> date:
    """First day of next month (the dollar budget resets then)."""
    return (today.replace(day=1) + timedelta(days=32)).replace(day=1)


def openai_budget(env: dict[str, str]) -> float:
    return _env_float(env, "OPENAI_BUDGET_USD", 4.5)


def big_tokens_per_question(tier: Tier) -> int:
    return tier.forecasts * sum(O_FORECAST_TOKENS)


def mini_tokens_per_question(tier: Tier) -> int:
    return tier.forecasts * 2 * sum(O_PARSE_TOKENS)


def openai_question_cost(env: dict[str, str]) -> float:
    """Expected paid USD per question while the free tokens last: the research call (tool use, billed)."""
    _, research, _ = openai_models(env)
    pin, _, pout = openai_price(research, env)
    searches = O_SEARCHES_PER_RESEARCH * _env_float(env, "OPENAI_WEB_SEARCH_COST", OPENAI_WEB_SEARCH_COST)
    return O_RESEARCH_TOKENS[0] * pin + O_RESEARCH_TOKENS[1] * pout + searches


def openai_overflow_cost(tier: Tier, env: dict[str, str]) -> float:
    """Standard-rate USD of one question's forecast tokens (what it costs past the free allowance)."""
    fid, _, pid = openai_models(env)
    fin, _, fout = openai_price(fid, env)
    qin, _, qout = openai_price(pid, env)
    per = O_FORECAST_TOKENS[0] * fin + O_FORECAST_TOKENS[1] * fout + 2 * (O_PARSE_TOKENS[0] * qin + O_PARSE_TOKENS[1] * qout)
    return tier.forecasts * per


@dataclass
class OpenAIPlan:
    tiers: dict[str, Tier]  # tier per kind for every question that runs
    free_left: int  # big-group tokens pacing may use today (allowance - used - margin)
    reserved_later: int  # questions expected later today that pacing keeps tokens for
    paid: list[int] = field(default_factory=list)  # indexes answered at minimum tier, paid from the dollar budget
    deferred: list[int] = field(default_factory=list)  # indexes left for a later run (close > 30 h away)
    reason: str = ""


def openai_free_left(free: dict[str, tuple[int, int]] | None, env: dict[str, str], group: str = "big") -> int:
    used, limit = (free or {}).get(group, (0, free_daily_limits(env)[group]))
    return max(0, int(limit * (1 - free_margin(env))) - int(used))


def openai_plan(questions: list[tuple[str, datetime | None]], free: dict[str, tuple[int, int]] | None,
                now: datetime, env: dict[str, str], defer: bool = True) -> OpenAIPlan:
    """questions: (kind, close time) in the order they will run (closing soonest first).
    Richest tier whose projected big-group tokens (this run + questions expected later today) fit the
    free allowance left. If not even the minimum fits: minimum tier, the first ones that still fit are free,
    the rest that close within DEFER_IF_CLOSES_AFTER_H hours are paid, the others wait for another run."""
    left = openai_free_left(free, env)
    hours_left = 24 - (now.hour + now.minute / 60)
    later = int(round(sum(questions_per_day(env, k) for k in KINDS) * hours_left / 24))
    after_split = now.date() >= _date(env, "BUDGET_SPLIT_DATE", "2026-11-01")

    def tiers_for(t: Tier) -> dict[str, Tier]:
        return {"main": t, "minibench": TIERS[-1] if after_split else t}

    def need(tiers: dict[str, Tier], extra: int) -> int:
        per = [big_tokens_per_question(tiers[k]) for k, _ in questions]
        return sum(per) + extra * big_tokens_per_question(tiers["main"])

    for t in TIERS:
        if need(tiers_for(t), later) <= left:
            return OpenAIPlan(tiers_for(t), left, later, reason=f"{t.name} fits today's free tokens")
    tiers = tiers_for(TIERS[-1])
    plan = OpenAIPlan(tiers, left, 0)
    budget_left = left
    cutoff = now + timedelta(hours=_env_float(env, "OPENAI_DEFER_HOURS", DEFER_IF_CLOSES_AFTER_H))
    for i, (kind, close) in enumerate(questions):
        cost = big_tokens_per_question(tiers[kind])
        if cost <= budget_left:
            budget_left -= cost
        elif not defer or close is None or close <= cutoff:
            plan.paid.append(i)
        else:
            plan.deferred.append(i)
    plan.reason = "free tokens short: minimum tier" + (
        f", {len(plan.paid)} paid, {len(plan.deferred)} deferred" if plan.paid or plan.deferred else "")
    return plan


def openai_pacing(plan: OpenAIPlan, remaining_usd: float, today: date, env: dict[str, str],
                  open_unforecasted: dict[str, int]) -> Pacing:
    """Pacing record for the Summary: tier from the token plan; USD per question = month budget left
    spread over the questions expected until the month ends; est_cost = expected paid USD per question."""
    days_left = max(1, (month_end(today) - today).days)
    n = {k: max(1, open_unforecasted.get(k, 0), round(days_left * questions_per_day(env, k))) for k in KINDS}
    spendable = max(0.0, remaining_usd - openai_stop_below(env))
    per_q = spendable / (n["main"] + n["minibench"])
    est = openai_question_cost(env)
    kinds = {k: KindPace(plan.tiers[k], per_q, est, n[k]) for k in KINDS}
    daily = est * sum(questions_per_day(env, k) for k in KINDS)
    run_out = today + timedelta(days=int(spendable / daily)) if daily > 0 else None
    return Pacing(kinds, today >= _date(env, "BUDGET_SPLIT_DATE", "2026-11-01"), run_out)


def openai_stop_below(env: dict[str, str]) -> float:
    return _env_float(env, "OPENAI_BUDGET_STOP_BELOW", 0.25)


def openai_lookup(env: dict[str, str], spent: float, free_used: dict[str, int], state_found: bool) -> KeyLookup:
    """Stands in for the OpenRouter /key read: limit = monthly budget, usage = locally booked USD;
    `free` = today's free tokens (used, allowance) per group."""
    if not env.get("OPENAI_API_KEY"):
        return KeyLookup(None, None, "openai")
    total = openai_budget(env)
    limits = free_daily_limits(env)
    note = "" if state_found else (
        "No saved usage record (.state/state.json) was found, so today's free tokens and this month's "
        "spend start from 0 here; the real usage may be higher. Check the OpenAI usage dashboard.")
    lk = KeyLookup(KeyInfo(limit=total, usage=spent, remaining=total - spent), 200, "openai", note)
    lk.free = {g: (int(free_used.get(g, 0)), limits[g]) for g in ("big", "mini")}
    return lk


def openai_lookup_from_store(env: dict[str, str], store: Any, today: date | None = None) -> KeyLookup:
    day = today or utc_today()
    if store is None:
        return openai_lookup(env, 0.0, {}, False)
    return openai_lookup(env, store.openai_month_spent(month_key(day)), store.openai_free_used(day.isoformat()),
                         bool(store.found))
