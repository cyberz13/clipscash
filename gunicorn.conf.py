"""Gunicorn config for VPS/Docker deployments.
   Use: gunicorn -c gunicorn.conf.py app:app
"""
import multiprocessing
import os

bind = f"0.0.0.0:{os.environ.get('CLIPSCASH_PORT', '5001')}"
workers = int(os.environ.get("WEB_CONCURRENCY", max(2, multiprocessing.cpu_count())))
worker_class = "sync"
timeout = 60
keepalive = 5
accesslog = "-"   # stdout
errorlog = "-"    # stderr
loglevel = os.environ.get("LOG_LEVEL", "info")
forwarded_allow_ips = "*"  # we trust the reverse proxy
proxy_protocol = False
