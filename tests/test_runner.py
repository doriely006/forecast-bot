import asyncio
import re
from datetime import date

import budget
import main
import runner
import state
from conftest import TOURNAMENTS, FakeClient, FakeOpenRouterError, load_fixture_questions

TERRA, MINI = (2e-6, 12e-6), (0.75e-6, 4.5e-6)
MODELS = {k: ("openai/gpt-5.6-terra", "openai/gpt-5.4-mini") for k in budget.KINDS}
PRICES = {k: (TERRA, MINI) for k in budget.KINDS}


def key(remaining=80.0, limit=100.0):
    return budget.KeyLookup(budget.KeyInfo(limit=limit, usage=limit - remaining, remaining=remaining), 200)


def do_run(client, publish, key_fn=None, env=None, mode="tournament", factory_log=None, store=None,
           concurrency=3):
    def factory(kind, tier):
        if factory_log is not None:
            factory_log.append((kind, tier.name))
        return main.make_bot(MODELS[kind][0], MODELS[kind][1], tier.forecasts, client)

    return asyncio.run(
        runner.run(
            mode=mode,
            publish=publish,
            bot_factory=factory,
            client=client,
            key_lookup_fn=key_fn or (lambda: key()),
            version=main.VERSION,
            models=MODELS,
            prices=PRICES,
            env=env if env is not None else {},
            tournaments=TOURNAMENTS,
            store=store,
            today=date(2026, 10, 16),
            concurrency=concurrency,
        )
    )


def test_full_round_publish_every_question_gets_prediction_and_comment(fake_llm):
    client = FakeClient()
    r = do_run(client, publish=True)
    # 8 fixture questions, 1 already forecasted, 1 always fails, 1 unit error twice (B2: not sent)
    assert r.already_forecasted == 1
    assert len(r.outcomes) == 7
    failed = sorted(o.title for o in r.outcomes if o.status == "failed")
    assert r.failed == 2 and "ALWAYSFAILS" in failed[0] and "UNITBUG twice" in failed[1]
    assert r.count("submitted") == 5  # FLAKY succeeded on retry
    assert {qid for _, qid, _ in client.predictions} == {1101, 1103, 1104, 2202, 2204}
    assert {pid for pid, _ in client.comments} == {101, 103, 104, 202, 204}
    for _, text in client.comments:
        assert text.startswith(f"# forecast-bot v{main.VERSION}")
    assert r.exit_code == 0


def test_comment_reports_clamp_and_matches_submitted_value(fake_llm):  # A4, binary
    client = FakeClient()
    do_run(client, publish=True)
    (p,) = [p for kind, qid, p in client.predictions if qid == 1101]
    assert p == 0.97
    (text,) = [t for pid, t in client.comments if pid == 101]
    assert "model aggregate 99.0% -> final 97.0%" in text
    assert re.search(r"\*Final Prediction\*: 97\.0%", text)


def test_final_prediction_in_comment_equals_submitted_mc(fake_llm, monkeypatch):  # A4, MC
    monkeypatch.setenv("MC_MIN_PROB", "0.05")
    client = FakeClient()
    do_run(client, publish=True)
    (probs,) = [p for kind, qid, p in client.predictions if qid == 1103]
    (text,) = [t for pid, t in client.comments if pid == 103]
    final = text.split("Final Prediction", 1)[1][:300]
    for option, value in probs.items():
        assert re.search(rf"{option}: {value * 100:.1f}%", final), (option, value, final)


def test_mc_floor_always_holds(fake_llm):
    client = FakeClient()
    do_run(client, publish=True)
    (probs,) = [p for kind, qid, p in client.predictions if qid == 1103]
    assert abs(sum(probs.values()) - 1) < 1e-9 and min(probs.values()) >= 0.01 - 1e-12


def test_mc_floor_change_is_reported_in_comment(fake_llm, monkeypatch):
    monkeypatch.setenv("MC_MIN_PROB", "0.05")
    client = FakeClient()
    do_run(client, publish=True)
    (probs,) = [p for kind, qid, p in client.predictions if qid == 1103]
    assert min(probs.values()) >= 0.05 - 1e-12
    (text,) = [t for pid, t in client.comments if pid == 103]
    assert re.search(r"Gamma: 1\.0% -> 5\.0%", text)


def test_unit_error_reasked_once_then_fixed(fake_llm):
    client = FakeClient()
    do_run(client, publish=True)
    (text,) = [t for pid, t in client.comments if pid == 104]
    assert "looked like a unit error" in text and "re-asked" in text
    (cdf,) = [c for kind, qid, c in client.predictions if qid == 1104]
    assert cdf[0] == 0.0  # closed lower bound respected
    assert all(b >= a for a, b in zip(cdf, cdf[1:]))


def test_unit_error_twice_is_not_submitted(fake_llm):  # B2: no wide/0.5 fallback is sent
    client = FakeClient()
    r = do_run(client, publish=True)
    assert not [c for kind, qid, c in client.predictions if qid == 2201]
    assert not [t for pid, t in client.comments if pid == 201]
    (o,) = [o for o in r.outcomes if "UNITBUG twice" in o.title]
    assert o.status == "failed" and "unit error twice" in o.detail


def test_discrete_question_valid_cdf(fake_llm):  # B8: CDF built and standardized by the SDK
    client = FakeClient()
    do_run(client, publish=True)
    (cdf,) = [c for kind, qid, c in client.predictions if qid == 2202]
    assert len(cdf) == 12
    assert cdf[0] == 0.0 and cdf[-1] < 1.0  # closed lower, open upper
    assert all(b > a for a, b in zip(cdf, cdf[1:]))


def test_dry_run_sends_nothing_and_default_is_dry(fake_llm):
    assert runner.env_flag("PUBLISH", {}) is False
    assert runner.env_flag("PUBLISH", {"PUBLISH": "true"}) is False  # only "1" publishes
    client = FakeClient()
    r = do_run(client, publish=False, env={"DRY_RUN_LLM": "1", "DRY_RUN_LLM_MAX_QUESTIONS": "99"})
    assert client.predictions == [] and client.comments == []
    assert r.count("dry_run") == 5


def test_scheduled_dry_run_lists_questions_without_llm(fake_llm):  # A1
    client = FakeClient()
    r = do_run(client, publish=False)
    assert fake_llm.calls == [] and r.outcomes == []
    assert r.not_attempted_cap == 7 and r.exit_code == 0
    text = runner.render_summary(r)
    assert "開放／截止時間" in text and "2026-10-15 00:00" in text


def test_dry_run_llm_default_cap_two_and_never_on_schedule(fake_llm):  # A1
    r = do_run(FakeClient(), publish=False, env={"DRY_RUN_LLM": "1"})
    assert len(r.outcomes) == 2 and r.not_attempted_cap == 5
    assert runner.max_forecasts_for("tournament", False, {"DRY_RUN_LLM": "1", "GITHUB_EVENT_NAME": "schedule"}) == 0
    assert runner.max_forecasts_for("tournament", True, {}) is None  # publishing: never capped


def test_test_questions_mode_caps_at_three(fake_llm):  # B7
    r = do_run(FakeClient(), publish=False, mode="test_questions")
    assert len(r.outcomes) == 3 and r.already_forecasted == 0  # test mode re-forecasts
    r1 = do_run(FakeClient(), publish=False, mode="test_questions", env={"TEST_MAX_QUESTIONS": "1"})
    assert len(r1.outcomes) == 1


def test_budget_stop_makes_no_llm_calls(fake_llm):
    client = FakeClient()
    r = do_run(client, publish=True, key_fn=lambda: key(remaining=2.0))
    assert fake_llm.calls == [] and client.predictions == []
    assert r.exit_code == 1 and "停止" in runner.render_summary(r)


def test_unlimited_key_runs_with_yellow_warning(fake_llm):  # C3
    client = FakeClient()
    r = do_run(client, publish=True, key_fn=lambda: budget.KeyLookup(budget.KeyInfo(None, 3.0, None), 200))
    assert r.decision.state == "ok" and client.predictions
    text = runner.render_summary(r)
    assert "[!WARNING]" in text and "no spending limit" in text and r.exit_code == 0


def test_pacing_picks_tier_per_tournament(fake_llm):  # A2, C2
    log = []
    do_run(FakeClient(), publish=True, factory_log=log)
    assert sorted(log) == [("main", "minimum"), ("minibench", "minimum")]  # ~$0.12 per question
    log = []
    do_run(FakeClient(), publish=True, key_fn=lambda: key(remaining=5000, limit=6000), factory_log=log)
    assert sorted(log) == [("main", "full"), ("minibench", "full")]
    assert main.make_bot("a", "b", 3, None).predictions_per_research_report == 3


def test_budget_runs_out_mid_run(fake_llm):
    calls = {"n": 0}

    def key_fn():
        calls["n"] += 1
        return key(remaining=50.0 if calls["n"] <= 3 else 1.0)

    r = do_run(FakeClient(), publish=True, key_fn=key_fn)
    assert r.count("skipped_budget") > 0


def test_comment_failure_is_retried(fake_llm):
    client = FakeClient()
    client.fail_comment_once = {101}
    do_run(client, publish=True)
    assert [pid for pid, _ in client.comments].count(101) == 1


def test_comment_exception_counted_red_and_stored_for_next_run(fake_llm, tmp_path):  # A3
    client = FakeClient()
    client.fail_comment_always = {101}
    store = state.PendingComments.load(tmp_path)
    r = do_run(client, publish=True, store=store)
    (o,) = [o for o in r.outcomes if o.url.endswith("/101/")]
    assert o.status == "comment_failed" and r.count("comment_failed") == 1
    assert any(qid == 1101 for _, qid, _ in client.predictions)  # forecast itself was sent
    text = runner.render_summary(r)
    assert "[!CAUTION]" in text and "沒送出去" in text
    saved = state.PendingComments.load(tmp_path).items
    assert list(saved) == ["101"] and saved["101"]["text"].startswith("# forecast-bot")
    assert (tmp_path / "changed").exists()
    assert r.exit_code == 0  # one comment failing is not systemic (B1)


def test_every_comment_failing_is_systemic(fake_llm):
    client = FakeClient()
    client.fail_comment_always = {101, 103, 104, 201, 202, 203, 204}
    assert do_run(client, publish=True).exit_code == 1


def test_402_stops_whole_run_without_retries(fake_llm):  # B3
    qs = load_fixture_questions()
    qs["FutureEval Fall 2026"][2].question_text += " PAYMENT402"  # closes first -> attempted first
    client = FakeClient(qs)
    r = do_run(client, publish=True, concurrency=1)
    research_calls = [c for c in fake_llm.calls if "PAYMENT402" in c]
    assert len(research_calls) == 1  # no per-question retry
    assert "402" in r.fatal and r.count("stopped") == 6 and client.predictions == []
    assert r.exit_code == 1 and "OpenRouter HTTP 402" in runner.render_summary(r)


def test_fatal_status_detection():
    assert runner.llm_fatal_status(FakeOpenRouterError(402)) == 402
    group = ExceptionGroup("wrapped", [RuntimeError("x"), FakeOpenRouterError(401)])
    assert runner.llm_fatal_status(group) == 401
    assert runner.llm_fatal_status('OpenrouterException - {"error":{"code":402}}') == 402
    assert runner.llm_fatal_status("This request requires more credits") == 402
    assert runner.llm_fatal_status("ValueError: 402 widgets expected") is None


def test_fetch_failure_is_reported(fake_llm):
    r = do_run(FakeClient(fail_fetch=True), publish=True)
    assert r.exit_code == 1 and "could not load questions" in runner.render_summary(r)


def test_one_tournament_failing_does_not_block_the_other(fake_llm):  # B12
    client = FakeClient(fail_tournaments=("MiniBench",))
    r = do_run(client, publish=True)
    assert r.exit_code == 0 and {qid for _, qid, _ in client.predictions} == {1101, 1103, 1104}
    assert "另一邊照跑" in runner.render_summary(r)


def test_summary_has_required_fields(fake_llm):
    calls = {"n": 0}

    def key_fn():
        calls["n"] += 1
        return key(remaining=80.0 - 0.1 * calls["n"])

    r = do_run(FakeClient(), publish=True, key_fn=key_fn)
    text = runner.render_summary(r)
    assert text.splitlines()[0].startswith("**已送出**")  # B5
    for needle in (f"forecast-bot v{main.VERSION}", "開放題：8", "這輪新交：5", "失敗：2",
                   "這輪估計花費：$", "OpenRouter 剩餘：$", "配速：", "預估見底日"):
        assert needle in text, needle
    dry = runner.render_summary(do_run(FakeClient(), publish=False))
    assert dry.splitlines()[0].startswith("**DRY RUN**")
    assert not runner.render_summary(r, with_header=False).startswith("**")


def test_cli_writes_summary_even_when_it_crashes(monkeypatch, tmp_path):  # B1, B5
    out = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(out))
    monkeypatch.setenv("METACULUS_TOKEN", "not-a-real-token")

    def boom(*a, **k):
        raise RuntimeError("model list exploded")

    monkeypatch.setattr(budget, "fetch_models", boom)
    assert main.cli(["--mode", "test_questions"]) == 1
    text = out.read_text()
    assert text.startswith("**DRY RUN**") and "這輪沒跑完" in text and "model list exploded" in text
    out.write_text("")
    monkeypatch.setenv("PUBLISH", "yes")
    assert main.cli(["--mode", "test_questions"]) == 1
    assert "PUBLISH=\"yes\" is not allowed" in out.read_text()
