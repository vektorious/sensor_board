"""Ingestion: ownership, authorization, validation, and storage.

Covers the device-creation, existing-device, and security groups of plan §21.
Each test uses its own device ID so the per-device write-rate limit never
couples one test to another.
"""
import itertools
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.database import get_session
from app.main import app
from app.models import Device
from app.security import hash_secret

client = TestClient(app)
URL = "/sensor/measurement"
AUTH = {"x-api-key": "testkey"}
KEY = "a-strong-client-chosen-write-key"

_ids = itertools.count()


def device_id(label: str) -> str:
    """A device ID unique to one assertion, so tests never share a device."""
    return f"{label}-{next(_ids)}"


def post(body: dict, headers: dict | None = None):
    return client.post(URL, json=body, headers=headers or {})


def claim(device: str, write_key: str | None = KEY, **extra):
    body = {"device_id": device, "sensors": {"temperature": 21.0}, **extra}
    if write_key is not None:
        body["write_key"] = write_key
    return post(body)


def query(sql: str, *params):
    conn = sqlite3.connect(os.environ["DB_PATH"])
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# --- device creation --------------------------------------------------------


def test_anonymous_claim_creates_device_with_client_key():
    d = device_id("claim")
    r = claim(d)
    assert r.status_code == 201
    assert r.json()["status"] == "created"
    assert r.json()["device_id"] == d
    assert r.json()["dashboard_url"] == f"/dashboard/device/{d}"


def test_response_never_returns_the_write_key():
    body = claim(device_id("nokey")).text
    assert KEY not in body
    assert "write_key" not in body


def test_write_key_is_stored_only_as_a_hash():
    d = device_id("hashed")
    claim(d)
    with get_session() as s:
        device = s.exec(select(Device).where(Device.device_id == d)).one()
    assert device.write_key_hash == hash_secret(KEY)
    assert not query("SELECT 1 FROM devices WHERE write_key_hash = ?", KEY)


def test_anonymous_claim_without_write_key_is_401():
    r = claim(device_id("nokeyclaim"), write_key=None)
    assert r.status_code == 401
    assert r.json()["code"] == "missing_write_key"


def test_anonymous_device_is_temporary():
    d = device_id("temp")
    claim(d)
    with get_session() as s:
        device = s.exec(select(Device).where(Device.device_id == d)).one()
    assert device.persistent is False
    assert device.policy == "anonymous"


def test_api_key_claim_without_write_key_creates_persistent_device():
    d = device_id("persist")
    assert post({"device_id": d, "sensors": {"t": 1}}, AUTH).status_code == 201
    with get_session() as s:
        device = s.exec(select(Device).where(Device.device_id == d)).one()
    assert device.persistent is True
    assert device.write_key_hash is None
    assert device.policy == "trusted"


def test_api_key_claim_may_also_set_a_write_key():
    d = device_id("both")
    assert post(
        {"device_id": d, "write_key": KEY, "sensors": {"t": 1}}, AUTH
    ).status_code == 201
    # Persistent, but the write key is now mandatory even with the API key.
    assert post({"device_id": d, "sensors": {"t": 2}}, AUTH).status_code == 401


def test_invalid_api_key_is_401_not_treated_as_anonymous():
    r = post(
        {"device_id": device_id("badkey"), "write_key": KEY, "sensors": {"t": 1}},
        {"x-api-key": "nope"},
    )
    assert r.status_code == 401
    assert r.json()["code"] == "invalid_api_key"


# --- existing devices -------------------------------------------------------


def test_correct_write_key_appends():
    d = device_id("append")
    claim(d)
    r = claim(d)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_missing_write_key_on_claimed_device_is_401():
    d = device_id("missing")
    claim(d)
    r = post({"device_id": d, "sensors": {"t": 1}})
    assert r.status_code == 401
    assert r.json()["code"] == "missing_write_key"


def test_wrong_write_key_is_403():
    d = device_id("wrong")
    claim(d)
    r = claim(d, write_key="not-the-key")
    assert r.status_code == 403
    assert r.json()["code"] == "invalid_write_key"


def test_wrong_write_key_reveals_nothing_about_the_real_one():
    d = device_id("opaque")
    claim(d, write_key="correct-horse")
    near_miss = claim(d, write_key="correct-hors").json()
    unrelated = claim(d, write_key="x").json()
    # Identical responses: no length hint, no "close" hint, no prefix match.
    assert near_miss == unrelated


def test_api_key_never_overrides_a_write_key():
    d = device_id("nooverride")
    claim(d)
    # A valid API key is a second credential, not a master key.
    assert post({"device_id": d, "sensors": {"t": 1}}, AUTH).status_code == 401
    assert post(
        {"device_id": d, "write_key": "wrong", "sensors": {"t": 1}}, AUTH
    ).status_code == 403
    # With the correct write key it goes through, API key or not.
    assert post(
        {"device_id": d, "write_key": KEY, "sensors": {"t": 1}}, AUTH
    ).status_code == 200


def test_any_valid_api_key_may_write_to_a_keyless_device():
    # The documented beta simplification (§7): keyless persistent devices are
    # not bound to the key that created them.
    d = device_id("shared")
    post({"device_id": d, "sensors": {"t": 1}}, AUTH)
    r = post({"device_id": d, "sensors": {"t": 2}}, {"x-api-key": "otherkey"})
    assert r.status_code == 200


def test_anonymous_cannot_write_to_a_keyless_device():
    d = device_id("keyless")
    post({"device_id": d, "sensors": {"t": 1}}, AUTH)
    r = post({"device_id": d, "sensors": {"t": 2}})
    assert r.status_code == 401
    assert r.json()["code"] == "api_key_required"


def test_one_device_key_cannot_write_to_another_device():
    first, second = device_id("dev-a"), device_id("dev-b")
    claim(first, write_key="key-one")
    claim(second, write_key="key-two")
    assert claim(second, write_key="key-one").status_code == 403


def test_rejected_write_does_not_advance_last_seen_or_store_rows():
    d = device_id("rejected")
    claim(d)
    with get_session() as s:
        before = s.exec(select(Device).where(Device.device_id == d)).one().last_seen_at
    rows_before = query("SELECT COUNT(*) FROM readings WHERE device_id = ?", d)[0][0]

    assert claim(d, write_key="wrong").status_code == 403

    with get_session() as s:
        after = s.exec(select(Device).where(Device.device_id == d)).one().last_seen_at
    assert after == before
    assert query("SELECT COUNT(*) FROM readings WHERE device_id = ?", d)[0][0] == rows_before


def test_write_key_cannot_be_changed_through_the_endpoint():
    d = device_id("immutable")
    claim(d, write_key="original")
    # Presenting the correct key does not re-register the one sent alongside.
    claim(d, write_key="original")
    assert claim(d, write_key="replacement").status_code == 403


def test_failed_claim_does_not_create_the_device():
    d = device_id("aborted")
    assert claim(d, write_key=None).status_code == 401
    assert query("SELECT COUNT(*) FROM devices WHERE device_id = ?", d)[0][0] == 0


# --- stored data ------------------------------------------------------------


def test_scalar_and_object_sensor_forms_both_work():
    r = claim(
        device_id("forms"),
        sensors={"temperature": 20.0, "moisture_pct": {"value": 61.0, "unit": "%"}},
    )
    assert r.status_code == 201
    assert r.json()["stored"] == 2


def test_supported_value_types_are_stored_with_their_type():
    d = device_id("types")
    r = claim(d, sensors={"num": 1.5, "flag": True, "label": "idle", "absent": None})
    assert r.status_code == 201
    rows = {
        name: (value, text, kind)
        for name, value, text, kind in query(
            "SELECT sensor_type, value, value_text, value_type FROM readings "
            "WHERE device_id = ?",
            d,
        )
    }
    assert rows["num"] == (1.5, None, "number")
    assert rows["flag"] == (1.0, None, "bool")   # charts still see a number
    assert rows["label"] == (None, "idle", "text")
    assert rows["absent"] == (None, None, "null")


def test_api_key_hash_is_recorded_and_plaintext_is_not():
    d = device_id("attrib")
    post({"device_id": d, "sensors": {"t": 1}}, AUTH)
    hashes = query(
        "SELECT DISTINCT api_key_hash FROM readings WHERE device_id = ?", d
    )
    assert hashes == [(hash_secret("testkey"),)]
    everything = query("SELECT * FROM readings")
    assert all("testkey" not in str(cell) for row in everything for cell in row)


def test_anonymous_rows_carry_no_api_key_hash():
    d = device_id("anon-rows")
    claim(d)
    assert query(
        "SELECT DISTINCT api_key_hash FROM readings WHERE device_id = ?", d
    ) == [(None,)]


def test_creating_ip_is_stored_only_as_a_hash():
    d = device_id("ip")
    claim(d)
    with get_session() as s:
        device = s.exec(select(Device).where(Device.device_id == d)).one()
    assert device.created_from_ip_hash
    assert len(device.created_from_ip_hash) == 64  # sha256 hex, not an address
    assert "." not in device.created_from_ip_hash


# --- secrets never leak -----------------------------------------------------


def test_write_keys_are_never_logged(caplog):
    d = device_id("logging")
    with caplog.at_level("INFO", logger="sensor_board.ingest"):
        claim(d, write_key="super-secret-value")
        claim(d, write_key="wrong-guess")
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert logged  # the request really was logged
    assert "super-secret-value" not in logged
    assert "wrong-guess" not in logged


def test_api_keys_are_never_logged(caplog):
    with caplog.at_level("INFO", logger="sensor_board.ingest"):
        post({"device_id": device_id("keylog"), "sensors": {"t": 1}}, AUTH)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "testkey" not in logged


def test_log_fields_cannot_forge_a_log_line():
    # device_id validation already rejects newlines; this guards the log path
    # itself, which is what would matter if validation ever loosened.
    from app.security import safe_log_value

    assert "\n" not in safe_log_value("evil\ndevice status=200")
    assert safe_log_value("x" * 500).endswith("…")


def test_dashboard_api_never_exposes_key_hashes():
    d = device_id("leak")
    claim(d)
    body = client.get(f"/dashboard/api/device/{d}/sensors").text
    assert "write_key" not in body
    assert "api_key" not in body
    assert hash_secret(KEY) not in body


# --- responses --------------------------------------------------------------


@pytest.mark.parametrize(
    "body, code",
    [
        ({"sensors": {"t": 1}}, "missing_device_id"),
        ({"device_id": "d"}, "missing_sensors"),
        ({"device_id": "d", "sensors": {}}, "empty_sensors"),
        ({"device_id": "d", "sensors": {"t": 1}, "nope": 1}, "unknown_field"),
        ({"device_id": "bad/id", "sensors": {"t": 1}}, "invalid_device_id"),
    ],
)
def test_validation_failures_return_a_machine_readable_code(body, code):
    r = post({**body, "write_key": KEY})
    assert r.status_code == 400
    assert r.json()["code"] == code


def test_malformed_json_is_400():
    r = client.post(URL, content="not json")
    assert r.status_code == 400
    assert r.json()["code"] == "malformed_json"


def test_errors_never_expose_internals():
    r = post({"device_id": "d", "write_key": KEY, "sensors": {"t": [1, 2, 3]}})
    assert r.status_code == 400
    assert "Traceback" not in r.text
    assert "sqlite" not in r.text.lower()


def test_security_headers_are_present_on_every_response():
    headers = client.get("/dashboard/").headers
    assert "default-src 'self'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
