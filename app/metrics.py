"""Platform metrics — lifetime totals and live active counts (plan §19).

Two different questions, two different mechanisms:

*Lifetime totals* ("how much has ever been published here?") are explicit
counters in ``platform_metrics``. They must be counters rather than queries,
because the rows they count get deleted when a device expires. They only ever
go up: cleanup jobs must never touch them.

*Active counts* ("how much is here right now?") are plain queries over the live
tables, so they can never drift out of sync with reality.

A third use of the same table is gauges — values that are overwritten rather
than accumulated, such as the last sampled database size (§27).
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func
from sqlmodel import Session, select

from app.database import get_session
from app.models import Device, PlatformMetric, Reading

# Counters that always exist, so the main page never has to handle a missing
# key and "nothing published yet" reads as 0 rather than blank.
LIFETIME_COUNTERS = ("devices_total", "measurements_total", "projects_total")


def bump(session: Session, name: str, delta: int = 1) -> None:
    """Increment a lifetime counter inside the caller's transaction.

    Takes an open session on purpose: §19 requires the counter to move in the
    same transaction as the creation it counts, so a rolled-back ingest cannot
    leave the totals inflated.
    """
    if delta == 0:
        return
    session.execute(
        _upsert_sql(),
        {"name": name, "value": delta, "now": datetime.now(UTC), "accumulate": 1},
    )


def set_gauge(session: Session, name: str, value: int) -> None:
    """Overwrite a value rather than accumulating it (e.g. last DB size)."""
    session.execute(
        _upsert_sql(),
        {"name": name, "value": value, "now": datetime.now(UTC), "accumulate": 0},
    )


def _upsert_sql():
    from sqlalchemy import text

    # One statement for both modes: :accumulate = 1 adds to the stored value,
    # 0 replaces it. Keeps the concurrent-worker case atomic in SQLite.
    return text(
        """
        INSERT INTO platform_metrics (metric_name, metric_value, updated_at)
        VALUES (:name, :value, :now)
        ON CONFLICT(metric_name) DO UPDATE SET
            metric_value = CASE WHEN :accumulate = 1
                                THEN platform_metrics.metric_value + excluded.metric_value
                                ELSE excluded.metric_value END,
            updated_at = excluded.updated_at
        """
    )


def get(name: str, default: int = 0) -> int:
    with get_session() as s:
        row = s.get(PlatformMetric, name)
    return default if row is None else row.metric_value


def get_with_time(name: str) -> tuple[int, datetime] | None:
    """Value plus when it was last written — used for growth-rate sampling."""
    with get_session() as s:
        row = s.get(PlatformMetric, name)
    if row is None:
        return None
    stamp = row.updated_at
    if stamp.tzinfo is None:  # SQLite round-trips datetimes without a zone
        stamp = stamp.replace(tzinfo=UTC)
    return row.metric_value, stamp


def lifetime() -> dict[str, int]:
    """Every lifetime counter, with the well-known ones defaulted to 0."""
    with get_session() as s:
        rows = s.exec(select(PlatformMetric)).all()
    out = {name: 0 for name in LIFETIME_COUNTERS}
    out.update({r.metric_name: r.metric_value for r in rows})
    return out


def active() -> dict[str, int]:
    """Counts of what currently exists, straight from the live tables."""
    with get_session() as s:
        devices = s.exec(select(func.count(Device.id))).one()
        measurements = s.exec(select(func.count(Reading.id))).one()
        projects = s.exec(
            select(func.count(func.distinct(Reading.project))).where(
                Reading.project.is_not(None)
            )
        ).one()
    return {
        "devices_active": devices or 0,
        "measurements_active": measurements or 0,
        "projects_active": projects or 0,
    }


def summary() -> dict[str, int]:
    """Lifetime totals and active counts together, for the main page."""
    return {**lifetime(), **active()}
