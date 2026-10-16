import urllib.error

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


def run(e, api=None, poster=None, age=1.0, tmp=None):
    if tmp is not None:
        e = dict(e, STATE_DIR=str(tmp))
    deps = precheck.Deps(get_json=api or Api(), post=poster or Poster(), sleep=lambda s: None,
                         commit_age=lambda: age)
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
    ]
