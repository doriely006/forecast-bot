"""v0.4.0 per-tournament provider (MAIN_/MINIBENCH_LLM_PROVIDER[_FROM]). Offline: fake bots, no network."""
import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

import budget
import main
import precheck
import runner
import state
from conftest import TOURNAMENTS, FakeClient

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
DAY = NOW.date()
KEYS = {"OPENAI_API_KEY": "o-not-real", "ANTHROPIC_API_KEY": "a-not-real"}
SPLIT = {**KEYS, "LLM_PROVIDER": "openai", "ANTHROPIC_BUDGET_USD": "15",
         "MINIBENCH_LLM_PROVIDER": "anthropic", "MINIBENCH_LLM_PROVIDER_FROM": "2026-11-01"}
O_MODELS, A_MODELS = ("gpt-5.4", "gpt-5.4-mini"), ("claude-opus-5-5", "claude-haiku-5-5")
O_PRICE, A_PRICE = ((2.5e-6, 15e-6), (0.75e-6, 4.5e-6)), ((4e-6, 20e-6), (0.1e-6, 0.5e-6))


# ------------------------------------------------------------ routing and date gate


@pytest.mark.parametrize("env", [
    {}, {"OPENAI_API_KEY": "z"}, {"ANTHROPIC_API_KEY": "y", "OPENAI_API_KEY": "z"},
    {"OPENROUTER_API_KEY": "x", "ANTHROPIC_API_KEY": "y"}, {"LLM_PROVIDER": "anthropic", "OPENAI_API_KEY": "z"},
])
def test_unset_vars_keep_todays_behaviour(env):
    base = budget.select_provider(env)
    assert budget.tournament_providers(env, DAY) == {"main": (base, ""), "minibench": (base, "")}
    assert budget.provider_env(env, base) == env  # nothing added: every provider function sees the same env
    assert budget.pace_kinds(env) == budget.KINDS and not budget.provider_schedule_notes(env, DAY)
    p = budget.pace(50.0, DAY, env, {"main": 2, "minibench": 2}, {k: O_PRICE for k in budget.KINDS})
    assert p == budget.pace(50.0, DAY, budget.provider_env(env, base), {"main": 2, "minibench": 2},
                            {k: O_PRICE for k in budget.KINDS})
    assert p.kinds["minibench"].tier.name == "minimum"  # C6 split rule untouched when both share one provider


@pytest.mark.parametrize("day,want", [(date(2026, 10, 31), "openai"), (date(2026, 11, 1), "anthropic"),
                                      (date(2026, 11, 2), "anthropic")])
def test_minibench_switches_on_its_from_date_main_stays(day, want):
    plan = budget.tournament_providers(SPLIT, day)
    assert plan["minibench"] == (want, "") and plan["main"] == ("openai", "")
    notes = budget.provider_schedule_notes(SPLIT, day)
    assert bool(notes) == (want == "openai")
    assert budget.tournament_provider({**SPLIT, "MINIBENCH_LLM_PROVIDER_FROM": ""}, "minibench", day)[0] == "anthropic"
    main_too = {**SPLIT, "MAIN_LLM_PROVIDER": "anthropic", "MAIN_LLM_PROVIDER_FROM": "2026-11-02"}
    assert budget.tournament_provider(main_too, "main", day)[0] == ("anthropic" if day >= date(2026, 11, 2) else "openai")


def test_missing_key_falls_back_to_global_with_note():
    env = {k: v for k, v in SPLIT.items() if k != "ANTHROPIC_API_KEY"}
    provider, note = budget.tournament_provider(env, "minibench", DAY)
    assert provider == "openai" and "ANTHROPIC_API_KEY" in note and "minibench" in note
    assert not precheck.validate_switches(env)


def test_bad_values_are_switch_errors():
    errs = precheck.validate_switches({"MINIBENCH_LLM_PROVIDER": "claude", "MAIN_LLM_PROVIDER_FROM": "Nov 1"})
    assert any("MINIBENCH_LLM_PROVIDER=\"claude\"" in e for e in errs)
    assert any("MAIN_LLM_PROVIDER_FROM=\"Nov 1\"" in e for e in errs)
    assert not precheck.validate_switches(SPLIT)


# ------------------------------------------------------------ pacing per provider


def test_openai_pacing_counts_only_main_when_minibench_is_elsewhere():
    env = {"MAIN_QUESTIONS_PER_DAY": "4", "MINIBENCH_QUESTIONS_PER_DAY": "4"}
    both = budget.openai_plan([("main", None)], {"big": (0, 250_000)}, NOW, env)
    solo_env = budget.provider_env(env, "openai", ["main"])
    solo = budget.openai_plan([("main", None)], {"big": (0, 250_000)}, NOW, solo_env)
    assert both.reserved_later == 4 and solo.reserved_later == 2 and set(solo.tiers) == {"main"}
    p = budget.openai_pacing(solo, 4.25, DAY, solo_env, {"main": 1})
    assert set(p.kinds) == {"main"} and p.kinds["main"].per_question_budget == pytest.approx(4.0 / (30 * 4))  # 30 days to month end x 4/day


def test_anthropic_minibench_pace_uses_its_own_budget_and_no_c6_minimum():
    env = budget.provider_env(SPLIT, "anthropic", ["minibench"])
    reset = budget.credit_period(DAY, budget.credit_reset_day(env))[1]  # 2026-11-13
    prices = {"minibench": A_PRICE}
    p = budget.pace(15.0, DAY, env, {"minibench": 3}, prices, reset, "anthropic")
    assert set(p.kinds) == {"minibench"} and p.kinds["minibench"].per_question_budget == pytest.approx(12 / 48)
    rich = budget.pace(100.0, DAY, env, {"minibench": 3}, prices, reset, "anthropic")
    assert rich.kinds["minibench"].tier.name == "full"  # dedicated budget: tier by fit, not forced minimum


# ------------------------------------------------------------ split run: isolation


class Report:
    def __init__(self, q, cost):
        self.question, self.errors, self.price_estimate, self.explanation = q, [], cost, "# fake"

    async def publish_report_to_metaculus(self, metaculus_client):
        metaculus_client.post_binary_question_prediction(self.question.id_of_question, 0.5)


class Bot:
    guard_notes: dict = {}

    def __init__(self, exc=None, on_call=None):
        self.exc, self.on_call, self.seen = exc, on_call, []

    @staticmethod
    def question_key(q):
        return q.page_url

    async def forecast_questions(self, qs, return_exceptions=False):
        self.seen += [q.id_of_post for q in qs]
        if self.exc:
            raise self.exc
        cost = self.on_call() if self.on_call else 0.0
        return [Report(qs[0], cost)]


def groups(store, main_bot, mb_bot, env=SPLIT, main_provider="openai", mb_provider="anthropic"):
    def group(kind, provider, bot, tour):
        genv = budget.provider_env(env, provider, [kind])
        look = (lambda: budget.anthropic_lookup_from_store(genv, store, DAY)) if provider == "anthropic" \
            else (lambda: budget.openai_lookup_from_store(genv, store, DAY))
        models = A_MODELS if provider == "anthropic" else O_MODELS
        prices = A_PRICE if provider == "anthropic" else O_PRICE
        return runner.Group(kind, provider, genv, [tour], look, lambda k, t: bot, {kind: models}, {kind: prices})

    return [group("main", main_provider, main_bot, TOURNAMENTS[0]),
            group("minibench", mb_provider, mb_bot, TOURNAMENTS[1])]


def split_run(store, gs, publish=True):
    client = FakeClient()
    s = asyncio.run(runner.run_split(mode="tournament", publish=publish, groups=gs, client=client,
                                     version=main.VERSION, store=store, today=DAY, now=NOW, concurrency=1))
    return s, client


def test_anthropic_credit_out_stops_only_minibench_main_still_submits(tmp_path):
    store = state.PendingComments.load(tmp_path)
    credit = main.AnthropicCreditError("Anthropic credit/spend limit reached (HTTP 400): credit balance too low")
    s, client = split_run(store, groups(store, Bot(), Bot(credit)))
    m, mb = s.groups
    assert m.count("submitted") == 3 and {pid for pid, _ in client.comments} == {101, 103, 104}
    assert mb.fatal.startswith("Anthropic HTTP 402") and mb.count("stopped") == 3 and not m.fatal
    assert s.systemic == [] and s.exit_code == 0 and len(s.demoted) == 1
    assert store.failures == {}  # fatal: nothing counted for MiniBench, main succeeded
    text = runner.render_split_summary(s)
    assert "minibench（Anthropic）這輪停了，另一個賽事照跑" in text and "> [!CAUTION]" not in text
    assert "### main：openai" in text and "### minibench：anthropic" in text and "Provider：anthropic" in text


def test_openai_quota_out_stops_only_main_minibench_still_submits(tmp_path):
    store = state.PendingComments.load(tmp_path)
    quota = main.OpenAIQuotaError("OpenAI quota/spend limit reached (HTTP 429, insufficient_quota)")
    s, client = split_run(store, groups(store, Bot(quota), Bot()))
    assert s.groups[1].count("submitted") == 4 and s.groups[0].fatal.startswith("OpenAI HTTP 402")
    assert s.exit_code == 0 and {pid for pid, _ in client.comments} == {201, 202, 203, 204}


def test_both_providers_out_is_red(tmp_path):
    store = state.PendingComments.load(tmp_path)
    s, _ = split_run(store, groups(store, Bot(main.OpenAIQuotaError("x")), Bot(main.AnthropicCreditError("y"))))
    assert s.exit_code == 1 and len(s.systemic) == 2 and not s.demoted


def test_minibench_budget_used_up_is_yellow_and_main_runs(tmp_path):
    store = state.PendingComments.load(tmp_path)
    store.add_spend("2026-10-13", 13.0)  # $2 left < BUDGET_STOP_BELOW $3
    mb = Bot()
    s, _ = split_run(store, groups(store, Bot(), mb))
    assert s.groups[1].decision.state == "stop" and s.groups[1].count("skipped_budget") == 4 and not mb.seen
    assert s.groups[0].count("submitted") == 3 and s.exit_code == 0


def test_ledgers_and_budgets_are_not_cross_charged(tmp_path):
    store = state.PendingComments.load(tmp_path)
    arecord = main.make_spend_recorder(store, budget.provider_env(SPLIT, "anthropic", ["minibench"]))
    orecord = main.make_openai_recorder(store, budget.provider_env(SPLIT, "openai", ["main"]))
    usage = {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500}
    month, start = budget.month_key(budget.utc_today()), budget.credit_period(
        budget.utc_today(), budget.credit_reset_day(SPLIT))[0].isoformat()
    s, _ = split_run(store, groups(store, Bot(on_call=lambda: orecord("gpt-5.4-mini", usage, 1, False)),
                                   Bot(on_call=lambda: arecord(0.25) or 0.25)))
    m, mb = s.groups
    assert store.period_spent(start) == pytest.approx(4 * 0.25)  # Anthropic ledger: MiniBench only
    assert store.openai_month_spent(month) == pytest.approx(3 * 0.01)  # OpenAI ledger: main only (search fee)
    assert m.provider == "openai" and m.budget_total == pytest.approx(4.5) and set(m.pacing.kinds) == {"main"}
    assert mb.provider == "anthropic" and mb.budget_total == pytest.approx(15) and set(mb.pacing.kinds) == {"minibench"}


def test_one_groups_probe_rollback_keeps_the_other_groups_counts(tmp_path):
    store = state.PendingComments.load(tmp_path)

    async def bad_probe():
        raise RuntimeError("upstream down")

    gs = groups(store, Bot(RuntimeError("main question broke")), Bot(RuntimeError("mb outage")))
    gs[1].probe_fn = bad_probe  # MiniBench: outage -> its +1s rolled back; main has no probe -> counts stay
    s, _ = split_run(store, gs)
    assert s.groups[1].upstream_suspect and set(store.failures) == {"1101", "1103", "1104"}


# ------------------------------------------------------------ precheck


def _get_json(url, headers=None, timeout=20):
    def one(pid):
        return {"id": pid, "title": f"Q{pid}", "question": {
            "id": pid * 10, "type": "binary", "open_time": "2026-10-31T00:00:00Z",
            "scheduled_close_time": "2026-11-01T20:00:00Z", "my_forecasts": {"history": []}}}
    if "tournaments=33121" in url:
        return {"results": [one(5)], "next": None}
    if "tournaments=minibench" in url:
        return {"results": [one(6)], "next": None}
    raise AssertionError(f"unexpected URL {url}")


def _precheck(tmp_path, env, now=NOW):
    e = {"METACULUS_TOKEN": "t", "GITHUB_EVENT_NAME": "schedule", "BOT_ENABLED": "1", "PUBLISH": "1",
         "REPO_PRIVATE": "false", "STATE_DIR": str(tmp_path), **env}
    deps = precheck.Deps(get_json=_get_json, post=lambda *a, **k: 200, sleep=lambda s: None,
                         commit_age=lambda: 1.0, now=lambda: now)
    return precheck.run(e, deps)


def test_precheck_shows_provider_and_budget_per_tournament(tmp_path):
    code, work, text = _precheck(tmp_path, SPLIT)
    assert code == 0 and work is True
    assert "- main（openai）：" in text and "- minibench（anthropic）：" in text
    assert "  - Provider：openai" in text and "  - Provider：anthropic" in text
    assert "  - OpenAI 本月付費（2026-11，本機記帳）：$0.00／預算 $4.50" in text
    assert "  - Anthropic 本期（2026-10-13 起，2026-11-13 重置，本機記帳）：已花 $0.00／預算 $15.00" in text
    assert "  - 免費額度配速：main " in text and "  - 配速（" in text and "minibench: $" in text


def test_precheck_before_from_date_is_single_provider_with_schedule_note(tmp_path):
    code, work, text = _precheck(tmp_path, SPLIT, datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc))
    assert code == 0 and text.count("- Provider：") == 1 and "- Provider：openai" in text
    assert "minibench 預定 2026-11-01（UTC）起改用 anthropic" in text and "（anthropic）：" not in text


def test_precheck_unset_vars_has_no_split_lines(tmp_path):
    code, work, text = _precheck(tmp_path, {"OPENAI_API_KEY": "o"})
    assert code == 0 and text.count("- Provider：openai") == 1 and "（openai）：" not in text and "預定" not in text


def test_precheck_minibench_out_of_budget_is_yellow_main_is_work(tmp_path):
    state.PendingComments.load(tmp_path).add_spend("2026-10-13", 13.0)
    code, work, text = _precheck(tmp_path, SPLIT)
    assert code == 0 and work is True and "minibench（Anthropic）這輪不跑，另一個賽事照跑" in text
    assert "> [!CAUTION]" not in text


def test_precheck_missing_anthropic_key_falls_back_yellow(tmp_path):
    env = {k: v for k, v in SPLIT.items() if k != "ANTHROPIC_API_KEY"}
    code, work, text = _precheck(tmp_path, env)
    assert code == 0 and work is True and "ANTHROPIC_API_KEY 沒設：minibench 這輪維持用 openai" in text
    assert text.count("- Provider：openai") == 1


# ------------------------------------------------------------ main.py wiring


def test_make_group_wires_each_tournament_to_its_own_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-not-real")
    monkeypatch.setenv("OPENAI_API_KEY", "o-not-real")
    store = state.PendingComments.load(tmp_path)
    tours = [("FutureEval Fall 2026", 33121), ("MiniBench", "minibench")]
    mb = main.make_group("minibench", "anthropic", SPLIT, tours, lambda: store, client=None)
    m = main.make_group("main", "openai", SPLIT, tours, lambda: store, client=None)
    assert mb.tournaments == [("MiniBench", "minibench")] and m.tournaments == [("FutureEval Fall 2026", 33121)]
    assert mb.models == {"minibench": A_MODELS} and m.models == {"main": O_MODELS}
    assert mb.env["LLM_PROVIDER"] == "anthropic" and budget.pace_kinds(mb.env) == ("minibench",)
    assert m.env["LLM_PROVIDER"] == "openai" and budget.pace_kinds(m.env) == ("main",)
    assert mb.key_lookup_fn().provider == "anthropic" and mb.key_lookup_fn().info.limit == 15.0
    assert m.key_lookup_fn().provider == "openai" and mb.probe_fn is not None and m.probe_fn is not None
