"""Jinja2 rendering helpers for the server-rendered operator UI."""

import os
from pathlib import Path
from typing import Any, Optional

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def render(
    request: Request,
    template: str,
    context: Optional[dict[str, Any]] = None,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render one template with the request available to url_for and escapes."""
    values = {
        "request": request,
        "actor": getattr(request.state, "actor", None),
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "ui_auth_required": os.environ.get(
            "FLOWOPS_UI_AUTH_REQUIRED", "false"
        ).lower()
        == "true",
        **(context or {}),
    }
    return templates.TemplateResponse(
        request=request,
        name=template,
        context=values,
        status_code=status_code,
    )
