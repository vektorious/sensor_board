"""Ingestion payload validation (plan §§12, 13, 15, 16).

Everything an untrusted client can put on the wire is checked here, before a
single row is written. The endpoint stays readable as a sequence of decisions,
and the rules are testable without going through HTTP.

Two design choices worth stating, because both close a hole rather than merely
tidying input:

* **Device IDs are not normalised, only restricted.** Case folding would make
  `Greenhouse` and `greenhouse` the same device, so whoever claimed one would
  silently own the other — an ownership surprise, which §15 explicitly warns
  against. Instead the character set is narrow enough (`[A-Za-z0-9_-]`) that
  there is nothing to normalise: no whitespace, no path separators, no control
  characters, no confusable Unicode.

* **Clients may not send their own timestamps.** §13 leaves this open; refusing
  them is the answer that needs no further defences. A client-supplied time can
  be used to backdate data past the retention window or to poison a chart's
  axis, and every guard against that is another rule to get right. The server's
  receipt time is authoritative, and expiry is measured against it.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from app.sensors import SENSORS

# Fields a payload may carry. Anything else is rejected rather than ignored, so
# a typo like "sensor" or "deviceId" fails loudly instead of storing nothing.
ALLOWED_TOP_LEVEL = {"device_id", "write_key", "project", "name", "sensors"}

# Device IDs and project names both appear in URLs, so they share the strictest
# character set. §27: a "/" in either silently breaks its dashboard link.
_URL_SAFE = re.compile(r"^[A-Za-z0-9_-]+$")
# Sensor names are not used in paths (only as query values), so "." and ":" are
# tolerated for names like "bme280.temp".
_SENSOR_NAME = re.compile(r"^[A-Za-z0-9_.:-]+$")

# Chart types the front-end knows how to render. An unknown value would leave a
# panel blank, so it is rejected instead of stored.
SUPPORTED_PLOTS = {"line", "gauge"}

MAX_WRITE_KEY_LENGTH = 512
MAX_NAME_LENGTH = 64
MAX_UNIT_LENGTH = 16


class ValidationError(Exception):
    """A rejected payload, carrying the machine-readable code for the response."""

    def __init__(self, code: str, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


@dataclass
class SensorValue:
    """One validated measurement, ready to become a row."""

    sensor_type: str
    value: float | None = None
    value_text: str | None = None
    value_type: str = "number"
    unit: str | None = None
    plot: str | None = None


@dataclass
class Payload:
    """A fully validated ingestion request."""

    device_id: str
    write_key: str | None
    project: str | None
    name: str | None
    sensors: list[SensorValue] = field(default_factory=list)

    @property
    def sensor_names(self) -> set[str]:
        return {s.sensor_type for s in self.sensors}


def validate(data: Any, policy) -> Payload:
    """Validate a decoded JSON body against a policy. Raises ValidationError."""
    if not isinstance(data, dict):
        raise ValidationError(
            "not_object", "Body must be a JSON object.", "Send {\"device_id\": …}."
        )

    unknown = set(data) - ALLOWED_TOP_LEVEL
    if unknown:
        listed = ", ".join(sorted(unknown))
        hint = f"Allowed fields: {', '.join(sorted(ALLOWED_TOP_LEVEL))}."
        if "timestamp" in unknown or "time" in unknown:
            # Worth its own sentence: this is a deliberate policy, not a typo.
            hint = (
                "Timestamps are assigned by the server on receipt and cannot be "
                "supplied by the client. " + hint
            )
        raise ValidationError("unknown_field", f"Unknown field(s): {listed}.", hint)

    device_id = _device_id(data.get("device_id"), policy)
    write_key = _write_key(data.get("write_key"))
    project = _url_safe_optional(data.get("project"), "project", policy.max_device_id_length)
    name = _display_text(data.get("name"), "name", MAX_NAME_LENGTH)
    sensors = _sensors(data.get("sensors"), policy)

    return Payload(
        device_id=device_id,
        write_key=write_key,
        project=project,
        name=name,
        sensors=sensors,
    )


def _device_id(raw: Any, policy) -> str:
    if raw is None:
        raise ValidationError(
            "missing_device_id",
            "Missing device_id.",
            "Include a device_id identifying the device. It is a public "
            "identifier, not a secret.",
        )
    if not isinstance(raw, str) or not raw:
        raise ValidationError("invalid_device_id", "device_id must be a non-empty string.")
    if len(raw) > policy.max_device_id_length:
        raise ValidationError(
            "invalid_device_id",
            f"device_id may be at most {policy.max_device_id_length} characters.",
        )
    if not _URL_SAFE.match(raw):
        raise ValidationError(
            "invalid_device_id",
            "device_id may contain only letters, digits, hyphens, and underscores.",
            "It appears in the dashboard URL, so characters like '/' and spaces "
            "are not allowed. IDs are case-sensitive.",
        )
    return raw


def _write_key(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise ValidationError(
            "invalid_write_key", "write_key must be a non-empty string when present."
        )
    if len(raw) > MAX_WRITE_KEY_LENGTH:
        raise ValidationError(
            "invalid_write_key",
            f"write_key may be at most {MAX_WRITE_KEY_LENGTH} characters.",
        )
    # No strength requirement by design (§14): the key protects one throwaway
    # device that expires in 48 hours, and the site offers a generator.
    return raw


def _url_safe_optional(raw: Any, field_name: str, max_length: int) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise ValidationError(
            f"invalid_{field_name}", f"{field_name} must be a non-empty string when present."
        )
    if len(raw) > max_length:
        raise ValidationError(
            f"invalid_{field_name}",
            f"{field_name} may be at most {max_length} characters.",
        )
    if not _URL_SAFE.match(raw):
        raise ValidationError(
            f"invalid_{field_name}",
            f"{field_name} may contain only letters, digits, hyphens, and underscores.",
            "It appears in the dashboard URL.",
        )
    return raw


def _display_text(raw: Any, field_name: str, max_length: int) -> str | None:
    """A human-readable label. Never appears in a URL, so only the characters
    that would corrupt a log line or a terminal are excluded."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValidationError(f"invalid_{field_name}", f"{field_name} must be a string.")
    if len(raw) > max_length:
        raise ValidationError(
            f"invalid_{field_name}", f"{field_name} may be at most {max_length} characters."
        )
    if any(not ch.isprintable() for ch in raw):
        raise ValidationError(
            f"invalid_{field_name}", f"{field_name} may not contain control characters."
        )
    return raw or None


def _sensors(raw: Any, policy) -> list[SensorValue]:
    if raw is None:
        raise ValidationError(
            "missing_sensors",
            "Missing sensors object.",
            'Send "sensors": {"temperature": 21.4}.',
        )
    if not isinstance(raw, dict) or not raw:
        raise ValidationError(
            "empty_sensors",
            "sensors must be a non-empty object of {name: value}.",
        )
    if len(raw) > policy.max_sensor_fields_per_request:
        raise ValidationError(
            "too_many_sensor_fields",
            f"At most {policy.max_sensor_fields_per_request} sensor fields per request "
            f"({len(raw)} sent).",
        )

    return [_sensor(name, entry, policy) for name, entry in raw.items()]


def _sensor(name: Any, entry: Any, policy) -> SensorValue:
    if not isinstance(name, str) or not name:
        raise ValidationError("invalid_sensor_name", "Sensor names must be non-empty strings.")
    if len(name) > policy.max_sensor_name_length:
        raise ValidationError(
            "invalid_sensor_name",
            f"Sensor name '{name[:24]}…' exceeds {policy.max_sensor_name_length} characters.",
        )
    if not _SENSOR_NAME.match(name):
        raise ValidationError(
            "invalid_sensor_name",
            f"Sensor name '{name}' may contain only letters, digits, and . : _ - characters.",
        )

    unit = None
    plot = None
    if isinstance(entry, dict):
        # Depth 2 = the payload object, then the sensors object, then this
        # entry. Anything nested inside the entry is depth 3 and rejected.
        if policy.max_nesting_depth < 2:
            raise ValidationError(
                "too_deep", "Nested sensor objects are not accepted."
            )
        extra = set(entry) - {"value", "unit", "plot"}
        if extra:
            raise ValidationError(
                "unknown_field",
                f"Sensor '{name}' has unknown field(s): {', '.join(sorted(extra))}.",
                "A sensor entry accepts value, unit, and plot.",
            )
        value = entry.get("value")
        unit = _display_text(entry.get("unit"), "unit", MAX_UNIT_LENGTH)
        plot = _plot(entry.get("plot"), name)
    else:
        value = entry

    return _typed_value(name, value, unit, plot, policy)


def _plot(raw: Any, sensor_name: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or raw not in SUPPORTED_PLOTS:
        raise ValidationError(
            "invalid_plot",
            f"Unsupported plot type for sensor '{sensor_name}'.",
            f"Supported plot types: {', '.join(sorted(SUPPORTED_PLOTS))}.",
        )
    return raw


def _typed_value(name: str, value: Any, unit: str | None, plot: str | None, policy) -> SensorValue:
    """Map a JSON scalar onto the stored representation (§12).

    Supported: number, boolean, short string, null. Containers are refused —
    a list or dict here would mean either silently dropping data or inventing a
    flattening rule, and neither belongs in an ingestion endpoint.
    """
    common = {"sensor_type": name, "unit": unit, "plot": plot}

    if value is None:
        return SensorValue(**common, value=None, value_type="null")

    # bool before int: in Python, bool *is* an int, and True would otherwise be
    # stored as an indistinguishable 1.0.
    if isinstance(value, bool):
        return SensorValue(**common, value=1.0 if value else 0.0, value_type="bool")

    if isinstance(value, (int, float)):
        if math.isnan(value) or math.isinf(value):
            raise ValidationError(
                "invalid_value",
                f"Sensor '{name}' has a non-finite value.",
                "NaN and infinity cannot be stored or charted.",
            )
        return SensorValue(**common, value=float(value), value_type="number")

    if isinstance(value, str):
        if len(value) > policy.max_string_value_length:
            raise ValidationError(
                "invalid_value",
                f"Sensor '{name}' string value exceeds "
                f"{policy.max_string_value_length} characters.",
            )
        if any(not ch.isprintable() for ch in value):
            raise ValidationError(
                "invalid_value",
                f"Sensor '{name}' string value may not contain control characters.",
            )
        return SensorValue(**common, value_text=value, value_type="text")

    raise ValidationError(
        "invalid_value",
        f"Sensor '{name}' has an unsupported value type.",
        "Values may be a number, boolean, short string, or null — not a list "
        "or an object.",
    )


def known_sensor_types() -> list[str]:
    """Sensor names the presentation registry styles. Documentation only —
    unlisted sensors are accepted and rendered with defaults."""
    return sorted(SENSORS)
