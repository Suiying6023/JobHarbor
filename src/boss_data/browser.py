"""Anonymous browser session for job status and activity observations."""
from __future__ import annotations

import asyncio
import importlib
import random
import time
from pathlib import Path
from urllib.parse import urlsplit

from .adapters.facts import active_days
from .adapters.page import FETCH_JOB_JS, classify, evidence_for_store, read_job_page, same_job
from .contract import canonical_web_id, now, public_evidence
from .local_files import file_mutex


def page_result(fetched, record):
    evidence = read_job_page(fetched)
    result = classify(evidence, title=record["metadata"].get("title"))
    returned = canonical_web_id(fetched.get("url"))
    identity = returned == record["source_id"]
    title_match = same_job(evidence.get("h1"), record["metadata"].get("title"))
    reason = evidence.get("reason") or result.reason
    state = "closed" if result.state == "gone" else result.state if result.state in ("open", "closed") else "unknown"
    if state in ("open", "closed") and result.state != "gone" and not identity:
        state, reason = "unknown", "canonical_identity_mismatch"
    if state in ("open", "closed") and (fetched.get("status") not in (200, 404) or result.state != "gone" and fetched.get("status") != 200):
        state, reason = "unknown", "missing_or_unsuccessful_http_status"
    outcome = state
    if evidence.get("reason") == "login":
        outcome = "login_expired"
    elif evidence.get("status") == 429:
        outcome = "rate_limited"
    elif evidence.get("reason") == "restricted":
        outcome = "restricted"
    elif result.state == "challenged":
        outcome = "verification_required"
    elif evidence.get("reason") == "fetch_error":
        outcome = "transport_error"
    raw = public_evidence(evidence_for_store(evidence))
    raw.update(identity_match=identity, requested_source_id=record["source_id"], returned_source_id=returned,
               title_match=title_match, state=state)
    return {"id": record["id"], "state": state, "outcome": outcome, "reason": reason,
            "evidence": raw, "body_acquisitions": 0}


class AnonymousSession:
    def __init__(self, settings, store, *, task_id=None, budget=12, cancelled=None):
        self.settings, self.store, self.task_id = settings, store, task_id
        self.budget = budget
        self.cancelled = cancelled or (lambda: False)
        self.driver = self.context = self.page = self.guard = None
        self.current = None
        self.requests = []
        self.operation_requests = []
        self.last_started = None
        latest = self.store.conn.execute("SELECT started_at FROM requests WHERE kind='anonymous_status' AND egress=? ORDER BY started_at DESC LIMIT 1", (settings.get("egress_label", "anonymous"),)).fetchone()
        from .contract import parse_time
        self.last_started_wall = parse_time(latest[0]).timestamp() if latest and parse_time(latest[0]) else None
        self.first = True

    async def __aenter__(self):
        if not self.settings.get("profile"):
            raise ValueError("Anonymous route needs an explicit profile directory")
        profile = Path(self.settings["profile"]).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        self.guard = file_mutex(profile.with_name(profile.name + ".service.lock"))
        self.guard.__enter__()
        try:
            api = importlib.import_module(self.settings.get("driver", "patchright") + ".async_api")
            self.driver = await api.async_playwright().start()
            proxy = self.settings.get("proxy")
            self.context = await self.driver.chromium.launch_persistent_context(str(profile),
                channel=self.settings.get("channel", "chrome"), headless=self.settings.get("headless", False),
                no_viewport=True, proxy={"server": proxy.replace("socks5h://", "socks5://")} if proxy else None,
                args=["--window-position=" + self.settings["window_position"]] if self.settings.get("window_position") else [])
            names = {c["name"] for c in await self.context.cookies(["https://www.zhipin.com/"])}
            if names.intersection({"wt", "wt2", "geek_zp_token", "zp_at", "bst"}):
                raise ValueError("Anonymous profile contains account credentials; use a separate logged-out profile")
            await self.context.route("**/*", self._route)
            self.context.on("response", self._response)
            self.context.on("requestfailed", self._failed)
            self.page = await self.context.new_page()
            return self
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        try:
            if self.context:
                await self.context.close()
        finally:
            self.context = None
            try:
                if self.driver:
                    await self.driver.stop()
            finally:
                self.driver = None
                if self.guard:
                    self.guard.__exit__(None, None, None)
                    self.guard = None

    async def _route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        boss = parsed.hostname and (parsed.hostname == "zhipin.com" or parsed.hostname.endswith(".zhipin.com"))
        allowed = boss and (request.resource_type == "document" or request.resource_type in ("fetch", "xhr") and parsed.path.startswith("/job_detail/"))
        if not allowed or len(self.requests) >= self.budget or self.cancelled():
            await route.abort()
            return
        metadata = {"observed_at": now(), "url": request.url, "egress": self.settings.get("egress_label", "anonymous")}
        ident = self.store.begin_request(metadata, kind="anonymous_status", task_id=self.task_id, canonical_id=self.current["id"])
        entry = {"request": request, "id": ident, "metadata": metadata, "response": None}
        self.requests.append(entry)
        self.operation_requests.append(entry)
        self.last_started = time.monotonic()
        self.last_started_wall = time.time()
        await route.continue_()

    def _response(self, response):
        for entry in self.requests:
            if entry["request"] == response.request:
                entry["response"] = response
                entry["metadata"].update(status=response.status, url=response.url, finished_at=now())
                self.store.finish_request(entry["id"], entry["metadata"], "redirect" if 300 <= response.status < 400 else "response_received")
                break

    def _failed(self, request):
        for entry in self.requests:
            if entry["request"] == request:
                entry["metadata"].update(transport_error="BrowserRequestFailed", finished_at=now())
                self.store.finish_request(entry["id"], entry["metadata"], "transport_error")
                break

    async def refresh(self, record):
        if record["namespace"] != "boss" or canonical_web_id(record.get("source_url")) != record["source_id"]:
            return {"id": record["id"], "outcome": "unsupported_source_url", "state": "unknown", "requests": 0}
        delay = random.uniform(*self.settings.get("interval_seconds", (15, 17)))
        if self.last_started is not None:
            await asyncio.sleep(max(0, delay - (time.monotonic() - self.last_started)))
        elif self.last_started_wall is not None:
            await asyncio.sleep(max(0, delay - (time.time() - self.last_started_wall)))
        if self.cancelled() or len(self.requests) >= self.budget:
            return {"id": record["id"], "outcome": "stopped", "state": "unknown", "requests": 0}
        self.current = record
        self.operation_requests = []
        started_at = now()
        try:
            if self.first:
                response = await self.page.goto(record["source_url"], wait_until="domcontentloaded", timeout=30000)
                self.first = False
                if response is None:
                    raise ValueError("Navigation did not return a document response")
                fetched = {"status": response.status, "url": response.url, "text": await response.text()}
            else:
                fetched = await asyncio.wait_for(self.page.evaluate(FETCH_JOB_JS, record["source_url"]), timeout=31)
        except Exception as error:
            fetched = {"status": None, "url": record["source_url"], "reason": "fetch_error", "error": type(error).__name__}
        result = page_result(fetched, record)
        observed_at = now()
        result.update(observed_at=observed_at, request_started_at=started_at, requests=len(self.operation_requests))
        self.store.observe_status(record["id"], result["state"], observed_at, result["evidence"], task_id=self.task_id)
        text = result["evidence"].get("active_time") if result["evidence"].get("identity_match") and fetched.get("status") == 200 and result["outcome"] not in ("verification_required", "restricted", "login_expired", "rate_limited") else ""
        self.store.observe_activity(record["id"], text, active_days(text or ""), observed_at, "anonymous_status")
        for i, entry in enumerate(self.operation_requests):
            metadata = entry["metadata"]
            self.store.finish_request(entry["id"], metadata, result["outcome"] if i == len(self.operation_requests) - 1 else "redirect")
        if self.settings.get("archive_response"):
            self.store.private("anonymous/" + (self.operation_requests[-1]["id"] if self.operation_requests else observed_at),
                record["id"], observed_at, "anonymous_response", {"fetched": fetched, "observed_at": observed_at})
        self.store.conn.commit()
        return result
