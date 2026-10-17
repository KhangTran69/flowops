"""Vercel/FastAPI default entrypoint.

Vercel scans root-level ``main.py`` for a FastAPI instance named ``app``. The
real application lives in ``src/api.py``; re-export it here so deployments do
not accidentally select ``docker/webhook-sink/app.py:Handler``.
"""

from src.api import app

__all__ = ["app"]
