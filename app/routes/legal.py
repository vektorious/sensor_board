"""Impressum / privacy routes.

Mounted at the site root rather than under ROOT_PATH: these have to be reachable
from every page, and a legal notice hiding under a dashboard prefix defeats the
point of having one.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import __version__, legal
from app.config import settings

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["legal_pages"] = legal.available


def _render(request: Request, slug: str) -> HTMLResponse:
    body = legal.render(slug)
    if body is None:
        # Known slug, but this instance has not published that page.
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "legal.html",
        {
            "app_title": settings.app_title,
            "brand": settings.brand,
            "version": __version__,
            "base_path": settings.root_path,
            "home_path": "/" if settings.root_path else None,
            "heading": legal.PAGES[slug],
            "body": body,
        },
    )


def _endpoint(slug: str):
    def handler(request: Request):
        return _render(request, slug)
    return handler


# One exact route per known page, NOT "/{slug}". A path parameter at the site
# root is a catch-all: it matches "/dashboard" too, which stops FastAPI issuing
# its redirect to "/dashboard/" and turns a working URL into a 404. The routes
# are generated from PAGES so that dict stays the single source of truth.
for _slug in legal.PAGES:
    router.add_api_route(
        f"/{_slug}",
        _endpoint(_slug),
        methods=["GET"],
        response_class=HTMLResponse,
        name=f"legal_{_slug}",
    )
