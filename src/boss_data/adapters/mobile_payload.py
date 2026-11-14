"""Normalize BOSS mobile search and detail responses."""
from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .records import JobRecord, Platform, job_from_row
from .normalization import active_days_from_desc, merge_unique, parse_salary, split_location
from .facts import mobile_activity_texts
from ..contract import retain_recruiter_activity


SEARCH_PATH = "/api/zpgeek/app/geek/search/cardlist"
RECOMMEND_PATH = "/api/zpgeek/app/geek/recommend/joblist"
JOB_FIELDS = (
    "jobId", "positionName", "jobDesc", "jobType", "jobValidStatus", "location", "locationName",
    "locationDesc", "areaDistrict", "businessDistrict", "address", "degree", "degreeName",
    "experience", "experienceName", "salaryDesc", "salaryMonth", "lowSalary", "highSalary",
    "brandId", "position", "positionCategory", "requiredSkills", "jobSkills", "proxyJob",
    "proxyType", "recruitmentCountDesc", "jobPubTimeDesc", "graduationTime", "deadLine",
    "latitude", "longitude", "department", "partTimeJobDesc", "performance",
)
BRAND_FIELDS = (
    "brandId", "encryptBrandId", "brandName", "comId", "comName", "industryName",
    "scaleName", "stageName", "multiIndustryName", "brandWelfare", "brandCertificate", "canJumpToBrandPage",
)
BOSS_FIELDS = ("bossId", "activeTimeDesc", "bossBehaviorLabels", "certStatus", "dimission",
               "jobPageInvalid", "jobPageInvalidDesc")


def _pick(value, keys):
    return {key: deepcopy(value[key]) for key in keys if key in value}


def _card(raw):
    position = raw.get("positionName") or raw.get("jobName") or {}
    title = position.get("name", "") if isinstance(position, dict) else position
    return {
        "job_id": str(raw["jobId"]), "title": title, "company": raw.get("company") or raw.get("brandName", ""),
        "city": raw.get("city") or raw.get("cityName", ""), "district": raw.get("areaDistrict", ""),
        "business_district": raw.get("areaBusinessName") or raw.get("businessDistrict", ""), "salary": raw.get("salaryDesc", ""),
        "experience": raw.get("experienceName") or raw.get("jobExperience", ""), "education": raw.get("degreeName") or raw.get("jobDegree", ""),
        "company_size": raw.get("scaleName") or raw.get("brandScaleName", ""), "financing_stage": raw.get("stageName") or raw.get("brandStageName", ""),
        "industry": raw.get("industryName") or raw.get("brandIndustry", ""), "labels": deepcopy(raw.get("jobLabels") or []),
        "summary": (raw.get("jobDesc") or {}).get("content") or raw.get("jobPreviewDesc") or raw.get("postDescription") or "",
        "recruiter_title": raw.get("title") or raw.get("bossTitle", ""), "recruiter_online": raw.get("online", False),
        "recruiter_active_status": raw.get("activeTime") or raw.get("activeTimeDesc") or raw.get("activeInfo") or "",
        "position_category": raw.get("positionCategory", ""),
    }


def normalize_search(data):
    """Return public cards and query choices; the client retains request context by jobId."""
    payload = data.get("zpData", data)
    if SEARCH_PATH in payload:
        payload = payload[SEARCH_PATH]["zpData"]
    groups = payload.get("cardList", [])
    cards = [_card(raw) for group in groups for raw in (group.get("positionSearchCardList") or [])]
    hints = _pick(payload, ("labelFilter", "isSupplySearch", "rcdType", "rcdIndex", "rcdTip"))
    if payload.get("nlpFilters"):
        hints["filter_groups"] = [{"name": item.get("name"), "value": item.get("value"),
                                   "groups": len(item.get("subLevelModelList", []))}
                                  for item in payload["nlpFilters"]]
    for key, fields in (
        ("correctQuery", ("originQuery", "correctWord", "prefix")),
        ("alternativeCityTip", ("title", "buttonText", "cityConfig")),
        ("citySuggestTip", ("title", "buttonText", "cityConfig")),
        ("relatedSearchTip", ("title", "words")),
        ("recommendQuery", ("title", "queryList")),
        ("switchSortTip", ("title", "currentSort", "targetSort")),
    ):
        if payload.get(key):
            hints[key] = _pick(payload[key], fields)
    return {
        "cards": cards, "count": len(cards), "sort": payload.get("sort"),
        "sorts": [_pick(item, ("code", "name", "nearest")) for item in payload.get("sorts", [])],
        "has_more": groups[0].get("hasMore") if groups else False,
        "filter_hints": hints,
    }


def detail_to_job(data, existing=None, search_card=None, query="", observed_at=None):
    """Map the returned mobile share path to the existing BOSS web job identity."""
    payload = data.get("zpData", data)
    info = payload["jobBaseInfo"]
    brand = payload.get("brandComInfo") or {}
    boss = payload.get("bossBaseInfo") or {}
    share = urlsplit(payload["bossCommonBizInfo"]["shareCopyLinkUrl"])
    match = re.fullmatch(r"/mpa/html/weijd/weijd-job/([A-Za-z0-9_~-]+)", share.path)
    if share.hostname not in ("m.zhipin.com", "www.zhipin.com") or match is None:
        raise ValueError("手机详情需要包含岗位ID的BOSS分享路径")
    source_id = match.group(1)
    if existing is None:
        job = JobRecord(platform=Platform.BOSS, source_job_id=source_id,
                        job_url=f"https://www.zhipin.com/job_detail/{source_id}.html",
                        title=info["positionName"])
    else:
        job = existing.model_copy(deep=True) if isinstance(existing, JobRecord) else job_from_row(existing)
        if job.source_job_id != source_id:
            raise ValueError("手机详情分享链接与已有岗位ID不一致")
    retain_recruiter_activity(job.raw, job.detail_fetched_at)
    card = _card(search_card) if search_card and "jobId" in search_card else (search_card or {})
    if card and card["job_id"] != str(info["jobId"]):
        raise ValueError("手机搜索卡片与详情的岗位ID不一致")
    now = observed_at or datetime.now(timezone.utc).isoformat()
    job.job_url = f"https://www.zhipin.com/job_detail/{source_id}.html"
    for field, key in (("title", "positionName"), ("city", "locationName"), ("district", "areaDistrict"),
                       ("business_district", "businessDistrict"), ("address", "address"),
                       ("education", "degreeName"), ("experience", "experienceName"),
                       ("recruitment_count", "recruitmentCountDesc"), ("position_name", "positionCategory")):
        if info.get(key):
            setattr(job, field, str(info[key]))
    for field, key in (("company_name", "brandName"), ("company_size", "scaleName"),
                       ("financing_stage", "stageName"), ("industry", "industryName")):
        if brand.get(key):
            setattr(job, field, str(brand[key]))
    if brand.get("encryptBrandId"):
        job.brand_id = str(brand["encryptBrandId"])
        job.company_url = f"https://www.zhipin.com/gongsi/{brand['encryptBrandId']}.html"
    if info.get("salaryDesc"):
        job.salary_text = info["salaryDesc"]
        job.salary_min_k, job.salary_max_k, job.salary_months = parse_salary(job.salary_text)
    if info.get("salaryMonth"):
        job.salary_months = int(info["salaryMonth"])
    job.location_text = split_location(job.city, job.district, job.business_district)
    for name in ("latitude", "longitude"):
        if name in info:
            setattr(job, name, info[name])
    active = min(mobile_activity_texts(boss),
                 key=lambda text: active_days_from_desc(text) if active_days_from_desc(text) is not None else float("inf"),
                 default="")
    job.recruiter_active_status = "今日活跃" if active.startswith("今日回复") else active
    job.recruiter_active_days = active_days_from_desc(active)
    job.recruiter_behavior_labels = deepcopy(boss.get("bossBehaviorLabels") or [])
    if boss.get("bossId"):
        job.recruiter_id = str(boss["bossId"])
    for field, key in (("recruiter_name", "name"), ("recruiter_title", "title")):
        if boss.get(key):
            setattr(job, field, str(boss[key]))
    if "certStatus" in boss:
        job.recruiter_certificated = boss["certStatus"] == 3
    if card:
        job.recruiter_online = card.get("recruiter_online", job.recruiter_online)
        job.labels = merge_unique(job.labels, card.get("labels", []))
    if "proxyJob" in info or "proxyType" in info:
        job.is_proxy_job = bool(info.get("proxyJob") or info.get("proxyType"))
    if "jobValidStatus" in info:
        job.job_invalid = info["jobValidStatus"] == 2
        job.job_status_desc = {1: "招聘中", 2: "职位已失效"}.get(info["jobValidStatus"], "")
    if boss.get("jobPageInvalid"):
        job.job_invalid = True
        job.job_status_desc = boss.get("jobPageInvalidDesc") or "职位已失效"
    job.skills = merge_unique(job.skills, info.get("requiredSkills") or [])
    job.welfare = merge_unique(job.welfare, (brand.get("brandWelfare") or {}).get("welfareList") or [])
    if query:
        job.matched_queries = merge_unique(job.matched_queries, [query])
    description = (info.get("jobDesc") or "").strip()
    if description:
        job.job_description = description
        job.detail_fetched_at = now
        job.raw["detail_access_verified"] = True
        job.raw["detail_source"] = {"kind": "boss_mobile_detail_response", "source_job_id": source_id,
                                    "fetched_at": now, "access_verified": True}
    facts = {field: info[key] for field, key in (("published_text", "jobPubTimeDesc"),
             ("graduation_text", "graduationTime"), ("deadline_text", "deadLine")) if info.get(key)}
    previous = job.raw.get("mobile") or {}
    mobile = {
        "transport": "android_api", "observed_at": now, "job_id": str(info["jobId"]), "facts": facts,
        "share_url": f"https://m.zhipin.com{share.path}",
        "detail": {"jobBaseInfo": _pick(info, JOB_FIELDS), "brandComInfo": _pick(brand, BRAND_FIELDS),
                   "bossBaseInfo": _pick(boss, BOSS_FIELDS)},
    }
    if card:
        mobile["list"] = deepcopy(card)
    elif previous.get("list"):
        mobile["list"] = previous["list"]
    job.raw["mobile"] = mobile
    if "jobType" in info:
        job.raw["jobType"] = info["jobType"]
    if card.get("labels"):
        job.raw["jobLabels"] = card["labels"]
    return job
