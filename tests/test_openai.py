"""v0.3.0 OpenAI path. Offline: a fake OpenAI client, no network, no real key."""
import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

import httpx
import openai
import pytest
from forecasting_tools import GeneralLlm

import budget
import main
import precheck
import runner
import state
from conftest import TOURNAMENTS, FakeClient, load_fixture_questions

ORIG_INVOKE = GeneralLlm.invoke
ENV = {"OPENAI_API_KEY": "test-key-not-real"}
SAMPLING = ("temperature", "top_p", "top_logprobs")
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def resp(text="ok", usage=None, searches=0, urls=(), status="completed"):
    ann = [SimpleNamespace(type="url_citation", url=u) for u in urls]
    out = [SimpleNamespace(type="web_search_call", action=SimpleNamespace(type="search")) for _ in range(searches)]
    if text:
        out.append(SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text=text, annotations=ann)]))
    return SimpleNamespace(output=out, status=status, incomplete_details=None,
                           usage=usage or {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500})


class FakeOpenAI:
    """client.responses.create(**kw) -> responder(kw); exceptions are raised."""

    def __init__(self, responder=None):
        self.calls, self.options = [], None
        self.responder = responder or (lambda kw: resp())
        self.responses = SimpleNamespace(create=self._create)

    async def _create(self, **kw):
        self.calls.append(kw)
        r = self.responder(kw)
        if isinstance(r, BaseException):
            raise r
        return r

    def with_options(self, **kw):
        self.options = kw
        return self


def api_error(cls, status, message, code=None):
    req = httpx.Request("POST", "https://api.openai.invalid/v1/responses")
    body = {"message": message, "type": code or "invalid_request_error", "code": code}
    return cls(message, response=httpx.Response(status, request=req), body=body)


def ledger_llms(tmp_path, responder=None, env=None):
    env = {**ENV, **(env or {})}
    store = state.PendingComments.load(tmp_path)
    client = FakeOpenAI(responder)
    ll = main.build_openai_llms("gpt-5.4", "gpt-5.4-mini", client, env, main.make_openai_recorder(store, env),
                                retry_wait_s=0)
    return ll, client, store


# ------------------------------------------------------------ provider selection


def test_provider_order_and_switch():
    sel = budget.select_provider
    assert sel({"OPENAI_API_KEY": "z"}) == "openai"
    assert sel({"ANTHROPIC_API_KEY": "y", "OPENAI_API_KEY": "z"}) == "anthropic"
    assert sel({"OPENROUTER_API_KEY": "x", "ANTHROPIC_API_KEY": "y", "OPENAI_API_KEY": "z"}) == "openrouter"
    assert sel({"OPENROUTER_API_KEY": "x", "LLM_PROVIDER": "OpenAI"}) == "openai"
    assert not precheck.validate_switches({"LLM_PROVIDER": "openai"})
    assert any("openai" in e for e in precheck.validate_switches({"LLM_PROVIDER": "gpt"}))
    d = budget.decide(budget.openai_lookup({}, 0.0, {}, True), {})
    assert d.state == "auth_error" and "OPENAI_API_KEY" in d.reason


def test_default_models_are_in_the_free_lists():
    f, r, p = budget.openai_models({})
    assert (f, r, p) == ("gpt-5.4", "gpt-5.4-mini", "gpt-5.4-mini")
    assert budget.free_group(f) == "big" and budget.free_group(r) == "mini"
    assert budget.free_group("gpt-5.4-2026-03-05") == "big" and budget.free_group("gpt-5.5") is None


# ------------------------------------------------------------ request shape


def test_request_shape_reasoning_effort_and_web_search(tmp_path):
    ll, client, _ = ledger_llms(tmp_path)
    for role in ("default", "researcher", "parser"):
        asyncio.run(ORIG_INVOKE(ll[role], f"prompt for {role}"))
    fc, rc, pc = client.calls
    for kw in client.calls:
        assert not [k for k in SAMPLING if k in kw]
        assert kw["input"][-1]["role"] == "user"
    assert fc["model"] == "gpt-5.4" and fc["reasoning"] == {"effort": "medium"} and "tools" not in fc
    assert rc["model"] == "gpt-5.4-mini" and rc["reasoning"] == {"effort": "low"}
    assert rc["tools"] == [{"type": "web_search", "search_context_size": "low"}] and rc["max_tool_calls"] == 3
    assert pc["reasoning"] == {"effort": "low"} and "tools" not in pc


# ------------------------------------------------------------ free tokens and dollars


def test_call_charge_free_crossing_tool_and_non_free_model():
    u = {"input_tokens": 6_000, "output_tokens": 4_000, "total_tokens": 10_000}
    full = 6_000 * 2.5e-6 + 4_000 * 15e-6
    assert budget.openai_call_charge(u, "gpt-5.4", {}, {"big": 0}) == ("big", 10_000, 0.0)
    assert budget.openai_call_charge(u, "gpt-5.4", {}, {"big": 240_000}) == ("big", 10_000, 0.0)  # exactly at the limit
    g, t, usd = budget.openai_call_charge(u, "gpt-5.4", {}, {"big": 240_001})  # crosses: whole request paid
    assert (g, t) == ("big", 10_000) and usd == pytest.approx(full)
    g, t, usd = budget.openai_call_charge(u, "gpt-5.4-mini", {}, {}, web_searches=2, used_tool=True)
    assert (g, t) == (None, 0) and usd == pytest.approx(6_000 * 0.75e-6 + 4_000 * 4.5e-6 + 0.02)
    g, t, usd = budget.openai_call_charge(u, "gpt-5.5", {}, {})  # not in the free lists: unknown price, dear
    assert g is None and usd == pytest.approx(6_000 * 5e-6 + 4_000 * 30e-6)
    cached = {"input_tokens": 1e6, "input_tokens_details": {"cached_tokens": 1e6}, "output_tokens": 0}
    assert budget.openai_token_cost(cached, "gpt-5.4", {}) == pytest.approx(0.25)
    assert budget.openai_call_charge(u, "gpt-5.4-mini", {"OPENAI_TOOL_TOKENS_FREE": "1"}, {}, 1, True)[2] == pytest.approx(0.01)


def test_ledger_day_rollover_and_month_budget(tmp_path):
    store = state.PendingComments.load(tmp_path)
    store.add_openai("2026-10-24", "2026-10", "big", 200_000, 0.0)
    store.add_openai("2026-10-24", "2026-10", None, 0, 0.75)
    again = state.PendingComments.load(tmp_path)
    assert again.openai_free_used("2026-10-24") == {"big": 200_000}
    assert again.openai_free_used("2026-10-25") == {}  # 00:00 UTC: allowance back
    again.add_openai("2026-10-25", "2026-10", "mini", 5_000, 0.1)
    assert again.openai_free_used("2026-10-25") == {"mini": 5_000}
    assert again.openai_month_spent("2026-10") == pytest.approx(0.85)
    again.add_openai("2026-11-01", "2026-11", None, 0, 0.2)
    assert again.openai_month_spent("2026-11") == pytest.approx(0.2) and again.openai_month_spent("2026-10") == 0.0
    lk = budget.openai_lookup_from_store(ENV, again, date(2026, 11, 1))
    assert lk.info.remaining == pytest.approx(4.3) and lk.free == {"big": (0, 250_000), "mini": (0, 2_500_000)}
    assert budget.decide(lk, ENV).state == "ok"
    again.add_openai("2026-11-01", "2026-11", None, 0, 4.1)  # $0.20 left < OPENAI_BUDGET_STOP_BELOW $0.25
    d = budget.decide(budget.openai_lookup_from_store(ENV, again, date(2026, 11, 1)), ENV)
    assert d.state == "stop" and "0.20" in d.reason


def test_recorder_books_free_tokens_then_paid_overflow(tmp_path):
    big = {"input_tokens": 100_000, "output_tokens": 40_000, "total_tokens": 140_000}
    ll, client, store = ledger_llms(tmp_path, lambda kw: resp(usage=big, searches=2 if "tools" in kw else 0))
    asyncio.run(ORIG_INVOKE(ll["default"], "one"))
    today = budget.utc_today()
    assert store.openai_free_used(today.isoformat()) == {"big": 140_000}
    assert store.openai_month_spent(budget.month_key(today)) == 0.0
    asyncio.run(ORIG_INVOKE(ll["default"], "two"))  # 280K > 250K: this request is paid in full
    assert store.openai_month_spent(budget.month_key(today)) == pytest.approx(100_000 * 2.5e-6 + 40_000 * 15e-6)
    before = store.openai_month_spent(budget.month_key(today))
    asyncio.run(ORIG_INVOKE(ll["researcher"], "search"))  # tool call: paid, not counted as free tokens
    assert store.openai_free_used(today.isoformat()) == {"big": 280_000}
    assert store.openai_month_spent(budget.month_key(today)) - before == pytest.approx(
        100_000 * 0.75e-6 + 40_000 * 4.5e-6 + 0.02)


# ------------------------------------------------------------ pacing plan


def _qs(*hours):
    return [("main", NOW + __import__("datetime").timedelta(hours=h)) for h in hours]


def test_plan_picks_richest_tier_that_fits_today():
    env = {"MAIN_QUESTIONS_PER_DAY": "0", "MINIBENCH_QUESTIONS_PER_DAY": "0"}
    per_full = budget.big_tokens_per_question(budget.TIERS[0])
    plan = budget.openai_plan(_qs(10, 50), {"big": (0, 250_000)}, NOW, env)
    assert plan.tiers["main"].name == ("full" if 2 * per_full <= 225_000 else "standard") and not plan.deferred
    plan = budget.openai_plan(_qs(*[50] * 8), {"big": (0, 250_000)}, NOW, env)  # 8 x full does not fit
    assert plan.tiers["main"].forecasts < 5 and not plan.deferred and not plan.paid
    # questions expected later today are reserved for: 12 h left x 8/day = 4 more
    busy = budget.openai_plan(_qs(50), {"big": (0, 250_000)}, NOW, {})
    assert busy.reserved_later == 4 and busy.tiers["main"].forecasts <= plan.tiers["main"].forecasts + 2


def test_plan_defers_far_questions_and_pays_near_ones_when_free_tokens_are_gone():
    env = {"MAIN_QUESTIONS_PER_DAY": "0", "MINIBENCH_QUESTIONS_PER_DAY": "0"}
    plan = budget.openai_plan(_qs(5, 29, 31, 200), {"big": (250_000, 250_000)}, NOW, env)
    assert plan.tiers["main"].name == "minimum"
    assert plan.paid == [0, 1] and plan.deferred == [2, 3]
    one = budget.big_tokens_per_question(budget.TIERS[-1])
    left = 2 * one + 1000  # room for exactly two minimum questions
    plan = budget.openai_plan(_qs(40, 41, 42, 5), {"big": (int(250_000 * 0.9) - left, 250_000)}, NOW, env)
    assert plan.paid == [3] and plan.deferred == [2]  # first two free, third far -> wait, last near -> paid
    no_defer = budget.openai_plan(_qs(200), {"big": (250_000, 250_000)}, NOW, env, defer=False)
    assert no_defer.paid == [0] and not no_defer.deferred  # Test Bot: never deferred


# ------------------------------------------------------------ errors


@pytest.mark.parametrize("cls,status,code,msg,want", [
    (openai.RateLimitError, 429, "insufficient_quota", "You exceeded your current quota", 402),
    (openai.RateLimitError, 429, "organization_spend_limit_exceeded", "Organization spend limit reached", 402),
    (openai.RateLimitError, 429, "project_spend_limit_exceeded", "Project spend limit reached", 402),
    (openai.RateLimitError, 429, "credit_balance_exhausted", "No credits left", 402),
    (openai.AuthenticationError, 401, "invalid_api_key", "Incorrect API key provided", 401),
    (openai.RateLimitError, 429, None, "Rate limit reached for requests", None),
    (openai.BadRequestError, 400, None, "Unsupported parameter", None),
])
def test_quota_spend_limit_and_auth_errors_map_to_fatal(tmp_path, cls, status, code, msg, want):
    calls = []

    def responder(kw):
        calls.append(1)
        return api_error(cls, status, msg, code)

    ll, _, _ = ledger_llms(tmp_path, responder)
    with pytest.raises(Exception) as e:
        asyncio.run(ORIG_INVOKE(ll["default"], "x"))
    assert runner.llm_fatal_status(e.value) == want
    if want or cls is openai.BadRequestError:
        assert len(calls) == 1  # fatal / bad request: no retry at this layer
    assert runner.llm_fatal_status("forecaster error: insufficient_quota") == 402  # report.errors text


def test_probe_ok_and_quota_exhausted(tmp_path):
    store = state.PendingComments.load(tmp_path)
    rec = main.make_openai_recorder(store, ENV)
    client = FakeOpenAI()
    asyncio.run(main.make_openai_probe("gpt-5.4-mini", client, rec)())
    kw = client.calls[0]
    assert kw["max_output_tokens"] == 16 and client.options["max_retries"] == 0 and "temperature" not in kw
    assert store.openai_free_used(budget.utc_today().isoformat()) == {"mini": 1500}
    bad = FakeOpenAI(lambda kw: api_error(openai.RateLimitError, 429, "quota", "insufficient_quota"))
    with pytest.raises(main.OpenAIQuotaError):
        asyncio.run(main.make_openai_probe("gpt-5.4-mini", bad, rec)())


# ------------------------------------------------------------ full runs

O_MODELS = {k: ("gpt-5.4", "gpt-5.4-mini") for k in budget.KINDS}
O_PRICES = {k: ((2.5e-6, 15e-6), (0.75e-6, 4.5e-6)) for k in budget.KINDS}


class Bot:
    guard_notes = {}

    def __init__(self, exc=None):
        self.exc, self.seen = exc, []

    @staticmethod
    def question_key(q):
        return q.page_url

    async def forecast_questions(self, qs, return_exceptions=False):
        self.seen += [q.id_of_post for q in qs]
        if self.exc:
            raise self.exc
        return [SimpleNamespace(question=qs[0], errors=[], price_estimate=0.0, explanation="# x")]


def _two_questions():  # 103 closes 2026-10-20 (past -> near), 101 closes 2026-11-01 (far)
    qs = load_fixture_questions()
    return FakeClient({"FutureEval Fall 2026": [q for q in qs["FutureEval Fall 2026"] if q.id_of_post in (101, 103)],
                       "MiniBench": []})


def _run(store, bot, env=None, publish=False):
    env = {**ENV, "DRY_RUN_LLM": "1", **(env or {})}
    return asyncio.run(runner.run(
        mode="tournament", publish=publish, bot_factory=lambda k, t: bot, client=_two_questions(),
        key_lookup_fn=lambda: budget.openai_lookup_from_store(env, store, NOW.date()), version=main.VERSION,
        models=O_MODELS, prices=O_PRICES, env=env, tournaments=TOURNAMENTS, store=store, today=NOW.date(),
        now=NOW, concurrency=1, provider="openai"))


def test_run_defers_far_question_when_free_tokens_are_used_up(tmp_path):
    store = state.PendingComments.load(tmp_path)
    store.add_openai("2026-10-24", "2026-10", "big", 250_000, 1.0)
    bot = Bot()
    r = _run(store, bot)
    assert bot.seen == [103] and r.count("dry_run") == 1  # near one answered (paid), far one waits
    assert [d["url"] for d in r.deferred] == ["https://example.invalid/questions/101/"]
    assert r.paid_overflow == 1 and r.pacing.kinds["main"].tier.name == "minimum"
    text = runner.render_summary(r)
    assert "Provider：openai" in text and "1 題延後" in text and "big 已用 250,000／250,000（剩 0）" in text
    assert "OpenAI 本月付費（2026-10，本機記帳）：$1.00／預算 $4.50" in text
    assert store.failures == {}  # deferred questions are never counted as failures


def test_run_next_utc_day_answers_both_again(tmp_path):
    store = state.PendingComments.load(tmp_path)
    store.add_openai("2026-10-23", "2026-10", "big", 250_000, 1.0)  # yesterday's usage
    bot = Bot()
    r = _run(store, bot)
    assert sorted(bot.seen) == [101, 103] and not r.deferred and r.paid_overflow == 0


def test_quota_exhausted_mid_run_stops_everything_and_counts_nothing(tmp_path):
    store = state.PendingComments.load(tmp_path)
    r = _run(store, Bot(main.OpenAIQuotaError("OpenAI quota/spend limit reached (HTTP 429, insufficient_quota)")),
             publish=True)
    assert r.fatal.startswith("OpenAI HTTP 402") and r.exit_code == 1
    assert r.count("failed") == 1 and r.count("stopped") == 1 and store.failures == {}


def test_month_budget_stop_skips_llm(tmp_path):
    store = state.PendingComments.load(tmp_path)
    store.add_openai("2026-10-24", "2026-10", None, 0, 4.4)
    r = _run(store, None, publish=True)
    assert r.decision.state == "stop" and r.count("skipped_budget") == 2


def test_precheck_on_openai_shows_usage_and_no_work_when_everything_is_deferred(tmp_path):
    def get_json(url, headers=None, timeout=20):
        if "tournaments=" in url and "tournaments=33121" in url:
            return {"results": [{"id": 5, "title": "Far", "question": {
                "id": 51, "type": "binary", "open_time": "2026-10-20T00:00:00Z",
                "scheduled_close_time": "2026-11-30T00:00:00Z", "my_forecasts": {"history": []}}}], "next": None}
        if "tournaments=" in url:
            return {"results": [], "next": None}
        raise AssertionError(f"unexpected URL {url}")

    e = {"METACULUS_TOKEN": "t", "OPENAI_API_KEY": "k", "GITHUB_EVENT_NAME": "schedule", "BOT_ENABLED": "1",
         "PUBLISH": "1", "REPO_PRIVATE": "false", "STATE_DIR": str(tmp_path)}
    deps = precheck.Deps(get_json=get_json, post=lambda *a, **k: 200, sleep=lambda s: None,
                         commit_age=lambda: 1.0, now=lambda: NOW)
    code, work, text = precheck.run(e, deps)
    assert code == 0 and work is True and "Provider：openai" in text and "No saved usage record" in text
    assert "big 已用 0／250,000" in text and "預算 $4.50" in text
    state.PendingComments.load(tmp_path).add_openai("2026-10-24", "2026-10", "big", 250_000, 0.5)
    code, work, text = precheck.run(e, deps)
    assert code == 0 and work is False and "1 題延後" in text
