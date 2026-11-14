"""JSON CLI for the source database and bounded data requests."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .config import Settings
from .store import SourceStore


def build_parser():
    parser = argparse.ArgumentParser(prog="boss-data", description="JobHarbor independent BOSS source-data tool")
    parser.add_argument("--config", help="Explicit YAML configuration; paths resolve against its directory")
    parser.add_argument("--database", help="Tool-owned source SQLite (never the mixed legacy database)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create an independent source database")
    sub.add_parser("doctor", help="Read local state and cached route outcomes; sends no BOSS requests")
    status = sub.add_parser("status", help="Source database, request counts, and task progress")
    status.add_argument("--id")
    status.add_argument("--task")
    status.add_argument("--minutes", type=int)
    listing = sub.add_parser("list", help="Read the source database without network requests")
    listing.add_argument("--ids", nargs="*")
    listing.add_argument("--status", choices=("open", "closed", "unknown"))
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--with-body", action="store_true")
    legacy = sub.add_parser("import-legacy", help="Extract source facts from the intact mixed database using mode=ro")
    legacy.add_argument("--source", required=True)
    legacy.add_argument("--ids", nargs="*")
    legacy.add_argument("--limit", type=int, default=0)
    legacy.add_argument("--mobile-directory", help="Explicit private mobile request/context archive, excluding the account session")
    private = sub.add_parser("import-private", help="Import private mobile request/context history without importing credentials")
    private.add_argument("--mobile-directory", required=True)
    ownership = sub.add_parser("ownership", help="Inspect legacy table/field ownership without modifying it")
    ownership.add_argument("--source", required=True)
    exp = sub.add_parser("export", help="Export manifest.json + jobs.jsonl, excluding private contexts and analysis")
    exp.add_argument("--out", required=True)
    exp.add_argument("--legacy-source")
    exp.add_argument("--ids", nargs="*")
    exp.add_argument("--limit", type=int, default=0)
    imp = sub.add_parser("import", help="Import a portable source contract without assigning new acquisition times")
    imp.add_argument("--manifest", required=True)
    database = sub.add_parser("database", help="Source database maintenance")
    dbsub = database.add_subparsers(dest="db_action", required=True)
    dbsub.add_parser("inspect")
    dbsub.add_parser("duplicates")
    backup = dbsub.add_parser("backup", help="Consistent SQLite backup to a new file")
    backup.add_argument("--out", required=True)
    changes = sub.add_parser("changes", help="Read the source change stream without advancing a consumer cursor")
    changes.add_argument("--after", type=int, default=0)
    changes.add_argument("--consumer")
    changes.add_argument("--limit", type=int, default=100)
    ack = sub.add_parser("ack", help="Advance an explicit consumer cursor")
    ack.add_argument("--consumer", required=True)
    ack.add_argument("--through", required=True, type=int)
    search = sub.add_parser("search", help="One authenticated HTTP search/list/page request")
    search.add_argument("--query", required=True)
    search.add_argument("--city", default="100010000")
    search.add_argument("--page", type=int, default=1)
    search.add_argument("--sort", type=int, default=-1)
    search.add_argument("--filter", action="append", default=[], metavar="K=V")
    detail = sub.add_parser("detail", help="Authenticated complete detail using saved source context")
    identity = detail.add_mutually_exclusive_group(required=True)
    identity.add_argument("--id")
    identity.add_argument("--mobile-id")
    detail.add_argument("--max-body-age-hours", type=float)
    detail.add_argument("--force", action="store_true")
    refresh = sub.add_parser("refresh", help="Bounded anonymous status or authenticated detail refresh")
    refresh.add_argument("--ids", nargs="+")
    refresh.add_argument("--mode", choices=("anonymous", "authenticated"), default="anonymous")
    refresh.add_argument("--max-requests", type=int, default=12)
    refresh.add_argument("--stale-hours", type=float)
    refresh.add_argument("--limit", type=int, default=12)
    refresh.add_argument("--include-closed", action="store_true")
    refresh.add_argument("--retry-after-seconds", type=float, default=60)
    refresh.add_argument("--repeat", action="store_true")
    refresh.add_argument("--max-seconds", type=float)
    selection = sub.add_parser("select-refresh", help="Select by source status age; no network or review dependency")
    selection.add_argument("--stale-hours", required=True, type=float)
    selection.add_argument("--ids", nargs="+")
    selection.add_argument("--limit", type=int, default=12)
    selection.add_argument("--include-closed", action="store_true")
    selection.add_argument("--retry-after-seconds", type=float, default=60)
    collect = sub.add_parser("collect", help="Bounded resumable query/page/detail collection without analysis gates")
    collect.add_argument("--query", action="append", required=True)
    collect.add_argument("--city", default="100010000")
    collect.add_argument("--pages", type=int, default=3)
    collect.add_argument("--details-per-page", type=int, default=1)
    collect.add_argument("--max-requests", type=int, required=True)
    collect.add_argument("--max-body-age-hours", type=float)
    collect.add_argument("--details-mode", choices=("missing", "refresh"), default="missing")
    collect.add_argument("--repeat", action="store_true")
    collect.add_argument("--max-seconds", type=float)
    loop = sub.add_parser("loop", help="Explicit autonomous repeat, until a persisted request/time limit or cancellation")
    loop.add_argument("--mode", choices=("collect", "anonymous", "authenticated"), default="collect")
    loop.add_argument("--query", action="append")
    loop.add_argument("--city", default="100010000")
    loop.add_argument("--pages", type=int, default=3)
    loop.add_argument("--details-per-page", type=int, default=1)
    loop.add_argument("--details-mode", choices=("missing", "refresh"), default="missing")
    loop.add_argument("--max-body-age-hours", type=float)
    loop.add_argument("--ids", nargs="+")
    loop.add_argument("--stale-hours", type=float)
    loop.add_argument("--limit", type=int, default=12)
    loop.add_argument("--include-closed", action="store_true")
    loop.add_argument("--max-requests", type=int)
    loop.add_argument("--max-seconds", type=float)
    loop.add_argument("--until-cancel", action="store_true")
    tasks = sub.add_parser("tasks", help="Show, cancel, or explicitly resume durable bounded tasks")
    tsub = tasks.add_subparsers(dest="task_action", required=True)
    tsub.add_parser("list")
    for action in ("show", "cancel", "run"):
        command = tsub.add_parser(action)
        command.add_argument("task_id")
        if action == "run":
            command.add_argument("--max-requests", type=int, default=12)
            command.add_argument("--resume", action="store_true", help="Resume a stopped/attention task after its cause is handled")
            command.add_argument("--extend-max-requests", type=int, help="Explicitly replace a loop's total request limit")
            command.add_argument("--extend-max-seconds", type=float, help="Explicitly replace its wall duration limit from first start")
    replay = sub.add_parser("replay", help="Replay an actual archived response without BOSS requests")
    replay.add_argument("--file", required=True)
    replay.add_argument("--kind", choices=("detail", "search", "page"), required=True)
    replay.add_argument("--observed-at")
    replay.add_argument("--url")
    replay.add_argument("--title")
    probe = sub.add_parser("probe", help="Explicit one-operation live probe; doctor never probes automatically")
    probe.add_argument("--route", choices=("mobile", "anonymous"), required=True)
    probe.add_argument("--id")
    probe.add_argument("--query", default="Python后端")
    probe.add_argument("--city", default="100010000")
    return parser


def execute(args):
    settings = Settings.load(args.config, args.database)
    if args.command == "doctor":
        from .inspection import doctor
        return doctor(settings)
    if args.command == "ownership":
        from .legacy import LegacySource
        with LegacySource(args.source) as legacy:
            return legacy.ownership()
    if args.command == "export":
        from .exporting import export_bundle, store_records
        if args.legacy_source:
            from .legacy import LegacySource
            with LegacySource(args.legacy_source) as legacy:
                return export_bundle(legacy.records(args.ids, args.limit), args.out, source_name=legacy.path.name)
        with SourceStore(settings.database) as store:
            source_id = store.conn.execute("SELECT value FROM source_meta WHERE key='store_id'").fetchone()[0]
            return export_bundle(store_records(store, args.ids, args.limit), args.out, source_name=store.path.name, source_store_id=source_id)
    network = args.command in ("search", "detail", "refresh", "collect", "loop", "probe", "replay") or args.command == "tasks" and args.task_action == "run"
    if network:
        from .client import BossCli
        client = BossCli(settings=settings, progress=lambda event: print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True))
        if args.command == "search":
            return client.search(args.query, args.city, args.page, args.sort, dict(item.split("=", 1) for item in args.filter))
        if args.command == "detail":
            return client.detail(canonical_id=args.id, mobile_id=args.mobile_id, max_body_age_hours=args.max_body_age_hours, force=args.force)
        if args.command == "refresh":
            return client.refresh(args.ids, mode=args.mode, max_requests=args.max_requests, repeat=args.repeat,
                                  max_seconds=args.max_seconds, stale_hours=args.stale_hours, limit=args.limit,
                                  include_closed=args.include_closed, retry_after_seconds=args.retry_after_seconds)
        if args.command == "collect":
            return client.collect(args.query, args.city, args.pages, args.details_per_page, args.max_requests, args.max_body_age_hours,
                                  args.details_mode, repeat=args.repeat, max_seconds=args.max_seconds)
        if args.command == "loop":
            if args.max_requests is None and args.max_seconds is None and not args.until_cancel:
                raise ValueError("Loop needs a request/time limit or explicit --until-cancel")
            if args.mode == "collect":
                if not args.query:
                    raise ValueError("Collection loop needs an explicit query")
                return client.collect(args.query, args.city, args.pages, args.details_per_page, args.max_requests,
                                      args.max_body_age_hours, args.details_mode, repeat=True, max_seconds=args.max_seconds)
            return client.refresh(args.ids, mode=args.mode, max_requests=args.max_requests, repeat=True, max_seconds=args.max_seconds,
                                  stale_hours=args.stale_hours, limit=args.limit, include_closed=args.include_closed)
        if args.command == "probe":
            if args.route == "anonymous":
                if not args.id:
                    raise ValueError("Anonymous probe needs a stored source ID")
                return client.refresh([args.id], max_requests=1)
            return client.search(args.query, args.city)
        if args.command == "replay":
            return client.replay(args.file, args.kind, observed_at=args.observed_at, url=args.url, title=args.title)
        return client.run_task(args.task_id, args.max_requests, resume=args.resume,
                               extend_max_requests=args.extend_max_requests, extend_max_seconds=args.extend_max_seconds)
    writable = args.command in ("init", "import-legacy", "import-private", "import", "ack") or args.command == "tasks" and args.task_action == "cancel"
    with SourceStore(settings.database, writable=writable) as store:
        if args.command == "init":
            return store.summary()
        if args.command == "import-legacy":
            from .legacy import LegacySource
            with LegacySource(args.source) as legacy:
                result = legacy.import_into(store, ids=args.ids, limit=args.limit)
            if args.mobile_directory:
                from .legacy import import_mobile_archive
                result["private_archive"] = import_mobile_archive(store, args.mobile_directory)
                result["after"] = store.summary()
            return result
        if args.command == "import-private":
            from .legacy import import_mobile_archive
            return import_mobile_archive(store, args.mobile_directory)
        if args.command == "import":
            from .exporting import import_bundle
            return import_bundle(store, args.manifest)
        if args.command == "status":
            from .inspection import request_stats
            return {"source": store.summary(), "job": store.get(args.id) if args.id else None,
                    "task": store.task(args.task) if args.task else None,
                    "requests": request_stats(store, task_id=args.task, minutes=args.minutes)}
        if args.command == "list":
            records = list(store.iter_jobs(args.ids, status=args.status, limit=args.limit))
            if not args.with_body:
                for record in records:
                    record["body"]["characters"] = len(record["body"].pop("text"))
            return {"jobs": records, "count": len(records), "boss_requests": 0}
        if args.command == "select-refresh":
            from .selection import stale_status_jobs
            return {"jobs": stale_status_jobs(store, args.stale_hours, ids=args.ids, limit=args.limit,
                        include_closed=args.include_closed, retry_after_seconds=args.retry_after_seconds), "boss_requests": 0}
        if args.command == "database":
            if args.db_action == "duplicates":
                from .inspection import duplicate_bodies
                return duplicate_bodies(store)
            if args.db_action == "backup":
                target = Path(args.out).resolve()
                if target.exists():
                    raise FileExistsError("Backup destination already exists")
                target.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(target) as destination:
                    store.conn.backup(destination)
                return {"backup": str(target), "source": str(store.path), "boss_requests": 0}
            return store.summary()
        if args.command == "changes":
            cursor = store.cursor(args.consumer) if args.consumer else args.after
            return {"after": cursor, "changes": store.changes(cursor, args.limit), "cursor_advanced": False}
        if args.command == "ack":
            store.acknowledge(args.consumer, args.through)
            return {"consumer": args.consumer, "sequence": store.cursor(args.consumer)}
        if args.command == "tasks":
            if args.task_action == "list":
                return {"tasks": store.tasks()}
            state = store.task(args.task_id)
            if not state:
                raise ValueError("Unknown task ID")
            if args.task_action == "cancel":
                state["cancel_requested"] = True
                if state["status"] != "running":
                    state["status"] = "stopped"
                store.save_task(state)
            return state
    raise ValueError("Unsupported command")


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        result = execute(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        outcome = result.get("outcome") or result.get("status")
        if outcome in ("attention_required", "verification_required", "account_restricted", "login_expired", "restricted", "rate_limited", "transport_error", "session_or_environment_unknown"):
            return 3
        return 2 if outcome in ("failed", "completed_with_unknowns", "context_missing", "identity_mismatch", "platform_error", "http_error", "decode_error") else 0
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
