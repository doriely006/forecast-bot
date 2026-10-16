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


def test_runs_once_a_day_or_manually():
    assert standings.due({}, datetime(2026, 11, 1, 0, 7, tzinfo=timezone.utc))
    assert not standings.due({}, datetime(2026, 11, 1, 0, 27, tzinfo=timezone.utc))
    assert standings.due({"GITHUB_EVENT_NAME": "workflow_dispatch"}, datetime(2026, 11, 1, 13, 0))
