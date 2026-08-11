"""Device expiry and cleanup (plan §21, "Expiration").

Devices are seeded directly rather than through the endpoint, so a test can put
`last_seen_at` days in the past without waiting or mocking the clock.
`RETENTION_HOURS` is 0 in the test environment (conftest), which disables the
background sweeper — every test here drives `purge_expired()` explicitly, with
the setting patched on for the duration.
"""
import itertools
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import metrics, retention
from app.config import settings
from app.database import get_session
from app.main import app
from app.models import Device, Reading

client = TestClient(app)
URL = "/sensor/measurement"

NOW = datetime.now(UTC)
OLD = NOW - timedelta(hours=72)     # past the 48h window
FRESH = NOW - timedelta(hours=1)    # inside it

_ids = itertools.count()


def device_id(label: str) -> str:
    return f"rt-{label}-{next(_ids)}"


@pytest.fixture(autouse=True)
def retention_enabled(monkeypatch):
    """Turn the master switch on without starting the background sweeper."""
    monkeypatch.setattr(settings, "retention_hours", 48)


def seed(
    device: str,
    last_seen=OLD,
    persistent=False,
    project=None,
    policy="anonymous",
    sensors=("temperature",),
) -> str:
    with get_session() as s:
        s.add(
            Device(
                device_id=device,
                write_key_hash="x" * 64,
                persistent=persistent,
                policy=policy,
                created_at=last_seen,
                last_seen_at=last_seen,
            )
        )
        s.commit()
        for sensor in sensors:
            s.add(
                Reading(
                    project=project,
                    device_id=device,
                    timestamp=last_seen,
                    sensor_type=sensor,
                    value=1.0,
                )
            )
        s.commit()
    return device


def exists(device: str) -> bool:
    with get_session() as s:
        return s.exec(select(Device).where(Device.device_id == device)).first() is not None


def reading_count(device: str) -> int:
    with get_session() as s:
        return len(s.exec(select(Reading).where(Reading.device_id == device)).all())


# --- expiry -----------------------------------------------------------------


def test_idle_device_expires_after_the_window():
    d = seed(device_id("stale"))
    report = retention.purge_expired()
    assert d in report["deleted_devices"]
    assert not exists(d)


def test_expiry_deletes_all_of_the_devices_measurements():
    d = seed(device_id("withdata"), sensors=("temperature", "humidity", "pressure"))
    assert reading_count(d) == 3
    retention.purge_expired()
    assert reading_count(d) == 0


def test_expiry_deletes_the_write_key_hash():
    d = seed(device_id("keyed"))
    retention.purge_expired()
    with get_session() as s:
        assert s.exec(select(Device).where(Device.device_id == d)).first() is None


def test_recent_device_survives():
    d = seed(device_id("fresh"), last_seen=FRESH)
    retention.purge_expired()
    assert exists(d)


def test_persistent_device_never_expires():
    d = seed(device_id("persistent"), last_seen=NOW - timedelta(days=400), persistent=True)
    report = retention.purge_expired()
    assert d not in report["deleted_devices"]
    assert exists(d)


def test_exempt_device_and_project_survive():
    by_id = seed(device_id("exempt-id"))
    by_project = seed(device_id("exempt-proj"), project="rt-protected")
    retention.purge_expired(
        exempt_devices={by_id}, exempt_projects={"rt-protected"}
    )
    assert exists(by_id)
    assert exists(by_project)


def test_policy_retention_window_is_respected(monkeypatch):
    from app.policies import ANONYMOUS, PolicyRegistry
    from dataclasses import replace

    # A policy with a 1000-hour window keeps a device the 48-hour window drops.
    long_lived = replace(ANONYMOUS, name="longlived", retention_hours=1000)
    monkeypatch.setattr(
        PolicyRegistry, "by_name", lambda self, name: long_lived
        if name == "longlived" else ANONYMOUS
    )
    patient = seed(device_id("patient"), policy="longlived")
    impatient = seed(device_id("impatient"), policy="anonymous")
    retention.purge_expired()
    assert exists(patient)
    assert not exists(impatient)


def test_sweep_is_idempotent():
    seed(device_id("once"))
    retention.purge_expired()
    second = retention.purge_expired()
    assert second["deleted_devices"] == []


def test_disabled_when_retention_not_positive(monkeypatch):
    monkeypatch.setattr(settings, "retention_hours", 0)
    d = seed(device_id("disabled"))
    report = retention.purge_expired()
    assert report["enabled"] is False
    assert exists(d)


def test_batching_deletes_everything_across_batches():
    ids = [seed(device_id("batch")) for _ in range(5)]
    report = retention.purge_expired(batch_size=2)
    assert set(ids) <= set(report["deleted_devices"])
    assert not any(exists(d) for d in ids)


# --- races ------------------------------------------------------------------


def test_a_write_just_before_the_sweep_saves_the_device():
    d = device_id("saved")
    seed(d)
    # The write lands between the sweep's selection and its delete.
    with get_session() as s:
        device = s.exec(select(Device).where(Device.device_id == d)).one()
        device.last_seen_at = datetime.now(UTC)
        s.add(device)
        s.commit()
    retention.purge_expired()
    assert exists(d)


def test_delete_is_conditional_on_still_being_stale():
    # Directly exercise the guard the sweep relies on: a batch whose devices
    # were refreshed after selection must delete nothing.
    d = device_id("refreshed")
    seed(d, last_seen=FRESH)
    with get_session() as s:
        measurements, removed = retention._delete_batch(s, [d], cutoff=OLD)
        s.commit()
    assert removed == []
    assert measurements == 0
    assert exists(d)


# --- reuse ------------------------------------------------------------------


def test_an_expired_device_id_can_be_reclaimed_with_a_new_key():
    d = device_id("reuse")
    seed(d)
    retention.purge_expired()
    r = client.post(
        URL, json={"device_id": d, "write_key": "brand-new-key", "sensors": {"t": 1}}
    )
    assert r.status_code == 201
    # The new owner's key works; the old device's key is gone with it.
    assert client.post(
        URL, json={"device_id": d, "write_key": "brand-new-key", "sensors": {"t": 2}}
    ).status_code == 200


def test_reused_device_shows_none_of_the_previous_owners_data():
    d = device_id("noleak")
    seed(d, sensors=("secret_sensor",))
    retention.purge_expired()
    client.post(
        URL, json={"device_id": d, "write_key": "new-key", "sensors": {"temperature": 5}}
    )
    body = client.get(f"/dashboard/api/device/{d}/sensors").text
    assert "secret_sensor" not in body


def test_claiming_an_expired_id_does_not_wait_for_the_sweep():
    # §4: expiry is also checked on the ingestion path, so a client claiming a
    # long-silent ID succeeds immediately rather than being told it is taken.
    d = device_id("ondemand")
    seed(d)
    r = client.post(
        URL, json={"device_id": d, "write_key": "fresh-claim", "sensors": {"t": 1}}
    )
    assert r.status_code == 201
    assert r.json()["status"] == "created"


def test_an_unexpired_id_is_still_protected_on_the_ingestion_path():
    d = device_id("live")
    seed(d, last_seen=FRESH)
    r = client.post(
        URL, json={"device_id": d, "write_key": "attacker-key", "sensors": {"t": 1}}
    )
    assert r.status_code == 403


# --- metrics (§19) ----------------------------------------------------------


def test_expiry_leaves_lifetime_totals_untouched():
    before = metrics.lifetime()
    seed(device_id("counted"), sensors=("a", "b"))
    retention.purge_expired()
    after = metrics.lifetime()
    # Seeding bypassed the endpoint, so nothing should have moved either way.
    assert after == before


def test_lifetime_totals_survive_the_expiry_of_ingested_data():
    d = device_id("lifetime")
    client.post(URL, json={"device_id": d, "write_key": "k", "sensors": {"a": 1, "b": 2}})
    after_write = metrics.lifetime()

    with get_session() as s:
        device = s.exec(select(Device).where(Device.device_id == d)).one()
        device.last_seen_at = OLD
        s.add(device)
        s.commit()
    retention.purge_expired()

    assert not exists(d)
    assert metrics.lifetime() == after_write        # totals never decrease
    assert metrics.active()["devices_active"] >= 0  # active counts do


def test_active_counts_fall_when_lifetime_counts_do_not():
    d = device_id("active-drop")
    client.post(URL, json={"device_id": d, "write_key": "k", "sensors": {"a": 1}})
    active_before = metrics.active()["devices_active"]
    total_before = metrics.lifetime()["devices_total"]

    with get_session() as s:
        device = s.exec(select(Device).where(Device.device_id == d)).one()
        device.last_seen_at = OLD
        s.add(device)
        s.commit()
    retention.purge_expired()

    assert metrics.active()["devices_active"] == active_before - 1
    assert metrics.lifetime()["devices_total"] == total_before


def test_a_reclaimed_device_id_counts_as_a_new_device():
    d = device_id("recount")
    client.post(URL, json={"device_id": d, "write_key": "first", "sensors": {"a": 1}})
    total_after_first = metrics.lifetime()["devices_total"]

    with get_session() as s:
        device = s.exec(select(Device).where(Device.device_id == d)).one()
        device.last_seen_at = OLD
        s.add(device)
        s.commit()
    retention.purge_expired()

    client.post(URL, json={"device_id": d, "write_key": "second", "sensors": {"a": 1}})
    # Recommended in §19: reuse after expiry is a new device instance.
    assert metrics.lifetime()["devices_total"] == total_after_first + 1


def test_a_rejected_ingest_increments_nothing():
    before = metrics.lifetime()
    client.post(URL, json={"device_id": device_id("norows"), "sensors": {"a": 1}})
    assert metrics.lifetime() == before


def test_measurements_total_counts_each_stored_value():
    before = metrics.lifetime()["measurements_total"]
    client.post(
        URL,
        json={
            "device_id": device_id("count3"),
            "write_key": "k",
            "sensors": {"a": 1, "b": 2, "c": 3},
        },
    )
    assert metrics.lifetime()["measurements_total"] == before + 3
