"""Briefing generation, pattern/suppression application, escalation evaluation.

The collator digests the raw store into a compact, agent-optimized briefing.
It applies patterns and suppressions so the agent doesn't have to — the
briefing is pre-filtered. Raw data remains available via drill-down resources.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .schema import Entry, severity_rank
from .store import AwarenessStore


def is_suppressed(alert: Entry, suppressions: list[Entry]) -> bool:
    """Check if an alert is suppressed, respecting escalation overrides.

    A suppression matches an alert if:
    - suppression source is empty (global) or matches the alert source
    - suppression metric is None or matches the alert metric
    - suppression tags is empty or overlaps with alert tags

    Escalation override: if the alert level exceeds the suppression's
    suppress_level, the suppression is bypassed (alert breaks through).
    """
    alert_data = alert.data
    alert_level = alert_data.get("level", "warning")
    alert_metric = alert_data.get("metric")

    for s in suppressions:
        if s.is_expired():
            continue
        s_data = s.data

        # Source match
        if s.source and s.source != alert.source:
            continue

        # Metric match
        s_metric = s_data.get("metric")
        if s_metric and s_metric != alert_metric:
            continue

        # Tag match
        s_tags = s_data.get("tags")
        if s_tags and not any(t in alert.tags for t in s_tags):
            continue

        # Matched — check escalation override
        suppress_level = s_data.get("suppress_level", "warning")
        escalated = s_data.get("escalation_override", True) and severity_rank(
            alert_level
        ) > severity_rank(suppress_level)
        return not escalated

    return False  # No matching suppression


def matches_pattern(alert: Entry, patterns: list[Entry]) -> bool:
    """Check if an alert matches a learned pattern (expected anomaly).

    Evaluates pattern conditions (day_of_week, hour_range) and checks if
    the pattern's effect is relevant to the alert via keyword matching.
    """
    now = datetime.now(timezone.utc)
    alert_data = alert.data

    for p in patterns:
        p_data = p.data
        conditions = p_data.get("conditions", {})
        effect = p_data.get("effect", "")

        # Check if the pattern's effect is relevant to this alert
        if not _effect_matches_alert(effect, alert_data):
            continue

        # Evaluate conditions — all must match
        if not _conditions_match(conditions, now):
            continue

        return True

    return False


def _effect_matches_alert(effect: str, alert_data: dict[str, Any]) -> bool:
    """Check if a pattern's effect string is relevant to an alert.

    Uses word-overlap matching: splits both the effect and alert fields into
    words and checks for significant overlap (ignoring common stop words).
    """
    if not effect:
        return False
    stop_words = {
        "suppress",
        "ignore",
        "skip",
        "the",
        "a",
        "an",
        "on",
        "in",
        "for",
        "and",
        "or",
        "is",
        "at",
    }
    effect_words = set(effect.lower().replace("—", " ").replace("-", " ").split()) - stop_words

    tokens = [
        alert_data.get("alert_id", ""),
        alert_data.get("alert_type", ""),
        alert_data.get("metric", ""),
        alert_data.get("message", ""),
    ]
    for token in tokens:
        if not token:
            continue
        token_words = set(token.lower().replace("—", " ").replace("-", " ").split()) - stop_words
        # Match if any effect word appears in the token or vice versa
        if effect_words & token_words:
            return True
    return False


def _conditions_match(conditions: dict[str, Any], now: datetime) -> bool:
    """Evaluate all conditions against current time. Empty conditions = always match."""
    if not conditions:
        return True
    for key, value in conditions.items():
        if key == "day_of_week":
            day_names = [
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
            ]
            current_day = day_names[now.weekday()]
            if current_day != str(value).lower():
                return False
        elif key == "hour_range":
            if isinstance(value, list) and len(value) == 2:
                if value[0] > value[1]:
                    # Overnight wraparound (e.g., [22, 6] = 10 PM to 6 AM)
                    if not (now.hour >= value[0] or now.hour < value[1]):
                        return False
                else:
                    if not (value[0] <= now.hour < value[1]):
                        return False
    return True


def compose_summary(briefing: dict[str, Any]) -> str:
    """Generate a one-line summary for the briefing."""
    sources = briefing.get("sources", {})
    total = len(sources)
    upcoming = briefing.get("upcoming", [])

    if not briefing.get("attention_needed"):
        return f"All clear across {total} source{'s' if total != 1 else ''}."

    parts = []
    criticals = [s for s, d in sources.items() if d.get("status") == "critical"]
    warnings = [s for s, d in sources.items() if d.get("status") == "warning"]
    stales = [s for s, d in sources.items() if d.get("status") == "stale"]

    if criticals:
        parts.append(f"{len(criticals)} critical on {', '.join(criticals)}")
    if warnings:
        parts.append(f"{len(warnings)} warning on {', '.join(warnings)}")
    if stales:
        parts.append(f"{', '.join(stales)} stale")
    if upcoming:
        parts.append(f"{len(upcoming)} upcoming item{'s' if len(upcoming) != 1 else ''}")

    return ". ".join(parts) + "." if parts else f"All clear across {total} sources."


def compose_mention(briefing: dict[str, Any]) -> str:
    """Generate a suggested mention the agent can use or rephrase."""
    sources = briefing.get("sources", {})
    upcoming = briefing.get("upcoming", [])
    parts = []

    for _source, info in sources.items():
        headline = info.get("headline")
        if headline:
            status = info.get("status", "")
            if status == "critical":
                parts.append(f"CRITICAL: {headline}")
            elif status == "stale":
                parts.append(f"STALE: {headline}")
            else:
                parts.append(f"FYI: {headline}")

    for item in upcoming:
        summary = item.get("summary", "")
        if summary:
            parts.append(summary)

    return " ".join(parts)


def generate_briefing(store: AwarenessStore) -> dict[str, Any]:
    """Generate a compact briefing from the raw store.

    This is the core collation logic. It:
    1. Checks each source for staleness (TTL expiry)
    2. Filters alerts through active suppressions
    3. Filters alerts through learned patterns
    4. Determines per-source status (ok/warning/critical/stale)
    5. Composes a summary line and suggested mention
    """
    now = datetime.now(timezone.utc)
    briefing: dict[str, Any] = {
        "generated": now.isoformat(),
        "staleness_sec": 0,
        "sources": {},
        "active_alerts": 0,
        "active_suppressions": 0,
        "upcoming": [],
        "attention_needed": False,
    }

    for source in store.get_sources():
        status = store.get_latest_status(source)
        alerts = store.get_active_alerts(source)
        suppressions = store.get_active_suppressions(source)

        # Check for stale sources (TTL expired)
        if status and status.is_stale():
            age = int(status.age_sec)
            briefing["sources"][source] = {
                "status": "stale",
                "last_report": status.updated,
                "headline": f"{source} has not reported in {age}s",
                "drill_down": f"awareness://status/{source}",
            }
            briefing["attention_needed"] = True
            continue

        # Apply suppressions — filter out suppressed alerts
        active_alerts = [a for a in alerts if not is_suppressed(a, suppressions)]

        # Apply learned patterns — filter out expected anomalies
        patterns = store.get_patterns(source)
        active_alerts = [a for a in active_alerts if not matches_pattern(a, patterns)]

        # Determine source status
        if any(a.data.get("level") == "critical" for a in active_alerts):
            source_status = "critical"
        elif active_alerts:
            source_status = "warning"
        else:
            source_status = "ok"

        source_entry: dict[str, Any] = {
            "status": source_status,
            "last_report": status.updated if status else None,
        }

        if active_alerts:
            top_alert = max(
                active_alerts,
                key=lambda a: severity_rank(a.data.get("level", "warning")),
            )
            source_entry["headline"] = top_alert.data.get("message", "")
            source_entry["drill_down"] = f"awareness://alerts/{source}"
            briefing["active_alerts"] += len(active_alerts)
            briefing["attention_needed"] = True

        briefing["sources"][source] = source_entry

    briefing["active_suppressions"] = store.count_active_suppressions()
    briefing["summary"] = compose_summary(briefing)

    if briefing["attention_needed"]:
        briefing["suggested_mention"] = compose_mention(briefing)

    return briefing
