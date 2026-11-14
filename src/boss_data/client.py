"""Python/Agent facade for bounded source-data work, without a dialogue dependency."""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .contract import mobile_request_origin, now, parse_time, record_from_job
from .local_files import file_mutex
from .store import SourceStore

ATTENTION = {"verification_required", "account_restricted", "login_expired", "restricted", "rate_limited", "transport_error", "session_or_environment_unknown", "decode_error"}


def mobile_outcome(metadata, response):
    if metadata.get("transport_error") or metadata.get("status") is None:
        return "transport_error"
    if metadata["status"] == 401:
        return "login_expired"
    if metadata["status"] == 403:
        return "restricted"
    if metadata["status"] == 429:
        return "rate_limited"
    if metadata["status"] != 200:
        return "http_error"
    subs = list(metadata.get("sub_responses") or [])
    subs += [value for value in (response.get("zpData") or {}).values() if isinstance(value, dict) and "code" in value]
    codes = [response.get("code"), *[s.get("code") for s in subs]]
    messages = " | ".join(str(m) for m in [response.get("message"), *[s.get("message") for s in subs]] if m)
    # App HttpResponse: 7/31/32 are login-state failures, 35/36 verification.
    # Code 37 needs its endpoint/message context; it is not a universal token verdict.
    if any(code in (7, 31, 32) for code in codes) or re.search(r"未登录|请.{0,2}登录|登录.*(?:失效|过期|超时)|(?:token|t2).*expired|not.?logged.?in|鉴权失败|认证失败", messages, re.I):
        return "login_expired"
    if any(code in (35, 36) for code in codes):
        return "account_restricted" if re.search(r"账号.*(?:封禁|冻结)|账户.*(?:封禁|冻结)|account.*(?:banned|suspended)", messages, re.I) else "verification_required"
    if 37 in codes:
        return "verification_required" if re.search(r"验证|安全检查|环境.*异常|异常访问|security.?check|verify", messages, re.I) else "session_or_environment_unknown"
    if response.get("decode_error"):
        return "decode_error"
    return "ok" if all(code == 0 for code in codes) else "platform_error"


def failure_result(metadata, response):
    outcome = mobile_outcome(metadata, response)
    subs = metadata.get("sub_responses") or [{"endpoint": key, "code": value.get("code"), "message": value.get("message")}
        for key, value in (response.get("zpData") or {}).items() if isinstance(value, dict) and "code" in value]
    actions = {"verification_required": "在原账号的BOSS App完成验证后，显式恢复此受影响任务。",
               "login_expired": "核对原账号登录状态并更新原会话，再显式恢复此任务。",
               "transport_error": "核对已配置出口与隧道；保持原账号和出口。"}
    return {"outcome": outcome, "requests": 1, "state": "unknown", "http_status": metadata.get("status"),
            "business_code": response.get("code"), "message": response.get("message"), "sub_responses": subs,
            "method": metadata.get("method"), "endpoint": metadata.get("endpoint"),
            "verification_context_available": bool(metadata.get("challenge_context")), "next_action": actions.get(outcome)}


class BossCli:
    def __init__(self, config=None, database=None, *, settings=None, progress=None):
        self.settings = settings or Settings.load(config, database)
        self.progress = progress

    def _report(self, event, **values):
        if self.progress:
            self.progress({"event": event, "at": now(), **values})

    def _mobile(self, store, operation, task=None):
        from .adapters.mobile_protocol import BossMobileClient
        cfg = self.settings.mobile
        if not cfg.get("session_file"):
            raise ValueError("Authenticated HTTP route needs an explicit session file")
        interval = random.uniform(*cfg.get("interval_seconds", (35, 55)))
        request_id = None

        def admitted():
            if task is not None and (self._cancelled(store, task) or self._limit_reached(store, task)):
                raise InterruptedError("task_cancelled")

        def started(metadata):
            nonlocal request_id
            metadata["egress"] = cfg.get("egress_label", "mobile")
            request_id = store.begin_request(metadata, kind=operation["kind"], task_id=task["task_id"] if task else None,
                                             canonical_id=operation.get("id"), origin_key=mobile_request_origin(metadata))
            if task is not None:
                task["inflight"] = {"operation": operation, "request_id": request_id, "started_at": metadata["observed_at"]}
                task["calls_in_batch"] = task.get("calls_in_batch", 0) + 1
                store.save_task(task)
            self._report("request_started", task_id=task["task_id"] if task else None, kind=operation["kind"], started_at=metadata["observed_at"])

        def finished(metadata, response):
            metadata["egress"] = cfg.get("egress_label", "mobile")
            metadata["finished_at"] = now()
            outcome = mobile_outcome(metadata, response)
            store.private("response/" + request_id, operation.get("id"), metadata["finished_at"], "mobile_response",
                          {"response": response, "metadata": metadata, "operation": operation})
            store.finish_request(request_id, metadata, outcome)

        client = BossMobileClient(cfg["session_file"], egress=cfg.get("proxy") or "direct", interval_seconds=interval,
                                  on_started=started, on_finished=finished, on_admit=admitted)
        client.egress = cfg.get("egress_label", "mobile")
        return client

    def _search_response(self, store, response, metadata, operation):
        from .adapters.mobile_payload import SEARCH_PATH, _card, normalize_search
        from .adapters.facts import active_days
        outcome = mobile_outcome(metadata, response)
        if outcome != "ok":
            return failure_result(metadata, response)
        normalized = normalize_search(response)
        payload = response.get("zpData") or {}
        payload = (payload.get(SEARCH_PATH) or {}).get("zpData") or payload
        cards = [card for group in payload.get("cardList", []) for card in group.get("positionSearchCardList", [])]
        public = []
        new_cards = 0
        observed_at = metadata.get("finished_at") or metadata["observed_at"]
        for raw in cards:
            card = _card(raw)
            mobile_id = card["job_id"]
            canonical = store.resolve("boss_mobile", mobile_id)
            card["canonical_id"] = canonical
            card["has_description"] = bool((store.get(canonical) or {}).get("body", {}).get("text"))
            text = "在线" if card.get("recruiter_online") is True else card.get("recruiter_active_status") or ""
            source = "android_api_list_online" if card.get("recruiter_online") is True else "android_api_list"
            card["activity_observation"] = {"text": text, "active_days": active_days(text), "observed_at": observed_at, "source": source}
            new_cards += store.put_card(mobile_id, card, raw, operation["query"], operation["page"], observed_at, canonical)
            if canonical:
                store.observe_activity(canonical, text, active_days(text), observed_at, source)
            public.append(card)
        return {"outcome": "ok", "query": operation["query"], "city_code": operation["city"], "page": operation["page"],
                "cards": public, "count": len(public), "has_more": normalized["has_more"], "new_cards": new_cards,
                "requests": 1, "new_bodies": 0, "observed_at": observed_at, "http_status": metadata["status"],
                "business_code": response["code"]}

    def _detail_context(self, store, canonical_id=None, mobile_id=None):
        if canonical_id and not mobile_id:
            record = store.get(canonical_id)
            if not record:
                raise ValueError("Unknown stored source job")
            mobile_id = next((a["source_id"] for a in record.get("aliases", []) if a["namespace"] == "boss_mobile"), None)
            if not mobile_id:
                found = store.conn.execute("SELECT source_id FROM source_aliases WHERE namespace='boss_mobile' AND canonical_id=? LIMIT 1", (canonical_id,)).fetchone()
                mobile_id = found[0] if found else None
        canonical_id = canonical_id or store.resolve("boss_mobile", mobile_id)
        card = store.card(mobile_id)
        raw = json.loads(card["private_card_json"]) if card else {}
        request = {"securityId": raw.get("securityId"), "lid": raw.get("lid", ""), "query": (card or {}).get("query", "")}
        if not request["securityId"] and mobile_id:
            stored = store.detail_context(mobile_id)
            if stored.get("request", {}).get("securityId"):
                if stored.get("canonical_id") and canonical_id and stored["canonical_id"] != canonical_id:
                    raise ValueError("Stored source detail context maps to a different canonical job")
                request = stored["request"]
        if not request["securityId"] and mobile_id and self.settings.mobile.get("session_file"):
            # An explicit private data path is a migration input, not an old-source-code import.
            saved = Path(self.settings.mobile["session_file"]).parent / "detail-contexts" / (str(mobile_id) + ".local.json")
            if saved.exists():
                context = json.loads(saved.read_text(encoding="utf-8-sig"))
                if context.get("canonical_id") and canonical_id and context["canonical_id"] != canonical_id:
                    raise ValueError("Saved detail context maps to a different canonical job")
                request = context.get("request") or request
        if not request.get("securityId") and canonical_id:
            source_raw = store.source_raw(canonical_id)
            if source_raw.get("securityId"):
                request = {"securityId": source_raw["securityId"], "lid": source_raw.get("lid", ""), "query": ""}
        return canonical_id, str(mobile_id) if mobile_id else None, raw, request

    def _detail_response(self, store, response, metadata, operation, card=None):
        from .adapters.mobile_payload import detail_to_job
        from .adapters.records import JobRecord
        from .adapters.facts import active_days
        outcome = mobile_outcome(metadata, response)
        if outcome != "ok":
            if operation.get("id"):
                store.observe_status(operation["id"], "unknown", metadata.get("finished_at") or metadata["observed_at"],
                    {"status": metadata.get("status"), "business_code": response.get("code"), "reason": outcome}, task_id=operation.get("task_id"))
            return failure_result(metadata, response)
        payload = response.get("zpData") or {}
        returned_mobile_id = str((payload.get("jobBaseInfo") or {}).get("jobId"))
        if operation.get("mobile_id") and returned_mobile_id != str(operation["mobile_id"]):
            return {"outcome": "identity_mismatch", "requests": 1, "reason": "Returned mobile job ID differs from requested source ID"}
        if not operation.get("mobile_id"):
            operation["mobile_id"] = returned_mobile_id
        observed_at = metadata.get("finished_at") or metadata["observed_at"]
        previous = store.get(operation.get("id")) if operation.get("id") else None
        old = None
        if previous:
            values = {k: v for k, v in previous["metadata"].items() if k in JobRecord.model_fields and v is not None}
            old = JobRecord(platform=previous["namespace"], source_job_id=previous["source_id"], job_url=previous["source_url"],
                **values, job_description=previous["body"]["text"], detail_fetched_at=previous["body"]["fetched_at"] or "",
                description_updated_at=previous["body"]["changed_at"] or "", raw={**store.source_raw(previous["id"]),
                    "activity_observation": previous["recruiter_activity"].get("last_valid")})
        try:
            job = detail_to_job(response, existing=old, search_card=card or {"job_id": operation["mobile_id"]},
                                query=operation.get("query", ""), observed_at=observed_at)
        except ValueError as error:
            return {"outcome": "identity_mismatch", "requests": 1, "reason": str(error)}
        record = record_from_job(job)
        existing = store.get(record["id"])
        card_row = store.card(operation["mobile_id"])
        record["observations"]["first_seen_at"] = (existing or {}).get("observations", {}).get("first_seen_at") or (card_row or {}).get("first_seen_at") or observed_at
        record["observations"]["last_list_seen_at"] = (card_row or {}).get("last_seen_at") or (existing or {}).get("observations", {}).get("last_list_seen_at")
        actual_full_body = bool((payload.get("jobBaseInfo") or {}).get("jobDesc", "").strip())
        acquisition = store.put(record, acquisition=actual_full_body)
        if not actual_full_body:
            acquisition = "no_full_body_received"
        store.alias("boss_mobile", operation["mobile_id"], record["id"])
        if card_row:
            store.conn.execute("UPDATE mobile_cards SET canonical_id=? WHERE mobile_job_id=?", (record["id"], operation["mobile_id"]))
        info = payload.get("jobBaseInfo") or {}
        boss = payload.get("bossBaseInfo") or {}
        state = "closed" if info.get("jobValidStatus") == 2 or boss.get("jobPageInvalid") else "open" if info.get("jobValidStatus") == 1 else "unknown"
        store.observe_status(record["id"], state, observed_at, {"status": metadata["status"], "business_code": 0, "job_status": job.job_status_desc,
                            "identity_match": True, "requested_source_id": record["source_id"], "returned_source_id": record["source_id"]}, task_id=operation.get("task_id"))
        active = job.recruiter_active_status
        store.observe_activity(record["id"], active, active_days(active), observed_at, "android_api_detail")
        store.private("job/" + record["id"] + "/" + observed_at, record["id"], observed_at, "normalized_source", job.raw)
        return {"outcome": "ok", "id": record["id"], "mobile_id": operation["mobile_id"], "state": state,
                "body_acquisition_outcome": acquisition, "new_bodies": int(acquisition == "first_body"),
                "changed_bodies": int(acquisition == "changed_body"), "requests": 1, "job": store.get(record["id"])}

    def _search(self, store, operation, task=None):
        client = self._mobile(store, operation, task)
        response = client.search(operation["query"], operation["city"], operation["page"], operation.get("sort", -1), operation.get("filters"))
        metadata = {**client.last_response, "finished_at": now()}
        return self._search_response(store, response, metadata, operation)

    def _detail(self, store, operation, task=None):
        ident, mobile_id, card, request = self._detail_context(store, operation.get("id"), operation.get("mobile_id"))
        operation.update(id=ident, mobile_id=mobile_id, query=request.get("query", ""))
        record = store.get(ident) if ident else None
        skip = self._detail_skip(record, operation)
        if skip:
            return {"outcome": skip, "id": ident, "requests": 0, "fetched_at": record["body"]["fetched_at"]}
        if not request.get("securityId"):
            return {"outcome": "context_missing", "id": ident, "mobile_id": mobile_id, "requests": 0}
        client = self._mobile(store, operation, task)
        response = client.detail(request["securityId"], request.get("lid", ""), request.get("query", ""))
        return self._detail_response(store, response, {**client.last_response, "finished_at": now()}, operation, card)

    @staticmethod
    def _detail_skip(record, operation):
        if not record or operation.get("force"):
            return None
        valid = record["status"].get("last_valid")
        if (valid or {}).get("outcome") == "closed" or not valid and (record["status"].get("saved_platform_fact") or {}).get("reported_state") == "closed":
            return "skipped_closed"
        if record["body"]["completeness"] == "complete":
            age = operation.get("max_body_age_hours")
            fetched = parse_time(record["body"]["fetched_at"])
            if age is not None and fetched and 0 <= (datetime.now(timezone.utc) - fetched).total_seconds() <= age * 3600:
                return "skipped_fresh_detail"
            if operation.get("details_mode") == "missing" and age is None:
                return "skipped_existing_body"
        return None

    def detail_candidates(self, store, cards, state, group):
        candidates = []
        seen = set(state.get("seen_details", []))
        for card in cards:
            mobile_id = card["job_id"]
            ident = store.resolve("boss_mobile", mobile_id) or card.get("canonical_id")
            if "mobile:" + mobile_id in seen or ident and ident in seen:
                continue
            record = store.get(ident) if ident else None
            operation = {"kind": "detail", "mobile_id": mobile_id, "id": ident, "detail_group": group,
                         "details_mode": state.get("details_mode", "missing"), "max_body_age_hours": state.get("max_body_age_hours")}
            if self._detail_skip(record, operation):
                continue
            rank = 0 if not record or not record["body"]["text"] else 1 if record["body"]["completeness"] != "complete" else 2
            fetched = parse_time(record["body"]["fetched_at"]) if record else None
            candidates.append((rank, fetched.timestamp() if fetched else 0, operation))
        return [operation for _, _, operation in sorted(candidates, key=lambda item: (item[0], item[1]))]

    def _execution_lock(self, channel):
        return self.settings.database.with_suffix("." + channel + ".executor.lock")

    def search(self, query, city="100010000", page=1, sort=-1, filters=None):
        if page < 1:
            raise ValueError("Page must be positive")
        with file_mutex(self._execution_lock("mobile")), SourceStore(self.settings.database, writable=True) as store:
            return self._search(store, {"kind": "search", "query": query, "city": city, "page": page, "sort": sort, "filters": filters or {}})

    def detail(self, canonical_id=None, mobile_id=None, max_body_age_hours=None, force=False):
        with file_mutex(self._execution_lock("mobile")), SourceStore(self.settings.database, writable=True) as store:
            return self._detail(store, {"kind": "detail", "id": canonical_id, "mobile_id": mobile_id,
                                       "max_body_age_hours": max_body_age_hours, "force": force})

    def _new_task(self, kind, plan, **extra):
        state = {"task_id": uuid.uuid4().hex, "kind": kind, "status": "queued", "created_at": now(), "updated_at": now(),
                 "plan": plan, "cursor": 0, "results": [], "cancel_requested": False, "seen_details": [], "inflight": None, **extra}
        with SourceStore(self.settings.database, writable=True) as store:
            store.save_task(state)
        return state["task_id"]

    def refresh(self, ids=None, *, mode="anonymous", max_requests=12, repeat=False, max_seconds=None,
                stale_hours=None, limit=12, include_closed=False, retry_after_seconds=60):
        if not ids and stale_hours is None:
            raise ValueError("Refresh needs explicit source IDs or an explicit status-age threshold")
        plan = [{"kind": "anonymous" if mode == "anonymous" else "detail", "id": i} for i in dict.fromkeys(ids or [])]
        maintenance = None
        if stale_hours is not None:
            maintenance = {"hours": stale_hours, "ids": ids, "limit": limit, "include_closed": include_closed,
                           "retry_after_seconds": retry_after_seconds}
            with SourceStore(self.settings.database) as store:
                from .selection import stale_status_jobs
                plan = [{"kind": "anonymous" if mode == "anonymous" else "detail", "id": item["id"]} for item in stale_status_jobs(store, **maintenance)]
        ident = self._new_task("anonymous" if mode == "anonymous" else "detail",
            plan, repeat=repeat, template_plan=deepcopy(plan), maintenance=maintenance, cycle=0,
            limits={"max_requests": max_requests if repeat else None, "max_seconds": max_seconds})
        return self.run_task(ident, max_requests)

    def collect(self, queries, city="100010000", pages=3, details_per_page=1, max_requests=12, max_body_age_hours=None, details_mode="missing", repeat=False, max_seconds=None):
        if pages < 1 or details_per_page < 0:
            raise ValueError("Pages must be positive and detail quota nonnegative")
        if repeat and details_mode == "refresh" and max_body_age_hours is None:
            raise ValueError("Repeated detail refresh needs an explicit body-age cache threshold")
        plan = [{"kind": "search", "query": query, "query_slot": slot, "city": city, "page": page}
                for slot, query in enumerate(queries) for page in range(1, pages + 1)]
        ident = self._new_task("collect", plan, details_per_page=details_per_page, max_body_age_hours=max_body_age_hours, details_mode=details_mode,
                              repeat=repeat, template_plan=deepcopy(plan), cycle=0,
                              limits={"max_requests": max_requests if repeat else None, "max_seconds": max_seconds})
        return self.run_task(ident, max_requests)

    def run_task(self, ident, max_requests=12, *, resume=False, extend_max_requests=None, extend_max_seconds=None):
        if max_requests is not None and max_requests < 1:
            raise ValueError("A task execution must have a positive request budget")
        with SourceStore(self.settings.database) as inspection:
            observed = inspection.task(ident)
        channel = "anonymous" if observed and observed["kind"] == "anonymous" else "mobile"
        with file_mutex(self._execution_lock(channel)), SourceStore(self.settings.database, writable=True) as store:
            state = store.task(ident)
            if state is None:
                raise ValueError("Unknown task")
            if extend_max_requests is not None:
                state.setdefault("limits", {})["max_requests"] = extend_max_requests
            if extend_max_seconds is not None:
                state.setdefault("limits", {})["max_seconds"] = extend_max_seconds
            state.setdefault("started_epoch", time.time())
            if self._limit_reached(store, state):
                store.save_task(state)
                return {"task_id": ident, "status": state["status"], "new_requests": 0, "reason": state["reason"]}
            if state["status"] == "succeeded":
                return {"task_id": ident, "status": "succeeded", "new_requests": 0, "cursor": state["cursor"]}
            if state["status"] in ("attention_required", "stopped", "failed", "running") and not resume:
                return {"task_id": ident, "status": state["status"], "new_requests": 0, "resume_required": True}
            state["cancel_requested"] = False
            state["resume_explicit"] = bool(resume)
            state["status"] = "running"
            store.save_task(state, clear_cancel=resume)
            self._report("task_started", task_id=ident, kind=state["kind"], request_budget=max_requests)
            before = store.conn.execute("SELECT count(*) FROM requests WHERE task_id=?", (ident,)).fetchone()[0]
            run_budget = max_requests if max_requests is not None else 2**63-1
            try:
                if state["kind"] == "anonymous":
                    asyncio.run(self._anonymous_task(store, state, run_budget))
                else:
                    self._mobile_task(store, state, run_budget, before)
            except KeyboardInterrupt:
                state["status"] = "stopped"
                state["reason"] = "operator_interrupt"
            except InterruptedError:
                if state["status"] != "limit_reached":
                    state["status"], state["reason"] = "stopped", "operator_cancelled"
            except Exception as error:
                state["status"] = "failed"
                state["reason"] = type(error).__name__ + ": " + str(error)[:200]
            finally:
                store.save_task(state)
            count = store.conn.execute("SELECT count(*) FROM requests WHERE task_id=?", (ident,)).fetchone()[0]
            return {"task_id": ident, "status": state["status"], "new_requests": count - before, "total_requests": count,
                    "cursor": state["cursor"], "planned_operations": len(state["plan"]), "last_result": state["results"][-1] if state["results"] else None,
                    "reason": state.get("reason")}

    def _cancelled(self, store, state):
        current = store.task(state["task_id"])
        if current and current.get("cancel_requested"):
            state["cancel_requested"] = True
            return True
        return False

    def _limit_reached(self, store, state):
        limits = state.get("limits") or {}
        count = store.conn.execute("SELECT count(*) FROM requests WHERE task_id=?", (state["task_id"],)).fetchone()[0]
        reached = limits.get("max_requests") is not None and count >= limits["max_requests"]
        expired = limits.get("max_seconds") is not None and time.time() - state.get("started_epoch", time.time()) >= limits["max_seconds"]
        if reached or expired:
            state["status"], state["reason"] = "limit_reached", "request_limit" if reached else "time_limit"
            return True
        return False

    def _next_cycle(self, store, state):
        state["cycle"] = state.get("cycle", 0) + 1
        if state.get("maintenance"):
            from .selection import stale_status_jobs
            selected = stale_status_jobs(store, **state["maintenance"])
            state["plan"] = [{"kind": "anonymous" if state["kind"] == "anonymous" else "detail", "id": item["id"]} for item in selected]
        else:
            state["plan"] = deepcopy(state["template_plan"])
        state.update(cursor=0, seen_details=[], completed_details=[], detail_candidates={})
        store.save_task(state)
        self._report("cycle_started", task_id=state["task_id"], cycle=state["cycle"], operations=len(state["plan"]))

    def _idle(self, store, state):
        state["status"] = "waiting"
        store.save_task(state)
        until = time.time() + 60
        while time.time() < until:
            if self._cancelled(store, state) or self._limit_reached(store, state):
                return False
            remaining = (state.get("limits") or {}).get("max_seconds")
            duration_left = max(0, remaining - (time.time() - state["started_epoch"])) if remaining is not None else 5
            time.sleep(min(5, duration_left, max(0, until-time.time())))
        state["status"] = "running"
        return True

    def _mobile_task(self, store, state, budget, before):
        if state.get("inflight"):
            inflight = state["inflight"]
            row = store.conn.execute("SELECT payload_json FROM private_sources WHERE origin_key=?", ("response/" + inflight["request_id"],)).fetchone()
            if row:
                saved = json.loads(row[0])
                operation = saved["operation"]
                result = self._search_response(store, saved["response"], saved["metadata"], operation) if operation["kind"] == "search" else self._detail_response(store, saved["response"], saved["metadata"], operation)
                self._complete_operation(store, state, operation, result)
                if result["outcome"] in ATTENTION:
                    state["status"], state["reason"] = "attention_required", result["outcome"]
                    return
            else:
                store.finish_request(inflight["request_id"], {"status": None, "error": "interrupted_without_response"}, "unknown_interrupted")
                if not state.get("resume_explicit"):
                    state["status"], state["reason"] = "attention_required", "interrupted_request_without_archived_response"
                    return
                state["results"].append({"operation": inflight["operation"], "outcome": "unknown_interrupted", "requests": 0})
                state["inflight"] = None
                store.save_task(state)
        calls_in_batch = state.get("calls_in_batch", 0)
        quota = state.get("batch_quota") or random.randint(*self.settings.mobile.get("batch_requests", (10, 14)))
        state.update(calls_in_batch=calls_in_batch,batch_quota=quota)
        store.save_task(state)
        while True:
            if self._limit_reached(store, state):
                return
            if state["cursor"] >= len(state["plan"]):
                if not state.get("repeat"):
                    break
                self._next_cycle(store, state)
                if not state["plan"]:
                    if not self._idle(store, state):
                        state["status"] = "stopped" if state.get("cancel_requested") else state["status"]
                        return
                    continue
            if self._cancelled(store, state):
                state["status"] = "stopped"
                return
            used = store.conn.execute("SELECT count(*) FROM requests WHERE task_id=?", (state["task_id"],)).fetchone()[0] - before
            if used >= budget:
                state["status"] = "ready"
                return
            if calls_in_batch >= quota:
                wait_until = state.get("rest_until_epoch") or time.time() + random.uniform(*self.settings.mobile.get("batch_pause_seconds", (360, 480)))
                state["rest_until_epoch"] = wait_until
                store.save_task(state)
                while time.time() < wait_until:
                    if self._limit_reached(store, state):
                        return
                    if self._cancelled(store, state):
                        state["status"] = "stopped"
                        return
                    time.sleep(min(5, wait_until - time.time()))
                calls_in_batch = 0
                quota = random.randint(*self.settings.mobile.get("batch_requests", (10, 14)))
                state.pop("rest_until_epoch", None)
                state.update(calls_in_batch=calls_in_batch,batch_quota=quota)
                store.save_task(state)
            operation = state["plan"][state["cursor"]]
            operation["task_id"] = state["task_id"]
            if operation.get("skip"):
                result = {"outcome": "skipped_page_end", "requests": 0}
            elif operation["kind"] == "search":
                result = self._search(store, operation, state)
            else:
                current_id = operation.get("id") or store.resolve("boss_mobile", operation.get("mobile_id"))
                result = {"outcome": "skipped_same_task_detail", "id": current_id, "requests": 0} if current_id and current_id in state.get("completed_details", []) else self._detail(store, operation, state)
            calls_in_batch += result.get("requests", 0)
            state.update(calls_in_batch=calls_in_batch, batch_quota=quota)
            self._complete_operation(store, state, operation, result)
            if result["outcome"] in ATTENTION:
                state["status"], state["reason"] = "attention_required", result["outcome"]
                return
        state["status"] = "completed_with_unknowns" if state.get("unresolved") else "succeeded"

    def _complete_operation(self, store, state, operation, result):
        from .store import summarize_result
        reference = "response/" + state["inflight"]["request_id"] if state.get("inflight") else None
        state["results"].append(summarize_result({"operation": operation, **result, "source_response": reference}))
        totals = state.setdefault("totals", {})
        totals["operations"] = totals.get("operations", 0) + 1
        totals["successful_operations"] = totals.get("successful_operations", 0) + int(result["outcome"] in ("ok", "open", "closed"))
        totals["request_attempts"] = store.conn.execute("SELECT count(*) FROM requests WHERE task_id=?", (state["task_id"],)).fetchone()[0]
        for name in ("new_bodies", "changed_bodies"):
            totals[name] = totals.get(name, 0) + result.get(name, 0)
        self._report("operation_finished", task_id=state["task_id"], kind=operation["kind"], outcome=result["outcome"],
                     requests=result.get("requests", 0), id=result.get("id"), cursor=state["cursor"])
        key = operation["kind"] + ":" + str(operation.get("mobile_id") or operation.get("id") or (operation.get("query"), operation.get("query_slot"), operation.get("page")))
        valid = result["outcome"] in ("ok", "open", "closed", "skipped_closed", "skipped_fresh_detail", "skipped_existing_body", "skipped_same_task_detail", "skipped_page_end")
        unresolved = state.setdefault("unresolved", {})
        if valid:
            unresolved.pop(key, None)
        else:
            unresolved[key] = {"operation": operation, "outcome": result["outcome"]}
        if result["outcome"] in ATTENTION:
            state["inflight"] = None
            store.save_task(state)
            return
        if operation["kind"] == "search" and result["outcome"] == "ok" and state["kind"] == "collect":
            quota = state.get("details_per_page", 0)
            group = str(operation.get("query_slot", 0)) + ":" + str(operation["page"])
            candidates = self.detail_candidates(store, result["cards"], state, group)
            added = candidates[:quota]
            state.setdefault("detail_candidates", {})[group] = candidates[quota:]
            for selected in added:
                state["seen_details"].append("mobile:" + selected["mobile_id"])
                if selected.get("id"):
                    state["seen_details"].append(selected["id"])
            state["plan"][state["cursor"] + 1:state["cursor"] + 1] = added
            if result["has_more"] is False:
                for future in state["plan"][state["cursor"] + 1:]:
                    if future["kind"] == "search" and future.get("query_slot") == operation.get("query_slot"):
                        future["skip"] = True
        if operation["kind"] == "detail":
            if result.get("id") and result["outcome"] == "ok":
                completed = state.setdefault("completed_details", [])
                if result["id"] not in completed:
                    completed.append(result["id"])
                if result["id"] not in state["seen_details"]:
                    state["seen_details"].append(result["id"])
            group = operation.get("detail_group")
            pending = state.get("detail_candidates", {}).get(group, []) if group else []
            if result.get("requests", 0) == 0 and pending:
                replacement = pending.pop(0)
                state["plan"].insert(state["cursor"] + 1, replacement)
                state["seen_details"].append("mobile:" + replacement["mobile_id"])
        state["cursor"] += 1
        state["inflight"] = None
        store.save_task(state)

    async def _anonymous_task(self, store, state, budget):
        from .browser import AnonymousSession
        quota = self.settings.anonymous.get("batch_requests",12)
        total_started = store.conn.execute("SELECT count(*) FROM requests WHERE task_id=?",(state["task_id"],)).fetchone()[0]
        state["anonymous_calls_in_batch"] = max(0,total_started-state.get("anonymous_completed_batches",0)*quota)
        while not state["plan"]:
            if not state.get("repeat"):
                state["status"] = "succeeded"
                return
            if not self._idle(store, state):
                state["status"] = "stopped" if state.get("cancel_requested") else state["status"]
                return
            self._next_cycle(store, state)
        async with AnonymousSession(self.settings.anonymous, store, task_id=state["task_id"], budget=budget,
                                    cancelled=lambda: self._cancelled(store, state) or self._limit_reached(store, state)) as browser:
            while True:
                if self._limit_reached(store, state):
                    return
                if state["cursor"] >= len(state["plan"]):
                    if not state.get("repeat"):
                        break
                    self._next_cycle(store, state)
                    if not state["plan"]:
                        if not self._idle(store, state):
                            state["status"] = "stopped" if state.get("cancel_requested") else state["status"]
                            return
                        continue
                if self._cancelled(store, state):
                    state["status"] = "stopped"
                    return
                if len(browser.requests) >= budget:
                    state["status"] = "ready"
                    return
                quota = self.settings.anonymous.get("batch_requests", 12)
                if state.get("anonymous_calls_in_batch", 0) >= quota:
                    until = state.get("anonymous_rest_until_epoch") or time.time() + random.uniform(*self.settings.anonymous.get("batch_pause_seconds", (15, 19)))
                    state["anonymous_rest_until_epoch"] = until
                    store.save_task(state)
                    while time.time() < until:
                        if self._cancelled(store, state) or self._limit_reached(store, state):
                            state["status"] = "stopped" if state.get("cancel_requested") else state["status"]
                            return
                        await asyncio.sleep(min(1, until-time.time()))
                    state["anonymous_calls_in_batch"] = 0
                    state["anonymous_completed_batches"] = state.get("anonymous_completed_batches",0)+1
                    state.pop("anonymous_rest_until_epoch", None)
                    store.save_task(state)
                operation = state["plan"][state["cursor"]]
                record = store.get(operation["id"])
                if record is None:
                    result = {"outcome": "missing_source_job", "requests": 0, "id": operation["id"]}
                else:
                    result = await browser.refresh(record)
                state["anonymous_calls_in_batch"] = state.get("anonymous_calls_in_batch", 0) + result.get("requests", 0)
                self._complete_operation(store, state, operation, result)
                if result["outcome"] in ATTENTION:
                    state["status"], state["reason"] = "attention_required", result["outcome"]
                    return
            state["status"] = "completed_with_unknowns" if state.get("unresolved") else "succeeded"

    def replay(self, file, kind, *, observed_at=None, url=None, title=None):
        path = Path(file).resolve()
        value = json.loads(path.read_text(encoding="utf-8-sig")) if path.suffix != ".html" else {"text": path.read_text(encoding="utf-8-sig"), "url": url, "status": None}
        observed_at = observed_at or value.get("observed_at")
        if kind == "page":
            from .browser import page_result
            from .contract import canonical_web_id
            fetched = value.get("fetched") or value
            requested = url or fetched.get("url")
            source_id = canonical_web_id(requested)
            record = {"id": "boss:" + (source_id or "unknown"), "source_id": source_id, "metadata": {"title": title}}
            result = page_result(fetched, record)
            result["requests"] = 0
            return result
        response = value.get("response") or value
        metadata = value.get("response_metadata") or (value.get("source") or {}).get("response") or value.get("metadata") or {}
        outcome = mobile_outcome(metadata, response)
        if outcome != "ok":
            return {**failure_result(metadata, response), "requests": 0}
        if kind == "search":
            from .adapters.mobile_payload import normalize_search
            return {"outcome": "ok", "requests": 0, **normalize_search(response)}
        if not parse_time(observed_at):
            raise ValueError("Detail replay needs its actual archived observation time")
        from .adapters.mobile_payload import detail_to_job
        return {"outcome": "ok", "requests": 0, "job": record_from_job(detail_to_job(response, observed_at=observed_at))}
