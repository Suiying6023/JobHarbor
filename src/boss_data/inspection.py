"""Read-only health, statistics, and source-level duplicate inspection."""
from __future__ import annotations

import importlib.metadata
import json
import socket
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .contract import now, parse_time
from .store import SourceStore


def doctor(settings):
    output = {"observed_at": now(), "boss_requests": 0, "database_exists": settings.database.exists(), "routes": {}}
    if settings.database.exists():
        with SourceStore(settings.database) as store:
            output["source"] = store.summary()
            latest = [dict(r) for r in store.conn.execute("SELECT started_at,outcome,kind,egress,http_status,business_code FROM requests WHERE coalesce(json_extract(evidence_json,'$.imported'),0)=0 AND (origin_key IS NULL OR origin_key LIKE 'mobile-wire/%') ORDER BY started_at DESC LIMIT 20")]
    else:
        latest = []
    for name, value in (("mobile", settings.mobile), ("anonymous", settings.anonymous)):
        proxy = urlsplit(value.get("proxy") or "")
        local_listener = None
        if proxy.hostname and proxy.port:
            try:
                with socket.create_connection((proxy.hostname, proxy.port), timeout=1):
                    local_listener = True
            except OSError:
                local_listener = False
        route = {"configured": bool(value), "egress": value.get("egress_label"), "local_proxy_listener": local_listener,
                 "listener_proves_business_access": False}
        if name == "mobile":
            path = Path(value["session_file"]) if value.get("session_file") else None
            route["session_file_present"] = bool(path and path.is_file())
            if route["session_file_present"]:
                session = json.loads(path.read_text(encoding="utf-8-sig"))
                required = ("t2", "secretKey", "client_info", "curidentity", "v", "app_id", "uniqid", "user_agent")
                route["required_session_fields_present"] = all(bool(session.get(key)) for key in required)
        else:
            route["profile_directory_present"] = bool(value.get("profile") and Path(value["profile"]).is_dir())
            try:
                route["driver_version"] = importlib.metadata.version(value.get("driver", "patchright"))
            except importlib.metadata.PackageNotFoundError:
                route["driver_version"] = None
        route["cached_last_results"] = [r for r in latest if r["egress"] == value.get("egress_label")][:3]
        output["routes"][name] = route
    return output


def request_stats(store, *, task_id=None, minutes=None):
    clauses, params = [], []
    if task_id:
        clauses.append("task_id=?")
        params.append(task_id)
    if minutes is not None:
        clauses.append("julianday(started_at)>julianday(?)")
        params.append((datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat())
    rows = [dict(r) for r in store.conn.execute("SELECT * FROM requests" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY started_at,id", params)]
    starts = [parse_time(r["started_at"]) for r in rows if parse_time(r["started_at"])]
    gaps = [(b - a).total_seconds() for a, b in zip(starts, starts[1:])]
    imported = lambda r: json.loads(r["evidence_json"]).get("imported") is True or bool(r["origin_key"] and not r["origin_key"].startswith("mobile-wire/"))
    channels = {}
    for channel in ("mobile", "anonymous"):
        selected = [r for r in rows if not imported(r) and (r["kind"] == "anonymous_status") == (channel == "anonymous")]
        dates = [parse_time(r["started_at"]) for r in selected if parse_time(r["started_at"])]
        channel_gaps = [(right-left).total_seconds() for left,right in zip(dates,dates[1:])]
        channels[channel] = {"attempts": len(selected),
                             "successes": sum(r["outcome"] in ("ok","open","closed") and r["http_status"] == 200 for r in selected),
                             "minimum_start_gap_seconds": min(channel_gaps) if channel_gaps else None,
                             "maximum_start_gap_seconds": max(channel_gaps) if channel_gaps else None}
    return {"observed_at": now(), "request_attempts": len(rows), "historical_imported_requests": sum(imported(r) for r in rows),
            "channels": channels,
            "successfully_observed_requests": sum(r["outcome"] in ("ok", "open", "closed") and r["http_status"] == 200 for r in rows),
            "outcomes": dict(Counter(r["outcome"] for r in rows)), "egresses": dict(Counter(r["egress"] or "unknown" for r in rows)),
            "minimum_start_gap_seconds": min(gaps) if gaps else None, "maximum_start_gap_seconds": max(gaps) if gaps else None,
            "starts": [{"started_at": r["started_at"], "kind": r["kind"], "outcome": r["outcome"], "egress": r["egress"],
                        "http_status": r["http_status"], "business_code": r["business_code"]} for r in rows[-30:]],
            "rate_claim": "Successful requests and body acquisitions are separate measures; a bounded sample is not a sustained rate."}


def duplicate_bodies(store):
    from .adapters.normalization import normalized_description
    groups = {}
    for record in store.iter_jobs():
        company = " ".join((record["metadata"].get("company_name") or "").split())
        text = record["body"]["text"]
        if company and text:
            groups.setdefault((company, normalized_description(text)), []).append(record)
    return {"basis": "Exact company name after whitespace normalization, and NFKC/whitespace-equivalent stored text; identity and acquisition times remain per job.",
            "groups": [{"company": company, "body_characters": len(body), "ids": [r["id"] for r in records],
                        "fetched_at": [r["body"]["fetched_at"] for r in records],
                        "completeness": [r["body"]["completeness"] for r in records]}
                       for (company, body), records in groups.items() if len(records) > 1]}
