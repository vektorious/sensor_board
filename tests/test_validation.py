"""Payload validation (plan §§12, 13, 15, 16).

Tested against the validator directly rather than through HTTP, so each rule
can be stated in one line and the failure message names the rule that broke.
The endpoint's translation of these into status codes is covered in
`test_ingest.py`.
"""
import pytest

from app.policies import ANONYMOUS
from app.validation import ValidationError, validate


def valid(**overrides) -> dict:
    return {"device_id": "greenhouse-01", "sensors": {"temperature": 21.5}, **overrides}


def check(payload):
    return validate(payload, ANONYMOUS)


def rejected(payload) -> str:
    with pytest.raises(ValidationError) as exc:
        check(payload)
    return exc.value.code


# --- shape (§16) ------------------------------------------------------------


def test_accepts_a_minimal_payload():
    payload = check(valid())
    assert payload.device_id == "greenhouse-01"
    assert payload.sensors[0].sensor_type == "temperature"
    assert payload.sensors[0].value == 21.5


@pytest.mark.parametrize("body", [[], "text", 42, None])
def test_body_must_be_an_object(body):
    assert rejected(body) == "not_object"


def test_unknown_top_level_fields_are_rejected_not_ignored():
    # A typo like "sensor" would otherwise store nothing and report success.
    assert rejected(valid(sensor={"t": 1})) == "unknown_field"


def test_client_timestamps_are_refused_with_an_explanation():
    with pytest.raises(ValidationError) as exc:
        check(valid(timestamp="2026-01-01T00:00:00Z"))
    assert exc.value.code == "unknown_field"
    assert "assigned by the server" in exc.value.hint


# --- device IDs (§15) -------------------------------------------------------


@pytest.mark.parametrize(
    "device_id",
    [
        "with/slash",       # would break the dashboard URL
        "../etc/passwd",    # path traversal
        "with space",
        "with\nnewline",
        "with\x00null",
        "émoji-ok?-no",
        "",
    ],
)
def test_unsafe_device_ids_are_rejected(device_id):
    assert rejected(valid(device_id=device_id)) == "invalid_device_id"


def test_device_id_length_is_capped():
    assert rejected(valid(device_id="a" * 65)) == "invalid_device_id"
    assert check(valid(device_id="a" * 64)).device_id == "a" * 64


def test_device_ids_are_case_sensitive_and_not_normalised():
    # Folding case would let whoever claimed "Greenhouse" silently own
    # "greenhouse" too, which §15 warns against.
    assert check(valid(device_id="Greenhouse")).device_id == "Greenhouse"


def test_missing_device_id_has_its_own_code():
    assert rejected({"sensors": {"t": 1}}) == "missing_device_id"


def test_project_shares_the_url_safe_rules():
    assert rejected(valid(project="has/slash")) == "invalid_project"
    assert check(valid(project="workshop-2026")).project == "workshop-2026"


def test_display_name_allows_free_text_but_not_control_characters():
    assert check(valid(name="Basil #3 (küche)")).name == "Basil #3 (küche)"
    assert rejected(valid(name="line\nbreak")) == "invalid_name"
    assert rejected(valid(name="x" * 65)) == "invalid_name"


# --- write keys (§14) -------------------------------------------------------


def test_any_non_empty_write_key_is_accepted():
    # Strength is deliberately not enforced: a weak key exposes only the user's
    # own throwaway device, and the site offers a generator.
    assert check(valid(write_key="a")).write_key == "a"


def test_write_key_must_be_a_non_empty_string():
    assert rejected(valid(write_key="")) == "invalid_write_key"
    assert rejected(valid(write_key=12345)) == "invalid_write_key"
    assert rejected(valid(write_key="k" * 513)) == "invalid_write_key"


# --- sensors (§12) ----------------------------------------------------------


def test_sensors_must_be_a_non_empty_object():
    assert rejected({"device_id": "d"}) == "missing_sensors"
    assert rejected(valid(sensors={})) == "empty_sensors"
    assert rejected(valid(sensors=[1, 2])) == "empty_sensors"


def test_sensor_field_count_is_capped():
    too_many = {f"s{i}": i for i in range(17)}
    assert rejected(valid(sensors=too_many)) == "too_many_sensor_fields"


def test_sensor_names_are_length_and_charset_limited():
    assert rejected(valid(sensors={"a" * 65: 1})) == "invalid_sensor_name"
    assert rejected(valid(sensors={"has space": 1})) == "invalid_sensor_name"
    assert rejected(valid(sensors={"": 1})) == "invalid_sensor_name"
    # Dotted and colon-separated names are common for multi-sensor boards.
    assert check(valid(sensors={"bme280.temp": 1})).sensors[0].sensor_type == "bme280.temp"


@pytest.mark.parametrize(
    "value, expected_type, expected_value, expected_text",
    [
        (21.5, "number", 21.5, None),
        (3, "number", 3.0, None),
        (True, "bool", 1.0, None),
        (False, "bool", 0.0, None),
        ("idle", "text", None, "idle"),
        (None, "null", None, None),
    ],
)
def test_supported_scalar_types(value, expected_type, expected_value, expected_text):
    sensor = check(valid(sensors={"s": value})).sensors[0]
    assert sensor.value_type == expected_type
    assert sensor.value == expected_value
    assert sensor.value_text == expected_text


@pytest.mark.parametrize("value", [[1, 2], {"nested": {"deep": 1}}, {"a": 1}])
def test_containers_are_not_measurements(value):
    # A list or a nested object would mean either dropping data silently or
    # inventing a flattening rule.
    assert rejected(valid(sensors={"s": value})) in ("invalid_value", "unknown_field")


def test_non_finite_numbers_are_rejected():
    assert rejected(valid(sensors={"s": float("nan")})) == "invalid_value"
    assert rejected(valid(sensors={"s": float("inf")})) == "invalid_value"


def test_string_values_are_length_limited_and_printable():
    assert rejected(valid(sensors={"s": "x" * 65})) == "invalid_value"
    assert rejected(valid(sensors={"s": "bad\x07bell"})) == "invalid_value"


def test_sensor_object_form_carries_unit_and_plot():
    sensor = check(
        valid(sensors={"t": {"value": 20.0, "unit": "C", "plot": "line"}})
    ).sensors[0]
    assert (sensor.value, sensor.unit, sensor.plot) == (20.0, "C", "line")


def test_unknown_plot_types_are_rejected():
    # An unrecognised chart type would leave the panel blank on the dashboard.
    assert rejected(valid(sensors={"t": {"value": 1, "plot": "pie"}})) == "invalid_plot"


def test_unknown_fields_inside_a_sensor_entry_are_rejected():
    assert rejected(valid(sensors={"t": {"value": 1, "colour": "red"}})) == "unknown_field"


def test_unit_is_length_limited():
    assert rejected(valid(sensors={"t": {"value": 1, "unit": "u" * 17}})) == "invalid_unit"
