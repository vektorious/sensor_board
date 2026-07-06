"""Tests for the hardened ingestion endpoint.

Env must be set before importing the app, because app.config reads it at import.
"""
import os
import sqlite3
import tempfile

os.environ["API_KEYS"] = "testkey,otherkey"
os.environ["ROOT_PATH"] = ""
_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db")
os.environ["DB_PATH"] = _DB_PATH

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.security import hash_api_key  # noqa: E402

client = TestClient(app)
URL = "/sensor/measurement"
AUTH = {"x-api-key": "testkey"}


def test_missing_key_is_401():
    r = client.post(URL, json={"device_uuid": "d", "sensors": {"t": 1}})
    assert r.status_code == 401
    assert r.json()["error"] == "Invalid or missing API key"
    assert "X-API-Key" in r.json()["hint"]


def test_wrong_key_is_401():
    r = client.post(URL, headers={"x-api-key": "nope"}, json={"device_uuid": "d", "sensors": {"t": 1}})
    assert r.status_code == 401


def test_valid_ingest_stores_rows_and_hash():
    r = client.post(URL, headers=AUTH, json={"device_uuid": "dev1", "sensors": {"temperature": 22.4, "humidity": 51}})
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "stored": 2}

    conn = sqlite3.connect(_DB_PATH)
    try:
        hashes = [row[0] for row in conn.execute(
            "SELECT DISTINCT api_key_hash FROM readings WHERE device_uuid='dev1'")]
    finally:
        conn.close()
    assert hashes == [hash_api_key("testkey")]


def test_plaintext_key_never_stored():
    client.post(URL, headers=AUTH, json={"device_uuid": "dev2", "sensors": {"t": 1}})
    conn = sqlite3.connect(_DB_PATH)
    try:
        # No column value anywhere equals the plaintext key.
        rows = conn.execute("SELECT * FROM readings").fetchall()
    finally:
        conn.close()
    assert all("testkey" not in str(cell) for row in rows for cell in row)


def test_bare_number_and_dict_forms_both_work():
    r = client.post(URL, headers=AUTH, json={
        "device_uuid": "dev3",
        "sensors": {"temperature": 20.0, "moisture_pct": {"value": 61.0, "unit": "%"}},
    })
    assert r.status_code == 200
    assert r.json()["stored"] == 2


def test_missing_device_is_400():
    r = client.post(URL, headers=AUTH, json={"sensors": {"t": 1}})
    assert r.status_code == 400
    assert "device_uuid" in r.json()["error"]


def test_missing_sensors_is_400():
    r = client.post(URL, headers=AUTH, json={"device_uuid": "d"})
    assert r.status_code == 400
    assert r.json()["error"] == "Missing sensors object"
    assert "example" in r.json()


def test_empty_sensors_is_400():
    r = client.post(URL, headers=AUTH, json={"device_uuid": "d", "sensors": {}})
    assert r.status_code == 400


def test_malformed_json_is_400():
    r = client.post(URL, headers=AUTH, content="not json")
    assert r.status_code == 400
    assert r.json()["error"] == "Malformed JSON"


def test_non_numeric_value_is_400():
    r = client.post(URL, headers=AUTH, json={"device_uuid": "d", "sensors": {"temperature": "hot"}})
    assert r.status_code == 400
    assert "temperature" in r.json()["error"]


def test_payload_too_large_is_413():
    big = {"device_uuid": "d", "sensors": {"t": 1}, "pad": "A" * (60 * 1024)}
    r = client.post(URL, headers=AUTH, json=big)
    assert r.status_code == 413
    assert r.json()["error"] == "Payload too large"
    assert r.json()["max_size"] == "50KB"
