import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db
from app.routes.api import router as api_router
from app.routes.ingest import router as ingest_router
from app.routes.web import router as web_router


def _setup_logging() -> None:
    """Ensure the app's own loggers emit at INFO, independent of the server.

    Uvicorn/Gunicorn configure their own loggers but not the root logger, so
    without this the ingest request log would be silently dropped. Self-contained
    (own handler, no propagation) so it never double-logs server messages.
    """
    log = logging.getLogger("sensor_board")
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


_setup_logging()

app = FastAPI(title=settings.app_title)

init_db()

_root = settings.root_path  # "" or e.g. "/dashboard"

# UI (static, JSON API, pages) lives under the configurable prefix.
app.mount(f"{_root}/static", StaticFiles(directory="app/static"), name="static")
app.include_router(api_router, prefix=_root)
app.include_router(web_router, prefix=_root)

# Ingestion keeps its own absolute path (not under the UI prefix), so devices
# post to a stable, clean URL regardless of where the UI is mounted.
app.include_router(ingest_router)
