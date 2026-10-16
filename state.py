"""
Comments waiting to be (re)sent, carried between runs with actions/cache
(spec 12, A3). Standard library only.

Write-ahead: the comment text is stored BEFORE the forecast is sent and
removed once the comment is confirmed. If the comment fails (or the job dies
in between), the next run re-sends the exact same text without calling an
LLM. Nothing else is persisted: failed questions are retried within one run
only (C5 cancelled cross-run failure counting).

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


def state_dir(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("STATE_DIR") or ".state")


def clear_changed(directory: Path) -> None:
    """Called once at the start of a run, right after the cache restore."""
    (directory / "changed").unlink(missing_ok=True)


@dataclass
class PendingComments:
    path: Path
    items: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, directory: Path) -> "PendingComments":
        path = directory / "state.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return cls(path=path, items=dict(data.get("pending_comments", {})))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"pending_comments": self.items}, ensure_ascii=False, indent=1),
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
