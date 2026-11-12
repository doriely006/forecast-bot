import urllib.error
from datetime import datetime, timedelta, timezone

import precheck
import state


def q(qid, forecasted=False, qtype="binary", open_t="2026-10-16T10:00:00Z", close_t="2026-10-16T11:30:00Z"):
    return {"id": qid, "type": qtype, "open_time": open_t, "scheduled_close_time": close_t,
            "my_forecasts": {"history": [{"x": 1}] if forecasted else []}}


def post(pid, *questions, title=None):
    if len(questions) == 1:
        return {"id": pid, "title": title or f"Question {pid}", "question": questions[0]}
    return {"id": pid, "title": title or f"Group {pid}",
            "group_of_questions": {"questions": [dict(x, label=f"L{x['id']}") for x in questions]}}


MAIN = [post(1, q(11, forecasted=True)), post(2, q(21, forecasted=True))]
MINI_P1 = [post(3, q(31, forecasted=True), q(32, forecasted=True))]
MINI_P2 = [{"id": 4, "title": "Cond", "conditional": {"question_yes": q(41), "question_no": q(42)}}]


class Api:
    def __init__(self, main=MAIN, mini1=MINI_P1, mini2=MINI_P2, key=None, fail=None):
        self.main, self.mini1, self.mini2 = main, mini1, mini2
        self.key = key if key is not None else {"data": {"limit": 100, "usage": 20, "limit_remaining": 80}}
        self.fail = fail or {}
        self.calls = []

    def __call__(self, url, headers=None, timeout=20):
        self.calls.append(url)
        for frag, code in self.fail.items():
            if frag in url:
                raise urllib.error.HTTPError(url, code, "fail", {}, None)
        if "openrouter.ai/api/v1/key" in url:
            return self.key
        if "tournaments=33121" in url:
            return {"results": self.main, "next": None}
        if "tournaments=minibench" in url:
            if "offset=0" in url:
                return {"results": self.mini1, "next": "page2"}
            return {"results": self.mini2, "next": None}
        if "tournaments=market-pulse-26q4" in url:  # v0.4.5: third tournament, no open questions
            return {"results": [], "next": None}
        raise AssertionError(url)


class Poster:
    def __init__(self, fail=False):
        self.fail, self.sent = fail, []

    def __call__(self, url, body, headers, timeout=30):
        if self.fail:
            raise urllib.error.HTTPError(url, 503, "down", {}, None)
        self.sent.append(body)
        return 201


def env(**kw):
    base = {"METACULUS_TOKEN": "t", "OPENROUTER_API_KEY": "k", "GITHUB_EVENT_NAME": "schedule",
            "BOT_ENABLED": "1", "REPO_PRIVATE": "false"}
    base.update(kw)
    return base


def run(e, api=None, poster=None, age=1.0, tmp=None, now=None):
    if tmp is not None:
        e = dict(e, STATE_DIR=str(tmp))
    extra = {"now": lambda: now} if now is not None else {}
    deps = precheck.Deps(get_json=api or Api(), post=poster or Poster(), sleep=lambda s: None,
                         commit_age=lambda: age, **extra)
    return precheck.run(e, deps)


def test_switches_only_accept_1():  # B5
    assert precheck.validate_switches({"PUBLISH": "1", "BOT_ENABLED": "1"}, "schedule") == []
    assert precheck.validate_switches({"PUBLISH": "true"}, "workflow_dispatch")
    assert precheck.validate_switches({"BOT_ENABLED": "0"}, "schedule")
    assert precheck.validate_switches({}, "workflow_dispatch") == []
    code, work, text = run(env(PUBLISH="yes"))
    assert code == 1 and not work and "開關設定錯誤" in text and text.startswith("**DRY RUN**")


def test_idle_publish_run_ends_without_work(tmp_path):  # B14
    code, work, text = run(env(PUBLISH="1"), tmp=tmp_path)
    assert code == 0 and work is False
    assert text.splitlines()[0].startswith("**已送出**")
    assert "還沒預測 0" in text and "不支援的題型跳過 1" in text and "到此結束" in text


def test_new_question_means_work(tmp_path):
    api = Api(main=MAIN + [post(5, q(51))])
    code, work, text = run(env(PUBLISH="1"), api=api, tmp=tmp_path)
    assert code == 0 and work is True and "還沒預測 1" in text
    assert any("offset=100" in c for c in api.calls)  # B12/B14: every page is read


def test_dry_run_lists_only_and_prints_windows(tmp_path):  # A1, C4
    api = Api(main=MAIN + [post(5, q(51))])
    code, work, text = run(env(), api=api, tmp=tmp_path)
    assert code == 0 and work is False and "只列題" in text
    assert "2026-10-16T10:00:00Z" in text and "最短開放窗：1.5 小時" in text


def test_dry_run_llm_only_on_manual_runs(tmp_path):  # A1
    api = Api(main=MAIN + [post(5, q(51))])
    assert run(env(DRY_RUN_LLM="1", GITHUB_EVENT_NAME="workflow_dispatch"), api=api, tmp=tmp_path)[1] is True
    assert run(env(DRY_RUN_LLM="1"), api=api, tmp=tmp_path)[1] is False


def test_repo_warnings_old_commit_and_private(tmp_path):  # override 2
    code, _, text = run(env(REPO_PRIVATE="true"), age=50, tmp=tmp_path)
    assert code == 0 and text.count("[!WARNING]") == 2 and "60 天" in text and "私人" in text
    _, _, fresh = run(env(), age=44, tmp=tmp_path)
    assert "[!WARNING]" not in fresh


def test_repo_private_falls_back_to_github_api():
    def get_json(url, headers=None, timeout=10):
        assert url == "https://api.github.com/repos/o/r" and headers["Authorization"] == "Bearer gt"
        return {"private": True}

    assert precheck.repo_is_private({"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "gt"}, get_json) is True
    assert precheck.repo_is_private({"REPO_PRIVATE": "false"}, get_json) is False


def test_one_tournament_failing_is_yellow_both_red(tmp_path):  # B12
    code, _, text = run(env(PUBLISH="1"), api=Api(fail={"minibench": 500}), tmp=tmp_path)
    assert code == 0 and "另一邊照跑" in text and "Question 1" in text
    code, _, text = run(env(PUBLISH="1"), api=Api(fail={"metaculus.com": 500}), tmp=tmp_path)
    assert code == 1 and "兩個賽事都抓題失敗" in text
    code, _, _ = run(env(), api=Api(fail={"33121": 401}), tmp=tmp_path)
    assert code == 1


def test_budget_red_only_when_it_matters(tmp_path):
    assert run(env(PUBLISH="1", OPENROUTER_API_KEY=""), tmp=tmp_path)[0] == 1
    assert run(env(OPENROUTER_API_KEY=""), tmp=tmp_path)[0] == 0  # dry listing needs no LLM
    unlimited = Api(key={"data": {"limit": None, "usage": 10}})
    code, _, text = run(env(PUBLISH="1"), api=unlimited, tmp=tmp_path)
    assert code == 0 and "no spending limit" in text  # C3: yellow, not refused
    assert "配速" in text and "預估額度見底日" in text  # A2


def test_pending_comment_is_resent_exactly(tmp_path):  # A3
    store = state.PendingComments.load(tmp_path)
    store.add(7, "# forecast-bot v0.1.1\nexact text", "https://www.metaculus.com/questions/7/")
    poster = Poster()
    code, _, text = run(env(PUBLISH="1"), poster=poster, tmp=tmp_path)
    assert poster.sent == [{"on_post": 7, "text": "# forecast-bot v0.1.1\nexact text",
                            "is_private": True, "included_forecast": True}]
    assert state.PendingComments.load(tmp_path).items == {} and "成功 1" in text


def test_pending_comment_still_failing_then_given_up(tmp_path):  # A3
    store = state.PendingComments.load(tmp_path)
    store.add(7, "# text", "u7")
    _, _, text = run(env(PUBLISH="1"), poster=Poster(fail=True), tmp=tmp_path)
    assert "[!CAUTION]" in text and "仍送不出去" in text
    assert state.PendingComments.load(tmp_path).items["7"]["attempts"] == 1
    for _ in range(state.MAX_RESEND_ATTEMPTS - 1):
        _, _, text = run(env(PUBLISH="1"), poster=Poster(fail=True), tmp=tmp_path)
    assert "放棄補送" in text and state.PendingComments.load(tmp_path).items == {}
    # dry runs never send comments
    store = state.PendingComments.load(tmp_path)
    store.add(8, "# t", "u8")
    poster = Poster()
    run(env(), poster=poster, tmp=tmp_path)
    assert poster.sent == []


def test_tournament_ids_match_sdk():
    from forecasting_tools import MetaculusClient

    assert precheck.TOURNAMENTS == [
        ("FutureEval Fall 2026", MetaculusClient.CURRENT_AI_COMPETITION_ID),
        ("MiniBench", MetaculusClient.CURRENT_MINIBENCH_ID),
        ("Market Pulse 26Q4", "market-pulse-26q4"),  # v0.4.5
    ]


def test_market_pulse_is_main_kind_and_in_tournament_mode():  # v0.4.5
    import budget
    import runner

    assert budget.kind_of("Market Pulse 26Q4") == "main"
    assert ("Market Pulse 26Q4", "market-pulse-26q4") in runner.tournaments_for_mode("tournament")
    assert ("Market Pulse 26Q4", "market-pulse-26q4") not in runner.tournaments_for_mode("test_questions")


def _fail_n(tmp_path, key, n):
    store = state.PendingComments.load(tmp_path)
    for _ in range(n):
        store.record_failure(key, "Question 5", "u", "RuntimeError: boom\nsecond line")


def test_skipped_question_is_not_work_and_is_listed_yellow(tmp_path):  # v0.1.2
    api = Api(main=MAIN + [post(5, q(51))])
    _fail_n(tmp_path, "51", 2)
    assert run(env(PUBLISH="1"), api=api, tmp=tmp_path)[1] is True  # 2 < 3: still work
    _fail_n(tmp_path, "51", 1)
    code, work, text = run(env(PUBLISH="1"), api=api, tmp=tmp_path)
    assert code == 0 and work is False and "到此結束" in text and "還沒預測 0" in text
    assert "[!WARNING]" in text and "51｜Question 5｜失敗 3 次｜RuntimeError: boom second line" in text
    # the limit is configurable
    assert run(env(PUBLISH="1", MAX_QUESTION_FAILED_RUNS="4"), api=api, tmp=tmp_path)[1] is True
    # a skipped question next to a new one: the new one is still work
    api2 = Api(main=MAIN + [post(5, q(51)), post(6, q(61))])
    assert run(env(PUBLISH="1"), api=api2, tmp=tmp_path)[1] is True


def test_dry_llm_run_also_ignores_skipped_in_count_but_first_run_has_no_cache(tmp_path):
    api = Api(main=MAIN + [post(5, q(51))])
    code, work, text = run(env(PUBLISH="1"), api=api, tmp=tmp_path / "never-restored")
    assert code == 0 and work is True and "連續失敗" not in text


def test_cooldown_decides_whether_a_skipped_question_is_work(tmp_path):  # v0.1.3
    t0 = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    api = Api(main=MAIN + [post(5, q(51))])
    store = state.PendingComments.load(tmp_path)
    for _ in range(3):
        store.record_failure("51", "Question 5", "u", "RuntimeError: boom", now=t0)
    code, work, text = run(env(PUBLISH="1"), api=api, tmp=tmp_path, now=t0 + timedelta(hours=5, minutes=59))
    assert code == 0 and work is False and "到此結束" in text
    assert "下次重試 2026-10-16 18:00 UTC" in text and "冷卻 6 小時" in text
    code, work, text = run(env(PUBLISH="1"), api=api, tmp=tmp_path, now=t0 + timedelta(hours=6))
    assert code == 0 and work is True and "還沒預測 1" in text and "連續失敗" not in text
    # SKIP_COOLDOWN_HOURS is read; garbage and < 1 fall back to 6
    for value, hours_later, expect in (("2", 2, True), ("2", 1, False), ("0", 5, False), ("x", 6, True)):
        got = run(env(PUBLISH="1", SKIP_COOLDOWN_HOURS=value), api=api, tmp=tmp_path,
                  now=t0 + timedelta(hours=hours_later))[1]
        assert got is expect, (value, hours_later)
    # an entry written by v0.1.2 (no timestamp) gets its one extra try at once
    store.failures["51"].pop("last_attempt")
    store.save()
    assert run(env(PUBLISH="1"), api=api, tmp=tmp_path, now=t0)[1] is True


# ---- v0.4.6: re-forecast window as work; group subquestions filtered by their own status / window
T_NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def answered(qid, close, last, **kw):
    x = q(qid, forecasted=True, open_t="2026-11-01T00:00:00Z", close_t=close, **kw)
    x["my_forecasts"] = {"history": [{"start_time": last.timestamp()}], "latest": {"start_time": last.timestamp()}}
    return x


def test_reforecast_due_question_is_work(tmp_path):
    api = Api(main=MAIN + [post(6, answered(61, "2026-11-20T18:00:00Z", T_NOW - timedelta(days=5)))])
    code, work, text = run(env(PUBLISH="1"), api=api, tmp=tmp_path, now=T_NOW)
    assert code == 0 and work is True and "截止前重答：1" in text and "還沒預測 0" in text


def test_reforecast_not_work_when_reanswered_outside_window_or_off(tmp_path):
    done = Api(main=MAIN + [post(6, answered(61, "2026-11-20T18:00:00Z", T_NOW - timedelta(hours=1)))])
    code, work, text = run(env(PUBLISH="1"), api=done, tmp=tmp_path, now=T_NOW)
    assert work is False and "截止前重答：0" in text
    far = Api(main=MAIN + [post(6, answered(61, "2026-11-22T18:00:00Z", T_NOW - timedelta(days=5)))])
    assert run(env(PUBLISH="1"), api=far, tmp=tmp_path, now=T_NOW)[1] is False
    due = Api(main=MAIN + [post(6, answered(61, "2026-11-20T18:00:00Z", T_NOW - timedelta(days=5)))])
    assert run(env(PUBLISH="1", REFORECAST_HOURS_BEFORE_CLOSE="0"), api=due, tmp=tmp_path, now=T_NOW)[1] is False
    closing = Api(main=MAIN + [post(6, answered(61, "2026-11-20T12:05:00Z", T_NOW - timedelta(days=5)))])
    assert run(env(PUBLISH="1"), api=closing, tmp=tmp_path, now=T_NOW)[1] is False  # MIN_MINUTES_BEFORE_CLOSE


def test_short_question_is_never_due_for_reforecast():
    close = datetime(2026, 11, 20, 13, 0, tzinfo=timezone.utc)
    assert not precheck.reforecast_due(True, close, close - timedelta(minutes=80), T_NOW, 12)
    assert precheck.reforecast_due(True, close, close - timedelta(hours=13), T_NOW, 12)
    assert not precheck.reforecast_due(True, close, None, T_NOW, 12)
    assert precheck.reforecast_hours({}) == 12 and precheck.reforecast_hours({"REFORECAST_HOURS_BEFORE_CLOSE": "-1"}) == 12


def test_last_forecast_time_reads_latest_then_history_and_iso():
    t = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)
    assert precheck.last_forecast_time({"my_forecasts": {"latest": {"start_time": t.timestamp()}}}) == t
    assert precheck.last_forecast_time({"my_forecasts": {"latest": None, "history": [
        {"start_time": "2026-10-30T00:00:00Z"}, {"start_time": t.timestamp()}]}}) == t
    assert precheck.last_forecast_time({"my_forecasts": {"history": []}}) is None
    assert precheck.last_forecast_time({}) is None


def test_group_subquestions_closed_or_upcoming_are_not_open(tmp_path):
    group = post(7, dict(q(71, open_t="2026-11-19T00:00:00Z", close_t="2026-11-21T00:00:00Z"), status="open"),
                 dict(q(72, open_t="2026-11-10T00:00:00Z", close_t="2026-11-21T00:00:00Z"), status="closed"),
                 dict(q(73, open_t="2026-11-25T00:00:00Z", close_t="2026-11-30T00:00:00Z"), status="upcoming"),
                 q(74, open_t="2026-11-10T00:00:00Z", close_t="2026-11-18T00:00:00Z"))  # no status: by its window
    items = precheck.flatten_post(group, "Market Pulse 26Q4", T_NOW)
    assert [it.question_id for it in items] == [71]
    code, work, text = run(env(PUBLISH="1"), api=Api(main=[group], mini1=[], mini2=[]), tmp=tmp_path, now=T_NOW)
    assert work is True and "開放題：1" in text and "還沒預測 1" in text


# ---- v0.4.7: group subquestions answered by the bot (real-API-like shape); precheck == SDK/runner
_T_POSTED = datetime(2026, 11, 12, 13, 57, 10, tzinfo=timezone.utc)


def _sub(qid, label):
    entry = {"question_id": qid, "author_id": 1, "start_time": _T_POSTED.timestamp(), "end_time": None,
             "forecast_values": [0.6, 0.4], "interval_lower_bounds": None, "centers": None,
             "interval_upper_bounds": None}
    return {"id": qid, "title": f"Market Pulse sub {label}", "label": label, "type": "binary", "status": "open",
            "open_time": "2026-11-10T00:00:00Z", "scheduled_close_time": "2026-11-21T20:00:00Z",
            "scheduled_resolve_time": "2026-11-22T00:00:00Z", "description": "", "fine_print": "",
            "resolution_criteria": "", "aggregations": {}, "my_forecasts": {"history": [entry], "latest": entry,
                                                                              "score_data": {}}}


GROUP_POST = {"id": 46000, "title": "Market Pulse group", "projects": {
    "tournament": [{"slug": "market-pulse-26q4", "id": 1}], "default_project": {"id": 1, "visibility": "normal"}},
    "group_of_questions": {"description": "d", "fine_print": "f", "resolution_criteria": "r",
                           "questions": [_sub(46249, "A"), _sub(46248, "B")]}}


class WithCpApi(Api):
    """The list endpoint only carries my_forecasts when with_cp=true (what the SDK sends)."""

    def __call__(self, url, headers=None, timeout=20):
        if "market-pulse-26q4" in url:
            import copy
            post = copy.deepcopy(GROUP_POST)
            if "with_cp=true" not in url:
                for x in post["group_of_questions"]["questions"]:
                    x.pop("my_forecasts")
            return {"results": [post], "next": None}
        return super().__call__(url, headers, timeout)


def test_precheck_sees_answered_group_subquestions_like_the_sdk(tmp_path):
    from forecasting_tools import MetaculusClient

    now = datetime(2026, 11, 12, 15, 14, tzinfo=timezone.utc)
    code, work, text = run(env(PUBLISH="1"), api=WithCpApi(main=[], mini1=[], mini2=[]), tmp=tmp_path, now=now)
    assert "已預測 2；還沒預測 0" in text and "截止前重答：0" in text and work is False
    assert "最後預測時間：讀到 2／讀不到 0" in text
    sdk = MetaculusClient._unpack_group_question(GROUP_POST)  # what the runner gets for the same post
    assert all(x.already_forecasted for x in sdk)
    items = precheck.flatten_post(GROUP_POST, "Market Pulse 26Q4", now)
    import runner
    assert [runner.last_forecast(x) for x in sdk] == [it.last_forecast for it in items] == [_T_POSTED] * 2


def test_forecast_shape_has_keys_and_types_never_values():
    shape = precheck.forecast_shape(_sub(1, "A"))
    assert "latest{" in shape and "start_time:float" in shape and "0.6" not in shape
    assert precheck.forecast_shape({"id": 1}).startswith("my_forecasts=missing")
