"""Tests for automatic retention (stale device/project deletion).

Uses distinctive identifiers and explicit parameters so it is independent of any
other rows already in the shared test DB.
"""
import os
import tempfile

os.environ.setdefault("API_KEYS", "testkey")
os.environ.setdefault("DB_PATH", tempfile.mkstemp(suffix=".db")[1])
# Keep the background sweeper from starting on import in this process.
os.environ.setdefault("RETENTION_HOURS", "0")

from datetime import UTC, datetime, timedelta  # noqa: E402

from app import retention  # noqa: E402
from app.database import get_session, init_db  # noqa: E402
from app.models import Reading  # noqa: E402
from app.queries import list_devices, list_projects  # noqa: E402

init_db()

NOW = datetime.now(UTC)
OLD = NOW - timedelta(hours=72)    # past the 48h window
FRESH = NOW - timedelta(hours=1)   # inside the window
P = "rt-"  # id prefix to isolate this test's rows from other tests' rows


def _seed():
    rows = [
        Reading(project=P + "old", device_uuid=P + "stale-solo", timestamp=OLD, sensor_type="t", value=1.0),
        Reading(project=P + "mixed", device_uuid=P + "stale-mix", timestamp=OLD, sensor_type="t", value=1.0),
        Reading(project=P + "mixed", device_uuid=P + "fresh-mix", timestamp=FRESH, sensor_type="t", value=2.0),
        Reading(project=P + "exdev", device_uuid=P + "keep-dev", timestamp=OLD, sensor_type="t", value=1.0),
        Reading(project=P + "keep", device_uuid=P + "stale-exproj", timestamp=OLD, sensor_type="t", value=1.0),
        Reading(project=P + "active", device_uuid=P + "fresh", timestamp=FRESH, sensor_type="t", value=3.0),
    ]
    with get_session() as s:
        s.add_all(rows)
        s.commit()


def _purge():
    return retention.purge_stale(
        retention_hours=48,
        exempt_devices={P + "keep-dev"},
        exempt_projects={P + "keep"},
    )


def test_purge_deletes_unexempt_stale_and_removes_emptied_projects():
    _seed()
    report = _purge()

    assert report["enabled"] is True
    # Only the stale, non-exempt devices are deleted.
    assert set(report["deleted_devices"]) == {P + "stale-solo", P + "stale-mix"}
    # A project is reported removed only once all its devices are gone.
    assert P + "old" in report["removed_projects"]
    assert P + "mixed" not in report["removed_projects"]

    devices = {d["device_uuid"] for d in list_devices()}
    assert P + "stale-solo" not in devices
    assert P + "stale-mix" not in devices
    # Fresh, exempt-by-device, and exempt-by-project devices all survive.
    assert {P + "fresh-mix", P + "keep-dev", P + "stale-exproj", P + "fresh"} <= devices

    projects = {p["project"] for p in list_projects()}
    assert P + "old" not in projects        # solo stale project vanished
    assert P + "mixed" in projects          # kept: still has a fresh device
    assert P + "keep" in projects           # kept: exempt project


def test_purge_is_idempotent():
    # Second run on the already-swept data deletes nothing new.
    report = _purge()
    assert report["deleted_devices"] == []
    assert report["removed_projects"] == []


def test_disabled_when_retention_not_positive():
    report = retention.purge_stale(retention_hours=0)
    assert report["enabled"] is False
    assert report["deleted_devices"] == []
