"""
Light first step of every tournament run. Standard library only: it runs on
the runner's system Python before any dependency is installed (spec 12, B14).

- Validates the switches: only "1" counts as on, other non-empty values fail (B5).
- Lists every open question of both tournaments with open/close time (A1, C4).
- PUBLISH=1: re-sends comments left over from earlier runs (A3) and decides
  whether there is anything new to forecast. Nothing new -> the heavy install
  and main.py are skipped and the run ends in seconds.
- Dry run: the listing IS the run, unless a manual run asked for DRY_RUN_LLM.
- Yellow warnings: repo turned private, last commit older than 45 days
  (GitHub disables schedules in public repos after 60 days without activity).
Writes `work=true|false` to $GITHUB_OUTPUT. Exit 1 only on systemic problems.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import budget
import state as state_mod

VERSION = "0.3.2"
SWITCHES = ("BOT_ENABLED", "PUBLISH", "DRY_RUN_LLM")
# Must equal forecasting_tools.MetaculusClient.CURRENT_AI_COMPETITION_ID /
# CURRENT_MINIBENCH_ID (asserted in tests).
TOURNAMENTS: list[tuple[str, int | str]] = [
    ("FutureEval Fall 2026", 33121),
    ("MiniBench", "minibench"),
]
SUPPORTED_TYPES = {"binary", "multiple_choice", "numeric", "discrete", "date"}
API_BASE = "https://www.metaculus.com/api"
STALE_COMMIT_DAYS = 45


def validate_switches(env: dict[str, str], event: str | None = None) -> list[str]:
    errors = []
    for name in SWITCHES:
        value = env.get(name, "")
        if value not in ("", "1"):
            errors.append(
                f'{name}="{value}" is not allowed: use 1 to turn it on, delete the variable to turn it off'
            )
    if budget.provider_error(env):
        errors.append(budget.provider_error(env))
    if event == "schedule" and env.get("BOT_ENABLED", "") != "1" and not errors:
        errors.append("scheduled run without BOT_ENABLED=1")
    return errors


def summary_header(publish: bool) -> str:
    """First line of every job summary (B5)."""
    if publish:
        return "**已送出**模式（PUBLISH=1：有新題就送出預測與說明）"
    return "**DRY RUN**（PUBLISH 沒設：什麼都不送到 Metaculus）"


@dataclass
class QItem:
    tournament: str
    post_id: int
    question_id: int | None
    qtype: str
    title: str
    open_time: str | None
    close_time: str | None
    forecasted: bool  # same rule as the SDK: my_forecasts.history non-empty

    @property
    def url(self) -> str:
        return f"https://www.metaculus.com/questions/{self.post_id}/"

    @property
    def supported(self) -> bool:
        return self.qtype in SUPPORTED_TYPES

    @property
    def open_hours(self) -> float | None:
        try:
            o = datetime.fromisoformat(str(self.open_time).replace("Z", "+00:00"))
            c = datetime.fromisoformat(str(self.close_time).replace("Z", "+00:00"))
            return (c - o).total_seconds() / 3600
        except (TypeError, ValueError):
            return None


def _forecasted(q: dict[str, Any]) -> bool:
    try:
        history = q["my_forecasts"]["history"]
    except (KeyError, TypeError):
        return False
    return bool(history)


def flatten_post(post: dict[str, Any], tournament: str) -> list[QItem]:
    if "notebook" in post:
        return []
    pid, title = post["id"], post.get("title", "")
    group = post.get("group_of_questions")
    if group:
        return [
            QItem(tournament, pid, q.get("id"), q.get("type", "?"), f"{title} / {q.get('label', '')}",
                  q.get("open_time"), q.get("scheduled_close_time"), _forecasted(q))
            for q in group.get("questions", [])
        ]
    if post.get("conditional"):
        yes = post["conditional"].get("question_yes", {})
        return [QItem(tournament, pid, None, "conditional", title, yes.get("open_time"),
                      yes.get("scheduled_close_time"), _forecasted(yes))]
    q = post.get("question") or {}
    return [QItem(tournament, pid, q.get("id"), q.get("type", "?"), title,
                  q.get("open_time"), q.get("scheduled_close_time"), _forecasted(q))]


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def fetch_open_items(
    tournament: str,
    tid: int | str,
    token: str,
    get_json: Callable[..., Any] = budget.http_get_json,
    sleep: Callable[[float], None] = time.sleep,
    base: str | None = None,
) -> list[QItem]:
    base = (base or os.environ.get("METACULUS_API_BASE_URL") or API_BASE).rstrip("/")
    items: list[QItem] = []
    offset = 0
    while True:
        params = urllib.parse.urlencode(
            {"tournaments": tid, "statuses": "open", "limit": 100, "offset": offset,
             "order_by": "-published_time"}
        )
        try:
            data = get_json(f"{base}/posts/?{params}",
                            {"Authorization": f"Token {token}", "Accept-Language": "en"}, 30)
        except urllib.error.HTTPError as e:
            raise FetchError(f"HTTP {e.code}", e.code) from None
        except Exception as e:
            raise FetchError(e.__class__.__name__) from None
        results = data.get("results", [])
        for post in results:
            items.extend(flatten_post(post, tournament))
        if not data.get("next") or not results:
            return items
        offset += 100
        sleep(1.0)


def http_post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout: int = 30) -> int:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={**headers, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def resend_pending(
    store: state_mod.PendingComments,
    token: str,
    post: Callable[..., int] = http_post_json,
    base: str | None = None,
) -> tuple[int, list[str], list[dict[str, Any]]]:
    """Re-sends stored comments exactly as written (A3). -> (sent, still failing, given up)."""
    base = (base or os.environ.get("METACULUS_API_BASE_URL") or API_BASE).rstrip("/")
    sent, failing = 0, []
    for post_id, item in list(store.items.items()):
        body = {"on_post": int(post_id), "text": item["text"], "is_private": True, "included_forecast": True}
        try:
            post(f"{base}/comments/create/", body, {"Authorization": f"Token {token}"})
            store.remove(post_id)
            sent += 1
        except Exception as e:
            code = getattr(e, "code", None)
            store.mark_attempt(post_id, f"HTTP {code}" if code else e.__class__.__name__)
            failing.append(f"[{post_id}]({item.get('url', '')}): {store.items[post_id]['last_error']}")
    return sent, failing, store.give_up()


def last_commit_age_days(now: datetime, run: Callable[..., Any] = subprocess.run) -> float | None:
    try:
        out = run(["git", "log", "-1", "--format=%ct"], capture_output=True, text=True, timeout=10)
        return (now.timestamp() - int(out.stdout.strip())) / 86400
    except Exception:
        return None


def repo_is_private(env: dict[str, str], get_json: Callable[..., Any] = budget.http_get_json) -> bool | None:
    """github.event.repository.private first; else ask the GitHub API with the job token."""
    raw = env.get("REPO_PRIVATE", "").strip().lower()
    if raw in ("true", "false"):
        return raw == "true"
    repo, token = env.get("GITHUB_REPOSITORY"), env.get("GITHUB_TOKEN")
    if not repo:
        return None
    try:
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return bool(get_json(f"https://api.github.com/repos/{repo}", headers, 10)["private"])
    except Exception:
        return None


def repo_warnings(age_days: float | None, private: bool | None) -> list[str]:
    out = []
    if age_days is not None and age_days > STALE_COMMIT_DAYS:
        out.append(
            f"最後一次 commit 是 {age_days:.0f} 天前。公開 repo 60 天沒有 commit，GitHub 會停掉排程；"
            "60 天前要有一次 commit（任何小改動都算）。"
        )
    if private:
        out.append("repo 現在是私人。私人 repo 每月只有 2,000 分鐘免費額度，每 20 分鐘跑一次會不夠用。")
    return out


def _money(x: float | None) -> str:
    return "n/a" if x is None else f"${x:.2f}"


def pacing_lines(decision: budget.BudgetDecision, items: list[QItem], env: dict[str, str]) -> list[str]:
    """A2: per-question budget and projected run-out date (catalogue prices; Anthropic: env/table prices,
    paced until the next credit reset)."""
    if decision.remaining is None:
        return []
    if budget.select_provider(env) == "openai":
        return []  # v0.3.0: OpenAI pacing is in openai_precheck (token plan)
    models, cheap, prices = budget.provider_prices(env, None)
    open_new = {k: 0 for k in budget.KINDS}
    for it in items:
        if it.supported and not it.forecasted:
            open_new[budget.kind_of(it.tournament)] += 1
    today = budget.utc_today()
    p = budget.pace(decision.remaining, today, env, open_new,
                    {k: (prices[models[k]], prices[cheap]) for k in budget.KINDS},
                    budget.pacing_horizon(env, today), budget.select_provider(env))
    parts = [
        f"{k}: {_money(v.per_question_budget)}／題 -> {v.tier.name}（{v.tier.forecasts} 次預測，估 {_money(v.est_cost)}）"
        for k, v in p.kinds.items()
    ]
    split = "已過分配日，正賽優先" if p.after_split else "分配日前，兩邊同配置"
    return [
        f"- 配速（{split}）：" + "；".join(parts),
        f"- 預估額度見底日：{p.run_out.isoformat() if p.run_out else 'n/a'}",
    ]


def openai_lines(free: dict[str, tuple[int, int]] | None, period: tuple[str, str], spent: float | None,
                 total: float | None) -> list[str]:
    """Today's free tokens per group and this month's paid spend (both booked locally)."""
    out = []
    if free:
        parts = [f"{g} 已用 {u:,}／{lim:,}（剩 {max(0, lim - u):,}）" for g, (u, lim) in free.items()]
        out.append("- OpenAI 今日免費 token（UTC 日，假設 00:00 UTC 重置，本機記帳）：" + "；".join(parts))
    out.append(f"- OpenAI 本月付費（{period[0][:7]}，本機記帳）：{_money(spent)}／預算 {_money(total)}")
    return out


def _when(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def openai_precheck(lookup: budget.KeyLookup, new: list[QItem], env: dict[str, str],
                    now: datetime) -> tuple[list[QItem], list[str]]:
    """v0.3.0: today's free tokens, month spend, and the same token plan as main.py; questions it would
    defer (free tokens used up, closing > 30 h away) are not work for this run."""
    start = now.date().replace(day=1)
    info = lookup.info
    lines = openai_lines(lookup.free, (start.isoformat(), budget.month_end(now.date()).isoformat()),
                         info.usage if info else None, budget.openai_budget(env))
    todo = sorted(new, key=lambda it: (_when(it.close_time) or datetime.max.replace(tzinfo=timezone.utc)))
    plan = budget.openai_plan([(budget.kind_of(it.tournament), _when(it.close_time)) for it in todo],
                              lookup.free, now, env)
    tiers = "、".join(f"{k} {t.name}（{t.forecasts} 次預測）" for k, t in plan.tiers.items())
    lines.append(f"- 免費額度配速：{tiers}；{plan.reason}")
    if plan.deferred:
        lines += _block("WARNING", f"**今天的免費 token 用完，{len(plan.deferred)} 題延後**（截止還超過 30 小時，"
                        "等 00:00 UTC 額度重置後再做）")
    gone = set(plan.deferred)
    return [it for i, it in enumerate(todo) if i not in gone], lines


def render_listing(items: list[QItem]) -> list[str]:
    """Open / close time of every open question (C4: kept as an after-the-fact check)."""
    if not items:
        return []
    lines = ["", "<details><summary>開放中的題目（開放／截止時間，UTC）</summary>", ""]
    windows = [(it.open_hours, it) for it in items if it.open_hours is not None]
    if windows:
        h, it = min(windows, key=lambda w: w[0])
        lines += [f"最短開放窗：{h:.1f} 小時（[{it.title[:60]}]({it.url})）", ""]
    lines += ["| tournament | id | type | question | opens | closes | hours | done |",
              "|---|---|---|---|---|---|---|---|"]
    for it in items:
        oh = f"{it.open_hours:.1f}" if it.open_hours is not None else "-"
        title = it.title[:70].replace("|", "/")
        done = "yes" if it.forecasted else ("skip" if not it.supported else "")
        lines.append(f"| {it.tournament} | {it.post_id}/{it.question_id} | {it.qtype} | [{title}]({it.url}) "
                     f"| {it.open_time or '-'} | {it.close_time or '-'} | {oh} | {done} |")
    return lines + ["", "</details>"]


def _block(kind: str, text: str) -> list[str]:
    return ["", f"> [!{kind}]", f"> {text}", ""]


def _skipped_block(stuck: list[dict[str, Any]], limit: int, cooldown: int) -> list[str]:
    if not stuck:
        return []
    rows = [f"> - {s['id']}｜{s['title'][:60].replace(chr(10), ' ')}｜失敗 {s['count']} 次｜"
            f"{' '.join(str(s['last_error']).split())[:200]}｜下次重試 {s['next_retry']} UTC" for s in stuck]
    return ["", "> [!WARNING]",
            f"> **{len(stuck)} 題連續失敗，暫停重試（不再呼叫 AI，省錢）**；"
            f"失敗 {limit} 輪就停，冷卻 {cooldown} 小時後再試一次"
            "（MAX_QUESTION_FAILED_RUNS／SKIP_COOLDOWN_HOURS），題目截止後自動消失", *rows, ""]


def write_out(name: str, value: str, env: dict[str, str]) -> None:
    path = env.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def write_summary(text: str, env: dict[str, str]) -> None:
    print(text)
    path = env.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


@dataclass
class Deps:
    get_json: Callable[..., Any] = budget.http_get_json
    post: Callable[..., int] = http_post_json
    sleep: Callable[[float], None] = time.sleep
    commit_age: Callable[[], float | None] = lambda: last_commit_age_days(datetime.now(timezone.utc))
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


def run(env: dict[str, str], deps: Deps | None = None) -> tuple[int, bool, str]:
    """-> (exit code, work for main.py, summary markdown)."""
    deps = deps or Deps()
    event = env.get("GITHUB_EVENT_NAME", "")
    publish = env.get("PUBLISH", "") == "1"
    lines = [summary_header(publish), "", f"## forecast-bot v{VERSION}：快速檢查", ""]
    errors = validate_switches(env, event)
    if errors:
        for e in errors:
            lines += _block("CAUTION", f"**開關設定錯誤**：{e}")
        return 1, False, "\n".join(lines) + "\n"

    systemic: list[str] = []
    for w in repo_warnings(deps.commit_age(), repo_is_private(env, deps.get_json)):
        lines += _block("WARNING", w)

    token = env.get("METACULUS_TOKEN", "")
    items: list[QItem] = []
    fetch_errors: list[str] = []
    counts: dict[str, int] = {}
    if not token:
        systemic.append("METACULUS_TOKEN 沒設，抓不到題目")
    else:
        for name, tid in TOURNAMENTS:  # B12: each tournament on its own
            try:
                got = fetch_open_items(name, tid, token, deps.get_json, deps.sleep)
                items += got
                counts[name] = len(got)
            except FetchError as e:
                fetch_errors.append(f"{name}: {e}")
                if e.status in (401, 403):
                    systemic.append(f"Metaculus 拒絕 token（{name}: {e}）")
        if len(fetch_errors) == len(TOURNAMENTS) and not systemic:
            systemic.append("兩個賽事都抓題失敗：" + "；".join(fetch_errors))
        elif fetch_errors:
            for err in fetch_errors:
                lines += _block("WARNING", f"**抓題失敗（另一邊照跑）**：{err}")

    store = state_mod.PendingComments.load(state_mod.state_dir(env))
    if publish and token and store.items:
        sent, failing, dropped = resend_pending(store, token, deps.post)
        lines.append(f"- 補送上次沒送成的說明：成功 {sent}，仍失敗 {len(failing)}")
        if failing:
            lines += _block("CAUTION", "**說明（comment）仍送不出去**，下一輪再補：" + "；".join(failing))
        for d in dropped:
            lines += _block("CAUTION", f"**放棄補送說明**（試了 {state_mod.MAX_RESEND_ATTEMPTS} 次）：{d.get('url', d['post_id'])}")

    provider = budget.select_provider(env)
    label = budget.provider_label(provider)
    lookup = budget.lookup_for(env, store, deps.get_json, deps.now().date())
    decision = budget.decide(lookup, env)
    fail_limit = state_mod.max_failed_runs(env)
    cooldown = state_mod.skip_cooldown_hours(env)
    # v0.1.2/3: questions that failed MAX_QUESTION_FAILED_RUNS runs in a row are not "work"
    # until SKIP_COOLDOWN_HOURS after their last attempt; then they are work again (one more try)
    dead, _due = store.split_skipped(fail_limit, cooldown, deps.now())
    todo = [it for it in items if it.supported and not it.forecasted]
    new = [it for it in todo if state_mod.question_key(it.question_id, it.post_id) not in dead]
    stuck = [
        {"id": k, "title": it.title, "url": it.url, "count": int(dead[k]["count"]),
         "last_error": dead[k].get("last_error", ""), "next_retry": dead[k]["next_retry"]}
        for it in todo
        if (k := state_mod.question_key(it.question_id, it.post_id)) in dead
    ]
    unsupported = [it for it in items if not it.supported]
    dry_llm = env.get("DRY_RUN_LLM", "") == "1" and event == "workflow_dispatch"
    needs_llm = (publish and bool(new)) or (not publish and dry_llm)
    if publish and decision.systemic:
        systemic.append(f"{label}：{decision.reason}")
    elif needs_llm and not decision.may_call_llm:
        systemic.append(f"{label}：{decision.reason}")
    if decision.warning:
        lines += _block("WARNING", decision.warning)

    per = "、".join(f"{k} {v}" for k, v in counts.items()) or "無"
    lines += [
        f"- 開放題：{len(items)}（{per}）；已預測 {sum(it.forecasted for it in items)}；"
        f"還沒預測 {len(new)}；不支援的題型跳過 {len(unsupported)}",
        f"- Provider：{provider}",
        f"- {label} 額度：{decision.state}（{decision.reason}）",
    ]
    if provider == "anthropic" and decision.remaining is not None:
        start, nxt = budget.credit_period(deps.now().date(), budget.credit_reset_day(env))
        lines.append(f"- Anthropic 本期（{start} 起，{nxt} 重置，本機記帳）：已花 "
                     f"{_money(budget.anthropic_budget(env) - decision.remaining)}／預算 "
                     f"{_money(budget.anthropic_budget(env))}")
    lines += pacing_lines(decision, items, env)
    if provider == "openai" and decision.remaining is not None:
        new, plan_lines = openai_precheck(lookup, new, env, deps.now())
        lines += plan_lines
        needs_llm = (publish and bool(new)) or (not publish and dry_llm)
    lines += _skipped_block(stuck, fail_limit, cooldown)
    work = needs_llm and not systemic
    if publish:
        lines.append(f"- 這輪：{'有新題，進入預測' if work else '沒有要做的事，到此結束（不安裝套件）'}")
    else:
        lines.append(f"- 這輪：{'手動試跑 AI（不送出）' if work else '只列題，不呼叫 AI'}")
    for s in systemic:
        lines += _block("CAUTION", f"**停止**：{s}")
    lines += render_listing(items)
    return (1 if systemic else 0), work, "\n".join(lines) + "\n"


def main() -> int:
    env = dict(os.environ)
    state_mod.clear_changed(state_mod.state_dir(env))
    code, work, text = run(env)
    write_summary(text, env)
    write_out("work", "true" if work else "false", env)
    return code


if __name__ == "__main__":
    sys.exit(main())
