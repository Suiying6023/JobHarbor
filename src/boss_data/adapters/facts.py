"""Source salary, activity and status normalization."""
from __future__ import annotations

import re


_SALARY_RE = re.compile(
    r"(?P<lo>\d+(?:\.\d+)?)\s*[-~—至]\s*(?P<hi>\d+(?:\.\d+)?)\s*(?P<unit>[Kk万])"
    r"(?:[·x×](?P<months>\d+)薪)?")


_DAILY_RE = re.compile(r"(?P<lo>\d+)\s*[-~—至]\s*(?P<hi>\d+)\s*元/天")


_ACTIVE_DAYS: dict[str, int] = {
    "在线": 0,          # 最强活跃信号：当前正在线
    "刚刚活跃": 0,
    "今日活跃": 0,
    "昨日活跃": 1,
    "3日内活跃": 3,
    "本周活跃": 7,
    "2周内活跃": 14,
    "本月活跃": 30,
    "2月内活跃": 60,
    "3月内活跃": 90,
    "4月内活跃": 120,
    "5月内活跃": 150,
    "近半年活跃": 180,  # "半年"里没有数字，正则兜不住
    "半年前活跃": 180,  # 字面是"半年以前"，按 180 天处理（最不活跃的一档）
}


ACTIVE_DESC_DAYS: list[tuple[str, int]] = sorted(
    _ACTIVE_DAYS.items(), key=lambda kv: -len(kv[0]))


CLOSED_MARKERS: tuple[str, ...] = (
    "职位已关闭", "该职位已关闭", "已下线", "职位已下线",
    "职位已结束", "暂停招聘", "招聘已结束",
)


def parse_salary(text: str) -> tuple[float | None, float | None, int | None]:
    """返回 (下限K, 上限K, 薪月数)。日薪按 21.75 天折算成月薪，仅用于横向比较。"""
    value = (text or "").replace(" ", "")
    match = _SALARY_RE.search(value)
    if match:
        lo, hi = float(match.group("lo")), float(match.group("hi"))
        if match.group("unit") == "万":
            lo, hi = lo * 10, hi * 10
        months = int(match.group("months")) if match.group("months") else None
        return lo, hi, months
    daily = _DAILY_RE.search(value)
    if daily:
        return (round(int(daily.group("lo")) * 21.75 / 1000, 2),
                round(int(daily.group("hi")) * 21.75 / 1000, 2), None)
    return None, None, None


def is_daily_rate(text: str) -> bool:
    return bool(_DAILY_RE.search((text or "").replace(" ", "")))


def active_days(desc: str) -> int | None:
    value = (desc or "").strip()
    if not value:
        return None
    # Keep the reply label verbatim; positive replies today imply activity today.
    if re.fullmatch(r"今日回复[1-9]\d*\+?次", value):
        return 0
    reply = re.fullmatch(r"(\d+)\s*(分钟|小时)前回复", value)
    if reply:
        return int(reply.group(1)) // (1440 if reply.group(2) == "分钟" else 24)
    for phrase, days in ACTIVE_DESC_DAYS:
        if phrase in value:
            return days
    # 兜底：形如 "5日内活跃" / "2月内活跃" 的未列举组合。
    match = re.search(r"(\d+)\s*(日|天|周|月|年)", value)
    if not match:
        return None
    factor = {"日": 1, "天": 1, "周": 7, "月": 30, "年": 365}[match.group(2)]
    return int(match.group(1)) * factor


def mobile_activity_texts(boss: dict) -> list[str]:
    """Retain source activity and timed replies, excluding untimed reply quality."""
    return [text for text in [boss.get("activeTimeDesc"), *(boss.get("bossBehaviorLabels") or [])]
            if text and ("活跃" in text or text == "在线" or
                         ("回复" in text and active_days(text) is not None))]
