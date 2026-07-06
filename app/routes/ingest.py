"""Device ingestion endpoint.

Generic JSON contract (no domain-specific wording):

    POST {INGEST_PATH}          (default /sensor/measurement)
    header: X-API-Key: <API_KEY>
    body:
    {
      "project": "workshop-2026",         # optional, groups devices
      "name": "Basil #3",                 # optional, human label
      "device_uuid": "a1b2c3d4",          # required, user-defined
      "sensors": {
        "temperature": {"value": 21.4, "unit": "C"},
        "moisture_pct": {"value": 62.0},
        "battery_voltage": 3.97            # bare number also accepted
      }
    }

Each sensor entry becomes one row, tagged with the SHA-256 hash of the API key
that submitted it (never the plaintext key). An optional per-entry "plot" field
is stored for the future plot-style feature and otherwise ignored.

Errors are descriptive JSON: 401 (bad/missing key), 413 (payload too large),
400 (malformed JSON, missing/empty sensors, missing device_uuid, non-numeric
value). Every request is logged with a short key-hash prefix — no plaintext key.
"""
import json
import logging
from datetime import datetime, UTC

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.database import get_session
from app.models import Reading
from app.security import hash_api_key

router = APIRouter()
logger = logging.getLogger("sensor_board.ingest")

# Shown in error responses to nudge testers toward a valid payload. Uses the
# real field names (device_uuid, sensors) — bare numeric sensor values are fine.
_EXAMPLE = {
    "device_uuid": "workbench-sensor-01",
    "sensors": {"temperature": 22.4, "humidity": 51},
}


@router.post(settings.ingest_path)
async def ingest(request: Request):
    # Mutable log record, filled in as far as parsing gets before an exit.
    log = {"device": None, "sensors": None, "bytes": 0, "key": "none"}

    def finish(status_code: int, payload: dict, error: str | None = None):
        logger.info(
            "ingest ts=%s device=%s sensors=%s bytes=%s key=%s status=%s%s",
            datetime.now(UTC).isoformat(),
            log["device"], log["sensors"], log["bytes"], log["key"], status_code,
            f" error={error!r}" if error else "",
        )
        return JSONResponse(status_code=status_code, content=payload)

    # --- authentication ---
    api_key = request.headers.get("x-api-key")
    if api_key not in settings.api_keys:
        return finish(
            401,
            {
                "error": "Invalid or missing API key",
                "hint": "Provide your API key using the X-API-Key header.",
            },
            error="unauthorized",
        )
    key_hash = hash_api_key(api_key)
    log["key"] = key_hash[:12]

    # --- payload size limit (read raw body once) ---
    body = await request.body()
    log["bytes"] = len(body)
    if len(body) > settings.max_payload_bytes:
        return finish(
            413,
            {"error": "Payload too large", "max_size": f"{settings.max_payload_bytes // 1024}KB"},
            error="payload_too_large",
        )

    # --- parse JSON ---
    try:
        data = json.loads(body)
    except Exception:
        return finish(
            400,
            {"error": "Malformed JSON", "hint": "Request body must be valid JSON.", "example": _EXAMPLE},
            error="malformed_json",
        )
    if not isinstance(data, dict):
        return finish(
            400,
            {"error": "Body must be a JSON object", "example": _EXAMPLE},
            error="not_object",
        )

    # --- validation ---
    device_uuid = data.get("device_uuid")
    log["device"] = device_uuid
    if not device_uuid:
        return finish(
            400,
            {
                "error": "Missing device_uuid",
                "hint": "Include a user-defined device_uuid identifying the device.",
                "example": _EXAMPLE,
            },
            error="missing_device",
        )

    sensors = data.get("sensors")
    if sensors is None:
        return finish(
            400,
            {"error": "Missing sensors object", "example": _EXAMPLE},
            error="missing_sensors",
        )
    if not isinstance(sensors, dict) or not sensors:
        return finish(
            400,
            {
                "error": "Empty or invalid sensors object",
                "hint": "sensors must be a non-empty object of {name: value}.",
                "example": _EXAMPLE,
            },
            error="empty_sensors",
        )
    log["sensors"] = ",".join(sensors.keys())

    # Extract + validate each sensor value (bare number or {"value": ...}).
    rows = []
    for sensor_type, entry in sensors.items():
        if isinstance(entry, dict):
            value = entry.get("value")
            unit = entry.get("unit")
            plot = entry.get("plot")
        else:
            value, unit, plot = entry, None, None
        # bool is a subclass of int — exclude it; only real numbers allowed.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return finish(
                400,
                {
                    "error": f"Invalid value for sensor '{sensor_type}'",
                    "hint": "Sensor values must be numbers.",
                    "example": _EXAMPLE,
                },
                error="invalid_value",
            )
        rows.append((sensor_type, float(value), unit, plot))

    # --- store ---
    project = data.get("project")
    name = data.get("name")
    timestamp = datetime.now(UTC)
    with get_session() as session:
        for sensor_type, value, unit, plot in rows:
            session.add(
                Reading(
                    project=project,
                    device_uuid=device_uuid,
                    device_name=name,
                    timestamp=timestamp,
                    sensor_type=sensor_type,
                    value=value,
                    unit=unit,
                    plot=plot,
                    api_key_hash=key_hash,
                )
            )
        session.commit()

    return finish(200, {"status": "ok", "stored": len(rows)})
