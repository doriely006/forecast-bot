"""
One forecasting run: budget + pacing -> fetch every open question -> forecast
the new ones -> (optionally) publish forecast, then comment -> job summary.

Everything with side effects is injected, so tests run fully offline.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import budget
import precheck
import state as state_mod

logger = logging.getLogger(__name__)

PER_QUESTION_RETRIES = 2  # extra attempts within one run; across runs only state.py's failure count (stop-loss)
PUBLISH_ATTEMPTS = 3
DEFAULT_MIN_MINUTES_BEFORE_CLOSE = 10
# outcomes that are neither a success nor this bot's failure: never systemic, never counted per question
NOT_ATTEMPTED = ("skipped_budget", "stopped", "skipped_closing", "closed")
COMMENT_ATTEMPTS = 3
MAX_QUESTIONS_TO_FETCH = 2000
PROBE_TIMEOUT_S = 30  # v0.1.4 health probe: one tiny cheap-model call, at most PROBE_ATTEMPTS HTTP calls per run
PROBE_ATTEMPTS = 2
PROBE_TOKENS_IN, PROBE_TOKENS_OUT = 30, 16  # rough size of one probe call, for the cost estimate


def env_flag(name: str, env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return env.get(name, "") == "1"  # B5: only "1" is on


def tournaments_for_mode(mode: str) -> list[tuple[str, int | str]]:
    if mode == "tournament":
        return list(precheck.TOURNAMENTS)
    if mode == "test_questions":
        return [("bot-testing-area", "bot-testing-area")]
    raise ValueError(f"Unknown mode {mode}")


def _int_env(env: dict[str, str], name: str, default: int) -> int:
    raw = env.get(name, "")
    return int(raw) if raw else default


def max_forecasts_for(mode: str, publish: bool, env: dict[str, str]) -> int | None:
    """
    None = no cap (publishing: never skip a question).
      test_questions          -> TEST_MAX_QUESTIONS, default 3 (B7)
      tournament dry run      -> 0: only list questions (A1). A manual run
                                 with DRY_RUN_LLM=1 forecasts up to
                                 DRY_RUN_LLM_MAX_QUESTIONS (default 2).
    """
    if mode == "test_questions":
        return _int_env(env, "TEST_MAX_QUESTIONS", 3)
    if publish:
        return None
    if env.get("DRY_RUN_LLM", "") == "1" and env.get("GITHUB_EVENT_NAME", "") != "schedule":
        return _int_env(env, "DRY_RUN_LLM_MAX_QUESTIONS", 2)
    return 0


async def fetch_open_questions(client: Any, tournament_id: int | str) -> list[Any]:
    """All open questions, every page (the SDK helper only reads page one)."""
    from forecasting_tools import ApiFilter

    api_filter = ApiFilter(
        allowed_tournaments=[tournament_id],
        allowed_statuses=["open"],
        group_question_mode="unpack_subquestions",
    )
    return await client.get_questions_matching_filter(
        api_filter,
        num_questions=MAX_QUESTIONS_TO_FETCH,
        error_if_question_target_missed=False,
    )


def is_supported(question: Any) -> bool:
    from forecasting_tools import BinaryQuestion, DateQuestion, MultipleChoiceQuestion, NumericQuestion

    return isinstance(question, (BinaryQuestion, MultipleChoiceQuestion, NumericQuestion, DateQuestion))


_FATAL_PATTERNS = [
    (re.compile(r'"code"\s*:\s*(40[12])\b'), None),
    (re.compile(r"(?:status[_ ]?code|error code|HTTP)\D{0,3}(40[12])\b", re.I), None),
    (re.compile(r"requires more credits|insufficient credits|insufficient balance", re.I), 402),
    # v0.3.0 OpenAI quota / spend-limit codes (in report.errors text)
    (re.compile(r"insufficient_quota|spend_limit_exceeded|credit_balance_exhausted|usage_limit_exceeded"), 402),
]


def llm_fatal_status(err: Any) -> int | None:
    """401/402 (OpenRouter, or Anthropic incl. credit exhausted mapped to 402) anywhere in an exception
    (chain, group) or text (B1, B3)."""
    seen: set[int] = set()
    stack = [err]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, BaseException):
            code = getattr(e, "status_code", None)
            if code in (401, 402):
                return int(code)
            stack += list(getattr(e, "exceptions", ()) or ())
            stack += [e.__cause__, e.__context__]
            text = str(e)
        else:
            text = str(e)
        for pattern, fixed in _FATAL_PATTERNS:
            m = pattern.search(text)
            if m:
                return fixed or int(m.group(1))
    return None


def root_message(e: BaseException, limit: int = 200) -> str:
    """Innermost cause of an SDK error (it wraps everything in ExceptionGroups)."""
    while True:
        inner = getattr(e, "exceptions", None)
        if inner:
            e = inner[0]
        elif e.__cause__ is not None:
            e = e.__cause__
        else:
            break
    return f"{e.__class__.__name__}: {str(e)[:limit]}"


@dataclass
class Outcome:
    tournament: str
    title: str
    url: str
    status: str  # submitted | comment_failed | dry_run | failed | skipped_budget | stopped
    #   | skipped_closing (v0.3.1: closes within MIN_MINUTES_BEFORE_CLOSE, no LLM call) | closed (v0.3.1: closed while forecasting)
    detail: str = ""
    cost: float = 0.0
    fatal: bool = False  # run-wide 401/402 (or Anthropic credit exhausted): not this question's fault, never counted


@dataclass
class RunResult:
    version: str
    mode: str
    publish: bool
    decision: budget.BudgetDecision
    models: dict[str, tuple[str, str]] = field(default_factory=dict)
    pacing: budget.Pacing | None = None
    open_counts: dict[str, int] = field(default_factory=dict)
    already_forecasted: int = 0
    unsupported: int = 0
    not_attempted_cap: int = 0
    fetch_errors: list[str] = field(default_factory=list)
    fetch_auth_error: bool = False
    outcomes: list[Outcome] = field(default_factory=list)
    windows: list[tuple[str, str, str, str, str]] = field(default_factory=list)
    llm_needed: bool = False
    skipped_failing: list[dict[str, Any]] = field(default_factory=list)  # failed MAX runs in a row
    fail_limit: int = 0
    cooldown_hours: int = 0
    upstream_suspect: bool = False  # >=2 attempted, all failed, and the probe failed too -> counts rolled back
    probe: str = ""  # v0.1.4: "" = not run | "ok" (upstream fine, failures are the questions') | "failed" | "fatal"
    probe_error: str = ""
    probe_calls: int = 0
    probe_cost: float = 0.0
    fatal: str = ""  # 401/402 mid-run: everything stopped (B3)
    provider: str = "openrouter"  # v0.2.0: "anthropic" when running on the Anthropic API
    period: tuple[str, str] | None = None  # Anthropic credit period (start, next reset), ISO dates
    budget_total: float | None = None  # Anthropic budget per credit period
    usage_before: float | None = None
    usage_after: float | None = None
    remaining_after: float | None = None
    free_before: dict[str, tuple[int, int]] | None = None  # v0.3.0 OpenAI: group -> (free tokens used today, allowance)
    free_after: dict[str, tuple[int, int]] | None = None
    deferred: list[dict[str, Any]] = field(default_factory=list)  # OpenAI: free tokens used up, closes > 30 h away
    paid_overflow: int = 0  # OpenAI: questions answered past the free allowance (paid at standard rates)
    plan_reason: str = ""
    notes: list[str] = field(default_factory=list)  # v0.4.0: yellow notes (per-tournament provider fallback)
    forecast_keys: set[str] = field(default_factory=set)  # v0.4.0: stop-loss prune set of this run

    def count(self, *statuses: str) -> int:
        return sum(o.status in statuses for o in self.outcomes)

    @property
    def new_ok(self) -> int:
        return self.count("submitted", "comment_failed", "dry_run")

    @property
    def failed(self) -> int:
        return self.count("failed")

    @property
    def spend(self) -> float:
        if self.usage_before is not None and self.usage_after is not None:
            return max(0.0, self.usage_after - self.usage_before)
        return sum(o.cost for o in self.outcomes) + self.probe_cost

    def label(self) -> str:
        return budget.provider_label(self.provider)

    def fatal_text(self, status: int) -> str:
        return f"{self.label()} HTTP {status}: run stopped, nothing else attempted"

    @property
    def systemic(self) -> list[str]:
        """Reasons that turn the job red (B1). Single-question failures do not."""
        out = []
        if self.fatal:
            out.append(self.fatal)
        if self.llm_needed and self.decision.systemic:
            out.append(f"{self.label()}: {self.decision.reason}")
        if self.fetch_auth_error or (self.fetch_errors and not self.open_counts):
            out.append("could not load questions: " + "; ".join(self.fetch_errors))
        attempted = [o for o in self.outcomes if o.status not in NOT_ATTEMPTED]
        if attempted and all(o.status == "failed" for o in attempted):
            out.append(f"every attempted question failed ({len(attempted)})")
        published = [o for o in self.outcomes if o.status in ("submitted", "comment_failed")]
        if published and all(o.status == "comment_failed" for o in published):
            out.append("no comment could be sent")
        return out

    @property
    def exit_code(self) -> int:
        return 1 if self.systemic else 0


def _iso(d: datetime | None) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M") if d else "-"


def min_minutes_before_close(env: dict[str, str]) -> int:
    """MIN_MINUTES_BEFORE_CLOSE; unset, empty, not a number or < 0 -> 10. 0 only skips already-closed ones."""
    try:
        value = int(str(env.get("MIN_MINUTES_BEFORE_CLOSE", "")).strip())
    except ValueError:
        return DEFAULT_MIN_MINUTES_BEFORE_CLOSE
    return value if value >= 0 else DEFAULT_MIN_MINUTES_BEFORE_CLOSE


def is_closed_error(err: Any) -> bool:
    """The post was refused because the question closed meanwhile (HTTP 405 / 'closed to forecasting')."""
    text = str(err)
    code = getattr(getattr(err, "response", None), "status_code", None)
    return code == 405 or "closed to forecasting" in text.lower() or bool(re.search(r"\b405\b", text))


def _close_key(item: tuple[str, Any]) -> float:
    close = item[1].close_time
    return close.timestamp() if close else float("inf")


async def run(
    *,
    mode: str,
    publish: bool,
    bot_factory: Callable[[str, budget.Tier], Any],
    client: Any,
    key_lookup_fn: Callable[[], budget.KeyLookup],
    version: str,
    models: dict[str, tuple[str, str]],
    prices: dict[str, tuple[tuple[float, float], tuple[float, float]]],
    env: dict[str, str] | None = None,
    tournaments: list[tuple[str, int | str]] | None = None,
    concurrency: int = 3,
    store: Any = None,
    today: date | None = None,
    now: datetime | None = None,
    probe_fn: Callable[[], Awaitable[Any]] | None = None,
    provider: str | None = None,
    prune_failures: bool = True,
) -> RunResult:
    env = dict(os.environ) if env is None else env
    provider = provider or budget.select_provider(env)
    lookup = key_lookup_fn()
    decision = budget.decide(lookup, env)
    result = RunResult(version, mode, publish, decision, models, provider=provider)
    day = today or budget.utc_today()
    horizon = None
    if provider == "anthropic":
        start, horizon = budget.credit_period(day, budget.credit_reset_day(env))
        result.period = (start.isoformat(), horizon.isoformat())
        result.budget_total = budget.anthropic_budget(env)
    elif provider == "openai":  # v0.3.0: dollars per calendar month, free tokens per UTC day
        result.period = (day.replace(day=1).isoformat(), budget.month_end(day).isoformat())
        result.budget_total = budget.openai_budget(env)
        result.free_before = lookup.free
    result.usage_before = lookup.info.usage if lookup.info else None
    skip_forecasted = mode == "tournament"
    clock = now or datetime.now(timezone.utc)  # same day rule as the OpenAI plan: a pinned `today` wins
    if clock.date() != day:
        clock = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    closing_cutoff = clock + timedelta(minutes=min_minutes_before_close(env))
    closing: list[Outcome] = []

    to_forecast: list[tuple[str, Any]] = []
    seen: set[Any] = set()
    for name, tid in tournaments or tournaments_for_mode(mode):
        try:  # B12: one tournament failing does not block the other
            questions = await fetch_open_questions(client, tid)
        except Exception as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            result.fetch_auth_error |= code in (401, 403)
            result.fetch_errors.append(f"{name}: {e.__class__.__name__}: {str(e)[:200]}")
            continue
        result.open_counts[name] = len(questions)
        for q in questions:
            result.windows.append(
                (name, q.question_text[:80], q.page_url, _iso(q.open_time), _iso(q.close_time))
            )
            key = q.id_of_question or q.page_url
            if key in seen:
                continue
            seen.add(key)
            if skip_forecasted and q.already_forecasted:
                result.already_forecasted += 1
            elif not is_supported(q):
                result.unsupported += 1
            elif q.close_time is not None and clock < q.close_time <= closing_cutoff:  # v0.3.1: no LLM money on it
                closing.append(Outcome(name, q.question_text[:80], q.page_url, "skipped_closing",
                                       f"close {_iso(q.close_time)}"))
            else:
                to_forecast.append((name, q))

    to_forecast.sort(key=_close_key)  # closing soonest first
    if store is not None and mode == "tournament":
        to_forecast = _drop_failing(store, to_forecast, env, result, now, prune_failures)
    open_new = {k: 0 for k in budget.KINDS}
    for name, _ in to_forecast:
        open_new[budget.kind_of(name)] += 1
    if decision.remaining is not None and provider != "openai":
        result.pacing = budget.pace(decision.remaining, day, env, open_new, prices, horizon, provider)
    cap = max_forecasts_for(mode, publish, env)
    if cap is not None and len(to_forecast) > cap:
        result.not_attempted_cap = len(to_forecast) - cap
        to_forecast = to_forecast[:cap]
    if decision.remaining is not None and provider == "openai":
        to_forecast = _openai_plan(to_forecast, lookup, decision, day, env, result, mode, now, open_new)
    result.llm_needed = bool(to_forecast)

    if to_forecast and (not decision.may_call_llm or result.pacing is None):
        result.outcomes = [
            Outcome(n, q.question_text[:80], q.page_url, "skipped_budget", decision.reason)
            for n, q in to_forecast
        ]
    elif to_forecast:
        assert result.pacing is not None
        bots = {k: bot_factory(k, kp.tier) for k, kp in result.pacing.kinds.items()}
        sem = asyncio.Semaphore(concurrency)
        stop = asyncio.Event()

        async def one(name: str, q: Any) -> Outcome:
            async with sem:
                if store is not None and not stop.is_set():  # write-ahead (v0.1.3): the +1 hits the disk first
                    store.begin_attempt(_qkey(q), q.question_text, q.page_url, now)
                outcome = await forecast_one(
                    bots[budget.kind_of(name)], client, name, q, publish, key_lookup_fn, env, stop, result, store
                )
                _count_failure(store, name, q, outcome, now)
                return outcome

        result.outcomes = list(await asyncio.gather(*[one(n, q) for n, q in to_forecast]))
        if probe_fn is not None and _upstream_suspect(result.outcomes):
            await _health_probe(probe_fn, result, next(iter(prices.values()))[1], store, stop)

    result.outcomes += closing
    after = key_lookup_fn().info if (decision.may_call_llm and to_forecast) else lookup.info
    if after is not None:
        result.usage_after = after.usage
        result.remaining_after = after.remaining
    if provider == "openai":
        result.free_after = key_lookup_fn().free if (decision.may_call_llm and to_forecast) else lookup.free
    return result


def _openai_plan(to_forecast: list[tuple[str, Any]], lookup: budget.KeyLookup, decision: budget.BudgetDecision,
                 day: date, env: dict[str, str], result: RunResult, mode: str, now: datetime | None,
                 open_new: dict[str, int]) -> list[tuple[str, Any]]:
    """v0.3.0: tier from today's free big-group tokens; with the allowance used up, questions closing
    more than 30 h from now wait for a later run (tournament mode only), sooner ones run paid."""
    clock = now or datetime.now(timezone.utc)
    if clock.date() != day:
        clock = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    plan = budget.openai_plan([(budget.kind_of(n), q.close_time) for n, q in to_forecast], lookup.free, clock,
                              env, defer=mode == "tournament")
    result.pacing = budget.openai_pacing(plan, decision.remaining or 0.0, day, env, open_new)
    result.plan_reason, result.paid_overflow = plan.reason, len(plan.paid)
    gone = set(plan.deferred)
    for i in plan.deferred:
        n, q = to_forecast[i]
        result.deferred.append({"id": _qkey(q), "title": q.question_text, "url": q.page_url,
                                "close": _iso(q.close_time)})
    return [item for i, item in enumerate(to_forecast) if i not in gone]


def _qkey(q: Any) -> str:
    return state_mod.question_key(q.id_of_question, q.id_of_post)


async def _health_probe(probe_fn: Callable[[], Awaitable[Any]], result: RunResult,
                        cheap_price: tuple[float, float], store: Any, stop: asyncio.Event) -> None:
    """v0.1.4: >=2 questions all failed, so ask the cheapest model for one tiny answer (30 s, one retry).
    ok -> the questions are at fault, counts stay | 401/402 -> fatal, nothing counts | other failure -> outage, nothing counts."""
    errors: list[str] = []
    for _ in range(PROBE_ATTEMPTS):
        result.probe_calls += 1
        result.probe_cost += cheap_price[0] * PROBE_TOKENS_IN + cheap_price[1] * PROBE_TOKENS_OUT
        try:
            await asyncio.wait_for(probe_fn(), PROBE_TIMEOUT_S)
            result.probe = "ok"
            return
        except Exception as e:  # includes asyncio.TimeoutError
            errors.append(root_message(e) if str(e) else e.__class__.__name__)
            logger.warning(f"Health probe failed: {errors[-1]}")
            status = llm_fatal_status(e)
            if status:
                result.probe, result.probe_error = "fatal", errors[-1]
                result.fatal = result.fatal or result.fatal_text(status)
                stop.set()
                if store is not None:
                    store.rollback_all()
                return
    result.probe, result.probe_error = "failed", " | ".join(errors)[:300]
    result.upstream_suspect = True
    if store is not None:
        store.rollback_all()


def _upstream_suspect(outcomes: list[Outcome]) -> bool:
    """>=2 questions really attempted and every one failed without a 401/402: only a reason to run the health probe."""
    attempted = [o for o in outcomes if o.status not in NOT_ATTEMPTED]
    return len(attempted) >= 2 and all(o.status == "failed" and not o.fatal for o in attempted)


def _drop_failing(store: Any, to_forecast: list[tuple[str, Any]], env: dict[str, str],
                  result: RunResult, now: datetime | None = None, prune: bool = True) -> list[tuple[str, Any]]:
    """Stop-loss: questions that failed MAX_QUESTION_FAILED_RUNS runs in a row get no LLM call,
    until SKIP_COOLDOWN_HOURS after their last attempt (then one more try)."""
    result.forecast_keys = {_qkey(q) for _, q in to_forecast}
    if prune and not result.fetch_errors:  # a tournament we could not read must not wipe its counts
        store.prune_failures({_qkey(q) for _, q in to_forecast})
    result.fail_limit = state_mod.max_failed_runs(env)
    result.cooldown_hours = state_mod.skip_cooldown_hours(env)
    waiting, _due = store.split_skipped(result.fail_limit, result.cooldown_hours, now)
    kept = []
    for name, q in to_forecast:
        item = waiting.get(_qkey(q))
        if item is None:
            kept.append((name, q))
        else:
            result.skipped_failing.append(
                {"id": _qkey(q), "title": q.question_text, "url": q.page_url,
                 "count": int(item["count"]), "last_error": item.get("last_error", ""),
                 "next_retry": item["next_retry"]}
            )
    return kept


def _count_failure(store: Any, tournament: str, q: Any, outcome: Outcome, now: datetime | None = None) -> None:
    """The +1 was written before the attempt. A forecast = reset; a real failure keeps it and records why;
    401/402, stopped and skipped_budget take it back."""
    if store is None:
        return
    key = _qkey(q)
    if outcome.status == "failed" and not outcome.fatal:
        store.finish_failure(key, outcome.detail.split(" | ")[-1], now)
    elif outcome.status in ("submitted", "comment_failed", "dry_run"):
        store.clear_failure(key)
    else:  # fatal failed, stopped, skipped_budget, closed (v0.3.1)
        store.rollback(key)


class _ForecastOnlyClient:
    """Lets the SDK post the forecast exactly as it would, but holds back the
    comment so it can be sent (and retried, and stored) separately (A3)."""

    def __init__(self, real: Any):
        self._real = real
        self.comment: tuple[Any, ...] | None = None

    def post_question_comment(self, *args: Any, **kwargs: Any) -> None:
        self.comment = args

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


async def forecast_one(
    bot: Any,
    client: Any,
    tournament: str,
    question: Any,
    publish: bool,
    key_lookup_fn: Callable[[], budget.KeyLookup],
    env: dict[str, str],
    stop: asyncio.Event,
    result: RunResult,
    store: Any = None,
) -> Outcome:
    title, url = question.question_text[:80], question.page_url
    if stop.is_set():
        return Outcome(tournament, title, url, "stopped", result.fatal)
    live = budget.decide(key_lookup_fn(), env)
    if not live.may_call_llm:
        return Outcome(tournament, title, url, "skipped_budget", live.reason)

    report = None
    errors: list[str] = []
    for attempt in range(1 + PER_QUESTION_RETRIES):
        if stop.is_set():
            return Outcome(tournament, title, url, "stopped", result.fatal)
        bot.guard_notes.pop(bot.question_key(question), None)
        try:
            report = (await bot.forecast_questions([question], return_exceptions=False))[0]
            break
        except Exception as e:
            errors.append(f"attempt {attempt + 1}: {root_message(e)}")
            logger.warning(f"Forecast failed for {url}: {errors[-1]}")
            status = llm_fatal_status(e)
            if status:  # B3: no retry, the whole run stops
                result.fatal = result.fatal or result.fatal_text(status)
                stop.set()
                return Outcome(tournament, title, url, "failed", " | ".join(errors), fatal=True)
    if report is None:
        return Outcome(tournament, title, url, "failed", " | ".join(errors))
    status = llm_fatal_status(" ".join(report.errors or []))
    if status:  # some forecasters hit 401/402 but enough succeeded: keep this one, stop the rest
        result.fatal = result.fatal or result.fatal_text(status)
        stop.set()

    cost = report.price_estimate or 0.0
    if not publish:
        return Outcome(tournament, title, url, "dry_run", "", cost)
    return await publish_one(report, client, tournament, store, cost)


async def publish_one(report: Any, client: Any, tournament: str, store: Any, cost: float) -> Outcome:
    q = report.question
    title, url, post_id = q.question_text[:80], q.page_url, q.id_of_post
    errors: list[str] = []
    if store is not None:
        store.add(post_id, report.explanation, url)  # write-ahead (A3)
    held = _ForecastOnlyClient(client)
    for attempt in range(PUBLISH_ATTEMPTS):
        try:
            await report.publish_report_to_metaculus(metaculus_client=held)
            break
        except Exception as e:
            errors.append(f"forecast {attempt + 1}: {e.__class__.__name__}: {str(e)[:150]}")
            logger.warning(f"Publishing forecast failed for {url}: {errors[-1]}")
            if is_closed_error(e):  # v0.3.1: retrying a closed question cannot work
                if store is not None:
                    store.remove(post_id)
                return Outcome(tournament, title, url, "closed", " | ".join(errors), cost)
    else:
        if store is not None:
            store.remove(post_id)
        return Outcome(tournament, title, url, "failed", " | ".join(errors), cost)

    text = held.comment[1] if held.comment else report.explanation
    for attempt in range(COMMENT_ATTEMPTS):
        try:
            client.post_question_comment(post_id, text)
            if store is not None:
                store.remove(post_id)
            return Outcome(tournament, title, url, "submitted", "", cost)
        except Exception as e:
            errors.append(f"comment {attempt + 1}: {e.__class__.__name__}: {str(e)[:150]}")
            logger.warning(f"Posting comment failed for {url}: {errors[-1]}")
    if store is not None:
        store.mark_attempt(post_id, errors[-1])
    return Outcome(tournament, title, url, "comment_failed", " | ".join(errors), cost)


def _money(x: float | None) -> str:
    return "n/a" if x is None else f"${x:.2f}"


def _block(kind: str, text: str) -> list[str]:
    return ["", f"> [!{kind}]", f"> {text}", ""]


def _asknews() -> str:
    return "on" if os.getenv("ASKNEWS_CLIENT_ID") and os.getenv("ASKNEWS_SECRET") else "off"


def render_skipped(skipped: list[dict[str, Any]], limit: int | None = None, cooldown: int | None = None) -> list[str]:
    """Yellow block: questions skipped after repeated failures (id, title, count, last error)."""
    if not skipped:
        return []
    head = f"**{len(skipped)} 題連續失敗，已停止重試（不再呼叫 AI，省錢）**"
    if limit:
        head += (f"；失敗 {limit} 輪就停，冷卻 {cooldown} 小時後再試一次"
                 f"（MAX_QUESTION_FAILED_RUNS／SKIP_COOLDOWN_HOURS），題目截止後自動消失")
    rows = ["> - " + f"{s['id']}｜{str(s['title'])[:60].replace(chr(10), ' ')}｜失敗 {s['count']} 次｜"
            f"{' '.join(str(s.get('last_error', '')).split())[:200]}｜下次重試 {s.get('next_retry', '-')} UTC"
            for s in skipped]
    return ["", "> [!WARNING]", f"> {head}", *rows, ""]


def render_summary(r: RunResult, with_header: bool = True, section: str | None = None) -> str:
    """section (v0.4.0): one tournament group inside a split summary -> `### section` heading; the
    header, mode line, red blocks, stop-loss list and question listing are left to the split summary."""
    if section is not None:
        lines: list[str] = ["", f"### {section}", ""]
    else:
        lines = [precheck.summary_header(r.publish), ""] if with_header else []
        lines += [f"## forecast-bot v{r.version}：預測", ""]
        lines.append(f"- 模式：`{r.mode}`" + ("" if r.publish else "（DRY RUN，什麼都不送）"))
    if r.models:
        cheap = next(iter(r.models.values()))[1]
        per = []
        for k, (fid, _) in r.models.items():
            n = f" x{r.pacing.kinds[k].tier.forecasts}" if r.pacing else ""
            per.append(f"{k} `{fid}`{n}")
        lines.append(f"- 模型：{'；'.join(per)}；研究／解析 `{cheap}`＋原生搜尋；AskNews {_asknews()}")
    for reason in (r.systemic if section is None else []):
        lines += _block("CAUTION", f"**停止／系統性問題**：{reason}")
    for note in r.notes:
        lines += _block("WARNING", note)
    if r.upstream_suspect:
        lines += _block("CAUTION", "**疑似上游故障（探測失敗），本輪不計失敗**：這輪試了 "
                        f"{r.failed} 題全部失敗，健康探測也失敗（{r.probe_error}），失敗次數沒有累計，下一輪照常重試。")
    elif r.probe == "ok":
        lines += _block("WARNING", f"**上游正常，失敗算題目的**：這輪試了 {r.failed} 題全部失敗，"
                        "但探測呼叫成功，所以這輪的失敗照常計數（連續失敗會被跳過）。")
    if r.decision.warning:
        lines += _block("WARNING", r.decision.warning)
    if r.fetch_errors and r.open_counts and not r.fetch_auth_error:
        for err in r.fetch_errors:
            lines += _block("WARNING", f"**抓題失敗（另一邊照跑）**：{err}")
    if with_header and section is None:  # with a precheck step above, it already lists these
        lines += render_skipped(r.skipped_failing, r.fail_limit, r.cooldown_hours)
    comment_failed = r.count("comment_failed")
    if comment_failed:
        lines += _block("CAUTION", f"**{comment_failed} 題的說明（comment）沒送出去**，預測已送；下一輪會自動補送。")
    lines.append(f"- Provider：{r.provider}")
    if r.provider == "anthropic" and r.period:
        lines.append(f"- Anthropic 本期花費（{r.period[0]} 起，{r.period[1]} 重置，本機記帳）：{_money(r.usage_after)}"
                     f"／預算 {_money(r.budget_total)}；剩餘 {_money(r.remaining_after)}")
    if r.provider == "openai" and r.period:
        lines += precheck.openai_lines(r.free_after or r.free_before, r.period, r.usage_after, r.budget_total)
        if r.plan_reason:
            lines.append(f"- 免費額度配速：{r.plan_reason}" + (f"；超出免費額度、付費作答 {r.paid_overflow} 題" if r.paid_overflow else ""))
        if r.deferred:
            lines += _block("WARNING", f"**今天的免費 token 用完，{len(r.deferred)} 題延後**（截止還超過 30 小時，"
                            "等 00:00 UTC 額度重置後的輪次再做）：" + "；".join(
                                f"[{str(d['title'])[:50]}]({d['url']})（截止 {d['close']}）" for d in r.deferred[:10]))
    lines.append(f"- {r.label()}：{r.decision.state}（{r.decision.reason}）")
    if r.pacing:
        parts = [f"{k} {_money(v.per_question_budget)}／題 -> {v.tier.name}" for k, v in r.pacing.kinds.items()]
        run_out = r.pacing.run_out.isoformat() if r.pacing.run_out else "n/a"
        lines.append(f"- 配速：{'；'.join(parts)}；預估見底日 {run_out}")
    counts = "、".join(f"{k} {v}" for k, v in r.open_counts.items()) or "無"
    lines += [
        f"- 開放題：{sum(r.open_counts.values())}（{counts}）",
        f"- 已預測跳過：{r.already_forecasted}；不支援題型跳過：{r.unsupported}",
        f"- 這輪新交：{r.new_ok}" + ("" if r.publish else "（dry run，沒送）"),
        f"- 失敗：{r.failed}",
        f"- 說明沒送出：{comment_failed}",
        f"- 臨截止跳過（不花 LLM）：{r.count('skipped_closing')}；作答期間已截止（沒送出）：{r.count('closed')}",
        f"- 預算不足跳過：{r.count('skipped_budget')}；{r.label()} 停止後沒做：{r.count('stopped')}",
    ]
    if r.not_attempted_cap:
        lines.append(f"- 這輪上限外沒做：{r.not_attempted_cap}")
    lines += [
        f"- 這輪估計花費：{_money(r.spend)}" + (f"（含探測 {r.probe_calls} 次）" if r.probe_calls else ""),
        f"- {r.label()} 剩餘：{_money(r.remaining_after)}",
    ]
    bad = [o for o in r.outcomes if o.status in ("failed", "comment_failed")]
    sub = "####" if section is not None else "###"
    if bad:
        lines += ["", f"{sub} 失敗的題目"]
        lines += [f"- [{o.title}]({o.url})（{o.status}）：{o.detail}" for o in bad]
    done = [o for o in r.outcomes if o.status in ("submitted", "comment_failed", "dry_run")]
    if done:
        lines += ["", f"{sub} 這輪預測的題目"]
        lines += [f"- [{o.title}]({o.url})（{o.tournament}，約 {_money(o.cost)}）" for o in done]
    if with_header and section is None:  # no precheck listing above (Test Bot): print open/close times here (C4)
        lines += _windows(r.windows)
    return "\n".join(lines) + "\n"


def _windows(windows: list[tuple[str, str, str, str, str]]) -> list[str]:
    if not windows:
        return []
    lines = ["", "<details><summary>開放中的題目（開放／截止時間，UTC）</summary>", "",
             "| tournament | question | opens | closes |", "|---|---|---|---|"]
    for t, title, url, op, cl in windows:
        lines.append(f"| {t} | [{title.replace('|', '/')}]({url}) | {op} | {cl} |")
    return lines + ["", "</details>"]


def render_crash(version: str, publish: bool, reason: str, with_header: bool = True) -> str:
    lines = [precheck.summary_header(publish), ""] if with_header else []
    lines += [f"## forecast-bot v{version}：預測", ""]
    lines += _block("CAUTION", f"**這輪沒跑完**：{reason}")
    return "\n".join(lines) + "\n"


def write_summary(text: str, env: dict[str, str] | None = None) -> None:
    env = os.environ if env is None else env
    print(text)
    path = env.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


# ------------------------------------------------------- v0.4.0 per-tournament provider


@dataclass
class Group:
    """One tournament kind on its own provider: own budget, pacing, ledger, stop event and probe."""
    kind: str
    provider: str
    env: dict[str, str]  # budget.provider_env(env, provider, [kind])
    tournaments: list[tuple[str, int | str]]
    key_lookup_fn: Callable[[], budget.KeyLookup]
    bot_factory: Callable[[str, budget.Tier], Any]
    models: dict[str, tuple[str, str]]
    prices: dict[str, tuple[tuple[float, float], tuple[float, float]]]
    probe_fn: Callable[[], Awaitable[Any]] | None = None


def provider_problems(r: RunResult) -> list[str]:
    """Run-wide provider trouble of one group: 401/402 mid-run, or no usable budget when LLM work was due."""
    out = [r.fatal] if r.fatal else []
    if r.llm_needed and r.decision.systemic:
        out.append(f"{r.label()}: {r.decision.reason}")
    return out


@dataclass
class SplitResult:
    version: str
    mode: str
    publish: bool
    groups: list[RunResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    crashes: list[str] = field(default_factory=list)  # a group whose run raised (a bug, not a provider error)

    @property
    def outcomes(self) -> list[Outcome]:
        return [o for r in self.groups for o in r.outcomes]

    @property
    def healthy(self) -> bool:
        """At least one group ran without provider trouble: the others' trouble is then only yellow."""
        return any(not provider_problems(r) for r in self.groups)

    @property
    def demoted(self) -> list[tuple[RunResult, str]]:
        if not self.healthy:
            return []
        return [(r, p) for r in self.groups for p in provider_problems(r)]

    @property
    def systemic(self) -> list[str]:
        out = list(self.crashes)
        if not self.healthy:
            out += [p for r in self.groups for p in provider_problems(r)]
        errors = [e for r in self.groups for e in r.fetch_errors]
        opened = any(r.open_counts for r in self.groups)
        if any(r.fetch_auth_error for r in self.groups) or (errors and not opened):
            out.append("could not load questions: " + "; ".join(errors))
        # a group's run-wide 401/402 is judged above, not as "every question failed"
        attempted = [o for o in self.outcomes if o.status not in NOT_ATTEMPTED and not o.fatal]
        if attempted and all(o.status == "failed" for o in attempted):
            out.append(f"every attempted question failed ({len(attempted)})")
        published = [o for o in self.outcomes if o.status in ("submitted", "comment_failed")]
        if published and all(o.status == "comment_failed" for o in published):
            out.append("no comment could be sent")
        return out

    @property
    def exit_code(self) -> int:
        return 1 if self.systemic else 0


def _fresh_write_ahead(store: Any) -> None:
    """A group's stop-loss rollback (fatal / outage probe) must only take back its own +1s."""
    wa = getattr(store, "_wa", None)
    if isinstance(wa, dict):
        wa.clear()


async def run_split(*, mode: str, publish: bool, groups: list[Group], client: Any, version: str,
                    store: Any = None, today: date | None = None, now: datetime | None = None,
                    concurrency: int = 3, notes: list[str] | None = None) -> SplitResult:
    """Groups run one after another (main first), each a full `run` on its own provider."""
    split = SplitResult(version, mode, publish, notes=list(notes or []))
    for g in groups:
        if store is not None:
            _fresh_write_ahead(store)
        try:
            r = await run(mode=mode, publish=publish, bot_factory=g.bot_factory, client=client,
                          key_lookup_fn=g.key_lookup_fn, version=version, models=g.models, prices=g.prices,
                          env=g.env, tournaments=g.tournaments, concurrency=concurrency, store=store,
                          today=today, now=now, probe_fn=g.probe_fn, provider=g.provider, prune_failures=False)
        except Exception as e:
            split.crashes.append(f"{g.kind}（{g.provider}）沒跑完：{e.__class__.__name__}: {str(e)[:300]}")
            continue
        split.groups.append(r)
    if store is not None and mode == "tournament" and split.groups and not split.crashes \
            and not any(r.fetch_errors for r in split.groups):
        store.prune_failures(set().union(*(r.forecast_keys for r in split.groups)))
    return split


def render_split_summary(s: SplitResult, with_header: bool = True) -> str:
    lines: list[str] = [precheck.summary_header(s.publish), ""] if with_header else []
    lines += [f"## forecast-bot v{s.version}：預測", ""]
    lines.append(f"- 模式：`{s.mode}`" + ("" if s.publish else "（DRY RUN，什麼都不送）"))
    lines.append("- 各賽事分開跑：" + "；".join(f"{'、'.join(r.models)} {r.provider}" for r in s.groups))
    for reason in s.systemic:
        lines += _block("CAUTION", f"**停止／系統性問題**：{reason}")
    for r, reason in s.demoted:
        lines += _block("WARNING", f"**{'、'.join(r.models)}（{r.label()}）這輪停了，另一個賽事照跑**：{reason}")
    for note in s.notes:
        lines += _block("WARNING", note)
    if with_header and s.groups:
        skipped = [x for r in s.groups for x in r.skipped_failing]
        lines += render_skipped(skipped, s.groups[0].fail_limit, s.groups[0].cooldown_hours)
    for r in s.groups:
        lines += render_summary(r, with_header=False, section=f"{'、'.join(r.models)}：{r.provider}").rstrip("\n").split("\n")
    if with_header:
        lines += _windows([w for r in s.groups for w in r.windows])
    return "\n".join(lines) + "\n"
