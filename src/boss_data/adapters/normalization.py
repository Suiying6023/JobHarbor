import unicodedata
from .facts import parse_salary, active_days as active_days_from_desc

def split_location(city: str, district: str, business: str) -> str:
    return "·".join(v for v in (city, district, business) if v)


def normalized_description(value: str) -> str:
    """Compare JD typography while retaining punctuation and letter case."""
    return " ".join(unicodedata.normalize("NFKC", value or "").split())


def merge_unique(left: list[str], right: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in [*left, *right]:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return output
