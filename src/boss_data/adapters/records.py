"""Extracted source record; product annotations are not part of this model."""
from __future__ import annotations
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from pydantic import BaseModel, Field

class Platform(StrEnum):
    BOSS = "boss"
    LIEPIN = "liepin"
    JOB51 = "job51"
    ZHILIAN = "zhilian"
    OFFICIAL = "official"
    COMMUNITY = "community"


class JobRecord(BaseModel):
    platform: Platform
    source_job_id: str
    job_url: str
    company_url: str = ""
    title: str
    company_name: str = ""
    city: str = ""
    district: str = ""
    business_district: str = ""
    location_text: str = ""
    salary_text: str = ""
    salary_min_k: float | None = None
    salary_max_k: float | None = None
    salary_months: int | None = None
    experience: str = ""
    education: str = ""
    company_size: str = ""
    financing_stage: str = ""
    industry: str = ""
    recruiter_name: str = ""
    recruiter_title: str = ""
    recruiter_active_status: str = ""
    recruiter_active_days: int | None = None
    recruiter_online: bool = False
    is_proxy_job: bool = False
    job_invalid: bool = False
    address: str = ""
    recruiter_certificated: bool = False
    recruiter_id: str = ""
    recruiter_behavior_labels: list[str] = Field(default_factory=list)
    brand_id: str = ""
    company_intro: str = ""
    company_founded_year: int | None = None
    # Original company registration fields.
    company_reg: dict[str, str] = Field(default_factory=dict)
    company_reg_updated_at: str = ""
    job_status_desc: str = ""
    position_name: str = ""
    recruitment_count: str = ""
    latitude: float | None = None
    longitude: float | None = None
    job_description: str = ""
    detail_fetched_at: str = ""
    description_updated_at: str = ""
    skills: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    welfare: list[str] = Field(default_factory=list)
    matched_queries: list[str] = Field(default_factory=list)
    source_rank: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def canonical_key(self) -> str:
        return f"{self.platform.value}:{self.source_job_id}"


def job_from_row(row) -> JobRecord:
    """Decode stored JSON fields and discard invalid collection times."""
    import json

    value = dict(row)
    for key in ("skills", "labels", "welfare", "matched_queries"):
        value[key] = json.loads(value.pop(key + "_json", "[]") or "[]")
    for key in ("raw", "company_reg"):
        value[key] = json.loads(value.pop(key + "_json", "{}") or "{}")
    if value.get("collected_at") and not _valid_time(value["collected_at"]):
        value.pop("collected_at")
    return JobRecord.model_validate(
        {key: item for key, item in value.items() if key in JobRecord.model_fields})


def _valid_time(value) -> bool:
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= datetime.now(timezone.utc)
