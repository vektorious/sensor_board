"""Read-side query helpers, shared by the web and API routes."""
from datetime import datetime, timedelta, UTC

from sqlalchemy import and_, func
from sqlmodel import select

from app import retention
from app.config import settings
from app.database import get_session
from app.models import Device, Reading
from app.sensors import meta_for, sort_key


def overview_stats() -> dict:
    """At-a-glance totals for the overview page."""
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    with get_session() as s:
        measurements = s.exec(select(func.count(Reading.id))).one()
        devices = s.exec(
            select(func.count(func.distinct(Reading.device_id)))
        ).one()
        projects = s.exec(
            select(func.count(func.distinct(Reading.project))).where(
                Reading.project.is_not(None)
            )
        ).one()
        active_24h = s.exec(
            select(func.count(func.distinct(Reading.device_id))).where(
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
                func.count(func.distinct(Reading.device_id)),
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
            Reading.device_id,
            func.max(Reading.timestamp).label("mts"),
        )
        if project is not None:
            latest_ts = latest_ts.where(Reading.project == project)
        latest_ts = latest_ts.group_by(Reading.device_id).subquery()

        stmt = select(Reading).join(
            latest_ts,
            and_(
                Reading.device_id == latest_ts.c.device_id,
                Reading.timestamp == latest_ts.c.mts,
            ),
        )
        if project is not None:
            stmt = stmt.where(Reading.project == project)
        rows = s.exec(stmt).all()

    # Dedupe on device_id (guards against tied timestamps) and sort by ID.
    by_device: dict[str, dict] = {}
    for r in rows:
        by_device.setdefault(
            r.device_id,
            {
                "device_id": r.device_id,
                "device_name": r.device_name,
                "project": r.project,
                "last_seen": r.timestamp,
            },
        )
    return [by_device[u] for u in sorted(by_device)]


def device_info(device_id: str) -> dict | None:
    """Identity, activity, and expiry state for one device, or None if unknown.

    Reads both tables: `devices` owns identity and expiry (it is what the
    sweeper acts on), while the newest reading supplies the display name and
    project, which always reflect the most recent write.
    """
    with get_session() as s:
        device = s.exec(
            select(Device).where(Device.device_id == device_id)
        ).first()
        if device is None:
            return None
        latest = s.exec(
            select(Reading)
            .where(Reading.device_id == device_id)
            .order_by(Reading.timestamp.desc())
        ).first()

        last_seen = device.last_seen_at
        if last_seen.tzinfo is None:  # SQLite round-trips without a zone
            last_seen = last_seen.replace(tzinfo=UTC)

        expires_at = None
        if not device.persistent and settings.retention_hours > 0:
            expires_at = last_seen + timedelta(
                hours=retention.retention_hours_for(device)
            )

        return {
            "device_id": device_id,
            "device_name": latest.device_name if latest else None,
            "project": latest.project if latest else None,
            "last_seen": last_seen,
            "persistent": device.persistent,
            "expires_at": expires_at,
            # Hours left before the sweeper may delete this device, so the page
            # can warn before the data disappears rather than after (§17).
            "expires_in_hours": (
                max(0.0, (expires_at - datetime.now(UTC)).total_seconds() / 3600)
                if expires_at
                else None
            ),
        }


def device_sensors(device_id: str) -> list[dict]:
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
            .where(Reading.device_id == device_id)
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
            .where(Reading.device_id == device_id)
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
            Reading.device_id,
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
        stmt = stmt.order_by(Reading.device_id, Reading.timestamp)
        rows = s.exec(stmt).all()

    grouped: dict[str, dict] = {}
    for device_id, name, ts, value in rows:
        if value is None:
            continue
        g = grouped.get(device_id)
        if g is None:
            g = grouped[device_id] = {
                "device_id": device_id,
                "device_name": device_id,
                "points": [],
            }
        if name:  # rows are ascending by ts, so the last non-null name wins
            g["device_name"] = name
        g["points"].append([ts.isoformat(), value])
    return [g for g in grouped.values() if g["points"]]


def series(device_id: str, sensor_type: str, hours: int) -> list[list]:
    """Time-ordered [iso_timestamp, value] points within the lookback window.

    hours <= 0 means "all history".
    """
    with get_session() as s:
        stmt = select(Reading.timestamp, Reading.value).where(
            Reading.device_id == device_id,
            Reading.sensor_type == sensor_type,
        )
        if hours and hours > 0:
            cutoff = datetime.now(UTC) - timedelta(hours=hours)
            stmt = stmt.where(Reading.timestamp >= cutoff)
        stmt = stmt.order_by(Reading.timestamp)
        rows = s.exec(stmt).all()
    return [[ts.isoformat(), v] for (ts, v) in rows if v is not None]
