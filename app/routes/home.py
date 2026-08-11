"""The public main page at `/` (plan §26).

Everything a newcomer needs on one page: what this is, how to publish a
measurement, what the limits are, and the two generators that produce a device
ID and a write key in the browser. The dashboard keeps its own prefix
(`/dashboard` by default), so this page and the dashboard never collide.

The page is rendered server-side with live numbers rather than fetched by
JavaScript, so it works with scripts blocked and needs no API call to be
useful.
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import __version__, metrics
from app.config import settings
from app.policies import registry
from app.validation import MAX_UNIT_LENGTH, SUPPORTED_PLOTS

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _ingest_url() -> str:
    """Absolute ingest URL when BASE_URL is configured, else the bare path."""
    return f"{settings.base_url}{settings.ingest_path}" if settings.base_url else settings.ingest_path


def _limits_table() -> list[dict]:
    """The anonymous limits, as rows for the documentation table (§22, §24).

    Read from the live policy rather than hard-coded, so the published numbers
    are the ones actually being enforced on this instance.
    """
    p = registry.anonymous
    return [
        {"limit": "Data retention", "value": f"{p.retention_hours} hours without a successful write"},
        {"limit": "Request body", "value": f"{p.max_payload_bytes // 1024} KB"},
        {"limit": "Sensor fields per request", "value": p.max_sensor_fields_per_request},
        {"limit": "Distinct sensors per device", "value": p.max_sensors_per_device},
        {"limit": "Device ID length", "value": f"{p.max_device_id_length} characters"},
        {"limit": "Sensor name length", "value": f"{p.max_sensor_name_length} characters"},
        {"limit": "Requests per minute (per IP)", "value": p.requests_per_minute_per_ip},
        {"limit": "Requests per day (per IP)", "value": f"{p.requests_per_day_per_ip:,}"},
        {"limit": "Writes per minute (per device)", "value": p.writes_per_minute_per_device},
        {"limit": "New devices per hour (per IP)", "value": p.new_devices_per_hour_per_ip},
        {"limit": "Active devices (per IP)", "value": p.active_devices_per_ip},
    ]


# Every error a well-formed client can provoke, so a device author can handle
# them without reading the source (§22).
ERROR_CODES = [
    ("400", "missing_device_id / invalid_device_id", "No device_id, or one with characters outside A–Z a–z 0–9 _ -"),
    ("400", "missing_sensors / empty_sensors", "No sensors object, or an empty one"),
    ("400", "invalid_value", "A value that is not a number, boolean, short string, or null — NaN and infinity included"),
    ("400", "unknown_field", "A field the endpoint does not accept, including timestamp"),
    ("401", "missing_write_key", "The device has a write key and the request did not present it"),
    ("401", "api_key_required", "The device ID is claimed by a keyless API-key device"),
    ("403", "invalid_write_key", "The write key was wrong"),
    ("413", "payload_too_large", "The request body is over the size limit"),
    ("429", "…_rate_limited / …_limit", "A rate or quota limit; retry after the seconds in Retry-After"),
    ("503", "storage_full / …_over_budget", "The platform is shedding load or out of storage; retry later"),
]


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    policy = registry.anonymous
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "app_title": settings.app_title,
            "brand": settings.brand,
            "version": __version__,
            # Assets live under the dashboard prefix; this page links into them.
            "base_path": settings.root_path,
            "home_path": "/",
            "dashboard_path": settings.root_path or "/",
            "feedback_url": settings.feedback_url,
            "ingest_url": _ingest_url(),
            "ingest_path": settings.ingest_path,
            "retention_hours": policy.retention_hours,
            "limits": _limits_table(),
            "error_codes": ERROR_CODES,
            "plot_types": sorted(SUPPORTED_PLOTS),
            "max_unit_length": MAX_UNIT_LENGTH,
            "stats": metrics.summary(),
        },
    )
