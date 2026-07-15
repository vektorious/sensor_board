"""Automatic retention: delete stale devices (and, implicitly, projects).

A device is *stale* when its most recent reading is older than
``settings.retention_hours`` (default 48h). Stale devices are purged — all of
their ``readings`` rows are deleted. Because a project is just the set of
readings that carry its name, a project vanishes automatically once every one
of its devices has been purged; a project with even one still-active (or
exempt) device is left untouched. That matches the rule "delete a project once
no device has sent data for 48h".

Exceptions: a device is spared if its UUID is in ``retention_exempt_devices``
or its latest project is in ``retention_exempt_projects``. The exempt sets are
config today (env vars); ``purge_stale`` takes them as parameters so the source
can later move to a table without touching this logic.

The sweep runs in a background daemon thread (see ``start_retention_sweeper``)
and is also safe to call directly (idempotent; concurrent sweeps across
gunicorn workers just delete the same rows once, guarded by WAL + busy_timeout).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, func
from sqlmodel import select

from app.config import settings
from app.database import get_session
from app.models import Reading

log = logging.getLogger("sensor_board.retention")

_sweeper_started = False
_start_lock = threading.Lock()


def purge_stale(
    now: datetime | None = None,
    retention_hours: int | None = None,
    exempt_devices: set[str] | None = None,
    exempt_projects: set[str] | None = None,
) -> dict:
    """Delete devices with no reading newer than the cutoff. Idempotent.

    Returns a small report: whether it ran, the cutoff, and the device UUIDs and
    project names that were removed.
    """
    hours = settings.retention_hours if retention_hours is None else retention_hours
    if hours <= 0:
        return {"enabled": False, "deleted_devices": [], "removed_projects": []}

    if exempt_devices is None:
        exempt_devices = settings.retention_exempt_devices
    if exempt_projects is None:
        exempt_projects = settings.retention_exempt_projects

    cutoff = (now or datetime.now(UTC)) - timedelta(hours=hours)

    with get_session() as s:
        projects_before = set(
            s.exec(
                select(Reading.project).where(Reading.project.is_not(None)).distinct()
            ).all()
        )

        # Latest row per device whose last reading predates the cutoff. The
        # cutoff comparison happens in SQL (like queries.series) so it works
        # regardless of how SQLite round-trips tz-aware vs. naive datetimes; the
        # join carries the device's latest project for exemption checks.
        latest_ts = (
            select(
                Reading.device_uuid,
                func.max(Reading.timestamp).label("mts"),
            )
            .group_by(Reading.device_uuid)
            .subquery()
        )
        stale_rows = s.exec(
            select(Reading.device_uuid, Reading.project)
            .join(
                latest_ts,
                and_(
                    Reading.device_uuid == latest_ts.c.device_uuid,
                    Reading.timestamp == latest_ts.c.mts,
                ),
            )
            .where(latest_ts.c.mts < cutoff)
        ).all()

        # Apply exemptions in Python (pure set membership — no datetime compare).
        stale = sorted(
            {
                uuid
                for uuid, project in stale_rows
                if uuid not in exempt_devices and project not in exempt_projects
            }
        )

        if stale:
            s.exec(delete(Reading).where(Reading.device_uuid.in_(stale)))
            s.commit()
            projects_after = set(
                s.exec(
                    select(Reading.project)
                    .where(Reading.project.is_not(None))
                    .distinct()
                ).all()
            )
        else:
            projects_after = projects_before

    removed_projects = sorted(projects_before - projects_after)
    if stale:
        log.info(
            "retention: purged %d stale device(s) (cutoff=%s); removed %d project(s)%s",
            len(stale),
            cutoff.isoformat(),
            len(removed_projects),
            f" {removed_projects}" if removed_projects else "",
        )
    return {
        "enabled": True,
        "cutoff": cutoff.isoformat(),
        "deleted_devices": stale,
        "removed_projects": removed_projects,
    }


def start_retention_sweeper() -> None:
    """Start the background sweeper once. No-op if retention is disabled.

    Runs an immediate sweep, then repeats every
    ``retention_sweep_interval_hours``. Daemon thread, so it never blocks
    shutdown. Started per gunicorn worker; redundant runs are harmless.
    """
    global _sweeper_started
    if settings.retention_hours <= 0:
        log.info("retention disabled (RETENTION_HOURS <= 0); sweeper not started")
        return
    with _start_lock:
        if _sweeper_started:
            return
        _sweeper_started = True

    interval = max(60.0, settings.retention_sweep_interval_hours * 3600.0)

    def _run() -> None:
        while True:
            try:
                purge_stale()
            except Exception:  # never let a bad sweep kill the thread
                log.exception("retention sweep failed")
            time.sleep(interval)

    threading.Thread(target=_run, name="retention-sweeper", daemon=True).start()
    log.info(
        "retention sweeper started: %dh retention, sweep every %.1fh",
        settings.retention_hours,
        interval / 3600.0,
    )
