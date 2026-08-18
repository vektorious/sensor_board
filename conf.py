"""Gunicorn config for Uberspace (web backend supervised by supervisord).

Set up on Uberspace with, e.g.:
    uberspace web backend set /  --http --port 8020
and run: gunicorn -c conf.py
"""
import os

# Resolve the app dir without relying on $HOME being present in the supervisord
# environment (expanduser falls back to the passwd database).
app_path = os.path.expanduser("~/sensor_board")

# gunicorn loads this file directly, before the app package is imported, so
# app.config's .env loading has not happened yet. Read the same file here so a
# single .env configures both the server and the app.
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(app_path, ".env"))
except ImportError:
    pass

chdir = app_path
bind = ":8020"
workers = 2
worker_class = "uvicorn_worker.UvicornWorker"
wsgi_app = "app.main:app"

errorlog = app_path + "/errors.log"
accesslog = app_path + "/access.log"
loglevel = "info"

# --- Real client addresses behind the Uberspace frontend ---------------------
# The app is reached only through Uberspace's web frontend, so the TCP peer is
# always that proxy — `request.client.host` reports it for every request, and
# without this every client shares one identity. The per-IP rate limits in
# app/limits.py then behave as *global* caps instead of per-client ones, which
# at a workshop would mean thirty boards on thirty networks competing for one
# device-creation allowance.
#
# uvicorn-worker passes this straight to uvicorn's Config (_workers.py), and
# uvicorn's ProxyHeadersMiddleware then rewrites the client from
# X-Forwarded-For — but *only* for connections coming from a listed address.
#
# Never set this to "*". The header is trivially forged, so trusting it from
# anywhere would let a client choose its own rate-limit identity, or spend
# somebody else's allowance by claiming their address.
forwarded_allow_ips = os.getenv("FORWARDED_ALLOW_IPS", "100.65.24.1")

# --- Logging -----------------------------------------------------------------
# uvicorn-worker replaces the uvicorn loggers' handlers with gunicorn's, so the
# formatters defined here decide how *all* of it is written.
#
# The access log previously had no timestamps at all: gunicorn's access_log_format
# is ignored under UvicornWorker, and uvicorn's own formatter emits only
# `client - "request" status`. That makes the file useless for answering when
# something happened — which is exactly what it gets consulted for.
#
# The app's own loggers are attached explicitly rather than left to propagate,
# so the per-request `ingest ts=…` line reliably lands in errors.log.
_datefmt = "%Y-%m-%d %H:%M:%S %z"

logconfig_dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "stamped": {
            "format": "%(asctime)s [%(process)d] [%(levelname)s] %(message)s",
            "datefmt": _datefmt,
        },
        # uvicorn.access already renders "client - \"request\" status" into the
        # message, so this only needs to prepend the time.
        "access": {
            "format": "%(asctime)s %(message)s",
            "datefmt": _datefmt,
        },
    },
    "handlers": {
        "error_file": {
            "class": "logging.FileHandler",
            "filename": errorlog,
            "formatter": "stamped",
        },
        "access_file": {
            "class": "logging.FileHandler",
            "filename": accesslog,
            "formatter": "access",
        },
    },
    "loggers": {
        "gunicorn.error": {
            "level": "INFO",
            "handlers": ["error_file"],
            "propagate": False,
        },
        "gunicorn.access": {
            "level": "INFO",
            "handlers": ["access_file"],
            "propagate": False,
        },
        "sensor_board": {
            "level": "INFO",
            "handlers": ["error_file"],
            "propagate": False,
        },
    },
    "root": {"level": "INFO", "handlers": ["error_file"]},
}
