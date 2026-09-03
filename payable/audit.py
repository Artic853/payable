"""Append-only decision log.

Every step either side takes -- the agent's reasoning, the merchant's checks, the
gateway's verdict -- lands here keyed by run_id, so any purchase can be replayed
after the fact and asked "why did you buy that?".

Backend is Redis Streams when REDIS_URL is set (that is the shape you want in
production: append-only, consumer groups, trimming) and a JSONL file otherwise,
so the demo has no infrastructure prerequisite. Both satisfy the same interface.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import SETTINGS
from .models import AuditEvent

STREAM_KEY = "payable:audit"


class AuditLog:
    def __init__(self, redis_url: str = "", jsonl_path: Path | None = None):
        self.jsonl_path = jsonl_path or SETTINGS.audit_jsonl_path
        self._lock = threading.Lock()
        self._redis = None
        self.backend = "jsonl"

        if redis_url:
            try:
                import redis  # type: ignore

                client = redis.Redis.from_url(redis_url, decode_responses=True)
                client.ping()
                self._redis = client
                self.backend = "redis-streams"
            except Exception:
                # Fall through to JSONL rather than taking the demo down.
                self._redis = None
                self.backend = "jsonl (redis unreachable)"

        if self._redis is None:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            self.jsonl_path.touch(exist_ok=True)

    # -- writing ---------------------------------------------------------

    def record(
        self,
        run_id: str,
        actor: str,
        step: str,
        decision: str = "",
        rationale: str = "",
        payload: dict[str, Any] | None = None,
        latency_ms: float = 0.0,
    ) -> AuditEvent:
        event = AuditEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            ts=time.time(),
            actor=actor,  # type: ignore[arg-type]
            step=step,
            decision=decision,
            rationale=rationale,
            payload=payload or {},
            latency_ms=latency_ms,
        )
        self._append(event)
        return event

    def _append(self, event: AuditEvent) -> None:
        row = event.model_dump()
        if self._redis is not None:
            self._redis.xadd(
                STREAM_KEY,
                {"run_id": event.run_id, "json": json.dumps(row, default=str)},
                maxlen=100_000,
                approximate=True,
            )
            return
        with self._lock:
            with self.jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")

    # -- reading ---------------------------------------------------------

    def events(self, run_id: str | None = None, limit: int = 500) -> list[AuditEvent]:
        rows: list[dict] = []
        if self._redis is not None:
            for _id, fields in self._redis.xrevrange(STREAM_KEY, count=limit * 4):
                try:
                    rows.append(json.loads(fields["json"]))
                except Exception:
                    continue
            rows.reverse()
        else:
            if not self.jsonl_path.exists():
                return []
            with self.jsonl_path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        if run_id:
            rows = [r for r in rows if r.get("run_id") == run_id]
        return [AuditEvent(**r) for r in rows[-limit:]]

    def runs(self, limit: int = 50) -> list[dict]:
        """Summarize recent runs, newest first."""
        by_run: dict[str, dict] = {}
        for ev in self.events(limit=5000):
            slot = by_run.setdefault(
                ev.run_id,
                {"run_id": ev.run_id, "started_at": ev.ts, "steps": 0, "outcome": "in_progress"},
            )
            slot["steps"] += 1
            slot["ended_at"] = ev.ts
            if ev.step == "run_complete":
                slot["outcome"] = ev.decision
                slot["intent"] = ev.payload.get("intent", "")
        ordered = sorted(by_run.values(), key=lambda r: r.get("started_at", 0), reverse=True)
        return ordered[:limit]

    def clear(self) -> None:
        if self._redis is not None:
            self._redis.delete(STREAM_KEY)
        elif self.jsonl_path.exists():
            self.jsonl_path.write_text("", encoding="utf-8")


AUDIT = AuditLog(redis_url=SETTINGS.redis_url)
