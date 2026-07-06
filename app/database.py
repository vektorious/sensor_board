"""SQLite engine + session helpers.

WAL mode is enabled so the ingestion writes never block dashboard reads (and
vice versa) across gunicorn workers. Extra composite indexes back the two hot
query shapes: per-device time-series and per-project scans.
"""
from sqlalchemy import Index, event, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings
from app.models import Reading

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
    cur.close()


# Composite indexes for the queries the dashboard actually runs.
Index(
    "ix_readings_device_sensor_ts",
    Reading.__table__.c.device_uuid,
    Reading.__table__.c.sensor_type,
    Reading.__table__.c.timestamp,
)
Index(
    "ix_readings_project_ts",
    Reading.__table__.c.project,
    Reading.__table__.c.timestamp,
)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _migrate()


def _migrate() -> None:
    """Idempotent, dependency-free schema migrations for existing SQLite files.

    create_all() creates missing tables but never alters existing ones, so a DB
    from before a column was added needs a manual ALTER. Kept tiny on purpose —
    no Alembic for a beta.
    """
    with engine.connect() as conn:
        cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info('readings')").fetchall()]
        if cols and "api_key_hash" not in cols:
            conn.exec_driver_sql("ALTER TABLE readings ADD COLUMN api_key_hash VARCHAR")
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_readings_api_key_hash "
                "ON readings (api_key_hash)"
            )
            conn.commit()


def get_session() -> Session:
    return Session(engine)
