"""Select source records by status observation age."""
from datetime import datetime, timezone

from .contract import parse_time


def stale_status_jobs(store, hours, *, ids=None, limit=12, include_closed=False, retry_after_seconds=60):
    if hours < 0 or limit < 1 or retry_after_seconds < 0:
        raise ValueError("Age/retry limits must be nonnegative and selection limit positive")
    instant = datetime.now(timezone.utc)
    candidates = []
    for record in store.iter_jobs(ids):
        if record["namespace"] != "boss":
            continue
        valid = record["status"].get("last_valid")
        saved_closed = (record["status"].get("saved_platform_fact") or {}).get("reported_state") == "closed"
        if not include_closed and ((valid or {}).get("outcome") == "closed" or not valid and saved_closed):
            continue
        observed = parse_time((valid or {}).get("observed_at"))
        age = (instant - observed).total_seconds() if observed else None
        if age is not None and 0 <= age < hours * 3600:
            continue
        attempted = parse_time((record["status"].get("latest_attempt") or {}).get("observed_at"))
        if attempted and 0 <= (instant - attempted).total_seconds() < retry_after_seconds:
            continue
        candidates.append((observed.timestamp() if observed else 0, record["id"], age))
    return [{"id": ident, "status_observation_age_seconds": age} for _, ident, age in sorted(candidates)[:limit]]
