"""Dashboard route — landing page."""

from __future__ import annotations

from fastapi import APIRouter, Request

from sleeve_notes import db as dbmod
from sleeve_notes_web._deps import templates
from sleeve_notes_web.services import stats


router = APIRouter()


@router.get("/")
def index(request: Request):
    with dbmod.session() as conn:
        coverage = stats.collection_coverage(conn)
        new_count, last_print_ts = stats.new_since_last_print(conn)
        totals = stats.collection_totals(conn)
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "active": "dashboard",
            "coverage": coverage,
            "new_count": new_count,
            "last_print_ts": last_print_ts,
            "totals": totals,
        },
    )
