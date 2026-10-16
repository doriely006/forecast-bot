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

Every change also writes `.state/changed`, so the workflow saves a new cache
entry only when something changed (an emptied store must be saved too,
otherwise the next run would restore the older, non-empty one).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_RESEND_ATTEMPTS = 24  # about 8 hours at one run every 20 minutes
DEFAULT_MAX_QUESTION_FAILED_RUNS = 3


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

    @classmethod
    def load(cls, directory: Path) -> "PendingComments":
        path = directory / "state.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return cls(
            path=path,
            items=dict(data.get("pending_comments", {})),
            failures=dict(data.get("question_failures", {})),
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"pending_comments": self.items, "question_failures": self.failures},
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

    def record_failure(self, key: str, title: str, url: str, error: str) -> int:
        item = self.failures.setdefault(key, {})
        item["count"] = int(item.get("count", 0)) + 1
        item["title"] = title[:80]
        item["url"] = url
        item["last_error"] = " ".join(error.split())[:200]
        self.save()
        return item["count"]

    def clear_failure(self, key: str) -> None:
        if self.failures.pop(key, None) is not None:
            self.save()

    def skipped(self, limit: int) -> dict[str, dict[str, Any]]:
        """Questions that failed `limit` runs in a row."""
        return {k: v for k, v in self.failures.items() if int(v.get("count", 0)) >= limit}

    def prune_failures(self, keep: set[str]) -> None:
        """Forget counts of questions that are no longer waiting for a forecast."""
        gone = [k for k in self.failures if k not in keep]
        for k in gone:
            del self.failures[k]
        if gone:
            self.save()
