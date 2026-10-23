"""v0.2.0 Anthropic path. Offline: a fake Anthropic client, no network, no real key."""
import asyncio
from datetime import date
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from forecasting_tools import GeneralLlm

import budget
import main
import precheck
import runner
import state
from conftest import TOURNAMENTS, FakeClient, load_fixture_questions

ORIG_INVOKE = GeneralLlm.invoke  # the fake_llm fixture patches GeneralLlm.invoke; AnthropicLlm gets the real one back
ENV = {"ANTHROPIC_API_KEY": "test-key-not-real"}
SAMPLING = ("temperature", "top_p", "top_k", "thinking", "tool_choice")


class Usage(dict):
    def model_dump(self):
        return dict(self)


def resp(text="ok", stop="end_turn", usage=None, extra=(), stop_details=None):
    content = list(extra) + ([SimpleNamespace(type="text", text=text, citations=None)] if text else [])
    return SimpleNamespace(content=content, stop_reason=stop, stop_details=stop_details,
                           usage=Usage(usage or {"input_tokens": 1000, "output_tokens": 500}))


class FakeAnthropic:
    """client.beta.messages.create(**kw) -> responder(kw); exceptions are raised."""

    def __init__(self, responder=None):
        self.calls = []
        self.options = None
        self.responder = responder or (lambda kw: resp())
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.calls.append(kw)
        r = self.responder(kw)
        if isinstance(r, BaseException):
            raise r
        return r

    def with_options(self, **kw):
        self.options = kw
        return self


def api_error(cls, status, message):
    req = httpx2.Request("POST", "https://api.anthropic.invalid/v1/messages")
    return cls(message, response=httpx2.Response(status, request=req), body=None)


def llms(client, env=None, booked=None):
    env = {**ENV, **(env or {})}
    record = (booked.append if booked is not None else (lambda usd: None))
    return main.build_anthropic_llms("claude-opus-5-5", "claude-haiku-5-5", client, env, record, retry_wait_s=0)


# ------------------------------------------------------------ provider selection


def test_provider_selection():
    sel = budget.select_provider
    assert sel({}) == "openrouter"  # nothing set: unchanged (the guard reports the missing key)
    assert sel({"OPENROUTER_API_KEY": "x"}) == "openrouter"
    assert sel({"OPENROUTER_API_KEY": "x", "ANTHROPIC_API_KEY": "y"}) == "openrouter"  # OpenRouter wins
    assert sel({"ANTHROPIC_API_KEY": "y"}) == "anthropic"
    assert sel({"OPENROUTER_API_KEY": "x", "LLM_PROVIDER": "anthropic"}) == "anthropic"
    assert sel({"ANTHROPIC_API_KEY": "y", "LLM_PROVIDER": " OpenRouter "}) == "openrouter"
    with pytest.raises(ValueError):
        sel({"LLM_PROVIDER": "claude"})
    assert any("LLM_PROVIDER" in e for e in precheck.validate_switches({"LLM_PROVIDER": "claude"}))


def test_anthropic_lookup_without_key_is_auth_error():
    d = budget.decide(budget.anthropic_lookup({}, 0.0, True), {})
    assert d.state == "auth_error" and "ANTHROPIC_API_KEY" in d.reason


# ------------------------------------------------------------ request shape


def test_no_sampling_params_and_expected_request_shape():
    client = FakeAnthropic()
    ll = llms(client)
    for role in ("default", "researcher", "summarizer", "parser"):
        asyncio.run(ORIG_INVOKE(ll[role], f"prompt for {role}"))
    by_model = {}
    for kw in client.calls:
        assert not [k for k in SAMPLING if k in kw], kw  # never sent (400 since 2026-10-21)
        assert not [k for k in kw if k.startswith("extra")]
        assert kw["messages"][-1]["role"] == "user"  # no prefill
        by_model.setdefault(kw["model"], []).append(kw)
    opus = by_model["claude-opus-5-5"][0]
    assert opus["output_config"] == {"effort": "medium"}
    assert opus["fallbacks"] == "default" and opus["betas"] == ["server-side-fallback-2026-07-01"]
    research, summary, parse = by_model["claude-haiku-5-5"]
    assert "fallbacks" not in research and "betas" not in research  # Haiku 5.5: no server-side fallback
    assert research["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    assert summary["output_config"] == {"effort": "low"} and "tools" not in parse


def test_effort_and_fallbacks_are_configurable():
    client = FakeAnthropic()
    ll = llms(client, {"ANTHROPIC_EFFORT": "high", "ANTHROPIC_FALLBACKS": "off"})
    asyncio.run(ORIG_INVOKE(ll["default"], "x"))
    assert client.calls[0]["output_config"] == {"effort": "high"} and "fallbacks" not in client.calls[0]


def test_refusal_is_checked_before_content_and_still_booked():
    class Exploding:
        def __iter__(self):
            raise AssertionError("content read on a refusal")

    r = resp(stop="refusal", stop_details=SimpleNamespace(category="bio"))
    r.content = Exploding()
    booked = []
    ll = llms(FakeAnthropic(lambda kw: r), booked=booked)
    with pytest.raises(main.AnthropicRefusal, match="bio"):
        asyncio.run(ORIG_INVOKE(ll["default"], "x"))
    assert len(booked) == 1 and booked[0] > 0  # one call, not retried, cost recorded


# ------------------------------------------------------------ spend accounting


def test_usage_cost_tokens_cache_and_web_search():
    u = {"input_tokens": 10_000, "output_tokens": 2_000, "cache_creation_input_tokens": 1_000,
         "cache_read_input_tokens": 4_000, "server_tool_use": {"web_search_requests": 3}}
    got = budget.anthropic_usage_cost(u, "claude-haiku-5-5", {})
    want = 10_000 * 0.1e-6 + 1_000 * 0.1e-6 * 1.25 + 4_000 * 0.1e-6 * 0.1 + 2_000 * 0.5e-6 + 3 * 0.01
    assert got == pytest.approx(want)
    assert budget.anthropic_usage_cost({"input_tokens": 1e6, "output_tokens": 0}, "claude-opus-5-5", {}) == pytest.approx(4)
    # Haiku 5.5 prompt over 100K tokens: higher rate card
    assert budget.anthropic_usage_cost({"input_tokens": 200_000}, "claude-haiku-5-5", {}) == pytest.approx(0.1)
    # env price override, 'in/out' per million tokens
    env = {"ANTHROPIC_FORECAST_PRICE": "5/25"}
    assert budget.anthropic_usage_cost({"output_tokens": 1e6}, "claude-opus-5-5", env) == pytest.approx(25)


def test_usage_cost_with_fallback_iterations_prices_each_attempt():
    u = {"input_tokens": 100, "output_tokens": 100,
         "iterations": [{"type": "message", "model": "claude-opus-5-5", "input_tokens": 1e6, "output_tokens": 0},
                        {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 1e6,
                         "output_tokens": 0}]}
    assert budget.anthropic_usage_cost(u, "claude-opus-5-5", {}) == pytest.approx(4 + 5)


def test_credit_period_and_ledger_reset(tmp_path):
    assert budget.credit_period(date(2026, 10, 23), 13) == (date(2026, 10, 13), date(2026, 11, 13))
    assert budget.credit_period(date(2026, 11, 12), 13) == (date(2026, 10, 13), date(2026, 11, 13))
    assert budget.credit_period(date(2026, 1, 5), 13) == (date(2025, 12, 13), date(2026, 1, 13))
    assert budget.credit_reset_day({"ANTHROPIC_CREDIT_RESET_DAY": "31"}) == 28
    assert budget.credit_reset_day({"ANTHROPIC_CREDIT_RESET_DAY": "x"}) == 13
    store = state.PendingComments.load(tmp_path)
    assert store.found is False
    store.add_spend("2026-10-13", 1.5)
    store.add_spend("2026-10-13", 0.25)
    again = state.PendingComments.load(tmp_path)  # persisted, next to the other state
    assert again.found and again.period_spent("2026-10-13") == pytest.approx(1.75)
    assert again.period_spent("2026-11-13") == 0.0
    again.add_spend("2026-11-13", 0.5)  # new period starts from zero
    assert state.PendingComments.load(tmp_path).spend["spent"] == pytest.approx(0.5)
    lk = budget.anthropic_lookup_from_store(ENV, again, date(2026, 11, 20))
    assert lk.info.usage == pytest.approx(0.5) and lk.info.remaining == pytest.approx(89.5)


def test_each_call_is_booked_via_the_recorder(tmp_path):
    store = state.PendingComments.load(tmp_path)
    record = main.make_spend_recorder(store, ENV)
    u = {"input_tokens": 1_000, "output_tokens": 1_000, "server_tool_use": {"web_search_requests": 2}}
    ll = main.build_anthropic_llms("claude-opus-5-5", "claude-haiku-5-5", FakeAnthropic(lambda kw: resp(usage=u)),
                                   ENV, record, retry_wait_s=0)
    asyncio.run(ORIG_INVOKE(ll["researcher"], "search"))
    start, _ = budget.credit_period(budget.utc_today(), 13)
    assert store.period_spent(start.isoformat()) == pytest.approx(1_000 * 0.1e-6 + 1_000 * 0.5e-6 + 0.02)


# ------------------------------------------------------------ errors


@pytest.mark.parametrize("cls,status,msg,want", [
    (anthropic.BadRequestError, 400, "Your credit balance is too low to access the Anthropic API.", 402),
    (anthropic.BadRequestError, 400, "You have reached your specified API usage limits.", 402),
    (anthropic.APIStatusError, 402, "billing problem", 402),
    (anthropic.RateLimitError, 429, "You have reached your monthly spend cap.", 402),
    (anthropic.AuthenticationError, 401, "invalid x-api-key", 401),
    (anthropic.BadRequestError, 400, "messages: roles must alternate", None),
    (anthropic.RateLimitError, 429, "Number of request tokens has exceeded your rate limit", None),
])
def test_credit_and_auth_errors_map_to_fatal(cls, status, msg, want):
    calls = []

    def responder(kw):
        calls.append(1)
        return api_error(cls, status, msg)

    ll = llms(FakeAnthropic(responder))
    with pytest.raises(Exception) as e:
        asyncio.run(ORIG_INVOKE(ll["default"], "x"))
    assert runner.llm_fatal_status(e.value) == want
    if want or cls is anthropic.BadRequestError:
        assert len(calls) == 1  # fatal / bad request: no retry at this layer


def test_web_search_error_object_and_pause_turn():
    err = SimpleNamespace(type="web_search_tool_result", content=SimpleNamespace(error_code="too_many_requests"))
    cite = SimpleNamespace(url="https://example.invalid/a")
    first = resp(text="Part one. ", stop="pause_turn", extra=[err])
    first.content[-1].citations = [cite]
    first.content[0].to_dict = lambda: {"type": "web_search_tool_result"}
    first.content[1].to_dict = lambda: {"type": "text", "text": "Part one. "}
    replies = [first, resp(text="Part two.")]
    client = FakeAnthropic(lambda kw: replies.pop(0))
    out = asyncio.run(ORIG_INVOKE(llms(client)["researcher"], "search"))
    assert out.startswith("Part one. Part two.") and "https://example.invalid/a" in out
    assert len(client.calls) == 2 and client.calls[1]["messages"][-1]["role"] == "assistant"


def test_probe_ok_and_credit_exhausted():
    booked = []
    client = FakeAnthropic()
    asyncio.run(main.make_anthropic_probe("claude-haiku-5-5", client, booked.append, ENV)())
    kw = client.calls[0]
    assert kw["max_tokens"] == 16 and not [k for k in SAMPLING if k in kw]
    assert client.options["max_retries"] == 0 and len(booked) == 1
    bad = FakeAnthropic(lambda kw: api_error(anthropic.BadRequestError, 400, "Your credit balance is too low"))
    with pytest.raises(main.AnthropicCreditError):
        asyncio.run(main.make_anthropic_probe("claude-haiku-5-5", bad, booked.append, ENV)())


# ------------------------------------------------------------ pacing and full runs

OPUS, HAIKU = budget.anthropic_price("claude-opus-5-5", {}), budget.anthropic_price("claude-haiku-5-5", {})
A_MODELS = {k: ("claude-opus-5-5", "claude-haiku-5-5") for k in budget.KINDS}
A_PRICES = {k: (OPUS, HAIKU) for k in budget.KINDS}


def test_pacing_uses_budget_until_next_reset_not_season_end():
    today = date(2026, 10, 23)
    reset = budget.pacing_horizon({**ENV}, today)
    assert reset == date(2026, 11, 13)
    p = budget.pace(60.0, today, {}, {}, A_PRICES, reset, "anthropic")
    days = (reset - today).days  # 21 days x 4 questions/day per tournament
    assert p.kinds["main"].remaining_questions == days * 4
    assert p.kinds["main"].per_question_budget == pytest.approx((60.0 - 3.0) / (2 * days * 4))
    season = budget.pace(60.0, today, {}, {}, A_PRICES, None, "anthropic")
    assert season.kinds["main"].per_question_budget < p.kinds["main"].per_question_budget
    assert budget.pacing_horizon({"OPENROUTER_API_KEY": "x"}, today) is None


def _anthropic_run(client, store, env, publish=False, factory=None, today=date(2026, 10, 23)):
    env = {**ENV, **env}
    return asyncio.run(runner.run(
        mode="tournament", publish=publish, bot_factory=factory, client=client,
        key_lookup_fn=lambda: budget.anthropic_lookup_from_store(env, store, today),
        version=main.VERSION, models=A_MODELS, prices=A_PRICES, env=env, tournaments=TOURNAMENTS,
        store=store, today=today, concurrency=1))


def _two_questions():
    qs = load_fixture_questions()
    return FakeClient({"FutureEval Fall 2026": [q for q in qs["FutureEval Fall 2026"] if q.id_of_post in (101, 103)],
                       "MiniBench": []})


def answer(kw):
    text = kw["messages"][-1]["content"]
    if "research assistant" in text:
        return resp("Fake research: base rate about 30%.", usage={"input_tokens": 20_000, "output_tokens": 2_000,
                                                                     "server_tool_use": {"web_search_requests": 2}})
    if "summarize" in text.lower():
        return resp("Fake summary.")
    if "The options are" in text:
        return resp("Outside view...\nAlpha: 70%\nBeta: 30%\nGamma: 0%", usage={"input_tokens": 4_000, "output_tokens": 6_000})
    return resp("Outside view... Inside view...\nProbability: 40%", usage={"input_tokens": 4_000, "output_tokens": 6_000})


def test_dry_run_on_anthropic_books_spend_and_sends_nothing(fake_llm, tmp_path, monkeypatch):
    monkeypatch.setattr(main.AnthropicLlm, "invoke", ORIG_INVOKE)  # real request path, fake transport
    client, fake = _two_questions(), FakeAnthropic(answer)
    store = state.PendingComments.load(tmp_path)
    record = main.make_spend_recorder(store, ENV)
    factory = lambda kind, tier: main.make_bot(*A_MODELS[kind], tier.forecasts, client, main.build_anthropic_llms(  # noqa: E731
        *A_MODELS[kind], fake, ENV, record, retry_wait_s=0))
    r = _anthropic_run(client, store, {"DRY_RUN_LLM": "1"}, factory=factory, today=budget.utc_today())
    assert r.count("dry_run") == 2 and not client.predictions and not client.comments  # PUBLISH unset: nothing sent
    assert fake.calls and all(not [k for k in SAMPLING if k in kw] for kw in fake.calls)
    assert r.provider == "anthropic" and r.spend > 0 and r.spend == pytest.approx(r.usage_after)
    text = runner.render_summary(r)
    assert "Provider：anthropic" in text and "Anthropic 本期花費" in text and "Anthropic 剩餘" in text
    assert "No saved spend record" in text  # empty state at the start of this run -> yellow box
    assert r.decision.warning


def test_credit_exhausted_mid_run_stops_everything_and_counts_nothing(tmp_path):
    client = _two_questions()
    store = state.PendingComments.load(tmp_path)

    class Bot:
        guard_notes = {}

        @staticmethod
        def question_key(q):
            return q.page_url

        async def forecast_questions(self, qs, return_exceptions=False):
            raise main.AnthropicCreditError("Anthropic credit/spend limit reached (HTTP 400): credit balance too low")

    r = _anthropic_run(client, store, {}, publish=True, factory=lambda kind, tier: Bot())
    assert r.fatal.startswith("Anthropic HTTP 402") and r.exit_code == 1
    assert r.count("failed") == 1 and r.count("stopped") == 1
    assert store.failures == {}  # rolled back: not this question's fault


def test_budget_stop_from_local_ledger(tmp_path):
    store = state.PendingComments.load(tmp_path)
    start, _ = budget.credit_period(date(2026, 10, 23), 13)
    store.add_spend(start.isoformat(), 88.0)  # $90 budget -> $2 left < BUDGET_STOP_BELOW $3
    r = _anthropic_run(_two_questions(), store, {}, publish=True, factory=lambda k, t: None)
    assert r.decision.state == "stop" and r.count("skipped_budget") == 2


def test_precheck_on_anthropic_uses_local_ledger_and_never_calls_openrouter(tmp_path):
    from datetime import datetime, timezone

    calls = []

    def get_json(url, headers=None, timeout=20):
        calls.append(url)
        if "tournaments=" in url:
            return {"results": [{"id": 5, "title": "New", "question": {
                "id": 51, "type": "binary", "open_time": "2026-10-20T00:00:00Z",
                "scheduled_close_time": "2026-10-30T00:00:00Z", "my_forecasts": {"history": []}}}], "next": None}
        raise AssertionError(f"unexpected URL {url}")

    e = {"METACULUS_TOKEN": "t", "ANTHROPIC_API_KEY": "k", "GITHUB_EVENT_NAME": "schedule", "BOT_ENABLED": "1",
         "PUBLISH": "1", "REPO_PRIVATE": "false", "STATE_DIR": str(tmp_path)}
    deps = precheck.Deps(get_json=get_json, post=lambda *a, **k: 200, sleep=lambda s: None,
                         commit_age=lambda: 1.0, now=lambda: datetime(2026, 10, 23, tzinfo=timezone.utc))
    code, work, text = precheck.run(e, deps)
    assert code == 0 and work is True and not [c for c in calls if "openrouter" in c]
    assert "Provider：anthropic" in text and "No saved spend record" in text and "已花 $0.00／預算 $90.00" in text
    store = state.PendingComments.load(tmp_path)
    store.add_spend("2026-10-13", 88.5)
    code, work, text = precheck.run(e, deps)
    assert code == 1 and work is False and "Anthropic：remaining $1.50" in text  # below BUDGET_STOP_BELOW
