"""JSON API consumed by the dashboard front-end (and usable directly)."""
from fastapi import APIRouter, HTTPException, Query

from app import queries
from app.config import settings

router = APIRouter(prefix="/api")


@router.get("/projects")
def api_projects():
    return {"projects": queries.list_projects()}


@router.get("/project/{slug}/devices")
def api_project_devices(slug: str):
    return {"project": slug, "devices": queries.list_devices(project=slug)}


@router.get("/project/{slug}/sensors")
def api_project_sensors(slug: str):
    """Sensor types present in the project — one aggregated chart each."""
    return {"project": slug, "sensors": queries.project_sensor_types(slug)}


@router.get("/project/{slug}/series")
def api_project_series(
    slug: str,
    sensor: str = Query(..., description="sensor_type key"),
    hours: int = Query(None, description="lookback window; use 0 for all"),
):
    if hours is None:
        hours = settings.default_range_hours
    return {
        "project": slug,
        "sensor": sensor,
        "hours": hours,
        "series": queries.project_series(slug, sensor, hours),
    }


@router.get("/device/{device_uuid}/sensors")
def api_device_sensors(device_uuid: str):
    """Panel descriptors (meta + latest value) — drives auto-population."""
    info = queries.device_info(device_uuid)
    if info is None:
        raise HTTPException(status_code=404, detail="Unknown device")
    return {"device": info, "sensors": queries.device_sensors(device_uuid)}


@router.get("/device/{device_uuid}/series")
def api_device_series(
    device_uuid: str,
    sensor: str = Query(..., description="sensor_type key"),
    hours: int = Query(None, description="lookback window; <=0 or omitted = default; use 0 for all"),
):
    if hours is None:
        hours = settings.default_range_hours
    return {
        "device_uuid": device_uuid,
        "sensor": sensor,
        "hours": hours,
        "points": queries.series(device_uuid, sensor, hours),
    }
