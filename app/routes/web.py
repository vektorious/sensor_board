"""HTML pages: overview, per-project, per-device."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import queries
from app.config import settings

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _ctx(**extra) -> dict:
    base = {
        "app_title": settings.app_title,
        "brand": settings.brand,
        "echarts_src": settings.echarts_src,
        "default_range_hours": settings.default_range_hours,
        # URL prefix the UI is mounted under; templates/JS prepend it to links
        # and fetches. "" at root, "/dashboard" when mounted there.
        "base_path": settings.root_path,
    }
    base.update(extra)
    return base


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        _ctx(
            projects=queries.list_projects(),
            devices=queries.list_devices(),
            stats=queries.overview_stats(),
        ),
    )


@router.get("/project/{slug}", response_class=HTMLResponse)
def project_dashboard(request: Request, slug: str):
    return templates.TemplateResponse(
        request,
        "project.html",
        _ctx(slug=slug, devices=queries.list_devices(project=slug)),
    )


@router.get("/device/{device_uuid}", response_class=HTMLResponse)
def device_dashboard(request: Request, device_uuid: str):
    return templates.TemplateResponse(
        request,
        "device.html",
        _ctx(device_uuid=device_uuid, info=queries.device_info(device_uuid)),
    )
