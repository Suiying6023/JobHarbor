"""Parse source-page evidence and classify job status."""
from __future__ import annotations
import re
import hashlib
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from .htmlpage import query
from .facts import CLOSED_MARKERS

MIN_JOB_HTML_LEN = 60_000


GONE_MARKERS = ("您访问的页面不存在", "页面不存在", "404 Not Found")


CHALLENGE_MARKERS = ("security-check", "请稍候", "security.html")


VERIFY_MARKERS = ("安全验证", "verify.html", "passport/zp/verify")


OPEN_VALUES = ("招聘中", "最新")


PAGE_SELECTORS = {
    "h1": ".info-primary .name h1",
    "h1_fallback": ".job-banner h1",
    "job_status": ".job-banner .job-status",
    "active_time": ".boss-active-time",
    "captcha": ".verify-wrap,.captcha,.slider-verify,#nc_1_n1z",
    "notice": ".code-block.show",
}


_RESTRICTION = re.compile(
    r"账[户号](?:可能)?存在异常|IP\s*(?:地址\s*)?存在异常|环境存在异常|"
    r"访问(?:过于|过)?频繁|操作过于频繁|操作太频繁|请先登录|未登录")


_FETCH_JOB_JS_TEMPLATE = """async (url) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), __FETCH_TIMEOUT_MS__);
  let response;
  try {
    response = await fetch(url, {credentials: 'include', signal: controller.signal});
    const text = await response.text();
    return {ok: true, status: response.status, url: response.url || '', text};
  } catch (e) {
    return {ok: false, reason: 'fetch_error',
            error: controller.signal.aborted ? 'fetch timeout' : String(e),
            status: response?.status, url: response?.url || url};
  } finally {
    clearTimeout(timer);
  }
}"""


FETCH_JOB_JS = _FETCH_JOB_JS_TEMPLATE.replace("__FETCH_TIMEOUT_MS__", "30000")


@dataclass(slots=True)
class JobStatus:
    """一条岗位的观测结果。`state` 是唯一判据，其余都是证据。"""

    state: str                      # open | closed | gone | challenged | unknown
    h1: str | None = None
    job_status: str | None = None
    active_time: str | None = None
    http_status: int | None = None
    length: int = 0
    url: str = ""
    reason: str = ""
    # 这一条是不是**自愈后**才拿到的（撞过 verify 页，导航一次后恢复）。
    recovered: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def closed(self) -> bool | None:
        """True=已关闭，False=在招，None=判不了。"""
        if self.state == "closed":
            return True
        if self.state == "open":
            return False
        return None

    @property
    def invalid(self) -> bool | None:
        """落库用的三态：True=失效，False=有效，None=未知（**不改库**）。"""
        if self.state in ("closed", "gone"):
            return True
        if self.state == "open":
            return False
        return None


def parse_job_page(html: str) -> dict:
    """从岗位页 HTML 里取容器。纯函数。

    找不到 `.info-primary .name h1` 时退回 `.job-banner h1`：平台改过一次结构，
    两份选择器都在实测里出现过，留一条退路比在判定处猜更便宜。
    """
    got = query(html, PAGE_SELECTORS)
    h1 = got["h1"]["text"] or got["h1_fallback"]["text"]
    notice = got["notice"]["text"] or ""
    visible = re.sub(r"<(script|style|noscript)\b[^>]*>.*?</\1>", " ", html or "", flags=re.S | re.I)
    visible = re.sub(r"<[^>]+>", " ", visible)
    restriction = _RESTRICTION.search(notice or visible) if not h1 else None
    return {
        "h1": h1,
        "job_status": got["job_status"]["text"],
        "active_time": got["active_time"]["text"],
        "captcha_present": bool(got["captcha"]["present"]),
        "restriction_notice": bool(restriction),
        "page_prompt": notice or (restriction.group(0) if restriction else ""),
    }


def read_job_page(fetched: dict) -> dict:
    """把 `FETCH_JOB_JS` 的返回变成 `classify` 吃的证据。纯函数。

    短页面的三种形态在这里分开（下架 / 挑战 / 验证），因为它们的处置完全相反。
    """
    if not fetched:
        return {"ok": False, "reason": "fetch_error", "error": "空返回"}
    if fetched.get("error") or fetched.get("reason") == "fetch_error":
        return {"ok": False, "reason": "fetch_error",
                "error": fetched.get("error") or "fetch_error",
                "status": fetched.get("status"), "url": fetched.get("url") or ""}

    text = fetched.get("text") or ""
    status = fetched.get("status")
    final_url = fetched.get("url") or ""
    path = urlsplit(final_url).path
    length = len(text)
    # `text` 一路带着：落库前由 `evidence_for_store` 换成摘要。
    # 摘要有用——同一份内容重复出现（平台给推荐页、缓存页）能一眼看出来。
    evidence: dict = {"status": status, "url": final_url, "len": length, "text": text}

    # Access signals apply to short error documents as well as full job pages.
    # Extract the containers first so a job's own text does not become a notice.
    evidence.update(parse_job_page(text))
    if status in (401, 403, 429):
        return {**evidence, "ok": False, "reason": "restricted", "challenged": True}
    if path == "/web/user" or path.startswith("/web/user/"):
        return {**evidence, "ok": False, "reason": "login", "challenged": True}

    # 正常岗位页的正文或脚本也会引用验证文案；它们不代表当前页面被拦截。
    # 保留真实验证地址和下方控件判定，只在没有岗位文档时用整页文案兜底。
    job_document = path.startswith("/job_detail/") and bool(evidence["h1"])
    if any(mark in final_url for mark in VERIFY_MARKERS) or \
            (not job_document and any(mark in text for mark in VERIFY_MARKERS)):
        return {**evidence, "ok": False, "reason": "verify", "challenged": True}
    if evidence["captcha_present"]:
        return {**evidence, "ok": False, "reason": "verify", "challenged": True}
    if evidence["restriction_notice"]:
        return {**evidence, "ok": False, "reason": "restricted", "challenged": True}
    if path == "/web/passport/zp/security.html":
        return {**evidence, "ok": False, "reason": "challenged", "challenged": True}
    if length < MIN_JOB_HTML_LEN:
        if status == 404 or any(mark in text for mark in GONE_MARKERS):
            return {**evidence, "ok": False, "reason": "gone", "gone": True}
        if any(mark in text for mark in CHALLENGE_MARKERS):
            return {**evidence, "ok": False, "reason": "challenged", "challenged": True}
        return {**evidence, "ok": False, "reason": "short"}

    evidence["ok"] = True
    # `gone` / `challenged` 两个布尔是给 `service_jobs.status_verdict` 用的：
    # 它按旧证据契约判（`evidence["gone"]`），而旧取值脚本**从来没给过**这个键，
    # 于是下架岗位在任务链路里一直被记成「判不出来」。这里一次补齐。
    evidence["challenged"] = bool(
        evidence.get("captcha_present") or evidence.get("restriction_notice")
        or status in (401, 403, 429))
    return evidence


def classify(evidence: dict, *, title: str | None = None) -> JobStatus:
    """把取值结果判成状态。纯函数，不碰网络。

    ⚠️ 判据是 `.job-status` 的**值**，不是节点空不空：在招是「招聘中」/「最新」，
    关闭是「职位已关闭」。按"有文本即关闭"判会把全部岗位判成关闭。
    """
    if not evidence:
        return JobStatus(state="unknown", reason="空证据")

    # These signals have priority even when a caller supplies a legacy reason.
    if evidence.get("status") in (401, 403, 429) or evidence.get("restriction_notice"):
        return JobStatus(state="challenged", reason="访问限制提示",
                         http_status=evidence.get("status"), length=evidence.get("len") or 0,
                         url=evidence.get("url") or "")

    if evidence.get("error"):
        return JobStatus(state="unknown", reason=f"fetch_error: {evidence['error']}"[:120],
                         http_status=evidence.get("status"), url=evidence.get("url") or "")

    reason = evidence.get("reason")
    if reason == "login":
        return JobStatus(state="challenged", reason="页面要求登录",
                         http_status=evidence.get("status"), length=evidence.get("len") or 0,
                         url=evidence.get("url") or "")
    if reason == "gone":
        return JobStatus(state="gone", reason="页面不存在/404",
                         http_status=evidence.get("status"), length=evidence.get("len") or 0)
    if reason == "verify":
        return JobStatus(state="challenged", reason="要求人工验证（verify.html）",
                         http_status=evidence.get("status"), length=evidence.get("len") or 0,
                         url=evidence.get("url") or "")
    if reason == "challenged":
        return JobStatus(state="challenged", reason="JS 挑战页",
                         http_status=evidence.get("status"), length=evidence.get("len") or 0)
    if reason == "short":
        return JobStatus(state="unknown", reason="短页面且无已知标志",
                         http_status=evidence.get("status"), length=evidence.get("len") or 0)
    if reason == "fetch_error":
        return JobStatus(state="unknown", reason="fetch 异常")

    status = evidence.get("status")
    if status in (401, 403, 429):
        return JobStatus(state="challenged", reason=f"HTTP {status}", http_status=status)
    if evidence.get("captcha_present"):
        return JobStatus(state="challenged", reason="出现验证控件", http_status=status)
    if evidence.get("restriction_notice"):
        return JobStatus(state="challenged", reason="限制提示文案", http_status=status)

    h1 = evidence.get("h1")
    # 身份校验：拿回来的必须是我们请求的那条岗位，否则判定会张冠李戴，
    # 而降级是单向的（误判关闭不可逆）。
    if title and h1 and not same_job(h1, title):
        return JobStatus(state="unknown", reason=f"身份不符: {h1[:20]}",
                         h1=h1, http_status=status, length=evidence.get("len") or 0)

    job_status = (evidence.get("job_status") or "").strip()
    common = dict(h1=h1, job_status=job_status or None,
                  active_time=evidence.get("active_time"),
                  http_status=status, length=evidence.get("len") or 0,
                  url=evidence.get("url") or "")
    if not job_status:
        return JobStatus(state="unknown", reason="缺 job-status 容器", **common)
    if any(value in job_status for value in CLOSED_MARKERS):
        return JobStatus(state="closed", **common)
    if any(value in job_status for value in OPEN_VALUES):
        return JobStatus(state="open", **common)
    # 平台加了新文案：不猜。猜错要么白刷一轮（便宜），要么误降一个在招岗位（不可逆）。
    return JobStatus(state="unknown", reason=f"未知状态文案: {job_status[:20]}", **common)


def same_job(h1: str | None, db_title: str | None) -> bool:
    """比较前 8 字：标题常有全角括号与后缀差异，全等太脆。"""
    left = " ".join((h1 or "").split())
    right = " ".join((db_title or "").split())
    if not left or not right:
        return False
    return left[:8] in right or right[:8] in left


def evidence_for_store(evidence: dict) -> dict:
    """落库前的证据：丢掉原始 HTML，只留能复算判定的东西。"""
    if not evidence:
        return {}
    trimmed = {key: value for key, value in evidence.items() if key != "text"}
    text = evidence.get("text")
    if isinstance(text, str):
        trimmed["text_len"] = len(text)
        trimmed["text_digest"] = hashlib.sha1(
            text.encode("utf-8", "replace")).hexdigest()[:12]
    return trimmed
