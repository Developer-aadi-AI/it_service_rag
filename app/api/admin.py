"""Internal endpoints (analytics). Disabled unless ADMIN_API_KEY is set; send it as X-Admin-Key."""
from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse

from app.analytics.summary import format_text, summarize
from app.api.deps import get_services
from app.services import Services

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin(x_admin_key: str | None = Header(None, description="value of ADMIN_API_KEY"),
                  svc: Services = Depends(get_services)) -> Services:
    expected = svc.settings.admin_api_key
    if not expected:
        raise HTTPException(status_code=404, detail="Not found.")  # feature off: don't advertise it
    if not x_admin_key or not hmac.compare_digest(x_admin_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing admin key.")
    return svc


@router.get("/analytics", summary="Analytics summary (JSON)")
def analytics(days: float = Query(7.0, gt=0, le=366), top: int = Query(10, ge=1, le=50),
              svc: Services = Depends(require_admin)) -> dict:
    """Sessions, questions (top questions, no-answer rate, latency), recommendations,
    leads, feedback, emails and errors for the last `days` days."""
    until = datetime.now(timezone.utc)
    return summarize(svc.db, until - timedelta(days=days), until, top)


@router.get("/analytics.txt", response_class=PlainTextResponse, summary="Analytics summary (text)")
def analytics_text(days: float = Query(7.0, gt=0, le=366), svc: Services = Depends(require_admin)) -> str:
    until = datetime.now(timezone.utc)
    return format_text(summarize(svc.db, until - timedelta(days=days), until))
