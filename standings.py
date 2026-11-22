"""
Standings check (spec 12, C7). Standard library only.

Reads the FutureEval main and MiniBench leaderboards and computes this bot's
rank among bots that are NOT Metaculus' own (`metac-*`). Stop-loss rule
(v0.4.10): once at least 40 main-tournament questions from the Claude era
(last forecast >= MAIN_CLAUDE_FROM) have resolved, an average per-question
peer score below STOP_LOSS_MIN_AVG (default 0) means "stop or switch model".
The rank is still printed but no longer part of the rule.

UNVERIFIED: written without a token. The endpoints below are the ones the
Metaculus site uses as far as known; field names are read defensively. The
first real run will tell: if parsing fails the summary says so and nothing
else is affected (the workflow step has continue-on-error).
  GET {base}/leaderboards/project/<project id>/
  GET {base}/projects/tournaments/<slug>/      (slug -> numeric id)
  GET {base}/users/me                          (bot username, if BOT_USERNAME unset)
"""
from __future__ import annotations

import os
import sys
import time
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import budget
import state as state_mod

API_BASE = "https://www.metaculus.com/api"
STOP_LOSS_MIN_RESOLVED = 40
STOP_LOSS_MIN_AVG = 0.0  # env STOP_LOSS_MIN_AVG overrides
MAIN_CLAUDE_FROM = "2026-11-12T13:55:00Z"  # env MAIN_CLAUDE_FROM: main-tournament provider switched to Claude
MINIBENCH_CLAUDE_FROM = "2026-11-01T00:00:00Z"  # env MINIBENCH_CLAUDE_FROM
SCORE_PAGES_MAX = 10  # 100 posts per page
REQUEST_GAP_S = 3.0  # pause between API requests (v0.4.9: three boards in a row drew 429)
RETRY_429_MAX = 4  # retries per request after a 429
RETRY_429_BASE_S = 15.0  # no Retry-After: wait 15, 30, 60, 120
RETRY_WAIT_CAP_S = 120.0  # one wait never longer than this
TOTAL_WAIT_CAP_S = 480.0  # all waits of one standings run (job timeout is 40 min)


@dataclass
class Entry:
    username: str
    is_bot: bool | None
    rank: int | None
    score: float | None
    prize: float | None
    questions: int | None  # questions that counted for this entry (resolved)


def _num(x: Any) -> float | None:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


class EntryShapeError(ValueError):
    """Entries present but unreadable; carries a values-free shape of the first entry."""

    def __init__(self, msg: str, shape: str):
        super().__init__(msg)
        self.shape = shape


def entry_shape(e: Any) -> str:
    if not isinstance(e, dict):
        return f"first entry type={type(e).__name__}"
    parts = []
    for k, v in e.items():
        if isinstance(v, dict):
            parts.append(f"{k}->{{{','.join(str(x) for x in v)}}}")
        else:
            parts.append(str(k))
    return "first entry keys=" + ",".join(parts)


def parse_entries(payload: Any) -> list[Entry]:
    if isinstance(payload, list):  # real API: list of leaderboard objects, each with `entries`
        boards = [b for b in payload if isinstance(b, dict)]
        payload = next((b for b in boards if b.get("is_primary_leaderboard")), boards[0] if boards else {})
    raw = payload.get("entries")
    if raw is None:
        raw = payload.get("results", [])
    out = []
    try:
        for e in raw:
            user = e.get("user") or {}
            name = user.get("username") or e.get("username") or ""
            if not name:
                continue
            q = e.get("contribution_count", e.get("question_count"))
            rank = e.get("rank")
            out.append(Entry(name, user.get("is_bot", e.get("is_bot")), int(rank) if rank is not None else None,
                             _num(e.get("score")), _num(e.get("prize")), int(q) if q is not None else None))
    except Exception as ex:
        raise EntryShapeError(f"{ex.__class__.__name__}: {ex}", entry_shape(raw[0])) from ex
    if raw and not out:
        raise EntryShapeError("no usable username in entries", entry_shape(raw[0]))
    return out


def non_metac_standing(entries: list[Entry], me: str) -> tuple[int, int, Entry] | None:
    """(rank among non-metac bots, how many non-metac bots, my entry) or None if I am not listed."""
    pool = [e for e in entries if not e.username.startswith("metac-") and e.is_bot is not False]
    pool.sort(key=lambda e: (e.rank if e.rank is not None else 10**9, -(e.score or 0)))
    for i, e in enumerate(pool, start=1):
        if e.username == me:
            return i, len(pool), e
    return None


def stop_loss_verdict(claude_n: int, claude_avg: float | None, min_avg: float = STOP_LOSS_MIN_AVG) -> str:
    """v0.4.10: judged on the Claude-era resolved main questions and their mean peer score, not on rank."""
    if claude_n < STOP_LOSS_MIN_RESOLVED or claude_avg is None:
        return f"Claude 時代已結算 {claude_n} 題，未滿 {STOP_LOSS_MIN_RESOLVED}"
    if claude_avg < min_avg:
        return (f"**觸發停損**：Claude 時代已結算 {claude_n} 題，每題平均 peer score {claude_avg:+.2f} < {min_avg:g}。"
                "停掉或換 model。")
    return f"Claude 時代已結算 {claude_n} 題，每題平均 peer score {claude_avg:+.2f} >= {min_avg:g}：繼續"


def _env_float(env: dict[str, str], key: str, default: float) -> float:
    try:
        return float(str(env.get(key, "")).strip())
    except ValueError:
        return default


def _iso(value: Any) -> datetime | None:
    import precheck  # lazy: precheck imports budget only, but keep standings importable alone
    return precheck._stamp_time(value)


def _fmt_t(t: datetime | None) -> str:
    return t.strftime("%Y-%m-%d %H:%MZ") if t else "?"


@dataclass
class Scored:
    post_id: Any
    qtype: str
    resolved_at: datetime | None
    last_forecast: datetime | None
    score: float | None


SCORE_KEYS = ("spot_peer_score", "peer_score")  # first one present wins


def spot_peer(score_data: Any) -> float | None:
    if not isinstance(score_data, dict):
        return None
    for k in SCORE_KEYS:
        if _num(score_data.get(k)) is not None:
            return _num(score_data.get(k))
    return None


def scored_questions(post: dict[str, Any]) -> tuple[list[Scored], set[str]]:
    """Resolved questions of one post on which this bot has a forecast with score_data. -> (rows, score_data keys)."""
    import precheck
    qs: list[Any] = []
    if isinstance(post.get("question"), dict):
        qs.append(post["question"])
    g = post.get("group_of_questions")
    if isinstance(g, dict):
        qs.extend(x for x in g.get("questions") or [] if isinstance(x, dict))
    rows, keys = [], set()
    for q in qs:
        mine = q.get("my_forecasts")
        if not isinstance(mine, dict) or not mine.get("history"):
            continue
        sd = mine.get("score_data")
        if isinstance(sd, dict):
            keys.update(str(k) for k in sd)
        if q.get("resolution") in (None, "") and q.get("status") != "resolved":
            continue
        rows.append(Scored(post.get("id") or q.get("id"), str(q.get("type") or "?"),
                           _iso(q.get("actual_resolve_time") or q.get("scheduled_resolve_time")),
                           precheck.last_forecast_time(q), spot_peer(sd)))
    return rows, keys


def era_stats(rows: list[Scored], cutoff: datetime) -> tuple[tuple[int, float | None], tuple[int, float | None]]:
    """((claude n, mean), (old n, mean)); only rows with a score count. No last-forecast time -> old era."""
    def agg(xs: list[Scored]) -> tuple[int, float | None]:
        v = [r.score for r in xs if r.score is not None]
        return len(v), (sum(v) / len(v) if v else None)
    scored = [r for r in rows if r.score is not None]
    new = [r for r in scored if r.last_forecast is not None and r.last_forecast >= cutoff]
    old = [r for r in scored if r not in new]
    return agg(new), agg(old)


def score_lines(label: str, rows: list[Scored], keys: set[str], cutoff: datetime) -> tuple[list[str], int, float | None]:
    out = [f"- {label} 逐題（已結算且 bot 有預測 {len(rows)} 題；score_data keys: {','.join(sorted(keys)) or '無'}）"]
    for r in sorted(rows, key=lambda r: r.resolved_at or datetime.min.replace(tzinfo=timezone.utc)):
        sc = "?" if r.score is None else f"{r.score:+.2f}"
        out.append(f"  - post {r.post_id}｜{r.qtype}｜結算 {_fmt_t(r.resolved_at)}｜最後預測 {_fmt_t(r.last_forecast)}｜peer {sc}")
    (cn, ca), (on, oa) = era_stats(rows, cutoff)
    f = lambda a: "n/a" if a is None else f"{a:+.2f}"
    out.append(f"  - {label} Claude 時代（最後預測 >= {_fmt_t(cutoff)}）n={cn}，每題平均 peer score {f(ca)}")
    out.append(f"  - {label} 舊時代 n={on}，每題平均 peer score {f(oa)}")
    return out, cn, ca


def resolve_project(project: str, get: Callable[[str], Any], base: str) -> str:
    if project.isdigit():
        return project
    return str(get(f"{base}/projects/tournaments/{project}/")["id"])


def shape_of(payload: Any, limit: int = 400) -> str:
    """Types and key names only, never values (public repo logs)."""
    parts = [f"top={type(payload).__name__}"]
    if isinstance(payload, dict):
        parts.append("keys=" + ",".join(str(k) for k in payload))
        for k, v in payload.items():
            if isinstance(v, (list, tuple)):
                first = v[0] if v else None
                if isinstance(first, dict):
                    sub = "keys=" + ",".join(str(x) for x in first)
                else:
                    sub = "type=" + type(first).__name__
                parts.append(f"first list field {k!r}: len={len(v)}, first element {sub}")
                break
    elif isinstance(payload, (list, tuple)):
        first = payload[0] if payload else None
        sub = "keys=" + ",".join(str(x) for x in first) if isinstance(first, dict) else "type=" + type(first).__name__
        parts.append(f"len={len(payload)}, first element {sub}")
    return "; ".join(parts)[:limit]


def board_overview(payload: Any, me: str) -> list[str]:
    """v0.4.2: one line per leaderboard (name/primary/bot_exclusion_status/entry count) plus my own
    entry wherever it sits, primary or not. Never prints anyone else's data."""
    if not isinstance(payload, list):
        return []
    out = []
    for i, b in enumerate(x for x in payload if isinstance(x, dict)):
        raw = b.get("entries") or []
        name = b.get("name") or b.get("score_type") or b.get("leaderboard_type") or f"#{i}"
        flag = "primary" if b.get("is_primary_leaderboard") else "non-primary"
        out.append(f"  - board {str(name)[:40]!r}（{flag}，bot_exclusion={b.get('bot_exclusion_status')}，{len(raw)} 筆）")
        for e in raw:
            if isinstance(e, dict) and ((e.get("user") or {}).get("username") or e.get("username")) == me:
                out.append(f"    - {me} 在此 board：rank {e.get('rank')}，score {e.get('score')}")
    return out


def _retry_after(e: Exception) -> float | None:
    try:
        return max(0.0, float(e.headers.get("Retry-After")))  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return None


def fetch_scored(pid: str, my_id: Any, get: Callable[[str], Any], base: str) -> tuple[list[Scored], set[str]]:
    """Resolved posts of one tournament that this bot forecast on (same params as forecasting_tools
    MetaculusClient: with_cp, forecaster_id)."""
    import urllib.parse
    rows: list[Scored] = []
    keys: set[str] = set()
    for page in range(SCORE_PAGES_MAX):
        params = {"tournaments": pid, "statuses": "resolved", "limit": 100, "offset": page * 100,
                  "order_by": "-published_time", "with_cp": "true", "include_conditional_cps": "true"}
        if my_id is not None:
            params["forecaster_id"] = my_id
        data = get(f"{base}/posts/?{urllib.parse.urlencode(params)}")
        results = data.get("results", []) if isinstance(data, dict) else []
        for post in results:
            r, k = scored_questions(post)
            rows.extend(r)
            keys |= k
        if not isinstance(data, dict) or not data.get("next") or not results:
            break
    return rows, keys


def standings_lines(
    env: dict[str, str],
    get_json: Callable[..., Any] = budget.http_get_json,
    sleep: Callable[[float], None] | None = None,
) -> list[str]:
    nap = sleep or time.sleep
    waited = [0.0]
    first = [True]
    token = env.get("METACULUS_TOKEN", "")
    base = (env.get("METACULUS_API_BASE_URL") or API_BASE).rstrip("/")
    headers = {"Authorization": f"Token {token}"} if token else {}

    seen: list[Any] = []

    def get(url: str) -> Any:
        seen.append(None)
        if not first[0]:
            nap(REQUEST_GAP_S)
        first[0] = False
        attempt = 0
        while True:
            try:
                data = get_json(url, headers, 30)
                break
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt >= RETRY_429_MAX:
                    raise
                ra = _retry_after(e)
                wait = ra if ra is not None else RETRY_429_BASE_S * (2 ** attempt)
                wait = min(wait, RETRY_WAIT_CAP_S)
                if waited[0] + wait > TOTAL_WAIT_CAP_S:
                    raise
                waited[0] += wait
                attempt += 1
                nap(wait)
        seen[-1] = data
        return data

    lines = ["", "### 排行榜（非 metac-* bot 裡的名次）"]
    my_id: Any = None
    try:
        if env.get("BOT_USERNAME"):
            me = env["BOT_USERNAME"]
        else:
            who = get(f"{base}/users/me")
            me, my_id = who["username"], who.get("id")
    except Exception as e:
        return lines + [f"- 讀不到 bot 帳號名稱（{e.__class__.__name__}）；可設 repo variable BOT_USERNAME"]
    lines.append(f"- bot 帳號：{me}")
    if my_id is None:  # BOT_USERNAME set: the id is still needed for forecaster_id
        try:
            my_id = get(f"{base}/users/me").get("id")
        except Exception:
            my_id = None
    claude_from = {
        "正賽": _iso(env.get("MAIN_CLAUDE_FROM") or MAIN_CLAUDE_FROM),
        "MiniBench": _iso(env.get("MINIBENCH_CLAUDE_FROM") or MINIBENCH_CLAUDE_FROM),
        # no era given for Market Pulse: defaults to the main-tournament provider switch
        "Market Pulse 26Q4": _iso(env.get("MARKETPULSE_CLAUDE_FROM") or env.get("MAIN_CLAUDE_FROM") or MAIN_CLAUDE_FROM),
    }
    boards = [("正賽", env.get("STANDINGS_MAIN_PROJECT") or "33121", True),
              ("MiniBench", env.get("STANDINGS_MINIBENCH_PROJECT") or "minibench", False),
              ("Market Pulse 26Q4", env.get("STANDINGS_MARKETPULSE_PROJECT") or "market-pulse-26q4", False)]
    for label, project, is_main in boards:
        seen.clear()
        try:
            pid = resolve_project(project, get, base)
            payload = get(f"{base}/leaderboards/project/{pid}/")
            entries = parse_entries(payload)
        except Exception as e:
            detail = f"{e.__class__.__name__}: {e}"[:200]
            shape = e.shape if isinstance(e, EntryShapeError) else shape_of(seen[-1]) if seen and seen[-1] is not None else "no response body"
            lines.append(f"- {label}：讀不到排行榜（{detail}）；回應形狀 {shape}"[:400])
            if is_main:
                lines.append("- 停損：排行榜讀不到，停損先不判斷")
            continue
        overview = board_overview(payload, me)
        found = non_metac_standing(entries, me) if entries else None
        if not entries:
            lines.append(f"- {label}：榜上 0 筆")
        elif found is None:
            lines.append(f"- {label}：榜上還沒有 {me}（共 {len(entries)} 筆）")
        else:
            rank, total, mine = found
            prize = f"，獎金欄 ${mine.prize:.0f}" if mine.prize else ""
            note = "（不參與停損）" if is_main else ""
            lines.append(f"- {label}：第 {rank}/{total}（全榜第 {mine.rank}）{prize}{note}")
        lines.extend(overview)
        # v0.4.10: per-question peer scores, era split, and (main only) the stop-loss verdict
        cn, ca = 0, None
        try:
            rows, keys = fetch_scored(pid, my_id, get, base)
            sl, cn, ca = score_lines(label, rows, keys, claude_from[label])
            lines.extend(sl)
        except Exception as e:
            lines.append(f"- {label} 逐題分數讀不到（{e.__class__.__name__}: {e}"[:200] + "）")
            if is_main:
                lines.append("- 停損：逐題分數讀不到，停損先不判斷")
                continue
        if is_main:
            lines.append(f"- 停損：{stop_loss_verdict(cn, ca, _env_float(env, 'STOP_LOSS_MIN_AVG', STOP_LOSS_MIN_AVG))}")
    return lines


DAY_FILE = "standings_day"


def due(env: dict[str, str], now: datetime, directory: Path | None = None) -> bool:
    """First run of the UTC day (date kept in .state/standings_day), or a manual run.
    Fail-open: if the stored date cannot be read, run."""
    if env.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        return True
    try:
        last = ((directory or state_mod.state_dir(env)) / DAY_FILE).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return True
    return last != now.strftime("%Y-%m-%d")


def mark_done(env: dict[str, str], now: datetime, directory: Path | None = None) -> None:
    """Record today's date and touch .state/changed so the workflow saves the cache."""
    d = directory or state_mod.state_dir(env)
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / DAY_FILE).write_text(now.strftime("%Y-%m-%d"), encoding="utf-8")
        (d / "changed").write_text("1", encoding="utf-8")
    except OSError:
        pass


def main() -> int:
    env = dict(os.environ)
    now = datetime.now(timezone.utc)
    if "--daily" in sys.argv:
        if not due(env, now):
            return 0
        mark_done(env, now)  # before the fetch: a failing board must not retry every 20 minutes
    text = "\n".join(standings_lines(env)) + "\n"
    print(text)
    if env.get("GITHUB_STEP_SUMMARY"):
        with open(env["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
