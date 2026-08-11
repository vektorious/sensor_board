import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import settings
from app.database import init_db
from app.limits import init_limits
from app.middleware import SecurityHeadersMiddleware
from app.routes.api import router as api_router
from app.routes.ingest import router as ingest_router
from app.routes.web import router as web_router
from app.retention import start_retention_sweeper


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

app = FastAPI(title=settings.app_title, version=__version__)

app.add_middleware(SecurityHeadersMiddleware)

init_db()
init_limits()

# Background sweeper: auto-delete devices/projects idle past the retention window.
start_retention_sweeper()

_root = settings.root_path  # "/dashboard" by default, "" to serve at the root

# UI (static, JSON API, pages) lives under the configurable prefix.
app.mount(f"{_root}/static", StaticFiles(directory="app/static"), name="static")
app.include_router(api_router, prefix=_root)
app.include_router(web_router, prefix=_root)

# Ingestion keeps its own absolute path (not under the UI prefix), so devices
# post to a stable, clean URL regardless of where the UI is mounted.
app.include_router(ingest_router)
