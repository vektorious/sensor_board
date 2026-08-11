"""Automatic expiry of idle devices (plan §4, §18).

A temporary device expires once it has gone ``retention_hours`` without a
*successful* write. Expiry is measured against ``devices.last_seen_at``, which
only successful writes advance — so a stream of rejected requests can never
keep somebody else's device alive, and a client cannot extend its own retention
by sending a doctored timestamp.

Persistent devices (those created through a valid API key) are exempt, as are
device IDs and project names listed in the configuration.

Expiring a device deletes the device row, which cascades to its measurements
and takes the stored write-key hash with it. The device ID then becomes free
for anyone to claim again — with a new write key, and with no trace of the
previous owner's data, which is what makes reuse safe (§17).

Lifetime metrics are deliberately untouched here: `devices_total` and
`measurements_total` count what has ever been published, so a sweep that
removed data must not move them (§19).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func
from sqlmodel import Session, select

from app.config import settings
from app.database import get_session
from app.models import Device, Reading
from app.policies import registry

log = logging.getLogger("sensor_board.retention")

_sweeper_started = False
_start_lock = threading.Lock()

# Devices are deleted in batches so one sweep of a large backlog holds the
# write lock briefly and repeatedly, rather than once for a long time.
BATCH_SIZE = 200


def retention_hours_for(device: Device) -> int:
    """How long this device may stay idle before expiring."""
    return registry.by_name(device.policy).retention_hours


def is_expired(device: Device, now: datetime | None = None) -> bool:
    """Whether a device has gone past its idle window (§4)."""
    if device.persistent:
        return False
    if settings.retention_hours <= 0:
        return False
    now = now or datetime.now(UTC)
    last_seen = device.last_seen_at
    if last_seen.tzinfo is None:  # SQLite round-trips datetimes without a zone
        last_seen = last_seen.replace(tzinfo=UTC)
    return now - last_seen >= timedelta(hours=retention_hours_for(device))


def is_exempt(
    device: Device,
    exempt_devices: set[str] | None = None,
    exempt_projects: set[str] | None = None,
    session: Session | None = None,
) -> bool:
    """Whether configuration protects this device from expiry."""
    devices = settings.retention_exempt_devices if exempt_devices is None else exempt_devices
    projects = (
        settings.retention_exempt_projects if exempt_projects is None else exempt_projects
    )
    if device.device_id in devices:
        return True
    if not projects or session is None:
        return False
    project = session.exec(
        select(Reading.project)
        .where(Reading.device_id == device.device_id)
        .order_by(Reading.timestamp.desc())
        .limit(1)
    ).first()
    return project in projects


def expire_if_stale(device_id: str, now: datetime | None = None) -> bool:
    """Delete one device if it has already expired. Returns True if it was.

    Called on the ingestion path when a device ID turns out to be taken (§4):
    without it, a client trying to claim an ID whose previous owner went silent
    days ago would be told the ID is in use until the next hourly sweep
    happened to run. Checking here makes expiry feel immediate.
    """
    now = now or datetime.now(UTC)
    with get_session() as session:
        device = session.exec(
            select(Device).where(Device.device_id == device_id)
        ).first()
        if device is None:
            return False
        if not is_expired(device, now) or is_exempt(device, session=session):
            return False
        deleted = _delete_batch(session, [device_id], now)
        session.commit()
    if deleted:
        log.info("retention: expired device on demand (id=%s)", _safe(device_id))
    return bool(deleted)


def purge_expired(
    now: datetime | None = None,
    exempt_devices: set[str] | None = None,
    exempt_projects: set[str] | None = None,
    batch_size: int = BATCH_SIZE,
) -> dict:
    """Delete every expired device. Idempotent; safe to run concurrently.

    Returns a report of what was removed, for logging and for the admin
    command.
    """
    if settings.retention_hours <= 0:
        return {"enabled": False, "deleted_devices": [], "deleted_measurements": 0,
                "removed_projects": []}

    now = now or datetime.now(UTC)
    deleted_devices: list[str] = []
    deleted_measurements = 0

    with get_session() as session:
        projects_before = _projects(session)

        for policy_name in _policies_in_use(session):
            cutoff = now - timedelta(
                hours=registry.by_name(policy_name).retention_hours
            )
            while True:
                candidates = session.exec(
                    select(Device)
                    .where(
                        Device.persistent == False,  # noqa: E712 (SQL, not Python)
                        Device.policy == policy_name,
                        Device.last_seen_at < cutoff,
                    )
                    .order_by(Device.last_seen_at)
                    .limit(batch_size)
                ).all()
                if not candidates:
                    break

                batch = [
                    d.device_id
                    for d in candidates
                    if not is_exempt(d, exempt_devices, exempt_projects, session)
                ]
                if batch:
                    measurements, removed = _delete_batch(session, batch, cutoff)
                    session.commit()
                    deleted_devices.extend(removed)
                    deleted_measurements += measurements

                if len(candidates) < batch_size:
                    break
                if not batch:
                    break  # every candidate in this batch is exempt

        projects_after = _projects(session) if deleted_devices else projects_before

    removed_projects = sorted(projects_before - projects_after)
    if deleted_devices:
        # Device IDs are public identifiers, so logging them is fine. Write-key
        # hashes are not logged at all — there is no reason to (§18).
        log.info(
            "retention: expired %d device(s) and %d measurement(s); "
            "removed %d project(s)%s",
            len(deleted_devices),
            deleted_measurements,
            len(removed_projects),
            f" {removed_projects}" if removed_projects else "",
        )
    return {
        "enabled": True,
        "deleted_devices": sorted(deleted_devices),
        "deleted_measurements": deleted_measurements,
        "removed_projects": removed_projects,
    }


def _delete_batch(session: Session, device_ids: list[str], cutoff: datetime):
    """Delete devices that are *still* stale, and their measurements.

    The `last_seen_at < cutoff` condition is repeated in the DELETE rather than
    trusted from the earlier SELECT. That is what makes the sweep safe against
    a device receiving a valid write mid-sweep: the write advances
    `last_seen_at`, the DELETE no longer matches, and the device survives (§4,
    §18). Without it there is a window in which a live device is deleted.
    """
    still_stale = session.exec(
        select(Device.device_id).where(
            Device.device_id.in_(device_ids),
            Device.persistent == False,  # noqa: E712
            Device.last_seen_at < cutoff,
        )
    ).all()
    if not still_stale:
        return 0, []

    measurements = session.exec(
        select(func.count(Reading.id)).where(Reading.device_id.in_(still_stale))
    ).one() or 0
    # Explicit rather than relying on the ON DELETE CASCADE, so the sweep
    # behaves identically if foreign keys are ever off on a connection.
    #
    # synchronize_session=False on both: the default asks SQLAlchemy to work
    # out which in-memory objects the criteria match by evaluating them in
    # Python, which cannot compare the timezone-aware cutoff against the naive
    # datetimes SQLite hands back. The criteria belong in SQL anyway — the
    # session is discarded straight after.
    session.exec(
        delete(Reading)
        .where(Reading.device_id.in_(still_stale))
        .execution_options(synchronize_session=False)
    )
    session.exec(
        delete(Device)
        .where(
            Device.device_id.in_(still_stale),
            Device.persistent == False,  # noqa: E712
            Device.last_seen_at < cutoff,
        )
        .execution_options(synchronize_session=False)
    )
    return measurements, list(still_stale)


def _policies_in_use(session: Session) -> list[str]:
    return list(
        session.exec(
            select(Device.policy).where(Device.persistent == False).distinct()  # noqa: E712
        ).all()
    )


def _projects(session: Session) -> set[str]:
    return set(
        session.exec(
            select(Reading.project).where(Reading.project.is_not(None)).distinct()
        ).all()
    )


def _safe(value: str) -> str:
    from app.security import safe_log_value

    return safe_log_value(value)


def sweep() -> dict:
    """One full maintenance pass: expire devices, tidy counters, watch growth."""
    from app import limits

    report = purge_expired()
    report["expired_windows"] = limits.sweep_expired_windows()
    report["storage"] = limits.observe_db_size()
    return report


def start_retention_sweeper() -> None:
    """Start the background sweeper once. No-op if retention is disabled.

    Runs an immediate pass, then repeats every
    ``retention_sweep_interval_hours``. Daemon thread, so it never blocks
    shutdown. Started per gunicorn worker; concurrent passes are harmless
    because every delete is conditional on the device still being stale.
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
                sweep()
            except Exception:  # never let a bad sweep kill the thread
                log.exception("retention sweep failed")
            time.sleep(interval)

    threading.Thread(target=_run, name="retention-sweeper", daemon=True).start()
    log.info(
        "retention sweeper started: sweep every %.1fh", interval / 3600.0
    )
