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


@router.get("/{slug}", response_class=HTMLResponse)
def legal_page(request: Request, slug: str):
    if slug not in legal.PAGES:
        raise HTTPException(status_code=404)
    body = legal.render(slug)
    if body is None:
        # The slug is known but this instance has not published that page.
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
