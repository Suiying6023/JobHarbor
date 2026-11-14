"""Read-only import of legacy source databases."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .contract import activity_signals, load_json, private_source_raw, public_evidence, record_from_row
from .store import SourceStore, encode


class LegacySource:
    def __init__(self, database):
        self.path = Path(database).resolve()
        self.conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        self.tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "jobs" not in self.tables:
            raise ValueError("Legacy input has no jobs table")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.conn.close()

    def ownership(self):
        from .contract import PUBLIC_JOB_FIELDS
        source_fields = {"canonical_id", "platform", "source_job_id", "job_url", "job_description", "raw_json",
                         "detail_fetched_at", "description_updated_at", "first_seen_at", "last_seen_at", "collected_at",
                         "skills_json", "labels_json", "welfare_json", "matched_queries_json", "company_reg_json",
                         "recruiter_active_status", "recruiter_active_days", *PUBLIC_JOB_FIELDS}
        columns = [r[1] for r in self.conn.execute("PRAGMA table_info(jobs)")]
        counts = {table: self.conn.execute("SELECT count(*) FROM " + table).fetchone()[0] for table in sorted(self.tables) if table != "sqlite_sequence"}
        return {"source_database": str(self.path), "connection": "mode=ro", "counts": counts,
                "jobs_source_fields": [c for c in columns if c in source_fields],
                "jobs_product_or_derived_fields_not_imported": [c for c in columns if c not in source_fields],
                "source_tables_imported": [t for t in ("jobs", "mobile_cards", "job_status_checks", "collection_observations", "request_log") if t in self.tables],
                "source_execution_history_kept_in_legacy": [t for t in ("collection_runs", "collection_rounds", "collection_queries", "keyword_progress") if t in self.tables],
                "product_tables_not_imported": [t for t in ("agent_assessments", "job_annotations", "annotation_prompts", "chat_actions", "cross_platform_groups") if t in self.tables]}

    def rows(self, ids=None, limit=0):
        parameters = list(ids or [])
        where = " WHERE canonical_id IN (" + ",".join("?" for _ in parameters) + ")" if parameters else ""
        sql = "SELECT * FROM jobs" + where + " ORDER BY canonical_id"
        if limit:
            sql += " LIMIT ?"
            parameters.append(limit)
        yield from self.conn.execute(sql, parameters)

    def records(self, ids=None, limit=0):
        for row in self.rows(ids, limit):
            record = record_from_row(row)
            if "job_status_checks" in self.tables:
                history = [dict(r) for r in self.conn.execute("SELECT * FROM job_status_checks WHERE canonical_id=? ORDER BY checked_at,id", (record["id"],))]
                attempts = [{"outcome": r["outcome"], "observed_at": r["checked_at"],
                             "evidence": public_evidence(load_json(r["evidence_json"], {})), "observation_id": r["id"]} for r in history]
                record["status"]["history"] = attempts
                if attempts:
                    record["status"]["latest_attempt"] = attempts[-1]
                    valid = [a for a in attempts if a["outcome"] in ("open", "closed")]
                    record["status"]["last_valid"] = valid[-1] if valid else None
            yield record

    def import_into(self, store: SourceStore, *, ids=None, limit=0):
        if store.path == self.path:
            raise ValueError("Legacy input and source destination must be different")
        before = store.summary()
        selected = set()
        missing_fetch, missing_changed = 0, 0
        for row in self.rows(ids, limit):
            record = record_from_row(row)
            selected.add(record["id"])
            missing_fetch += not bool(row["detail_fetched_at"])
            missing_changed += not bool(row["description_updated_at"])
            store.put(record)
            raw = load_json(row["raw_json"], {})
            facts = private_source_raw(raw)
            store.private(f"{self.path.as_uri()}#source-raw/{record['id']}/{record['body']['version_id']}/{record['body']['fetched_at']}",
                          record["id"], record["body"]["fetched_at"], "legacy_source_raw", facts)
            for number, signal in enumerate(activity_signals(raw, row["detail_fetched_at"])):
                store.observe_activity(record["id"], signal["text"], signal["active_days"], signal["observed_at"], signal["source"],
                    origin_key=f"{self.path.as_uri()}#job-activity/{record['id']}/{number}")
            if record["body"]["text"]:
                store.conn.execute("INSERT OR IGNORE INTO body_acquisitions(canonical_id,version_id,fetched_at,outcome,source_kind,origin_key) VALUES (?,?,?,?,?,?)",
                    (record["id"], record["body"]["version_id"], record["body"]["fetched_at"], "imported_current_snapshot", record["body"]["evidence"].get("kind"),
                     f"{self.path.as_uri()}#current-body/{record['id']}"))
        orphan_cards = 0
        if "mobile_cards" in self.tables:
            from .adapters.mobile_payload import _card
            for row in self.conn.execute("SELECT * FROM mobile_cards ORDER BY last_seen_at"):
                original = row["canonical_id"]
                actual = original if original in selected or store.get(original) else store.resolve("boss_mobile", row["mobile_job_id"])
                orphan = bool(original and not actual)
                if ids or limit:
                    if not actual:
                        continue
                private = load_json(row["card_json"], {})
                public_fields = ("job_id", "title", "company", "city", "district", "business_district", "salary", "experience", "education",
                                 "company_size", "financing_stage", "industry", "labels", "summary", "recruiter_title", "recruiter_online",
                                 "recruiter_active_status", "position_category")
                public = _card(private) if "jobId" in private else {k: v for k, v in private.items() if k in public_fields}
                public.update(canonical_id=actual, has_description=bool((store.get(actual) or {}).get("body", {}).get("text")))
                from .adapters.facts import active_days
                activity_text = "在线" if public.get("recruiter_online") is True else public.get("recruiter_active_status") or ""
                activity_source = "android_api_list_online" if public.get("recruiter_online") is True else "android_api_list"
                if activity_text:
                    public["activity_observation"] = {"text": activity_text, "active_days": active_days(activity_text),
                                                       "observed_at": row["last_seen_at"], "source": activity_source}
                    if actual:
                        store.observe_activity(actual, activity_text, active_days(activity_text), row["last_seen_at"], activity_source,
                            origin_key=f"{self.path.as_uri()}#card-activity/{row['mobile_job_id']}/{row['last_seen_at']}")
                if orphan:
                    orphan_cards += 1
                    private = {**private, "_legacy_unresolved_canonical_id": original}
                    public["mapping_state"] = "missing_source_job"
                store.put_card(row["mobile_job_id"], public, private, row["query"], row["page"], row["last_seen_at"], actual,
                               first_seen_at=row["first_seen_at"])
        if "job_status_checks" in self.tables:
            for row in self.conn.execute("SELECT * FROM job_status_checks ORDER BY checked_at,id"):
                if row["canonical_id"] not in selected:
                    continue
                evidence = load_json(row["evidence_json"], {})
                store.observe_status(row["canonical_id"], row["outcome"], row["checked_at"], evidence, task_id=row["task_id"],
                    origin_key=f"{self.path.as_uri()}#status/{row['id']}")
                if evidence.get("activity_observed") and evidence.get("activity_source") != "android_api":
                    from .adapters.facts import active_days
                    text = evidence.get("activity_text") or evidence.get("active_time") or ""
                    store.observe_activity(row["canonical_id"], text, active_days(text), row["checked_at"], "web_status",
                        origin_key=f"{self.path.as_uri()}#status-activity/{row['id']}")
        snapshot_versions = 0
        if "collection_observations" in self.tables:
            for row in self.conn.execute("SELECT * FROM collection_observations"):
                if row["canonical_id"] not in selected:
                    continue
                source_row = load_json(row["row_json"], {})
                snapshot = record_from_row(source_row)
                origin = f"{self.path.as_uri()}#list/{row['round_id']}/{row['canonical_id']}"
                store.conn.execute("INSERT OR IGNORE INTO list_observations VALUES (?,?,?,?,?,?)",
                    (origin, row["canonical_id"], row["first_observed_at"], row["last_observed_at"], row["queries_json"], encode(snapshot)))
                if snapshot["body"]["text"]:
                    version = snapshot["body"]["version_id"]
                    old = store.conn.execute("SELECT body_text FROM body_versions WHERE canonical_id=? AND version_id=?", (row["canonical_id"], version)).fetchone()
                    if old and old[0] != snapshot["body"]["text"]:
                        version = f"{row['canonical_id']}@snapshot/{row['round_id']}"
                    cursor = store.conn.execute("INSERT OR IGNORE INTO body_versions VALUES (?,?,?,?)",
                        (row["canonical_id"], version, snapshot["body"]["text"], snapshot["body"]["changed_at"]))
                    snapshot_versions += cursor.rowcount
        if "request_log" in self.tables and not ids and not limit:
            for row in self.conn.execute("SELECT * FROM request_log WHERE kind IN ('detail','detail_page','list','recommend','security_check') ORDER BY id"):
                store.record_request({"observed_at": row["ts"], "finished_at": None, "code": row["code"], "script": row["script"], "note": row["note"], "imported": True}, kind=row["kind"],
                    outcome=row["outcome"] or "unknown", origin_key=f"{self.path.as_uri()}#request/{row['id']}")
        store.conn.commit()
        return {"source": str(self.path), "source_connection": "mode=ro", "destination": str(store.path),
                "source_jobs_selected": len(selected), "missing_body_fetched_at_preserved": missing_fetch,
                "missing_body_changed_at_preserved": missing_changed, "unresolved_mobile_links_not_inherited": orphan_cards,
                "historical_snapshot_versions_added": snapshot_versions, "before": before, "after": store.summary(),
                "legacy_database_writes": 0, "analysis_or_personal_tables_imported": 0, "boss_requests": 0}


def import_mobile_archive(store, directory):
    """Explicit private wire/context history import; never account credentials."""
    from .contract import mobile_request_origin
    from .client import mobile_outcome
    directory = Path(directory).resolve()
    contexts = requests = 0
    log = directory / "requests.jsonl"
    if log.exists():
        for line in log.read_text(encoding="utf-8-sig").split("\n"):
            if not line.strip():
                continue
            metadata = json.loads(line)
            origin = mobile_request_origin(metadata)
            if store.conn.execute("SELECT 1 FROM requests WHERE origin_key=?", (origin,)).fetchone():
                continue
            outcome = mobile_outcome(metadata, {"code": metadata.get("code")})
            store.record_request({**metadata, "imported": True, "finished_at": None}, kind=metadata.get("endpoint") or "mobile",
                                 outcome=outcome, origin_key=origin)
            requests += 1
    for path in sorted((directory / "detail-contexts").glob("*.local.json")):
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        mobile_id = str(payload.get("job_id") or path.stem.removesuffix(".local"))
        canonical = payload.get("canonical_id")
        actual = canonical if canonical and store.get(canonical) else store.resolve("boss_mobile", mobile_id)
        key = path.as_uri() + "#" + str(payload.get("observed_at"))
        existed = store.conn.execute("SELECT 1 FROM private_sources WHERE origin_key=?", (key,)).fetchone()
        payload["job_id"] = mobile_id
        store.private(key, actual, payload.get("observed_at"), "mobile_detail_context", payload)
        if not existed:
            contexts += 1
    store.conn.commit()
    return {"directory": str(directory), "requests_added": requests, "private_detail_contexts_added": contexts,
            "account_session_imported": False, "boss_requests": 0}
