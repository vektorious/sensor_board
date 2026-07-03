"""Device ingestion endpoint.

Generic JSON contract (no domain-specific wording):

    POST {INGEST_PATH}          (default /sensor/measurement)
    header: x-api-key: <API_KEY>
    body:
    {
      "project": "workshop-2026",         # optional, groups devices
      "name": "Basil #3",                 # optional, human label
      "device_uuid": "a1b2c3d4",          # required
      "sensors": {
        "temperature": {"value": 21.4, "unit": "C"},
        "moisture_pct": {"value": 62.0},
        "battery_voltage": 3.97            # bare number also accepted
      }
    }

Each sensor entry becomes one row. An optional per-entry "plot" field is stored
for the future plot-style feature and otherwise ignored.
"""
from datetime import datetime, UTC

from fastapi import APIRouter, HTTPException, Request

from app.config import settings
from app.database import get_session
from app.models import Reading

router = APIRouter()


@router.post(settings.ingest_path)
async def ingest(request: Request):
    if request.headers.get("x-api-key") not in settings.api_keys:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    device_uuid = data.get("device_uuid")
    if not device_uuid:
        raise HTTPException(status_code=400, detail="device_uuid is required")

    project = data.get("project")
    name = data.get("name")
    sensors = data.get("sensors", {}) or {}
    timestamp = datetime.now(UTC)

    stored = 0
    with get_session() as session:
        for sensor_type, entry in sensors.items():
            # Accept both {"value": x, "unit": u, "plot": p} and a bare number.
            if isinstance(entry, dict):
                value = entry.get("value")
                unit = entry.get("unit")
                plot = entry.get("plot")
            else:
                value, unit, plot = entry, None, None

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
                )
            )
            stored += 1
        session.commit()

    return {"status": "ok", "stored": stored}
