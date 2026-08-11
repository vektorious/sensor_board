"""SQLite engine, session helpers, and schema migration.

WAL mode is enabled so the ingestion writes never block dashboard reads (and
vice versa) across gunicorn workers. Extra composite indexes back the two hot
query shapes: per-device time-series and per-project scans.

Migrations are hand-rolled and idempotent — no Alembic for a beta. Every one is
safe to run against a fresh database and against a database that has already
been migrated, because startup runs them unconditionally in every worker.
"""
import logging

from sqlalchemy import Index, event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings
from app.models import Device, PlatformMetric, Reading  # noqa: F401  (registers tables)

log = logging.getLogger("sensor_board.database")

engine = create_engine(
    settings.database_url,
    echo=False,
    connect_args={"check_same_thread": False},
)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _connection_record):
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=5000")
    # Off by default in SQLite; without it the readings -> devices cascade
    # would silently do nothing.
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


# Composite indexes for the queries the dashboard actually runs.
Index(
    "ix_readings_device_sensor_ts",
    Reading.__table__.c.device_id,
    Reading.__table__.c.sensor_type,
    Reading.__table__.c.timestamp,
)
Index(
    "ix_readings_project_ts",
    Reading.__table__.c.project,
    Reading.__table__.c.timestamp,
)

# Where a pre-0.2 `readings` table gets moved aside. Kept rather than dropped
# so nothing is destroyed silently; safe to delete by hand afterwards.
_LEGACY_READINGS = "readings_pre_v0_2"


def init_db() -> None:
    _retire_legacy_readings()
    SQLModel.metadata.create_all(engine)


def _table_columns(conn, table: str) -> list[str]:
    return [r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info('{table}')").fetchall()]


def _retire_legacy_readings() -> None:
    """Move a pre-0.2 `readings` table aside so create_all() can build the new one.

    0.2 renamed `device_uuid` to `device_id`, added a foreign key to `devices`,
    and changed what a measurement can hold. Pre-0.2 rows are *not* carried
    over: the old data has no owner in the new model (no device row, no write
    key), and the beta has one real device, so migrating it would be more
    machinery than it is worth. The rows are renamed rather than dropped so an
    operator can still inspect or export them before deleting the table.
    """
    with engine.connect() as conn:
        cols = _table_columns(conn, "readings")
        if not cols or "device_uuid" not in cols:
            return  # fresh database, or already on the 0.2 schema

        # Indexes follow the renamed table but keep their names, which would
        # collide with the ones create_all() is about to create.
        for (name,) in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='readings' AND sql IS NOT NULL"
        ).fetchall():
            conn.exec_driver_sql(f'DROP INDEX IF EXISTS "{name}"')
        conn.exec_driver_sql(f"DROP TABLE IF EXISTS {_LEGACY_READINGS}")
        conn.exec_driver_sql(f"ALTER TABLE readings RENAME TO {_LEGACY_READINGS}")
        conn.commit()

    log.warning(
        "pre-0.2 readings table found; renamed to %s and starting fresh. "
        "Drop that table once you no longer need the old rows.",
        _LEGACY_READINGS,
    )


def get_session() -> Session:
    return Session(engine)


def db_size_bytes() -> int:
    """Size of the database on disk, including the WAL, in bytes."""
    from pathlib import Path

    total = 0
    base = Path(settings.db_path)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(base) + suffix)
        if p.exists():
            total += p.stat().st_size
    return total
