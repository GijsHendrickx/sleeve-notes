"""Dashboard route — landing page for signed-out, dashboard for signed-in."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from sleeve_notes import db as dbmod
from sleeve_notes.generate_sticker_pdf import list_print_runs
from sleeve_notes_web._deps import templates
from sleeve_notes_web.routes.print_runs import _settings_summary
from sleeve_notes_web.services import stats
from sleeve_notes_web.services.deps import User, optional_user


router = APIRouter()


@router.get("/")
def index(request: Request, user: User | None = Depends(optional_user)):
    if user is None:
        return templates.TemplateResponse(
            request,
            "landing.html",
            {"request": request, "active": "landing"},
        )
    with dbmod.session() as conn:
        coverage = stats.collection_coverage(conn, user.id)
        new_count, last_print_ts = stats.new_since_last_print(conn, user.id)
        totals = stats.collection_totals(conn, user.id)
        runs = list_print_runs(conn, user.id)
    recent_runs = runs[:5]
    for r in recent_runs:
        r["settings_summary"] = _settings_summary(r["settings"]) if r["settings"] else None
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "active": "dashboard",
            "coverage": coverage,
            "new_count": new_count,
            "last_print_ts": last_print_ts,
            "totals": totals,
            "run_count": len(runs),
            "recent_runs": recent_runs,
        },
    )
