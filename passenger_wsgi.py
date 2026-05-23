"""WSGI entry point for Hostinger Cloud Hosting (Passenger).

Hostinger's "Setup Python App" uses Phusion Passenger which auto-detects
this file. Just point the "Application startup file" to passenger_wsgi.py
and the "Application Entry point" to `application` in hPanel.
"""
import os
import sys

# Ensure stdout/stderr handle UTF-8 (Hostinger sometimes uses ASCII locale)
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Make sure CLIPSCASH_ENV defaults to prod when running under Passenger
os.environ.setdefault("CLIPSCASH_ENV", "prod")
os.environ.setdefault("CLIPSCASH_BEHIND_PROXY", "1")

# Add this dir to sys.path so `import app` works regardless of cwd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app as application  # noqa: E402  Passenger expects `application`
