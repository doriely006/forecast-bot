"""
Comments waiting to be (re)sent, carried between runs with actions/cache
(spec 12, A3). Standard library only.

Write-ahead: the comment text is stored BEFORE the forecast is sent and
removed once the comment is confirmed. If the comment fails (or the job dies
in between), the next run re-sends the exact same text without calling an
LLM.

Also stored (v0.1.2, "C5 partial restore"): a per-question count of
consecutive runs in which no forecast came out. It exists only to stop a
question that fails every run from burning money every 20 minutes: at
MAX_QUESTION_FAILED_RUNS the question is skipped (no LLM call). It never
resends or reuses a failed forecast. 401/402 are run-wide problems and are
never counted; one success resets the question to zero.

v0.1.3: the count is write-ahead (+1 and `last_attempt` are saved BEFORE the
question is forecast, so a killed job still leaves the count); fatal/stopped/
budget-skipped and "upstream outage" runs roll the +1 back. A skipped question
gets one more try SKIP_COOLDOWN_HOURS (default 6) after its last attempt.

Every change also writes `.state/changed`, so the workflow saves a new cache
entry only when something changed (an emptied store must be saved too,
otherwise the next run would restore the older, non-empty one).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

MAX_RESEND_ATTEMPTS = 24  # about 8 hours at one run every 20 minutes
DEFAULT_MAX_QUESTION_FAILED_RUNS = 3
DEFAULT_SKIP_COOLDOWN_HOURS = 6


def state_dir(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("STATE_DIR") or ".state")


def max_failed_runs(env: dict[str, str] | None = None) -> int:
    """MAX_QUESTION_FAILED_RUNS; unset, empty, not a number or < 1 -> default 3."""
    env = os.environ if env is None else env
    try:
        value = int(str(env.get("MAX_QUESTION_FAILED_RUNS", "")).strip())
    except ValueError:
        return DEFAULT_MAX_QUESTION_FAILED_RUNS
    return value if value >= 1 else DEFAULT_MAX_QUESTION_FAILED_RUNS


def skip_cooldown_hours(env: dict[str, str] | None = None) -> int:
    """SKIP_COOLDOWN_HOURS; unset, empty, not a number or < 1 -> default 6."""
    env = os.environ if env is None else env
    try:
        value = int(str(env.get("SKIP_COOLDOWN_HOURS", "")).strip())
    except ValueError:
        return DEFAULT_SKIP_COOLDOWN_HOURS
    return value if value >= 1 else DEFAULT_SKIP_COOLDOWN_HOURS


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value: Any) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def question_key(question_id: Any, post_id: Any) -> str:
    """Same key in precheck (QItem) and runner (SDK question)."""
    return str(question_id) if question_id is not None else f"p{post_id}"


def clear_changed(directory: Path) -> None:
    """Called once at the start of a run, right after the cache restore."""
    (directory / "changed").unlink(missing_ok=True)


@dataclass
class PendingComments:
    path: Path
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    failures: dict[str, dict[str, Any]] = field(default_factory=dict)
    # v0.2.0: locally tracked Anthropic spend for the current credit period (the API exposes no balance)
    spend: dict[str, Any] = field(default_factory=dict)
    found: bool = True  # False when state.json did not exist / could not be read at load time
    # this run's write-ahead +1s: key -> the entry as it was before (None = no entry)
    _wa: dict[str, dict[str, Any] | None] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, directory: Path) -> "PendingComments":
        path = directory / "state.json"
        found = True
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data, found = {}, False
        return cls(
            path=path,
            items=dict(data.get("pending_comments", {})),
            failures=dict(data.get("question_failures", {})),
            spend=dict(data.get("anthropic_spend", {})),
            found=found,
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"pending_comments": self.items, "question_failures": self.failures,
                 "anthropic_spend": self.spend},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        tmp.replace(self.path)
        (self.path.parent / "changed").write_text("1", encoding="utf-8")

    def add(self, post_id: int, text: str, url: str) -> None:
        self.items[str(post_id)] = {
            "text": text,
            "url": url,
            "since": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.save()

    def remove(self, post_id: int | str) -> None:
        if self.items.pop(str(post_id), None) is not None:
            self.save()

    def mark_attempt(self, post_id: int | str, error: str) -> None:
        item = self.items.get(str(post_id))
        if item is not None:
            item["attempts"] = int(item.get("attempts", 0)) + 1
            item["last_error"] = error[:200]
            self.save()

    def give_up(self) -> list[dict[str, Any]]:
        """Drops comments that failed MAX_RESEND_ATTEMPTS times; returns them."""
        dead = [k for k, v in self.items.items() if int(v.get("attempts", 0)) >= MAX_RESEND_ATTEMPTS]
        out = [dict(self.items.pop(k), post_id=k) for k in dead]
        if out:
            self.save()
        return out

    # --- per-question failure counting (v0.1.2) ---

    def failed_runs(self, key: str) -> int:
        return int(self.failures.get(key, {}).get("count", 0))

    def record_failure(self, key: str, title: str, url: str, error: str, now: datetime | None = None) -> int:
        """Counts a failure NOW (no write-ahead). The runner uses begin_attempt instead."""
        item = self.failures.setdefault(key, {})
        item["count"] = int(item.get("count", 0)) + 1
        self._fill(item, title, url, error, now)
        self.save()
        return item["count"]

    @staticmethod
    def _fill(item: dict[str, Any], title: str, url: str, error: str, now: datetime | None) -> None:
        item["title"] = title[:80]
        item["url"] = url
        item["last_error"] = " ".join(error.split())[:200]
        item["last_attempt"] = _stamp(now or _now())

    def begin_attempt(self, key: str, title: str, url: str, now: datetime | None = None) -> None:
        """Write-ahead: +1 and the attempt time hit the disk before the question is forecast."""
        if key in self._wa:
            return
        prev = self.failures.get(key)
        self._wa[key] = dict(prev) if prev is not None else None
        item = self.failures.setdefault(key, {})
        item["count"] = int(item.get("count", 0)) + 1
        self._fill(item, title, url, item.get("last_error", "attempt started, no result recorded"), now)
        self.save()

    def finish_failure(self, key: str, error: str, now: datetime | None = None) -> None:
        """The attempt failed for good: keep the write-ahead +1, record why."""
        item = self.failures.get(key)
        if item is not None:
            item["last_error"] = " ".join(error.split())[:200]
            item["last_attempt"] = _stamp(now or _now())
            self.save()

    def rollback(self, key: str) -> None:
        """Takes back this run's write-ahead +1 (fatal 401/402, stopped, no budget)."""
        if key not in self._wa:
            return
        prev = self._wa.pop(key)
        if prev is None:
            self.failures.pop(key, None)
        else:
            self.failures[key] = prev
        self.save()

    def rollback_all(self) -> int:
        """Upstream-outage run: take back every +1 this run made."""
        keys = list(self._wa)
        for k in keys:
            self.rollback(k)
        return len(keys)

    def clear_failure(self, key: str) -> None:
        self._wa.pop(key, None)
        if self.failures.pop(key, None) is not None:
            self.save()

    def skipped(self, limit: int) -> dict[str, dict[str, Any]]:
        """Questions that failed `limit` runs in a row."""
        return {k: v for k, v in self.failures.items() if int(v.get("count", 0)) >= limit}

    def split_skipped(
        self, limit: int, cooldown_hours: int, now: datetime | None = None
    ) -> tuple[dict[str, dict[str, Any]], set[str]]:
        """-> (still cooling: key -> entry plus `next_retry` UTC text, keys whose cooldown is over).

        An entry without a readable `last_attempt` (written before v0.1.3) is due at once."""
        now = now or _now()
        waiting: dict[str, dict[str, Any]] = {}
        due: set[str] = set()
        for k, v in self.skipped(limit).items():
            last = _parse(v.get("last_attempt"))
            retry = last + timedelta(hours=cooldown_hours) if last else None
            if retry is None or retry <= now:
                due.add(k)
            else:
                waiting[k] = dict(v, next_retry=retry.strftime("%Y-%m-%d %H:%M"))
        return waiting, due

    def prune_failures(self, keep: set[str]) -> None:
        """Forget counts of questions that are no longer waiting for a forecast."""
        gone = [k for k in self.failures if k not in keep]
        for k in gone:
            del self.failures[k]
        if gone:
            self.save()

    # --- Anthropic spend per credit period (v0.2.0) ---

    def period_spent(self, period_start: str) -> float:
        """Spend recorded for the credit period starting `period_start` (ISO date); another period -> 0."""
        if self.spend.get("period_start") != period_start:
            return 0.0
        return float(self.spend.get("spent", 0.0))

    def add_spend(self, period_start: str, usd: float, now: datetime | None = None) -> float:
        """Adds one call's cost and saves at once (a killed job keeps what it spent). New period -> starts at 0."""
        if self.spend.get("period_start") != period_start:
            self.spend = {"period_start": period_start, "spent": 0.0, "calls": 0}
        self.spend["spent"] = round(float(self.spend.get("spent", 0.0)) + max(0.0, usd), 6)
        self.spend["calls"] = int(self.spend.get("calls", 0)) + 1
        self.spend["updated"] = _stamp(now or _now())
        self.save()
        return self.spend["spent"]
