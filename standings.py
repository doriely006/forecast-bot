"""
Standings check (spec 12, C7). Standard library only.

Reads the FutureEval main and MiniBench leaderboards and computes this bot's
rank among bots that are NOT Metaculus' own (`metac-*`). Stop-loss rule:
once at least 40 main-tournament questions have resolved for this bot, a rank
in the bottom half of non-metac bots means "stop or switch model".

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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import budget

API_BASE = "https://www.metaculus.com/api"
STOP_LOSS_MIN_RESOLVED = 40


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


def parse_entries(payload: dict[str, Any]) -> list[Entry]:
    raw = payload.get("entries")
    if raw is None:
        raw = payload.get("results", [])
    out = []
    for e in raw:
        user = e.get("user") or {}
        name = user.get("username") or e.get("username") or ""
        if not name:
            continue
        q = e.get("contribution_count", e.get("question_count"))
        rank = e.get("rank")
        out.append(Entry(name, user.get("is_bot", e.get("is_bot")), int(rank) if rank is not None else None,
                         _num(e.get("score")), _num(e.get("prize")), int(q) if q is not None else None))
    return out


def non_metac_standing(entries: list[Entry], me: str) -> tuple[int, int, Entry] | None:
    """(rank among non-metac bots, how many non-metac bots, my entry) or None if I am not listed."""
    pool = [e for e in entries if not e.username.startswith("metac-") and e.is_bot is not False]
    pool.sort(key=lambda e: (e.rank if e.rank is not None else 10**9, -(e.score or 0)))
    for i, e in enumerate(pool, start=1):
        if e.username == me:
            return i, len(pool), e
    return None


def stop_loss_verdict(rank: int, total: int, resolved: int | None) -> str:
    if resolved is None:
        return "結算題數讀不到，停損規則先不判斷"
    if resolved < STOP_LOSS_MIN_RESOLVED:
        return f"已結算 {resolved} 題，未滿 {STOP_LOSS_MIN_RESOLVED} 題，停損規則先不判斷"
    if rank > total / 2:
        return f"**觸發停損**：已結算 {resolved} 題，非 metac bot 裡第 {rank}/{total}，落在後 50%。停掉或換 model。"
    return f"已結算 {resolved} 題，非 metac bot 裡第 {rank}/{total}，在前 50%：繼續"


def resolve_project(project: str, get: Callable[[str], Any], base: str) -> str:
    if project.isdigit():
        return project
    return str(get(f"{base}/projects/tournaments/{project}/")["id"])


def standings_lines(
    env: dict[str, str], get_json: Callable[..., Any] = budget.http_get_json
) -> list[str]:
    token = env.get("METACULUS_TOKEN", "")
    base = (env.get("METACULUS_API_BASE_URL") or API_BASE).rstrip("/")
    headers = {"Authorization": f"Token {token}"} if token else {}

    def get(url: str) -> Any:
        return get_json(url, headers, 30)

    lines = ["", "### 排行榜（非 metac-* bot 裡的名次）"]
    try:
        me = env.get("BOT_USERNAME") or get(f"{base}/users/me")["username"]
    except Exception as e:
        return lines + [f"- 讀不到 bot 帳號名稱（{e.__class__.__name__}）；可設 repo variable BOT_USERNAME"]
    boards = [("正賽", env.get("STANDINGS_MAIN_PROJECT") or "33121", True),
              ("MiniBench", env.get("STANDINGS_MINIBENCH_PROJECT") or "minibench", False)]
    for label, project, is_main in boards:
        try:
            pid = resolve_project(project, get, base)
            entries = parse_entries(get(f"{base}/leaderboards/project/{pid}/"))
        except Exception as e:
            lines.append(f"- {label}：讀不到排行榜（{e.__class__.__name__}）")
            continue
        found = non_metac_standing(entries, me)
        if found is None:
            lines.append(f"- {label}：榜上還沒有 {me}（共 {len(entries)} 筆）")
            continue
        rank, total, mine = found
        prize = f"，獎金欄 ${mine.prize:.0f}" if mine.prize else ""
        lines.append(f"- {label}：第 {rank}/{total}（全榜第 {mine.rank}）{prize}")
        if is_main:
            lines.append(f"- 停損：{stop_loss_verdict(rank, total, mine.questions)}")
    return lines


def due(env: dict[str, str], now: datetime) -> bool:
    """Once a day (the first run after 00:00 UTC), or on a manual run."""
    return env.get("GITHUB_EVENT_NAME") == "workflow_dispatch" or (now.hour == 0 and now.minute < 20)


def main() -> int:
    env = dict(os.environ)
    if "--daily" in sys.argv and not due(env, datetime.now(timezone.utc)):
        return 0
    text = "\n".join(standings_lines(env)) + "\n"
    print(text)
    if env.get("GITHUB_STEP_SUMMARY"):
        with open(env["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
