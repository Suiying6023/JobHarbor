"""Serialize mobile requests and enforce a shared start interval."""
from contextlib import closing, contextmanager
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import time


@contextmanager
def request_slot(directory: Path, interval_seconds: float):
    queued_at = time.monotonic()
    with closing(sqlite3.connect(directory / "request-queue.sqlite3", timeout=300)) as queue:
        queue.execute("CREATE TABLE IF NOT EXISTS request_clock (id INTEGER PRIMARY KEY, started_at REAL NOT NULL)")
        queue.execute("BEGIN IMMEDIATE")
        previous = queue.execute("SELECT started_at FROM request_clock WHERE id=1").fetchone()
        last_started = previous[0] if previous else 0
        if previous is None:
            log = directory / "requests.jsonl"
            if log.exists():
                lines = log.read_text(encoding="utf-8").strip().split("\n")
                if lines:
                    last_started = datetime.fromisoformat(json.loads(lines[-1])["observed_at"]).timestamp()
        waited_for_queue = time.monotonic() - queued_at
        delay = max(0, interval_seconds - (time.time() - last_started))
        if delay:
            time.sleep(delay)
        pacing = {"interval_seconds": interval_seconds, "queue_wait_seconds": round(waited_for_queue, 3),
                  "interval_wait_seconds": round(delay, 3)}

        def mark_started(observed_at):
            queue.execute("INSERT INTO request_clock VALUES (1,?) ON CONFLICT(id) DO UPDATE SET started_at=excluded.started_at",
                          (datetime.fromisoformat(observed_at).timestamp(),))

        try:
            yield pacing, mark_started
        finally:
            queue.commit()
