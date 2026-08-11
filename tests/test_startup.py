"""Schema setup under concurrent workers.

Gunicorn boots several workers at once and every one of them runs `init_db()`.
Being idempotent across *sequential* runs is not enough — two processes that
each check "does this table exist?" before either has finished creating it will
both try, and the loser dies with "index … already exists". That is exactly how
the 0.2 upgrade failed on the first real deployment, so it is tested with real
processes rather than threads: the file lock that fixes it is cross-process, and
threads in one interpreter would not exercise it.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# What a worker does at startup, reduced to the part that touches the schema.
#
# The barrier matters. Left to themselves, the processes stagger by however long
# each interpreter takes to import the app, which is easily enough for one to
# finish before the next begins — and then the race never happens and the test
# passes against broken code. Every worker imports first, then waits for a wall
# clock instant they all share, so they enter init_db() together.
_BOOT = """
import os, time
from app.database import init_db
from app.limits import init_limits

start_at = float(os.environ["BOOT_AT"])
while time.time() < start_at:
    time.sleep(0.001)

init_db()
init_limits()
print("booted")
"""

# How long to give every worker to import before the barrier opens.
_BARRIER_DELAY_SECONDS = 3.0

# The pre-0.2 shape: `device_uuid`, no devices table, no foreign key.
_LEGACY_SCHEMA = """
CREATE TABLE readings (
    id INTEGER NOT NULL PRIMARY KEY,
    project VARCHAR,
    device_uuid VARCHAR NOT NULL,
    device_name VARCHAR,
    timestamp DATETIME NOT NULL,
    sensor_type VARCHAR NOT NULL,
    value FLOAT,
    unit VARCHAR,
    plot VARCHAR,
    api_key_hash VARCHAR
);
CREATE INDEX ix_readings_device_uuid ON readings (device_uuid);
CREATE INDEX ix_readings_timestamp ON readings (timestamp);
CREATE INDEX ix_readings_project ON readings (project);
CREATE INDEX ix_readings_device_sensor_ts
    ON readings (device_uuid, sensor_type, timestamp);
"""


def legacy_db(rows: int = 5) -> str:
    path = tempfile.mkstemp(suffix=".db")[1]
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_LEGACY_SCHEMA)
        conn.executemany(
            "INSERT INTO readings (project, device_uuid, timestamp, sensor_type, value)"
            " VALUES (?,?,?,?,?)",
            [("demo", "old-device", "2026-07-01 10:00:00", "temperature", 20.0 + i)
             for i in range(rows)],
        )
        conn.commit()
    finally:
        conn.close()
    return path


def boot(db_path: str, count: int = 1) -> list[subprocess.CompletedProcess]:
    """Start `count` workers and release them into init_db() simultaneously."""
    env = {
        **os.environ,
        "DB_PATH": db_path,
        "API_KEY": "x",
        "RETENTION_HOURS": "0",
        "BOOT_AT": str(time.time() + _BARRIER_DELAY_SECONDS),
    }
    running = [
        subprocess.Popen(
            [sys.executable, "-c", _BOOT],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(count)
    ]
    results = []
    for process in running:
        out, err = process.communicate(timeout=60)
        results.append(subprocess.CompletedProcess(process.args, process.returncode, out, err))
    return results


def tables(db_path: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()


def count_rows(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


@pytest.mark.parametrize("workers", [2, 4])
def test_concurrent_workers_all_boot_on_a_fresh_database(workers):
    db = tempfile.mkstemp(suffix=".db")[1]
    results = boot(db, count=workers)
    for result in results:
        assert result.returncode == 0, result.stderr
    assert tables(db) >= {"devices", "readings", "platform_metrics", "rate_counters"}


@pytest.mark.parametrize("workers", [2, 4])
def test_concurrent_workers_all_boot_while_retiring_a_legacy_table(workers):
    # The upgrade case: every worker sees a pre-0.2 database at once.
    db = legacy_db()
    results = boot(db, count=workers)
    for result in results:
        assert result.returncode == 0, result.stderr
        assert "already exists" not in result.stderr


def test_legacy_rows_are_never_destroyed_by_a_racing_worker():
    db = legacy_db(rows=7)
    boot(db, count=4)
    present = tables(db)
    assert "readings_pre_v0_2" in present
    # Every original row is still readable; only one table was parked, so no
    # worker overwrote another's copy of the data.
    assert count_rows(db, "readings_pre_v0_2") == 7
    assert count_rows(db, "readings") == 0
    assert not [t for t in present if t.startswith("readings_pre_v0_2_")]


def test_restart_after_migration_parks_nothing_further():
    db = legacy_db()
    boot(db, count=2)
    before = tables(db)
    boot(db, count=2)          # a later restart, as supervisord would do
    assert tables(db) == before


def test_a_second_legacy_table_is_parked_beside_the_first_not_over_it():
    # Contrived, but it is the case that would silently destroy data: the
    # parked table holds the only copy of the old rows.
    db = legacy_db(rows=3)
    boot(db, count=1)
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP TABLE readings")
        conn.executescript(_LEGACY_SCHEMA)   # a second pre-0.2 table appears
        conn.commit()
    finally:
        conn.close()
    boot(db, count=1)
    assert count_rows(db, "readings_pre_v0_2") == 3     # untouched
    assert "readings_pre_v0_2_2" in tables(db)
