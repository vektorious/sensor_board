"""Limit enforcement (plan §21, "Limits").

The endpoint-level tests build a deliberately tiny policy and patch it in, so
each limit can be tripped in a few requests instead of hundreds. The values
themselves — 30 requests a minute, 16 sensor fields, and the rest of §24 — are
asserted separately in `test_policies.py`; here the question is only whether a
limit that exists is actually enforced, and with which status and code.
"""
import itertools
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import limits
from app.main import app
from app.policies import registry

client = TestClient(app)
URL = "/sensor/measurement"
KEY = "write-key-for-limit-tests"

_ids = itertools.count()


def device_id(label: str) -> str:
    return f"lim-{label}-{next(_ids)}"


def claim(device: str, sensors=None, write_key=KEY, using=None):
    return (using or client).post(
        URL,
        json={
            "device_id": device,
            "write_key": write_key,
            "sensors": sensors or {"temperature": 20.0},
        },
    )


@pytest.fixture
def fresh_ip():
    """A client whose requests come from an address no other test has used.

    IP-scoped limits (requests per minute, devices per hour, active devices)
    accumulate across the whole suite otherwise, because every TestClient
    request reports the same host — so a test with a deliberately tiny cap
    would trip on counts left behind by its predecessors.
    """
    host = f"198.51.100.{next(_ids) % 250 + 1}-{next(_ids)}"
    return TestClient(app, client=(host, 5000))


@pytest.fixture
def tiny_policy(monkeypatch):
    """Install a restrictive anonymous policy for the duration of one test.

    Built from the *configured* anonymous policy, not from the `ANONYMOUS`
    defaults: conftest raises the IP-scoped limits because every test shares
    one client address, and starting from the defaults would quietly put them
    back, throttling tests that are about something else entirely.
    """
    configured = registry.anonymous

    def install(**overrides):
        policy = replace(configured, **overrides)
        monkeypatch.setattr(
            type(registry), "anonymous", property(lambda self: policy)
        )
        return policy

    return install


# --- payload size (§9) ------------------------------------------------------


def test_oversized_body_is_413():
    r = client.post(
        URL,
        json={
            "device_id": device_id("big"),
            "write_key": KEY,
            "sensors": {"temperature": 20.0},
            "name": "A" * 40_000,
        },
    )
    assert r.status_code == 413
    assert r.json()["code"] == "payload_too_large"
    assert "16 KB" in r.json()["error"]


def test_oversized_body_is_refused_without_a_content_length_header():
    # A chunked request declares no length, so the header check cannot fire and
    # the stream cap is the only thing standing between us and the whole body.
    def chunks():
        yield b'{"device_id": "stream-test", "write_key": "k", "sensors": {"t": 1}, "pad": "'
        for _ in range(40):
            yield b"A" * 1024
        yield b'"}'

    r = client.post(URL, content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_lying_content_length_is_still_refused():
    r = client.post(
        URL,
        content=b"x" * 40_000,
        headers={"content-type": "application/json", "content-length": "10"},
    )
    # Starlette truncates the body to the declared length, so this arrives as
    # malformed JSON rather than as an oversized body — either way, the 40 KB
    # never reaches the parser.
    assert r.status_code in (400, 413)


# --- sensor counts (§12) ----------------------------------------------------


def test_too_many_sensor_fields_in_one_request_is_400():
    sensors = {f"s{i}": float(i) for i in range(20)}  # default cap is 16
    r = claim(device_id("fields"), sensors)
    assert r.status_code == 400
    assert r.json()["code"] == "too_many_sensor_fields"


def test_distinct_sensors_per_device_is_capped(tiny_policy):
    tiny_policy(max_sensors_per_device=3, max_sensor_fields_per_request=8)
    d = device_id("cardinality")
    assert claim(d, {"a": 1, "b": 2, "c": 3}).status_code == 201
    # Same three again is fine — the cap is on distinct names, not on writes.
    assert claim(d, {"a": 4, "b": 5, "c": 6}).status_code == 200
    r = claim(d, {"d": 7})
    assert r.status_code == 429
    assert r.json()["code"] == "too_many_sensors"


# --- request rates (§10) ----------------------------------------------------


def test_per_ip_request_limit_returns_429_with_retry_after(tiny_policy, fresh_ip):
    tiny_policy(requests_per_minute_per_ip=3)
    codes = [claim(device_id("ip"), using=fresh_ip).status_code for _ in range(5)]
    assert 429 in codes
    r = claim(device_id("ip"), using=fresh_ip)
    assert r.status_code == 429
    assert r.json()["code"] == "ip_minute_rate_limited"
    assert int(r.headers["retry-after"]) >= 1


def test_per_ip_limit_counts_failed_requests_too(tiny_policy, fresh_ip):
    tiny_policy(requests_per_minute_per_ip=3)
    # Rejected payloads still consume the allowance, so probing is not cheap.
    for _ in range(4):
        fresh_ip.post(URL, json={"nonsense": True})
    assert claim(device_id("ipfail"), using=fresh_ip).status_code == 429


def test_per_device_write_limit_is_429(tiny_policy):
    tiny_policy(writes_per_minute_per_device=2)
    d = device_id("devrate")
    assert claim(d).status_code == 201
    assert claim(d).status_code == 200
    r = claim(d)
    assert r.status_code == 429
    assert r.json()["code"] == "device_minute_rate_limited"


# --- device creation (§11) --------------------------------------------------


def test_new_device_creation_per_ip_is_limited(tiny_policy, fresh_ip):
    tiny_policy(new_devices_per_hour_per_ip=2)
    assert claim(device_id("create"), using=fresh_ip).status_code == 201
    assert claim(device_id("create"), using=fresh_ip).status_code == 201
    r = claim(device_id("create"), using=fresh_ip)
    assert r.status_code == 429
    assert r.json()["code"] == "device_creation_limited"


def test_writing_to_an_existing_device_is_not_a_creation(tiny_policy, fresh_ip):
    tiny_policy(new_devices_per_hour_per_ip=1)
    d = device_id("notcreate")
    assert claim(d, using=fresh_ip).status_code == 201
    # The allowance is spent, but appending to the device just claimed is not
    # a creation and must keep working.
    assert claim(d, using=fresh_ip).status_code == 200


def test_failed_creation_does_not_consume_the_hourly_allowance(tiny_policy, fresh_ip):
    tiny_policy(new_devices_per_hour_per_ip=1)
    # A claim rejected for a missing write key never created anything, so it
    # must not spend the slot a real claim would have used.
    assert claim(device_id("burned"), write_key=None, using=fresh_ip).status_code == 401
    assert claim(device_id("burned"), using=fresh_ip).status_code == 201


def test_active_device_cap_per_ip(tiny_policy, fresh_ip):
    tiny_policy(active_devices_per_ip=1, new_devices_per_hour_per_ip=100)
    assert claim(device_id("active"), using=fresh_ip).status_code == 201
    r = claim(device_id("active"), using=fresh_ip)
    assert r.status_code == 429
    assert r.json()["code"] == "active_device_limit"


# --- write budget and storage ceiling (§24) ---------------------------------


def test_global_write_budget_sheds_load():
    # A bucket subject no other test uses, so the outcome depends only on what
    # this test spends.
    limits.check_bucket("test_budget", "subject", 1, 2, 2)  # drain it
    decision = limits.check_bucket("test_budget", "subject", 1, 2, 2)
    assert not decision
    assert decision.code == "test_budget_over_budget"
    assert decision.retry_after >= 1


def test_a_request_larger_than_the_burst_is_refused_outright():
    decision = limits.check_bucket("test_oversize", "subject", 10, 5, 50)
    assert not decision
    assert "burst" in decision.message


def test_bucket_refills_over_time():
    limits.check_bucket("test_refill", "s", 1000, 10, 10)  # drain
    # 1000 tokens/sec refills the 10-token bucket almost immediately.
    import time

    time.sleep(0.05)
    assert limits.check_bucket("test_refill", "s", 1000, 10, 10)


def test_storage_ceiling_returns_503(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "db_size_max_bytes", 1)
    r = claim(device_id("full"))
    assert r.status_code == 503
    assert r.json()["code"] == "storage_full"
    assert int(r.headers["retry-after"]) > 0


def test_storage_ceiling_blocks_before_anything_is_written(monkeypatch):
    from app.config import settings

    d = device_id("full-nowrite")
    monkeypatch.setattr(settings, "db_size_max_bytes", 1)
    claim(d)
    monkeypatch.undo()
    # The device was never created, so this is still a first write.
    assert claim(d).status_code == 201


# --- window bookkeeping -----------------------------------------------------


def test_windows_are_swept_so_the_table_cannot_grow_forever():
    limits.check_window("sweep_scope", "subject", 100, 60)
    removed = limits.sweep_expired_windows(older_than_seconds=-1)
    assert removed >= 1


def seed_size_sample(size_bytes: int, hours_ago: float) -> None:
    """Backdate the stored database-size sample.

    Growth is measured between two samples, so a test that wants a rate has to
    place the earlier one in the past — two real samples taken milliseconds
    apart are exactly the case the code now refuses to extrapolate from.
    """
    from datetime import UTC, datetime, timedelta

    from app.database import get_session
    from app.models import PlatformMetric

    stamp = datetime.now(UTC) - timedelta(hours=hours_ago)
    with get_session() as s:
        row = s.get(PlatformMetric, "db_size_bytes")
        if row is None:
            row = PlatformMetric(metric_name="db_size_bytes", metric_value=size_bytes)
        row.metric_value = size_bytes
        row.updated_at = stamp
        s.add(row)
        s.commit()


def test_db_size_observation_records_a_gauge_and_a_rate():
    seed_size_sample(1, hours_ago=1)
    report = limits.observe_db_size()
    assert report["size_bytes"] > 0
    assert report["mb_per_hour"] is not None


def test_growth_warning_fires_above_the_threshold(monkeypatch, caplog):
    from app.config import settings

    seed_size_sample(1, hours_ago=1)
    monkeypatch.setattr(settings, "db_growth_warn_mb_per_hour", -1.0)
    with caplog.at_level("WARNING", logger="sensor_board.limits"):
        report = limits.observe_db_size()
    assert report["warnings"]
    assert "MB/h" in caplog.text


def test_no_growth_rate_is_reported_from_samples_taken_moments_apart(monkeypatch, caplog):
    from app.config import settings

    # Two restarts seconds apart would otherwise divide an ordinary WAL
    # checkpoint by ~0 and report thousands of MB/h — which is what the first
    # real deployment logged.
    monkeypatch.setattr(settings, "db_growth_warn_mb_per_hour", 0.001)
    limits.observe_db_size()
    with caplog.at_level("WARNING", logger="sensor_board.limits"):
        report = limits.observe_db_size()
    assert report["mb_per_hour"] is None
    assert "MB/h" not in caplog.text
