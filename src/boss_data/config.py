from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Settings:
    database: Path
    mobile: dict
    anonymous: dict

    @classmethod
    def load(cls, config=None, database=None):
        base = Path(config).resolve().parent if config else Path.cwd()
        value = {}
        if config:
            import yaml
            value = yaml.safe_load(Path(config).read_text(encoding="utf-8-sig")) or {}
        resolve = lambda path: str((base / Path(path)).resolve())
        mobile = dict(value.get("mobile") or {})
        anonymous = dict(value.get("anonymous") or {})
        if mobile.get("session_file"):
            mobile["session_file"] = resolve(mobile["session_file"])
        if anonymous.get("profile"):
            anonymous["profile"] = resolve(anonymous["profile"])
        db = Path(database).resolve() if database else Path(resolve(value.get("database", ".bosscli/source.sqlite3")))
        return cls(db, mobile, anonymous)
