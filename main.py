"""
forecast-bot: Metaculus FutureEval Fall 2026 + MiniBench bot.

Based on Metaculus/metac-bot-template (see README for the upstream commit).
The forecasting flow is the template's; what changed is listed in README.

Entry point: `python main.py --mode tournament|test_questions`
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

import dotenv

from bot_helpers import silence_noisy_dependencies

silence_noisy_dependencies()

from forecasting_tools import (  # noqa: E402
    AskNewsSearcher,
    BinaryPrediction,
    BinaryQuestion,
    DatePercentile,
    DateQuestion,
    ForecastBot,
    GeneralLlm,
    MetaculusQuestion,
    MultipleChoiceQuestion,
    NumericDistribution,
    NumericQuestion,
    Percentile,
    PredictedOptionList,
    ReasonedPrediction,
    clean_indents,
    structure_output,
)
from forecasting_tools.data_models.multiple_choice_report import (  # noqa: E402
    PredictedOption,
)

import guards  # noqa: E402

dotenv.load_dotenv()
logger = logging.getLogger(__name__)

VERSION = "0.3.0"

# Model choice (explicit candidate lists, per-tournament config) lives in
# budget.py so the light precheck step can use it without dependencies.


def register_prices_with_litellm(prices: dict[str, tuple[float, float]]) -> None:
    """Teach litellm the prices so per-question cost estimates are not $0."""
    import litellm

    litellm.register_model(
        {
            f"openrouter/{mid}": {
                "input_cost_per_token": inp,
                "output_cost_per_token": out,
                "litellm_provider": "openrouter",
                "mode": "chat",
            }
            for mid, (inp, out) in prices.items()
        }
    )


def _web_search_llm(model_id: str) -> GeneralLlm:
    # OpenRouter web plugin pinned to the provider's NATIVE search (B4). Unlike
    # the ":online" suffix it cannot silently fall back to Exa, which the
    # donated credits do not cover.
    return GeneralLlm(
        model=f"openrouter/{model_id}",
        temperature=None,
        timeout=300,
        allowed_tries=2,
        extra_body={"plugins": [{"id": "web", "engine": "native"}]},
    )


def make_probe(cheap_id: str) -> Any:
    """v0.1.4 health probe: one HTTP call, no SDK retry, the cheapest (research) model, tiny answer.
    Any returned answer (even empty, e.g. a reasoning model spending its 16 tokens) means the upstream is up."""

    async def probe() -> None:
        import litellm
        import runner

        await litellm.acompletion(
            model=f"openrouter/{cheap_id}",
            messages=[{"role": "user", "content": 'Reply with the single word "ok".'}],
            max_tokens=16,
            timeout=runner.PROBE_TIMEOUT_S,
            num_retries=0,
        )

    return probe


def build_llms(forecast_id: str, cheap_id: str) -> dict[str, Any]:
    """Final forecasts on `forecast_id`; research, summary and parsing on the cheap model (C2)."""
    return {
        "default": GeneralLlm(
            model=f"openrouter/{forecast_id}", temperature=None, timeout=300, allowed_tries=2
        ),
        "researcher": _web_search_llm(cheap_id),
        "summarizer": GeneralLlm(
            model=f"openrouter/{cheap_id}", temperature=None, timeout=120, allowed_tries=2
        ),
        "parser": GeneralLlm(
            model=f"openrouter/{cheap_id}", temperature=None, timeout=120, allowed_tries=2
        ),
    }


# ---------------------------------------------------------- Anthropic (v0.2.0)
# Used when there is no OpenRouter key (budget.select_provider). Official
# `anthropic` SDK, not litellm. Never sent: temperature/top_p/top_k (400 since
# 2026-10-21), `thinking` (always on for Opus 5.5; effort is the control),
# tool_choice, assistant prefill.

FALLBACK_BETA = "server-side-fallback-2026-07-01"  # pairs with fallbacks="default"
WEB_SEARCH_TOOL = "web_search_20250305"  # basic variant; the dynamic-filtering ones are not listed for Haiku 5.5
MAX_CONTINUATIONS = 3  # pause_turn continuations of one server-tool turn
_CREDIT_TEXT = re.compile(r"credit balance|spend(?:ing)? (?:limit|cap)|usage limit|billing", re.I)


class AnthropicCreditError(Exception):
    """Credit used up / spend limit reached. status_code 402 -> runner's run-wide stop (no retry, not counted)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.status_code = 402


class AnthropicRefusal(RuntimeError):
    """stop_reason == "refusal" (after any server-side fallback). Counts as an ordinary failure."""


def anthropic_fatal(e: BaseException) -> BaseException | None:
    """402, or a 400/429 whose message is about credit/spend limits -> AnthropicCreditError.
    401 needs no mapping: the SDK's AuthenticationError already carries status_code 401."""
    import anthropic

    if isinstance(e, anthropic.APIStatusError):
        msg = str(getattr(e, "message", "") or e)
        if e.status_code == 402 or (e.status_code in (400, 429) and _CREDIT_TEXT.search(msg)):
            return AnthropicCreditError(f"Anthropic credit/spend limit reached (HTTP {e.status_code}): {msg[:200]}")
    return None


def _no_retry_types() -> tuple[type[BaseException], ...]:
    import anthropic

    return (AnthropicCreditError, AnthropicRefusal, anthropic.AuthenticationError,
            anthropic.PermissionDeniedError, anthropic.BadRequestError, anthropic.NotFoundError)


class AnthropicLlm(GeneralLlm):
    """GeneralLlm look-alike (so ForecastBot, structure_output and the retry/cost plumbing keep working)
    whose direct call goes to the Anthropic Messages API instead of litellm."""

    def __init__(self, model: str, client: Any, env: dict[str, str], record: Any, *, effort: str,
                 max_tokens: int, web_search: bool = False, fallbacks: bool = False,
                 allowed_tries: int = 2, retry_wait_s: float = 5.0) -> None:
        super().__init__(model=model, temperature=None, timeout=600, allowed_tries=allowed_tries)
        self.client, self.env, self.record = client, env, record
        self.effort, self.max_tokens, self.retry_wait_s = effort, max_tokens, retry_wait_s
        self.fallbacks = fallbacks
        self.tools: list[dict[str, Any]] = []
        if web_search:
            self.tools.append({
                "type": env.get("ANTHROPIC_WEB_SEARCH_TOOL") or WEB_SEARCH_TOOL,
                "name": "web_search",
                "max_uses": int(env.get("ANTHROPIC_WEB_SEARCH_MAX_USES") or 5),
            })

    def request_params(self, prompt: Any) -> dict[str, Any]:
        system, messages = _to_anthropic_messages(prompt)
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": messages,
            "output_config": {"effort": self.effort},
        }
        if system:
            params["system"] = system
        if self.tools:
            params["tools"] = self.tools
        if self.fallbacks:
            params["fallbacks"] = "default"
            params["betas"] = [FALLBACK_BETA]
        return params

    async def _invoke_with_request_cost_time_and_token_limits_and_retry(self, prompt: Any) -> Any:
        # Own retry loop: auth/credit/refusal/bad-request errors are never retried here (the SDK
        # already retried 408/409/429/5xx and connection errors twice).
        for attempt in range(max(1, self.allowed_tries)):
            try:
                return await self._mockable_direct_call_to_model(prompt)
            except _no_retry_types():
                raise
            except Exception:
                if attempt + 1 >= max(1, self.allowed_tries):
                    raise
                await asyncio.sleep(self.retry_wait_s * (attempt + 1))

    async def _mockable_direct_call_to_model(self, prompt: Any) -> Any:
        import anthropic
        import budget
        from forecasting_tools.ai_models.ai_utils.response_types import TextTokenCostResponse
        from forecasting_tools.ai_models.resource_managers.monetary_cost_manager import MonetaryCostManager

        params = self.request_params(prompt)
        base_messages = list(params["messages"])
        texts: list[str] = []
        urls: list[str] = []
        search_errors: list[str] = []
        cost = 0.0
        tokens_in = tokens_out = 0
        stop_reason = None
        for _ in range(1 + MAX_CONTINUATIONS):
            try:
                resp = await self.client.beta.messages.create(**params)
            except anthropic.APIStatusError as e:
                fatal = anthropic_fatal(e)
                if fatal is not None:
                    raise fatal from e
                raise
            usage = resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else dict(resp.usage)
            call_cost = budget.anthropic_usage_cost(usage, self.model, self.env)
            cost += call_cost
            tokens_in += int(usage.get("input_tokens") or 0)
            tokens_out += int(usage.get("output_tokens") or 0)
            self.record(call_cost)  # every call is booked, refusals included
            MonetaryCostManager.increase_current_usage_in_parent_managers(call_cost)
            stop_reason = resp.stop_reason
            if stop_reason == "refusal":  # always checked before reading content
                details = getattr(resp, "stop_details", None)
                category = getattr(details, "category", None) if details is not None else None
                raise AnthropicRefusal(f"{self.model} declined the request (refusal, category {category})")
            for block in resp.content:
                kind = getattr(block, "type", None)
                if kind == "text":
                    texts.append(block.text)
                    for c in getattr(block, "citations", None) or []:
                        url = getattr(c, "url", None)
                        if url and url not in urls:
                            urls.append(url)
                elif kind == "web_search_tool_result" and not isinstance(block.content, list):
                    # server-tool errors come back inside a 200, as an object instead of a result list
                    search_errors.append(str(getattr(block.content, "error_code", "unknown")))
            if stop_reason != "pause_turn":
                break
            params["messages"] = base_messages + [
                {"role": "assistant", "content": [b.to_dict() if hasattr(b, "to_dict") else b for b in resp.content]}
            ]
        if search_errors:
            logger.warning(f"Web search errors from {self.model}: {', '.join(search_errors)}")
        answer = "".join(texts).strip()
        if not answer:
            raise RuntimeError(
                f"{self.model} returned no text (stop_reason {stop_reason}"
                + (f", web search errors: {', '.join(search_errors)}" if search_errors else "") + ")"
            )
        if stop_reason == "max_tokens":
            logger.warning(f"{self.model} hit max_tokens={self.max_tokens}; the answer may be cut off")
        if urls:
            answer += "\n\nSources:\n" + "\n".join(f"- {u}" for u in urls[:20])
        return TextTokenCostResponse(
            data=answer,
            prompt_tokens_used=tokens_in,
            completion_tokens_used=tokens_out,
            total_tokens_used=tokens_in + tokens_out,
            model=self.model,
            cost=cost,
        )


def supports_server_fallback(model: str) -> bool:
    """Haiku 5.5 has no server-side fallback (a list 400s, "default" does nothing)."""
    return not model.startswith("claude-haiku")


def build_anthropic_llms(forecast_id: str, cheap_id: str, client: Any, env: dict[str, str],
                         record: Any, **kw: Any) -> dict[str, Any]:
    """Final forecasts on `forecast_id` (effort ANTHROPIC_EFFORT, default medium, refusal fallbacks on);
    research on the cheap model with server web search; summary and parsing on the cheap model at low effort."""
    fallbacks = env.get("ANTHROPIC_FALLBACKS", "").strip().lower() != "off"
    max_tokens = int(env.get("ANTHROPIC_MAX_TOKENS") or 16000)

    def llm(model: str, effort: str, tokens: int, web: bool = False) -> AnthropicLlm:
        return AnthropicLlm(model, client, env, record, effort=effort, max_tokens=tokens, web_search=web,
                            fallbacks=fallbacks and supports_server_fallback(model), **kw)

    return {
        "default": llm(forecast_id, env.get("ANTHROPIC_EFFORT") or "medium", max_tokens),
        "researcher": llm(cheap_id, env.get("ANTHROPIC_RESEARCH_EFFORT") or "medium", max_tokens, web=True),
        "summarizer": llm(cheap_id, "low", 4000),
        "parser": llm(cheap_id, "low", 4000),
    }


def make_anthropic_probe(cheap_id: str, client: Any, record: Any, env: dict[str, str]) -> Any:
    """v0.1.4 health probe on Anthropic: one tiny call, no SDK retry; any 200 means the upstream is up."""

    async def probe() -> None:
        import anthropic
        import budget
        import runner

        c = client.with_options(max_retries=0, timeout=runner.PROBE_TIMEOUT_S) if hasattr(client, "with_options") else client
        try:
            resp = await c.beta.messages.create(
                model=cheap_id, max_tokens=16, output_config={"effort": "low"},
                messages=[{"role": "user", "content": 'Reply with the single word "ok".'}],
            )
        except anthropic.APIStatusError as e:
            fatal = anthropic_fatal(e)
            if fatal is not None:
                raise fatal from e
            raise
        usage = resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else dict(resp.usage)
        record(budget.anthropic_usage_cost(usage, cheap_id, env))

    return probe


def make_spend_recorder(ledger: Any, env: dict[str, str]) -> Any:
    """Books each Anthropic call into the current credit period (state.json, saved at once)."""
    import budget

    def record(usd: float) -> None:
        start, _ = budget.credit_period(budget.utc_today(), budget.credit_reset_day(env))
        ledger.add_spend(start.isoformat(), usd)

    return record


# ------------------------------------------------------------ OpenAI (v0.3.0)
# Direct OpenAI Responses API with the official `openai` SDK (already a dependency),
# not litellm. reasoning.effort is always sent (gpt-5.4 / -mini default to none);
# never sent: temperature/top_p. Research = web_search tool, billed (tool use is
# excluded from the free daily tokens). Every call is booked by make_openai_recorder.

OPENAI_FATAL_CODES = {"insufficient_quota", "credit_balance_exhausted", "organization_spend_limit_exceeded",
                      "project_spend_limit_exceeded", "organization_usage_limit_exceeded", "billing_hard_limit_reached"}
_OPENAI_FATAL_TEXT = re.compile(r"exceeded your current quota|spend limit|credit balance|billing", re.I)


class OpenAIQuotaError(Exception):
    """Quota / credit / spend limit used up. status_code 402 -> runner's run-wide stop (no retry, not counted)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.status_code = 402


def openai_fatal(e: BaseException) -> BaseException | None:
    """402, or any error whose code/type is a quota or spend-limit code (429 insufficient_quota,
    *_spend_limit_exceeded, credit_balance_exhausted ...) -> OpenAIQuotaError. 401 needs no mapping:
    the SDK's AuthenticationError carries status_code 401."""
    import openai

    if isinstance(e, openai.APIStatusError):
        codes = {str(getattr(e, "code", "") or ""), str(getattr(e, "type", "") or "")}
        msg = str(getattr(e, "message", "") or e)
        if e.status_code == 402 or codes & OPENAI_FATAL_CODES or (e.status_code == 429 and _OPENAI_FATAL_TEXT.search(msg)):
            return OpenAIQuotaError(f"OpenAI quota/spend limit reached (HTTP {e.status_code}, "
                                    f"{(codes & OPENAI_FATAL_CODES or {'billing'}).pop()}): {msg[:200]}")
    return None


def _openai_no_retry_types() -> tuple[type[BaseException], ...]:
    import openai

    return (OpenAIQuotaError, openai.AuthenticationError, openai.PermissionDeniedError,
            openai.BadRequestError, openai.NotFoundError)


def _dump(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    return obj.model_dump() if hasattr(obj, "model_dump") else dict(vars(obj))


def _web_search_calls(output: list[Any]) -> int:
    """Billed search calls in one response: web_search_call items whose action is a search
    (open_page / find_in_page are not billed as searches)."""
    n = 0
    for item in output or []:
        if getattr(item, "type", None) == "web_search_call":
            action = getattr(item, "action", None)
            kind = getattr(action, "type", None) if action is not None else None
            n += 1 if kind in (None, "search") else 0
    return n


class OpenAILlm(GeneralLlm):
    """GeneralLlm look-alike whose direct call goes to the OpenAI Responses API."""

    def __init__(self, model: str, client: Any, env: dict[str, str], record: Any, *, effort: str,
                 max_tokens: int, web_search: bool = False, allowed_tries: int = 2,
                 retry_wait_s: float = 5.0) -> None:
        super().__init__(model=model, temperature=None, timeout=600, allowed_tries=allowed_tries)
        self.client, self.env, self.record = client, env, record
        self.effort, self.max_tokens, self.retry_wait_s = effort, max_tokens, retry_wait_s
        self.web_search = web_search

    def request_params(self, prompt: Any) -> dict[str, Any]:
        system, messages = _to_anthropic_messages(prompt)  # same {role, content} shape
        params: dict[str, Any] = {
            "model": self.model,
            "input": messages,
            "max_output_tokens": self.max_tokens,
            "reasoning": {"effort": self.effort},
        }
        if system:
            params["instructions"] = system
        if self.web_search:
            params["tools"] = [{"type": "web_search",
                                "search_context_size": self.env.get("OPENAI_SEARCH_CONTEXT") or "low"}]
            params["max_tool_calls"] = int(self.env.get("OPENAI_WEB_SEARCH_MAX_CALLS") or 3)
        return params

    async def _invoke_with_request_cost_time_and_token_limits_and_retry(self, prompt: Any) -> Any:
        for attempt in range(max(1, self.allowed_tries)):
            try:
                return await self._mockable_direct_call_to_model(prompt)
            except _openai_no_retry_types():
                raise
            except Exception:
                if attempt + 1 >= max(1, self.allowed_tries):
                    raise
                await asyncio.sleep(self.retry_wait_s * (attempt + 1))

    async def _mockable_direct_call_to_model(self, prompt: Any) -> Any:
        import openai
        from forecasting_tools.ai_models.ai_utils.response_types import TextTokenCostResponse
        from forecasting_tools.ai_models.resource_managers.monetary_cost_manager import MonetaryCostManager

        try:
            resp = await self.client.responses.create(**self.request_params(prompt))
        except openai.APIStatusError as e:
            fatal = openai_fatal(e)
            if fatal is not None:
                raise fatal from e
            raise
        usage = _dump(getattr(resp, "usage", None))
        searches = _web_search_calls(getattr(resp, "output", None) or [])
        cost = self.record(self.model, usage, searches, self.web_search)  # every call is booked
        MonetaryCostManager.increase_current_usage_in_parent_managers(cost)
        texts: list[str] = []
        urls: list[str] = []
        for item in getattr(resp, "output", None) or []:
            if getattr(item, "type", None) != "message":
                continue
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) == "output_text":
                    texts.append(part.text)
                    for a in getattr(part, "annotations", None) or []:
                        url = getattr(a, "url", None)
                        if getattr(a, "type", None) == "url_citation" and url and url not in urls:
                            urls.append(url)
                elif getattr(part, "type", None) == "refusal":
                    raise RuntimeError(f"{self.model} refused: {str(getattr(part, 'refusal', ''))[:200]}")
        answer = "".join(texts).strip()
        status = getattr(resp, "status", None)
        reason = getattr(getattr(resp, "incomplete_details", None), "reason", None)
        if not answer:
            raise RuntimeError(f"{self.model} returned no text (status {status}, {reason or 'no reason'})")
        if status == "incomplete":
            logger.warning(f"{self.model} response incomplete ({reason}); the answer may be cut off")
        if urls:
            answer += "\n\nSources:\n" + "\n".join(f"- {u}" for u in urls[:20])
        tin, tout = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        return TextTokenCostResponse(data=answer, prompt_tokens_used=tin, completion_tokens_used=tout,
                                     total_tokens_used=tin + tout, model=self.model, cost=cost)


def build_openai_llms(forecast_id: str, parse_id: str, client: Any, env: dict[str, str], record: Any,
                      research_id: str | None = None, **kw: Any) -> dict[str, Any]:
    """Forecasts on `forecast_id` (OPENAI_EFFORT, default medium); research on the research model with
    web search (OPENAI_RESEARCH_EFFORT, default low); summary and parsing on `parse_id` at low effort."""
    import budget

    research_id = research_id or budget.openai_models(env)[1]

    def llm(model: str, effort: str, tokens: int, web: bool = False) -> OpenAILlm:
        return OpenAILlm(model, client, env, record, effort=effort, max_tokens=tokens, web_search=web, **kw)

    return {
        "default": llm(forecast_id, env.get("OPENAI_EFFORT") or "medium", int(env.get("OPENAI_MAX_TOKENS") or 16000)),
        "researcher": llm(research_id, env.get("OPENAI_RESEARCH_EFFORT") or "low", 12000, web=True),
        "summarizer": llm(parse_id, "low", 4000),
        "parser": llm(parse_id, "low", 4000),
    }


def make_openai_recorder(ledger: Any, env: dict[str, str]) -> Any:
    """Books each OpenAI call: free-group tokens for today (UTC) and paid USD for this month; returns the USD."""
    import budget

    def record(model: str, usage: dict[str, Any], web_searches: int = 0, used_tool: bool = False) -> float:
        today = budget.utc_today()
        group, tokens, usd = budget.openai_call_charge(
            usage, model, env, ledger.openai_free_used(today.isoformat()), web_searches, used_tool)
        ledger.add_openai(today.isoformat(), budget.month_key(today), group, tokens, usd)
        return usd

    return record


def make_openai_probe(parse_id: str, client: Any, record: Any) -> Any:
    """v0.1.4 health probe on OpenAI: one tiny call, no SDK retry; any 200 means the upstream is up."""

    async def probe() -> None:
        import openai
        import runner

        c = client.with_options(max_retries=0, timeout=runner.PROBE_TIMEOUT_S) if hasattr(client, "with_options") else client
        try:
            resp = await c.responses.create(model=parse_id, max_output_tokens=16, reasoning={"effort": "low"},
                                            input='Reply with the single word "ok".')
        except openai.APIStatusError as e:
            fatal = openai_fatal(e)
            if fatal is not None:
                raise fatal from e
            raise
        record(parse_id, _dump(getattr(resp, "usage", None)), 0, False)

    return probe


def _to_anthropic_messages(prompt: Any) -> tuple[str, list[dict[str, Any]]]:
    """str -> one user message; a list of {role, content} dicts (GeneralLlm's shape) -> system text + the rest."""
    if isinstance(prompt, str):
        return "", [{"role": "user", "content": prompt}]
    if isinstance(prompt, list):
        system = "\n\n".join(str(m["content"]) for m in prompt if m.get("role") == "system")
        rest = [{"role": m["role"], "content": m["content"]} for m in prompt if m.get("role") != "system"]
        return system, rest
    raise TypeError(f"unsupported prompt type for the Anthropic path: {type(prompt).__name__}")


# --------------------------------------------------------------------- bot


def _date(d: datetime | None) -> str:
    return d.date().isoformat() if d else "not stated"


def timing_block(question: MetaculusQuestion, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return clean_indents(
        f"""
        Today is {now.date().isoformat()}.
        Forecasting on this question closes on {_date(question.close_time)}.
        The question is scheduled to resolve on {_date(question.scheduled_resolution_time)}.
        This question has NOT resolved yet. If your research seems to show the
        outcome is already settled, treat that as strong evidence, but do not
        assume the resolution criteria are met unless a source shows it
        explicitly, with a date, after the question opened.
        """
    )


OUTSIDE_THEN_INSIDE = clean_indents(
    """
    Work in this order and write each step down briefly:
    1. Time left until the outcome is known.
    2. Outside view: a reference class of similar situations and its base rate
       over a window of this length; the status-quo outcome and how likely it
       is to persist until resolution.
    3. Inside view: the specific evidence in the research that should move you
       away from that base rate, and in which direction and by how much.
    4. One short scenario for each main outcome.
    Then give your final answer.
    """
)


class ForecastBot2026(ForecastBot):
    """
    Single-prompt bot (template flow) plus final-value guardrails from
    guards.py. No human-in-the-loop overrides exist anywhere in this class.
    """

    _max_concurrent_research = 2
    _structure_output_validation_samples = 2

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._research_limiter = asyncio.Semaphore(self._max_concurrent_research)
        self.guard_notes: dict[str, list[str]] = {}

    # ---------------------------------------------------------- bookkeeping

    @staticmethod
    def question_key(question: MetaculusQuestion) -> str:
        return f"{question.id_of_post}:{question.id_of_question}:{question.page_url}"

    def add_guard_note(self, question: MetaculusQuestion, note: str) -> None:
        self.guard_notes.setdefault(self.question_key(question), []).append(note)

    # ------------------------------------------------------------- research

    async def run_research(self, question: MetaculusQuestion) -> str:
        async with self._research_limiter:
            parts: list[str] = []
            if os.getenv("ASKNEWS_CLIENT_ID") and os.getenv("ASKNEWS_SECRET"):
                try:
                    news = await AskNewsSearcher().call_preconfigured_version(
                        "asknews/news-summaries", question.question_text
                    )
                    parts.append(f"## News (AskNews)\n{news}")
                except Exception as e:  # research is best-effort
                    logger.warning(f"AskNews failed: {e.__class__.__name__}")
            researcher = self.get_llm("researcher", "llm")
            web = await researcher.invoke(self._research_prompt(question))
            parts.append(f"## Web research\n{web}")
            return "\n\n".join(parts)

    @staticmethod
    def _research_prompt(question: MetaculusQuestion) -> str:
        return clean_indents(
            f"""
            You are a research assistant for a forecaster. Search the web and
            write a concise but detailed rundown of the most relevant recent
            news and data for the question below, with dates and source links.
            Include base-rate data (how often similar things happened) and the
            current status quo. Say whether current information already
            satisfies the resolution criteria. Do not give a forecast.

            {timing_block(question)}

            Question:
            {question.question_text}

            Resolution criteria:
            {question.resolution_criteria}

            {question.fine_print}
            """
        )

    def _question_block(self, question: MetaculusQuestion, research: str) -> str:
        return clean_indents(
            f"""
            Question:
            {question.question_text}

            Background:
            {question.background_info}

            Resolution criteria (not yet satisfied at question creation):
            {question.resolution_criteria}

            {question.fine_print}

            Research:
            {research}

            {timing_block(question)}
            """
        )

    # --------------------------------------------------------------- binary

    async def _run_forecast_on_binary(
        self, question: BinaryQuestion, research: str
    ) -> ReasonedPrediction[float]:
        prompt = clean_indents(
            f"""
            You are forecasting the probability that this question resolves Yes.

            {self._question_block(question, research)}

            {OUTSIDE_THEN_INSIDE}

            The last thing you write is your final answer as: "Probability: ZZ%", 0-100
            """
        )
        reasoning = await self.get_llm("default", "llm").invoke(prompt)
        parsed: BinaryPrediction = await structure_output(
            reasoning,
            BinaryPrediction,
            model=self.get_llm("parser", "llm"),
            num_validation_samples=self._structure_output_validation_samples,
            additional_instructions=self._resolved_parsing_message(),
        )
        # No clamping here: the raw value stays visible in the comment and the
        # final clamp happens once, in _aggregate_predictions.
        value = float(parsed.prediction_in_decimal)
        return ReasonedPrediction(prediction_value=value, reasoning=reasoning)

    # ------------------------------------------------------ multiple choice

    async def _run_forecast_on_multiple_choice(
        self, question: MultipleChoiceQuestion, research: str
    ) -> ReasonedPrediction[PredictedOptionList]:
        prompt = clean_indents(
            f"""
            You are forecasting which option this question resolves to.

            The options are: {question.options}

            {self._question_block(question, research)}

            {OUTSIDE_THEN_INSIDE}
            Leave some probability on options that look unlikely; surprises happen.

            The last thing you write is your final probabilities for the N options in this order {question.options} as:
            Option_A: Probability_A
            Option_B: Probability_B
            ...
            Option_N: Probability_N
            """
        )
        reasoning = await self.get_llm("default", "llm").invoke(prompt)
        instructions = clean_indents(
            f"""
            Make sure that all option names are one of the following:
            {question.options}
            The text may prepend options with some variation of "Option"; remove it if it is not part of the option name.
            Do not skip options with 0%; include them with 0% probability.
            {self._resolved_parsing_message()}
            """
        )
        parsed: PredictedOptionList = await structure_output(
            text_to_structure=reasoning,
            output_type=PredictedOptionList,
            model=self.get_llm("parser", "llm"),
            num_validation_samples=self._structure_output_validation_samples,
            additional_instructions=instructions,
        )
        return ReasonedPrediction(prediction_value=parsed, reasoning=reasoning)

    @staticmethod
    def _resolved_parsing_message() -> str:
        return (
            "- If the text concludes that the question has already resolved *in the past* "
            "(i.e. it treats the question as decided rather than something to forecast), "
            "please DO NOT return a parsed output, even if a final forecast is also given. "
            "Instead indicate that the answer is not explicitly given in the text.\n"
        )

    # ---------------------------------------------------- numeric / discrete

    @staticmethod
    def _bound_messages(question: NumericQuestion | DateQuestion) -> str:
        if isinstance(question, DateQuestion):
            upper, lower, unit = (
                question.upper_bound.date().isoformat(),
                question.lower_bound.date().isoformat(),
                "",
            )
        else:
            upper = (
                question.nominal_upper_bound
                if question.nominal_upper_bound is not None
                else question.upper_bound
            )
            lower = (
                question.nominal_lower_bound
                if question.nominal_lower_bound is not None
                else question.lower_bound
            )
            unit = question.unit_of_measure or ""
        up = (
            f"The question creator thinks the number is likely not higher than {upper} {unit}."
            if question.open_upper_bound
            else f"The outcome can not be higher than {upper} {unit}."
        )
        low = (
            f"The question creator thinks the number is likely not lower than {lower} {unit}."
            if question.open_lower_bound
            else f"The outcome can not be lower than {lower} {unit}."
        )
        return f"{low}\n{up}"

    def _numeric_prompt(self, question: NumericQuestion, research: str) -> str:
        unit = question.unit_of_measure or "Not stated (please infer this)"
        return clean_indents(
            f"""
            You are forecasting a numeric quantity.

            {self._question_block(question, research)}

            Units for answer: {unit}
            {self._bound_messages(question)}

            Formatting:
            - Give values in exactly the units above (e.g. if units are "million $", write 1500 for $1.5 billion).
            - Never use scientific notation.
            - Percentile 10 is the smallest value; values increase with the percentile.

            {OUTSIDE_THEN_INSIDE}
            Set wide 10/90 intervals to account for unknown unknowns.

            The last thing you write is your final answer as:
            "
            Percentile 10: XX (lowest number value)
            Percentile 20: XX
            Percentile 40: XX
            Percentile 60: XX
            Percentile 80: XX
            Percentile 90: XX (highest number value)
            "
            """
        )

    async def _parse_numeric(
        self, question: NumericQuestion, reasoning: str
    ) -> list[Percentile]:
        instructions = clean_indents(
            f"""
            The text gives a forecast distribution for the numeric question: "{question.question_text}".
            - Return exactly ONE distribution: the single final one that answers this question.
            - Give the values in the units: {question.unit_of_measure}. Convert if the text uses other units
              (e.g. $500,000,000 with units "B $" is 0.5).
            - Turn scientific notation into regular numbers.
            - If percentiles are not explicitly given, do not return a parsed output.
            {self._resolved_parsing_message()}
            """
        )
        return await structure_output(
            reasoning,
            list[Percentile],
            model=self.get_llm("parser", "llm"),
            additional_instructions=instructions,
            num_validation_samples=self._structure_output_validation_samples,
        )

    async def _run_forecast_on_numeric(
        self, question: NumericQuestion, research: str
    ) -> ReasonedPrediction[NumericDistribution]:
        """
        One forecaster. A median that looks like a unit error is re-asked once;
        if it is still wrong this forecaster FAILS (B2: no wide/0.5 fallback is
        ever submitted). The SDK aggregates the forecasters that succeeded, and
        fails the question when fewer than half succeed.
        """
        llm = self.get_llm("default", "llm")
        lo, hi = question.lower_bound, question.upper_bound
        prompt = self._numeric_prompt(question, research)
        reasoning = await llm.invoke(prompt)
        percentiles = await self._parse_numeric(question, reasoning)
        median = guards.implied_median([(p.percentile, p.value) for p in percentiles])

        if guards.looks_like_unit_error(median, lo, hi):
            retry_prompt = clean_indents(
                f"""
                {prompt}

                Your previous answer was:
                {reasoning}

                Its median ({median:g} {question.unit_of_measure or ''}) is more than ten
                times outside this question's range ({lo:g} to {hi:g}). That almost always
                means a unit mistake (e.g. thousands vs millions, percent vs fraction).
                Re-read the units and give your final answer again in exactly those units.
                """
            )
            reasoning2 = await llm.invoke(retry_prompt)
            percentiles2 = await self._parse_numeric(question, reasoning2)
            median2 = guards.implied_median([(p.percentile, p.value) for p in percentiles2])
            if guards.looks_like_unit_error(median2, lo, hi):
                raise ValueError(
                    f"unit error twice (median {median:g}, then {median2:g}, range {lo:g}-{hi:g}); "
                    "this forecaster is dropped"
                )
            note = (
                f"one forecaster's median {median:g} looked like a unit error "
                f"(range {lo:g}-{hi:g}); re-asked, new median {median2:g}"
            )
            self.add_guard_note(question, note)
            reasoning = f"{reasoning2}\n\n[guardrail] {note}"
            percentiles = percentiles2
        # CDF validity (monotonic, bounds, minimum step) is the SDK's job (B8).
        dist = NumericDistribution.from_question(percentiles, question)
        return ReasonedPrediction(prediction_value=dist, reasoning=reasoning)

    # ----------------------------------------------------------------- date

    async def _run_forecast_on_date(
        self, question: DateQuestion, research: str
    ) -> ReasonedPrediction[NumericDistribution]:
        prompt = clean_indents(
            f"""
            You are forecasting when something will happen.

            {self._question_block(question, research)}

            {self._bound_messages(question)}
            Write dates as YYYY-MM-DD, earliest date at percentile 10, increasing after that.

            {OUTSIDE_THEN_INSIDE}
            Set wide 10/90 intervals to account for unknown unknowns.

            The last thing you write is your final answer as:
            "
            Percentile 10: YYYY-MM-DD (oldest date)
            Percentile 20: YYYY-MM-DD
            Percentile 40: YYYY-MM-DD
            Percentile 60: YYYY-MM-DD
            Percentile 80: YYYY-MM-DD
            Percentile 90: YYYY-MM-DD (newest date)
            "
            """
        )
        reasoning = await self.get_llm("default", "llm").invoke(prompt)
        parsed: list[DatePercentile] = await structure_output(
            reasoning,
            list[DatePercentile],
            model=self.get_llm("parser", "llm"),
            num_validation_samples=self._structure_output_validation_samples,
            additional_instructions=(
                "Return exactly one final distribution of dates. Assume midnight UTC.\n"
                + self._resolved_parsing_message()
            ),
        )
        percentiles = [
            Percentile(percentile=p.percentile, value=p.value.timestamp()) for p in parsed
        ]
        dist = NumericDistribution.from_question(percentiles, question)
        return ReasonedPrediction(prediction_value=dist, reasoning=reasoning)

    # ------------------------------------------------- final-value guardrails

    async def _aggregate_predictions(self, predictions: list[Any], question: MetaculusQuestion) -> Any:
        aggregate = await super()._aggregate_predictions(predictions, question)
        return self.apply_final_guards(aggregate, question)

    def apply_final_guards(self, aggregate: Any, question: MetaculusQuestion) -> Any:
        if isinstance(question, BinaryQuestion):
            result = guards.clamp_binary(float(aggregate))
            if result.note:
                self.add_guard_note(question, result.note)
            return result.value

        if isinstance(question, MultipleChoiceQuestion):
            raw = {o.option_name: o.probability for o in aggregate.predicted_options}
            result = guards.floor_and_normalize(raw)
            if result.note:
                self.add_guard_note(question, result.note)
            return PredictedOptionList(
                predicted_options=[
                    PredictedOption(option_name=k, probability=v)
                    for k, v in result.value.items()
                ]
            )

        if isinstance(question, NumericQuestion):
            # Single unit check on the aggregate (B8). CDF validity is left to
            # the SDK, which standardizes the CDF when it is built.
            median = guards.implied_median(
                [(p.percentile, p.value) for p in aggregate.get_cdf()]
            )
            if guards.looks_like_unit_error(median, question.lower_bound, question.upper_bound):
                raise ValueError(
                    f"aggregate median {median:g} looks like a unit error "
                    f"(range {question.lower_bound:g}-{question.upper_bound:g}); not submitted"
                )
            return aggregate

        return aggregate

    def _create_unified_explanation(
        self,
        question: MetaculusQuestion,
        research_prediction_collections: list[Any],
        aggregated_prediction: Any,
        final_cost: float,
        time_spent_in_minutes: float,
    ) -> str:
        body = super()._create_unified_explanation(
            question,
            research_prediction_collections,
            aggregated_prediction,
            final_cost,
            time_spent_in_minutes,
        )
        return self.guard_header(question) + body

    def guard_header(self, question: MetaculusQuestion) -> str:
        notes = self.guard_notes.get(self.question_key(question), [])
        # The SDK requires the explanation to start with a markdown heading.
        lines = [f"# forecast-bot v{VERSION}", "Fully automated; no human edits."]
        if notes:
            lines.append("**Final adjustments (model value -> submitted value):**")
            lines += [f"- {n}" for n in notes]
        else:
            lines.append("Final adjustments: none; the submitted value is the model aggregate.")
        return "\n".join(lines) + "\n\n"


# --------------------------------------------------------------------- CLI


def make_bot(forecast_id: str, cheap_id: str, forecasts: int, client: Any,
             llms: dict[str, Any] | None = None) -> ForecastBot2026:
    return ForecastBot2026(
        research_reports_per_question=1,
        predictions_per_research_report=forecasts,
        use_research_summary_to_forecast=False,
        publish_reports_to_metaculus=False,  # runner.py publishes (forecast, then comment)
        folder_to_save_reports_to=None,
        skip_previously_forecasted_questions=False,  # runner.py filters and counts
        extra_metadata_in_explanation=True,
        llms=llms if llms is not None else build_llms(forecast_id, cheap_id),
        metaculus_client=client,
    )


def cli(argv: list[str] | None = None) -> int:
    import budget
    import precheck
    import runner
    import state
    from bot_helpers import check_environment
    from forecasting_tools import MetaculusClient

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    parser = argparse.ArgumentParser(description="forecast-bot")
    parser.add_argument("--mode", choices=["tournament", "test_questions"], default="tournament")
    args = parser.parse_args(argv)
    env = dict(os.environ)
    publish = env.get("PUBLISH", "") == "1"
    with_header = env.get("SUMMARY_HEADER_DONE", "") != "1"
    errors = precheck.validate_switches(env, env.get("GITHUB_EVENT_NAME"))
    if errors:
        runner.write_summary(runner.render_crash(VERSION, publish, "; ".join(errors), with_header))
        return 1
    print(f"forecast-bot v{VERSION} mode={args.mode} publish={'yes' if publish else 'no (dry run)'}")

    result = None
    crash = "stopped before the run finished"
    try:
        check_environment(strict=True)
        provider = budget.select_provider(env)
        client = MetaculusClient()
        # Tournament runs (dry or not) keep the failure counts; only publishing stores comments.
        store = state.PendingComments.load(state.state_dir(env)) if args.mode == "tournament" else None
        if provider == "anthropic":  # v0.2.0
            import anthropic

            forecast_ids, cheap_id, prices = budget.provider_prices(env)
            # the spend ledger lives in the same state file; Test Bot loads it only for the ledger
            ledger = store if store is not None else state.PendingComments.load(state.state_dir(env))
            record = make_spend_recorder(ledger, env)
            aclient = anthropic.AsyncAnthropic(max_retries=2, timeout=600)
            llms_for = lambda kind: build_anthropic_llms(forecast_ids[kind], cheap_id, aclient, env, record)  # noqa: E731
            key_lookup_fn = lambda: budget.anthropic_lookup_from_store(env, ledger)  # noqa: E731
            probe_fn = make_anthropic_probe(cheap_id, aclient, record, env)
        elif provider == "openai":  # v0.3.0
            import openai

            forecast_ids, cheap_id, prices = budget.provider_prices(env)
            ledger = store if store is not None else state.PendingComments.load(state.state_dir(env))
            orecord = make_openai_recorder(ledger, env)
            oclient = openai.AsyncOpenAI(max_retries=2, timeout=600)
            llms_for = lambda kind: build_openai_llms(forecast_ids[kind], cheap_id, oclient, env, orecord)  # noqa: E731
            key_lookup_fn = lambda: budget.openai_lookup_from_store(env, ledger)  # noqa: E731
            probe_fn = make_openai_probe(cheap_id, oclient, orecord)
        else:
            models = budget.fetch_models()
            forecast_ids, cheap_id, prices = budget.provider_prices(env, models)
            register_prices_with_litellm(prices)
            llms_for = lambda kind: build_llms(forecast_ids[kind], cheap_id)  # noqa: E731
            key_lookup_fn = lambda: budget.fetch_key_info(env.get("OPENROUTER_API_KEY"))  # noqa: E731
            probe_fn = make_probe(cheap_id)
        result = asyncio.run(
            runner.run(
                mode=args.mode,
                publish=publish,
                bot_factory=lambda kind, tier: make_bot(
                    forecast_ids[kind], cheap_id, tier.forecasts, client, llms_for(kind)
                ),
                client=client,
                key_lookup_fn=key_lookup_fn,
                version=VERSION,
                models={k: (forecast_ids[k], cheap_id) for k in budget.KINDS},
                prices={k: (prices[forecast_ids[k]], prices[cheap_id]) for k in budget.KINDS},
                env=env,
                store=store,
                probe_fn=probe_fn,
                provider=provider,
            )
        )
        crash = ""
    except Exception as e:
        crash = f"{e.__class__.__name__}: {str(e)[:300]}"
    finally:
        # B1: the summary is written whatever happened.
        if result is not None and not crash:
            runner.write_summary(runner.render_summary(result, with_header))
        else:
            runner.write_summary(runner.render_crash(VERSION, publish, crash, with_header))
    return result.exit_code if result is not None and not crash else 1


if __name__ == "__main__":
    sys.exit(cli())
