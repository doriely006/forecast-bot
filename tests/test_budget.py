import urllib.error
from datetime import date

import budget

TERRA, MINI = (2e-6, 12e-6), (0.75e-6, 4.5e-6)
PRICES = {k: (TERRA, MINI) for k in budget.KINDS}


def lookup(limit, usage, status=200):
    info = budget.parse_key_payload(
        {"data": {"limit": limit, "usage": usage, "limit_remaining": None if limit is None else limit - usage}}
    )
    return budget.KeyLookup(info, status)


def test_states_ok_and_stop():
    ok = budget.decide(lookup(100, 10), {})
    assert ok.state == "ok" and ok.may_call_llm and ok.remaining == 90 and not ok.systemic
    stop = budget.decide(lookup(100, 98), {})  # $2 left
    assert stop.state == "stop" and not stop.may_call_llm and stop.systemic


def test_stop_threshold_from_env():
    env = {"BUDGET_STOP_BELOW": "20"}
    assert budget.decide(lookup(100, 60), env).state == "ok"
    assert budget.decide(lookup(100, 85), env).state == "stop"


def test_unlimited_key_is_paced_on_usage_not_refused():  # C3
    d = budget.decide(lookup(None, 5), {})
    assert d.state == "ok" and d.remaining == 95 and "no spending limit" in d.warning
    assert budget.decide(lookup(None, 5), {"ASSUMED_BUDGET_USD": "50"}).remaining == 45
    assert budget.decide(lookup(None, 99), {}).state == "stop"


def test_unknown_and_auth_errors_never_call_llm():
    d = budget.decide(budget.KeyLookup(None, None), {"OPENROUTER_API_KEY": "x"})
    assert d.state == "unknown" and not d.may_call_llm
    assert budget.decide(budget.KeyLookup(None, None), {}).state == "auth_error"  # key not set
    assert budget.decide(budget.KeyLookup(None, 401), {"OPENROUTER_API_KEY": "x"}).state == "auth_error"
    assert budget.decide(budget.KeyLookup(None, 402), {"OPENROUTER_API_KEY": "x"}).state == "stop"


def test_fetch_key_info_parses_and_sends_bearer():
    seen = {}

    def get_json(url, headers, timeout=20):
        seen["url"], seen["auth"] = url, headers["Authorization"]
        return {"data": {"limit": 50, "usage": 12.5, "limit_remaining": 37.5}}

    lk = budget.fetch_key_info("test-key-not-real", get_json=get_json)
    assert lk.info.remaining == 37.5 and lk.info.limit == 50 and lk.http_status == 200
    assert seen["url"] == budget.OPENROUTER_KEY_URL and seen["auth"] == "Bearer test-key-not-real"


def test_fetch_key_info_failure_and_missing_key():
    assert budget.fetch_key_info(None).info is None

    def raise_401(*a, **k):
        raise urllib.error.HTTPError("u", 401, "no", {}, None)

    assert budget.fetch_key_info("x", get_json=raise_401).http_status == 401


def test_model_lists_prefer_terra_then_gpt54_and_exclude_pro_image_batch():  # C2, B11
    listed = [{"id": "openai/gpt-5.4"}, {"id": "openai/gpt-5.6-terra"}, {"id": "openai/gpt-5.4-mini"}]
    assert budget.pick_model(listed, budget.FORECAST_CANDIDATES) == "openai/gpt-5.6-terra"
    assert budget.pick_model([{"id": "openai/gpt-5.4"}], budget.FORECAST_CANDIDATES) == "openai/gpt-5.4"
    assert budget.pick_model(None, budget.FORECAST_CANDIDATES) == "openai/gpt-6.1-sol"
    # per-tournament config wins when listed; excluded variants are never used
    assert budget.pick_model(listed, budget.FORECAST_CANDIDATES, "openai/gpt-5.4") == "openai/gpt-5.4"
    for bad in ("openai/gpt-5.4-pro", "openai/gpt-5.4-image", "openai/gpt-5.4:batch"):
        assert not budget.allowed_model(bad)
        assert budget.pick_model([{"id": bad}] + listed, budget.FORECAST_CANDIDATES, bad) == "openai/gpt-5.6-terra"
    for mid in budget.FORECAST_CANDIDATES + budget.CHEAP_CANDIDATES:
        assert budget.allowed_model(mid)


def test_pacing_about_15_cents_per_question_at_start():  # A2
    p = budget.pace(100.0, date(2026, 10, 16), {}, {}, PRICES)
    main = p.kinds["main"]
    assert 0.14 < main.per_question_budget < 0.17
    assert main.tier.forecasts >= 1 and main.est_cost <= budget.tier_cost(budget.TIERS[0], TERRA, MINI)
    assert p.run_out is not None and p.run_out > date(2026, 12, 1)


def test_pacing_never_skips_when_poor_only_stop_does():  # A2
    p = budget.pace(4.0, date(2026, 10, 16), {}, {"main": 50}, PRICES)
    assert p.kinds["main"].tier == budget.TIERS[-1]  # cheapest, not zero
    rich = budget.pace(1000.0, date(2026, 12, 20), {}, {}, PRICES)
    assert rich.kinds["main"].tier == budget.TIERS[0]


def test_split_after_first_minibench_round():  # C6
    before = budget.pace(60.0, date(2026, 10, 30), {}, {}, PRICES)
    assert before.kinds["main"].per_question_budget == before.kinds["minibench"].per_question_budget
    after = budget.pace(60.0, date(2026, 11, 2), {}, {}, PRICES)
    assert after.after_split and after.kinds["minibench"].tier == budget.TIERS[-1]
    assert after.kinds["main"].per_question_budget > after.kinds["minibench"].per_question_budget
    later = budget.pace(60.0, date(2026, 10, 20), {"BUDGET_SPLIT_DATE": "2026-10-19", "MAIN_BUDGET_SHARE": "0.5"},
                        {}, PRICES)
    assert later.after_split
    assert abs(later.kinds["main"].per_question_budget - later.kinds["minibench"].per_question_budget) < 1e-9


def test_default_final_model_is_gpt_61_sol_then_terra_then_54():  # v0.1.5
    assert budget.FORECAST_CANDIDATES == ["openai/gpt-6.1-sol", "openai/gpt-5.6-terra", "openai/gpt-5.4"]
    listed = [{"id": m} for m in ("openai/gpt-5.4", "openai/gpt-5.6-terra", "openai/gpt-6.1-sol")]
    assert budget.pick_model(listed, budget.FORECAST_CANDIDATES) == "openai/gpt-6.1-sol"
    assert budget.pick_model(None, budget.FORECAST_CANDIDATES) == "openai/gpt-6.1-sol"
    assert budget.CHEAP_CANDIDATES[0] == "openai/gpt-5.4-mini"


def test_sol_missing_from_models_falls_back_to_terra_then_54():
    no_sol = [{"id": "openai/gpt-5.6-terra"}, {"id": "openai/gpt-5.4"}, {"id": "openai/gpt-5.4-mini"}]
    assert budget.pick_model(no_sol, budget.FORECAST_CANDIDATES) == "openai/gpt-5.6-terra"
    assert budget.pick_model(no_sol[1:], budget.FORECAST_CANDIDATES) == "openai/gpt-5.4"


def test_pro_codex_image_batch_variants_excluded_including_sol_pro():
    bad = ("openai/gpt-6.1-sol-pro", "openai/gpt-6-sol-pro", "openai/gpt-5.6-terra-pro",
           "openai/gpt-6.1-sol-image", "openai/gpt-6.1-sol:batch", "openai/gpt-5.3-codex")
    listed = [{"id": "openai/gpt-6.1-sol"}]
    for b in bad:
        assert not budget.allowed_model(b)
        assert budget.pick_model([{"id": b}] + listed, budget.FORECAST_CANDIDATES, b) == "openai/gpt-6.1-sol"
    assert budget.allowed_model("openai/gpt-6.1-sol")


def test_sol_fallback_price_and_tier_costs():
    f = budget.FALLBACK_PRICES["openai/gpt-6.1-sol"]
    c = budget.FALLBACK_PRICES["openai/gpt-5.4-mini"]
    assert f == (2e-6, 10e-6)
    got = [round(budget.tier_cost(t, f, c), 3) for t in budget.TIERS]
    assert got == [0.303, 0.196, 0.143, 0.089]  # README cost model
