import json
from datetime import datetime, timezone
from pathlib import Path

import standings

FIX = json.loads((Path(__file__).parent / "fixtures" / "standings.json").read_text())
ME = "fixture-forecast-bot"


def fake_get(url, headers=None, timeout=30):
    if url.endswith("/users/me"):
        return {"username": ME}
    if url.endswith("/projects/tournaments/minibench/"):
        return FIX["minibench_project"]
    if url.endswith("/leaderboards/project/33121/"):
        return FIX["main"]
    if url.endswith("/leaderboards/project/33999/"):
        return FIX["minibench"]
    raise AssertionError(url)


def test_rank_among_non_metac_bots():  # C7
    entries = standings.parse_entries(FIX["main"])
    rank, total, mine = standings.non_metac_standing(entries, ME)
    assert (rank, total) == (4, 5) and mine.rank == 6 and mine.questions == 44
    assert standings.non_metac_standing(entries, "nobody") is None


def test_stop_loss_rule():  # C7: 40 resolved main questions, bottom 50% of non-metac bots
    assert "觸發停損" in standings.stop_loss_verdict(4, 5, 44)
    assert "繼續" in standings.stop_loss_verdict(2, 5, 44)
    assert "未滿 40" in standings.stop_loss_verdict(5, 5, 39)
    assert "讀不到" in standings.stop_loss_verdict(5, 5, None)


def test_summary_lines_from_fixture():
    lines = "\n".join(standings.standings_lines({"METACULUS_TOKEN": "t"}, fake_get))
    assert "正賽：第 4/5（全榜第 6）" in lines and "觸發停損" in lines
    assert "MiniBench：第 1/2（全榜第 2），獎金欄 $120" in lines


def test_unreadable_leaderboard_is_reported_not_raised():
    def broken(url, headers=None, timeout=30):
        if url.endswith("/users/me"):
            return {"username": ME}
        raise ValueError("bad payload")

    lines = "\n".join(standings.standings_lines({"BOT_USERNAME": ME}, broken))
    assert lines.count("讀不到排行榜") == 2


def test_runs_once_a_day_or_manually(tmp_path):
    env = {"STATE_DIR": str(tmp_path / "st")}
    d1 = datetime(2026, 11, 1, 3, 7, tzinfo=timezone.utc)
    assert standings.due(env, d1)  # nothing stored: fail-open
    standings.mark_done(env, d1)
    assert (tmp_path / "st" / "changed").exists()  # makes the workflow save the cache
    assert not standings.due(env, datetime(2026, 11, 1, 13, 7, tzinfo=timezone.utc))
    assert standings.due(env, datetime(2026, 11, 2, 2, 47, tzinfo=timezone.utc))
    assert standings.due({**env, "GITHUB_EVENT_NAME": "workflow_dispatch"}, d1)


def test_due_fails_open_on_unreadable_state(tmp_path):
    (tmp_path / "standings_day").write_bytes(b"\xff\xfe\x00")
    assert standings.due({"STATE_DIR": str(tmp_path)}, datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc))


def test_username_line():
    lines = standings.standings_lines({"METACULUS_TOKEN": "t"}, fake_get)
    assert f"- bot 帳號：{ME}" in lines


def test_bad_payload_dumps_shape_without_values():
    secret = "SECRET-VALUE-123"

    def odd(url, headers=None, timeout=30):
        if url.endswith("/users/me"):
            return {"username": ME, "email": secret}
        if "/projects/tournaments/" in url:
            return {"id": 5, "note": secret}
        return {"results": [secret, secret], "meta": {"x": secret}}  # strings, not dicts -> AttributeError

    lines = "\n".join(standings.standings_lines({"METACULUS_TOKEN": "t"}, odd))
    assert lines.count("讀不到排行榜（AttributeError") == 2
    assert "top=dict" in lines and "keys=results,meta" in lines and "first element type=str" in lines
    assert secret not in lines and "email" not in lines
    assert all(len(x) <= 400 for x in lines.splitlines())
    assert "top=list" in standings.shape_of([{"a": secret}]) and secret not in standings.shape_of([{"a": secret}])


def test_list_payload_picks_primary_then_first():
    e = lambda n: {"entries": [{"user": {"username": n}, "rank": 1}]}
    assert standings.parse_entries([e("a"), {**e("b"), "is_primary_leaderboard": True}])[0].username == "b"
    assert standings.parse_entries([e("a"), e("b")])[0].username == "a"
    assert standings.parse_entries([{"entries": []}]) == []
    assert standings.parse_entries([]) == []


def test_empty_board_is_clean_and_unusable_entries_dump_entry_shape():
    secret = "SECRET-VALUE-123"

    def get(url, headers=None, timeout=30):
        if url.endswith("/users/me"):
            return {"username": ME}
        if "/projects/tournaments/" in url:
            return {"id": 7}
        if url.endswith("/leaderboards/project/33121/"):
            return [{"is_primary_leaderboard": True, "entries": []}]
        return [{"entries": [{"who": {"name": secret}, "pos": secret}]}]

    lines = "\n".join(standings.standings_lines({"METACULUS_TOKEN": "t"}, get))
    assert "正賽：榜上 0 筆" in lines
    assert "first entry keys=who->{name},pos" in lines and secret not in lines
