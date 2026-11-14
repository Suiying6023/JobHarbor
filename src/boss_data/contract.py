"""Versioned source facts. Observations and body acquisition are separate clocks."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

CONTRACT_VERSION = "job-source/1"
PUBLIC_JOB_FIELDS = (
    "title", "company_name", "company_url", "city", "district", "business_district", "location_text",
    "salary_text", "salary_min_k", "salary_max_k", "salary_months", "education", "experience", "address",
    "company_size", "financing_stage", "industry", "company_intro", "company_founded_year", "company_reg",
    "position_name", "recruitment_count", "latitude", "longitude", "skills", "labels", "welfare", "matched_queries",
    "recruiter_name", "recruiter_title", "recruiter_id", "recruiter_behavior_labels", "recruiter_certificated", "recruiter_online", "brand_id",
    "job_invalid", "job_status_desc", "job_type", "daily_rate", "is_proxy_job", "source_rank", "company_reg_updated_at",
)
RAW_SOURCE_KEYS = ("detail", "detail_source", "detail_access_verified", "detail_preview", "mobile",
                   "activity_observation", "jobType", "jobLabels", "external_source", "description_source")


def now():
    return datetime.now(timezone.utc).isoformat()


def mobile_request_origin(metadata):
    return "mobile-wire/" + str(metadata.get("observed_at")) + "/" + str(metadata.get("method")) + "/" + str(metadata.get("endpoint"))


def parse_time(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def load_json(value, default=None):
    if not value:
        return default
    return json.loads(value) if isinstance(value, str) else value


def private_source_raw(raw):
    """Select source payload fields for private storage."""
    if raw.get("encryptJobId") or raw.get("mobile") or raw.get("detail") or raw.get("external_source"):
        return dict(raw)
    return {key: raw[key] for key in RAW_SOURCE_KEYS if key in raw}


def public_url(url):
    parsed = urlsplit(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    # Retain public job locators and document anchors across source namespaces.
    # Private session/signing parameters are not part of provenance.
    host = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
    private_key = lambda name: bool(re.search(r"cookie|token|secret|signature|authorization|credential|session", name, re.I)) or name.lower() in {"sig", "sp", "t2", "wt", "wt2", "securityid", "lid", "auth"}
    query = urlencode([(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if not private_key(key)])
    fragment = parsed.fragment
    if "=" in fragment and any(private_key(key) for key, _ in parse_qsl(fragment, keep_blank_values=True)):
        fragment = ""
    return urlunsplit((parsed.scheme, host, parsed.path, query, fragment))


def canonical_web_id(url):
    parsed = urlsplit(url or "")
    match = re.fullmatch(r"/job_detail/([A-Za-z0-9_~-]+)\.html", parsed.path)
    return match.group(1) if match and parsed.hostname in ("www.zhipin.com", "m.zhipin.com") else None


def activity_signals(raw, detail_fetched_at=None):
    from .adapters.facts import active_days, mobile_activity_texts
    signals = []
    retained = raw.get("activity_observation") or {}
    if retained:
        signals.append({"text": retained.get("text", ""), "active_days": retained.get("active_days"),
                        "observed_at": retained.get("observed_at"), "source": retained.get("source")})
    mobile = raw.get("mobile") or {}
    boss = mobile.get("detail", {}).get("bossBaseInfo", {})
    for text in mobile_activity_texts(boss):
        signals.append({"text": text, "active_days": active_days(text),
                        "observed_at": mobile.get("observed_at"), "source": "android_api_detail"})
    web = raw.get("detail") or {}
    text = (web.get("bossInfo") or {}).get("activeTimeDesc")
    if text:
        signals.append({"text": text, "active_days": active_days(text),
                        "observed_at": (raw.get("detail_source") or {}).get("fetched_at") or detail_fetched_at,
                        "source": "web_detail"})
    return signals


def retain_recruiter_activity(raw, detail_fetched_at):
    """Retain the latest observed recruiter activity."""
    signals = [s for s in activity_signals(raw, detail_fetched_at)
               if s["active_days"] is not None and parse_time(s["observed_at"])]
    if signals:
        raw["activity_observation"] = max(signals, key=lambda s: (parse_time(s["observed_at"]), -s["active_days"]))


def record_from_row(row):
    row = dict(row)
    raw = load_json(row.get("raw_json"), {}) or row.get("raw") or {}
    metadata = {k: row.get(k) for k in PUBLIC_JOB_FIELDS if k in row}
    for key in ("skills", "labels", "welfare", "matched_queries", "recruiter_behavior_labels"):
        if key + "_json" in row:
            metadata[key] = load_json(row[key + "_json"], [])
    if "company_reg_json" in row:
        metadata["company_reg"] = load_json(row["company_reg_json"], {})
    boss = ((raw.get("mobile") or {}).get("detail") or {}).get("bossBaseInfo") or {}
    if "bossBehaviorLabels" in boss:
        metadata["recruiter_behavior_labels"] = list(boss["bossBehaviorLabels"] or [])
    if boss.get("bossId"):
        metadata["recruiter_id"] = str(boss["bossId"])
    if metadata.get("job_type") is None and "jobType" in raw:
        metadata["job_type"] = raw["jobType"]
    if metadata.get("daily_rate") is None:
        from .adapters.facts import is_daily_rate
        metadata["daily_rate"] = is_daily_rate(metadata.get("salary_text") or "")
    from .adapters.facts import CLOSED_MARKERS
    stored_closed = bool(row.get("job_invalid")) or raw.get("jobValidStatus") == 2 or any(marker in (row.get("job_status_desc") or "") for marker in CLOSED_MARKERS)
    saved_platform_fact = {"job_invalid": row.get("job_invalid"), "job_status_text": row.get("job_status_desc") or None,
                           "raw_status_code": raw.get("jobValidStatus"), "reported_state": "closed" if stored_closed else "unknown",
                           "observed_at": None, "source": "stored_platform_fields_without_status_observation_time"}
    platform = str(row.get("platform") or "boss")
    source_id = str(row.get("source_job_id") or "")
    canonical = row.get("canonical_id") or f"{platform}:{source_id}"
    text = row.get("job_description") or ""
    detail_source = raw.get("detail_source") or {}
    source_kind = detail_source.get("kind")
    verified = raw.get("detail_access_verified") is True or detail_source.get("access_verified") is True
    complete = "complete" if text and verified else "unknown" if text else "absent"
    version_at = row.get("description_updated_at") or None
    fetched_at = row.get("detail_fetched_at") or None
    signals = activity_signals(raw, fetched_at)
    if not signals and row.get("recruiter_active_status"):
        signals.append({"text": row["recruiter_active_status"], "active_days": row.get("recruiter_active_days"),
                        "observed_at": None, "source": "stored_fields_without_observation_time"})
    valid_signals = [s for s in signals if parse_time(s["observed_at"]) and s["active_days"] is not None]
    latest_activity = max(valid_signals, key=lambda s: (parse_time(s["observed_at"]), -s["active_days"])) if valid_signals else None
    mobile = raw.get("mobile") or {}
    return {
        "contract_version": CONTRACT_VERSION, "id": canonical, "namespace": platform, "source_id": source_id,
        "aliases": [{"namespace": "boss_mobile", "source_id": str(mobile["job_id"])}] if mobile.get("job_id") else [],
        "source_url": public_url(row.get("job_url")), "metadata": metadata,
        "body": {"text": text, "completeness": complete, "fetched_at": fetched_at, "changed_at": version_at,
                 "version_id": f"{canonical}@{version_at or 'legacy-current-unknown'}" if text else None,
                 "evidence": {"kind": source_kind, "access_verified": verified,
                              "source_job_id": detail_source.get("source_job_id"),
                              "observed_at": detail_source.get("fetched_at")}},
        "observations": {"first_seen_at": row.get("first_seen_at") or row.get("collected_at") or None,
                         "last_list_seen_at": row.get("last_seen_at") or None,
                         "published_at": None, "published_text": (mobile.get("facts") or {}).get("published_text"),
                         "publication_text_observed_at": mobile.get("observed_at") if (mobile.get("facts") or {}).get("published_text") else None,
                         "deadline_text": (mobile.get("facts") or {}).get("deadline_text")},
        "status": {"last_valid": None, "latest_attempt": None, "saved_platform_fact": saved_platform_fact},
        "recruiter_activity": {"last_valid": latest_activity, "latest_attempt": latest_activity, "observations": signals},
    }


def record_from_job(job):
    row = job.model_dump(mode="json")
    row["canonical_id"] = job.canonical_key()
    return record_from_row(row)


def public_evidence(evidence):
    keys = ("status", "http_status", "url", "len", "h1", "job_status", "active_time", "captcha_present",
            "restriction_notice", "page_prompt", "ok", "reason", "challenged", "gone", "error", "text_len",
            "identity_match", "requested_source_id", "returned_source_id", "title_match", "activity_observed",
            "activity_text", "activity_days", "activity_source", "state", "transport_error", "business_code")
    result = {key: evidence[key] for key in keys if key in evidence}
    if result.get("url"):
        result["url"] = public_url(result["url"])
    return result
