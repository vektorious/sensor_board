from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db
from app.routes.api import router as api_router
from app.routes.ingest import router as ingest_router
from app.routes.web import router as web_router

app = FastAPI(title=settings.app_title)

init_db()

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(ingest_router)
app.include_router(api_router)
app.include_router(web_router)
