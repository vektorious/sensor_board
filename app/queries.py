"""Read-side query helpers, shared by the web and API routes."""
from datetime import datetime, timedelta, UTC

from sqlalchemy import and_, func
from sqlmodel import select

from app.database import get_session
from app.models import Reading
from app.sensors import meta_for, sort_key


def overview_stats() -> dict:
    """At-a-glance totals for the overview page."""
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    with get_session() as s:
        measurements = s.exec(select(func.count(Reading.id))).one()
        devices = s.exec(
            select(func.count(func.distinct(Reading.device_uuid)))
        ).one()
        projects = s.exec(
            select(func.count(func.distinct(Reading.project))).where(
                Reading.project.is_not(None)
            )
        ).one()
        active_24h = s.exec(
            select(func.count(func.distinct(Reading.device_uuid))).where(
                Reading.timestamp >= cutoff
            )
        ).one()
        last_seen = s.exec(select(func.max(Reading.timestamp))).one()
    return {
        "devices": devices or 0,
        "measurements": measurements or 0,
        "projects": projects or 0,
        "active_24h": active_24h or 0,
        "last_seen": last_seen,
    }


def list_projects() -> list[dict]:
    """Distinct projects with device counts and last-seen time."""
    with get_session() as s:
        rows = s.exec(
            select(
                Reading.project,
                func.count(func.distinct(Reading.device_uuid)),
                func.max(Reading.timestamp),
            )
            .where(Reading.project.is_not(None))
            .group_by(Reading.project)
            .order_by(Reading.project)
        ).all()
    return [
        {"project": p, "device_count": n, "last_seen": last}
        for (p, n, last) in rows
    ]


def list_devices(project: str | None = None) -> list[dict]:
    """Devices, optionally filtered to one project, with last-seen time.

    One query: the latest row per device (name/project reflect the most recent
    reading) via a max-timestamp self-join, instead of a per-device N+1.
    """
    with get_session() as s:
        latest_ts = select(
            Reading.device_uuid,
            func.max(Reading.timestamp).label("mts"),
        )
        if project is not None:
            latest_ts = latest_ts.where(Reading.project == project)
        latest_ts = latest_ts.group_by(Reading.device_uuid).subquery()

        stmt = select(Reading).join(
            latest_ts,
            and_(
                Reading.device_uuid == latest_ts.c.device_uuid,
                Reading.timestamp == latest_ts.c.mts,
            ),
        )
        if project is not None:
            stmt = stmt.where(Reading.project == project)
        rows = s.exec(stmt).all()

    # Dedupe on device_uuid (guards against tied timestamps) and sort by uuid.
    by_uuid: dict[str, dict] = {}
    for r in rows:
        by_uuid.setdefault(
            r.device_uuid,
            {
                "device_uuid": r.device_uuid,
                "device_name": r.device_name,
                "project": r.project,
                "last_seen": r.timestamp,
            },
        )
    return [by_uuid[u] for u in sorted(by_uuid)]


def device_info(device_uuid: str) -> dict | None:
    """Identity/metadata for a single device, or None if unknown."""
    with get_session() as s:
        latest = s.exec(
            select(Reading)
            .where(Reading.device_uuid == device_uuid)
            .order_by(Reading.timestamp.desc())
        ).first()
    if latest is None:
        return None
    return {
        "device_uuid": device_uuid,
        "device_name": latest.device_name,
        "project": latest.project,
        "last_seen": latest.timestamp,
    }


def device_sensors(device_uuid: str) -> list[dict]:
    """Every sensor a device has reported, with presentation meta + latest value.

    This is what drives auto-population: the panel list is derived from the data,
    not hardcoded.
    """
    with get_session() as s:
        # Latest row per sensor_type for this device, in one query (max-timestamp
        # self-join), instead of a distinct-then-per-sensor N+1.
        latest_ts = (
            select(
                Reading.sensor_type,
                func.max(Reading.timestamp).label("mts"),
            )
            .where(Reading.device_uuid == device_uuid)
            .group_by(Reading.sensor_type)
            .subquery()
        )
        rows = s.exec(
            select(Reading)
            .join(
                latest_ts,
                and_(
                    Reading.sensor_type == latest_ts.c.sensor_type,
                    Reading.timestamp == latest_ts.c.mts,
                ),
            )
            .where(Reading.device_uuid == device_uuid)
        ).all()

    panels = []
    seen: set[str] = set()
    for latest in rows:
        if latest.sensor_type in seen:  # guard against tied timestamps
            continue
        seen.add(latest.sensor_type)
        meta = meta_for(latest.sensor_type, latest.unit, latest.plot)
        meta["latest"] = latest.value
        meta["timestamp"] = latest.timestamp.isoformat()
        panels.append(meta)
    panels.sort(key=lambda m: sort_key(m["key"]))
    return panels


def project_sensor_types(project: str) -> list[dict]:
    """Distinct sensor types reported by any device in a project, as panel meta
    (sorted). Drives the project page's aggregated charts."""
    with get_session() as s:
        types = s.exec(
            select(Reading.sensor_type)
            .where(Reading.project == project)
            .distinct()
        ).all()
    metas = [meta_for(t) for t in types]
    metas.sort(key=lambda m: sort_key(m["key"]))
    return metas


def project_series(project: str, sensor_type: str, hours: int) -> list[dict]:
    """One time-series per device in the project for a given sensor.

    Single query over the project's rows for this sensor (backed by
    ix_readings_project_ts), grouped by device in Python — avoids the old
    per-device N+1 (list_devices + a series query each).
    """
    with get_session() as s:
        stmt = select(
            Reading.device_uuid,
            Reading.device_name,
            Reading.timestamp,
            Reading.value,
        ).where(
            Reading.project == project,
            Reading.sensor_type == sensor_type,
        )
        if hours and hours > 0:
            cutoff = datetime.now(UTC) - timedelta(hours=hours)
            stmt = stmt.where(Reading.timestamp >= cutoff)
        stmt = stmt.order_by(Reading.device_uuid, Reading.timestamp)
        rows = s.exec(stmt).all()

    grouped: dict[str, dict] = {}
    for uuid, name, ts, value in rows:
        if value is None:
            continue
        g = grouped.get(uuid)
        if g is None:
            g = grouped[uuid] = {"device_uuid": uuid, "device_name": uuid, "points": []}
        if name:  # rows are ascending by ts, so the last non-null name wins
            g["device_name"] = name
        g["points"].append([ts.isoformat(), value])
    return [g for g in grouped.values() if g["points"]]


def series(device_uuid: str, sensor_type: str, hours: int) -> list[list]:
    """Time-ordered [iso_timestamp, value] points within the lookback window.

    hours <= 0 means "all history".
    """
    with get_session() as s:
        stmt = select(Reading.timestamp, Reading.value).where(
            Reading.device_uuid == device_uuid,
            Reading.sensor_type == sensor_type,
        )
        if hours and hours > 0:
            cutoff = datetime.now(UTC) - timedelta(hours=hours)
            stmt = stmt.where(Reading.timestamp >= cutoff)
        stmt = stmt.order_by(Reading.timestamp)
        rows = s.exec(stmt).all()
    return [[ts.isoformat(), v] for (ts, v) in rows if v is not None]
