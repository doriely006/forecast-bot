import asyncio
import re

import budget
import main
import runner
from conftest import TOURNAMENTS, FakeClient


def key(remaining=80.0, limit=100.0):
    return budget.KeyInfo(limit=limit, usage=limit - remaining, remaining=remaining)


def do_run(client, publish, key_fn=None, env=None, mode="tournament", factory_log=None):
    def factory(economy):
        if factory_log is not None:
            factory_log.append(economy)
        return main.make_bot("openai/gpt-5.4", "openai/gpt-5.4-mini", economy, client)

    return asyncio.run(
        runner.run(
            mode=mode,
            publish=publish,
            bot_factory=factory,
            client=client,
            key_info_fn=key_fn or (lambda: key()),
            version=main.VERSION,
            models_label="fake",
            env=env if env is not None else {},
            tournaments=TOURNAMENTS,
        )
    )


def test_full_round_publish_every_question_gets_prediction_and_comment(fake_llm):
    client = FakeClient()
    r = do_run(client, publish=True)
    by_url = {o.url: o for o in r.outcomes}
    # 8 fixture questions, 1 already forecasted, 1 always fails
    assert r.already_forecasted == 1
    assert len(r.outcomes) == 7
    assert r.failed == 1 and "ALWAYSFAILS" in next(o.title for o in r.outcomes if o.status == "failed")
    submitted = [o for o in r.outcomes if o.status == "submitted"]
    assert len(submitted) == 6  # FLAKY succeeded on retry
    posted_qids = {qid for _, qid, _ in client.predictions}
    comment_posts = {pid for pid, _ in client.comments}
    assert posted_qids == {1101, 1103, 1104, 2201, 2202, 2204}
    assert comment_posts == {101, 103, 104, 201, 202, 204}
    for _, text in client.comments:
        assert text.startswith(f"# forecast-bot v{main.VERSION}")
    assert r.exit_code == 0
    assert by_url  # silence unused


def test_comment_reports_clamp_and_matches_submitted_value(fake_llm):
    client = FakeClient()
    do_run(client, publish=True)
    (p,) = [p for kind, qid, p in client.predictions if qid == 1101]
    assert p == 0.97
    (text,) = [t for pid, t in client.comments if pid == 101]
    assert "model aggregate 99.0% -> final 97.0%" in text
    assert re.search(r"\*Final Prediction\*: 97\.0%", text)


def test_mc_floor_always_holds(fake_llm):
    # The SDK parser already lifts 0% options to ~1%; the final guard makes
    # sure the submitted list never goes below MC_MIN_PROB and sums to 1.
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


def test_unit_error_twice_falls_back_to_wide(fake_llm):
    import guards

    client = FakeClient()
    do_run(client, publish=True)
    (text,) = [t for pid, t in client.comments if pid == 201]
    assert "unit error twice" in text and "wide distribution" in text
    (cdf,) = [c for kind, qid, c in client.predictions if qid == 2201]
    assert guards.cdf_problems(cdf, True, True, 201) == []
    # wide: the middle half of the range carries a sizeable share, not a spike
    assert 0.3 < cdf[150] - cdf[50] < 0.7


def test_discrete_question_valid_cdf(fake_llm):
    import guards

    client = FakeClient()
    do_run(client, publish=True)
    (cdf,) = [c for kind, qid, c in client.predictions if qid == 2202]
    assert len(cdf) == 12
    assert guards.cdf_problems(cdf, False, True, 12) == []


def test_dry_run_sends_nothing_and_default_is_dry(fake_llm):
    assert runner.env_flag("PUBLISH", {}) is False
    assert runner.env_flag("PUBLISH", {"PUBLISH": "true"}) is False  # only "1" publishes
    client = FakeClient()
    r = do_run(client, publish=False, env={"MAX_FORECASTS_PER_RUN": "99"})
    assert client.predictions == [] and client.comments == []
    assert sum(o.status == "dry_run" for o in r.outcomes) == 6


def test_scheduled_dry_run_lists_questions_without_llm(fake_llm):
    client = FakeClient()
    r = do_run(client, publish=False)
    assert fake_llm.calls == [] and r.outcomes == []
    assert r.not_attempted_cap == 7
    text = runner.render_summary(r)
    assert "Open questions (open / close time, UTC)" in text and "2026-10-15 00:00" in text


def test_test_questions_mode_caps_at_three(fake_llm):
    client = FakeClient()
    r = do_run(client, publish=False, mode="test_questions")
    assert len(r.outcomes) == 3 and r.already_forecasted == 0  # test mode re-forecasts


def test_budget_stop_makes_no_llm_calls(fake_llm):
    client = FakeClient()
    r = do_run(client, publish=True, key_fn=lambda: key(remaining=2.0))
    assert fake_llm.calls == [] and client.predictions == []
    assert r.exit_code == 1 and "BUDGET STOP" in runner.render_summary(r)


def test_unlimited_key_refused(fake_llm):
    client = FakeClient()
    r = do_run(client, publish=True, key_fn=lambda: budget.KeyInfo(None, 3.0, None))
    assert r.decision.state == "refuse_unlimited" and client.predictions == []


def test_economy_mode_uses_three_forecasts(fake_llm):
    log = []
    client = FakeClient()
    do_run(client, publish=True, key_fn=lambda: key(remaining=10.0), factory_log=log)
    assert log == [True]
    bot = main.make_bot("a", "b", True, client)
    assert bot.predictions_per_research_report == 3
    assert main.make_bot("a", "b", False, client).predictions_per_research_report == 5


def test_budget_runs_out_mid_run(fake_llm):
    calls = {"n": 0}

    def key_fn():
        calls["n"] += 1
        return key(remaining=50.0 if calls["n"] <= 3 else 1.0)

    client = FakeClient()
    r = do_run(client, publish=True, key_fn=key_fn)
    assert any(o.status == "skipped_budget" for o in r.outcomes)


def test_comment_failure_is_retried(fake_llm):
    client = FakeClient()
    client.fail_comment_once = {101}
    do_run(client, publish=True)
    assert [pid for pid, _ in client.comments].count(101) == 1


def test_fetch_failure_is_reported(fake_llm):
    client = FakeClient(fail_fetch=True)
    r = do_run(client, publish=True)
    assert r.exit_code == 1 and "Could not load questions" in runner.render_summary(r)


def test_summary_has_required_fields(fake_llm):
    client = FakeClient()
    calls = {"n": 0}

    def key_fn():
        calls["n"] += 1
        return key(remaining=80.0 - 0.1 * calls["n"])

    r = do_run(client, publish=True, key_fn=key_fn)
    text = runner.render_summary(r)
    for needle in (f"forecast-bot v{main.VERSION}", "Open questions: 8", "New forecasts this run: 6",
                   "Failed: 1", "Estimated spend this run: $", "OpenRouter key remaining: $"):
        assert needle in text, needle
