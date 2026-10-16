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
from datetime import date, datetime, timezone
from typing import Any, Callable

import budget
import precheck
import state as state_mod

logger = logging.getLogger(__name__)

PER_QUESTION_RETRIES = 2  # extra attempts within one run; across runs only state.py's failure count (stop-loss)
PUBLISH_ATTEMPTS = 3
COMMENT_ATTEMPTS = 3
MAX_QUESTIONS_TO_FETCH = 2000


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
]


def llm_fatal_status(err: Any) -> int | None:
    """401/402 from OpenRouter anywhere in an exception (chain, group) or text (B1, B3)."""
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
    detail: str = ""
    cost: float = 0.0
    fatal: bool = False  # run-wide OpenRouter 401/402: not this question's fault, never counted


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
    fatal: str = ""  # OpenRouter 401/402 mid-run: everything stopped (B3)
    usage_before: float | None = None
    usage_after: float | None = None
    remaining_after: float | None = None

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
        return sum(o.cost for o in self.outcomes)

    @property
    def systemic(self) -> list[str]:
        """Reasons that turn the job red (B1). Single-question failures do not."""
        out = []
        if self.fatal:
            out.append(self.fatal)
        if self.llm_needed and self.decision.systemic:
            out.append(f"OpenRouter: {self.decision.reason}")
        if self.fetch_auth_error or (self.fetch_errors and not self.open_counts):
            out.append("could not load questions: " + "; ".join(self.fetch_errors))
        attempted = [o for o in self.outcomes if o.status not in ("skipped_budget", "stopped")]
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
) -> RunResult:
    env = dict(os.environ) if env is None else env
    lookup = key_lookup_fn()
    decision = budget.decide(lookup, env)
    result = RunResult(version, mode, publish, decision, models)
    result.usage_before = lookup.info.usage if lookup.info else None
    skip_forecasted = mode == "tournament"

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
            else:
                to_forecast.append((name, q))

    to_forecast.sort(key=_close_key)  # closing soonest first
    if store is not None and mode == "tournament":
        to_forecast = _drop_failing(store, to_forecast, env, result)
    if decision.remaining is not None:
        open_new = {k: 0 for k in budget.KINDS}
        for name, _ in to_forecast:
            open_new[budget.kind_of(name)] += 1
        result.pacing = budget.pace(decision.remaining, today or budget.utc_today(), env, open_new, prices)
    cap = max_forecasts_for(mode, publish, env)
    if cap is not None and len(to_forecast) > cap:
        result.not_attempted_cap = len(to_forecast) - cap
        to_forecast = to_forecast[:cap]
    result.llm_needed = bool(to_forecast)

    if to_forecast and (not decision.may_call_llm or result.pacing is None):
        result.outcomes = [
            Outcome(n, q.question_text[:80], q.page_url, "skipped_budget", decision.reason)
            for n, q in to_forecast
        ]
    elif to_forecast:
        assert result.pacing is not None
        bots = {k: bot_factory(k, result.pacing.kinds[k].tier) for k in budget.KINDS}
        sem = asyncio.Semaphore(concurrency)
        stop = asyncio.Event()

        async def one(name: str, q: Any) -> Outcome:
            async with sem:
                outcome = await forecast_one(
                    bots[budget.kind_of(name)], client, name, q, publish, key_lookup_fn, env, stop, result, store
                )
                _count_failure(store, name, q, outcome)
                return outcome

        result.outcomes = list(await asyncio.gather(*[one(n, q) for n, q in to_forecast]))

    after = key_lookup_fn().info if (decision.may_call_llm and to_forecast) else lookup.info
    if after is not None:
        result.usage_after = after.usage
        result.remaining_after = after.remaining
    return result


def _qkey(q: Any) -> str:
    return state_mod.question_key(q.id_of_question, q.id_of_post)


def _drop_failing(store: Any, to_forecast: list[tuple[str, Any]], env: dict[str, str],
                  result: RunResult) -> list[tuple[str, Any]]:
    """Stop-loss: questions that failed MAX_QUESTION_FAILED_RUNS runs in a row get no LLM call."""
    if not result.fetch_errors:  # a tournament we could not read must not wipe its counts
        store.prune_failures({_qkey(q) for _, q in to_forecast})
    result.fail_limit = state_mod.max_failed_runs(env)
    dead = store.skipped(result.fail_limit)
    kept = []
    for name, q in to_forecast:
        item = dead.get(_qkey(q))
        if item is None:
            kept.append((name, q))
        else:
            result.skipped_failing.append(
                {"id": _qkey(q), "title": q.question_text, "url": q.page_url,
                 "count": int(item["count"]), "last_error": item.get("last_error", "")}
            )
    return kept


def _count_failure(store: Any, tournament: str, q: Any, outcome: Outcome) -> None:
    """No forecast sent (or produced, in a dry run) = +1; a forecast = reset. 401/402 never count."""
    if store is None:
        return
    key = _qkey(q)
    if outcome.status == "failed" and not outcome.fatal:
        last = outcome.detail.split(" | ")[-1]
        store.record_failure(key, q.question_text, q.page_url, last)
    elif outcome.status in ("submitted", "comment_failed", "dry_run"):
        store.clear_failure(key)


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
                result.fatal = result.fatal or f"OpenRouter HTTP {status}: run stopped, nothing else attempted"
                stop.set()
                return Outcome(tournament, title, url, "failed", " | ".join(errors), fatal=True)
    if report is None:
        return Outcome(tournament, title, url, "failed", " | ".join(errors))
    status = llm_fatal_status(" ".join(report.errors or []))
    if status:  # some forecasters hit 401/402 but enough succeeded: keep this one, stop the rest
        result.fatal = result.fatal or f"OpenRouter HTTP {status}: run stopped, nothing else attempted"
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


def render_skipped(skipped: list[dict[str, Any]], limit: int | None = None) -> list[str]:
    """Yellow block: questions skipped after repeated failures (id, title, count, last error)."""
    if not skipped:
        return []
    head = f"**{len(skipped)} 題連續失敗，已停止重試（不再呼叫 AI，省錢）**"
    if limit:
        head += f"；要再試改 MAX_QUESTION_FAILED_RUNS（現在 {limit}），題目截止後自動消失"
    rows = ["> - " + f"{s['id']}｜{str(s['title'])[:60].replace(chr(10), ' ')}｜失敗 {s['count']} 次｜"
            f"{' '.join(str(s.get('last_error', '')).split())[:200]}" for s in skipped]
    return ["", "> [!WARNING]", f"> {head}", *rows, ""]


def render_summary(r: RunResult, with_header: bool = True) -> str:
    lines: list[str] = [precheck.summary_header(r.publish), ""] if with_header else []
    lines += [f"## forecast-bot v{r.version}：預測", ""]
    lines.append(f"- 模式：`{r.mode}`" + ("" if r.publish else "（DRY RUN，什麼都不送）"))
    if r.models:
        cheap = next(iter(r.models.values()))[1]
        per = []
        for k, (fid, _) in r.models.items():
            n = f" x{r.pacing.kinds[k].tier.forecasts}" if r.pacing else ""
            per.append(f"{k} `{fid}`{n}")
        lines.append(f"- 模型：{'；'.join(per)}；研究／解析 `{cheap}`＋原生搜尋；AskNews {_asknews()}")
    for reason in r.systemic:
        lines += _block("CAUTION", f"**停止／系統性問題**：{reason}")
    if r.decision.warning:
        lines += _block("WARNING", r.decision.warning)
    if r.fetch_errors and r.open_counts and not r.fetch_auth_error:
        for err in r.fetch_errors:
            lines += _block("WARNING", f"**抓題失敗（另一邊照跑）**：{err}")
    if with_header:  # with a precheck step above, it already lists these
        lines += render_skipped(r.skipped_failing, r.fail_limit)
    comment_failed = r.count("comment_failed")
    if comment_failed:
        lines += _block("CAUTION", f"**{comment_failed} 題的說明（comment）沒送出去**，預測已送；下一輪會自動補送。")
    lines.append(f"- OpenRouter：{r.decision.state}（{r.decision.reason}）")
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
        f"- 預算不足跳過：{r.count('skipped_budget')}；OpenRouter 停止後沒做：{r.count('stopped')}",
    ]
    if r.not_attempted_cap:
        lines.append(f"- 這輪上限外沒做：{r.not_attempted_cap}")
    lines += [
        f"- 這輪估計花費：{_money(r.spend)}",
        f"- OpenRouter 剩餘：{_money(r.remaining_after)}",
    ]
    bad = [o for o in r.outcomes if o.status in ("failed", "comment_failed")]
    if bad:
        lines += ["", "### 失敗的題目"]
        lines += [f"- [{o.title}]({o.url})（{o.status}）：{o.detail}" for o in bad]
    done = [o for o in r.outcomes if o.status in ("submitted", "comment_failed", "dry_run")]
    if done:
        lines += ["", "### 這輪預測的題目"]
        lines += [f"- [{o.title}]({o.url})（{o.tournament}，約 {_money(o.cost)}）" for o in done]
    if with_header and r.windows:  # no precheck listing above (Test Bot): print open/close times here (C4)
        lines += ["", "<details><summary>開放中的題目（開放／截止時間，UTC）</summary>", "",
                  "| tournament | question | opens | closes |", "|---|---|---|---|"]
        for t, title, url, op, cl in r.windows:
            lines.append(f"| {t} | [{title.replace('|', '/')}]({url}) | {op} | {cl} |")
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


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
