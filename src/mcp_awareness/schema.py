# mcp-awareness — ambient system awareness for AI agents
# Copyright (C) 2026 Chris Means
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

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
    INTENTION = "intention"


# Valid states for the INTENTION lifecycle
INTENTION_STATES = {"pending", "fired", "active", "completed", "snoozed", "cancelled"}


SEVERITY_RANK = {
    "info": 0,
    "warning": 1,
    "critical": 2,
}


def severity_rank(level: str) -> int:
    return SEVERITY_RANK.get(level, -1)


def make_id() -> str:
    return str(uuid.uuid4())


def now_utc() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def parse_iso(s: str) -> datetime:
    # Python 3.10 doesn't support 'Z' suffix in fromisoformat — normalize to +00:00
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    # Always return timezone-aware datetimes — naive inputs assumed UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_iso(dt: datetime) -> str:
    """Convert a datetime to ISO 8601 string for JSON serialization."""
    return dt.isoformat()


def ensure_dt(val: str | datetime) -> datetime:
    """Coerce a string or datetime to a timezone-aware datetime."""
    if isinstance(val, datetime):
        return val
    return parse_iso(val)


def ensure_dt_optional(val: str | datetime | None) -> datetime | None:
    """Coerce a string/datetime/None to a timezone-aware datetime or None."""
    if val is None:
        return None
    return ensure_dt(val)


# Keep backward compat for any code still calling now_iso()
def now_iso() -> str:
    return now_utc().isoformat()


@dataclass
class Entry:
    """Common envelope for all awareness store entries."""

    id: str
    type: EntryType
    source: str
    tags: list[str]
    created: datetime
    updated: datetime
    expires: datetime | None
    data: dict[str, Any]
    logical_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "type": self.type.value if isinstance(self.type, EntryType) else self.type,
            "source": self.source,
            "tags": self.tags,
            "created": to_iso(self.created),
            "updated": to_iso(self.updated),
            "expires": to_iso(self.expires) if self.expires else None,
            "data": self.data,
        }
        if self.logical_key is not None:
            d["logical_key"] = self.logical_key
        return d

    def to_list_dict(self) -> dict[str, Any]:
        """Lightweight metadata-only representation — no content or changelog.

        Type-aware: uses message for alerts, goal+state for intentions,
        description for everything else.
        """
        entry_type = self.type.value if isinstance(self.type, EntryType) else self.type
        # Type-aware description extraction
        desc = self.data.get("description", "")
        if not desc and entry_type == "alert":
            desc = self.data.get("message", "")
        d: dict[str, Any] = {
            "id": self.id,
            "type": entry_type,
            "source": self.source,
            "tags": self.tags,
            "description": desc,
            "created": to_iso(self.created),
            "updated": to_iso(self.updated),
        }
        # Include key fields for intentions in list mode
        if entry_type == "intention":
            d["goal"] = self.data.get("goal", "")
            d["state"] = self.data.get("state", "pending")
        if self.logical_key is not None:
            d["logical_key"] = self.logical_key
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Entry:
        return cls(
            id=d["id"],
            type=EntryType(d["type"]),
            source=d["source"],
            tags=d.get("tags", []),
            created=ensure_dt(d["created"]),
            updated=ensure_dt(d["updated"]),
            expires=ensure_dt_optional(d.get("expires")),
            data=d.get("data", {}),
            logical_key=d.get("logical_key"),
        )

    def is_expired(self) -> bool:
        if not self.expires:
            return False
        return datetime.now(timezone.utc) >= self.expires

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
        return (datetime.now(timezone.utc) - self.updated).total_seconds()


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
