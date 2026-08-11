"""Policy defaults and overrides (plan §7, §24).

The first test is the important one: it pins every default to the number in
§24, so a change to a limit has to be a deliberate edit to both the plan and
this file rather than an accident. The rest cover the two rules a policy may
never break — it cannot become unlimited, and it cannot drop below the
anonymous baseline.
"""
import pytest

from app.policies import ANONYMOUS, TRUSTED, PolicyRegistry


def test_anonymous_defaults_match_the_documented_limits():
    assert ANONYMOUS.retention_hours == 48
    assert ANONYMOUS.persistent_devices is False
    assert ANONYMOUS.max_payload_bytes == 16 * 1024
    assert ANONYMOUS.max_sensor_fields_per_request == 16
    assert ANONYMOUS.max_sensors_per_device == 16
    assert ANONYMOUS.max_device_id_length == 64
    assert ANONYMOUS.max_sensor_name_length == 64
    assert ANONYMOUS.max_nesting_depth == 2
    assert ANONYMOUS.requests_per_minute_per_ip == 30
    assert ANONYMOUS.requests_per_day_per_ip == 1_000
    assert ANONYMOUS.writes_per_minute_per_device == 12
    assert ANONYMOUS.new_devices_per_hour_per_ip == 5
    assert ANONYMOUS.active_devices_per_ip == 10
    assert ANONYMOUS.writes_per_second == 3
    assert ANONYMOUS.write_burst == 300


def test_trusted_grants_persistence_and_more_room():
    assert TRUSTED.persistent_devices is True
    assert TRUSTED.max_payload_bytes > ANONYMOUS.max_payload_bytes
    assert TRUSTED.writes_per_minute_per_device > ANONYMOUS.writes_per_minute_per_device


def test_no_policy_is_unlimited():
    # §7: overriding a limit means raising it, never removing it.
    registry = PolicyRegistry()
    for name in registry.names():
        policy = registry.by_name(name)
        for field in (
            "max_payload_bytes",
            "max_sensor_fields_per_request",
            "max_sensors_per_device",
            "requests_per_minute_per_ip",
            "requests_per_day_per_ip",
            "writes_per_minute_per_device",
            "new_devices_per_hour_per_ip",
            "active_devices_per_ip",
            "writes_per_second",
            "write_burst",
        ):
            assert getattr(policy, field) > 0, f"{name}.{field}"


@pytest.fixture
def env(monkeypatch):
    """Build a registry from a specific environment."""

    def build(**vars):
        for key, value in vars.items():
            monkeypatch.setenv(key, str(value))
        return PolicyRegistry()

    return build


def test_env_override_raises_a_limit(env):
    registry = env(POLICY_TRUSTED_WRITES_PER_MINUTE_PER_DEVICE=600)
    assert registry.by_name("trusted").writes_per_minute_per_device == 600


def test_non_positive_override_is_refused(env, caplog):
    with caplog.at_level("WARNING", logger="sensor_board.policies"):
        registry = env(POLICY_TRUSTED_MAX_PAYLOAD_BYTES=0)
    assert registry.by_name("trusted").max_payload_bytes == TRUSTED.max_payload_bytes
    assert "must stay positive" in caplog.text


def test_non_integer_override_is_ignored(env, caplog):
    with caplog.at_level("WARNING", logger="sensor_board.policies"):
        registry = env(POLICY_TRUSTED_MAX_PAYLOAD_BYTES="lots")
    assert registry.by_name("trusted").max_payload_bytes == TRUSTED.max_payload_bytes
    assert "not an integer" in caplog.text


def test_a_policy_cannot_drop_below_the_anonymous_baseline(env, caplog):
    with caplog.at_level("WARNING", logger="sensor_board.policies"):
        registry = env(POLICY_TRUSTED_MAX_SENSOR_FIELDS_PER_REQUEST=2)
    # 2 is below the anonymous 16, so the baseline wins: an API key must never
    # buy a client *less* room than sending nothing at all.
    assert registry.by_name("trusted").max_sensor_fields_per_request == 16
    assert "below the anonymous baseline" in caplog.text


def test_keys_map_to_their_configured_policy(env):
    registry = env(
        API_KEY_POLICIES="workshop-key:workshop,other-key:trusted",
        POLICY_WORKSHOP_MAX_SENSORS_PER_DEVICE=99,
    )
    assert registry.for_api_key("workshop-key").name == "workshop"
    assert registry.for_api_key("workshop-key").max_sensors_per_device == 99
    assert registry.for_api_key("other-key").name == "trusted"


def test_an_unmapped_valid_key_gets_the_trusted_policy(env):
    registry = env(API_KEY_POLICIES="")
    assert registry.for_api_key("some-key").name == "trusted"


def test_a_custom_policy_starts_from_trusted(env):
    registry = env(API_KEY_POLICIES="k:custom")
    custom = registry.by_name("custom")
    # Inherits trusted's persistence rather than starting from anonymous, so a
    # named policy does not silently make its devices expire.
    assert custom.persistent_devices is True
    assert custom.max_payload_bytes == TRUSTED.max_payload_bytes


def test_malformed_key_mapping_is_ignored(env, caplog):
    with caplog.at_level("WARNING", logger="sensor_board.policies"):
        registry = env(API_KEY_POLICIES="no-colon-here,,good-key:trusted")
    assert registry.for_api_key("good-key").name == "trusted"
    assert "malformed" in caplog.text
