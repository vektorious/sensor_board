"""Gunicorn config for Uberspace (web backend supervised by supervisord).

Set up on Uberspace with, e.g.:
    uberspace web backend set /  --http --port 8020
and run: gunicorn -c conf.py
"""
import os

app_path = os.environ["HOME"] + "/sensor_board"

chdir = app_path
bind = ":8020"
workers = 2
worker_class = "uvicorn.workers.UvicornWorker"
wsgi_app = "app.main:app"

errorlog = app_path + "/errors.log"
accesslog = app_path + "/access.log"
loglevel = "info"
