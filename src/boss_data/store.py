"""SQLite storage for source records and resumable tasks."""
from __future__ import annotations

import json
from copy import deepcopy
import sqlite3
import uuid
from pathlib import Path

from .contract import CONTRACT_VERSION, now, parse_time, public_evidence

_UNSET = object()

SCHEMA = """
CREATE TABLE source_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE jobs (
 canonical_id TEXT PRIMARY KEY, platform TEXT NOT NULL, source_job_id TEXT NOT NULL,
 title TEXT, company_name TEXT, job_url TEXT, job_description TEXT NOT NULL,
 detail_fetched_at TEXT, description_updated_at TEXT, body_completeness TEXT NOT NULL,
 body_version_id TEXT, last_valid_status TEXT, status_observed_at TEXT,
 latest_attempt_outcome TEXT, latest_attempt_at TEXT, record_json TEXT NOT NULL
);
CREATE TABLE body_versions (
 canonical_id TEXT NOT NULL, version_id TEXT NOT NULL, body_text TEXT NOT NULL, changed_at TEXT,
 PRIMARY KEY(canonical_id,version_id)
);
CREATE TABLE body_acquisitions (
 id INTEGER PRIMARY KEY, canonical_id TEXT NOT NULL, version_id TEXT, fetched_at TEXT,
 outcome TEXT NOT NULL, source_kind TEXT, origin_key TEXT UNIQUE
);
CREATE TABLE status_observations (
 id INTEGER PRIMARY KEY, canonical_id TEXT NOT NULL, observed_at TEXT, outcome TEXT NOT NULL,
 task_id TEXT, evidence_json TEXT NOT NULL, origin_key TEXT UNIQUE
);
CREATE INDEX status_job_time ON status_observations(canonical_id,observed_at,id);
CREATE TABLE activity_observations (
 id INTEGER PRIMARY KEY, canonical_id TEXT NOT NULL, observed_at TEXT, text TEXT, active_days INTEGER,
 source TEXT, origin_key TEXT UNIQUE
);
CREATE TABLE mobile_cards (
 mobile_job_id TEXT PRIMARY KEY, canonical_id TEXT, query TEXT, page INTEGER, first_seen_at TEXT,
 last_seen_at TEXT, public_json TEXT NOT NULL, private_card_json TEXT NOT NULL
);
CREATE TABLE source_aliases (namespace TEXT, source_id TEXT, canonical_id TEXT NOT NULL,
 PRIMARY KEY(namespace,source_id));
CREATE TABLE private_sources (origin_key TEXT PRIMARY KEY, canonical_id TEXT, observed_at TEXT,
 kind TEXT, payload_json TEXT NOT NULL);
CREATE INDEX private_source_job ON private_sources(canonical_id,observed_at DESC);
CREATE INDEX private_detail_id ON private_sources(kind,json_extract(payload_json,'$.job_id'),observed_at DESC);
CREATE TABLE requests (
 id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, kind TEXT NOT NULL, outcome TEXT NOT NULL,
 http_status INTEGER, business_code INTEGER, egress TEXT, task_id TEXT, canonical_id TEXT,
 evidence_json TEXT NOT NULL, origin_key TEXT UNIQUE
);
CREATE INDEX request_time ON requests(started_at);
CREATE INDEX request_task ON requests(task_id,started_at);
CREATE TABLE tasks (task_id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, state_json TEXT NOT NULL);
CREATE TABLE source_changes (sequence INTEGER PRIMARY KEY, canonical_id TEXT NOT NULL,
 kind TEXT NOT NULL, observed_at TEXT, version_id TEXT);
CREATE TABLE consumer_cursors (name TEXT PRIMARY KEY, sequence INTEGER NOT NULL);
CREATE TABLE list_observations (origin_key TEXT PRIMARY KEY, canonical_id TEXT NOT NULL,
 first_observed_at TEXT,last_observed_at TEXT,queries_json TEXT NOT NULL,source_json TEXT NOT NULL);
"""


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class SourceStore:
    def __init__(self, path, *, writable=False):
        self.path = Path(path).resolve()
        self.writable = writable
        if self.path.exists():
            # Validate the database format before enabling writes.
            check = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
            try:
                owner = check.execute("SELECT value FROM source_meta WHERE key='owner'").fetchone()
                if not owner or owner[0] != "JobMarketRadar":
                    raise ValueError("Database is not a JobHarbor source store; use import-legacy to migrate it")
            except sqlite3.OperationalError as exc:
                raise ValueError("Database is not a JobHarbor source store; use import-legacy to migrate it") from exc
            finally:
                check.close()
            self.conn = sqlite3.connect(self.path, timeout=30) if writable else sqlite3.connect(
                self.path.as_uri() + "?mode=ro", uri=True)
        elif writable:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path)
            self.conn.executescript(SCHEMA)
            self.conn.executemany("INSERT INTO source_meta VALUES (?,?)", [
                ("owner", "JobMarketRadar"), ("schema_version", "1"), ("contract_version", CONTRACT_VERSION),
                ("store_id", uuid.uuid4().hex), ("created_at", now())])
            self.conn.commit()
        else:
            raise FileNotFoundError(self.path)
        self.conn.row_factory = sqlite3.Row
        if writable:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("CREATE INDEX IF NOT EXISTS private_source_job ON private_sources(canonical_id,observed_at DESC)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS private_detail_id ON private_sources(kind,json_extract(payload_json,'$.job_id'),observed_at DESC)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS request_task ON requests(task_id,started_at)")
            self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.writable and not args[0]:
            self.conn.commit()
        self.close()

    def get(self, canonical_id):
        row = self.conn.execute("SELECT record_json FROM jobs WHERE canonical_id=?", (canonical_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def iter_jobs(self, ids=None, *, status=None, limit=0):
        where, parameters = [], []
        if ids:
            where.append("canonical_id IN (" + ",".join("?" for _ in ids) + ")")
            parameters.extend(ids)
        if status:
            where.append("coalesce(last_valid_status,'unknown')=?")
            parameters.append(status)
        sql = "SELECT record_json FROM jobs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY canonical_id"
        if limit:
            sql += " LIMIT ?"
            parameters.append(limit)
        for row in self.conn.execute(sql, parameters):
            yield json.loads(row[0])

    def put(self, record, *, acquisition=False, origin_key=None):
        record = deepcopy(record)
        incoming = deepcopy(record)
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        previous = self.get(record["id"])
        outcome = "first_body" if record["body"]["text"] else "no_body"
        historical = False
        incoming_body = None
        if previous and previous["body"]["text"]:
            old_time = parse_time(previous["body"]["fetched_at"])
            new_time = parse_time(record["body"]["fetched_at"])
            historical = new_time is None or old_time is not None and (
                new_time < old_time or not acquisition and new_time == old_time and record["body"]["text"] != previous["body"]["text"])
            if historical:
                incoming_body = self._archive_body(record["id"], incoming["body"])
                record["body"] = deepcopy(previous["body"])
                outcome = "historical_body_preserved"
                payload = encode(incoming)
                if not self.conn.execute("SELECT 1 FROM private_sources WHERE canonical_id=? AND kind='source_snapshot' AND payload_json=?", (record["id"], payload)).fetchone():
                    self.private("source-snapshot/" + uuid.uuid4().hex, record["id"], incoming["body"]["fetched_at"], "source_snapshot", incoming)
        if acquisition and record["body"]["completeness"] == "complete" and (not previous or not previous["body"]["text"]):
            record["body"]["changed_at"] = record["body"]["fetched_at"]
            record["body"]["version_id"] = f'{record["id"]}@{record["body"]["fetched_at"]}'
        if previous:
            if acquisition and not historical and record["body"]["completeness"] == "complete":
                from .adapters.normalization import normalized_description
                if normalized_description(previous["body"]["text"]) == normalized_description(record["body"]["text"]):
                    outcome = "unchanged_body_reacquired"
                    record["body"]["changed_at"] = previous["body"]["changed_at"]
                    # Semantic equality permits fact reuse, but each exported
                    # verbatim text must resolve to its own archived representation.
                    record["body"]["version_id"] = previous["body"]["version_id"] if previous["body"]["text"] == record["body"]["text"] else (
                        f'{record["id"]}@representation/{record["body"]["fetched_at"] or uuid.uuid4().hex}')
                elif previous["body"]["text"]:
                    outcome = "changed_body"
                    record["body"]["changed_at"] = record["body"]["fetched_at"]
                    record["body"]["version_id"] = f'{record["id"]}@{record["body"]["fetched_at"]}'
            elif acquisition and not historical:
                # A preview or empty detail never renews an archived complete JD.
                record["body"] = previous["body"]
                outcome = "preview_or_missing_body_preserved"
            new_clock, old_clock = self._fact_clock(incoming), self._fact_clock(previous)
            newer = new_clock is not None and (old_clock is None or new_clock > old_clock)
            merged = dict(previous["metadata"])
            for key, value in record["metadata"].items():
                if value is not None and value != "" and (newer or merged.get(key) is None or merged.get(key) == ""):
                    merged[key] = value
            record["metadata"] = merged
            if not newer:
                record["source_url"] = previous.get("source_url") or record.get("source_url")
            observations = {**previous["observations"]}
            for key, value in record["observations"].items():
                if value is not None and (newer or observations.get(key) is None):
                    observations[key] = value
            first = [x for x in (previous["observations"].get("first_seen_at"), incoming["observations"].get("first_seen_at")) if parse_time(x)]
            if first:
                observations["first_seen_at"] = min(first, key=parse_time)
            last = [x for x in (previous["observations"].get("last_list_seen_at"), incoming["observations"].get("last_list_seen_at")) if parse_time(x)]
            if last:
                observations["last_list_seen_at"] = max(last, key=parse_time)
            old_pub = parse_time(previous["observations"].get("publication_text_observed_at"))
            new_pub = parse_time(incoming["observations"].get("publication_text_observed_at"))
            if old_pub and (not new_pub or new_pub < old_pub):
                for key in ("published_text", "publication_text_observed_at"):
                    observations[key] = previous["observations"].get(key)
            record["observations"] = observations
            record["status"]["last_valid"] = self._latest_observation(previous["status"].get("last_valid"), incoming["status"].get("last_valid"), valid=True)
            record["status"]["latest_attempt"] = self._latest_observation(previous["status"].get("latest_attempt"), incoming["status"].get("latest_attempt"))
            if not newer and previous["status"].get("saved_platform_fact"):
                record["status"]["saved_platform_fact"] = previous["status"]["saved_platform_fact"]
            for key in ("last_valid", "latest_attempt"):
                record["recruiter_activity"][key] = self._latest_observation(previous["recruiter_activity"].get(key), incoming["recruiter_activity"].get(key))
            for observation in previous["recruiter_activity"].get("observations", []):
                if observation not in record["recruiter_activity"]["observations"]:
                    record["recruiter_activity"]["observations"].append(observation)
            aliases = {(a["namespace"], a["source_id"]): a for a in [*previous.get("aliases", []), *record.get("aliases", [])]}
            record["aliases"] = list(aliases.values())
        record["body"] = self._archive_body(record["id"], record["body"])
        body = record["body"]
        state = record["status"]
        valid, attempt = state.get("last_valid") or {}, state.get("latest_attempt") or {}
        self.conn.execute("""INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
         ON CONFLICT(canonical_id) DO UPDATE SET
         platform=excluded.platform,source_job_id=excluded.source_job_id,title=excluded.title,
         company_name=excluded.company_name,job_url=excluded.job_url,job_description=excluded.job_description,
         detail_fetched_at=excluded.detail_fetched_at,description_updated_at=excluded.description_updated_at,
         body_completeness=excluded.body_completeness,body_version_id=excluded.body_version_id,
         last_valid_status=excluded.last_valid_status,status_observed_at=excluded.status_observed_at,
         latest_attempt_outcome=excluded.latest_attempt_outcome,latest_attempt_at=excluded.latest_attempt_at,
         record_json=excluded.record_json""", (
            record["id"], record["namespace"], record["source_id"], record["metadata"].get("title"),
            record["metadata"].get("company_name"), record.get("source_url"), body["text"], body["fetched_at"],
            body["changed_at"], body["completeness"], body["version_id"], valid.get("outcome"), valid.get("observed_at"),
            attempt.get("outcome"), attempt.get("observed_at"), encode(record)))
        for alias in record.get("aliases", []):
            self.alias(alias["namespace"], alias["source_id"], record["id"])
        if acquisition:
            acquired = incoming_body or body
            self.conn.execute("INSERT OR IGNORE INTO body_acquisitions(canonical_id,version_id,fetched_at,outcome,source_kind,origin_key) VALUES (?,?,?,?,?,?)",
                (record["id"], acquired["version_id"], acquired["fetched_at"], outcome, acquired["evidence"].get("kind"), origin_key))
        if not previous or previous["body"] != record["body"] or previous["metadata"] != record["metadata"] or previous["observations"] != record["observations"] or previous["recruiter_activity"] != record["recruiter_activity"]:
            self.conn.execute("INSERT INTO source_changes(canonical_id,kind,observed_at,version_id) VALUES (?,?,?,?)",
                (record["id"], "body_acquired" if acquisition else "import", body["fetched_at"], body["version_id"]))
        return outcome

    @staticmethod
    def _fact_clock(record):
        values = [record["body"].get("fetched_at"), record["observations"].get("last_list_seen_at"),
                  record["observations"].get("publication_text_observed_at")]
        parsed = [parse_time(value) for value in values if parse_time(value)]
        return max(parsed) if parsed else None

    @staticmethod
    def _latest_observation(old, new, *, valid=False):
        candidates = [value for value in (old, new) if value]
        if valid:
            candidates = [value for value in candidates if value.get("outcome") in ("open", "closed") and parse_time(value.get("observed_at"))]
        dated = [value for value in candidates if parse_time(value.get("observed_at"))]
        return max(dated, key=lambda value: parse_time(value["observed_at"])) if dated else candidates[0] if candidates else None

    def _archive_body(self, canonical_id, body):
        body = deepcopy(body)
        if not body["text"]:
            return body
        version = body["version_id"] or canonical_id + "@unknown/" + uuid.uuid4().hex
        row = self.conn.execute("SELECT body_text FROM body_versions WHERE canonical_id=? AND version_id=?", (canonical_id, version)).fetchone()
        if row and row[0] != body["text"]:
            version = canonical_id + "@representation/" + (body.get("fetched_at") or uuid.uuid4().hex)
            collision = self.conn.execute("SELECT body_text FROM body_versions WHERE canonical_id=? AND version_id=?", (canonical_id, version)).fetchone()
            if collision and collision[0] != body["text"]:
                version += "/" + uuid.uuid4().hex
        body["version_id"] = version
        self.conn.execute("INSERT OR IGNORE INTO body_versions VALUES (?,?,?,?)", (canonical_id, version, body["text"], body["changed_at"]))
        return body

    def alias(self, namespace, source_id, canonical_id):
        previous = self.resolve(namespace, source_id)
        if previous and previous != canonical_id:
            raise ValueError(f"Source ID maps to two different jobs: {namespace}:{source_id}")
        self.conn.execute("INSERT OR IGNORE INTO source_aliases VALUES (?,?,?)", (namespace, str(source_id), canonical_id))

    def resolve(self, namespace, source_id):
        row = self.conn.execute("SELECT canonical_id FROM source_aliases WHERE namespace=? AND source_id=?", (namespace, str(source_id))).fetchone()
        return row[0] if row else None

    def private(self, key, canonical_id, observed_at, kind, payload):
        self.conn.execute("INSERT OR IGNORE INTO private_sources VALUES (?,?,?,?,?)", (key, canonical_id, observed_at, kind, encode(payload)))

    def private_latest(self, canonical_id):
        row = self.conn.execute("SELECT payload_json FROM private_sources WHERE canonical_id=? ORDER BY observed_at DESC,rowid DESC LIMIT 1", (canonical_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def source_raw(self, canonical_id):
        row = self.conn.execute("SELECT payload_json FROM private_sources WHERE canonical_id=? AND kind IN ('legacy_source_raw','normalized_source','legacy_source_facts') ORDER BY observed_at DESC,rowid DESC LIMIT 1", (canonical_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def detail_context(self, mobile_id):
        row = self.conn.execute("SELECT payload_json FROM private_sources WHERE kind='mobile_detail_context' AND json_extract(payload_json,'$.job_id')=? ORDER BY observed_at DESC,rowid DESC LIMIT 1", (str(mobile_id),)).fetchone()
        return json.loads(row[0]) if row else {}

    def card(self, mobile_id):
        row = self.conn.execute("SELECT * FROM mobile_cards WHERE mobile_job_id=?", (str(mobile_id),)).fetchone()
        return dict(row) if row else None

    def put_card(self, mobile_id, public, private, query, page, observed_at, canonical_id=None, *, first_seen_at=_UNSET):
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        previous = self.card(mobile_id)
        canonical_id = canonical_id or self.resolve("boss_mobile", mobile_id) or (previous or {}).get("canonical_id")
        first_input = observed_at if first_seen_at is _UNSET else first_seen_at
        first_values = [value for value in ((previous or {}).get("first_seen_at"), first_input) if parse_time(value)]
        first = min(first_values, key=parse_time) if first_values else (previous or {}).get("first_seen_at") or first_input
        old_time = parse_time((previous or {}).get("last_seen_at"))
        new_time = parse_time(observed_at)
        newer = previous is None or new_time is not None and (old_time is None or new_time > old_time)
        if previous and not newer:
            public, private = json.loads(previous["public_json"]), json.loads(previous["private_card_json"])
            query, page, observed_at = previous["query"], previous["page"], previous["last_seen_at"]
            canonical_id = previous["canonical_id"] or canonical_id
        public = {**public, "canonical_id": canonical_id}
        record = self.get(canonical_id) if canonical_id else None
        public["has_description"] = bool(record and record["body"]["text"])
        self.conn.execute("""INSERT INTO mobile_cards VALUES (?,?,?,?,?,?,?,?)
         ON CONFLICT(mobile_job_id) DO UPDATE SET canonical_id=coalesce(excluded.canonical_id,mobile_cards.canonical_id),
         query=excluded.query,page=excluded.page,first_seen_at=excluded.first_seen_at,last_seen_at=excluded.last_seen_at,
         public_json=excluded.public_json,private_card_json=excluded.private_card_json""",
         (str(mobile_id), canonical_id, query, page, first, observed_at, encode(public), encode(private)))
        if canonical_id:
            self.alias("boss_mobile", mobile_id, canonical_id)
        if record:
            old_seen = parse_time(record["observations"].get("last_list_seen_at"))
            selected_seen = parse_time(observed_at)
            changed = False
            if selected_seen and (not old_seen or selected_seen > old_seen):
                record["observations"]["last_list_seen_at"] = observed_at
                changed = True
            existing_first = parse_time(record["observations"].get("first_seen_at"))
            if parse_time(first) and (not existing_first or parse_time(first) < existing_first):
                record["observations"]["first_seen_at"] = first
                changed = True
            if changed:
                self._update_observations(record)
                self.conn.execute("INSERT INTO source_changes(canonical_id,kind,observed_at,version_id) VALUES (?,?,?,?)",
                                  (canonical_id, "list", observed_at, record["body"]["version_id"]))
        return not previous

    def observe_status(self, canonical_id, outcome, observed_at, evidence, *, task_id=None, origin_key=None):
        evidence = public_evidence(evidence)
        cursor = self.conn.execute("INSERT OR IGNORE INTO status_observations(canonical_id,observed_at,outcome,task_id,evidence_json,origin_key) VALUES (?,?,?,?,?,?)",
            (canonical_id, observed_at, outcome, task_id, encode(evidence), origin_key))
        if cursor.rowcount == 0:
            return
        record = self.get(canonical_id)
        if record is None:
            return
        attempt = {"outcome": outcome, "observed_at": observed_at, "evidence": evidence}
        latest = record["status"].get("latest_attempt")
        if not latest or (parse_time(observed_at) and (not parse_time(latest.get("observed_at")) or parse_time(observed_at) >= parse_time(latest["observed_at"]))):
            record["status"]["latest_attempt"] = attempt
        valid = record["status"].get("last_valid")
        if outcome in ("open", "closed") and parse_time(observed_at) and (not valid or not parse_time(valid.get("observed_at")) or parse_time(observed_at) >= parse_time(valid["observed_at"])):
            record["status"]["last_valid"] = attempt
        self._update_observations(record)
        self.conn.execute("INSERT INTO source_changes(canonical_id,kind,observed_at,version_id) VALUES (?,?,?,?)",
                          (canonical_id, "status", observed_at, record["body"]["version_id"]))

    def observe_activity(self, canonical_id, text, days, observed_at, source, *, origin_key=None):
        cursor = self.conn.execute("INSERT OR IGNORE INTO activity_observations(canonical_id,observed_at,text,active_days,source,origin_key) VALUES (?,?,?,?,?,?)",
            (canonical_id, observed_at, text, days, source, origin_key))
        if cursor.rowcount == 0:
            return
        record = self.get(canonical_id)
        if not record:
            return
        observation = {"text": text or "", "active_days": days, "observed_at": observed_at, "source": source}
        activity = record["recruiter_activity"]
        last = activity.get("last_valid")
        if text and days is not None and parse_time(observed_at) and (not last or not parse_time(last.get("observed_at")) or parse_time(observed_at) >= parse_time(last["observed_at"])):
            activity["last_valid"] = observation
        latest = activity.get("latest_attempt")
        if not latest or (parse_time(observed_at) and (not parse_time(latest.get("observed_at")) or parse_time(observed_at) >= parse_time(latest["observed_at"]))):
            activity["latest_attempt"] = observation
        if observation not in activity["observations"]:
            activity["observations"].append(observation)
        self._update_observations(record)
        self.conn.execute("INSERT INTO source_changes(canonical_id,kind,observed_at,version_id) VALUES (?,?,?,?)",
                          (canonical_id, "activity", observed_at, record["body"]["version_id"]))

    def _update_observations(self, record):
        valid, attempt = record["status"].get("last_valid") or {}, record["status"].get("latest_attempt") or {}
        self.conn.execute("UPDATE jobs SET last_valid_status=?,status_observed_at=?,latest_attempt_outcome=?,latest_attempt_at=?,record_json=? WHERE canonical_id=?",
            (valid.get("outcome"), valid.get("observed_at"), attempt.get("outcome"), attempt.get("observed_at"), encode(record), record["id"]))

    def record_request(self, metadata, *, kind, outcome, task_id=None, canonical_id=None, origin_key=None):
        ident = uuid.uuid4().hex
        evidence = {key: metadata[key] for key in ("elapsed_seconds", "pacing", "transport_error", "sub_code", "sub_responses", "error", "method", "endpoint", "headers", "script", "note", "imported") if key in metadata}
        self.conn.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(origin_key) DO UPDATE SET evidence_json=json_patch(requests.evidence_json,excluded.evidence_json)", (
            ident, metadata.get("observed_at") or metadata.get("started_at"), metadata.get("finished_at") if "finished_at" in metadata else now(), kind,
            outcome, metadata.get("status"), metadata.get("code"), metadata.get("egress"), task_id, canonical_id, encode(evidence), origin_key))
        if origin_key:
            ident = self.conn.execute("SELECT id FROM requests WHERE origin_key=?", (origin_key,)).fetchone()[0]
        return ident

    def begin_request(self, metadata, *, kind, task_id=None, canonical_id=None, origin_key=None):
        ident = self.record_request({**metadata, "finished_at": None}, kind=kind, outcome="pending", task_id=task_id, canonical_id=canonical_id, origin_key=origin_key)
        self.conn.commit()
        return ident

    def finish_request(self, ident, metadata, outcome):
        evidence = {key: metadata[key] for key in ("elapsed_seconds", "pacing", "transport_error", "sub_code", "sub_responses", "error", "endpoint", "url") if key in metadata}
        if "url" in evidence:
            from .contract import public_url
            evidence["url"] = public_url(evidence["url"])
        self.conn.execute("UPDATE requests SET finished_at=?,outcome=?,http_status=?,business_code=?,evidence_json=? WHERE id=?",
            (metadata.get("finished_at") or now(), outcome, metadata.get("status"), metadata.get("code"), encode(evidence), ident))
        self.conn.commit()

    def save_task(self, state, *, clear_cancel=False):
        previous = self.task(state["task_id"])
        if previous and previous.get("cancel_requested") and not clear_cancel:
            state["cancel_requested"] = True
        state["results"] = [summarize_result(result) for result in state.get("results", [])[-100:]]
        state["updated_at"] = now()
        self.conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at,state_json=excluded.state_json",
            (state["task_id"], state["kind"], state["status"], state["created_at"], state["updated_at"], encode(state)))
        self.conn.commit()

    def task(self, ident):
        row = self.conn.execute("SELECT state_json FROM tasks WHERE task_id=?", (ident,)).fetchone()
        return json.loads(row[0]) if row else None

    def tasks(self):
        return [json.loads(r[0]) for r in self.conn.execute("SELECT state_json FROM tasks ORDER BY created_at DESC")]

    def changes(self, after=0, limit=100):
        return [dict(r) for r in self.conn.execute("SELECT * FROM source_changes WHERE sequence>? ORDER BY sequence LIMIT ?", (after, limit))]

    def cursor(self, name):
        row = self.conn.execute("SELECT sequence FROM consumer_cursors WHERE name=?", (name,)).fetchone()
        return row[0] if row else 0

    def acknowledge(self, name, through):
        if through < self.cursor(name) or through > self.conn.execute("SELECT coalesce(max(sequence),0) FROM source_changes").fetchone()[0]:
            raise ValueError("Cursor must advance to an existing source-change sequence")
        self.conn.execute("INSERT INTO consumer_cursors VALUES (?,?) ON CONFLICT(name) DO UPDATE SET sequence=excluded.sequence", (name, through))

    def summary(self):
        count = lambda table: self.conn.execute("SELECT count(*) FROM " + table).fetchone()[0]
        return {"database": str(self.path), "owner": "JobMarketRadar", "schema_version": 1,
                "jobs": count("jobs"), "mobile_cards": count("mobile_cards"), "source_aliases": count("source_aliases"),
                "body_versions": count("body_versions"), "body_acquisitions": count("body_acquisitions"),
                "status_observations": count("status_observations"), "activity_observations": count("activity_observations"),
                "requests": count("requests"), "tasks": count("tasks"), "list_observations": count("list_observations"),
                "status_counts": dict(self.conn.execute("SELECT coalesce(last_valid_status,'unknown'),count(*) FROM jobs GROUP BY last_valid_status")),
                "body_completeness": dict(self.conn.execute("SELECT body_completeness,count(*) FROM jobs GROUP BY body_completeness")),
                "request_outcomes": dict(self.conn.execute("SELECT outcome,count(*) FROM requests GROUP BY outcome")),
                "acquisition_outcomes": dict(self.conn.execute("SELECT outcome,count(*) FROM body_acquisitions GROUP BY outcome"))}


def summarize_result(result):
    keys = ("operation", "outcome", "id", "mobile_id", "state", "requests", "count", "query", "city_code", "page", "has_more",
            "new_cards", "new_bodies", "changed_bodies", "body_acquisition_outcome", "observed_at", "request_started_at",
            "http_status", "business_code", "endpoint", "method", "reason", "message", "evidence", "source_response", "body_version_id")
    compact = {key: result[key] for key in keys if key in result}
    if result.get("job"):
        compact["body_version_id"] = result["job"]["body"]["version_id"]
    if result.get("cards"):
        compact["source_ids"] = [card.get("canonical_id") or "boss_mobile:" + card["job_id"] for card in result["cards"]]
    elif result.get("source_ids"):
        compact["source_ids"] = result["source_ids"]
    return compact
