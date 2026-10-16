"""
One bot run: budget check -> fetch every open question -> forecast the new
ones -> (optionally) publish -> write the GitHub job summary.

Everything with side effects is injected, so tests run fully offline.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import budget

logger = logging.getLogger(__name__)

PER_QUESTION_RETRIES = 2  # extra attempts after the first one
PUBLISH_ATTEMPTS = 3
MAX_QUESTIONS_TO_FETCH = 2000


def env_flag(name: str, env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return env.get(name, "").strip() == "1"


def tournaments_for_mode(mode: str) -> list[tuple[str, int | str]]:
    from forecasting_tools import MetaculusClient

    if mode == "tournament":
        return [
            ("FutureEval Fall 2026", MetaculusClient.CURRENT_AI_COMPETITION_ID),
            ("MiniBench", MetaculusClient.CURRENT_MINIBENCH_ID),
        ]
    if mode == "test_questions":
        return [("bot-testing-area", "bot-testing-area")]
    raise ValueError(f"Unknown mode {mode}")


def max_forecasts_for(mode: str, publish: bool, env: dict[str, str]) -> int | None:
    """
    None = no cap. Defaults:
      tournament + publish   -> no cap (never skip a question)
      tournament + dry run   -> 0 (scheduled dry runs only list questions;
                                otherwise they would re-forecast the same
                                questions every hour and burn credits)
      test_questions         -> 3
    Override with MAX_FORECASTS_PER_RUN.
    """
    raw = env.get("MAX_FORECASTS_PER_RUN", "").strip()
    if raw:
        return int(raw)
    if mode == "test_questions":
        return 3
    return None if publish else 0


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


@dataclass
class Outcome:
    tournament: str
    title: str
    url: str
    status: str  # submitted | dry_run | failed | skipped_budget
    detail: str = ""
    cost: float = 0.0


@dataclass
class RunResult:
    version: str
    mode: str
    publish: bool
    models: str
    decision: budget.BudgetDecision
    open_counts: dict[str, int] = field(default_factory=dict)
    already_forecasted: int = 0
    not_attempted_cap: int = 0
    fetch_errors: list[str] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)
    windows: list[tuple[str, str, str, str, str]] = field(default_factory=list)
    usage_before: float | None = None
    usage_after: float | None = None
    remaining_after: float | None = None

    @property
    def new_ok(self) -> int:
        return sum(o.status in ("submitted", "dry_run") for o in self.outcomes)

    @property
    def failed(self) -> int:
        return sum(o.status == "failed" for o in self.outcomes)

    @property
    def spend(self) -> float:
        if self.usage_before is not None and self.usage_after is not None:
            return max(0.0, self.usage_after - self.usage_before)
        return sum(o.cost for o in self.outcomes)

    @property
    def exit_code(self) -> int:
        if self.decision.red or self.fetch_errors:
            return 1
        attempted = [o for o in self.outcomes if o.status != "skipped_budget"]
        if attempted and all(o.status == "failed" for o in attempted):
            return 1
        return 0


def _iso(d: datetime | None) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M") if d else "-"


def _close_key(item: tuple[str, Any]) -> float:
    close = item[1].close_time
    return close.timestamp() if close else float("inf")


async def run(
    *,
    mode: str,
    publish: bool,
    bot_factory: Callable[[bool], Any],
    client: Any,
    key_info_fn: Callable[[], budget.KeyInfo | None],
    version: str,
    models_label: str,
    env: dict[str, str] | None = None,
    tournaments: list[tuple[str, int | str]] | None = None,
    concurrency: int = 3,
) -> RunResult:
    env = dict(os.environ) if env is None else env
    info = key_info_fn()
    decision = budget.decide(info, env)
    result = RunResult(version, mode, publish, models_label, decision)
    result.usage_before = info.usage if info else None
    skip_forecasted = mode == "tournament"

    to_forecast: list[tuple[str, Any]] = []
    seen: set[Any] = set()
    for name, tid in tournaments or tournaments_for_mode(mode):
        try:
            questions = await fetch_open_questions(client, tid)
        except Exception as e:
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
                continue
            to_forecast.append((name, q))

    to_forecast.sort(key=_close_key)  # closing soonest first
    cap = max_forecasts_for(mode, publish, env)
    if cap is not None and len(to_forecast) > cap:
        result.not_attempted_cap = len(to_forecast) - cap
        to_forecast = to_forecast[:cap]

    if not decision.may_call_llm:
        result.outcomes = [
            Outcome(n, q.question_text[:80], q.page_url, "skipped_budget", decision.reason)
            for n, q in to_forecast
        ]
    elif to_forecast:
        bot = bot_factory(decision.state == "economy")
        sem = asyncio.Semaphore(concurrency)

        async def one(name: str, q: Any) -> Outcome:
            async with sem:
                return await forecast_one(bot, client, name, q, publish, key_info_fn, env)

        result.outcomes = list(await asyncio.gather(*[one(n, q) for n, q in to_forecast]))

    after = key_info_fn() if (decision.may_call_llm and to_forecast) else info
    if after is not None:
        result.usage_after = after.usage
        result.remaining_after = after.remaining
    return result


async def forecast_one(
    bot: Any,
    client: Any,
    tournament: str,
    question: Any,
    publish: bool,
    key_info_fn: Callable[[], budget.KeyInfo | None],
    env: dict[str, str],
) -> Outcome:
    title, url = question.question_text[:80], question.page_url
    live = budget.decide(key_info_fn(), env)
    if not live.may_call_llm:
        return Outcome(tournament, title, url, "skipped_budget", live.reason)

    report = None
    errors: list[str] = []
    for attempt in range(1 + PER_QUESTION_RETRIES):
        bot.guard_notes.pop(bot.question_key(question), None)
        try:
            report = (await bot.forecast_questions([question], return_exceptions=False))[0]
            break
        except Exception as e:
            errors.append(f"attempt {attempt + 1}: {e.__class__.__name__}: {str(e)[:150]}")
            logger.warning(f"Forecast failed for {url}: {errors[-1]}")
    if report is None:
        return Outcome(tournament, title, url, "failed", " | ".join(errors))

    if not publish:
        return Outcome(tournament, title, url, "dry_run", "", report.price_estimate or 0.0)

    for attempt in range(PUBLISH_ATTEMPTS):
        try:
            await report.publish_report_to_metaculus(metaculus_client=client)
            return Outcome(tournament, title, url, "submitted", "", report.price_estimate or 0.0)
        except Exception as e:
            errors.append(f"publish {attempt + 1}: {e.__class__.__name__}: {str(e)[:150]}")
            logger.warning(f"Publish failed for {url}: {errors[-1]}")
    return Outcome(tournament, title, url, "failed", " | ".join(errors), report.price_estimate or 0.0)


def _money(x: float | None) -> str:
    return "n/a" if x is None else f"${x:.2f}"


def render_summary(r: RunResult) -> str:
    lines: list[str] = [f"## forecast-bot v{r.version}", ""]
    mode_line = "PUBLISH (forecasts and comments are sent)" if r.publish else "DRY RUN (nothing is sent to Metaculus)"
    lines.append(f"- Mode: `{r.mode}`, {mode_line}")
    lines.append(f"- Models: {r.models}")
    if r.decision.red:
        lines += [
            "",
            "> [!CAUTION]",
            f"> **BUDGET STOP ({r.decision.state})**: {r.decision.reason}. No LLM calls were made.",
            "",
        ]
    else:
        lines.append(f"- Budget: {r.decision.state} ({r.decision.reason})")
    for err in r.fetch_errors:
        lines += ["", "> [!CAUTION]", f"> **Could not load questions**: {err}", ""]
    total_open = sum(r.open_counts.values())
    counts = ", ".join(f"{k}: {v}" for k, v in r.open_counts.items()) or "none"
    lines += [
        f"- Open questions: {total_open} ({counts})",
        f"- Already forecasted (skipped): {r.already_forecasted}",
        f"- New forecasts this run: {r.new_ok}" + (" (dry run, not sent)" if not r.publish else ""),
        f"- Failed: {r.failed}",
        f"- Skipped for budget: {sum(o.status == 'skipped_budget' for o in r.outcomes)}",
    ]
    if r.not_attempted_cap:
        lines.append(f"- Not attempted (per-run cap): {r.not_attempted_cap}")
    lines += [
        f"- Estimated spend this run: {_money(r.spend)}",
        f"- OpenRouter key remaining: {_money(r.remaining_after)}",
    ]
    if r.failed:
        lines += ["", "### Failed questions"]
        lines += [f"- [{o.title}]({o.url}): {o.detail}" for o in r.outcomes if o.status == "failed"]
    done = [o for o in r.outcomes if o.status in ("submitted", "dry_run")]
    if done:
        lines += ["", "### Forecasted"]
        lines += [f"- [{o.title}]({o.url}) ({o.tournament}, ~{_money(o.cost)})" for o in done]
    if not r.publish and r.windows:
        lines += ["", "### Open questions (open / close time, UTC)", "", "| tournament | question | opens | closes |", "|---|---|---|---|"]
        for t, title, url, op, cl in r.windows:
            safe = title.replace("|", "/")
            lines.append(f"| {t} | [{safe}]({url}) | {op} | {cl} |")
    return "\n".join(lines) + "\n"


def write_summary(text: str, env: dict[str, str] | None = None) -> None:
    env = os.environ if env is None else env
    print(text)
    path = env.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
