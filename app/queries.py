"""Read-side query helpers, shared by the web and API routes."""
from datetime import datetime, timedelta, UTC

from sqlalchemy import func
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
    """Devices, optionally filtered to one project, with last-seen time."""
    with get_session() as s:
        stmt = select(
            Reading.device_uuid,
            func.max(Reading.timestamp),
        )
        if project is not None:
            stmt = stmt.where(Reading.project == project)
        stmt = stmt.group_by(Reading.device_uuid).order_by(Reading.device_uuid)
        rows = s.exec(stmt).all()

        devices = []
        for uuid, last_seen in rows:
            latest = s.exec(
                select(Reading)
                .where(Reading.device_uuid == uuid)
                .order_by(Reading.timestamp.desc())
            ).first()
            devices.append(
                {
                    "device_uuid": uuid,
                    "device_name": latest.device_name if latest else None,
                    "project": latest.project if latest else None,
                    "last_seen": last_seen,
                }
            )
    return devices


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
        types = s.exec(
            select(Reading.sensor_type)
            .where(Reading.device_uuid == device_uuid)
            .distinct()
            .order_by(Reading.sensor_type)
        ).all()

        panels = []
        for sensor_type in types:
            latest = s.exec(
                select(Reading)
                .where(
                    Reading.device_uuid == device_uuid,
                    Reading.sensor_type == sensor_type,
                )
                .order_by(Reading.timestamp.desc())
            ).first()
            meta = meta_for(sensor_type, latest.unit if latest else None,
                            latest.plot if latest else None)
            meta["latest"] = latest.value if latest else None
            meta["timestamp"] = latest.timestamp.isoformat() if latest else None
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
    """One time-series per device in the project for a given sensor."""
    out = []
    for d in list_devices(project=project):
        pts = series(d["device_uuid"], sensor_type, hours)
        if pts:
            out.append(
                {
                    "device_uuid": d["device_uuid"],
                    "device_name": d["device_name"] or d["device_uuid"],
                    "points": pts,
                }
            )
    return out


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
