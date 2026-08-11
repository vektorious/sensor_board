"""Administrative command line (plan §18).

    python -m app.admin status
    python -m app.admin cleanup [--dry-run]
    python -m app.admin device <device_id>
    python -m app.admin delete-device <device_id>
    python -m app.admin delete-key-data <api_key_hash>

Deliberately small and read-mostly: the plan puts "complex administrative
interfaces" out of scope (§23). This exists so an operator can force a sweep,
inspect the platform, and remove one tester's data without opening a SQLite
shell and hand-writing DELETEs against a live database.

No command prints or accepts a plaintext credential. `delete-key-data` takes
the *hash* recorded on the rows, which is what appears in the ingest log.
"""
import argparse
import json
import sys
from datetime import UTC, datetime

from sqlalchemy import delete
from sqlmodel import func, select

from app import __version__, limits, metrics, retention
from app.config import settings
from app.database import db_size_bytes, get_session, init_db
from app.models import Device, Reading


def cmd_status(_args) -> dict:
    with get_session() as s:
        persistent = s.exec(
            select(func.count(Device.id)).where(Device.persistent == True)  # noqa: E712
        ).one()
        temporary = s.exec(
            select(func.count(Device.id)).where(Device.persistent == False)  # noqa: E712
        ).one()
    size = db_size_bytes()
    return {
        "version": __version__,
        "database": settings.db_path,
        "size_bytes": size,
        "size_mb": round(size / 1024**2, 2),
        "size_warn_at_gb": round(settings.db_size_warn_bytes / 1024**3, 2),
        "size_ceiling_gb": round(settings.db_size_max_bytes / 1024**3, 2),
        "devices_persistent": persistent or 0,
        "devices_temporary": temporary or 0,
        **metrics.summary(),
    }


def cmd_cleanup(args) -> dict:
    if args.dry_run:
        now = datetime.now(UTC)
        with get_session() as s:
            devices = s.exec(select(Device)).all()
            due = [
                d.device_id
                for d in devices
                if retention.is_expired(d, now) and not retention.is_exempt(d, session=s)
            ]
        return {"dry_run": True, "would_delete": sorted(due), "count": len(due)}
    report = retention.sweep()
    return report


def cmd_device(args) -> dict:
    with get_session() as s:
        device = s.exec(
            select(Device).where(Device.device_id == args.device_id)
        ).first()
        if device is None:
            return {"error": "unknown device", "device_id": args.device_id}
        readings = s.exec(
            select(func.count(Reading.id)).where(Reading.device_id == args.device_id)
        ).one()
        sensors = s.exec(
            select(Reading.sensor_type)
            .where(Reading.device_id == args.device_id)
            .distinct()
        ).all()
        expired = retention.is_expired(device)
        return {
            "device_id": device.device_id,
            # Whether a write key exists, never the hash itself.
            "has_write_key": device.write_key_hash is not None,
            "persistent": device.persistent,
            "policy": device.policy,
            "retention_hours": retention.retention_hours_for(device),
            "created_at": str(device.created_at),
            "last_seen_at": str(device.last_seen_at),
            "expired": expired,
            "measurements": readings or 0,
            "sensors": sorted(sensors),
        }


def cmd_delete_device(args) -> dict:
    with get_session() as s:
        readings = s.exec(
            select(func.count(Reading.id)).where(Reading.device_id == args.device_id)
        ).one()
        s.exec(
            delete(Reading)
            .where(Reading.device_id == args.device_id)
            .execution_options(synchronize_session=False)
        )
        result = s.exec(
            delete(Device)
            .where(Device.device_id == args.device_id)
            .execution_options(synchronize_session=False)
        )
        s.commit()
    return {
        "deleted_device": args.device_id,
        "existed": bool(result.rowcount),
        "deleted_measurements": readings or 0,
    }


def cmd_delete_key_data(args) -> dict:
    """Remove every measurement submitted with one API key hash.

    Devices are left alone: a keyless persistent device may have been written
    to by several keys, so deleting it would take other people's data with it.
    """
    with get_session() as s:
        result = s.exec(
            delete(Reading)
            .where(Reading.api_key_hash == args.api_key_hash)
            .execution_options(synchronize_session=False)
        )
        s.commit()
    return {"api_key_hash": args.api_key_hash, "deleted_measurements": result.rowcount}


COMMANDS = {
    "status": cmd_status,
    "cleanup": cmd_cleanup,
    "device": cmd_device,
    "delete-device": cmd_delete_device,
    "delete-key-data": cmd_delete_key_data,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.admin", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="platform totals, device counts, database size")

    cleanup = sub.add_parser("cleanup", help="run the retention sweep now")
    cleanup.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be deleted without deleting it",
    )

    device = sub.add_parser("device", help="inspect one device")
    device.add_argument("device_id")

    delete_device = sub.add_parser("delete-device", help="delete one device and its data")
    delete_device.add_argument("device_id")

    delete_key = sub.add_parser(
        "delete-key-data", help="delete every measurement submitted with an API key hash"
    )
    delete_key.add_argument("api_key_hash")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    init_db()
    limits.init_limits()
    result = COMMANDS[args.command](args)
    print(json.dumps(result, indent=2, default=str))
    return 1 if isinstance(result, dict) and "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
