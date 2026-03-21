"""Entry types, validation, and common envelope for the awareness store."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class EntryType(str, Enum):
    STATUS = "status"
    ALERT = "alert"
    PATTERN = "pattern"
    SUPPRESSION = "suppression"
    CONTEXT = "context"
    PREFERENCE = "preference"
    NOTE = "note"


SEVERITY_RANK = {
    "info": 0,
    "warning": 1,
    "critical": 2,
}


def severity_rank(level: str) -> int:
    return SEVERITY_RANK.get(level, -1)


def make_id() -> str:
    return str(uuid.uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


@dataclass
class Entry:
    """Common envelope for all awareness store entries."""

    id: str
    type: EntryType
    source: str
    tags: list[str]
    created: str
    updated: str
    expires: str | None
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value if isinstance(self.type, EntryType) else self.type,
            "source": self.source,
            "tags": self.tags,
            "created": self.created,
            "updated": self.updated,
            "expires": self.expires,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Entry:
        return cls(
            id=d["id"],
            type=EntryType(d["type"]),
            source=d["source"],
            tags=d.get("tags", []),
            created=d["created"],
            updated=d["updated"],
            expires=d.get("expires"),
            data=d.get("data", {}),
        )

    def is_expired(self) -> bool:
        if not self.expires:
            return False
        return datetime.now(timezone.utc) >= parse_iso(self.expires)

    def is_stale(self) -> bool:
        """For status entries: check if TTL has expired since last update."""
        if self.type != EntryType.STATUS:
            return False
        ttl = self.data.get("ttl_sec")
        if ttl is None:
            return False
        return self.age_sec > float(ttl)

    @property
    def age_sec(self) -> float:
        return (datetime.now(timezone.utc) - parse_iso(self.updated)).total_seconds()


def validate_entry_data(data: dict[str, Any]) -> list[str]:
    """Validate raw entry data before creating an Entry. Returns list of errors."""
    errors = []
    for f in ("type", "source"):
        if f not in data:
            errors.append(f"Missing required field: {f}")
    if "type" in data:
        try:
            EntryType(data["type"])
        except ValueError:
            valid = [e.value for e in EntryType]
            errors.append(f"Invalid type: {data['type']}. Must be one of: {valid}")
    if "data" in data and not isinstance(data["data"], dict):
        errors.append("'data' must be a dict")
    if "tags" in data and not isinstance(data["tags"], list):
        errors.append("'tags' must be a list")
    return errors
