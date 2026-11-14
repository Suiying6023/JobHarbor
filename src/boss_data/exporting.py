"""JSONL import and export for job-source/1."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from .contract import CONTRACT_VERSION, PUBLIC_JOB_FIELDS, now, public_evidence, public_url
from .store import encode


def public_record(record):
    body = record["body"]
    status = record["status"]
    observation = lambda item: ({"outcome": item.get("outcome"), "observed_at": item.get("observed_at"),
                               "evidence": public_evidence(item.get("evidence") or {}),
                               **({"observation_id": item["observation_id"]} if "observation_id" in item else {})} if item else None)
    activity = lambda item: {key: item.get(key) for key in ("text", "active_days", "observed_at", "source")} if item else None
    return {
        "contract_version": CONTRACT_VERSION, "id": record["id"], "namespace": record["namespace"], "source_id": record["source_id"],
        "aliases": [{"namespace": a["namespace"], "source_id": a["source_id"]} for a in record.get("aliases", [])],
        "source_url": public_url(record.get("source_url")),
        "metadata": {key: record["metadata"][key] for key in PUBLIC_JOB_FIELDS if key in record["metadata"]},
        "body": {key: body.get(key) for key in ("text", "completeness", "fetched_at", "changed_at", "version_id")}
                | {"evidence": {key: (body.get("evidence") or {}).get(key) for key in ("kind", "access_verified", "source_job_id", "observed_at")}},
        "observations": {key: record["observations"].get(key) for key in ("first_seen_at", "last_list_seen_at", "published_at", "published_text", "publication_text_observed_at", "deadline_text")},
        "status": {"last_valid": observation(status.get("last_valid")), "latest_attempt": observation(status.get("latest_attempt")),
                   "history": [observation(item) for item in status.get("history", [])],
                   "saved_platform_fact": {key: (status.get("saved_platform_fact") or {}).get(key) for key in
                       ("job_invalid", "job_status_text", "raw_status_code", "reported_state", "observed_at", "source")}},
        "recruiter_activity": {"last_valid": activity(record["recruiter_activity"].get("last_valid")),
                               "latest_attempt": activity(record["recruiter_activity"].get("latest_attempt")),
                               "observations": [activity(item) for item in record["recruiter_activity"].get("observations", [])]},
    }


def export_bundle(records, output, *, source_name, source_store_id=None):
    destination = Path(output).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if (destination / "manifest.json").exists() or (destination / "jobs.jsonl").exists():
        raise FileExistsError("Choose a new export directory; existing source contracts are preserved")
    count = 0
    namespaces = {}
    with (destination / "jobs.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            public = public_record(record)
            stream.write(encode(public) + "\n")
            count += 1
            namespaces[public["namespace"]] = namespaces.get(public["namespace"], 0) + 1
    manifest = {"contract_version": CONTRACT_VERSION, "bundle_id": uuid.uuid4().hex, "generated_at": now(),
                "source_store_id": source_store_id, "source_name": source_name, "record_count": count,
                "namespace_counts": namespaces, "files": {"jobs": "jobs.jsonl"},
                "generation_time_is_acquisition_time": False, "includes_private_context": False,
                "includes_analysis": False, "unknown_times_preserved": True}
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"output": str(destination), **manifest}


def store_records(store, ids=None, limit=0):
    for record in store.iter_jobs(ids, limit=limit):
        history = [dict(r) for r in store.conn.execute("SELECT id,observed_at,outcome,evidence_json FROM status_observations WHERE canonical_id=? ORDER BY observed_at,id", (record["id"],))]
        record["status"]["history"] = [{"observation_id": row["id"], "outcome": row["outcome"], "observed_at": row["observed_at"],
                                         "evidence": json.loads(row["evidence_json"])} for row in history]
        activity = [{"text": row["text"], "active_days": row["active_days"], "observed_at": row["observed_at"], "source": row["source"]}
                    for row in store.conn.execute("SELECT * FROM activity_observations WHERE canonical_id=? ORDER BY observed_at,id", (record["id"],))]
        for observation in record["recruiter_activity"].get("observations", []):
            if observation not in activity:
                activity.append(observation)
        record["recruiter_activity"]["observations"] = activity
        yield record


def import_bundle(store, manifest_path):
    path = Path(manifest_path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    if manifest.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("Unsupported source contract")
    jobs = (path.parent / manifest["files"]["jobs"]).resolve()
    if not jobs.is_relative_to(path.parent):
        raise ValueError("Source contract file must be inside its bundle")
    count = 0
    for line in jobs.read_text(encoding="utf-8-sig").split("\n"):
        if not line.strip():
            continue
        record = public_record(json.loads(line))
        if record["id"] != f'{record["namespace"]}:{record["source_id"]}':
            raise ValueError("Canonical identity does not match its source namespace and ID")
        history = record["status"].pop("history", [])
        store.put(record)
        for i, observation in enumerate(history):
            store.observe_status(record["id"], observation["outcome"], observation["observed_at"], observation["evidence"],
                origin_key=f"contract/{manifest.get('source_store_id') or manifest['bundle_id']}/{record['id']}/{observation.get('observation_id', i)}")
        for observation in record["recruiter_activity"].get("observations", []):
            origin = encode([observation.get(key) for key in ("text", "active_days", "observed_at", "source")])
            store.observe_activity(record["id"], observation.get("text"), observation.get("active_days"),
                                   observation.get("observed_at"), observation.get("source"),
                origin_key=f"contract/{manifest.get('source_store_id') or manifest['bundle_id']}/{record['id']}/activity/{origin}")
        count += 1
    if count != manifest["record_count"]:
        raise ValueError("Source contract count does not match its manifest")
    store.conn.commit()
    return {"imported_records": count, "database": str(store.path), "boss_requests": 0}
